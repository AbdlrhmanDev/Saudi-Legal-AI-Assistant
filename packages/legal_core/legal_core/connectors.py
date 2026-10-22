"""Source connectors: discovery for each connector type, judicial ingestion and health reporting.

A connector only *discovers*: it returns links to official documents (or judicial records) with
their provenance. Whether anything becomes legal evidence is decided later by acquisition, parsing
and the deterministic release gate. Every outcome — including "the publisher blocked us" — is
recorded on the source row and in `source_sync_logs`; nothing is reported as synchronized unless the
connector actually read the source.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.models import JudicialDecision, SourceRegistry, utcnow
from legal_core.normalize import lexical_text
from legal_core.snapshots import store_snapshot
from legal_core.source_catalog import BOE_LAW_PATTERN

log = logging.getLogger(__name__)

MOJ_API = "https://laws-gateway.moj.gov.sa/apis/legislations/v1"
MOJ_JUDGMENT_PAGE = "https://laws.moj.gov.sa/ar/JudicialDecisionsList/0/{id}"
# The Ministry of Justice gateway sits behind a strict web-application firewall: a handful of quick
# requests are rejected. Its requests are spaced well apart.
HOST_INTERVALS = {"laws-gateway.moj.gov.sa": 6.0}

_GENERIC_ANCHORS = {
    "تنزيل", "تحميل", "عرض", "اضغط هنا", "المزيد", "اقرأ المزيد", "التفاصيل", "download", "view", "pdf", "more",
    "read more", "details",
}  # fmt: skip
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")


class ConnectorError(RuntimeError):
    pass


class HttpLike(Protocol):
    last_status: int | None

    def get(self, url: str) -> bytes: ...

    def post_json(self, url: str, payload: dict) -> bytes: ...

    def get_json(self, url: str) -> bytes: ...


class FetcherHttp:
    """Adapts a plain fetch function (fixtures, tests) to the connector interface."""

    def __init__(self, fetch: Callable[..., bytes]) -> None:
        self._fetch = fetch
        self.last_status: int | None = None

    def get(self, url: str) -> bytes:
        self.last_status = None
        body = self._fetch(url)
        self.last_status = 200
        return body

    def post_json(self, url: str, payload: dict) -> bytes:
        return self._fetch(url, payload) if _accepts_payload(self._fetch) else self._fetch(url)

    def get_json(self, url: str) -> bytes:
        return self._fetch(url)


def _accepts_payload(fn: Callable) -> bool:
    try:
        return fn.__code__.co_argcount >= 2  # type: ignore[attr-defined]
    except AttributeError:
        return False


@dataclass
class DiscoveredItem:
    external_id: str
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    document_ref: str | None = None  # URL of the official text to fetch (None: detection only)
    owner_key: str | None = None  # catalog source that owns this document (e.g. a BOE law page)


@dataclass
class JudicialRef:
    external_id: str
    title: str
    court_name: str | None = None
    court_type: int | None = None
    city: str | None = None
    case_number: str | None = None
    judgment_number: str | None = None
    hijri_date: str | None = None
    is_appeal: bool = False


@dataclass
class Discovery:
    items: list[DiscoveredItem] = field(default_factory=list)
    judicial: list[JudicialRef] = field(default_factory=list)
    fingerprint: str | None = None
    note: str | None = None
    skipped: int = 0  # feed entries without a link or title


# --- URL and text helpers --------------------------------------------------------------------


def canonical_document_url(url: str) -> str:
    """Stable identity for a document URL. BOE law pages are case-insensitive and appear in several
    spellings on sector sites; they are folded to one form."""
    from legal_core.monitor import canonicalize_url

    parts = urlsplit(url.strip())
    match = re.search(r"lawdetails/([0-9a-f-]{36})/1", parts.path.lower())
    if parts.hostname and parts.hostname.lower() == "laws.boe.gov.sa" and match:
        return f"https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/{match.group(1)}/1"
    return canonicalize_url(url)  # lower-cased scheme/host, no fragment, tracking parameters removed


def visible_text(html: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "form", "input"]):
        tag.decompose()
    return _WHITESPACE.sub(" ", soup.get_text(" ")).strip()


def page_fingerprint(html: bytes) -> str:
    """Hash of the visible text only: markup churn (tokens, build hashes) does not count as a change."""
    text = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", visible_text(html))  # clocks in page headers
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _title_from_filename(url: str) -> str:
    stem = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    stem = re.sub(r"\.(pdf|docx?)$", "", stem, flags=re.IGNORECASE)
    return _WHITESPACE.sub(" ", re.sub(r"[+_]", " ", stem)).strip()


def _anchor_title(anchor, url: str) -> str:
    text = _WHITESPACE.sub(" ", anchor.get_text(" ")).strip()
    if text and text.lower() not in _GENERIC_ANCHORS and len(text) >= 4:
        return text[:300]
    # A "download" button: the title is in the surrounding card or row.
    node = anchor
    for _level in range(4):
        node = node.parent
        # A container holding other links belongs to more than this document: its heading is not ours.
        if node is None or len(node.find_all("a", href=True)) > 1:
            break
        heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"])
        if heading is not None:
            candidate = _WHITESPACE.sub(" ", heading.get_text(" ")).strip()
            if len(candidate) >= 4 and candidate.lower() not in _GENERIC_ANCHORS:
                return candidate[:300]
        block = _WHITESPACE.sub(" ", node.get_text(" ")).strip()
        for generic in _GENERIC_ANCHORS:
            block = block.replace(generic, " ")
        block = re.sub(r"^\d{1,3}[\s.\-)]+", "", _WHITESPACE.sub(" ", block).strip())  # list numbering
        if 8 <= len(block) <= 300:
            return block
    return _title_from_filename(url) or url


def extract_listing_links(html: bytes, base_url: str, patterns: list[str]) -> list[tuple[str, str]]:
    """(canonical URL, title) for every link whose lower-cased absolute URL matches a pattern."""
    from bs4 import BeautifulSoup

    compiled = [re.compile(p) for p in patterns]
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if not absolute.startswith("https://"):
            continue
        lowered = unquote(absolute).lower()
        if not any(p.search(lowered) for p in compiled):
            continue
        url = canonical_document_url(absolute)
        title = _anchor_title(anchor, url)
        if url not in found or (len(title) > len(found[url]) and found[url] == _title_from_filename(url)):
            found[url] = title
    return list(found.items())


# --- discovery per connector type -------------------------------------------------------------


def _discover_rss(source: SourceRegistry, raw: bytes, is_fixture: bool) -> Discovery:
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ConnectorError(f"feed could not be parsed: {feed.bozo_exception}")
    discovery = Discovery()
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            discovery.skipped += 1
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        enclosures = entry.get("enclosures") or []
        url = canonical_document_url(link)
        if source.crawl_policy == "fixture":
            ref = enclosures[0].get("href") if enclosures else None
        elif source.crawl_policy == "fetch_allowed":
            ref = url
        else:
            ref = None  # feed_only: the official text has to be supplied another way
        discovery.items.append(
            DiscoveredItem(
                external_id=(entry.get("id") or url).strip(),
                url=url,
                title=title,
                summary=(entry.get("summary") or "").strip(),
                published_at=datetime(*stamp[:6], tzinfo=UTC) if stamp else None,
                document_ref=ref,
            )
        )
    return discovery


def _discover_listing(source: SourceRegistry, raw: bytes) -> Discovery:
    patterns = list((source.connector_config or {}).get("link_patterns") or [])
    discovery = Discovery(fingerprint=page_fingerprint(raw))
    for url, title in extract_listing_links(raw, source.feed_url, patterns):
        owner = "boe" if re.match(BOE_LAW_PATTERN, url.lower()) and source.source_key != "boe" else None
        discovery.items.append(
            DiscoveredItem(external_id=url, url=url, title=title, document_ref=url, owner_key=owner)
        )
    if not discovery.items:
        discovery.note = "the page was read but contains no server-rendered links matching this source's document rules"
    return discovery


def _discover_moj_judgments(http: HttpLike, settings: Settings, page_size: int) -> Discovery:
    raw = http.post_json(f"{MOJ_API}/Judgements/judgements-list", {"pageNumber": 1, "pageSize": page_size})
    data = _moj_json(raw)
    discovery = Discovery()
    for row in (data.get("model") or {}).get("judgementsCollection") or []:
        if not row.get("id"):
            continue
        discovery.judicial.append(
            JudicialRef(
                external_id=str(row["id"]),
                title=f"حكم رقم {row.get('judgementNumber') or ''}".strip(),
                court_name=row.get("courtName"),
                court_type=row.get("courtType"),
                city=row.get("city"),
                case_number=row.get("caseNumber"),
                judgment_number=row.get("judgementNumber"),
                hijri_date=row.get("judgementDate"),
                is_appeal=bool(row.get("isAppeal")),
            )
        )
    total = (data.get("model") or {}).get("totalCount")
    discovery.note = f"{total} judgments published" if total is not None else None
    return discovery


def _moj_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("the judgments API did not return JSON (request rejected?)") from exc
    if not isinstance(data, dict) or data.get("success") is False:
        raise ConnectorError(f"the judgments API reported an error: {str(data)[:200]}")
    return data


def discover(source: SourceRegistry, http: HttpLike, settings: Settings) -> Discovery:
    """Run the source's connector once. Raises on any failure; the caller records the outcome."""
    kind = source.connector_type
    if kind == "none":
        raise ConnectorError("no permitted automated access path is registered for this source")
    if kind == "moj_judgments_api":
        return _discover_moj_judgments(http, settings, settings.connector_max_judgments_per_run)
    raw = http.get(source.feed_url)
    if kind == "rss":
        return _discover_rss(source, raw, source.crawl_policy == "fixture")
    if kind == "html_listing":
        return _discover_listing(source, raw)
    if kind == "page_watch":
        return Discovery(fingerprint=page_fingerprint(raw))
    raise ConnectorError(f"unknown connector type: {kind}")


# --- judicial material -----------------------------------------------------------------------

_COURT_LEVEL_BY_TYPE = {1: "first_instance", 2: "appeal", 3: "supreme"}


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = _HTML_TAG.sub(" ", text)
    from html import unescape

    lines = [_WHITESPACE.sub(" ", line).strip() for line in unescape(text).splitlines()]
    return "\n".join(line for line in lines if line)


def store_moj_judgment(session: Session, source: SourceRegistry, ref: JudicialRef, raw: bytes) -> JudicialDecision:
    """Persist one judgment from the official detail payload (insert or update when its text changed)."""
    data = _moj_json(raw).get("model") or {}
    body = "\n\n".join(
        part
        for part in (
            html_to_text(data.get("judgmentFacts")),
            html_to_text(data.get("judgmentReasons")),
            html_to_text(data.get("judgmentRuling")),
            html_to_text(data.get("judgmentTextofRulling")),
        )
        if part
    )
    if not body:
        raise ConnectorError("the judgment detail has no published text")
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    page_url = MOJ_JUDGMENT_PAGE.format(id=ref.external_id)
    snapshot = store_snapshot(session, raw, canonical_url=page_url, source_id=source.id,
                              content_type="application/json", is_test_data=source.is_test_data)  # fmt: skip
    record = session.scalars(
        select(JudicialDecision).where(
            JudicialDecision.source_id == source.id, JudicialDecision.external_id == ref.external_id
        )
    ).first()
    if record is None:
        record = JudicialDecision(source_id=source.id, external_id=ref.external_id)
        session.add(record)
    elif record.content_hash == checksum:
        return record
    level = "appeal" if ref.is_appeal else _COURT_LEVEL_BY_TYPE.get(ref.court_type or 0, "unknown")
    title = (data.get("title") or ref.title or "").strip()
    record.title = title
    record.material_type = "judgment"
    record.court_name = ref.court_name
    record.court_level = level
    record.city = ref.city
    record.case_number = ref.case_number
    record.judgment_number = data.get("judgmentNumber") or ref.judgment_number
    record.hijri_date = data.get("judgmentHijriiDate") or ref.hijri_date
    record.judgment_date = _to_date(data.get("judgmentDate"))
    record.finality_status = "unknown"
    record.principles = []
    record.text = body
    record.source_url = page_url
    record.content_hash = checksum
    record.snapshot_id = snapshot.id
    record.retrieved_at = utcnow()
    record.lexical_text = lexical_text(f"{title}\n{ref.court_name or ''}\n{body}")
    record.is_test_data = source.is_test_data
    session.flush()
    return record


def store_judicial_page(
    session: Session, source: SourceRegistry, url: str, title: str, raw: bytes, text: str
) -> JudicialDecision:
    """A published collection of judgments or principles (Board of Grievances knowledge center)."""
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    snapshot = store_snapshot(session, raw, canonical_url=url, source_id=source.id, is_test_data=source.is_test_data)
    record = session.scalars(
        select(JudicialDecision).where(JudicialDecision.source_id == source.id, JudicialDecision.external_id == url)
    ).first()
    if record is None:
        record = JudicialDecision(source_id=source.id, external_id=url)
        session.add(record)
    elif record.content_hash == checksum:
        return record
    principles = "مبادئ" in title or "المبادئ" in title
    record.title = title
    record.material_type = "principles" if principles else "judgment"
    record.court_name = source.authority
    record.court_level = "supreme" if "العليا" in title else "unknown"
    record.finality_status = "unknown"
    record.principles = []
    record.text = text
    record.source_url = url
    record.content_hash = checksum
    record.snapshot_id = snapshot.id
    record.retrieved_at = utcnow()
    record.lexical_text = lexical_text(f"{title}\n{text}")
    record.is_test_data = source.is_test_data
    session.flush()
    return record


# --- health classification ---------------------------------------------------------------------


def classify_failure(exc: BaseException, status: int | None) -> tuple[str, str]:
    """Map a connector failure to (health_status, human-readable detail)."""
    from legal_core.monitor import RobotsDisallowed, SourcePolicyError

    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        if isinstance(item, RobotsDisallowed):
            return "robots_disallowed", "the publisher's robots.txt disallows automated access to this endpoint"
        if isinstance(item, httpx.HTTPStatusError):
            code = item.response.status_code
            if code in (401, 403):
                return "blocked", f"HTTP {code}: the publisher refused automated access (not bypassed)"
            if code == 429:
                return "blocked", "HTTP 429: rate limited by the publisher"
            return "unavailable", f"HTTP {code} from the publisher"
        if isinstance(item, SourcePolicyError) and "unsupported content type: text/html" in str(item):
            return "blocked", "the API answered with an HTML page instead of JSON (request rejected by the publisher)"
        if isinstance(item, SourcePolicyError):
            return "unavailable", f"refused by source policy: {item}"
        if isinstance(item, httpx.TimeoutException):
            return "unavailable", "the publisher did not respond in time"
        if isinstance(item, httpx.TransportError):
            return "unavailable", f"connection failed: {type(item).__name__}"
        if isinstance(item, ConnectorError) and "rejected" in str(item):
            return "blocked", str(item)
    if status in (401, 403, 429):
        return "blocked", f"HTTP {status}: the publisher refused automated access"
    return "unavailable", f"{type(exc).__name__}: {exc}"[:500]


def record_health(source: SourceRegistry, *, status: str, detail: str | None, http_status: int | None) -> None:
    source.health_status = status
    source.health_detail = (detail or "")[:1000] or None
    source.last_http_status = http_status
    source.last_probe_at = utcnow()


def make_official_http(settings: Settings) -> HttpLike:
    from legal_core.monitor import OfficialHttpClient

    return OfficialHttpClient(settings, min_interval_seconds=HOST_INTERVALS)


def probe_source(source: SourceRegistry, http: HttpLike, settings: Settings) -> dict:
    """Read a source once with its connector and record reachability. Does not create candidates."""
    if source.consolidated_into:
        record_health(source, status=source.health_status or "unknown",
                      detail=f"consolidated into {source.consolidated_into}", http_status=source.last_http_status)  # fmt: skip
        return {"source": source.name, "status": "consolidated", "into": source.consolidated_into}
    try:
        if source.connector_type == "none":
            # Reachability only (robots.txt still applies); nothing is harvested from this source.
            http.get(source.feed_url)
            detail = source.access_notes or "reachable; no permitted automated access path is registered"
            record_health(source, status="available", detail=f"reachable, not harvested: {detail}"[:1000],
                          http_status=http.last_status or 200)  # fmt: skip
            return {"source": source.name, "status": "available", "detail": detail, "http_status": http.last_status}
        discovery = discover(source, http, settings)
    except Exception as exc:  # every failure is reported, none is hidden
        status, detail = classify_failure(exc, http.last_status)
        record_health(source, status=status, detail=detail, http_status=http.last_status)
        return {"source": source.name, "status": status, "detail": detail, "http_status": http.last_status}
    found = len(discovery.items) + len(discovery.judicial)
    degraded = source.connector_type in ("html_listing", "moj_judgments_api", "rss") and found == 0
    status = "degraded" if degraded else "available"
    detail = discovery.note or (f"{found} item(s) discoverable" if found else "page readable; change detection active")
    record_health(source, status=status, detail=detail, http_status=http.last_status or 200)
    return {"source": source.name, "status": status, "detail": detail, "items": found, "http_status": http.last_status}

"""Official-source monitoring (FR-12..FR-14, lifecycle steps 1-4; PRD v2 steps 1-4).

A feed item is a detection signal only. It creates an update candidate and, where the source
policy permits, an immutable snapshot of the official text. Whether anything is published is
decided later by the deterministic release gate (legal_core.release), never here.
"""

import hashlib
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import urllib.robotparser
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import certifi
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.ingestion import DocumentMetadata, ingest_document
from legal_core.intent import infer_domain
from legal_core.models import (
    DOMAINS,
    NOT_FETCHED_CLASSIFICATIONS,
    JudicialDecision,
    LegalInstrument,
    SourceRegistry,
    SourceSyncLog,
    UpdateCandidate,
    deferred_backlog,
    utcnow,
)
from legal_core.normalize import lexical_tokens, normalize_ar
from legal_core.snapshots import store_snapshot

log = logging.getLogger(__name__)

Fetcher = Callable[[str], bytes]
T = TypeVar("T")
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$)")
_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_MAX_FEED_BYTES = 5 * 1024 * 1024
_INTERMEDIATES = Path(__file__).parent / "certs" / "intermediates.pem"
_ALLOWED_MIME = ("text/html", "application/xhtml+xml", "application/pdf", "text/plain", "application/rss+xml",
                 "application/atom+xml", "application/xml", "text/xml")  # fmt: skip

# Checked in order: an item about amending a law also contains the word "نظام", and a draft
# amendment is still a draft. Single words are matched as whole (light-stemmed) tokens, phrases
# as substrings.
_CLASS_RULES = (
    ("draft", ("مشروع نظام", "مشروع لايحه", "مشروع تعديل", "مشروع قواعد", "مسوده", "مرييات", "استطلاع")),
    ("amendment", ("تعديل", "تعديلات", "تعدل", "يعدل", "معدل", "استبدال", "تحذف", "تضاف")),
    ("repeal", ("الغاء ", "يلغي", "تلغي", "ملغي")),
    ("regulation", ("لايحه", "لوايح", "قواعد", "ضوابط", "دليل", "تنظيم", "ترتيبات تنظيميه")),
    ("new_law", ("نظام",)),
    ("royal_decree", ("مرسوم ملكي", "امر ملكي")),
    ("decision", ("قرار",)),
    ("circular", ("تعميم",)),
    ("notice", ("اعلان", "بيان")),
)
_ONE_LETTER_PREFIXES = "بلك"
# A title that only cites an instrument number says nothing about its subject.
_BARE_REFERENCE = re.compile(r"^(مرسوم ملكي|امر ملكي|قرار) (.* )?رقم ")
_OPERATIVE = ("يقرر", "رسمنا بما هو ات", "امرنا بما هو ات")
_LEAD_CHARS = 400


class MonitorError(RuntimeError):
    pass


class SourcePolicyError(MonitorError):
    """The request is not permitted by the source policy; retrying cannot help."""


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _match_rules(text: str) -> str:
    tokens = set(lexical_tokens(text, drop_stopwords=False))
    tokens |= {t[1:] for t in tokens if t[0] in _ONE_LETTER_PREFIXES and len(t) > 3}
    for label, keywords in _CLASS_RULES:
        if any((k in text) if " " in k else (k in tokens) for k in keywords):
            return label
    return "irrelevant"


def _operative_lead(summary: str) -> str:
    """The opening of the operative part, after the recitals ("بعد الاطلاع على ...")."""
    text = normalize_ar(summary)
    starts = [i + len(m) for m in _OPERATIVE if (i := text.find(m)) >= 0]
    return text[min(starts) if starts else 0 :][:_LEAD_CHARS]


def classify(title: str, summary: str = "") -> str:
    """The title decides. The body is read only when the title names no subject, and then only
    its operative opening: gazette items carry the full text, and a long text nearly always
    mentions an amendment somewhere."""
    head = normalize_ar(title)
    label = _match_rules(head)
    if summary and (label == "irrelevant" or _BARE_REFERENCE.match(head)):
        lead = _match_rules(_operative_lead(summary))
        if lead != "irrelevant":
            return lead
    return label


def content_fingerprint(title: str, summary: str) -> str:
    return hashlib.sha256(f"{normalize_ar(title)}\n{normalize_ar(summary)}".encode()).hexdigest()


def guess_target_instrument(session: Session, title: str, summary: str = "") -> uuid.UUID | None:
    """Potentially affected instrument: the longest known title quoted in the item's title or in
    the operative opening of its text. A suggestion only; the reviewer can change it."""
    haystack = f" {normalize_ar(title)} {_operative_lead(summary)} "
    best: tuple[int, uuid.UUID] | None = None
    for instrument_id, name in session.execute(select(LegalInstrument.id, LegalInstrument.official_title_ar)):
        phrase = " ".join(normalize_ar(_PARENS.sub(" ", name)).split())
        if phrase and f" {phrase} " in haystack and (best is None or len(phrase) > best[0]):
            best = (len(phrase), instrument_id)
    return best[1] if best else None


def fetch_with_retries(fetch: Fetcher, url: str, settings: Settings, on_retry: Callable[[str], None]) -> bytes:
    """Transient failures are retried with exponential backoff (NFR reliability)."""
    return with_retries(lambda: fetch(url), settings, on_retry)


def with_retries(call: Callable[[], T], settings: Settings, on_retry: Callable[[str], None]) -> T:
    last: Exception | None = None
    for attempt in range(settings.fetch_max_attempts):
        try:
            return call()
        except (httpx.HTTPError, OSError, MonitorError) as exc:
            last = exc
            client_error = (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < 500
                and exc.response.status_code != 429
            )
            if client_error or isinstance(exc, SourcePolicyError):
                break  # not transient
            if attempt + 1 < settings.fetch_max_attempts:
                on_retry(f"attempt {attempt + 1} failed: {type(exc).__name__}: {exc}")
                time.sleep(settings.fetch_backoff_seconds * (2**attempt))
    raise MonitorError(f"source unavailable after retries: {type(last).__name__}: {last}") from last


def tls_context() -> ssl.SSLContext:
    """Normal certificate verification against the standard roots. Some official sites do not
    send their intermediate certificate, so the known ones are supplied from a bundled file;
    each of them is itself issued by a standard root."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(_INTERMEDIATES))
    return context


def assert_public_host(host: str) -> None:
    """SSRF guard: every address the name resolves to must be publicly routable, so an allowlisted
    name can never be pointed at loopback, private ranges or cloud metadata endpoints."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise MonitorError(f"cannot resolve {host}: {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise SourcePolicyError(f"{host} resolves to a non-public address")


def check_official_url(url: str, settings: Settings) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in settings.allowlisted_hosts:
        raise SourcePolicyError(f"host not allowlisted: {host}")
    if parts.port not in (None, 443) or parts.username or parts.password:
        raise SourcePolicyError("explicit ports and credentials are not allowed in source URLs")
    return host


class RobotsDisallowed(SourcePolicyError):
    """The publisher's robots.txt forbids this URL for our agent."""


_JSON_MIME = ("application/json", "text/json")


class OfficialHttpClient:
    """HTTPS access restricted to allowlisted public hosts. Each redirect hop is re-validated; MIME
    type and size are checked while streaming; robots.txt and a per-host delay are honoured.

    `get` returns document bytes; `post_json` serves read-only JSON search APIs of official portals
    (same checks, JSON responses only). `last_status` keeps the final HTTP status for health reports.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resolve: Callable[[str], None] = assert_public_host,
        transport: httpx.BaseTransport | None = None,
        min_interval_seconds: dict[str, float] | None = None,
    ) -> None:
        self.settings = settings
        self._resolve = resolve
        self._transport = transport
        self._context = tls_context()
        self._limit = max(_MAX_FEED_BYTES, settings.max_upload_bytes)
        self._last_request: dict[str, float] = {}
        self._intervals = {k.lower(): v for k, v in (min_interval_seconds or {}).items()}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.last_status: int | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            verify=self._context,
            timeout=self.settings.fetch_timeout_seconds,
            headers={"User-Agent": self.settings.http_user_agent},
            follow_redirects=False,
            transport=self._transport,
        )

    def _polite(self, host: str) -> None:
        interval = max(self.settings.fetch_min_interval_seconds, self._intervals.get(host, 0.0))
        with self._lock:
            wait = self._last_request.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def _request(
        self, client: httpx.Client, method: str, url: str, *, allowed_mime: tuple[str, ...] | None, json_body=None
    ) -> bytes:
        for _hop in range(4):
            host = check_official_url(url, self.settings)
            self._resolve(host)
            self._polite(host)
            api = allowed_mime == _JSON_MIME
            headers = {"Accept": "application/json", "languageCode": "ar"} if api else None
            with client.stream(method, url, json=json_body, headers=headers) as resp:
                self.last_status = resp.status_code
                if resp.is_redirect:
                    if method != "GET":
                        raise SourcePolicyError("redirects are not followed for API requests")
                    url = str(resp.url.join(resp.headers.get("location", "")))
                    continue
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if allowed_mime and mime and mime not in allowed_mime:
                    raise SourcePolicyError(f"unsupported content type: {mime}")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._limit:
                    raise MonitorError("response too large")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._limit:
                        raise MonitorError("response too large")
                return bytes(body)
        raise SourcePolicyError("too many redirects")

    def _robots_allows(self, client: httpx.Client, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                body = self._request(client, "GET", f"https://{host}/robots.txt", allowed_mime=None)
                parser.parse(body.decode("utf-8", "replace").splitlines())
                self._robots[host] = parser
            except (httpx.HTTPError, MonitorError):
                self._robots[host] = None  # no readable robots.txt: no published restrictions
        parser = self._robots[host]
        return parser is None or parser.can_fetch(self.settings.http_user_agent, url)

    def _guarded(self, method: str, url: str, *, allowed_mime: tuple[str, ...], json_body=None) -> bytes:
        check_official_url(url, self.settings)
        self.last_status = None
        with self._client() as client:
            if self.settings.respect_robots_txt and not self._robots_allows(client, url):
                raise RobotsDisallowed(f"disallowed by robots.txt: {url}")
            return self._request(client, method, url, allowed_mime=allowed_mime, json_body=json_body)

    def get(self, url: str) -> bytes:
        return self._guarded("GET", url, allowed_mime=_ALLOWED_MIME)

    def post_json(self, url: str, payload: dict) -> bytes:
        return self._guarded("POST", url, allowed_mime=_JSON_MIME, json_body=payload)

    def get_json(self, url: str) -> bytes:
        return self._guarded("GET", url, allowed_mime=_JSON_MIME, json_body=None)


def make_http_fetcher(
    settings: Settings,
    *,
    resolve: Callable[[str], None] = assert_public_host,
    transport: httpx.BaseTransport | None = None,
) -> Fetcher:
    """HTTPS GET restricted to allowlisted public hosts (see OfficialHttpClient)."""
    return OfficialHttpClient(settings, resolve=resolve, transport=transport).get


def make_fixture_fetcher(settings: Settings) -> Fetcher:
    """Reads clearly-labelled test data from the fixtures directory (never real sources)."""
    root = settings.fixtures_dir.resolve()

    def fetch(ref: str) -> bytes:
        path = (root / ref).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise SourcePolicyError(f"fixture not found: {ref}")
        return path.read_bytes()

    return fetch


def _fetcher_for(source: SourceRegistry, settings: Settings) -> Fetcher:
    if source.crawl_policy == "fixture":
        if not settings.allow_fixture_sources:
            raise MonitorError("fixture sources are disabled (ALLOW_FIXTURE_SOURCES=false)")
        return make_fixture_fetcher(settings)
    if source.crawl_policy == "manual":
        raise MonitorError("source is manual-upload only")
    host = (urlsplit(source.feed_url).hostname or "").lower()
    if host != source.allowlisted_host.lower() or host not in settings.allowlisted_hosts:
        raise MonitorError(f"feed host is not on the source allowlist: {host}")
    return make_http_fetcher(settings)


def is_due(source: SourceRegistry, now: datetime) -> bool:
    return source.last_checked_at is None or now - source.last_checked_at >= timedelta(minutes=source.check_interval)


# Classifications that can never become binding text, so their documents are never fetched.
_NOT_FETCHED = NOT_FETCHED_CLASSIFICATIONS


def _classification_for(source: SourceRegistry, title: str, summary: str) -> str:
    """A consultation platform only publishes drafts and news/directories only publish notices,
    whatever their titles say; legislation sources are classified from the item itself."""
    if source.document_kind == "draft":
        return "draft"
    if source.document_kind in ("news", "directory", "archive"):
        return "notice"
    return classify(title, summary)


def _http_for(source: SourceRegistry, settings: Settings):
    from legal_core.connectors import FetcherHttp, make_official_http

    if source.crawl_policy == "fixture":
        if not settings.allow_fixture_sources:
            raise MonitorError("fixture sources are disabled (ALLOW_FIXTURE_SOURCES=false)")
        return FetcherHttp(make_fixture_fetcher(settings))
    if source.crawl_policy == "manual" and source.connector_type != "none":
        raise MonitorError("source is manual-upload only")
    host = (urlsplit(source.feed_url).hostname or "").lower()
    if host != source.allowlisted_host.lower() or host not in settings.allowlisted_hosts:
        raise MonitorError(f"feed host is not on the source allowlist: {host}")
    return make_official_http(settings)


def _owner_source(session: Session, key: str | None, cache: dict) -> SourceRegistry | None:
    if not key:
        return None
    if key not in cache:
        cache[key] = session.scalars(select(SourceRegistry).where(SourceRegistry.source_key == key)).first()
    return cache[key]


def _backlog(session: Session, source: SourceRegistry, limit: int) -> list[UpdateCandidate]:
    """Discovered documents whose fetch was deferred by the per-source politeness cap, oldest first."""
    if limit <= 0:
        return []
    return list(
        session.scalars(
            select(UpdateCandidate)
            .where(
                # Its own documents, and documents it found for another publisher (a ministry page
                # linking to the Bureau of Experts), so the backlog drains whichever source runs.
                (UpdateCandidate.source_id == source.id)
                | (UpdateCandidate.evidence["discovered_via"].as_string() == (source.source_key or "")),
                deferred_backlog(),
            )
            .order_by(UpdateCandidate.discovered_at, UpdateCandidate.id)
            .limit(limit)
        )
    )


def acquisition_budget(source: SourceRegistry, settings: Settings) -> int:
    """Documents one connector run may fetch: the source's own catalog limit (a daily gazette feed needs
    more than a 500-law archive), never above the global CONNECTOR_MAX_ACQUISITIONS_PER_SOURCE ceiling."""
    own = (source.connector_config or {}).get("max_items")
    ceiling = settings.connector_max_acquisitions_per_source
    return min(int(own), ceiling) if own else ceiling


def _refresh_due(candidate: UpdateCandidate, settings: Settings, now: datetime) -> bool:
    if candidate.proposed_version_id is None or candidate.state not in ("approved", "quarantined", "needs_review"):
        return False
    stamp = (candidate.evidence or {}).get("last_refreshed_at")
    last = datetime.fromisoformat(stamp) if stamp else candidate.discovered_at
    return now - last >= timedelta(days=settings.connector_refresh_days)


def _refresh_known_document(
    session, source, known: UpdateCandidate, title: str, settings, fetch, on_retry, counts, now
) -> None:
    """Re-read a consolidated law page we already hold. Changed official text becomes a new candidate
    (an amendment of the same instrument); unchanged text only records the check."""
    content = fetch_with_retries(fetch, known.document_ref or known.canonical_url, settings, on_retry)
    checksum = hashlib.sha256(content).hexdigest()
    counts["refreshed"] += 1
    version = known.proposed_version
    if version is not None and version.raw_hash == checksum:
        return
    if session.scalars(
        select(UpdateCandidate.id).where(
            UpdateCandidate.source_id == source.id,
            UpdateCandidate.canonical_url == known.canonical_url,
            UpdateCandidate.changed_hash == checksum,
        )
    ).first():
        return
    candidate = UpdateCandidate(
        source_id=source.id, origin="feed", external_id=known.external_id, canonical_url=known.canonical_url,
        title=title or known.title, summary="", changed_hash=checksum, classification=known.classification,
        target_instrument_id=version.instrument_id if version else known.target_instrument_id, state="candidate",
        is_test_data=source.is_test_data, document_ref=known.document_ref,
        evidence={"refresh_of": str(known.id), "last_refreshed_at": now.isoformat()},
    )  # fmt: skip
    session.add(candidate)
    session.flush()
    counts["changed"] += 1
    acquire(session, source, candidate, settings, fetch, on_retry, counts, content=content)


def _sync_judicial(session, source, discovery, http, settings, on_retry, counts) -> None:
    from legal_core.connectors import MOJ_API, store_moj_judgment

    budget = settings.connector_max_judgments_per_run
    known = set(
        session.scalars(
            select(JudicialDecision.external_id).where(
                JudicialDecision.source_id == source.id,
                JudicialDecision.external_id.in_([ref.external_id for ref in discovery.judicial] or [""]),
            )
        )
    )
    for ref in discovery.judicial:
        counts["seen"] += 1
        if ref.external_id in known:
            counts["unchanged"] += 1
            continue
        if counts["judicial_new"] + counts["judicial_failed"] >= budget:
            counts["deferred"] += 1
            continue
        url = f"{MOJ_API}/Judgements/get-details?id={quote(ref.external_id, safe='')}&lang=ar"
        try:
            with session.begin_nested():
                raw = with_retries(lambda u=url: http.get_json(u), settings, on_retry)
                store_moj_judgment(session, source, ref, raw)
            counts["judicial_new"] += 1
        except Exception as exc:  # one judgment must not abort the source
            log.warning("judgment_failed", extra={"external_id": ref.external_id, "error": type(exc).__name__})
            counts["judicial_failed"] += 1


def _sync_judicial_pages(session, source, discovery, fetch, settings, on_retry, counts) -> None:
    from legal_core.connectors import store_judicial_page, visible_text

    budget = acquisition_budget(source, settings)
    for item in discovery.items:
        counts["seen"] += 1
        exists = session.scalars(
            select(JudicialDecision.id).where(
                JudicialDecision.source_id == source.id, JudicialDecision.external_id == item.url
            )
        ).first()
        if exists:
            counts["unchanged"] += 1
            continue
        if counts["judicial_new"] + counts["judicial_failed"] >= budget:
            counts["deferred"] += 1
            continue
        try:
            with session.begin_nested():
                raw = fetch_with_retries(fetch, item.url, settings, on_retry)
                text = visible_text(raw) if not raw.startswith(b"%PDF") else ""
                if not text:
                    from legal_core.parser import extract_text

                    text = extract_text(raw, "pdf")
                store_judicial_page(session, source, item.url, item.title, raw, text)
            counts["judicial_new"] += 1
        except Exception as exc:
            log.warning("judicial_page_failed", extra={"url": item.url, "error": type(exc).__name__})
            counts["judicial_failed"] += 1


def sync_source(
    session: Session,
    source: SourceRegistry,
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
    http=None,
    on_retry: Callable[[str], None] = lambda _msg: None,
    now: datetime | None = None,
) -> dict:
    """Run one source's connector. Returns counts; raises MonitorError when the source cannot be read."""
    from legal_core.connectors import FetcherHttp, discover, record_health

    if not source.enabled:
        raise MonitorError("source is disabled")
    if source.consolidated_into:
        raise MonitorError(f"source is consolidated into {source.consolidated_into}")
    now = now or utcnow()
    if http is None:
        http = FetcherHttp(fetcher) if fetcher else _http_for(source, settings)
    fetch = fetcher or http.get
    is_fixture = source.crawl_policy == "fixture"
    counts = {
        "seen": 0, "new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "acquired": 0, "acquire_failed": 0,
        "duplicates_across_sources": 0, "deferred": 0, "refreshed": 0, "page_changed": 0, "judicial_new": 0,
        "judicial_failed": 0,
    }  # fmt: skip

    try:
        discovery = with_retries(lambda: discover(source, http, settings), settings, on_retry)
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError(f"{type(exc).__name__}: {exc}") from exc
    found = len(discovery.items) + len(discovery.judicial)
    empty_listing = source.connector_type in ("html_listing", "moj_judgments_api") and found == 0
    record_health(
        source,
        status="degraded" if empty_listing else "available",
        detail=discovery.note or f"{found} item(s) discovered",
        http_status=http.last_status or 200,
    )
    if discovery.fingerprint:
        if source.page_fingerprint and source.page_fingerprint != discovery.fingerprint:
            counts["page_changed"] = 1
        source.page_fingerprint = discovery.fingerprint
    counts["skipped"] += discovery.skipped
    counts["seen"] += discovery.skipped

    if discovery.judicial:
        _sync_judicial(session, source, discovery, http, settings, on_retry, counts)
        return counts
    if source.document_kind == "judicial":
        _sync_judicial_pages(session, source, discovery, fetch, settings, on_retry, counts)
        return counts

    # Documents deferred by an earlier run are fetched first, within this run's politeness budget.
    budget = 10**6 if is_fixture else acquisition_budget(source, settings)
    for candidate in _backlog(session, source, budget):
        acquire(session, candidate.source or source, candidate, settings, fetch, on_retry, counts)
        budget -= 1
    refresh_budget = settings.connector_max_refreshes_per_source
    owners: dict = {}

    for item in discovery.items:
        counts["seen"] += 1
        host = (urlsplit(item.url).hostname or "").lower()
        if not is_fixture and host not in settings.allowlisted_hosts:
            counts["skipped"] += 1  # an official page linking off-site is not a trusted publication URL
            continue
        owner = _owner_source(session, item.owner_key, owners) or source
        fingerprint = content_fingerprint(item.title, item.summary)

        # FR-13: identity = canonical URL / item id; change = content hash.
        known = session.scalars(
            select(UpdateCandidate)
            .where(
                UpdateCandidate.source_id == owner.id,
                (UpdateCandidate.external_id == item.external_id) | (UpdateCandidate.canonical_url == item.url),
            )
            .order_by(UpdateCandidate.discovered_at.desc())
        ).all()
        listing = source.connector_type == "html_listing"
        if any(k.changed_hash == fingerprint for k in known) or (listing and known):
            counts["unchanged"] += 1
            # A listing only names documents; amendments to a consolidated law appear in its page.
            # The held version, not a newer refresh attempt that was excluded or rejected.
            latest = next((k for k in known if k.proposed_version_id and k.state in ("approved", "quarantined", "needs_review")), known[0])
            if (
                listing
                and refresh_budget > 0
                and owner.document_kind in ("legislation", "gazette")
                and _refresh_due(latest, settings, now)
            ):
                refresh_budget -= 1
                # Stamped outside the savepoint: a permanently failing page waits its turn instead of
                # using the refresh budget of every later law on the listing.
                latest.evidence = {**(latest.evidence or {}), "last_refreshed_at": now.isoformat()}
                try:
                    with session.begin_nested():
                        _refresh_known_document(
                            session, owner, latest, item.title, settings, fetch, on_retry, counts, now
                        )
                except Exception as exc:
                    log.warning("refresh_failed", extra={"url": item.url, "error": type(exc).__name__})
                    counts["acquire_failed"] += 1
            continue
        # The same official page announced by another source (RSS + listing, or a ministry linking to
        # the Bureau of Experts) is processed once, by the source that first found it.
        if session.scalars(
            select(UpdateCandidate.id).where(
                UpdateCandidate.canonical_url == item.url, UpdateCandidate.source_id != owner.id
            )
        ).first():
            counts["duplicates_across_sources"] += 1
            continue

        candidate = UpdateCandidate(
            source_id=owner.id,
            origin="feed",
            external_id=item.external_id,
            canonical_url=item.url,
            title=item.title,
            summary=item.summary,
            published_at=item.published_at,
            changed_hash=fingerprint,
            classification=_classification_for(owner, item.title, item.summary),
            target_instrument_id=guess_target_instrument(session, item.title, item.summary),
            state="candidate",
            is_test_data=owner.is_test_data,
            document_ref=item.document_ref,
            evidence={"discovered_via": source.source_key} if owner is not source else {},
        )
        session.add(candidate)
        session.flush()
        counts["changed" if known else "new"] += 1

        # Drafts, notices and unrelated items are never fetched: they cannot become binding text.
        if candidate.classification in _NOT_FETCHED or not candidate.document_ref:
            continue
        if budget <= 0:
            counts["deferred"] += 1  # fetched by a later run (backlog)
            continue
        budget -= 1
        acquire(session, owner, candidate, settings, fetch, on_retry, counts)

    return counts


def acquire(session, source, candidate, settings, fetch, on_retry, counts, *, content: bytes | None = None) -> None:
    """Lifecycle steps 2-4: take an immutable snapshot of the official text when the source policy
    permits automated retrieval, then parse it. A failure is recorded on the candidate and retried by
    a later run; it never aborts the run or touches the corpus."""
    ref = candidate.document_ref
    if not ref:
        return
    try:
        with session.begin_nested():
            if content is None:
                content = fetch_with_retries(fetch, ref, settings, on_retry)
            published = candidate.published_at.date() if candidate.published_at else None
            snapshot = store_snapshot(
                session,
                content,
                canonical_url=candidate.canonical_url,
                source_id=source.id,
                published_at=published,
                is_test_data=source.is_test_data,
            )
            candidate.state = "retrieved"
            # A repeal decision names the instrument it ends; it is stored as its own notice.
            repeal = candidate.classification == "repeal"
            domain = source.default_domain if source.default_domain in DOMAINS else None
            ingest_document(
                session,
                content=content,
                filename=Path(urlsplit(ref).path).name or None,
                mime_type=None,
                meta=DocumentMetadata(
                    title_ar=candidate.title if repeal else None,
                    domain=infer_domain(candidate.title) or (domain if domain != "other" else None),
                    instrument_type="notice" if repeal else None,
                    source_url=candidate.canonical_url,
                    issuing_authority=source.authority,
                    published_at=published,
                    candidate_id=candidate.id,
                    is_test_data=source.is_test_data,
                    standalone=repeal,
                ),
                settings=settings,
                snapshot_id=snapshot.id,
            )
        counts["acquired"] += 1
    except Exception as exc:  # one bad document must not abort the run or touch the corpus
        log.warning("acquire_failed", extra={"candidate_id": str(candidate.id), "error": type(exc).__name__})
        candidate.state, candidate.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        counts["acquire_failed"] += 1


def sync_sources(
    session: Session,
    settings: Settings,
    *,
    source_id: uuid.UUID | None = None,
    force: bool = False,
    fetcher: Fetcher | None = None,
    on_retry: Callable[[str], None] = lambda _msg: None,
    run_id: uuid.UUID | None = None,
) -> tuple[dict, list[str]]:
    """Run every enabled, due source. One failing source does not stop the others; each execution is
    logged in source_sync_logs with what it saw and, on failure, why."""
    from legal_core.connectors import classify_failure, record_health

    stmt = (
        select(SourceRegistry)
        .where(SourceRegistry.enabled.is_(True), SourceRegistry.consolidated_into.is_(None))
        .order_by(SourceRegistry.name)
    )
    if source_id:
        stmt = select(SourceRegistry).where(SourceRegistry.id == source_id)
    sources = list(session.scalars(stmt))
    if source_id and not sources:
        raise MonitorError("source not found")
    totals: dict = {"sources_checked": 0, "sources_failed": 0}
    errors: list[str] = []
    now = utcnow()
    for source in sources:
        if not force and not is_due(source, now):
            continue
        totals["sources_checked"] += 1
        source.last_checked_at = now
        log_row = SourceSyncLog(source_id=source.id, sync_run_id=run_id, started_at=utcnow(), status="failed",
                                connector_type=source.connector_type, counts={})  # fmt: skip
        try:
            with session.begin_nested():
                counts = sync_source(session, source, settings, fetcher=fetcher, on_retry=on_retry, now=now)
            source.last_success_at, source.last_error = now, None
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            changed = any(counts.get(k) for k in ("new", "changed", "acquired", "page_changed", "judicial_new"))
            log_row.status, log_row.counts = ("changed" if changed else "no_change"), counts
            log_row.http_status = source.last_http_status
        except Exception as exc:
            source.last_error = f"{type(exc).__name__}: {exc}"[:500]
            status, detail = classify_failure(exc, None)
            record_health(source, status=status, detail=detail, http_status=_status_of(exc))
            totals["sources_failed"] += 1
            errors.append(f"{source.name}: {source.last_error}")
            log_row.error, log_row.http_status = f"{status}: {detail}"[:1000], source.last_http_status
        log_row.finished_at = utcnow()
        session.add(log_row)
        session.flush()
    return totals, errors


def _status_of(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code
        current = current.__cause__
    return None

"""Official-source monitoring (FR-12..FR-14, lifecycle steps 1-4).

A feed item is a detection signal only. It creates an update candidate for human
review and never changes what users are told.
"""

import hashlib
import logging
import re
import ssl
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.ingestion import DocumentMetadata, ingest_document
from legal_core.models import LegalInstrument, SourceRegistry, UpdateCandidate, utcnow
from legal_core.normalize import lexical_tokens, normalize_ar

log = logging.getLogger(__name__)

Fetcher = Callable[[str], bytes]
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$)")
_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_MAX_FEED_BYTES = 5 * 1024 * 1024
_INTERMEDIATES = Path(__file__).parent / "certs" / "intermediates.pem"

# Checked in order: an item about amending a law also contains the word "نظام".
# Single words are matched as whole (light-stemmed) tokens, phrases as substrings.
_CLASS_RULES = (
    ("amendment", ("تعديل", "تعديلات", "تعدل", "يعدل", "معدل", "استبدال", "تحذف", "تضاف")),
    ("regulation", ("لايحه", "لوايح", "قواعد", "ضوابط", "دليل")),
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
    last: Exception | None = None
    for attempt in range(settings.fetch_max_attempts):
        try:
            return fetch(url)
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
    raise MonitorError(f"source unavailable after retries: {type(last).__name__}: {last}")


def tls_context() -> ssl.SSLContext:
    """Normal certificate verification against the standard roots. Some official sites do not
    send their intermediate certificate, so the known ones are supplied from a bundled file;
    each of them is itself issued by a standard root."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(_INTERMEDIATES))
    return context


def make_http_fetcher(settings: Settings) -> Fetcher:
    context = tls_context()

    def fetch(url: str) -> bytes:
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme != "https" or host not in settings.allowlisted_hosts:
            raise SourcePolicyError(f"host not allowlisted: {host}")
        with httpx.Client(
            verify=context,
            timeout=settings.fetch_timeout_seconds,
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
            max_redirects=3,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if (resp.url.host or "").lower() not in settings.allowlisted_hosts:
                raise SourcePolicyError(f"redirected to a host that is not allowlisted: {resp.url.host}")
            if len(resp.content) > max(_MAX_FEED_BYTES, settings.max_upload_bytes):
                raise MonitorError("response too large")
            return resp.content

    return fetch


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


def _entry_time(entry) -> datetime | None:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*stamp[:6], tzinfo=UTC) if stamp else None


def sync_source(
    session: Session,
    source: SourceRegistry,
    settings: Settings,
    *,
    fetcher: Fetcher | None = None,
    on_retry: Callable[[str], None] = lambda _msg: None,
) -> dict:
    """Poll one source. Returns counts; raises MonitorError when the source cannot be read."""
    if not source.enabled:
        raise MonitorError("source is disabled")
    fetch = fetcher or _fetcher_for(source, settings)
    is_fixture = source.crawl_policy == "fixture"
    counts = {"seen": 0, "new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "acquired": 0, "acquire_failed": 0}

    raw = fetch_with_retries(fetch, source.feed_url, settings, on_retry)
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise MonitorError(f"feed could not be parsed: {feed.bozo_exception}")

    for entry in feed.entries:
        counts["seen"] += 1
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        summary = (entry.get("summary") or "").strip()
        if not link or not title:
            counts["skipped"] += 1
            continue
        canonical = canonicalize_url(link)
        host = (urlsplit(canonical).hostname or "").lower()
        if not is_fixture and host not in settings.allowlisted_hosts:
            counts["skipped"] += 1  # an official feed linking off-site is not a trusted publication URL
            continue
        external_id = (entry.get("id") or canonical).strip()
        fingerprint = content_fingerprint(title, summary)

        # FR-13: identity = canonical URL / item id; change = content hash.
        known = session.scalars(
            select(UpdateCandidate).where(
                UpdateCandidate.source_id == source.id,
                (UpdateCandidate.external_id == external_id) | (UpdateCandidate.canonical_url == canonical),
            )
        ).all()
        if any(k.changed_hash == fingerprint for k in known):
            counts["unchanged"] += 1
            continue

        candidate = UpdateCandidate(
            source_id=source.id,
            origin="feed",
            external_id=external_id,
            canonical_url=canonical,
            title=title,
            summary=summary,
            published_at=_entry_time(entry),
            changed_hash=fingerprint,
            classification=classify(title, summary),
            target_instrument_id=guess_target_instrument(session, title, summary),
            state="candidate",
            is_test_data=source.is_test_data,
        )
        session.add(candidate)
        session.flush()
        counts["changed" if known else "new"] += 1

        if candidate.classification != "irrelevant":
            _acquire(session, source, candidate, entry, settings, fetch, on_retry, counts)

    return counts


def _acquire(session, source, candidate, entry, settings, fetch, on_retry, counts) -> None:
    """Lifecycle step 3: take an immutable snapshot of the official text when the source policy
    permits automated retrieval. Otherwise the candidate waits for a manual admin upload."""
    if source.crawl_policy == "fixture":
        enclosures = entry.get("enclosures") or []
        ref = enclosures[0].get("href") if enclosures else None
    elif source.crawl_policy == "fetch_allowed":
        ref = candidate.canonical_url
    else:
        ref = None
    if not ref:
        return
    try:
        with session.begin_nested():
            content = fetch_with_retries(fetch, ref, settings, on_retry)
            candidate.state = "retrieved"
            ingest_document(
                session,
                content=content,
                filename=Path(urlsplit(ref).path).name or None,
                mime_type=None,
                meta=DocumentMetadata(
                    source_url=candidate.canonical_url,
                    issuing_authority=source.authority,
                    published_at=candidate.published_at.date() if candidate.published_at else None,
                    candidate_id=candidate.id,
                    is_test_data=source.is_test_data,
                ),
                settings=settings,
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
) -> tuple[dict, list[str]]:
    """Poll every enabled, due source. One failing source does not stop the others."""
    stmt = select(SourceRegistry).where(SourceRegistry.enabled.is_(True)).order_by(SourceRegistry.name)
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
        try:
            with session.begin_nested():
                counts = sync_source(session, source, settings, fetcher=fetcher, on_retry=on_retry)
            source.last_success_at, source.last_error = now, None
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
        except Exception as exc:
            source.last_error = f"{type(exc).__name__}: {exc}"[:500]
            totals["sources_failed"] += 1
            errors.append(f"{source.name}: {source.last_error}")
        session.flush()
    return totals, errors

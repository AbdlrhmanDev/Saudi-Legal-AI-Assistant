"""Question answering pipeline: retrieve -> gate evidence -> draft -> validate -> trace (PRD 9).

Scaling notes: the database connection is returned to the pool before the (slow) model call, and
answers are cached per process under the active index generation, so a corpus update invalidates
the cache by construction.
"""

import copy
import hashlib
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.freshness import cached_freshness
from legal_core.generations import active_generation_id
from legal_core.llm import ANSWER_STATUSES, AnswerModel, Draft, Evidence, quote_evidence
from legal_core.models import DocumentVersion, LegalInstrument, QueryTrace
from legal_core.normalize import normalize_ar
from legal_core.official_services import suggest_official_services
from legal_core.retrieval import RetrievedArticle, mentioned_article_numbers, retrieve
from legal_core.temporal import forthcoming_after

log = logging.getLogger(__name__)


class AnswerCache:
    """Thread-safe LRU with TTL. Keys include the active generation id, so entries for an older
    corpus are never served after an update is activated."""

    def __init__(self) -> None:
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple, ttl: float) -> dict | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if time.monotonic() - hit[0] > ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(hit[1])

    def put(self, key: tuple, value: dict, size: int) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), copy.deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


answer_cache = AnswerCache()

RIYADH = timezone(timedelta(hours=3))

DISCLAIMER_AR = (
    "هذه معلومات عامة مستندة إلى النصوص المفهرسة فقط وليست استشارة قانونية. "
    "للحالات الخاصة يُنصح بالرجوع إلى محامٍ مرخص."
)
INSUFFICIENT_AR = "لم أجد في المصادر القانونية المفهرسة نصًا ساريًا يجيب عن هذا السؤال."
ERROR_AR = "تعذّر إكمال الطلب حالياً. لم تُقدَّم أي إجابة نظامية؛ يُرجى المحاولة لاحقاً."

_PENALTY_TERMS = ("عقوبه", "عقوبات", "غرامه", "جزاء")


def _order_evidence_for_question(query: str, items: list[RetrievedArticle]) -> list[RetrievedArticle]:
    """Prefer operative provisions over issuance recitals and definitions.

    Retrieval establishes relevance; this only orders already-strong passages so the offline
    extractive fallback does not quote a royal-decree recital when an actual penalty article is
    available.
    """
    if not items:
        return items
    operative = [item for item in items if normalize_ar(item.article_label or "") != "اداه الاصدار"]
    items = operative or items
    penalty_question = any(term in normalize_ar(query) for term in _PENALTY_TERMS)

    def key(item: RetrievedArticle) -> tuple[int, int, float, int]:
        text = normalize_ar(item.text)
        # A criminal penalty provision is normally the best direct answer to a broad
        # "what is the penalty?" question.  Give imprisonment and an express crime
        # reference more weight than administrative closure language so an ancillary
        # violation article cannot outrank the principal offence provision merely by
        # having a slightly better lexical rank.
        penalty_detail = (
            4 * ("سجن" in text)
            + 2 * ("غرامه" in text)
            + 2 * ("جريمه" in text)
            + 2 * ("مصادره" in text)
            + ("اغلاق" in text)
        )
        return (
            penalty_detail if penalty_question else 0,
            item.lexical_matches,
            item.lexical_coverage,
            -(item.lexical_rank or 10**9),
        )

    return sorted(items, key=key, reverse=True)


def today_riyadh() -> date:
    return datetime.now(RIYADH).date()


def _evidence_text(item: RetrievedArticle, settings: Settings) -> str:
    """Whole article when it fits; otherwise the matching sub-section, never a mid-sentence cut."""
    return item.text if len(item.text) <= settings.max_evidence_chars else item.best_chunk_text


def _validate(draft: Draft, evidence: list[Evidence], query: str | None = None) -> str | None:
    """Return a reason when the draft violates the grounding contract (PRD 9.1 step 7)."""
    by_id = {ev.eid: ev for ev in evidence}
    if draft.answer_status not in ANSWER_STATUSES:
        return "answer_status is not one of the allowed values"
    if draft.answer_status == "insufficient_evidence":
        return None
    if not draft.answer_ar.strip():
        return "answer_ar is empty"
    unknown = [c for c in draft.citation_ids if c not in by_id]
    if unknown:
        return f"citations {unknown} are not in the evidence set"
    if not draft.citation_ids:
        return "a grounded or partial answer must cite at least one evidence id"
    # Article numbers named in the prose must come from cited passages (or be quoted inside them).
    allowed = {by_id[c].article_number for c in draft.citation_ids if by_id[c].article_number}
    for c in draft.citation_ids:
        allowed |= mentioned_article_numbers(by_id[c].text)
    stray = mentioned_article_numbers(draft.answer_ar) - allowed
    if stray:
        return f"answer mentions article numbers {sorted(stray)} that are not supported by the cited evidence"
    # The official traffic fine table separates roads posted at 120 or less from roads
    # posted at 140. A vague speeding question must not silently select one branch.
    # This only applies when both branch labels are present in the retrieved evidence.
    normalized_query = normalize_ar(query or "")
    evidence_text = "\n".join(ev.text for ev in evidence)
    speed_table = "السرعة من 120" in evidence_text and "السرعة 140" in evidence_text
    unspecified_road_limit = "120" not in (query or "") and "140" not in (query or "")
    if "سرع" in normalized_query and speed_table and unspecified_road_limit:
        if "120" not in draft.answer_ar or "140" not in draft.answer_ar:
            return "answer must cover both posted-speed branches (120-or-less and 140) found in the evidence"
        if "30" in (query or ""):
            required_amounts = {"800", "1000", "1200", "1500", "2000"}
            missing_amounts = sorted(amount for amount in required_amounts if amount not in draft.answer_ar)
            if missing_amounts:
                return (
                    "answer to an unbounded over-30 query must include every matching fine range; "
                    f"missing values {missing_amounts}"
                )
        if draft.answer_status != "partial" or not draft.caveats:
            return "answer depends on the unspecified posted road speed and must be partial with a caveat"
    return None


def _draft_with_validation(
    llm: AnswerModel, query: str, as_of: date, evidence: list[Evidence]
) -> tuple[Draft, list[str], str | None]:
    """One attempt, one retry with validator feedback, then a conservative verbatim quotation."""
    notes: list[str] = []
    feedback = None
    for attempt in range(2):
        try:
            draft = llm.draft(query, as_of, evidence, feedback)
        except Exception as exc:
            log.warning("llm_failed", extra={"error": type(exc).__name__, "attempt": attempt})
            notes.append(f"llm_error:{type(exc).__name__}")
            break
        problem = _validate(draft, evidence, query)
        if problem is None:
            return draft, notes, None
        notes.append(f"validation_failed:{problem}")
        feedback = f"Your previous output was rejected: {problem}. Fix it and follow the rules exactly."
    quoted, ids = quote_evidence(evidence, limit=1, question=query)
    fallback = Draft(
        answer_ar=f"تعذّر توليد صياغة موثوقة، وهذا هو النص ذو الصلة كما ورد في المصدر:\n\n{quoted}",
        answer_status="partial",
        citation_ids=ids,
        caveats=["عُرض النص حرفياً دون تفسير لتعذّر التحقق من الصياغة المولّدة."],
    )
    return fallback, notes, "; ".join(notes)


def _citation(item: RetrievedArticle, as_of: date) -> dict:
    return {
        "law_id": str(item.instrument_id),
        "instrument_id": str(item.instrument_id),
        "article_id": str(item.article_id),
        "version_id": str(item.version_id),
        "source_url": item.source_url,
        "official_url": item.source_url,
        "law_title_ar": item.law_title_ar,
        "article_number": item.article_number,
        "article_label": item.article_label,
        "chapter": item.chapter,
        "quote": item.text,
        "effective_from": item.effective_from.isoformat(),
        "effective_to": item.effective_to.isoformat() if item.effective_to else None,
        "legal_status": "active",  # retrieval only returns versions in force on as_of_date
        "is_test_data": item.is_test_data,
    }


def _forthcoming_versions(session: Session, instrument_ids: set[uuid.UUID], as_of: date) -> list[dict]:
    """Approved versions that start after `as_of`, loaded before the model call."""
    if not instrument_ids:
        return []
    rows = session.execute(
        select(DocumentVersion, LegalInstrument)
        .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(DocumentVersion.instrument_id.in_(instrument_ids), forthcoming_after(as_of))
        .order_by(DocumentVersion.effective_from)
    ).all()
    return [
        {
            "law_id": str(instrument.id),
            "law_title_ar": instrument.official_title_ar,
            "version_id": str(version.id),
            "effective_from": version.effective_from.isoformat(),
            "legal_status": "scheduled",
        }
        for version, instrument in rows
    ]


def _temporal_notes(
    cited: list[RetrievedArticle], as_of: date, today: date, upcoming: list[dict]
) -> tuple[list[str], list[dict]]:
    caveats: list[str] = []
    if as_of != today:
        caveats.append(f"الإجابة مبنية على النصوص السارية بتاريخ {as_of.isoformat()} وليس بتاريخ اليوم.")
    for item in {c.version_id: c for c in cited}.values():
        if item.effective_to is not None:
            caveats.append(
                f"نص «{item.law_title_ar}» المستشهد به سارٍ حتى {item.effective_to.isoformat()} ثم حلّت محله نسخة أخرى."
            )
    cited_laws = {str(c.instrument_id) for c in cited}
    forthcoming = [f for f in upcoming if f["law_id"] in cited_laws]
    for item in forthcoming:
        caveats.append(
            f"توجد نسخة معتمدة من «{item['law_title_ar']}» يبدأ سريانها في "
            f"{item['effective_from']}؛ وهي غير سارية بتاريخ {as_of.isoformat()} ولم تُستخدم في الإجابة."
        )
    return caveats, forthcoming


def _freshness_fields(session: Session, settings: Settings) -> dict:
    try:
        report = cached_freshness(session, settings)
    except Exception:  # freshness is informative; it must never break an answer
        log.exception("freshness_unavailable")
        session.rollback()
        return {"corpus_verified_at": None, "coverage_notice": "Selected Saudi legal sources; not exhaustive",
                "source_health": "degraded", "freshness_warning_ar": None}  # fmt: skip
    return {
        "corpus_verified_at": report["corpus_verified_at"],
        "coverage_notice": report["coverage_notice"],
        "source_health": report["source_health"],
        "freshness_warning_ar": report["warning_ar"],
    }


def answer_question(
    session: Session,
    *,
    query: str,
    domain: str | None,
    as_of_date: date | None,
    settings: Settings,
    embedder: Embedder,
    llm: AnswerModel,
) -> dict:
    started = time.perf_counter()
    trace_id = uuid.uuid4()
    today = today_riyadh()
    as_of = as_of_date or today
    timings: dict[str, int] = {}
    retrieved: list[RetrievedArticle] = []
    retrieved_trace: list[dict] = []
    cited: list[RetrievedArticle] = []
    cited_ids: list[str] = []
    caveats: list[str] = []
    forthcoming: list[dict] = []
    error: str | None = None
    failed = False
    language = None
    answer_ar, status = INSUFFICIENT_AR, "insufficient_evidence"
    citations: list[dict] = []
    freshness = _freshness_fields(session, settings)
    # Service discovery is deliberately independent of legal evidence. A useful official
    # transaction may be shown even when the corpus cannot support a legal answer.
    related_services = suggest_official_services(
        query,
        live=settings.official_service_search_enabled and settings.environment != "test",
        timeout_seconds=settings.official_service_timeout_seconds,
    )

    cache_key = None
    cached = None
    if settings.answer_cache_size > 0:
        cache_key = (
            "structured-answer-v9",
            normalize_ar(query),
            domain,
            as_of,
            today,
            active_generation_id(session),
            llm.model_name,
            embedder.model_name,
        )
        cached = answer_cache.get(cache_key, settings.answer_cache_ttl_seconds)

    if cached is not None:
        answer_ar, status, citations = cached["answer_ar"], cached["answer_status"], cached["citations"]
        caveats, forthcoming, language = cached["caveats"], cached["forthcoming"], cached["language"]
        retrieved_trace, cited_ids = cached["retrieved"], cached["cited_ids"]
        timings["cache_hit"] = 1
    else:
        try:
            t = time.perf_counter()
            result = retrieve(session, query=query, as_of=as_of, domain=domain, embedder=embedder, settings=settings)
            timings["retrieval"] = int((time.perf_counter() - t) * 1000)
            retrieved, language = result.items, result.language
            strong = _order_evidence_for_question(query, [r for r in retrieved if r.strong])
            hinted_ids = {uuid.UUID(value) for value in result.instrument_hints}
            upcoming = _forthcoming_versions(session, hinted_ids | {r.instrument_id for r in strong}, as_of)
            # Everything the answer needs from the database is loaded: hand the connection back to
            # the pool so a slow model call does not hold it (the trace write takes a new one).
            session.commit()

            if strong:
                evidence = [
                    Evidence(
                        eid=f"E{i}",
                        law_title_ar=item.law_title_ar,
                        article_label=item.article_label,
                        article_number=item.article_number,
                        text=_evidence_text(item, settings),
                        effective_from=item.effective_from,
                        effective_to=item.effective_to,
                        lexical_coverage=item.lexical_coverage,
                    )
                    for i, item in enumerate(strong, start=1)
                ]
                by_eid = dict(zip((ev.eid for ev in evidence), strong, strict=True))
                t = time.perf_counter()
                draft, _notes, error = _draft_with_validation(llm, query, as_of, evidence)
                timings["generation"] = int((time.perf_counter() - t) * 1000)

                if draft.answer_status != "insufficient_evidence":
                    answer_ar, status = draft.answer_ar.strip(), draft.answer_status
                    cited = [by_eid[c] for c in dict.fromkeys(draft.citation_ids)]
                    caveats.extend(draft.caveats)
                    # Two reviewed versions of one instrument in force on the same date is a data
                    # conflict: qualify the answer instead of silently choosing one.
                    versions_per_law: dict[uuid.UUID, set[uuid.UUID]] = {}
                    for item in strong:
                        versions_per_law.setdefault(item.instrument_id, set()).add(item.version_id)
                    if any(len(v) > 1 for v in versions_per_law.values()):
                        status = "partial"
                        caveats.append("وُجدت نسختان معتمدتان متعارضتان سارّيتان في التاريخ نفسه؛ أُحيل الأمر للمراجعة.")
                    # A question with a single content word cannot identify one rule: qualify the answer.
                    if len(result.query_tokens) <= 1 and not result.article_hints:
                        status = "partial"
                        caveats.append("السؤال عام؛ يُرجى تحديد النظام أو الموضوع المقصود للحصول على إجابة أدق.")
                    temporal, forthcoming = _temporal_notes(cited, as_of, today, upcoming)
                    caveats.extend(temporal)
            elif upcoming:
                forthcoming = upcoming
                first = upcoming[0]
                answer_ar = (
                    f"يوجد نص معتمد بعنوان «{first['law_title_ar']}»، لكنه غير نافذ بتاريخ "
                    f"{as_of.isoformat()}. يبدأ سريانه في {first['effective_from']}، ولذلك لم يُستخدم "
                    "للإجابة عن الحكم الساري في التاريخ المحدد."
                )
                caveats.append("يمكن تغيير تاريخ السؤال إلى تاريخ النفاذ أو ما بعده للاطلاع على النص بعد سريانه.")
            if any(r.quarantined for r in retrieved):
                caveats.append("استُبعد مقطع من المصادر لاحتوائه على تعليمات موجهة إلى المساعد وليس نصاً نظامياً.")
            if result.degraded:
                caveats.append("البحث الدلالي غير متاح مؤقتاً؛ اعتمدت النتائج على البحث النصي فقط.")
            if language == "en" and status != "insufficient_evidence":
                caveats.append("النص العربي الرسمي هو المرجع الحاكم.")
        except Exception as exc:  # never return a half-built legal answer
            log.exception("chat_pipeline_failed")
            session.rollback()
            error = f"{type(exc).__name__}"
            failed = True
            answer_ar, status, cited, caveats, forthcoming = ERROR_AR, "insufficient_evidence", [], [], []
        citations = [_citation(c, as_of) for c in cited]
        retrieved_trace = [r.trace() for r in retrieved]
        cited_ids = [str(c.article_id) for c in cited]
        caveats = list(dict.fromkeys(caveats))
        # Only clean, complete answers are cached; degraded or failed ones are recomputed next time.
        if cache_key is not None and not failed and error is None and not (retrieved and result.degraded):
            answer_cache.put(
                cache_key,
                {
                    "answer_ar": answer_ar, "answer_status": status, "citations": citations, "caveats": caveats,
                    "forthcoming": forthcoming, "language": language, "retrieved": retrieved_trace,
                    "cited_ids": cited_ids,
                },  # fmt: skip
                settings.answer_cache_size,
            )

    if freshness["source_health"] != "healthy" and freshness["freshness_warning_ar"] and status != "insufficient_evidence":
        caveats = [*caveats, freshness["freshness_warning_ar"]]

    latency_ms = int((time.perf_counter() - started) * 1000)
    timings["total"] = latency_ms
    session.add(
        QueryTrace(
            id=trace_id,
            query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            query_text=query if settings.trace_store_query_text else None,
            query_language=language,
            domain=domain,
            as_of_date=as_of,
            retrieved=retrieved_trace,
            retrieved_version_ids=sorted({r["version_id"] for r in retrieved_trace}),
            cited_article_ids=cited_ids,
            response_status="error" if failed else status,
            error=error,
            latency_ms=latency_ms,
            timings_ms=timings,
            llm_model=llm.model_name,
            embedding_model=embedder.model_name,
            auto_delete_at=datetime.now(UTC) + timedelta(days=settings.trace_retention_days),
        )
    )
    session.commit()

    return {
        "answer_ar": answer_ar,
        "answer_status": status,
        "as_of_date": as_of.isoformat(),
        "citations": citations,
        "caveats": caveats,
        "trace_id": str(trace_id),
        "disclaimer_ar": DISCLAIMER_AR,
        "forthcoming": forthcoming,
        "corpus_verified_at": freshness["corpus_verified_at"],
        "coverage_notice": freshness["coverage_notice"],
        "source_health": freshness["source_health"],
        "related_services": related_services,
    }

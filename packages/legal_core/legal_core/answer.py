"""Question answering pipeline: retrieve -> gate evidence -> draft -> validate -> trace (PRD 9)."""

import hashlib
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.llm import ANSWER_STATUSES, AnswerModel, Draft, Evidence, quote_evidence
from legal_core.models import DocumentVersion, LegalInstrument, QueryTrace
from legal_core.retrieval import RetrievedArticle, mentioned_article_numbers, retrieve
from legal_core.temporal import forthcoming_after

log = logging.getLogger(__name__)

RIYADH = timezone(timedelta(hours=3))

DISCLAIMER_AR = (
    "هذه معلومات عامة مستندة إلى النصوص المفهرسة فقط وليست استشارة قانونية. "
    "للحالات الخاصة يُنصح بالرجوع إلى محامٍ مرخص."
)
INSUFFICIENT_AR = (
    "لا تتوفر في المصادر المفهرسة نصوص سارية كافية للإجابة عن هذا السؤال. "
    "تغطية المساعد تقتصر على مجموعة محددة من الأنظمة، ولا يمكن ذكر حكم نظامي دون سند من نص معتمد."
)
ERROR_AR = "تعذّر إكمال الطلب حالياً. لم تُقدَّم أي إجابة نظامية؛ يُرجى المحاولة لاحقاً."


def today_riyadh() -> date:
    return datetime.now(RIYADH).date()


def _evidence_text(item: RetrievedArticle, settings: Settings) -> str:
    """Whole article when it fits; otherwise the matching sub-section, never a mid-sentence cut."""
    return item.text if len(item.text) <= settings.max_evidence_chars else item.best_chunk_text


def _validate(draft: Draft, evidence: list[Evidence]) -> str | None:
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
        problem = _validate(draft, evidence)
        if problem is None:
            return draft, notes, None
        notes.append(f"validation_failed:{problem}")
        feedback = f"Your previous output was rejected: {problem}. Fix it and follow the rules exactly."
    quoted, ids = quote_evidence(evidence, limit=1)
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
        "article_id": str(item.article_id),
        "version_id": str(item.version_id),
        "source_url": item.source_url,
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


def _temporal_notes(session: Session, cited: list[RetrievedArticle], as_of: date, today: date) -> tuple[list[str], list[dict]]:
    caveats: list[str] = []
    forthcoming: list[dict] = []
    if as_of != today:
        caveats.append(f"الإجابة مبنية على النصوص السارية بتاريخ {as_of.isoformat()} وليس بتاريخ اليوم.")
    for item in {c.version_id: c for c in cited}.values():
        if item.effective_to is not None:
            caveats.append(
                f"نص «{item.law_title_ar}» المستشهد به سارٍ حتى {item.effective_to.isoformat()} ثم حلّت محله نسخة أخرى."
            )
    instrument_ids = {c.instrument_id for c in cited}
    if instrument_ids:
        rows = session.execute(
            select(DocumentVersion, LegalInstrument)
            .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
            .where(DocumentVersion.instrument_id.in_(instrument_ids), forthcoming_after(as_of))
            .order_by(DocumentVersion.effective_from)
        ).all()
        for version, instrument in rows:
            forthcoming.append(
                {
                    "law_id": str(instrument.id),
                    "law_title_ar": instrument.official_title_ar,
                    "version_id": str(version.id),
                    "effective_from": version.effective_from.isoformat(),
                    "legal_status": "scheduled",
                }
            )
            caveats.append(
                f"توجد نسخة معتمدة من «{instrument.official_title_ar}» يبدأ سريانها في "
                f"{version.effective_from.isoformat()}؛ وهي غير سارية بتاريخ {as_of.isoformat()} ولم تُستخدم في الإجابة."
            )
    return caveats, forthcoming


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
    cited: list[RetrievedArticle] = []
    caveats: list[str] = []
    forthcoming: list[dict] = []
    error: str | None = None
    failed = False
    language = None
    answer_ar, status = INSUFFICIENT_AR, "insufficient_evidence"

    try:
        t = time.perf_counter()
        result = retrieve(session, query=query, as_of=as_of, domain=domain, embedder=embedder, settings=settings)
        timings["retrieval"] = int((time.perf_counter() - t) * 1000)
        retrieved, language = result.items, result.language
        strong = [r for r in retrieved if r.strong]

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
                temporal, forthcoming = _temporal_notes(session, cited, as_of, today)
                caveats.extend(temporal)
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
            retrieved=[r.trace() for r in retrieved],
            retrieved_version_ids=sorted({str(r.version_id) for r in retrieved}),
            cited_article_ids=[str(c.article_id) for c in cited],
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
        "citations": [_citation(c, as_of) for c in cited],
        "caveats": list(dict.fromkeys(caveats)),
        "trace_id": str(trace_id),
        "disclaimer_ar": DISCLAIMER_AR,
        "forthcoming": forthcoming,
    }

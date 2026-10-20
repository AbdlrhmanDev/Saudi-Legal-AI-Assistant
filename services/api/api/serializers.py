from datetime import date, datetime

from legal_core.models import (
    AmendmentRelation,
    Article,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    LegalInstrument,
    QuarantineEvent,
    QueryTrace,
    ReviewEvent,
    SourceRegistry,
    SyncRun,
    UpdateCandidate,
    User,
)
from legal_core.temporal import legal_status


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _id(value) -> str | None:
    return str(value) if value else None


def user(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "name": u.name, "role": u.role, "last_login_at": _iso(u.last_login_at)}


def instrument(i: LegalInstrument) -> dict:
    return {
        "id": str(i.id),
        "official_title_ar": i.official_title_ar,
        "domain": i.domain,
        "issuing_authority": i.issuing_authority,
        "instrument_type": i.instrument_type,
        "canonical_source_url": i.canonical_source_url,
        "current_review_status": i.current_review_status,
        "is_test_data": i.is_test_data,
    }


def version(v: DocumentVersion, as_of: date, *, admin: bool = False) -> dict:
    data = {
        "id": str(v.id),
        "instrument_id": str(v.instrument_id),
        "version_label": v.version_label,
        "source_url": v.source_url,
        "source_authority": v.source_authority,
        "issued_at": _iso(v.issued_at),
        "published_at": _iso(v.published_at),
        "effective_from": _iso(v.effective_from),
        "effective_to": _iso(v.effective_to),
        "end_reason": v.end_reason,
        "legal_status": legal_status(v, as_of),
        "status_as_of": as_of.isoformat(),
        "article_count": v.article_count,
        "retrieved_at": _iso(v.retrieved_at),
        "raw_hash": v.raw_hash,
        "is_test_data": v.is_test_data,
    }
    if admin:
        data.update(
            {
                "review_status": v.review_status,
                "index_active": v.index_active,
                "original_filename": v.original_filename,
                "mime_type": v.mime_type,
                "metadata_gaps": v.metadata_gaps,
                "parse_warnings": v.parse_warnings,
                "instrument_title_ar": v.instrument.official_title_ar,
                "instrument_type": v.instrument.instrument_type,
                "issuing_authority": v.instrument.issuing_authority,
                "domain": v.instrument.domain,
            }
        )
    return data


def article(a: Article, *, with_text: bool = True) -> dict:
    data = {
        "id": str(a.id),
        "version_id": str(a.version_id),
        "article_number": a.article_number,
        "article_label": a.article_label,
        "chapter": a.chapter,
        "ordering": a.ordering,
    }
    if with_text:
        data["original_text_ar"] = a.original_text_ar
    return data


def candidate(c: UpdateCandidate, *, detail: bool = False) -> dict:
    data = {
        "id": str(c.id),
        "origin": c.origin,
        "source_id": _id(c.source_id),
        "source_name": c.source.name if c.source else None,
        "external_id": c.external_id,
        "canonical_url": c.canonical_url,
        "title": c.title,
        "published_at": _iso(c.published_at),
        "discovered_at": _iso(c.discovered_at),
        "changed_hash": c.changed_hash,
        "classification": c.classification,
        "classification_source": c.classification_source,
        "target_instrument_id": _id(c.target_instrument_id),
        "target_instrument_title_ar": c.target_instrument.official_title_ar if c.target_instrument else None,
        "proposed_version_id": _id(c.proposed_version_id),
        "state": c.state,
        "error": c.error,
        "is_test_data": c.is_test_data,
        "diff_summary": (c.diff or {}).get("summary"),
        "affected_article_numbers": (c.diff or {}).get("affected_article_numbers", []),
    }
    if detail:
        data["summary"] = c.summary
        data["diff"] = c.diff
        data["evidence"] = c.evidence
        data["sync_run_id"] = _id(c.sync_run_id)
    return data


def review_event(e: ReviewEvent) -> dict:
    return {
        "id": str(e.id),
        "candidate_id": _id(e.candidate_id),
        "version_id": _id(e.version_id),
        "actor_id": _id(e.actor_id),
        "decision": e.decision,
        "decision_at": _iso(e.decision_at),
        "reason": e.reason,
        "effective_date_evidence": e.effective_date_evidence,
        "details": e.details,
        "sync_run_id": _id(e.sync_run_id),
    }


def job(j: IngestionJob) -> dict:
    return {
        "id": str(j.id),
        "job_type": j.job_type,
        "status": j.status,
        "created_at": _iso(j.created_at),
        "started_at": _iso(j.started_at),
        "finished_at": _iso(j.finished_at),
        "retry_count": j.retry_count,
        "errors": j.errors,
        "counts": j.counts,
        "result": j.result,
    }


def source(s: SourceRegistry) -> dict:
    return {
        "id": str(s.id),
        "name": s.name,
        "authority": s.authority,
        "feed_url": s.feed_url,
        "allowlisted_host": s.allowlisted_host,
        "enabled": s.enabled,
        "crawl_policy": s.crawl_policy,
        "check_interval": s.check_interval,
        "last_checked_at": _iso(s.last_checked_at),
        "last_success_at": _iso(s.last_success_at),
        "last_error": s.last_error,
        "is_test_data": s.is_test_data,
    }


def trace(t: QueryTrace, *, detail: bool = False) -> dict:
    data = {
        "id": str(t.id),
        "query_time": _iso(t.query_time),
        "as_of_date": _iso(t.as_of_date),
        "domain": t.domain,
        "query_language": t.query_language,
        "response_status": t.response_status,
        "latency_ms": t.latency_ms,
        "error": t.error,
        "retrieved_count": len(t.retrieved),
        "cited_article_ids": t.cited_article_ids,
        "retrieved_version_ids": t.retrieved_version_ids,
    }
    if detail:
        data.update(
            {
                "query_hash": t.query_hash,
                "query_text": t.query_text,
                "retrieved": t.retrieved,
                "timings_ms": t.timings_ms,
                "llm_model": t.llm_model,
                "embedding_model": t.embedding_model,
                "auto_delete_at": _iso(t.auto_delete_at),
            }
        )
    return data


def sync_run(r: SyncRun) -> dict:
    return {
        "id": str(r.id),
        "run_key": r.run_key,
        "logical_date_riyadh": _iso(r.logical_date_riyadh),
        "trigger": r.trigger,
        "status": r.status,
        "scheduled_at": _iso(r.scheduled_at),
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "retry_count": r.retry_count,
        "retry_after": _iso(r.retry_after),
        "stage_counts": r.stage_counts,
        "error_code": r.error_code,
        "errors": r.errors,
    }


def generation(g: IndexGeneration, *, detail: bool = False) -> dict:
    data = {
        "id": str(g.id),
        "number": g.number,
        "trigger": g.trigger,
        "validation_status": g.validation_status,
        "is_active": g.is_active,
        "built_at": _iso(g.built_at),
        "activated_at": _iso(g.activated_at),
        "retired_at": _iso(g.retired_at),
        "parent_id": _id(g.parent_id),
        "sync_run_id": _id(g.sync_run_id),
        "version_count": g.version_count,
        "article_count": g.article_count,
        "changed_chunk_count": g.changed_chunk_count,
        "reused_chunk_count": g.reused_chunk_count,
        "health_ok": bool(g.health.get("ok")) if g.health else None,
        "note": g.note,
    }
    if detail:
        data.update({"health": g.health, "manifest": g.manifest})
    return data


def quarantine(q: QuarantineEvent) -> dict:
    c = q.candidate
    return {
        "id": str(q.id),
        "candidate_id": str(q.candidate_id),
        "candidate_title": c.title if c else None,
        "classification": c.classification if c else None,
        "canonical_url": c.canonical_url if c else None,
        "sync_run_id": _id(q.sync_run_id),
        "version_id": _id(q.version_id),
        "source_snapshot_id": _id(q.source_snapshot_id),
        "reason_code": q.reason_code,
        "detail": q.detail,
        "source_evidence": q.source_evidence,
        "created_at": _iso(q.created_at),
        "retry_after": _iso(q.retry_after),
        "resolution_state": q.resolution_state,
        "resolved_at": _iso(q.resolved_at),
        "is_test_data": c.is_test_data if c else False,
    }


def amendment(a: AmendmentRelation, target: LegalInstrument, version: DocumentVersion, as_of: date) -> dict:
    return {
        "id": str(a.id),
        "change_type": a.change_type,
        "law_id": str(target.id),
        "law_title_ar": target.official_title_ar,
        "affected_article_number": a.affected_article_number,
        "version_id": str(version.id),
        "effective_from": _iso(a.effective_from),
        "legal_status": "repealed_from" if a.change_type == "repealed" else legal_status(version, as_of),
        "published_at": _iso(version.published_at),
        "source_url": version.source_url,
        "publication_evidence": a.publication_evidence,
        "date_evidence": a.date_evidence,
        "recorded_at": _iso(a.created_at),
        "is_test_data": version.is_test_data,
    }

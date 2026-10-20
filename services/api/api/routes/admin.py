"""Admin / reviewer API. Every route requires the admin role, enforced server-side (FR-19)."""

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, or_, select

from api import serializers
from api.deps import Admin, ApiError, Db, EmbedderDep, PageDep, SettingsDep, ok, require_admin
from api.schemas import (
    ApproveRequest,
    Classification,
    DocumentMetadataPatch,
    Domain,
    IndexRollbackRequest,
    RejectRequest,
    RollbackRequest,
    SourcePatch,
    SyncRequest,
    UpdateCandidatePatch,
)
from legal_core.answer import answer_cache, today_riyadh
from legal_core.freshness import clear_cache, freshness_report
from legal_core.ingestion import IngestionError, _metadata_gaps, validate_source_url
from legal_core.jobs import enqueue
from legal_core.models import (
    CANDIDATE_STATES,
    INSTRUMENT_TYPES,
    JOB_STATUSES,
    JOB_TYPES,
    QUARANTINE_RESOLUTIONS,
    REVIEW_STATUSES,
    SYNC_STATUSES,
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
    WorkerHeartbeat,
    utcnow,
)
from legal_core.normalize import normalize_ar
from legal_core.parser import ExtractionError, sniff_kind
from legal_core.review import (
    APPROVABLE_STATES,
    ReviewError,
    approve_candidate,
    reject_candidate,
    rollback_generation,
    rollback_version,
    update_candidate,
)
from legal_core.snapshots import store_snapshot
from legal_core.sync import trigger_manual_run

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])

EVALUATION_REPORT = Path("tests/evaluation/results/latest.json")


def _one_of(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise ApiError(422, "VALIDATION_ERROR", f"{field} must be one of {list(allowed)}.")


def _review(call):
    try:
        return call()
    except ReviewError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from None


# --- documents ---------------------------------------------------------------


@router.post(
    "/documents",
    status_code=201,
    summary="Upload a PDF/HTML/text document, or register an allowlisted official URL",
    description=(
        "Creates an ingestion job. The parsed version enters the review queue; it is not searchable "
        "until approved. Missing metadata is flagged for review and never inferred."
    ),
)
async def upload_document(
    db: Db,
    admin: Admin,
    settings: SettingsDep,
    file: Annotated[UploadFile | None, File(description="PDF, HTML or UTF-8 text")] = None,
    register_url: Annotated[str | None, Form(max_length=2000, description="Official HTML page to fetch instead of a file")] = None,
    title_ar: Annotated[str | None, Form(max_length=500)] = None,
    domain: Annotated[Domain | None, Form()] = None,
    instrument_type: Annotated[str | None, Form(max_length=40)] = None,
    issuing_authority: Annotated[str | None, Form(max_length=300)] = None,
    source_url: Annotated[str | None, Form(max_length=2000)] = None,
    version_label: Annotated[str | None, Form(max_length=200)] = None,
    issued_at: Annotated[date | None, Form()] = None,
    published_at: Annotated[date | None, Form()] = None,
    effective_from: Annotated[date | None, Form()] = None,
    instrument_id: Annotated[uuid.UUID | None, Form()] = None,
    candidate_id: Annotated[uuid.UUID | None, Form()] = None,
    is_test_data: Annotated[bool, Form()] = False,
) -> dict:
    _one_of(instrument_type, INSTRUMENT_TYPES, "instrument_type")
    if (file is None) == (register_url is None):
        raise ApiError(422, "VALIDATION_ERROR", "Provide exactly one of `file` or `register_url`.")
    if instrument_id and db.get(LegalInstrument, instrument_id) is None:
        raise ApiError(422, "VALIDATION_ERROR", "instrument_id does not exist.")
    if candidate_id and db.get(UpdateCandidate, candidate_id) is None:
        raise ApiError(422, "VALIDATION_ERROR", "candidate_id does not exist.")
    try:
        source_url = validate_source_url(source_url)
        payload: dict
        if file is not None:
            content = await file.read(settings.max_upload_bytes + 1)
            if len(content) > settings.max_upload_bytes:
                raise ApiError(422, "FILE_TOO_LARGE", f"Maximum upload size is {settings.max_upload_bytes} bytes.")
            if not content:
                raise ApiError(422, "VALIDATION_ERROR", "The uploaded file is empty.")
            kind = sniff_kind(content, file.filename, file.content_type)
            snapshot = store_snapshot(
                db, content, canonical_url=None, content_type=f"{kind}", is_test_data=is_test_data
            )
            payload = {
                # Immutable snapshot in PostgreSQL: the worker needs no access to this API's disk.
                "snapshot_id": str(snapshot.id),
                # Display only; the stored path is server-generated.
                "filename": Path(file.filename or "").name[:255] or None,
                "mime_type": file.content_type,
            }
        else:
            url = validate_source_url(register_url)
            host = (urlsplit(url).hostname or "").lower()
            if urlsplit(url).scheme != "https" or host not in settings.allowlisted_hosts:
                raise ApiError(422, "SOURCE_NOT_ALLOWLISTED", "register_url must be an https URL on an approved official host.")
            payload = {"fetch_url": url, "filename": None, "mime_type": "text/html"}
            source_url = source_url or url
    except (IngestionError, ExtractionError) as exc:
        raise ApiError(422, "INVALID_DOCUMENT", str(exc)) from None

    payload["meta"] = {
        "title_ar": title_ar,
        "domain": domain,
        "instrument_type": instrument_type,
        "issuing_authority": issuing_authority,
        "source_url": source_url,
        "version_label": version_label,
        "issued_at": issued_at.isoformat() if issued_at else None,
        "published_at": published_at.isoformat() if published_at else None,
        "effective_from": effective_from.isoformat() if effective_from else None,
        "instrument_id": str(instrument_id) if instrument_id else None,
        "candidate_id": str(candidate_id) if candidate_id else None,
        "is_test_data": is_test_data,
    }
    job = enqueue(db, "document_ingest", payload, admin.id)
    return ok({"job_id": str(job.id), "status": job.status}, "Ingestion job queued.")


@router.get("/documents", summary="Corpus catalog: every version, including unreviewed ones")
def list_documents(
    db: Db,
    page: PageDep,
    review_status: Annotated[str | None, Query(max_length=20)] = None,
    instrument_id: uuid.UUID | None = None,
    domain: Domain | None = None,
) -> dict:
    _one_of(review_status, REVIEW_STATUSES, "review_status")
    today = today_riyadh()
    stmt = select(DocumentVersion).join(LegalInstrument).order_by(DocumentVersion.retrieved_at.desc())
    if review_status:
        stmt = stmt.where(DocumentVersion.review_status == review_status)
    if instrument_id:
        stmt = stmt.where(DocumentVersion.instrument_id == instrument_id)
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    return ok(page.run(db, stmt, lambda v: serializers.version(v, today, admin=True)))


@router.get("/documents/{version_id}", summary="One version with parsed articles and review history")
def get_document(version_id: uuid.UUID, db: Db) -> dict:
    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Version not found.")
    events = db.scalars(
        select(ReviewEvent).where(ReviewEvent.version_id == version.id).order_by(ReviewEvent.decision_at)
    ).all()
    return ok(
        {
            "version": serializers.version(version, today_riyadh(), admin=True),
            "law": serializers.instrument(version.instrument),
            "articles": [serializers.article(a) for a in version.articles],
            "review_events": [serializers.review_event(e) for e in events],
        }
    )


@router.patch("/documents/{version_id}", summary="Correct metadata on an unapproved document version")
def patch_document(
    version_id: uuid.UUID, body: DocumentMetadataPatch, db: Db, settings: SettingsDep
) -> dict:
    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Version not found.")
    if version.review_status not in APPROVABLE_STATES or version.index_active:
        raise ApiError(409, "IMMUTABLE_VERSION", "Only an unapproved or quarantined version can be edited.")
    if not body.model_fields_set:
        raise ApiError(422, "VALIDATION_ERROR", "Nothing to update.")

    instrument = version.instrument
    if "instrument_type" in body.model_fields_set:
        _one_of(body.instrument_type, INSTRUMENT_TYPES, "instrument_type")
    if "source_url" in body.model_fields_set:
        try:
            body.source_url = validate_source_url(body.source_url)
        except IngestionError as exc:
            raise ApiError(422, "INVALID_DOCUMENT", str(exc)) from None
    if "title_ar" in body.model_fields_set and body.title_ar:
        normalized = normalize_ar(body.title_ar)
        conflict = db.scalars(
            select(LegalInstrument).where(
                LegalInstrument.title_normalized == normalized,
                LegalInstrument.id != instrument.id,
            )
        ).first()
        if conflict:
            raise ApiError(409, "TITLE_CONFLICT", "Another legal instrument already uses this title.")
        instrument.official_title_ar = body.title_ar
        instrument.title_normalized = normalized
    if "domain" in body.model_fields_set and body.domain is not None:
        instrument.domain = body.domain
    if "instrument_type" in body.model_fields_set and body.instrument_type is not None:
        instrument.instrument_type = body.instrument_type
    if "issuing_authority" in body.model_fields_set:
        instrument.issuing_authority = body.issuing_authority
        version.source_authority = body.issuing_authority
    if "source_url" in body.model_fields_set:
        version.source_url = body.source_url
        instrument.canonical_source_url = body.source_url
    for field in ("version_label", "issued_at", "published_at", "effective_from"):
        if field in body.model_fields_set:
            setattr(version, field, getattr(body, field))

    candidates = db.scalars(select(UpdateCandidate).where(UpdateCandidate.proposed_version_id == version.id)).all()
    for candidate in candidates:
        candidate.title = instrument.official_title_ar
        candidate.canonical_url = version.source_url
        candidate.target_instrument_id = instrument.id
    version.metadata_gaps = _metadata_gaps(version, instrument, settings)
    db.flush()
    return ok(serializers.version(version, today_riyadh(), admin=True), "Document metadata updated.")


@router.delete("/documents/{version_id}", summary="Delete an unapproved draft version")
def delete_document(version_id: uuid.UUID, db: Db) -> dict:
    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Version not found.")
    if version.review_status != "needs_review" or version.index_active:
        raise ApiError(409, "IMMUTABLE_VERSION", "Only an unapproved version awaiting review can be deleted.")
    candidates = db.scalars(select(UpdateCandidate).where(UpdateCandidate.proposed_version_id == version.id)).all()
    candidate_ids = [candidate.id for candidate in candidates]
    event_filters = [ReviewEvent.version_id == version.id]
    if candidate_ids:
        event_filters.append(ReviewEvent.candidate_id.in_(candidate_ids))
    if db.scalar(select(ReviewEvent.id).where(or_(*event_filters)).limit(1)):
        raise ApiError(409, "AUDIT_HISTORY_EXISTS", "This version has review history and cannot be deleted.")

    instrument = version.instrument
    for candidate in candidates:
        db.delete(candidate)
    db.delete(version)
    db.flush()
    remaining = db.scalar(
        select(func.count()).select_from(DocumentVersion).where(DocumentVersion.instrument_id == instrument.id)
    ) or 0
    if remaining == 0:
        db.delete(instrument)
    return ok({"id": str(version_id)}, "Draft document deleted.")


@router.post("/versions/{version_id}/rollback", summary="Roll back index activation of an approved version")
def rollback(version_id: uuid.UUID, body: RollbackRequest, db: Db, admin: Admin) -> dict:
    event = _review(lambda: rollback_version(db, version_id, actor_id=admin.id, reason=body.reason))
    return ok(serializers.review_event(event), "Version rolled back; history preserved.")


@router.post("/versions/{version_id}/reindex", status_code=201, summary="Rebuild the search index of an approved version")
def reindex(version_id: uuid.UUID, db: Db, admin: Admin) -> dict:
    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Version not found.")
    if version.review_status != "approved":
        raise ApiError(409, "INVALID_STATE", "Only approved versions can be re-indexed.")
    job = enqueue(db, "reindex", {"version_id": str(version.id)}, admin.id)
    return ok({"job_id": str(job.id), "status": job.status}, "Re-index job queued.")


# --- jobs --------------------------------------------------------------------


@router.get("/jobs", summary="Ingestion, feed-sync and re-index runs")
def list_jobs(
    db: Db,
    page: PageDep,
    status: Annotated[str | None, Query(max_length=20)] = None,
    job_type: Annotated[str | None, Query(max_length=30)] = None,
) -> dict:
    _one_of(status, JOB_STATUSES, "status")
    _one_of(job_type, JOB_TYPES, "job_type")
    stmt = select(IngestionJob).order_by(IngestionJob.created_at.desc())
    if status:
        stmt = stmt.where(IngestionJob.status == status)
    if job_type:
        stmt = stmt.where(IngestionJob.job_type == job_type)
    return ok(page.run(db, stmt, serializers.job))


@router.get("/jobs/{job_id}", summary="Job status")
def get_job(job_id: uuid.UUID, db: Db) -> dict:
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Job not found.")
    return ok(serializers.job(job))


# --- update candidates ---------------------------------------------------------


@router.get("/updates", summary="Update candidates and review queue")
def list_updates(
    db: Db,
    page: PageDep,
    state: Annotated[str | None, Query(max_length=20)] = None,
    classification: Classification | None = None,
) -> dict:
    _one_of(state, CANDIDATE_STATES, "state")
    stmt = select(UpdateCandidate).order_by(UpdateCandidate.discovered_at.desc())
    if state:
        stmt = stmt.where(UpdateCandidate.state == state)
    if classification:
        stmt = stmt.where(UpdateCandidate.classification == classification)
    return ok(page.run(db, stmt, serializers.candidate))


@router.post(
    "/updates/sync",
    status_code=201,
    summary="Discovery-only scan of enabled sources (no publication; see /sync-runs/trigger)",
)
def sync_updates(db: Db, admin: Admin, body: SyncRequest | None = None) -> dict:
    source_id = body.source_id if body else None
    if source_id and db.get(SourceRegistry, source_id) is None:
        raise ApiError(404, "NOT_FOUND", "Source not found.")
    job = enqueue(db, "feed_sync", {"source_id": str(source_id) if source_id else None, "force": True}, admin.id)
    return ok({"job_id": str(job.id), "status": job.status}, "Source scan queued.")


@router.get("/updates/{candidate_id}", summary="Candidate detail: publication metadata, diff, review history")
def get_update(candidate_id: uuid.UUID, db: Db) -> dict:
    candidate = db.get(UpdateCandidate, candidate_id)
    if candidate is None:
        raise ApiError(404, "NOT_FOUND", "Update candidate not found.")
    events = db.scalars(
        select(ReviewEvent).where(ReviewEvent.candidate_id == candidate.id).order_by(ReviewEvent.decision_at)
    ).all()
    version = candidate.proposed_version
    return ok(
        {
            "candidate": serializers.candidate(candidate, detail=True),
            "proposed_version": serializers.version(version, today_riyadh(), admin=True) if version else None,
            "review_events": [serializers.review_event(e) for e in events],
        }
    )


@router.patch("/updates/{candidate_id}", summary="Override classification or affected instrument")
def patch_update(candidate_id: uuid.UUID, body: UpdateCandidatePatch, db: Db, admin: Admin) -> dict:
    if body.classification is None and body.target_instrument_id is None:
        raise ApiError(422, "VALIDATION_ERROR", "Nothing to update.")
    candidate = _review(
        lambda: update_candidate(
            db,
            candidate_id,
            actor_id=admin.id,
            classification=body.classification,
            target_instrument_id=body.target_instrument_id,
            reason=body.reason,
        )
    )
    return ok(serializers.candidate(candidate, detail=True))


@router.post("/updates/{candidate_id}/approve", summary="Validate and approve; indexes the version")
def approve(
    candidate_id: uuid.UUID, body: ApproveRequest, db: Db, admin: Admin, settings: SettingsDep, embedder: EmbedderDep
) -> dict:
    event = _review(
        lambda: approve_candidate(
            db,
            candidate_id,
            actor_id=admin.id,
            effective_from=body.effective_from,
            effective_date_evidence=body.effective_date_evidence,
            published_at=body.published_at,
            reason=body.reason,
            acknowledge_warnings=body.acknowledge_warnings,
            embedder=embedder,
            settings=settings,
        )
    )
    version = db.get(DocumentVersion, event.version_id)
    return ok(
        {
            "review_event": serializers.review_event(event),
            "version": serializers.version(version, today_riyadh(), admin=True),
        },
        "Approved. The version is retrievable from its effective date.",
    )


@router.post("/updates/{candidate_id}/reject", summary="Reject with reason")
def reject(candidate_id: uuid.UUID, body: RejectRequest, db: Db, admin: Admin) -> dict:
    event = _review(lambda: reject_candidate(db, candidate_id, actor_id=admin.id, reason=body.reason))
    return ok(serializers.review_event(event), "Rejected.")


# --- sources, audit, traces, dashboard -----------------------------------------


@router.get("/sources", summary="Source registry and last-check status")
def list_sources(db: Db) -> dict:
    sources = db.scalars(select(SourceRegistry).order_by(SourceRegistry.name)).all()
    return ok({"items": [serializers.source(s) for s in sources]})


@router.patch("/sources/{source_id}", summary="Enable/disable a source or change its interval")
def patch_source(source_id: uuid.UUID, body: SourcePatch, db: Db, settings: SettingsDep) -> dict:
    source = db.get(SourceRegistry, source_id)
    if source is None:
        raise ApiError(404, "NOT_FOUND", "Source not found.")
    if body.enabled is not None:
        if body.enabled and source.crawl_policy == "manual":
            raise ApiError(409, "SOURCE_MANUAL_ONLY", "This source has no feed to monitor; its documents are uploaded by hand.")
        if body.enabled and source.crawl_policy != "fixture":
            if source.allowlisted_host.lower() not in settings.allowlisted_hosts:
                raise ApiError(422, "SOURCE_NOT_ALLOWLISTED", "The source host is not on the configured allowlist.")
        source.enabled = body.enabled
    if body.check_interval is not None:
        source.check_interval = body.check_interval
    return ok(serializers.source(source))


@router.get("/audit", summary="Audit trail of review decisions and index activations")
def audit(db: Db, page: PageDep, decision: Annotated[str | None, Query(max_length=40)] = None) -> dict:
    stmt = select(ReviewEvent).order_by(ReviewEvent.decision_at.desc())
    if decision:
        stmt = stmt.where(ReviewEvent.decision == decision)
    return ok(page.run(db, stmt, serializers.review_event))


@router.get("/traces", summary="Request traces (retrieval diagnostics)")
def list_traces(db: Db, page: PageDep, response_status: Annotated[str | None, Query(max_length=30)] = None) -> dict:
    stmt = select(QueryTrace).order_by(QueryTrace.query_time.desc())
    if response_status:
        stmt = stmt.where(QueryTrace.response_status == response_status)
    return ok(page.run(db, stmt, serializers.trace))


@router.get("/traces/{trace_id}", summary="Trace detail: answer ↔ retrieved citations ↔ versions")
def get_trace(trace_id: uuid.UUID, db: Db) -> dict:
    trace = db.get(QueryTrace, trace_id)
    if trace is None:
        raise ApiError(404, "NOT_FOUND", "Trace not found.")
    return ok(serializers.trace(trace, detail=True))


@router.get("/stats", summary="Dashboard summary: corpus, queue, jobs, sources, latency")
def stats(db: Db, settings: SettingsDep) -> dict:
    def grouped(column, model) -> dict:
        return {key: count for key, count in db.execute(select(column, func.count()).select_from(model).group_by(column))}

    latency = db.execute(
        select(
            func.count(),
            func.percentile_cont(0.5).within_group(QueryTrace.latency_ms),
            func.percentile_cont(0.95).within_group(QueryTrace.latency_ms),
        )
    ).one()
    heartbeat = db.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
    last_run = db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc().nulls_last())).first()
    generation = db.scalars(select(IndexGeneration).where(IndexGeneration.is_active.is_(True))).first()
    failed_jobs = db.scalars(
        select(IngestionJob).where(IngestionJob.status == "failed").order_by(IngestionJob.created_at.desc()).limit(5)
    ).all()
    sources = db.scalars(select(SourceRegistry).order_by(SourceRegistry.name)).all()
    return ok(
        {
            "corpus": {
                "instruments": db.scalar(select(func.count()).select_from(LegalInstrument)),
                "versions_by_review_status": grouped(DocumentVersion.review_status, DocumentVersion),
                "instruments_by_domain": grouped(LegalInstrument.domain, LegalInstrument),
            },
            "updates_by_state": grouped(UpdateCandidate.state, UpdateCandidate),
            "jobs_by_status": grouped(IngestionJob.status, IngestionJob),
            "recent_failed_jobs": [serializers.job(j) for j in failed_jobs],
            "sources": [serializers.source(s) for s in sources],
            "last_successful_source_check": max((s.last_success_at for s in sources if s.last_success_at), default=None),
            "chat": {
                "requests": latency[0],
                "latency_ms_p50": None if latency[1] is None else round(latency[1]),
                "latency_ms_p95": None if latency[2] is None else round(latency[2]),
                "by_status": grouped(QueryTrace.response_status, QueryTrace),
            },
            "worker": {
                "last_seen_at": heartbeat,
                "alive": bool(heartbeat and (utcnow() - heartbeat).total_seconds() <= settings.worker_stale_seconds),
            },
            "sync": {
                "last_run": serializers.sync_run(last_run) if last_run else None,
                "runs_by_status": grouped(SyncRun.status, SyncRun),
                "open_quarantine": db.scalar(
                    select(func.count(func.distinct(QuarantineEvent.candidate_id))).where(
                        QuarantineEvent.resolution_state == "open"
                    )
                ),
                "active_generation": serializers.generation(generation) if generation else None,
            },
        }
    )


# --- autonomous synchronization (PRD v2 section 14) -----------------------------------


@router.get("/sync-runs", summary="Nightly synchronization runs: status, stage counts, retries, errors")
def list_sync_runs(db: Db, page: PageDep, status: Annotated[str | None, Query(max_length=20)] = None) -> dict:
    _one_of(status, SYNC_STATUSES, "status")
    stmt = select(SyncRun).order_by(SyncRun.logical_date_riyadh.desc(), SyncRun.started_at.desc().nulls_last())
    if status:
        stmt = stmt.where(SyncRun.status == status)
    return ok(page.run(db, stmt, serializers.sync_run))


@router.get("/sync-runs/{run_id}", summary="One run with its candidates, quarantines and audit events")
def get_sync_run(run_id: uuid.UUID, db: Db) -> dict:
    run = db.get(SyncRun, run_id)
    if run is None:
        raise ApiError(404, "NOT_FOUND", "Sync run not found.")
    candidates = db.scalars(
        select(UpdateCandidate).where(UpdateCandidate.sync_run_id == run.id).order_by(UpdateCandidate.discovered_at)
    ).all()
    quarantined = db.scalars(select(QuarantineEvent).where(QuarantineEvent.sync_run_id == run.id)).all()
    events = db.scalars(
        select(ReviewEvent).where(ReviewEvent.sync_run_id == run.id).order_by(ReviewEvent.decision_at)
    ).all()
    generations = db.scalars(select(IndexGeneration).where(IndexGeneration.sync_run_id == run.id)).all()
    return ok(
        {
            "run": serializers.sync_run(run),
            "candidates": [serializers.candidate(c) for c in candidates],
            "quarantine": [serializers.quarantine(q) for q in quarantined],
            "audit_events": [serializers.review_event(e) for e in events],
            "generations": [serializers.generation(g) for g in generations],
        }
    )


@router.post(
    "/sync-runs/trigger",
    status_code=201,
    summary="Run the full synchronization now (same pipeline as midnight, idempotent)",
    description="For tests and recovery. Routine operation needs no manual trigger.",
)
def trigger_sync(db: Db, admin: Admin, settings: SettingsDep) -> dict:
    run = trigger_manual_run(db, settings, admin.id)
    job = enqueue(db, "nightly_sync", {"run_id": str(run.id)}, admin.id)
    return ok({"run": serializers.sync_run(run), "job_id": str(job.id)}, "Synchronization queued.")


@router.get("/quarantine", summary="Candidates the release gate refused to publish, with reason codes")
def list_quarantine(
    db: Db,
    page: PageDep,
    resolution_state: Annotated[str | None, Query(max_length=20)] = "open",
    reason_code: Annotated[str | None, Query(max_length=60)] = None,
) -> dict:
    _one_of(resolution_state, QUARANTINE_RESOLUTIONS, "resolution_state")
    stmt = select(QuarantineEvent).order_by(QuarantineEvent.created_at.desc())
    if resolution_state:
        stmt = stmt.where(QuarantineEvent.resolution_state == resolution_state)
    if reason_code:
        stmt = stmt.where(QuarantineEvent.reason_code == reason_code)
    return ok(page.run(db, stmt, serializers.quarantine))


@router.get("/index/generations", summary="Validated retrieval generations, newest first")
def list_generations(db: Db, page: PageDep) -> dict:
    return ok(page.run(db, select(IndexGeneration).order_by(IndexGeneration.number.desc()), serializers.generation))


@router.get("/index/generations/{generation_id}", summary="One generation with its health report and manifest")
def get_generation(generation_id: uuid.UUID, db: Db) -> dict:
    generation = db.get(IndexGeneration, generation_id)
    if generation is None:
        raise ApiError(404, "NOT_FOUND", "Generation not found.")
    return ok(serializers.generation(generation, detail=True))


@router.post("/index/rollback", summary="Restore the previous (or a chosen) validated retrieval generation")
def rollback_index(body: IndexRollbackRequest, db: Db, admin: Admin) -> dict:
    generation = _review(
        lambda: rollback_generation(
            db, actor_id=admin.id, reason=body.reason, target_generation_id=body.target_generation_id
        )
    )
    clear_cache()
    answer_cache.clear()
    return ok(serializers.generation(generation), f"Generation {generation.number} is now active.")


@router.get("/freshness", summary="Freshness report computed now (uncached)")
def admin_freshness(db: Db, settings: SettingsDep) -> dict:
    return ok(freshness_report(db, settings))


@router.get("/evaluation", summary="Latest measured evaluation report, if one has been generated")
def evaluation() -> dict:
    if not EVALUATION_REPORT.is_file():
        raise ApiError(404, "NOT_FOUND", "No evaluation report yet. Run `python -m tests.evaluation.run_eval`.")
    return ok(json.loads(EVALUATION_REPORT.read_text(encoding="utf-8")))

"""Background jobs: queue in PostgreSQL, executed by the worker (FR-01, FR-12, FR-20).

Each job runs in its own transaction. A failure rolls that transaction back before
the failure is recorded, so a failed or duplicate ingestion cannot leave partial
articles behind. Uploaded bytes travel as database snapshots, so the API and the worker
can run as separate services without a shared volume.
"""

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.indexing import reindex_version
from legal_core.ingestion import DocumentMetadata, IngestionError, ingest_document
from legal_core.models import DocumentVersion, IngestionJob, SourceRegistry, SyncRun, utcnow
from legal_core.monitor import (
    Fetcher,
    MonitorError,
    fetch_with_retries,
    make_fixture_fetcher,
    make_http_fetcher,
    sync_sources,
)
from legal_core.parser import ExtractionError
from legal_core.snapshots import snapshot_content, store_snapshot

log = logging.getLogger(__name__)

_PERMANENT = (IngestionError, ExtractionError, MonitorError)
_EXT = {"pdf": ".pdf", "html": ".html", "text": ".txt"}


def save_upload(content: bytes, kind: str, settings: Settings) -> str:
    """Legacy file storage (jobs queued before snapshots were stored in the database).
    Stored under a server-generated name; the client filename is never used as a path."""
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{_EXT[kind]}"
    (settings.storage_dir / name).write_bytes(content)
    return name


def read_upload(name: str, settings: Settings) -> bytes:
    root = settings.storage_dir.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise IngestionError("stored upload is missing")
    return path.read_bytes()


def enqueue(session: Session, job_type: str, payload: dict, actor_id: uuid.UUID | None = None) -> IngestionJob:
    job = IngestionJob(job_type=job_type, payload=payload, created_by=actor_id)
    session.add(job)
    session.flush()
    return job


def claim_next_job(session: Session) -> uuid.UUID | None:
    job = session.scalars(
        select(IngestionJob)
        .where(IngestionJob.status == "queued")
        .order_by(IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None
    job.status, job.started_at = "running", utcnow()
    session.commit()
    return job.id


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _execute(
    session: Session, job: IngestionJob, settings: Settings, embedder: Embedder, fetcher: Fetcher | None
) -> tuple[str, dict, dict, list[str], int]:
    payload = job.payload
    if job.job_type == "document_ingest":
        m = payload.get("meta", {})
        retries: list[str] = []
        storage_path = None
        if payload.get("snapshot_id"):
            snapshot_id = uuid.UUID(payload["snapshot_id"])
            content = snapshot_content(session, snapshot_id)
        elif payload.get("fetch_url"):
            # Registered official URL (FR-01): fetched only from allowlisted hosts, then snapshotted.
            content = fetch_with_retries(
                fetcher or make_http_fetcher(settings), payload["fetch_url"], settings, retries.append
            )
            snapshot_id = store_snapshot(
                session, content, canonical_url=payload["fetch_url"], content_type="text/html",
                is_test_data=bool(m.get("is_test_data")),
            ).id  # fmt: skip
        else:
            content, storage_path = read_upload(payload["storage_path"], settings), payload["storage_path"]
            snapshot_id = None
        result = ingest_document(
            session,
            content=content,
            filename=payload.get("filename"),
            mime_type=payload.get("mime_type"),
            storage_path=storage_path,
            snapshot_id=snapshot_id,
            actor_id=job.created_by,
            settings=settings,
            meta=DocumentMetadata(
                title_ar=m.get("title_ar"),
                domain=m.get("domain"),
                issuing_authority=m.get("issuing_authority"),
                instrument_type=m.get("instrument_type"),
                source_url=m.get("source_url"),
                version_label=m.get("version_label"),
                issued_at=_date(m.get("issued_at")),
                published_at=_date(m.get("published_at")),
                effective_from=_date(m.get("effective_from")),
                instrument_id=_uuid(m.get("instrument_id")),
                candidate_id=_uuid(m.get("candidate_id")),
                is_test_data=bool(m.get("is_test_data")),
            ),
        )
        counts = {"articles": result.article_count, "duplicates": int(result.duplicate)}
        return ("duplicate" if result.duplicate else "succeeded"), result.as_dict(), counts, retries, len(retries)

    if job.job_type == "feed_sync":
        retries = []
        counts, errors = sync_sources(
            session,
            settings,
            source_id=_uuid(payload.get("source_id")),
            force=bool(payload.get("force")),
            fetcher=fetcher,
            on_retry=retries.append,
        )
        status = "failed" if counts["sources_failed"] and counts["sources_failed"] == counts["sources_checked"] else "succeeded"
        return status, {}, counts, retries + errors, len(retries)

    if job.job_type == "source_probe":
        from legal_core.connectors import FetcherHttp, make_official_http, probe_source

        stmt = select(SourceRegistry).order_by(SourceRegistry.category, SourceRegistry.name)
        if payload.get("source_id"):
            stmt = stmt.where(SourceRegistry.id == uuid.UUID(payload["source_id"]))
        else:
            stmt = stmt.where(SourceRegistry.is_test_data.is_(False))
        official = None
        results = []
        for source in session.scalars(stmt):
            if fetcher is not None:
                http = FetcherHttp(fetcher)
            elif source.crawl_policy == "fixture":
                http = FetcherHttp(make_fixture_fetcher(settings))
            else:
                official = official or make_official_http(settings)
                http = official
            results.append({"source_id": str(source.id), **probe_source(source, http, settings)})
        counts: dict[str, int] = {}
        for row in results:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return "succeeded", {"results": results}, counts, [], 0

    if job.job_type == "reindex":
        version = session.get(DocumentVersion, uuid.UUID(payload["version_id"]))
        if version is None or version.review_status != "approved":
            raise IngestionError("only approved versions can be re-indexed")
        chunks = reindex_version(session, version, embedder, settings)
        return "succeeded", {"version_id": str(version.id)}, {"chunks": chunks}, [], 0

    raise IngestionError(f"unknown job type: {job.job_type}")


def run_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    settings: Settings,
    embedder: Embedder,
    fetcher: Fetcher | None = None,
) -> str:
    """Execute one claimed job and persist its outcome. Returns the final status."""
    outcome: tuple[str, dict, dict, list[str], int] | None = None
    failure: Exception | None = None
    with session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if job is not None and job.job_type == "nightly_sync":
            return _run_sync_job(session_factory, job_id, settings, embedder, fetcher)
    with session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        if job.status == "queued":
            job.status, job.started_at = "running", utcnow()
            session.commit()
        try:
            outcome = _execute(session, job, settings, embedder, fetcher)
            session.commit()
        except Exception as exc:
            session.rollback()
            failure = exc

    with session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if outcome is not None:
            job.status, job.result, job.counts, extra_errors, retries = outcome
            job.errors = [*job.errors, *extra_errors]
            job.retry_count += retries
            job.finished_at = utcnow()
        else:
            message = f"{type(failure).__name__}: {failure}"[:1000]
            job.errors = [*job.errors, message]
            if not isinstance(failure, _PERMANENT) and job.retry_count < settings.job_max_retries:
                job.retry_count += 1
                job.status, job.started_at = "queued", None  # transient: try again
            else:
                job.status, job.finished_at = "failed", utcnow()
            log.warning("job_failed", extra={"job_id": str(job_id), "job_type": job.job_type, "error": message})
        session.commit()
        return job.status


def _run_sync_job(
    session_factory: sessionmaker[Session], job_id: uuid.UUID, settings: Settings, embedder: Embedder, fetcher
) -> str:
    """Operator-triggered synchronization: the same pipeline as the midnight run."""
    from legal_core.sync import run_manual_sync

    with session_factory() as session:
        job = session.get(IngestionJob, job_id)
        job.status, job.started_at = "running", job.started_at or utcnow()
        run_id = uuid.UUID(job.payload["run_id"])
        session.commit()
    status = run_manual_sync(session_factory, run_id, settings, embedder, fetcher=fetcher)
    with session_factory() as session:
        job = session.get(IngestionJob, job_id)
        run = session.get(SyncRun, run_id)
        job.status = "failed" if status == "failed" else "succeeded"
        job.result, job.finished_at = {
            "run_id": str(run_id),
            "run_status": status,
            "stage_counts": run.stage_counts if run else {},
        }, utcnow()
        session.commit()
        return job.status

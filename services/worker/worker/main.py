"""Worker process: executes queued jobs and schedules source checks.

    python -m worker.main
"""

import logging
import os
import signal
import socket
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from legal_core.config import get_settings
from legal_core.db import get_sessionmaker
from legal_core.embeddings import get_embedder
from legal_core.jobs import claim_next_job, enqueue, run_job
from legal_core.logconfig import configure_logging
from legal_core.models import IngestionJob, QueryTrace, WorkerHeartbeat, utcnow

log = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def heartbeat() -> None:
    with get_sessionmaker()() as session:
        stmt = insert(WorkerHeartbeat).values(worker_id=WORKER_ID, last_seen_at=utcnow())
        session.execute(
            stmt.on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": stmt.excluded.last_seen_at})
        )
        session.commit()


def schedule_feed_sync() -> None:
    """Queue a scan unless one is already waiting; per-source intervals are enforced in the job."""
    with get_sessionmaker()() as session:
        pending = session.scalars(
            select(IngestionJob.id).where(
                IngestionJob.job_type == "feed_sync", IngestionJob.status.in_(("queued", "running"))
            )
        ).first()
        if pending is None:
            enqueue(session, "feed_sync", {"source_id": None, "force": False})
            session.commit()


def purge_expired_traces() -> None:
    """Retention policy: traces are deleted after TRACE_RETENTION_DAYS."""
    with get_sessionmaker()() as session:
        deleted = session.execute(delete(QueryTrace).where(QueryTrace.auto_delete_at < utcnow())).rowcount
        session.commit()
        if deleted:
            log.info("traces_purged", extra={"count": deleted})


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    embedder = get_embedder(settings)
    session_factory = get_sessionmaker()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=settings.worker_heartbeat_seconds, max_instances=1)
    scheduler.add_job(schedule_feed_sync, "interval", minutes=min(settings.monitor_interval_minutes, 15), max_instances=1)
    scheduler.add_job(purge_expired_traces, "interval", hours=6, max_instances=1)
    heartbeat()
    scheduler.start()
    log.info("worker_started", extra={"worker_id": WORKER_ID, "embedding_model": embedder.model_name})

    try:
        while not stop.is_set():
            try:
                with session_factory() as session:
                    job_id = claim_next_job(session)
                if job_id is None:
                    stop.wait(settings.worker_poll_seconds)
                    continue
                status = run_job(session_factory, job_id, settings, embedder)
                log.info("job_finished", extra={"job_id": str(job_id), "status": status})
            except Exception:  # e.g. database briefly unavailable: keep the worker alive
                log.exception("worker_loop_error")
                stop.wait(settings.worker_poll_seconds * 2)
    finally:
        scheduler.shutdown(wait=False)
        log.info("worker_stopped", extra={"worker_id": WORKER_ID})


if __name__ == "__main__":
    main()

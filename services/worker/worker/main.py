"""Worker process: executes queued jobs and runs the nightly synchronization.

    python -m worker.main

Run exactly one worker replica per deployment (API replicas scale independently). Even if two
start, the database advisory lock and the unique per-day run key keep one logical run per
Asia/Riyadh calendar day. Alternatively, disable the in-process schedule (SYNC_SCHEDULER=false)
and run `python -m legal_core.cli nightly-sync` from a Kubernetes CronJob (deploy/k8s).
"""

import logging
import os
import signal
import socket
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from legal_core.config import get_settings
from legal_core.db import get_sessionmaker
from legal_core.embeddings import get_embedder
from legal_core.jobs import claim_next_job, run_job
from legal_core.logconfig import configure_logging
from legal_core.models import QueryTrace, RateLimitCounter, WorkerHeartbeat, utcnow
from legal_core.sync import run_due_syncs

log = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
CATCH_UP_MINUTES = 5


def heartbeat() -> None:
    with get_sessionmaker()() as session:
        stmt = insert(WorkerHeartbeat).values(worker_id=WORKER_ID, last_seen_at=utcnow())
        session.execute(
            stmt.on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": stmt.excluded.last_seen_at})
        )
        session.commit()


def nightly_sync(embedder) -> None:
    """00:00 Asia/Riyadh, plus a short periodic tick that catches up missed days and retries
    failed runs. Both are idempotent: a day that already has its run is skipped."""
    try:
        results = run_due_syncs(get_sessionmaker(), get_settings(), embedder)
        if results:
            log.info("sync_tick", extra={"runs": [(d.isoformat(), s) for d, s in results]})
    except Exception:
        log.exception("sync_tick_failed")


def purge_expired() -> None:
    """Retention: traces after TRACE_RETENTION_DAYS; rate-limit windows after an hour."""
    with get_sessionmaker()() as session:
        deleted = session.execute(delete(QueryTrace).where(QueryTrace.auto_delete_at < utcnow())).rowcount
        session.execute(delete(RateLimitCounter).where(RateLimitCounter.window_start < utcnow() - timedelta(hours=1)))
        session.commit()
        if deleted:
            log.info("traces_purged", extra={"count": deleted})


def main() -> None:
    settings = get_settings()
    settings.assert_safe_for_environment()
    configure_logging(settings.log_level)
    embedder = get_embedder(settings)
    session_factory = get_sessionmaker()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(heartbeat, "interval", seconds=settings.worker_heartbeat_seconds, max_instances=1)
    scheduler.add_job(purge_expired, "interval", hours=1, max_instances=1)
    if os.environ.get("SYNC_SCHEDULER", "true").lower() != "false":
        cron = CronTrigger(hour=settings.sync_hour, minute=settings.sync_minute, timezone=settings.sync_tz)
        scheduler.add_job(nightly_sync, cron, args=[embedder], id="nightly_sync", max_instances=1, coalesce=True,
                          misfire_grace_time=3600)  # fmt: skip
        scheduler.add_job(nightly_sync, "interval", minutes=CATCH_UP_MINUTES, args=[embedder], id="sync_catch_up",
                          max_instances=1, coalesce=True)  # fmt: skip
    heartbeat()
    scheduler.start()
    log.info(
        "worker_started",
        extra={"worker_id": WORKER_ID, "embedding_model": embedder.model_name, "sync_timezone": settings.sync_timezone},
    )

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

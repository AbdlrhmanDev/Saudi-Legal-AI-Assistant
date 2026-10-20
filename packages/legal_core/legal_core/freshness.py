"""Freshness contract (PRD v2 6.2, FR-17): what users are told about how current the corpus is.

A completed midnight check never means every future amendment is already included, so this report
states the last verified sync, per-source health, covered domains and the latest included official
publication, and carries a warning whenever a source or the pipeline is stale.
"""

import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.models import (
    DocumentVersion,
    IndexGeneration,
    LegalInstrument,
    SourceRegistry,
    SyncRun,
    utcnow,
)
from legal_core.temporal import in_force_on

COVERAGE_NOTICE = "Selected Saudi legal sources; not exhaustive"
COVERAGE_NOTICE_AR = "يغطي المساعد مصادر نظامية سعودية مختارة فقط، وليست تغطية شاملة لجميع الأنظمة."
_WARNINGS_AR = {
    "delayed": "تأخر تحديث بعض المصادر الرسمية؛ قد لا تتضمن الإجابات أحدث التعديلات.",
    "degraded": "تعذّر التحقق من المصادر الرسمية منذ مدة؛ لا يمكن تأكيد أن النصوص المعروضة هي الأحدث.",
}
_STATUS_ORDER = {"healthy": 0, "delayed": 1, "degraded": 2}
_cache: dict = {}
_cache_lock = threading.Lock()


def _local(value: datetime | None, settings: Settings) -> str | None:
    return value.astimezone(settings.sync_tz).isoformat(timespec="seconds") if value else None


def _source_status(source: SourceRegistry, now: datetime, settings: Settings) -> str:
    if source.last_success_at is None:
        return "degraded"
    age = now - source.last_success_at
    if age > timedelta(hours=settings.freshness_degraded_hours):
        return "degraded"
    failing_since_success = source.last_error and source.last_checked_at and source.last_checked_at > source.last_success_at
    if age > timedelta(hours=settings.freshness_delayed_hours) or failing_since_success:
        return "delayed"
    return "healthy"


def freshness_report(session: Session, settings: Settings, now: datetime | None = None) -> dict:
    from legal_core.sync import riyadh_today, scheduled_instant

    now = now or utcnow()
    verified = session.scalars(
        select(SyncRun)
        .where(SyncRun.status.in_(("succeeded", "no_op", "partial")))
        .order_by(SyncRun.finished_at.desc().nulls_last())
    ).first()
    latest = session.scalars(select(SyncRun).order_by(SyncRun.started_at.desc().nulls_last())).first()
    sources = session.scalars(
        select(SourceRegistry).where(SourceRegistry.enabled.is_(True)).order_by(SourceRegistry.name)
    ).all()
    source_rows = [
        {
            "name": s.name,
            "authority": s.authority,
            "status": _source_status(s, now, settings),
            "last_success_at": _local(s.last_success_at, settings),
            "last_checked_at": _local(s.last_checked_at, settings),
            "is_test_data": s.is_test_data,
        }
        for s in sources
    ]
    statuses = [row["status"] for row in source_rows] or ["degraded"]
    if verified is None or verified.finished_at is None or now - verified.finished_at > timedelta(
        hours=settings.freshness_degraded_hours
    ):
        statuses.append("degraded")
    elif now - verified.finished_at > timedelta(hours=settings.freshness_delayed_hours):
        statuses.append("delayed")
    if latest is not None and latest.status == "failed":
        statuses.append("delayed")
    health = max(statuses, key=_STATUS_ORDER.__getitem__)

    today = riyadh_today(settings, now)
    domains = session.scalars(
        select(LegalInstrument.domain)
        .join(DocumentVersion, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(in_force_on(today))
        .distinct()
        .order_by(LegalInstrument.domain)
    ).all()
    latest_publication = session.scalar(
        select(func.max(DocumentVersion.published_at)).where(
            DocumentVersion.review_status.in_(("approved", "evidence")),
            (DocumentVersion.index_active.is_(True)) | (DocumentVersion.review_status == "evidence"),
        )
    )
    generation = session.scalars(select(IndexGeneration).where(IndexGeneration.is_active.is_(True))).first()
    next_run = scheduled_instant(settings, today)
    if next_run <= now:
        next_run = scheduled_instant(settings, today + timedelta(days=1))
    return {
        "corpus_verified_at": _local(verified.finished_at if verified else None, settings),
        "last_run": None
        if latest is None
        else {
            "logical_date": latest.logical_date_riyadh.isoformat(),
            "status": latest.status,
            "trigger": latest.trigger,
            "finished_at": _local(latest.finished_at, settings),
        },
        "source_health": health,
        "sources": source_rows,
        "covered_domains": list(domains),
        "latest_included_publication": latest_publication.isoformat() if latest_publication else None,
        "active_generation": None
        if generation is None
        else {"number": generation.number, "activated_at": _local(generation.activated_at, settings)},
        "next_scheduled_sync": _local(next_run, settings),
        "timezone": settings.sync_timezone,
        "coverage_notice": COVERAGE_NOTICE,
        "coverage_notice_ar": COVERAGE_NOTICE_AR,
        "warning_ar": _WARNINGS_AR.get(health),
    }


def cached_freshness(session: Session, settings: Settings) -> dict:
    """Per-process cache so every chat answer can carry freshness without an extra query burst."""
    ttl = settings.freshness_cache_seconds
    if ttl <= 0:
        return freshness_report(session, settings)
    with _cache_lock:
        hit = _cache.get("report")
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    report = freshness_report(session, settings)
    with _cache_lock:
        _cache["report"] = (time.monotonic(), report)
    return report


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()

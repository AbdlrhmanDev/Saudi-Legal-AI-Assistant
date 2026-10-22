"""Nightly autonomous synchronization (PRD v2 sections 6 and 8).

Discover -> fetch -> deduplicate -> parse -> classify -> validate -> version -> index -> activate
-> audit, once per Asia/Riyadh calendar day at 00:00.

Guarantees:
- One logical run per Saudi date: scheduled and catch-up runs share the unique key daily:<date>.
- One active run at a time across all workers: a PostgreSQL advisory lock.
- Missed days inside SYNC_CATCHUP_DAYS are caught up; failed runs retry with exponential backoff.
- Idempotent: unchanged feed items, identical snapshots and identical article text are skipped,
  so a re-run never duplicates versions, chunks or embeddings.
- Fail-safe: the corpus changes only through one transaction that ends with a health-checked
  index generation switch. Anything uncertain is quarantined; the last trusted corpus stays live.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from legal_core import alerts
from legal_core.agent import AgentToolbox, Classifier, SearchProvider, get_classifier
from legal_core.config import Settings
from legal_core.db import get_engine
from legal_core.embeddings import Embedder
from legal_core.generations import IndexHealthError, activate_generation, record_failed_generation
from legal_core.indexing import IndexStats, index_version
from legal_core.models import (
    PUBLISHABLE_CLASSIFICATIONS,
    AmendmentRelation,
    DocumentVersion,
    QuarantineEvent,
    ReviewEvent,
    SourceRegistry,
    SyncRun,
    UpdateCandidate,
    utcnow,
)
from legal_core.monitor import Fetcher, _fetcher_for, acquire, sync_sources
from legal_core.relationships import link_instruments
from legal_core.release import TRANSIENT_REASONS, GateDecision, evaluate, unprocessed_candidates
from legal_core.review import authoritative_versions, place_version

log = logging.getLogger(__name__)

SYNC_LOCK_KEY = 0x5A1E_5153
FINISHED = ("succeeded", "no_op", "partial")


def riyadh_today(settings: Settings, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(settings.sync_tz).date()


def scheduled_instant(settings: Settings, logical_date: date) -> datetime:
    return datetime.combine(logical_date, time(settings.sync_hour, settings.sync_minute), tzinfo=settings.sync_tz)


def daily_key(logical_date: date) -> str:
    return f"daily:{logical_date.isoformat()}"


@contextmanager
def sync_lock(*, blocking: bool) -> Iterator[bool]:
    """Session-level advisory lock on a dedicated connection: only one run executes at a time,
    whichever worker, CronJob or API-triggered process starts it."""
    conn = get_engine().connect()
    try:
        fn = "pg_advisory_lock" if blocking else "pg_try_advisory_lock"
        acquired = conn.execute(text(f"SELECT {fn}(:key)"), {"key": SYNC_LOCK_KEY}).scalar()
        acquired = True if acquired is None else bool(acquired)
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_LOCK_KEY})
                conn.commit()
    finally:
        conn.close()


def due_runs(session: Session, settings: Settings, now: datetime) -> list[tuple[date, str]]:
    """Riyadh dates whose 00:00 has passed and that still need their logical run (oldest first)."""
    today = riyadh_today(settings, now)
    due: list[tuple[date, str]] = []
    for offset in range(max(settings.sync_catchup_days, 1) - 1, -1, -1):
        logical = today - timedelta(days=offset)
        if scheduled_instant(settings, logical) > now:
            continue
        run = session.scalars(select(SyncRun).where(SyncRun.run_key == daily_key(logical))).first()
        trigger = "scheduled" if logical == today else "catch_up"
        if run is None or run.status == "queued":
            due.append((logical, run.trigger if run else trigger))
        elif (
            run.status == "failed"
            and run.retry_count < settings.sync_max_retries
            and (run.retry_after is None or run.retry_after <= now)
        ):
            due.append((logical, run.trigger))
    return due


def ensure_run(
    session: Session, logical_date: date, trigger: str, settings: Settings, *, actor_id: uuid.UUID | None = None
) -> SyncRun:
    key = f"manual:{uuid.uuid4().hex}" if trigger == "manual" else daily_key(logical_date)
    session.execute(
        insert(SyncRun)
        .values(
            id=uuid.uuid4(), run_key=key, logical_date_riyadh=logical_date, trigger=trigger, status="queued",
            scheduled_at=scheduled_instant(settings, logical_date) if trigger != "manual" else utcnow(),
            retry_count=0, stage_counts={}, errors=[], created_by=actor_id,
        )  # fmt: skip
        .on_conflict_do_nothing(index_elements=["run_key"])
    )
    return session.scalars(select(SyncRun).where(SyncRun.run_key == key)).one()


def _recover_stale_runs(session: Session, settings: Settings, now: datetime) -> None:
    """Called while holding the lock: a run still marked running belongs to a dead process."""
    for run in session.scalars(select(SyncRun).where(SyncRun.status == "running")):
        run.status, run.error_code, run.finished_at = "failed", "WORKER_LOST", now
        run.errors = [*run.errors, "the process executing this run stopped before finishing"]
        run.retry_after = now


@dataclass
class _Context:
    session_factory: sessionmaker[Session]
    settings: Settings
    embedder: Embedder
    classifier: Classifier
    fetcher: Fetcher | None
    search: SearchProvider | None
    run_id: uuid.UUID
    now: datetime
    stages: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)


def run_due_syncs(
    session_factory: sessionmaker[Session],
    settings: Settings,
    embedder: Embedder,
    *,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    classifier: Classifier | None = None,
    search: SearchProvider | None = None,
) -> list[tuple[date, str]]:
    """Scheduler entry point (cron tick, catch-up tick or `cli nightly-sync`). Safe to call often."""
    now = now or utcnow()
    with sync_lock(blocking=False) as acquired:
        if not acquired:
            log.info("sync_skipped_lock_held")
            return []
        with session_factory() as session:
            _recover_stale_runs(session, settings, now)
            due = due_runs(session, settings, now)
            run_ids = [(d, ensure_run(session, d, trigger, settings).id) for d, trigger in due]
            session.commit()
        results = []
        for logical, run_id in run_ids:
            status = execute_run(
                session_factory, run_id, settings, embedder, now=now, fetcher=fetcher, classifier=classifier, search=search
            )
            results.append((logical, status))
        return results


def run_manual_sync(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    settings: Settings,
    embedder: Embedder,
    **kwargs,
) -> str:
    """Operator-triggered run (same pipeline). Waits for any scheduled run to finish first."""
    with sync_lock(blocking=True):
        return execute_run(session_factory, run_id, settings, embedder, **kwargs)


def execute_run(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    settings: Settings,
    embedder: Embedder,
    *,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    classifier: Classifier | None = None,
    search: SearchProvider | None = None,
) -> str:
    now = now or utcnow()
    with session_factory() as session:
        run = session.get(SyncRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError(f"sync run {run_id} not found")
        if run.status in FINISHED:
            return run.status
        if run.status == "failed":
            run.retry_count += 1
        run.status, run.started_at, run.finished_at, run.error_code = "running", utcnow(), None, None
        session.commit()
        logical = run.logical_date_riyadh

    ctx = _Context(
        session_factory, settings, embedder, classifier or get_classifier(settings), fetcher, search, run_id, now
    )
    log.info("sync_started", extra={"run_id": str(run_id), "logical_date": logical.isoformat()})
    try:
        status = _pipeline(ctx)
    except Exception as exc:
        log.exception("sync_failed", extra={"run_id": str(run_id)})
        with session_factory() as session:
            run = session.get(SyncRun, run_id)
            run.status, run.finished_at, run.stage_counts = "failed", utcnow(), ctx.stages
            run.error_code = type(exc).__name__[:60]
            run.errors = [*run.errors, *ctx.errors, f"{type(exc).__name__}: {exc}"[:1000]]
            run.retry_after = now + timedelta(seconds=settings.sync_retry_backoff_seconds * (2**run.retry_count))
            session.commit()
        alerts.emit(settings, "sync_failed", f"nightly sync for {logical} failed: {type(exc).__name__}",
                    {"run_id": str(run_id)})  # fmt: skip
        return "failed"
    log.info("sync_finished", extra={"run_id": str(run_id), "status": status, "stages": ctx.stages})
    return status


# --- pipeline ------------------------------------------------------------------------


def _pipeline(ctx: _Context) -> str:
    settings = ctx.settings
    with ctx.session_factory() as session:
        totals, errors = sync_sources(
            session, settings, force=True, fetcher=ctx.fetcher, on_retry=ctx.errors.append, run_id=ctx.run_id
        )
        ctx.errors.extend(errors)
        session.execute(
            update(UpdateCandidate)
            .where(UpdateCandidate.sync_run_id.is_(None), UpdateCandidate.origin.in_(("feed", "agent")))
            .values(sync_run_id=ctx.run_id)
        )
        session.commit()
    ctx.stages["discover"] = {
        k: totals.get(k, 0)
        for k in (
            "sources_checked", "sources_failed", "seen", "new", "changed", "duplicates_across_sources", "deferred",
            "page_changed", "refreshed", "judicial_new", "judicial_failed",
        )
    }  # fmt: skip
    ctx.stages["fetch"] = {"acquired": totals.get("acquired", 0), "acquire_failed": totals.get("acquire_failed", 0)}
    ctx.stages["deduplicate"] = {"unchanged_items": totals.get("unchanged", 0), "skipped_items": totals.get("skipped", 0)}

    ctx.stages["discover"]["idle_sources_probed"] = _probe_idle_sources(ctx)
    retried = _retry_transient(ctx)
    ctx.stages["fetch"]["retried"] = retried
    ctx.stages["fetch"]["supplementary"] = _supplementary_discovery(ctx)

    to_publish = _classify_and_gate(ctx)
    published, generation = _publish(ctx, to_publish)

    with ctx.session_factory() as session:
        run = session.get(SyncRun, ctx.run_id)
        changed_items = totals.get("new", 0) + totals.get("changed", 0)
        decided = sum(ctx.stages["validate"].values())
        if totals.get("sources_checked") and totals.get("sources_failed") == totals.get("sources_checked"):
            status = "failed"
        elif totals.get("sources_failed"):
            status = "partial"
        elif not (changed_items or decided or retried or published):
            status = "no_op"
        else:
            status = "succeeded"
        ctx.stages["audit"] = {
            "events": session.scalar(select(func.count(ReviewEvent.id)).where(ReviewEvent.sync_run_id == ctx.run_id)) or 0,
            "quarantined": len(ctx.quarantined),
        }
        run.status, run.finished_at, run.stage_counts = status, utcnow(), ctx.stages
        run.errors = [*run.errors, *ctx.errors][-50:]
        if status == "failed":
            run.error_code = "ALL_SOURCES_FAILED"
            run.retry_after = ctx.now + timedelta(seconds=settings.sync_retry_backoff_seconds * (2**run.retry_count))
        session.commit()

    if totals.get("sources_failed"):
        alerts.emit(settings, "source_failure", f"{totals['sources_failed']} source(s) could not be read",
                    {"run_id": str(ctx.run_id), "errors": errors[:10]})  # fmt: skip
    if ctx.quarantined:
        alerts.emit(settings, "quarantine", f"{len(ctx.quarantined)} candidate(s) quarantined",
                    {"run_id": str(ctx.run_id), "reasons": dict(Counter(ctx.quarantined))})  # fmt: skip
    # A successful discovery/publication is useful operator information, not an error. Keep it
    # quiet when nothing changed so the daily schedule does not create notification noise.
    discovered = totals.get("new", 0) + totals.get("changed", 0)
    if discovered or published:
        alerts.emit(
            settings,
            "legal_updates",
            f"{discovered} new or changed item(s) found; {published} published to the active RAG index",
            {
                "run_id": str(ctx.run_id),
                "discovered": discovered,
                "new": totals.get("new", 0),
                "changed": totals.get("changed", 0),
                "published": published,
                "quarantined": len(ctx.quarantined),
                "index_generation": generation,
            },
        )
    return status


def _quarantine(
    session: Session, ctx: _Context, candidate: UpdateCandidate, code: str, detail: str, evidence: dict | None = None
) -> None:
    version = candidate.proposed_version
    if version is not None and version.review_status in ("needs_review", "parsed", "retrieved"):
        version.review_status = "quarantined"
    candidate.state = "quarantined"
    transient = code in TRANSIENT_REASONS
    # A candidate may be evaluated again by a recovery/manual run. Keep the previous event as
    # immutable audit history, but only one event may remain actionable in the review queue.
    session.execute(
        update(QuarantineEvent)
        .where(
            QuarantineEvent.candidate_id == candidate.id,
            QuarantineEvent.resolution_state == "open",
        )
        .values(resolution_state="superseded", resolved_at=utcnow())
    )
    session.add(
        QuarantineEvent(
            candidate_id=candidate.id,
            sync_run_id=ctx.run_id,
            source_snapshot_id=version.snapshot_id if version else None,
            version_id=version.id if version else None,
            reason_code=code,
            detail=detail[:2000],
            source_evidence={
                "title": candidate.title,
                "url": candidate.canonical_url,
                "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
                **(evidence or {}),
            },
            retry_after=ctx.now + timedelta(days=1) if transient else None,
        )
    )
    session.add(
        ReviewEvent(
            candidate_id=candidate.id, version_id=version.id if version else None, decision="auto_quarantined",
            reason=f"{code}: {detail}"[:2000], sync_run_id=ctx.run_id, details=evidence or {},
        )  # fmt: skip
    )
    ctx.quarantined.append(code)


def _probe_idle_sources(ctx: _Context) -> int:
    """Every registered source is checked nightly: sources that were not read by a connector (disabled,
    or with no permitted access path) get a reachability probe so their reported health stays current.
    Automated tests never reach the network."""
    if ctx.settings.environment == "test":
        return 0
    from legal_core.connectors import FetcherHttp, make_official_http, probe_source

    probed = 0
    with ctx.session_factory() as session:
        idle = session.scalars(
            select(SourceRegistry).where(
                SourceRegistry.enabled.is_(False),
                SourceRegistry.consolidated_into.is_(None),
                SourceRegistry.is_test_data.is_(False),
            )
        ).all()
        http = FetcherHttp(ctx.fetcher) if ctx.fetcher else make_official_http(ctx.settings)
        for source in idle:
            probe_source(source, http, ctx.settings)
            probed += 1
        session.commit()
    return probed


def _retry_transient(ctx: _Context) -> int:
    """Re-attempt candidates blocked by a transient cause once their retry time has come."""
    retried = 0
    with ctx.session_factory() as session:
        due = session.scalars(
            select(QuarantineEvent).where(
                QuarantineEvent.resolution_state == "open",
                QuarantineEvent.reason_code.in_(TRANSIENT_REASONS),
                QuarantineEvent.retry_after <= ctx.now,
            )
        ).all()
        for event in due:
            candidate = event.candidate
            event.resolution_state, event.resolved_at = "superseded", utcnow()
            if candidate.state != "quarantined":
                continue
            version = candidate.proposed_version
            if version is None:
                candidate.state = "failed"
            else:
                version.review_status, candidate.state = "needs_review", "needs_review"
            retried += 1
        counts = Counter()
        for candidate in session.scalars(
            select(UpdateCandidate).where(
                UpdateCandidate.state == "failed",
                UpdateCandidate.document_ref.is_not(None),
                UpdateCandidate.proposed_version_id.is_(None),
            )
        ):
            source = candidate.source
            if source is None or not source.enabled:
                continue
            try:
                fetch = ctx.fetcher or _fetcher_for(source, ctx.settings)
            except Exception as exc:
                ctx.errors.append(f"{source.name}: {exc}")
                continue
            acquire(session, source, candidate, ctx.settings, fetch, ctx.errors.append, counts)
        session.commit()
    return retried + counts.get("acquired", 0)


def _supplementary_discovery(ctx: _Context) -> int:
    """Bounded agent search for the official text of legal items whose feed had none. A search hit
    only locates a page; it is fetched from an allowlisted host and still passes the full gate."""
    if ctx.search is None:
        return 0
    located = 0
    with ctx.session_factory() as session:
        fetch_hosts = {
            s.allowlisted_host.lower(): s
            for s in session.scalars(
                select(SourceRegistry).where(SourceRegistry.enabled.is_(True), SourceRegistry.crawl_policy == "fetch_allowed")
            )
        }
        toolbox = AgentToolbox(session, ctx.settings, fetcher=ctx.fetcher, search=ctx.search)
        pending = session.scalars(
            select(UpdateCandidate).where(
                UpdateCandidate.state == "candidate",
                UpdateCandidate.document_ref.is_(None),
                UpdateCandidate.classification.in_(PUBLISHABLE_CLASSIFICATIONS),
            )
        ).all()
        counts = Counter()
        for candidate in pending:
            for hit in toolbox.search_official_sites(candidate.title):
                source = fetch_hosts.get((urlsplit(hit["url"]).hostname or "").lower())
                if source is None:
                    continue
                candidate.document_ref = hit["url"]
                acquire(session, source, candidate, ctx.settings, toolbox._fetch, ctx.errors.append, counts)
                located += 1
                break
        if toolbox.blocked:
            ctx.errors.append(f"agent search blocked {len(toolbox.blocked)} non-allowlisted result(s)")
        session.commit()
    return located


def _classify_and_gate(ctx: _Context) -> list[tuple[uuid.UUID, GateDecision]]:
    outcomes: Counter = Counter()
    classes: Counter = Counter()
    to_publish: list[tuple[uuid.UUID, GateDecision]] = []
    with ctx.session_factory() as session:
        for candidate in unprocessed_candidates(session, ctx.settings.sync_max_candidates_per_run):
            version = candidate.proposed_version
            proposal = ctx.classifier.propose(session, candidate, version.raw_text if version else "")
            decision = evaluate(session, candidate, proposal, ctx.settings)
            classes[candidate.classification] += 1
            candidate.evidence = {"agent": proposal.as_dict(), "gate": decision.as_dict()}
            outcomes[decision.outcome] += 1
            if decision.outcome == "publish":
                to_publish.append((candidate.id, decision))
            elif decision.outcome == "exclude":
                candidate.state = "excluded"
                if version is not None and version.review_status == "needs_review":
                    version.review_status = "rejected"
                session.add(
                    ReviewEvent(
                        candidate_id=candidate.id, version_id=version.id if version else None, decision="auto_excluded",
                        reason=f"{decision.reason_code}: {decision.reasons[0][1]}"[:2000], sync_run_id=ctx.run_id,
                    )  # fmt: skip
                )
            else:
                code, detail = decision.reasons[0] if decision.reasons else ("UNVERIFIED", "")
                _quarantine(session, ctx, candidate, code, detail, {"gate": decision.as_dict()})
        session.commit()
    ctx.stages["parse"] = {"documents": ctx.stages["fetch"]["acquired"] + ctx.stages["fetch"].get("retried", 0)}
    ctx.stages["classify"] = dict(classes)
    ctx.stages["validate"] = {"publish": outcomes["publish"], "quarantine": outcomes["quarantine"],
                              "exclude": outcomes["exclude"]}  # fmt: skip
    return to_publish


def _publish(ctx: _Context, to_publish: list[tuple[uuid.UUID, GateDecision]]) -> tuple[int, int | None]:
    """Version + index + activate in one transaction (lifecycle steps 7-9)."""
    ctx.stages["version"] = {"published": 0, "repeals": 0, "scheduled": 0}
    ctx.stages["index"] = {"chunks": 0, "embedded": 0, "reused": 0}
    ctx.stages["activate"] = {"generation": None}
    if not to_publish:
        return 0, None

    health_failure: dict | None = None
    published_ids: list[uuid.UUID] = []
    with ctx.session_factory() as session:
        budget = ctx.settings.sync_max_embedding_inputs_per_run
        stats = IndexStats()
        changed: list[DocumentVersion] = []
        today = riyadh_today(ctx.settings, ctx.now)
        for candidate_id, decision in to_publish:
            candidate = session.get(UpdateCandidate, candidate_id, with_for_update=True)
            version = candidate.proposed_version
            if decision.kind != "repeal" and len(version.articles) > budget:
                _quarantine(session, ctx, candidate, "EMBEDDING_BUDGET_EXCEEDED",
                            "this run's embedding budget is used up; retried by a later run")  # fmt: skip
                continue
            try:
                with session.begin_nested():
                    version_stats = _publish_one(session, ctx, candidate, decision)
            except Exception as exc:
                log.warning("publish_failed", extra={"candidate_id": str(candidate_id), "error": type(exc).__name__})
                session.refresh(candidate)
                _quarantine(session, ctx, candidate, "PUBLISH_FAILED", f"{type(exc).__name__}: {exc}"[:500])
                continue
            budget -= version_stats.chunks
            stats += version_stats
            published_ids.append(candidate_id)
            if decision.kind != "repeal":
                changed.append(version)
            ctx.stages["version"]["repeals" if decision.kind == "repeal" else "published"] += 1
            if decision.effective and decision.effective.value and decision.effective.value > today:
                ctx.stages["version"]["scheduled"] += 1
        generation = None
        if published_ids:
            try:
                generation = activate_generation(
                    session, trigger="nightly_sync", changed=changed, stats=stats, sync_run_id=ctx.run_id,
                    note=f"{len(published_ids)} change(s) published automatically",
                )  # fmt: skip
            except IndexHealthError as exc:
                health_failure = exc.health
        if health_failure is None:
            link_instruments(session)
            session.commit()
        else:
            session.rollback()

    if health_failure is not None:
        with ctx.session_factory() as session:
            record_failed_generation(session, trigger="nightly_sync", health=health_failure, sync_run_id=ctx.run_id,
                                     note="staged generation failed validation; previous generation kept active")  # fmt: skip
            for candidate_id, _decision in to_publish:
                candidate = session.get(UpdateCandidate, candidate_id)
                if candidate.state != "quarantined":
                    _quarantine(session, ctx, candidate, "INDEX_HEALTH_FAILED", "the staged index failed its health checks",
                                {"health": health_failure})  # fmt: skip
            session.commit()
        alerts.emit(ctx.settings, "index_health_failed", "staged index generation failed validation; nothing published",
                    {"run_id": str(ctx.run_id)})  # fmt: skip
        ctx.stages["version"] = {"published": 0, "repeals": 0, "scheduled": 0}
        return 0, None

    ctx.stages["index"] = {"chunks": stats.chunks, "embedded": stats.embedded, "reused": stats.reused}
    ctx.stages["activate"] = {"generation": generation.number if generation else None}
    return len(published_ids), generation.number if generation else None


def _publish_one(session: Session, ctx: _Context, candidate: UpdateCandidate, decision: GateDecision) -> IndexStats:
    version = candidate.proposed_version
    effective = decision.effective.value
    spans = "; ".join(e.span for e in decision.effective.evidence)[:2000]
    version.validation_evidence = {
        "method": "release_gate",
        "gate": decision.as_dict(),
        "agent": candidate.evidence.get("agent"),
        "sync_run_id": str(ctx.run_id),
    }
    publication = f"{candidate.title} ({candidate.canonical_url})"[:2000]
    relations: list[AmendmentRelation] = []
    stats = IndexStats()
    details: dict = {"kind": decision.kind, "checks": decision.checks}

    if decision.kind == "repeal":
        target_versions = authoritative_versions(session, candidate.target_instrument_id, exclude=version.id)
        ended = [v for v in target_versions if v.effective_from < effective][-1]
        details["repealed_version"] = {
            "version_id": str(ended.id),
            "previous_effective_to": ended.effective_to.isoformat() if ended.effective_to else None,
            "previous_end_reason": ended.end_reason,
        }
        ended.effective_to, ended.end_reason = effective, "repealed"
        version.effective_from, version.review_status, version.index_active = effective, "evidence", False
        relations.append(AmendmentRelation(target_instrument_id=candidate.target_instrument_id, change_type="repealed"))
    else:
        details.update(place_version(session, version, effective))
        stats = index_version(session, version, ctx.embedder, ctx.settings)
        details["indexed_chunks"], details["embedded_chunks"] = stats.chunks, stats.embedded
        if decision.kind == "new_law":
            relations.append(AmendmentRelation(target_instrument_id=version.instrument_id, change_type="new_instrument"))
        for change in (candidate.diff or {}).get("changes", []):
            relations.append(
                AmendmentRelation(
                    target_instrument_id=version.instrument_id,
                    change_type=change["change"],
                    affected_article_number=change["article_number"][:40],
                    before_article_id=uuid.UUID(change["before_article_id"]) if change["before_article_id"] else None,
                    after_article_id=uuid.UUID(change["after_article_id"]) if change["after_article_id"] else None,
                )
            )
    for relation in relations:
        relation.amending_version_id, relation.candidate_id, relation.sync_run_id = version.id, candidate.id, ctx.run_id
        relation.effective_from, relation.publication_evidence, relation.date_evidence = effective, publication, spans
        session.add(relation)

    candidate.state = "approved"
    # A newer publication of the same item supersedes earlier quarantined attempts.
    for event in session.scalars(
        select(QuarantineEvent)
        .join(UpdateCandidate, QuarantineEvent.candidate_id == UpdateCandidate.id)
        .where(
            QuarantineEvent.resolution_state == "open",
            UpdateCandidate.source_id == candidate.source_id,
            UpdateCandidate.external_id == candidate.external_id,
        )
    ):
        event.resolution_state, event.resolved_at = "superseded", utcnow()
    session.add(
        ReviewEvent(
            candidate_id=candidate.id, version_id=version.id, decision="auto_published",
            reason="all deterministic release checks passed", effective_date_evidence=spans or None,
            sync_run_id=ctx.run_id, details=details,
        )  # fmt: skip
    )
    session.flush()
    return stats


def trigger_manual_run(session: Session, settings: Settings, actor_id: uuid.UUID | None) -> SyncRun:
    return ensure_run(session, riyadh_today(settings), "manual", settings, actor_id=actor_id)

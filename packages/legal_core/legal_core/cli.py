"""Operational commands.

    python -m legal_core.cli create-admin      # from ADMIN_EMAIL / ADMIN_PASSWORD
    python -m legal_core.cli seed-sources      # official source registry only
    python -m legal_core.cli bootstrap         # admin + official source registry (no test data)
    python -m legal_core.cli purge-test-data   # remove test/demo rows and rebuild the active index
    python -m legal_core.cli reindex-approved  # rebuild approved text with the configured embedding model
    python -m legal_core.cli sync-now          # run the full official synchronization immediately
    python -m legal_core.cli nightly-sync      # run every due Asia/Riyadh daily sync once, then exit
                                               # (Kubernetes CronJob / external scheduler entry point)
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import bcrypt
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, selectinload

from legal_core.config import Settings, get_settings
from legal_core.db import get_sessionmaker, session_scope
from legal_core.embeddings import get_embedder
from legal_core.generations import activate_generation
from legal_core.indexing import IndexStats, article_context, content_hash, index_version
from legal_core.ingestion import DocumentMetadata, ingest_document
from legal_core.models import (
    PUBLISHABLE_CLASSIFICATIONS,
    AmendmentRelation,
    Article,
    ArticleChunk,
    DocumentVersion,
    IndexGeneration,
    IngestionJob,
    LegalInstrument,
    QuarantineEvent,
    QueryTrace,
    ReviewEvent,
    SourceRegistry,
    SourceSnapshot,
    UpdateCandidate,
    User,
)
from legal_core.review import approve_candidate

# Starting points from the PRD reference list. Only Umm Al-Qura's verified legal feed is enabled;
# manual-only portals remain disabled until an administrator reviews their access and reuse terms.
OFFICIAL_SOURCES = [
    # Direct feed verification shows that /rssFeed/21 returns decisions-and-regulations URLs.
    # Entries link to full official text on the same allowlisted host.
    ("Umm Al-Qura RSS - Decisions & Regulations", "Umm Al-Qura (Official Gazette)", "https://www.uqn.gov.sa/rssFeed/21", "www.uqn.gov.sa", "fetch_allowed", True),
    ("Bureau of Experts - Saudi Laws Portal", "Bureau of Experts at the Council of Ministers", "https://laws.boe.gov.sa/", "laws.boe.gov.sa", "manual", False),
    ("HRSD - Labor Law", "Ministry of Human Resources and Social Development", "https://www.hrsd.gov.sa/", "www.hrsd.gov.sa", "manual", False),
    ("Ministry of Commerce - Regulations", "Ministry of Commerce", "https://mc.gov.sa/ar/Regulations/pages/default.aspx", "mc.gov.sa", "manual", False),
    ("SDAIA - PDPL Knowledge Center", "Saudi Data & AI Authority", "https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/", "dgp.sdaia.gov.sa", "manual", False),
]  # fmt: skip
FIXTURE_SOURCE = "TEST FIXTURE - simulated official feed"


def create_admin(session: Session, settings: Settings) -> str:
    if not settings.admin_password:
        raise SystemExit("ADMIN_PASSWORD is not set")
    if len(settings.admin_password) < 12:
        raise SystemExit("ADMIN_PASSWORD must be at least 12 characters")
    email = settings.admin_email.lower()
    user = session.scalars(select(User).where(User.email == email)).first()
    password_hash = bcrypt.hashpw(settings.admin_password.encode()[:72], bcrypt.gensalt()).decode("ascii")
    if user:
        user.password_hash, user.role = password_hash, "admin"
        return f"admin updated: {email}"
    session.add(User(email=email, role="admin", password_hash=password_hash))
    return f"admin created: {email}"


def seed_sources(session: Session, settings: Settings) -> str:
    existing = {s.name: s for s in session.scalars(select(SourceRegistry))}
    added = updated = 0
    for name, authority, url, host, policy, enabled in OFFICIAL_SOURCES:
        source = existing.get(name)
        if source is None:
            source = SourceRegistry(name=name, authority=authority, feed_url=url, allowlisted_host=host)
            session.add(source)
            added += 1
        else:
            updated += 1
        source.authority = authority
        source.feed_url = url
        source.allowlisted_host = host
        source.crawl_policy = policy
        # Automated tests supply their own isolated fixture source and must never touch the network.
        source.enabled = enabled and settings.environment != "test"
        source.check_interval = settings.monitor_interval_minutes
        source.is_test_data = False
        session.flush()
        # Repair candidates discovered when this source used the old feed-only policy. They are
        # retried by the next full sync and still pass every release-gate check before publication.
        if enabled and policy == "fetch_allowed":
            for candidate in session.scalars(
                select(UpdateCandidate).where(
                    UpdateCandidate.source_id == source.id,
                    UpdateCandidate.proposed_version_id.is_(None),
                    UpdateCandidate.document_ref.is_(None),
                    UpdateCandidate.canonical_url.is_not(None),
                    UpdateCandidate.classification.in_(PUBLISHABLE_CLASSIFICATIONS),
                )
            ):
                candidate.document_ref = candidate.canonical_url
                candidate.state = "failed"
    return f"official sources added: {added}, updated: {updated}"


def seed_fixtures(session: Session, settings: Settings) -> str:
    """Test-suite helper. Never exposed as an operational CLI command."""
    if not settings.allow_fixture_sources:
        raise SystemExit("test fixtures require ALLOW_FIXTURE_SOURCES=true")
    source = session.scalars(select(SourceRegistry).where(SourceRegistry.name == FIXTURE_SOURCE)).first()
    if source is None:
        session.add(
            SourceRegistry(
                name=FIXTURE_SOURCE,
                authority="TEST DATA (not an official source)",
                feed_url="feeds/simulated_feed.xml",
                allowlisted_host="fixture.invalid",
                enabled=True,
                crawl_policy="fixture",
                check_interval=60,
                is_test_data=True,
            )
        )
    manifest = json.loads((settings.fixtures_dir / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    embedder = get_embedder(settings)
    loaded = skipped = 0
    for doc in manifest["documents"]:
        if not doc.get("seed", True):
            continue
        path = settings.fixtures_dir / "corpus" / doc["file"]
        result = ingest_document(
            session,
            content=path.read_bytes(),
            filename=Path(doc["file"]).name,
            mime_type="text/plain",
            settings=settings,
            meta=DocumentMetadata(
                title_ar=doc["title_ar"],
                domain=doc["domain"],
                instrument_type=doc["instrument_type"],
                issuing_authority=doc["issuing_authority"],
                source_url=doc["source_url"],
                version_label=doc.get("version_label"),
                published_at=date.fromisoformat(doc["published_at"]),
                is_test_data=True,
            ),
        )
        if result.duplicate:
            skipped += 1
            continue
        approve_candidate(
            session,
            result.candidate_id,
            actor_id=None,
            effective_from=date.fromisoformat(doc["effective_from"]),
            effective_date_evidence="Fixture manifest (fictional test data, seeded by CLI).",
            reason="seed-fixtures",
            acknowledge_warnings=True,  # the adversarial fixture is loaded on purpose
            embedder=embedder,
            settings=settings,
        )
        loaded += 1
    return f"fixture documents approved: {loaded}, already present: {skipped}"


def purge_test_data(session: Session, settings: Settings) -> str:
    """Remove only rows explicitly marked as test data, then activate an official-only index.

    Production/official rows and users are preserved. Historical index generations are removed
    because their manifests may reference deleted test versions and must never be rollback targets.
    """
    test_source_ids = set(session.scalars(select(SourceRegistry.id).where(SourceRegistry.is_test_data.is_(True))))
    test_instrument_ids = set(
        session.scalars(select(LegalInstrument.id).where(LegalInstrument.is_test_data.is_(True)))
    )
    test_version_ids = set(
        session.scalars(
            select(DocumentVersion.id).where(
                or_(
                    DocumentVersion.is_test_data.is_(True),
                    DocumentVersion.instrument_id.in_(test_instrument_ids),
                )
            )
        )
    )
    test_article_ids = set(session.scalars(select(Article.id).where(Article.version_id.in_(test_version_ids))))
    test_snapshot_ids = set(
        session.scalars(
            select(SourceSnapshot.id).where(
                or_(SourceSnapshot.is_test_data.is_(True), SourceSnapshot.source_id.in_(test_source_ids))
            )
        )
    )
    test_candidate_ids = set(
        session.scalars(
            select(UpdateCandidate.id).where(
                or_(
                    UpdateCandidate.is_test_data.is_(True),
                    UpdateCandidate.source_id.in_(test_source_ids),
                    UpdateCandidate.proposed_version_id.in_(test_version_ids),
                    UpdateCandidate.target_instrument_id.in_(test_instrument_ids),
                )
            )
        )
    )
    nonlegal_candidate_ids = set(
        session.scalars(
            select(UpdateCandidate.id).where(
                UpdateCandidate.proposed_version_id.is_(None),
                or_(
                    UpdateCandidate.canonical_url.contains("/news/"),
                    UpdateCandidate.canonical_url.contains("/advertisements/"),
                    UpdateCandidate.canonical_url.contains("/infographic/"),
                ),
            )
        )
    )
    removable_candidate_ids = test_candidate_ids | nonlegal_candidate_ids

    counts = {
        "sources": len(test_source_ids),
        "instruments": len(test_instrument_ids),
        "versions": len(test_version_ids),
        "candidates": len(test_candidate_ids),
        "nonlegal_feed_items": len(nonlegal_candidate_ids),
        "snapshots": len(test_snapshot_ids),
    }
    session.execute(
        delete(AmendmentRelation).where(
            or_(
                AmendmentRelation.candidate_id.in_(removable_candidate_ids),
                AmendmentRelation.amending_version_id.in_(test_version_ids),
                AmendmentRelation.target_instrument_id.in_(test_instrument_ids),
                AmendmentRelation.before_article_id.in_(test_article_ids),
                AmendmentRelation.after_article_id.in_(test_article_ids),
            )
        )
    )
    session.execute(
        delete(QuarantineEvent).where(
            or_(
                QuarantineEvent.candidate_id.in_(removable_candidate_ids),
                QuarantineEvent.version_id.in_(test_version_ids),
                QuarantineEvent.source_snapshot_id.in_(test_snapshot_ids),
            )
        )
    )
    session.execute(
        delete(ReviewEvent).where(
            or_(ReviewEvent.candidate_id.in_(removable_candidate_ids), ReviewEvent.version_id.in_(test_version_ids))
        )
    )
    session.execute(delete(UpdateCandidate).where(UpdateCandidate.id.in_(removable_candidate_ids)))
    session.execute(delete(ArticleChunk).where(ArticleChunk.article_id.in_(test_article_ids)))
    session.execute(delete(Article).where(Article.id.in_(test_article_ids)))
    session.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(test_version_ids)))
    session.execute(delete(SourceSnapshot).where(SourceSnapshot.id.in_(test_snapshot_ids)))
    session.execute(delete(SourceRegistry).where(SourceRegistry.id.in_(test_source_ids)))
    session.execute(delete(LegalInstrument).where(LegalInstrument.id.in_(test_instrument_ids)))

    # Remove traces and background-job history that explicitly mention deleted demo objects.
    deleted_version_strings = {str(v) for v in test_version_ids}
    for trace in session.scalars(select(QueryTrace)):
        if deleted_version_strings.intersection(trace.retrieved_version_ids or []):
            session.delete(trace)
    deleted_candidate_strings = {str(c) for c in removable_candidate_ids}
    for job in session.scalars(select(IngestionJob)):
        values = [job.payload or {}, job.result or {}]
        if any(v.get("is_test_data") is True for v in values) or any(
            v.get("candidate_id") in deleted_candidate_strings for v in values
        ):
            session.delete(job)

    # Old manifests may point to test versions. Retaining them would make rollback unsafe.
    session.execute(update(IndexGeneration).values(parent_id=None, is_active=False))
    session.execute(delete(IndexGeneration))
    session.flush()
    generation = activate_generation(
        session,
        trigger="bootstrap",
        changed=[],
        stats=IndexStats(),
        note="official-only corpus after test data purge",
    )
    counts["active_official_versions"] = generation.version_count
    return "test data removed: " + ", ".join(f"{key}={value}" for key, value in counts.items())


def backfill_chunk_hashes(session: Session, settings: Settings) -> str:
    """Chunks indexed before incremental embedding have no content hash, so the first amendment
    would re-embed every article. Hashing them once (no model call) makes reuse work. Idempotent."""
    rows = session.execute(
        select(ArticleChunk, Article, LegalInstrument)
        .join(Article, ArticleChunk.article_id == Article.id)
        .join(DocumentVersion, Article.version_id == DocumentVersion.id)
        .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(ArticleChunk.content_hash.is_(None), ArticleChunk.embedding_model.is_not(None))
    ).all()
    for chunk, article, instrument in rows:
        embedding_input = f"{article_context(instrument.official_title_ar, article)}\n{chunk.chunk_text}"
        chunk.content_hash = content_hash(chunk.embedding_model, embedding_input)
    return f"chunk hashes backfilled: {len(rows)}"


def reindex_approved(session: Session, settings: Settings) -> str:
    """Atomically rebuild every active approved version with the configured embedder.

    This is required after changing EMBEDDING_PROVIDER, EMBEDDING_MODEL or EMBEDDING_DIM.
    `index_version` embeds before replacing existing rows, while the surrounding CLI transaction
    preserves the previous index if any provider call or generation health check fails.
    """
    embedder = get_embedder(settings)
    versions = session.scalars(
        select(DocumentVersion)
        .options(selectinload(DocumentVersion.instrument), selectinload(DocumentVersion.articles))
        .where(DocumentVersion.review_status == "approved", DocumentVersion.index_active.is_(True))
        .order_by(DocumentVersion.id)
    ).all()
    stats = IndexStats()
    for version in versions:
        stats += index_version(session, version, embedder, settings)
    generation = activate_generation(
        session,
        trigger="manual",
        changed=list(versions),
        stats=stats,
        note=f"full approved-corpus reindex with {embedder.model_name}",
    )
    return (
        f"approved corpus reindexed: model={embedder.model_name}, versions={len(versions)}, "
        f"chunks={stats.chunks}, embedded={stats.embedded}, reused={stats.reused}, "
        f"generation={generation.number}"
    )


def nightly_sync(settings: Settings) -> int:
    from legal_core.logconfig import configure_logging
    from legal_core.sync import run_due_syncs

    configure_logging(settings.log_level)
    results = run_due_syncs(get_sessionmaker(), settings, get_embedder(settings))
    for logical, status in results:
        print(f"{logical.isoformat()}: {status}")
    if not results:
        print("no sync due (already done today, or another run holds the lock)")
    return 1 if any(status == "failed" for _, status in results) else 0


def sync_now(settings: Settings) -> int:
    """Operator CLI equivalent of the Operations screen's full-sync button."""
    from legal_core.logconfig import configure_logging
    from legal_core.sync import run_manual_sync, trigger_manual_run

    configure_logging(settings.log_level)
    session_factory = get_sessionmaker()
    with session_factory() as session:
        run = trigger_manual_run(session, settings, actor_id=None)
        run_id = run.id
        session.commit()
    status = run_manual_sync(session_factory, run_id, settings, get_embedder(settings))
    print(f"manual sync {run_id}: {status}")
    return 1 if status == "failed" else 0


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(prog="legal_core.cli")
    commands = [
        "create-admin",
        "seed-sources",
        "bootstrap",
        "purge-test-data",
        "reindex-approved",
        "nightly-sync",
        "sync-now",
    ]
    parser.add_argument("command", choices=commands)
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.assert_safe_for_environment()
    if args.command == "nightly-sync":
        return nightly_sync(settings)
    if args.command == "sync-now":
        return sync_now(settings)
    steps = {
        "create-admin": [create_admin],
        "seed-sources": [seed_sources],
        "bootstrap": [create_admin, seed_sources, backfill_chunk_hashes],
        "purge-test-data": [purge_test_data, seed_sources],
        "reindex-approved": [reindex_approved],
    }[args.command]
    for step in steps:
        with session_scope() as session:
            print(step(session, settings))


if __name__ == "__main__":
    sys.exit(main())

"""Retrieval generations (PRD v2 lifecycle steps 8-9, FR-12).

Callers stage their changes (new versions indexed, effective intervals adjusted) inside one
database transaction and then call `activate_generation`. It health-checks the staged corpus and
records a new active generation, or raises `IndexHealthError` so the caller rolls the whole
transaction back. Readers therefore only ever see a fully validated corpus.

Rollback re-applies an earlier generation's manifest. Chunks of every version are retained, so a
rollback needs no re-embedding.
"""

import uuid
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from legal_core.indexing import IndexStats
from legal_core.models import Article, ArticleChunk, DocumentVersion, IndexGeneration, utcnow
from legal_core.normalize import lexical_tokens

_GENERATION_LOCK = 0x5A1E_6E4E  # pg_advisory_xact_lock key serializing generation switches
_RECALL_SAMPLES = 3


class IndexHealthError(RuntimeError):
    def __init__(self, health: dict) -> None:
        failed = [name for name, check in health["checks"].items() if not check["ok"]]
        super().__init__(f"index health check failed: {', '.join(failed)}")
        self.health = health


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def current_manifest(session: Session) -> dict[str, dict]:
    rows = session.execute(
        select(DocumentVersion.id, DocumentVersion.effective_from, DocumentVersion.effective_to, DocumentVersion.end_reason)
        .where(DocumentVersion.review_status == "approved", DocumentVersion.index_active.is_(True))
    ).all()
    return {
        str(vid): {"effective_from": _iso(start), "effective_to": _iso(end), "end_reason": reason}
        for vid, start, end, reason in rows
    }


def active_generation(session: Session) -> IndexGeneration | None:
    return session.scalars(select(IndexGeneration).where(IndexGeneration.is_active.is_(True))).first()


def active_generation_id(session: Session) -> uuid.UUID | None:
    return session.scalar(select(IndexGeneration.id).where(IndexGeneration.is_active.is_(True)))


def _check(ok: bool, **details) -> dict:
    return {"ok": bool(ok), **details}


def check_health(session: Session, manifest: dict[str, dict], changed: list[DocumentVersion]) -> dict:
    """Document count, affected articles, embedding completeness, citation resolution, sample recall
    and non-overlapping intervals (PRD v2 10.3)."""
    version_ids = [uuid.UUID(v) for v in manifest]
    changed_ids = [v.id for v in changed]
    checks: dict[str, dict] = {}

    checks["document_count"] = _check(len(version_ids) > 0 or not changed, versions=len(version_ids))

    missing_embeddings = session.scalar(
        select(func.count(ArticleChunk.id))
        .join(Article, ArticleChunk.article_id == Article.id)
        .where(Article.version_id.in_(version_ids), ArticleChunk.embedding.is_(None))
    ) or 0
    checks["embedding_completeness"] = _check(missing_embeddings == 0, chunks_without_embedding=missing_embeddings)

    unindexed = session.scalars(
        select(Article.id).where(
            Article.version_id.in_(changed_ids),
            func.length(func.trim(Article.original_text_ar)) > 0,
            ~select(ArticleChunk.id).where(ArticleChunk.article_id == Article.id).exists(),
        )
    ).all()
    affected = session.scalar(select(func.count(Article.id)).where(Article.version_id.in_(changed_ids))) or 0
    checks["affected_article_count"] = _check(not unindexed, articles=affected, articles_without_chunks=len(unindexed))

    unresolvable = [str(v.id) for v in changed if not (v.source_url or v.instrument.canonical_source_url)]
    checks["citation_resolution"] = _check(not unresolvable, versions_without_source_url=unresolvable)

    misses: list[str] = []
    sampled = 0
    for version in changed:
        for article in [a for a in version.articles if a.original_text_ar.strip()][:_RECALL_SAMPLES]:
            tokens = list(dict.fromkeys(t for t in lexical_tokens(article.original_text_ar) if len(t) >= 3))[:4]
            if not tokens:
                continue
            sampled += 1
            found = session.scalar(
                select(func.count(ArticleChunk.id)).where(
                    ArticleChunk.article_id == article.id,
                    ArticleChunk.lexical_index.op("@@")(func.to_tsquery("simple", " & ".join(tokens))),
                )
            )
            if not found:
                misses.append(str(article.id))
    checks["sample_query_recall"] = _check(not misses, sampled=sampled, misses=misses)

    overlaps = session.execute(
        text(
            """
            SELECT a.id::text, b.id::text FROM document_versions a JOIN document_versions b
              ON a.instrument_id = b.instrument_id AND a.id < b.id
            WHERE a.id = ANY(:ids) AND b.id = ANY(:ids)
              AND daterange(a.effective_from, a.effective_to) && daterange(b.effective_from, b.effective_to)
            """
        ),
        {"ids": version_ids},
    ).all()
    checks["non_overlapping_intervals"] = _check(not overlaps, overlapping=[list(o) for o in overlaps])

    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


def activate_generation(
    session: Session,
    *,
    trigger: str,
    changed: list[DocumentVersion],
    stats: IndexStats | None = None,
    sync_run_id: uuid.UUID | None = None,
    note: str | None = None,
    parent_id: uuid.UUID | None = None,
) -> IndexGeneration:
    """Validate the staged corpus and make it the active generation, atomically with the caller's
    transaction. Raises IndexHealthError (nothing activated) when a check fails."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _GENERATION_LOCK})
    session.flush()
    manifest = current_manifest(session)
    health = check_health(session, manifest, changed)
    if not health["ok"]:
        raise IndexHealthError(health)
    stats = stats or IndexStats()
    previous = active_generation(session)
    now = utcnow()
    if previous is not None:
        previous.is_active, previous.validation_status, previous.retired_at = False, "retired", now
        session.flush()  # the partial unique index allows one active row at a time
    number = (session.scalar(select(func.max(IndexGeneration.number))) or 0) + 1
    article_count = session.scalar(
        select(func.count(Article.id)).where(Article.version_id.in_([uuid.UUID(v) for v in manifest]))
    ) or 0
    generation = IndexGeneration(
        number=number,
        trigger=trigger,
        validation_status="active",
        is_active=True,
        built_at=now,
        activated_at=now,
        parent_id=parent_id or (previous.id if previous else None),
        sync_run_id=sync_run_id,
        version_count=len(manifest),
        article_count=article_count,
        changed_chunk_count=stats.embedded,
        reused_chunk_count=stats.reused,
        manifest=manifest,
        health=health,
        note=note,
    )
    session.add(generation)
    session.flush()
    return generation


def record_failed_generation(
    session: Session, *, trigger: str, health: dict, sync_run_id: uuid.UUID | None, note: str
) -> IndexGeneration:
    """Keep a trace of a staged generation that failed validation (written after the rollback)."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _GENERATION_LOCK})
    number = (session.scalar(select(func.max(IndexGeneration.number))) or 0) + 1
    generation = IndexGeneration(
        number=number, trigger=trigger, validation_status="failed", is_active=False, health=health,
        sync_run_id=sync_run_id, note=note, manifest={},
    )  # fmt: skip
    session.add(generation)
    session.flush()
    return generation


def previous_generation(session: Session, current: IndexGeneration) -> IndexGeneration | None:
    return session.scalars(
        select(IndexGeneration)
        .where(IndexGeneration.validation_status == "retired", IndexGeneration.number < current.number)
        .order_by(IndexGeneration.number.desc())
    ).first()


def apply_manifest(session: Session, manifest: dict[str, dict]) -> list[DocumentVersion]:
    """Make exactly the manifest's versions authoritative, with its intervals. Returns the versions
    whose state changed."""
    wanted = {uuid.UUID(k): v for k, v in manifest.items()}
    touched: list[DocumentVersion] = []
    current = session.scalars(
        select(DocumentVersion).where(DocumentVersion.review_status == "approved", DocumentVersion.index_active.is_(True))
    ).all()
    for version in current:
        if version.id not in wanted:
            version.review_status, version.index_active = "rolled_back", False
            touched.append(version)
    session.flush()
    for version_id, interval in wanted.items():
        version = session.get(DocumentVersion, version_id)
        if version is None:
            raise LookupError(f"version {version_id} from the generation manifest no longer exists")
        start = date.fromisoformat(interval["effective_from"]) if interval["effective_from"] else None
        end = date.fromisoformat(interval["effective_to"]) if interval["effective_to"] else None
        state = (version.review_status, version.index_active, version.effective_from, version.effective_to, version.end_reason)
        version.review_status, version.index_active = "approved", True
        version.effective_from, version.effective_to, version.end_reason = start, end, interval["end_reason"]
        if state != (version.review_status, version.index_active, start, end, interval["end_reason"]):
            touched.append(version)
    session.flush()
    return touched

"""Relational + vector schema (PRD section 11).

Invariants:
- An article belongs to exactly one document version; article text is never updated.
- "Current law" is a time-dependent view over versions, never an overwritten row.
- Only versions with review_status='approved' and index_active are retrievable.
"""

import uuid
from datetime import UTC, date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from legal_core.config import get_settings

DOMAINS = ("labor", "commercial", "data_protection", "other")
INSTRUMENT_TYPES = ("law", "implementing_regulation", "regulation", "guidance", "notice", "draft", "other")
REVIEW_STATUSES = ("retrieved", "parsed", "needs_review", "approved", "rejected", "failed", "rolled_back")
CANDIDATE_STATES = ("candidate", "retrieved", "parsed", "needs_review", "approved", "rejected", "failed")
CLASSIFICATIONS = (
    "new_law",
    "amendment",
    "regulation",
    "royal_decree",
    "decision",
    "circular",
    "notice",
    "irrelevant",
)
CRAWL_POLICIES = ("feed_only", "fetch_allowed", "fixture", "manual")
JOB_TYPES = ("document_ingest", "feed_sync", "reindex")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "duplicate")
ROLES = ("user", "admin")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_users_role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    password_hash: Mapped[str] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    # Tokens issued before this moment are rejected (password reset ends other sessions).
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PasswordResetToken(Base):
    """Single-use, short-lived reset token. Only a SHA-256 of the token is stored."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LegalInstrument(Base):
    __tablename__ = "legal_instruments"
    __table_args__ = (
        CheckConstraint(_in("domain", DOMAINS), name="ck_instruments_domain"),
        CheckConstraint(_in("instrument_type", INSTRUMENT_TYPES), name="ck_instruments_type"),
        Index(
            "ix_instruments_title_trgm",
            "title_normalized",
            postgresql_using="gin",
            postgresql_ops={"title_normalized": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    official_title_ar: Mapped[str] = mapped_column(Text)
    title_normalized: Mapped[str] = mapped_column(Text, unique=True)
    domain: Mapped[str] = mapped_column(String(40), default="other", index=True)
    issuing_authority: Mapped[str | None] = mapped_column(Text)
    instrument_type: Mapped[str] = mapped_column(String(40), default="law")
    canonical_source_url: Mapped[str | None] = mapped_column(Text)
    current_review_status: Mapped[str] = mapped_column(String(20), default="needs_review")
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="instrument")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "raw_hash", name="uq_versions_instrument_hash"),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_versions_review_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from",
            name="ck_versions_interval",
        ),
        # Authoritative content must have a confirmed effective date (never inferred).
        CheckConstraint(
            "review_status <> 'approved' OR effective_from IS NOT NULL",
            name="ck_versions_approved_has_effective_from",
        ),
        Index("ix_versions_retrievable", "instrument_id", "effective_from", "effective_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("legal_instruments.id"), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_authority: Mapped[str | None] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64), index=True)
    version_label: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[date | None] = mapped_column(Date)
    # Legal validity interval; effective_to is exclusive.
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    end_reason: Mapped[str | None] = mapped_column(String(20))  # superseded | repealed
    review_status: Mapped[str] = mapped_column(String(20), default="parsed", index=True)
    index_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # System knowledge time (observed_at), distinct from legal validity.
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    storage_path: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_gaps: Mapped[list] = mapped_column(JSON, default=list)
    parse_warnings: Mapped[list] = mapped_column(JSON, default=list)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    instrument: Mapped[LegalInstrument] = relationship(back_populates="versions")
    articles: Mapped[list["Article"]] = relationship(
        back_populates="version", order_by="Article.ordering", cascade="all, delete-orphan"
    )


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("version_id", "ordering", name="uq_articles_version_ordering"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id"), index=True)
    article_number: Mapped[str | None] = mapped_column(String(40), index=True)
    article_label: Mapped[str | None] = mapped_column(Text)
    chapter: Mapped[str | None] = mapped_column(Text)
    original_text_ar: Mapped[str] = mapped_column(Text)
    normalized_text_ar: Mapped[str] = mapped_column(Text)
    ordering: Mapped[int] = mapped_column(Integer)

    version: Mapped[DocumentVersion] = relationship(back_populates="articles")
    chunks: Mapped[list["ArticleChunk"]] = relationship(
        back_populates="article", order_by="ArticleChunk.chunk_index", cascade="all, delete-orphan"
    )


class ArticleChunk(Base):
    __tablename__ = "article_chunks"
    __table_args__ = (
        Index("ix_chunks_lexical", "lexical_index", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("articles.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_text: Mapped[str] = mapped_column(Text)
    source_offset: Mapped[int] = mapped_column(Integer, default=0)
    lexical_text: Mapped[str] = mapped_column(Text)
    lexical_index: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', lexical_text)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(get_settings().embedding_dim))
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    article: Mapped[Article] = relationship(back_populates="chunks")


class SourceRegistry(Base):
    __tablename__ = "source_registry"
    __table_args__ = (CheckConstraint(_in("crawl_policy", CRAWL_POLICIES), name="ck_sources_policy"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    authority: Mapped[str] = mapped_column(Text)
    feed_url: Mapped[str] = mapped_column(Text)
    allowlisted_host: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    crawl_policy: Mapped[str] = mapped_column(String(20), default="feed_only")
    check_interval: Mapped[int] = mapped_column(Integer, default=360)  # minutes
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False)


class UpdateCandidate(Base):
    __tablename__ = "update_candidates"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", "changed_hash", name="uq_candidates_identity"),
        CheckConstraint(_in("state", CANDIDATE_STATES), name="ck_candidates_state"),
        CheckConstraint(_in("classification", CLASSIFICATIONS), name="ck_candidates_classification"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_registry.id"), index=True)
    origin: Mapped[str] = mapped_column(String(20), default="feed")  # feed | manual_upload
    external_id: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_hash: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(20), default="notice")
    classification_source: Mapped[str] = mapped_column(String(20), default="auto")  # auto | reviewer
    target_instrument_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_instruments.id"))
    proposed_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_versions.id"))
    state: Mapped[str] = mapped_column(String(20), default="candidate", index=True)
    diff: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[SourceRegistry | None] = relationship()
    target_instrument: Mapped[LegalInstrument | None] = relationship()
    proposed_version: Mapped[DocumentVersion | None] = relationship()


class ReviewEvent(Base):
    """Append-only audit trail of reviewer decisions and index activations."""

    __tablename__ = "review_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("update_candidates.id"), index=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_versions.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(40))
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    effective_date_evidence: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(_in("job_type", JOB_TYPES), name="ck_jobs_type"),
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class QueryTrace(Base):
    """Links answer <-> retrieved citations <-> document versions. No user identifiers."""

    __tablename__ = "query_traces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    query_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    query_hash: Mapped[str] = mapped_column(String(64))
    query_text: Mapped[str | None] = mapped_column(Text)  # only when TRACE_STORE_QUERY_TEXT=true
    query_language: Mapped[str | None] = mapped_column(String(10))
    domain: Mapped[str | None] = mapped_column(String(40))
    as_of_date: Mapped[date] = mapped_column(Date)
    retrieved: Mapped[list] = mapped_column(JSON, default=list)
    retrieved_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    cited_article_ids: Mapped[list] = mapped_column(JSON, default=list)
    response_status: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    timings_ms: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_model: Mapped[str | None] = mapped_column(String(100))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    auto_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

"""Initial schema: versioned legal corpus, vector index, review queue, jobs, traces.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

from legal_core.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def _in(column: str, *values: str) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("last_login_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(_in("role", "user", "admin"), name="ck_users_role"),
    )

    op.create_table(
        "legal_instruments",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("official_title_ar", sa.Text, nullable=False),
        sa.Column("title_normalized", sa.Text, nullable=False, unique=True),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("issuing_authority", sa.Text),
        sa.Column("instrument_type", sa.String(40), nullable=False),
        sa.Column("canonical_source_url", sa.Text),
        sa.Column("current_review_status", sa.String(20), nullable=False),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(_in("domain", "labor", "commercial", "data_protection", "other"), name="ck_instruments_domain"),
        sa.CheckConstraint(
            _in("instrument_type", "law", "implementing_regulation", "regulation", "guidance", "notice", "draft", "other"),
            name="ck_instruments_type",
        ),
    )
    op.create_index("ix_legal_instruments_domain", "legal_instruments", ["domain"])
    op.create_index(
        "ix_instruments_title_trgm",
        "legal_instruments",
        ["title_normalized"],
        postgresql_using="gin",
        postgresql_ops={"title_normalized": "gin_trgm_ops"},
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("instrument_id", sa.Uuid, sa.ForeignKey("legal_instruments.id"), nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("source_authority", sa.Text),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("version_label", sa.String(200)),
        sa.Column("issued_at", sa.Date),
        sa.Column("published_at", sa.Date),
        sa.Column("effective_from", sa.Date),
        sa.Column("effective_to", sa.Date),
        sa.Column("end_reason", sa.String(20)),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("index_active", sa.Boolean, nullable=False),
        sa.Column("retrieved_at", TS, nullable=False),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("storage_path", sa.Text),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("article_count", sa.Integer, nullable=False),
        sa.Column("metadata_gaps", sa.JSON, nullable=False),
        sa.Column("parse_warnings", sa.JSON, nullable=False),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.Column("created_by", sa.Uuid, sa.ForeignKey("users.id")),
        sa.UniqueConstraint("instrument_id", "raw_hash", name="uq_versions_instrument_hash"),
        sa.CheckConstraint(
            _in("review_status", "retrieved", "parsed", "needs_review", "approved", "rejected", "failed", "rolled_back"),
            name="ck_versions_review_status",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from", name="ck_versions_interval"
        ),
        sa.CheckConstraint(
            "review_status <> 'approved' OR effective_from IS NOT NULL", name="ck_versions_approved_has_effective_from"
        ),
    )
    op.create_index("ix_document_versions_instrument_id", "document_versions", ["instrument_id"])
    op.create_index("ix_document_versions_raw_hash", "document_versions", ["raw_hash"])
    op.create_index("ix_document_versions_review_status", "document_versions", ["review_status"])
    op.create_index("ix_versions_retrievable", "document_versions", ["instrument_id", "effective_from", "effective_to"])

    op.create_table(
        "articles",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("version_id", sa.Uuid, sa.ForeignKey("document_versions.id"), nullable=False),
        sa.Column("article_number", sa.String(40)),
        sa.Column("article_label", sa.Text),
        sa.Column("chapter", sa.Text),
        sa.Column("original_text_ar", sa.Text, nullable=False),
        sa.Column("normalized_text_ar", sa.Text, nullable=False),
        sa.Column("ordering", sa.Integer, nullable=False),
        sa.UniqueConstraint("version_id", "ordering", name="uq_articles_version_ordering"),
    )
    op.create_index("ix_articles_version_id", "articles", ["version_id"])
    op.create_index("ix_articles_article_number", "articles", ["article_number"])

    op.create_table(
        "article_chunks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("article_id", sa.Uuid, sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("source_offset", sa.Integer, nullable=False),
        sa.Column("lexical_text", sa.Text, nullable=False),
        sa.Column("lexical_index", TSVECTOR, sa.Computed("to_tsvector('simple', lexical_text)", persisted=True)),
        # Dimension is fixed at migration time; changing EMBEDDING_DIM later needs a new migration.
        sa.Column("embedding", Vector(get_settings().embedding_dim)),
        sa.Column("embedding_model", sa.String(100)),
    )
    op.create_index("ix_article_chunks_article_id", "article_chunks", ["article_id"])
    op.create_index("ix_chunks_lexical", "article_chunks", ["lexical_index"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_embedding",
        "article_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "source_registry",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("authority", sa.Text, nullable=False),
        sa.Column("feed_url", sa.Text, nullable=False),
        sa.Column("allowlisted_host", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("crawl_policy", sa.String(20), nullable=False),
        sa.Column("check_interval", sa.Integer, nullable=False),
        sa.Column("last_checked_at", TS),
        sa.Column("last_success_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.CheckConstraint(_in("crawl_policy", "feed_only", "fetch_allowed", "fixture", "manual"), name="ck_sources_policy"),
    )

    op.create_table(
        "update_candidates",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("source_registry.id")),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("canonical_url", sa.Text),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("published_at", TS),
        sa.Column("discovered_at", TS, nullable=False),
        sa.Column("changed_hash", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("classification_source", sa.String(20), nullable=False),
        sa.Column("target_instrument_id", sa.Uuid, sa.ForeignKey("legal_instruments.id")),
        sa.Column("proposed_version_id", sa.Uuid, sa.ForeignKey("document_versions.id")),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("diff", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.UniqueConstraint("source_id", "external_id", "changed_hash", name="uq_candidates_identity"),
        sa.CheckConstraint(
            _in("state", "candidate", "retrieved", "parsed", "needs_review", "approved", "rejected", "failed"),
            name="ck_candidates_state",
        ),
        sa.CheckConstraint(
            _in("classification", "new_law", "amendment", "regulation", "notice", "irrelevant"),
            name="ck_candidates_classification",
        ),
    )
    op.create_index("ix_update_candidates_source_id", "update_candidates", ["source_id"])
    op.create_index("ix_update_candidates_state", "update_candidates", ["state"])

    op.create_table(
        "review_events",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("candidate_id", sa.Uuid, sa.ForeignKey("update_candidates.id")),
        sa.Column("version_id", sa.Uuid, sa.ForeignKey("document_versions.id")),
        sa.Column("actor_id", sa.Uuid, sa.ForeignKey("users.id")),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("decision_at", TS, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("effective_date_evidence", sa.Text),
        sa.Column("details", sa.JSON, nullable=False),
    )
    op.create_index("ix_review_events_candidate_id", "review_events", ["candidate_id"])
    op.create_index("ix_review_events_version_id", "review_events", ["version_id"])
    op.create_index("ix_review_events_decision_at", "review_events", ["decision_at"])

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("retry_count", sa.Integer, nullable=False),
        sa.Column("errors", sa.JSON, nullable=False),
        sa.Column("counts", sa.JSON, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
        sa.Column("created_by", sa.Uuid, sa.ForeignKey("users.id")),
        sa.CheckConstraint(_in("job_type", "document_ingest", "feed_sync", "reindex"), name="ck_jobs_type"),
        sa.CheckConstraint(_in("status", "queued", "running", "succeeded", "failed", "duplicate"), name="ck_jobs_status"),
    )
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_index("ix_ingestion_jobs_created_at", "ingestion_jobs", ["created_at"])

    op.create_table(
        "query_traces",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("query_time", TS, nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("query_text", sa.Text),
        sa.Column("query_language", sa.String(10)),
        sa.Column("domain", sa.String(40)),
        sa.Column("as_of_date", sa.Date, nullable=False),
        sa.Column("retrieved", sa.JSON, nullable=False),
        sa.Column("retrieved_version_ids", sa.JSON, nullable=False),
        sa.Column("cited_article_ids", sa.JSON, nullable=False),
        sa.Column("response_status", sa.String(30), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("timings_ms", sa.JSON, nullable=False),
        sa.Column("llm_model", sa.String(100)),
        sa.Column("embedding_model", sa.String(100)),
        sa.Column("auto_delete_at", TS),
    )
    op.create_index("ix_query_traces_query_time", "query_traces", ["query_time"])
    op.create_index("ix_query_traces_auto_delete_at", "query_traces", ["auto_delete_at"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(100), primary_key=True),
        sa.Column("last_seen_at", TS, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "worker_heartbeats",
        "query_traces",
        "ingestion_jobs",
        "review_events",
        "update_candidates",
        "source_registry",
        "article_chunks",
        "articles",
        "document_versions",
        "legal_instruments",
        "users",
    ):
        op.drop_table(table)

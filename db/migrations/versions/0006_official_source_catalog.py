"""Official source catalog, connector health, judicial decisions, document relationships,
per-source sync logs and a wider legal-domain taxonomy.

Views named after the integration specification (official_sources, legal_documents, ...) are thin
aliases over the existing tables so both vocabularies query the same rows.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

_DOMAINS_OLD = ("labor", "commercial", "data_protection", "other")
_DOMAINS_NEW = (
    *_DOMAINS_OLD, "real_estate", "tax_customs", "banking_finance", "capital_markets", "insurance", "investment",
    "social_insurance", "health", "food_drug", "transport", "municipal", "cybersecurity", "digital_telecom",
    "intellectual_property", "judicial", "administrative", "criminal", "consumer", "competition",
)  # fmt: skip
_HEALTH = ("unknown", "available", "degraded", "unavailable", "blocked", "robots_disallowed")
_INTEGRATION = ("integrated", "discovery_only", "not_integrated", "consolidated")
_COURT_LEVELS = ("first_instance", "appeal", "supreme", "committee", "unknown")
_RELATIONS = ("implementing_regulation_of", "amends", "repeals", "related")
_LOG_STATUSES = ("changed", "no_change", "failed", "skipped")
_JOBS_OLD = ("document_ingest", "feed_sync", "reindex", "nightly_sync")
_JOBS_NEW = (*_JOBS_OLD, "source_probe")

_VIEWS = {
    "official_sources": "SELECT * FROM source_registry",
    "source_connectors": (
        "SELECT id AS source_id, source_key, name, connector_type, feed_url AS endpoint_url, connector_config, "
        "crawl_policy, enabled, integration_status, health_status, health_detail, last_http_status, last_probe_at, "
        "last_checked_at, last_success_at, last_error FROM source_registry"
    ),
    "legal_documents": "SELECT * FROM legal_instruments",
    "legal_document_versions": "SELECT * FROM document_versions",
    "legal_articles": "SELECT * FROM articles",
    "legal_amendments": "SELECT * FROM amendment_relations",
    "legal_chunks": "SELECT * FROM article_chunks",
    "retrieval_audit_logs": "SELECT * FROM query_traces",
}


def _in(column: str, *values: str) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint("ck_instruments_domain", "legal_instruments", type_="check")
    op.create_check_constraint("ck_instruments_domain", "legal_instruments", _in("domain", *_DOMAINS_NEW))
    op.drop_constraint("ck_jobs_type", "ingestion_jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "ingestion_jobs", _in("job_type", *_JOBS_NEW))

    columns = [
        sa.Column("source_key", sa.String(60)),
        sa.Column("catalog_numbers", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("name_ar", sa.Text),
        sa.Column("official_url", sa.Text),
        sa.Column("category", sa.String(2)),
        sa.Column("authority_tier", sa.String(20), nullable=False, server_default="primary"),
        sa.Column("document_kind", sa.String(20), nullable=False, server_default="gazette"),
        sa.Column("connector_type", sa.String(30), nullable=False, server_default="rss"),
        sa.Column("connector_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("default_domain", sa.String(40), nullable=False, server_default="other"),
        sa.Column("consolidated_into", sa.String(60)),
        sa.Column("integration_status", sa.String(20), nullable=False, server_default="integrated"),
        sa.Column("health_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("health_detail", sa.Text),
        sa.Column("last_http_status", sa.Integer),
        sa.Column("last_probe_at", TS),
        sa.Column("page_fingerprint", sa.String(64)),
        sa.Column("access_notes", sa.Text),
        sa.Column("licensing_status", sa.String(40), nullable=False, server_default="public_terms_unreviewed"),
    ]
    for column in columns:
        op.add_column("source_registry", column)
    op.create_unique_constraint("source_registry_source_key_key", "source_registry", ["source_key"])
    op.create_check_constraint("ck_sources_health", "source_registry", _in("health_status", *_HEALTH))
    op.create_check_constraint("ck_sources_integration", "source_registry", _in("integration_status", *_INTEGRATION))

    op.create_table(
        "judicial_decisions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("source_registry.id"), nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("material_type", sa.String(30), nullable=False),
        sa.Column("court_name", sa.Text),
        sa.Column("court_level", sa.String(20), nullable=False),
        sa.Column("city", sa.Text),
        sa.Column("case_number", sa.String(80)),
        sa.Column("judgment_number", sa.String(80)),
        sa.Column("hijri_date", sa.String(20)),
        sa.Column("judgment_date", sa.Date),
        sa.Column("finality_status", sa.String(20), nullable=False),
        sa.Column("principles", sa.JSON, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.Uuid, sa.ForeignKey("source_snapshots.id")),
        sa.Column("retrieved_at", TS, nullable=False),
        sa.Column("lexical_text", sa.Text, nullable=False),
        sa.Column("lexical_index", TSVECTOR, sa.Computed("to_tsvector('simple', lexical_text)", persisted=True)),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.UniqueConstraint("source_id", "external_id", name="uq_judicial_source_external"),
        sa.CheckConstraint(_in("court_level", *_COURT_LEVELS), name="ck_judicial_court_level"),
    )
    op.create_index("ix_judicial_decisions_source_id", "judicial_decisions", ["source_id"])
    op.create_index("ix_judicial_decisions_judgment_date", "judicial_decisions", ["judgment_date"])
    op.create_index("ix_judicial_lexical", "judicial_decisions", ["lexical_index"], postgresql_using="gin")

    op.create_table(
        "document_relationships",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("from_instrument_id", sa.Uuid, sa.ForeignKey("legal_instruments.id"), nullable=False),
        sa.Column("to_instrument_id", sa.Uuid, sa.ForeignKey("legal_instruments.id"), nullable=False),
        sa.Column("relation_type", sa.String(40), nullable=False),
        sa.Column("evidence", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("from_instrument_id", "to_instrument_id", "relation_type", name="uq_document_relationship"),
        sa.CheckConstraint(_in("relation_type", *_RELATIONS), name="ck_document_relationship_type"),
    )
    op.create_index("ix_document_relationships_from_instrument_id", "document_relationships", ["from_instrument_id"])
    op.create_index("ix_document_relationships_to_instrument_id", "document_relationships", ["to_instrument_id"])

    op.create_table(
        "source_sync_logs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("source_registry.id"), nullable=False),
        sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("connector_type", sa.String(30), nullable=False),
        sa.Column("http_status", sa.Integer),
        sa.Column("counts", sa.JSON, nullable=False),
        sa.Column("error", sa.Text),
        sa.CheckConstraint(_in("status", *_LOG_STATUSES), name="ck_source_sync_logs_status"),
    )
    op.create_index("ix_source_sync_logs_source_id", "source_sync_logs", ["source_id"])
    op.create_index("ix_source_sync_logs_sync_run_id", "source_sync_logs", ["sync_run_id"])
    op.create_index("ix_source_sync_logs_started_at", "source_sync_logs", ["started_at"])

    for name, select in _VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {select}")


def downgrade() -> None:
    for name in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.drop_table("source_sync_logs")
    op.drop_table("document_relationships")
    op.drop_table("judicial_decisions")
    op.drop_constraint("ck_sources_integration", "source_registry", type_="check")
    op.drop_constraint("ck_sources_health", "source_registry", type_="check")
    op.drop_constraint("source_registry_source_key_key", "source_registry", type_="unique")
    for column in (
        "licensing_status", "access_notes", "page_fingerprint", "last_probe_at", "last_http_status", "health_detail",
        "health_status", "integration_status", "consolidated_into", "default_domain", "connector_config",
        "connector_type", "document_kind", "authority_tier", "category", "official_url", "name_ar", "catalog_numbers",
        "source_key",
    ):  # fmt: skip
        op.drop_column("source_registry", column)
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'source_probe'")
    op.drop_constraint("ck_jobs_type", "ingestion_jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "ingestion_jobs", _in("job_type", *_JOBS_OLD))
    op.execute(f"UPDATE legal_instruments SET domain = 'other' WHERE NOT ({_in('domain', *_DOMAINS_OLD)})")
    op.drop_constraint("ck_instruments_domain", "legal_instruments", type_="check")
    op.create_check_constraint("ck_instruments_domain", "legal_instruments", _in("domain", *_DOMAINS_OLD))

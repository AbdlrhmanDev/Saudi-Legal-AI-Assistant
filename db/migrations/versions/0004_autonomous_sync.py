"""Autonomous nightly synchronization (PRD v2): sync runs, index generations, quarantine,
immutable source snapshots, amendment relations and shared rate-limit counters.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def _in(column: str, *values: str) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


_REVIEW_OLD = ("retrieved", "parsed", "needs_review", "approved", "rejected", "failed", "rolled_back")
_REVIEW_NEW = (*_REVIEW_OLD, "quarantined", "evidence")
_STATE_OLD = ("candidate", "retrieved", "parsed", "needs_review", "approved", "rejected", "failed")
_STATE_NEW = (*_STATE_OLD, "quarantined", "excluded")
_CLASS_OLD = ("new_law", "amendment", "regulation", "royal_decree", "decision", "circular", "notice", "irrelevant")
_CLASS_NEW = ("new_law", "amendment", "repeal", "regulation", "royal_decree", "decision", "circular", "notice",
              "draft", "irrelevant")  # fmt: skip
_JOBS_OLD = ("document_ingest", "feed_sync", "reindex")
_JOBS_NEW = (*_JOBS_OLD, "nightly_sync")


def _recheck(table: str, name: str, column: str, values: tuple[str, ...]) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, _in(column, *values))


def upgrade() -> None:
    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("source_registry.id")),
        sa.Column("canonical_url", sa.Text),
        sa.Column("source_key", sa.Text, nullable=False),
        sa.Column("body_checksum", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("content_type", sa.String(100)),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("retrieved_at", TS, nullable=False),
        sa.Column("published_at", sa.Date),
        sa.Column("is_test_data", sa.Boolean, nullable=False),
        sa.UniqueConstraint("source_key", "body_checksum", name="uq_snapshots_source_checksum"),
    )
    op.create_index("ix_source_snapshots_source_id", "source_snapshots", ["source_id"])
    op.create_index("ix_source_snapshots_body_checksum", "source_snapshots", ["body_checksum"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("run_key", sa.String(80), nullable=False, unique=True),
        sa.Column("logical_date_riyadh", sa.Date, nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("scheduled_at", TS),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("retry_count", sa.Integer, nullable=False),
        sa.Column("retry_after", TS),
        sa.Column("stage_counts", sa.JSON, nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("errors", sa.JSON, nullable=False),
        sa.Column("created_by", sa.Uuid, sa.ForeignKey("users.id")),
        sa.CheckConstraint(_in("trigger", "scheduled", "catch_up", "manual"), name="ck_sync_runs_trigger"),
        sa.CheckConstraint(
            _in("status", "queued", "running", "succeeded", "no_op", "partial", "failed"), name="ck_sync_runs_status"
        ),
    )
    op.create_index("ix_sync_runs_logical_date_riyadh", "sync_runs", ["logical_date_riyadh"])
    op.create_index("ix_sync_runs_status", "sync_runs", ["status"])

    op.create_table(
        "index_generations",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("number", sa.Integer, nullable=False, unique=True),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("validation_status", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("built_at", TS, nullable=False),
        sa.Column("activated_at", TS),
        sa.Column("retired_at", TS),
        sa.Column("parent_id", sa.Uuid, sa.ForeignKey("index_generations.id")),
        sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")),
        sa.Column("version_count", sa.Integer, nullable=False),
        sa.Column("article_count", sa.Integer, nullable=False),
        sa.Column("changed_chunk_count", sa.Integer, nullable=False),
        sa.Column("reused_chunk_count", sa.Integer, nullable=False),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("health", sa.JSON, nullable=False),
        sa.Column("note", sa.Text),
        sa.CheckConstraint(
            _in("validation_status", "building", "active", "retired", "failed"), name="ck_generations_status"
        ),
    )
    op.create_index("ix_index_generations_sync_run_id", "index_generations", ["sync_run_id"])
    op.create_index(
        "uq_generations_one_active", "index_generations", ["is_active"], unique=True, postgresql_where=sa.text("is_active")
    )

    op.create_table(
        "quarantine_events",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("candidate_id", sa.Uuid, sa.ForeignKey("update_candidates.id"), nullable=False),
        sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")),
        sa.Column("source_snapshot_id", sa.Uuid, sa.ForeignKey("source_snapshots.id")),
        sa.Column("version_id", sa.Uuid, sa.ForeignKey("document_versions.id")),
        sa.Column("reason_code", sa.String(60), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("source_evidence", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("retry_after", TS),
        sa.Column("resolution_state", sa.String(20), nullable=False),
        sa.Column("resolved_at", TS),
        sa.Column("resolved_by", sa.Uuid, sa.ForeignKey("users.id")),
        sa.CheckConstraint(_in("resolution_state", "open", "resolved", "superseded"), name="ck_quarantine_resolution"),
    )
    op.create_index("ix_quarantine_events_candidate_id", "quarantine_events", ["candidate_id"])
    op.create_index("ix_quarantine_events_sync_run_id", "quarantine_events", ["sync_run_id"])
    op.create_index("ix_quarantine_events_reason_code", "quarantine_events", ["reason_code"])
    op.create_index("ix_quarantine_events_created_at", "quarantine_events", ["created_at"])
    op.create_index("ix_quarantine_events_resolution_state", "quarantine_events", ["resolution_state"])

    op.create_table(
        "amendment_relations",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("amending_version_id", sa.Uuid, sa.ForeignKey("document_versions.id"), nullable=False),
        sa.Column("target_instrument_id", sa.Uuid, sa.ForeignKey("legal_instruments.id"), nullable=False),
        sa.Column("candidate_id", sa.Uuid, sa.ForeignKey("update_candidates.id")),
        sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("affected_article_number", sa.String(40)),
        sa.Column("before_article_id", sa.Uuid, sa.ForeignKey("articles.id")),
        sa.Column("after_article_id", sa.Uuid, sa.ForeignKey("articles.id")),
        sa.Column("effective_from", sa.Date),
        sa.Column("publication_evidence", sa.Text),
        sa.Column("date_evidence", sa.Text),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_amendment_relations_amending_version_id", "amendment_relations", ["amending_version_id"])
    op.create_index("ix_amendment_relations_target_instrument_id", "amendment_relations", ["target_instrument_id"])
    op.create_index("ix_amendment_relations_created_at", "amendment_relations", ["created_at"])

    # Counters are disposable: UNLOGGED skips the WAL, which keeps per-request writes cheap.
    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket", sa.String(200), nullable=False),
        sa.Column("window_start", TS, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("bucket", "window_start", name="pk_rate_limit_counters"),
        prefixes=["UNLOGGED"],
    )
    op.create_index("ix_rate_limit_counters_window_start", "rate_limit_counters", ["window_start"])

    op.add_column("document_versions", sa.Column("snapshot_id", sa.Uuid, sa.ForeignKey("source_snapshots.id")))
    op.add_column(
        "document_versions", sa.Column("validation_evidence", sa.JSON, nullable=False, server_default=sa.text("'{}'"))
    )
    op.create_index("ix_document_versions_snapshot_id", "document_versions", ["snapshot_id"])

    op.add_column("article_chunks", sa.Column("content_hash", sa.String(64)))
    op.create_index("ix_article_chunks_content_hash", "article_chunks", ["content_hash"])

    op.add_column("update_candidates", sa.Column("document_ref", sa.Text))
    op.add_column("update_candidates", sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")))
    op.add_column("update_candidates", sa.Column("evidence", sa.JSON, nullable=False, server_default=sa.text("'{}'")))
    op.create_index("ix_update_candidates_sync_run_id", "update_candidates", ["sync_run_id"])

    op.add_column("review_events", sa.Column("sync_run_id", sa.Uuid, sa.ForeignKey("sync_runs.id")))
    op.create_index("ix_review_events_sync_run_id", "review_events", ["sync_run_id"])

    _recheck("document_versions", "ck_versions_review_status", "review_status", _REVIEW_NEW)
    _recheck("update_candidates", "ck_candidates_state", "state", _STATE_NEW)
    _recheck("update_candidates", "ck_candidates_classification", "classification", _CLASS_NEW)
    _recheck("ingestion_jobs", "ck_jobs_type", "job_type", _JOBS_NEW)

    # Existing deployments: the currently served corpus becomes generation 1.
    op.execute(
        """
        INSERT INTO index_generations (id, number, trigger, validation_status, is_active, built_at, activated_at,
            version_count, article_count, changed_chunk_count, reused_chunk_count, manifest, health, note)
        SELECT gen_random_uuid(), 1, 'bootstrap', 'active', true, now(), now(), count(*),
            coalesce(sum(article_count), 0), 0, 0,
            json_object_agg(id::text, json_build_object(
                'effective_from', effective_from, 'effective_to', effective_to, 'end_reason', end_reason)),
            '{}'::json, 'Corpus served before autonomous synchronization was enabled.'
        FROM document_versions
        WHERE review_status = 'approved' AND index_active
        HAVING count(*) > 0
        """
    )


def downgrade() -> None:
    op.execute("UPDATE ingestion_jobs SET job_type = 'feed_sync' WHERE job_type = 'nightly_sync'")
    op.execute("UPDATE update_candidates SET classification = 'notice' WHERE classification IN ('repeal', 'draft')")
    op.execute("UPDATE update_candidates SET state = 'failed' WHERE state IN ('quarantined', 'excluded')")
    op.execute("UPDATE document_versions SET review_status = 'rejected' WHERE review_status IN ('quarantined', 'evidence')")
    _recheck("ingestion_jobs", "ck_jobs_type", "job_type", _JOBS_OLD)
    _recheck("update_candidates", "ck_candidates_classification", "classification", _CLASS_OLD)
    _recheck("update_candidates", "ck_candidates_state", "state", _STATE_OLD)
    _recheck("document_versions", "ck_versions_review_status", "review_status", _REVIEW_OLD)

    op.drop_index("ix_review_events_sync_run_id", "review_events")
    op.drop_column("review_events", "sync_run_id")
    op.drop_index("ix_update_candidates_sync_run_id", "update_candidates")
    op.drop_column("update_candidates", "evidence")
    op.drop_column("update_candidates", "sync_run_id")
    op.drop_column("update_candidates", "document_ref")
    op.drop_index("ix_article_chunks_content_hash", "article_chunks")
    op.drop_column("article_chunks", "content_hash")
    op.drop_index("ix_document_versions_snapshot_id", "document_versions")
    op.drop_column("document_versions", "validation_evidence")
    op.drop_column("document_versions", "snapshot_id")
    for table in ("rate_limit_counters", "amendment_relations", "quarantine_events", "index_generations", "sync_runs",
                  "source_snapshots"):  # fmt: skip
        op.drop_table(table)

"""Allow only one open quarantine event per update candidate.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old retries could leave several open audit events for one candidate. Preserve the newest
    # actionable event and close older entries before enforcing the invariant in PostgreSQL.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY candidate_id
                       ORDER BY created_at DESC, id DESC
                   ) AS position
            FROM quarantine_events
            WHERE resolution_state = 'open'
        )
        UPDATE quarantine_events AS event
        SET resolution_state = 'superseded',
            resolved_at = coalesce(event.resolved_at, now())
        FROM ranked
        WHERE event.id = ranked.id
          AND ranked.position > 1
        """
    )
    op.create_index(
        "uq_quarantine_one_open_per_candidate",
        "quarantine_events",
        ["candidate_id"],
        unique=True,
        postgresql_where=sa.text("resolution_state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_quarantine_one_open_per_candidate", table_name="quarantine_events")

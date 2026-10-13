"""Add specific legal update classifications.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_OLD = "'new_law', 'amendment', 'regulation', 'notice', 'irrelevant'"
_NEW = "'new_law', 'amendment', 'regulation', 'royal_decree', 'decision', 'circular', 'notice', 'irrelevant'"


def upgrade() -> None:
    op.drop_constraint("ck_candidates_classification", "update_candidates", type_="check")
    op.create_check_constraint("ck_candidates_classification", "update_candidates", f"classification IN ({_NEW})")


def downgrade() -> None:
    op.execute("UPDATE update_candidates SET classification = 'notice' WHERE classification IN ('royal_decree', 'decision', 'circular')")
    op.drop_constraint("ck_candidates_classification", "update_candidates", type_="check")
    op.create_check_constraint("ck_candidates_classification", "update_candidates", f"classification IN ({_OLD})")

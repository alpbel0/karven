"""fetch_job_waiters: parked ideas/visuals waiting on an on-demand fetch (1.5)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-03

Task 1.5 parks the idea/visual that needs an on-demand series while the fetch is
running, so it can resume where it left off once the data arrives. The new
``fetch_job_waiters`` table records that parking state:

- one row per ``(fetch_job_id, waiter_type, waiter_id)``; ``waiter_type`` is
  ``idea``/``visual`` (no CHECK yet: the values are Task 1.7's), ``waiter_id`` is
  text with no foreign key because those tables do not exist yet,
- ``status`` is ``waiting`` (parked), ``ready`` (job completed) or ``dropped``
  (job failed); a terminal fetch job resolves its ``waiting`` rows in the same
  transaction,
- ``fetch_job_id`` cascades on delete so a waiter never outlives its job.

Unlike the earlier forward-only migrations this one implements ``downgrade``:
Task 1.5's acceptance explicitly asks that it can be rolled back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WAITER_STATUS_CHECK = "status IN ('waiting', 'ready', 'dropped')"


def upgrade() -> None:
    """Create ``fetch_job_waiters``."""
    op.create_table(
        "fetch_job_waiters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fetch_job_id", sa.Integer(), nullable=False),
        sa.Column("waiter_type", sa.Text(), nullable=False),
        sa.Column("waiter_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'waiting'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["fetch_job_id"],
            ["fetch_jobs.id"],
            name="fk_fetch_job_waiters_job",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "fetch_job_id",
            "waiter_type",
            "waiter_id",
            name="uq_fetch_job_waiters_identity",
        ),
        sa.CheckConstraint(_WAITER_STATUS_CHECK, name="ck_fetch_job_waiters_status"),
    )
    op.create_index(
        "ix_fetch_job_waiters_waiter", "fetch_job_waiters", ["waiter_type", "waiter_id"]
    )
    op.create_index("ix_fetch_job_waiters_job", "fetch_job_waiters", ["fetch_job_id"])


def downgrade() -> None:
    """Drop ``fetch_job_waiters`` and its indexes."""
    op.drop_index("ix_fetch_job_waiters_job", table_name="fetch_job_waiters")
    op.drop_index("ix_fetch_job_waiters_waiter", table_name="fetch_job_waiters")
    op.drop_table("fetch_job_waiters")

"""fetch_failures: one diagnosis record per failed on-demand job (Task 1.6)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-03

The data-fetch agent diagnoses a failed on-demand job and records exactly one
row per ``(fetch_job_id, round)``; the unique constraint makes the diagnose task
idempotent. The row is the single place a non-fetched series is recorded for the
admin list (Task 5.3):

- ``outcome`` is ``recorded`` (diagnosed, no retry), ``retry_requested`` (a
  retry job was opened/attached) or ``agent_error`` (the agent itself failed),
- ``category`` is the diagnosis class the code validates before acting,
- ``alternatives`` is a JSONB list stored only and never auto-fetched,
- ``retry_job_id`` points at the job a retry created/attached; ``fetch_job_id``
  cascades on delete while ``retry_job_id`` is ``SET NULL`` so a retry job's
  deletion never destroys the diagnosis.

Implements ``downgrade`` (Task 1.6 asks that the migration round-trips).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORY_CHECK = (
    "category IN ('no_connector', 'not_in_source', 'bad_request', 'source_error', "
    "'transient', 'unknown')"
)
_OUTCOME_CHECK = "outcome IN ('retry_requested', 'recorded', 'agent_error')"


def upgrade() -> None:
    """Create ``fetch_failures`` and its indexes."""
    op.create_table(
        "fetch_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fetch_job_id", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("dataset_code", sa.Text(), nullable=True),
        sa.Column(
            "codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_reason", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column(
            "alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("retry_job_id", sa.Integer(), nullable=True),
        sa.Column("prompt_key", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_checksum", sa.Text(), nullable=True),
        sa.Column("llm_provider", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column(
            "llm_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("agent_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["fetch_job_id"],
            ["fetch_jobs.id"],
            name="fk_fetch_failures_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retry_job_id"],
            ["fetch_jobs.id"],
            name="fk_fetch_failures_retry_job",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name="fk_fetch_failures_institution",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name="fk_fetch_failures_series",
        ),
        sa.UniqueConstraint("fetch_job_id", "round", name="uq_fetch_failures_job_round"),
        sa.CheckConstraint(_CATEGORY_CHECK, name="ck_fetch_failures_category"),
        sa.CheckConstraint(_OUTCOME_CHECK, name="ck_fetch_failures_outcome"),
    )
    op.create_index("ix_fetch_failures_fetch_job", "fetch_failures", ["fetch_job_id"])


def downgrade() -> None:
    """Drop ``fetch_failures`` and its index."""
    op.drop_index("ix_fetch_failures_fetch_job", table_name="fetch_failures")
    op.drop_table("fetch_failures")

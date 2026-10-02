"""data model: institutions, series, observations, fetch_jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FREQUENCY_CHECK = (
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual')"
)
_FETCH_JOB_STATUS_CHECK = "status IN ('requested', 'fetching', 'completed', 'failed')"
_ACTIVE_JOB_PREDICATE = "status IN ('requested', 'fetching')"


def upgrade() -> None:
    """Create the source-agnostic data model, its latest view and immutability."""
    op.create_table(
        "institutions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("code", name="uq_institutions_code"),
    )

    op.create_table(
        "series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_category", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("frequency", sa.Text(), nullable=False),
        sa.Column(
            "breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("is_core", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"], ["institutions.id"], name="fk_series_institution"
        ),
        sa.UniqueConstraint(
            "institution_id", "external_code", name="uq_series_institution_external_code"
        ),
        sa.CheckConstraint(_FREQUENCY_CHECK, name="ck_series_frequency"),
    )

    op.create_table(
        "fetch_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name="fk_fetch_jobs_series"),
        sa.ForeignKeyConstraint(
            ["institution_id"], ["institutions.id"], name="fk_fetch_jobs_institution"
        ),
        sa.CheckConstraint(_FETCH_JOB_STATUS_CHECK, name="ck_fetch_jobs_status"),
    )
    op.create_index(
        "uq_fetch_jobs_active",
        "fetch_jobs",
        ["institution_id", "external_code"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_JOB_PREDICATE),
    )

    op.create_table(
        "observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        # No default: the writer must always supply fetched_at.
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_object_key", sa.Text(), nullable=True),
        sa.Column("fetch_job_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name="fk_observations_series"),
        sa.ForeignKeyConstraint(
            ["fetch_job_id"], ["fetch_jobs.id"], name="fk_observations_fetch_job"
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_observations_series_period_fetched
        ON observations (series_id, period, fetched_at DESC);
        """
    )

    op.execute(
        """
        CREATE VIEW latest_observations AS
        SELECT DISTINCT ON (series_id, period)
            id,
            series_id,
            period,
            value,
            fetched_at,
            source_updated_at,
            raw_object_key,
            fetch_job_id,
            created_at
        FROM observations
        ORDER BY series_id, period, fetched_at DESC, id DESC;
        """
    )

    op.execute(
        """
        CREATE FUNCTION observations_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'observations is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER observations_no_update_delete
        BEFORE UPDATE OR DELETE ON observations
        FOR EACH ROW EXECUTE FUNCTION observations_immutable();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

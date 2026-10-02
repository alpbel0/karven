"""catalog as datasets + dimension codelists; series on first use

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30

The catalog no longer enumerates every series. Each source dataset keeps its
dimensions and each dimension's full code list; a ``series`` row is created
only when a breakdown is first fetched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSION_ROLE_CHECK = "role IN ('time', 'geo', 'frequency', 'other')"


def upgrade() -> None:
    """Create datasets/dimensions/codes and link series to a dataset."""
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_category", sa.Text(), nullable=True),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("obs_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "source_incomplete", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("source_incomplete_note", sa.Text(), nullable=True),
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
            ["institution_id"], ["institutions.id"], name="fk_datasets_institution"
        ),
        sa.UniqueConstraint(
            "institution_id", "external_code", name="uq_datasets_institution_external_code"
        ),
    )

    op.create_table(
        "dataset_dimensions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasets.id"], name="fk_dataset_dimensions_dataset"
        ),
        sa.UniqueConstraint("dataset_id", "code", name="uq_dataset_dimensions_dataset_code"),
        sa.CheckConstraint(_DIMENSION_ROLE_CHECK, name="ck_dataset_dimensions_role"),
    )

    op.create_table(
        "dimension_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dimension_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("parent_code", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["dimension_id"], ["dataset_dimensions.id"], name="fk_dimension_codes_dimension"
        ),
        sa.UniqueConstraint("dimension_id", "code", name="uq_dimension_codes_dimension_code"),
    )

    # The catalog is populated from scratch after this migration; the series
    # table is empty, so the NOT NULL columns apply without a backfill.
    op.add_column("series", sa.Column("dataset_id", sa.Integer(), nullable=False))
    op.add_column(
        "series",
        sa.Column(
            "dimension_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key("fk_series_dataset", "series", "datasets", ["dataset_id"], ["id"])
    op.create_index("ix_series_dataset_id", "series", ["dataset_id"])


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

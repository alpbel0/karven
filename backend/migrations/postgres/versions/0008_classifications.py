"""classifications: source-independent classification versions and correspondences

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30

The TÜİK classification server (``siniflama.tuik.gov.tr``) publishes every
classification version as a code tree plus correspondence tables between
versions. The tables here are source-independent (``source`` names the origin)
so other sources can add classifications later.

Never delete: a code that disappears is kept with ``attributes['removed_at']``.
A databrowser2 dimension whose codes match a version exactly links through
``dimension_classification_links``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_JSONB_DEFAULT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Create the classification tables, their constraints and indexes."""
    op.create_table(
        "classifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("type_code", sa.Text(), nullable=False),
        sa.Column("short_name", sa.Text(), nullable=True),
        sa.Column("short_name_en", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("owner_en", sa.Text(), nullable=True),
        sa.Column("attributes", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_classifications_source_external_id"),
    )

    op.create_table(
        "classification_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("classification_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("parent_code", sa.Text(), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("label_en", sa.Text(), nullable=True),
        sa.Column("attributes", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name="fk_classification_items_classification",
        ),
        sa.UniqueConstraint(
            "classification_id", "code", name="uq_classification_items_classification_code"
        ),
    )
    op.create_index(
        "ix_classification_items_parent",
        "classification_items",
        ["classification_id", "parent_code"],
    )

    op.create_table(
        "classification_correspondences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("from_classification_id", sa.Integer(), nullable=True),
        sa.Column("to_classification_id", sa.Integer(), nullable=True),
        sa.Column("attributes", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["from_classification_id"],
            ["classifications.id"],
            name="fk_correspondences_from_classification",
        ),
        sa.ForeignKeyConstraint(
            ["to_classification_id"],
            ["classifications.id"],
            name="fk_correspondences_to_classification",
        ),
        sa.UniqueConstraint(
            "source",
            "external_id",
            name="uq_classification_correspondences_source_external_id",
        ),
    )

    op.create_table(
        "classification_correspondence_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("correspondence_id", sa.Integer(), nullable=False),
        sa.Column("from_code", sa.Text(), nullable=False),
        sa.Column("to_code", sa.Text(), nullable=False),
        sa.Column("from_label", sa.Text(), nullable=True),
        sa.Column("to_label", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["correspondence_id"],
            ["classification_correspondences.id"],
            name="fk_correspondence_items_correspondence",
        ),
        sa.UniqueConstraint(
            "correspondence_id",
            "from_code",
            "to_code",
            name="uq_correspondence_items_correspondence_codes",
        ),
    )

    op.create_table(
        "dimension_classification_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dimension_id", sa.Integer(), nullable=False),
        sa.Column("classification_id", sa.Integer(), nullable=False),
        sa.Column("matched_codes", sa.Integer(), nullable=False),
        sa.Column("total_codes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["dimension_id"],
            ["dataset_dimensions.id"],
            name="fk_dimension_classification_links_dimension",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name="fk_dimension_classification_links_classification",
        ),
        sa.UniqueConstraint(
            "dimension_id",
            "classification_id",
            name="uq_dimension_classification_links_dimension_classification",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

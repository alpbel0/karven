"""catalog_links: reviewer-approved series mappings (Turcat -> databrowser2)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30

Stores proposed/accepted mappings between one breakdown of a source dataset and
one breakdown of a target dataset. Rows are not written by hand: Jev proposes
them (``method='jev_proposed'``) and a reviewer accepts or rejects. The same
pair is never proposed twice (unique constraint), so the CLI is idempotent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RELATION_CHECK = "relation IN ('same_series', 'related')"
_METHOD_CHECK = "method IN ('jev_proposed', 'manual')"
_STATUS_CHECK = "status IN ('proposed', 'accepted', 'rejected')"


def upgrade() -> None:
    """Create catalog_links with its checks and the unique pair constraint."""
    op.create_table(
        "catalog_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_dataset_id", sa.Integer(), nullable=False),
        sa.Column(
            "from_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("to_dataset_id", sa.Integer(), nullable=False),
        sa.Column(
            "to_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["from_dataset_id"], ["datasets.id"], name="fk_catalog_links_from_dataset"
        ),
        sa.ForeignKeyConstraint(
            ["to_dataset_id"], ["datasets.id"], name="fk_catalog_links_to_dataset"
        ),
        sa.UniqueConstraint(
            "from_dataset_id",
            "from_codes",
            "to_dataset_id",
            "to_codes",
            name="uq_catalog_links_pair",
        ),
        sa.CheckConstraint(_RELATION_CHECK, name="ck_catalog_links_relation"),
        sa.CheckConstraint(_METHOD_CHECK, name="ck_catalog_links_method"),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_catalog_links_status"),
    )
    op.create_index("ix_catalog_links_from_dataset_id", "catalog_links", ["from_dataset_id"])
    op.create_index("ix_catalog_links_to_dataset_id", "catalog_links", ["to_dataset_id"])
    op.create_index("ix_catalog_links_status", "catalog_links", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

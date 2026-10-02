"""documents.content_text + document_dataset_links

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02

The TÜİK press-release channel (``veriportali.tuik.gov.tr``) stores bulletin
catalogue rows in the existing ``documents`` table (``source='tuik_press'``) and
fetches each release's body on demand. This migration adds:

- ``documents.content_text``: the plain text of an on-demand fetched release
  (NULL until the detail is fetched; the catalogue walk never fills it), and
- ``document_dataset_links``: the datasets a release's statistical tables point
  at, resolved by code (``dataset_id`` stays NULL while the dataset is not
  catalogued and is re-resolved on the next fetch).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_JSONB_DEFAULT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Add ``documents.content_text`` and create ``document_dataset_links``."""
    op.add_column("documents", sa.Column("content_text", sa.Text(), nullable=True))
    op.create_table(
        "document_dataset_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("dataset_code", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=True),
        sa.Column("dataset_id", sa.Integer(), nullable=True),
        sa.Column(
            "relation",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'statistical_table'"),
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_dataset_links_document",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_document_dataset_links_dataset",
        ),
        sa.UniqueConstraint("document_id", "dataset_code", name="uq_document_dataset_links_pair"),
    )
    op.create_index(
        "ix_document_dataset_links_dataset_id", "document_dataset_links", ["dataset_id"]
    )
    op.create_index(
        "ix_document_dataset_links_dataset_code", "document_dataset_links", ["dataset_code"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

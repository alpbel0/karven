"""documents: source-independent publication catalogue rows

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01

The TÜİK Biruni publication system (``biruni.tuik.gov.tr/yayin``) publishes a
catalogue of publications, reports and micro-data sets. The table here is
source-independent (``source`` names the origin, ``tuik_yayin`` today) so TÜİK
bulletins and TCMB reports can reuse it later. A row is catalogue metadata plus
a link; the file itself is not downloaded.

Never delete: a document that disappears is kept with
``attributes['removed_at']`` and a reappearing row clears the marker.
``published_at`` is only set when the source gives a real date (never invented).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_JSONB_DEFAULT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    """Create the documents table, its constraints and indexes."""
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("institution_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False, server_default=sa.text("'tr'")),
        sa.Column("attributes", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name="fk_documents_institution",
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_documents_source_external_id"),
    )
    op.create_index("ix_documents_source_doc_type", "documents", ["source", "doc_type"])
    op.create_index("ix_documents_year", "documents", ["year"])


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

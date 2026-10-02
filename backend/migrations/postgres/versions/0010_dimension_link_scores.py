"""dimension_classification_links: store coverage, label agreement and attributes

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30

The first linking rule matched a dimension to a version only when every
dimension code appeared in it. That over-linked on small numeric lists and
missed the classifications that matter (COICOP/NACE/CPA write codes
differently). A link is now scored: ``coverage`` (share of the dimension's
meaningful codes found in the version) and ``label_agreement`` (share of the
found codes whose label matches the version's) sit next to the counts, and
``attributes`` records how the codes were normalized. The table is derived and
recomputed on every run, so existing rows backfill the two scores to 0.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Add the score columns and the attributes map to the link table."""
    op.add_column(
        "dimension_classification_links",
        sa.Column("coverage", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "dimension_classification_links",
        sa.Column("label_agreement", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "dimension_classification_links",
        sa.Column(
            "attributes",
            _JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

"""dataset ``veri_yok`` flag (Task 2.4b)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05

Schema only; the rule is derived from the source attributes by the enrichment
pass (``app.catalog.enrich``). ``veri_yok`` marks a Veri Portalı report dataset
whose data cannot be downloaded (no dimensions, portal ``downloadable = false``);
the search candidate query excludes it.

``downgrade`` drops the column (the migration round-trips).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the boolean flag with a false default (existing rows stay searchable)."""
    op.add_column(
        "datasets",
        sa.Column("veri_yok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    """Drop the flag column."""
    op.drop_column("datasets", "veri_yok")

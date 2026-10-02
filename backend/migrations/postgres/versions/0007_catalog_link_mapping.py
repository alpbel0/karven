"""catalog_links.mapping: structured transformation rules for a link

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

A link can carry a machine-readable mapping that transforms the source breakdown
and period into the target one: ``region`` (through ``region_crosswalk``),
``period`` (``same`` / ``month_of_year`` / ``month_dimension``) and ``scale``.
The JSON is validated in ``app.catalog.mapping``; an empty object means "no
transformation beyond the recorded ``to_codes``".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add catalog_links.mapping (JSONB, default empty object)."""
    op.add_column(
        "catalog_links",
        sa.Column(
            "mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

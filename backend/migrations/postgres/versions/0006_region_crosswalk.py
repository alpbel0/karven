"""region_crosswalk: source-independent region code mappings

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30

One row maps a code in one scheme (e.g. CİP province plate ``6``) to a code in
another scheme (e.g. İBBS level-3 ``TR510``). The table is not tied to any
dataset: other sources add their own schemes later and Phase 3 maps read it.
Only provinces are populated today; ``level`` is checked so extending it is a
one-line change here and in ``app.data.models``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METHOD_CHECK = "method IN ('label_match', 'manual')"
# Only provinces for now; add a value (and update REGION_LEVELS in models) to
# extend the check.
REGION_LEVELS: tuple[str, ...] = ("province",)
_LEVEL_CHECK = "level IN (" + ", ".join(f"'{level}'" for level in REGION_LEVELS) + ")"


def upgrade() -> None:
    """Create region_crosswalk with its checks and unique mapping constraint."""
    op.create_table(
        "region_crosswalk",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_scheme", sa.Text(), nullable=False),
        sa.Column("from_code", sa.Text(), nullable=False),
        sa.Column("to_scheme", sa.Text(), nullable=False),
        sa.Column("to_code", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "from_scheme",
            "from_code",
            "to_scheme",
            name="uq_region_crosswalk_from",
        ),
        sa.CheckConstraint(_METHOD_CHECK, name="ck_region_crosswalk_method"),
        sa.CheckConstraint(_LEVEL_CHECK, name="ck_region_crosswalk_level"),
    )
    op.create_index("ix_region_crosswalk_to", "region_crosswalk", ["to_scheme", "to_code"])


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

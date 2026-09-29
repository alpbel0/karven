"""initial empty schema

Revision ID: 0001
Revises:
Create Date: 2026-09-29

"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

"""series: allow the ``irregular`` frequency

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02

TÜİK election results (``secimdagitimapp``) have no fixed cadence: each
observation is a real election date. The ``series`` table's frequency check only
allowed the regular cadences, so the new ``irregular`` value is added here (and
to :data:`app.data.periods.FREQUENCIES`) to let those series be stored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FREQUENCY_CHECK = (
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual', 'irregular')"
)


def upgrade() -> None:
    """Widen the series frequency check constraint with ``irregular``."""
    op.drop_constraint("ck_series_frequency", "series", type_="check")
    op.create_check_constraint("ck_series_frequency", "series", _FREQUENCY_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

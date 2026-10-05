"""series: allow the ``biennial`` frequency

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-05

TÜİK publishes the waste and waste-water surveys every second year (SDMX ``A2``,
"Biennial"). They were first stored as ``annual`` by the first-letter rule, which
hides that every other year is missing. The ``series`` frequency check now also
allows ``biennial`` (and :data:`app.data.periods.FREQUENCIES` lists it).

``downgrade`` restores the previous check, and refuses to run while a ``biennial``
series exists (the old check would reject the row; converting it silently to
``annual`` would be wrong).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FREQUENCY_CHECK = (
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual', "
    "'biennial', 'irregular')"
)
_PREVIOUS_CHECK = (
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual', 'irregular')"
)


def upgrade() -> None:
    """Widen the series frequency check constraint with ``biennial``."""
    op.drop_constraint("ck_series_frequency", "series", type_="check")
    op.create_check_constraint("ck_series_frequency", "series", _FREQUENCY_CHECK)


def downgrade() -> None:
    """Restore the previous check; refuse while biennial series exist."""
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT count(*) FROM series WHERE frequency = 'biennial'")
    ).scalar()
    if count:
        raise RuntimeError(
            f"cannot downgrade 0020: {count} series have frequency 'biennial' "
            "(re-label or delete them first)"
        )
    op.drop_constraint("ck_series_frequency", "series", type_="check")
    op.create_check_constraint("ck_series_frequency", "series", _PREVIOUS_CHECK)

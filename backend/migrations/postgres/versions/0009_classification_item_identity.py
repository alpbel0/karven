"""classification_items: identify an item by (code, parent_code, label)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30

The first load revealed the source repeats a ``code`` inside one version:

- ``238`` LOCARNO lists many terms under one code (``01.01``/``SINIF01`` ->
  "UNLU MAMULLER", "BİSKÜVİLER", "EKMEK", ...).
- ``1283``/``1285`` EKONOMİK GRUPLAR put one country under several parents.
- ``1530``, ``1691``, ``1707`` repeat a code under two parents.

``(classification_id, code)`` therefore lost 6,582 rows on the live load. The
identity becomes ``(code, parent_code, label)``; a ``NULL`` ``parent_code``
must still collide with itself, hence ``NULLS NOT DISTINCT`` (PostgreSQL 15+).
The old unique constraint is dropped and an index on
``(classification_id, code)`` keeps code lookups covered. Existing rows stay
valid: their triple is unique.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the code-only unique constraint with the triple identity."""
    op.create_index(
        "ix_classification_items_classification_code",
        "classification_items",
        ["classification_id", "code"],
    )
    op.drop_constraint(
        "uq_classification_items_classification_code",
        "classification_items",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_classification_items_identity",
        "classification_items",
        ["classification_id", "code", "parent_code", "label"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

"""release_calendar + data_alerts: calendar-driven core refresh and its records

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-03

The core list is refreshed by polling the sources' release calendars (Task 1.4c).
This migration adds two tables:

- ``release_calendar``: one published release date per (source, key, date). Rows
  are upserted by that triple on every calendar sync; ``attributes`` keeps the
  raw item and ``raw_object_key`` points at the stored payload.
- ``data_alerts``: one open alert per (institution, scope, kind) — a partial
  unique index enforces that ``status='open'`` rows cannot duplicate while
  letting resolved history accumulate. ``scope`` is a series external code, or a
  source key for source-level alerts.

The ``latest_observations`` view and the ``observations`` immutability trigger
are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_JSONB_DEFAULT = sa.text("'{}'::jsonb")

_ALERT_KIND_CHECK = "kind IN ('no_new_period', 'format_changed', 'repeated_failure')"
_ALERT_STATUS_CHECK = "status IN ('open', 'resolved')"


def upgrade() -> None:
    """Create ``release_calendar`` and ``data_alerts``."""
    op.create_table(
        "release_calendar",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("period_label", sa.Text(), nullable=True),
        sa.Column("expected_on", sa.Date(), nullable=False),
        sa.Column("expected_at", sa.Text(), nullable=True),
        sa.Column("attributes", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=True),
        sa.UniqueConstraint("source", "key", "expected_on", name="uq_release_calendar_identity"),
    )
    op.create_index(
        "ix_release_calendar_source_key_expected",
        "release_calendar",
        ["source", "key", "expected_on"],
    )

    op.create_table(
        "data_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", _JSONB, nullable=False, server_default=_JSONB_DEFAULT),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["institution_id"], ["institutions.id"], name="fk_data_alerts_institution"
        ),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name="fk_data_alerts_series"),
        sa.CheckConstraint(_ALERT_KIND_CHECK, name="ck_data_alerts_kind"),
        sa.CheckConstraint(_ALERT_STATUS_CHECK, name="ck_data_alerts_status"),
    )
    op.create_index(
        "uq_data_alerts_open_scope_kind",
        "data_alerts",
        ["institution_id", "scope", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_data_alerts_status", "data_alerts", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")

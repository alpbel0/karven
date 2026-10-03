"""Data-alert records for the core refresh (Task 1.4c; panel is Task 5.5).

An alert is a RECORD only: there is no notification channel yet. One open row is
kept per ``(institution_id, scope, kind)`` (partial unique index in migration
0014). ``open_alert`` is idempotent: re-opening a live alert refreshes its
``last_seen_at``/``message``/``detail`` instead of inserting a second row, and
``resolve_alerts`` flips the open row to ``resolved`` rather than deleting it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data.models import DataAlert

NO_NEW_PERIOD = "no_new_period"
FORMAT_CHANGED = "format_changed"
REPEATED_FAILURE = "repeated_failure"

KINDS = (NO_NEW_PERIOD, FORMAT_CHANGED, REPEATED_FAILURE)
OPEN = "open"
RESOLVED = "resolved"


def open_alert(
    session: Session,
    *,
    institution_id: int,
    scope: str,
    kind: str,
    message: str,
    detail: dict[str, Any] | None = None,
    series_id: int | None = None,
    now: datetime | None = None,
) -> DataAlert:
    """Open (or refresh) the single live alert for ``scope``/``kind``.

    Returns the open row. A re-raise updates ``last_seen_at``, ``message`` and
    ``detail`` in place; it never inserts a duplicate.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown alert kind {kind!r}")
    moment = now or datetime.now(UTC)
    payload = detail or {}
    alert = session.scalar(
        sa.select(DataAlert).where(
            DataAlert.institution_id == institution_id,
            DataAlert.scope == scope,
            DataAlert.kind == kind,
            DataAlert.status == OPEN,
        )
    )
    if alert is None:
        alert = DataAlert(
            institution_id=institution_id,
            series_id=series_id,
            scope=scope,
            kind=kind,
            status=OPEN,
            message=message,
            detail=payload,
            opened_at=moment,
            last_seen_at=moment,
        )
        session.add(alert)
    else:
        alert.message = message
        alert.detail = payload
        alert.last_seen_at = moment
        if series_id is not None:
            alert.series_id = series_id
    session.flush()
    return alert


def resolve_alerts(
    session: Session,
    *,
    institution_id: int,
    scope: str,
    kinds: Iterable[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Resolve the open alert(s) for ``scope``; return how many were closed."""
    moment = now or datetime.now(UTC)
    statement = sa.select(DataAlert).where(
        DataAlert.institution_id == institution_id,
        DataAlert.scope == scope,
        DataAlert.status == OPEN,
    )
    if kinds is not None:
        wanted = list(kinds)
        statement = statement.where(DataAlert.kind.in_(wanted))
    resolved = 0
    for alert in session.scalars(statement).all():
        alert.status = RESOLVED
        alert.resolved_at = moment
        alert.last_seen_at = moment
        resolved += 1
    session.flush()
    return resolved


def list_alerts(
    session: Session,
    *,
    statuses: Iterable[str] | None = None,
    scope: str | None = None,
) -> list[DataAlert]:
    """Return alerts, newest first; open-only unless ``statuses`` says otherwise."""
    statement = sa.select(DataAlert)
    if statuses is not None:
        statement = statement.where(DataAlert.status.in_(list(statuses)))
    if scope is not None:
        statement = statement.where(DataAlert.scope == scope)
    statement = statement.order_by(DataAlert.opened_at.desc(), DataAlert.id.desc())
    return list(session.scalars(statement).all())


__all__ = [
    "FORMAT_CHANGED",
    "KINDS",
    "NO_NEW_PERIOD",
    "OPEN",
    "REPEATED_FAILURE",
    "RESOLVED",
    "list_alerts",
    "open_alert",
    "resolve_alerts",
]

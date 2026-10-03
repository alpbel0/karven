"""Shared constructions for the core CLI and scheduler (Task 1.4d).

The core CLI (:mod:`app.core.__main__`) and the scheduler both build the same
objects: an EVDS3 connector per source, and the two release-calendar clients.
Keeping the constructors here means the scheduler can reuse exactly what the CLI
does without duplicating (or drifting from) it. A ``store`` of ``None`` means a
dry run: no MinIO, nothing written.
"""

from __future__ import annotations

from app.config import Settings
from app.connectors.base import ObjectStore
from app.connectors.tcmb.client import EvdsClient
from app.connectors.tcmb.connector import SOURCES, TcmbConnector
from app.core.calendar import TuikCalendarClient


def build_connector(
    source: str,
    *,
    store: ObjectStore | None,
    settings_obj: Settings | None = None,
) -> TcmbConnector:
    """The EVDS3 connector for one source (``tcmb``, ``hmb`` or ``tuik-evds``)."""
    client = EvdsClient(
        store=store,
        institution=SOURCES[source].institution_code,
        settings_obj=settings_obj,
    )
    return TcmbConnector(client=client, source=source, settings_obj=settings_obj)


def build_evds_calendar_client(
    *,
    store: ObjectStore | None,
    settings_obj: Settings | None = None,
) -> EvdsClient:
    """The EVDS3 client used by ``sync_calendar`` (institution ``tcmb``)."""
    return EvdsClient(store=store, institution="tcmb", settings_obj=settings_obj)


def build_tuik_calendar_client(
    *,
    store: ObjectStore | None,
    settings_obj: Settings | None = None,
) -> TuikCalendarClient:
    """The TÜİK yearly-bulletin calendar client used by ``sync_calendar``."""
    return TuikCalendarClient(store=store, settings_obj=settings_obj)


__all__ = [
    "build_connector",
    "build_evds_calendar_client",
    "build_tuik_calendar_client",
]

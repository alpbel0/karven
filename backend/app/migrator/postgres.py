"""Run PostgreSQL migrations programmatically with Alembic."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def upgrade_postgres() -> None:
    """Upgrade the PostgreSQL schema to head (idempotent)."""
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")

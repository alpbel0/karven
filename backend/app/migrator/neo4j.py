"""Minimal forward-only Cypher migration runner.

Files live in ``migrations/neo4j`` and are named ``<version>_<name>.cypher``
(e.g. ``0001_constraints.cypher``), applied in filename order. Every statement
must be idempotent (``IF NOT EXISTS``). Applied files are recorded as
``(:Migration {version, name, checksum, applied_at})``; if an already-applied
file's checksum changed the run fails loudly instead of re-applying.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "neo4j"

APPLIED_QUERY = "MATCH (m:Migration) RETURN m.version AS version, m.checksum AS checksum"
RECORD_QUERY = (
    "CREATE (m:Migration {"
    "version: $version, name: $name, checksum: $checksum, applied_at: datetime()"
    "})"
)


class MigrationError(RuntimeError):
    """Raised for invalid migration files or setup."""


class ChecksumMismatchError(MigrationError):
    """Raised when an applied migration file's content changed."""


@dataclass(frozen=True)
class MigrationFile:
    version: str
    name: str
    path: Path
    checksum: str
    statements: tuple[str, ...]

    @property
    def filename(self) -> str:
        return self.path.name


def parse_filename(filename: str) -> tuple[str, str]:
    version, _, name = Path(filename).stem.partition("_")
    if not version or not name:
        raise MigrationError(
            f"invalid migration filename {filename!r}; expected <version>_<name>.cypher"
        )
    return version, name


def split_statements(text: str) -> list[str]:
    """Split a .cypher file into statements on semicolons.

    ``//`` line comments are removed. Statements must not contain semicolons
    inside string literals (our migration files never do).
    """
    without_comments = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    return [part.strip() for part in without_comments.split(";") if part.strip()]


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationFile]:
    """Load and order all .cypher migration files in ``directory``."""
    files: list[MigrationFile] = []
    for path in sorted(directory.glob("*.cypher")):
        version, name = parse_filename(path.name)
        raw = path.read_bytes()
        files.append(
            MigrationFile(
                version=version,
                name=name,
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                statements=tuple(split_statements(raw.decode("utf-8"))),
            )
        )
    versions = [migration.version for migration in files]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions in {directory}")
    return files


def _fetch_applied(session: Any) -> dict[str, str]:
    applied: dict[str, str] = {}
    for record in session.run(APPLIED_QUERY):
        applied[record["version"]] = record["checksum"]
    return applied


def apply_migrations(session: Any, files: Iterable[MigrationFile]) -> list[str]:
    """Apply pending files through ``session`` and return their versions."""
    migrations = list(files)
    applied = _fetch_applied(session)
    newly_applied: list[str] = []
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise ChecksumMismatchError(
                    f"{migration.filename} was already applied with checksum "
                    f"{recorded}, but the file now has {migration.checksum}"
                )
            logger.info("neo4j: %s already applied, skipping", migration.filename)
            continue
        for statement in migration.statements:
            session.run(statement)
        session.run(
            RECORD_QUERY,
            version=migration.version,
            name=migration.name,
            checksum=migration.checksum,
        )
        newly_applied.append(migration.version)
        logger.info("neo4j: applied %s", migration.filename)
    if not newly_applied:
        logger.info("neo4j: nothing to do")
    return newly_applied


def run_migrations(
    directory: Path = MIGRATIONS_DIR,
    *,
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> list[str]:
    """Connect to Neo4j and apply pending migrations. Returns new versions."""
    from neo4j import GraphDatabase

    from app.config import settings

    files = discover_migrations(directory)
    if not files:
        logger.info("neo4j: no migration files found")
        return []

    resolved_uri = uri or settings.neo4j_uri
    resolved_user = user or settings.neo4j_user
    resolved_password = password
    if resolved_password is None and settings.neo4j_password is not None:
        resolved_password = settings.neo4j_password.get_secret_value()

    if not resolved_uri or not resolved_user or resolved_password is None:
        raise MigrationError(
            "neo4j connection settings are incomplete (need NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)"
        )

    driver = GraphDatabase.driver(resolved_uri, auth=(resolved_user, resolved_password))
    try:
        with driver.session() as session:
            return apply_migrations(session, files)
    finally:
        driver.close()

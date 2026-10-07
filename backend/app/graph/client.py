"""Neo4j driver factory and session context manager for the graph layer.

The graph is optional (``neo4j_enabled``): when it is off, or when the URI/user/
password settings are incomplete, a clear :class:`GraphError` is raised instead
of letting the driver fail with an opaque connection error.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import settings as default_settings
from app.graph.errors import GraphError


def _resolve(settings: Any) -> tuple[str, str, str]:
    if not getattr(settings, "neo4j_enabled", False):
        raise GraphError(
            "Neo4j is disabled; set NEO4J_ENABLED=true (with the `graph` compose "
            "profile) before using the graph layer"
        )
    uri = getattr(settings, "neo4j_uri", None)
    user = getattr(settings, "neo4j_user", None)
    password_secret = getattr(settings, "neo4j_password", None)
    password = password_secret.get_secret_value() if password_secret is not None else None
    missing = [
        name
        for name, value in (
            ("NEO4J_URI", uri),
            ("NEO4J_USER", user),
            ("NEO4J_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise GraphError(f"neo4j connection settings are incomplete (missing {', '.join(missing)})")
    return uri, user, password


def create_driver(settings: Any | None = None) -> Any:
    """Build a sync ``neo4j`` driver from settings; raise GraphError if unusable."""
    from neo4j import GraphDatabase

    uri, user, password = _resolve(settings if settings is not None else default_settings)
    return GraphDatabase.driver(uri, auth=(user, password))


@contextmanager
def graph_client(settings: Any | None = None) -> Iterator[Any]:
    """Yield a session from a fresh driver and always close the driver."""
    driver = create_driver(settings)
    try:
        with driver.session() as session:
            yield session
    finally:
        driver.close()

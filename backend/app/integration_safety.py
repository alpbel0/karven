"""Safety guard for integration tests.

Integration tests must only ever run against the isolated `karven-test`
stack, never against live services. This module holds the pure validation used
by the integration test session fixture (and unit-tested on its own).
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

TEST_APP_ENV = "test"
TEST_POSTGRES_DB = "karven_test"
TEST_PORTS = frozenset({25432, 27687, 26379, 29000})


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _url_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return urlsplit(value).port
    except ValueError:
        return None


def validate_integration_env(env: Mapping[str, str]) -> list[str]:
    """Return a list of problems; empty means it is safe to run integration tests."""
    problems: list[str] = []

    if env.get("APP_ENV") != TEST_APP_ENV:
        problems.append(f"APP_ENV must be {TEST_APP_ENV!r} (got {env.get('APP_ENV')!r})")

    if env.get("POSTGRES_DB") != TEST_POSTGRES_DB:
        problems.append(
            f"POSTGRES_DB must be {TEST_POSTGRES_DB!r} (got {env.get('POSTGRES_DB')!r})"
        )

    checks = {
        "POSTGRES_PORT": _as_int(env.get("POSTGRES_PORT")),
        "Neo4j bolt port (NEO4J_URI)": _url_port(env.get("NEO4J_URI")),
        "Redis port (REDIS_URL)": _url_port(env.get("REDIS_URL")),
        "MinIO port (MINIO_ENDPOINT)": _url_port(env.get("MINIO_ENDPOINT")),
    }
    allowed = sorted(TEST_PORTS)
    for label, port in checks.items():
        if port not in TEST_PORTS:
            problems.append(f"{label} must be one of {allowed} (got {port})")

    return problems

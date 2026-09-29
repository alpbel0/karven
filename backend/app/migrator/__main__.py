"""Entry point: ``python -m app.migrator`` runs postgres -> neo4j -> minio.

Idempotent: a second run changes nothing and exits 0. Exits non-zero on failure.
"""

from __future__ import annotations

import logging

from pydantic import SecretStr

from app.config import settings
from app.migrator.minio import create_client, ensure_bucket
from app.migrator.neo4j import run_migrations as run_neo4j_migrations
from app.migrator.postgres import upgrade_postgres

logger = logging.getLogger("app.migrator")


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Alembic's env.py reconfigures the root logger; keep our lines single.
    logger.propagate = False


def _required(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"missing required setting: {name}")
    return value


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"missing required setting: {name}")
    return value.get_secret_value()


def main() -> int:
    _configure_logging()
    try:
        logger.info("postgres: applying migrations")
        upgrade_postgres()
        logger.info("postgres: up to date")

        logger.info("neo4j: applying migrations")
        run_neo4j_migrations()
        logger.info("neo4j: up to date")

        bucket = _required(settings.minio_bucket_raw, "MINIO_BUCKET_RAW")
        logger.info("minio: ensuring bucket %s", bucket)
        client = create_client(
            _required(settings.minio_endpoint, "MINIO_ENDPOINT"),
            _required(settings.minio_access_key, "MINIO_ACCESS_KEY"),
            _required_secret(settings.minio_secret_key, "MINIO_SECRET_KEY"),
        )
        try:
            ensure_bucket(client, bucket)
        finally:
            client.close()
    except Exception:
        logger.exception("migrator failed")
        return 1
    logger.info("migrator: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

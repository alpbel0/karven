"""Integration tests against the isolated `karven-test` stack.

Run them with `uv run python scripts/integration.py` (which sets up the stack
and the required environment). A session fixture refuses to run otherwise.
"""

import os
import urllib.request

import boto3
import pytest
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

from app.config import settings

pytestmark = pytest.mark.integration


def test_alembic_version_is_at_head() -> None:
    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    finally:
        engine.dispose()

    assert version == "0007"


def test_neo4j_migration_node_and_constraint() -> None:
    password = settings.neo4j_password.get_secret_value() if settings.neo4j_password else ""
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, password))
    try:
        with driver.session() as session:
            versions = [
                record["version"]
                for record in session.run("MATCH (m:Migration) RETURN m.version AS version")
            ]
            constraint_names = [
                record["name"] for record in session.run("SHOW CONSTRAINTS YIELD name RETURN name")
            ]
    finally:
        driver.close()

    assert versions == ["0001"]
    assert "migration_version_unique" in constraint_names


def test_minio_bucket_exists() -> None:
    assert settings.minio_bucket_raw == "karven-test-raw"
    assert settings.minio_secret_key is not None
    client = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key.get_secret_value(),
        region_name="us-east-1",
    )
    try:
        client.head_bucket(Bucket=settings.minio_bucket_raw)
    finally:
        client.close()


def test_api_health_endpoint() -> None:
    port = os.environ.get("API_HOST_PORT", "28000")

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
        assert response.status == 200
        assert response.read() == b'{"status":"ok"}'

"""Integration tests for the versioned prompt store (isolated test stack)."""

from __future__ import annotations

import json
from uuid import uuid4

import boto3
import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.session import SessionLocal
from app.llm.chat import ChatClient
from app.llm.raw_log import build_raw_logger
from app.prompts import service
from app.prompts.errors import PromptNotFoundError
from app.prompts.models import PromptVersion
from app.prompts.ref import PromptRef
from tests.llm_helpers import FakeTransport, chat_response, make_settings

pytestmark = pytest.mark.integration


def _unique_key(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}.system"


def test_add_activate_and_supersede_keeps_old_version() -> None:
    key = _unique_key("lifecycle")
    body_v1 = "hello ${name}"
    with SessionLocal() as session:
        v1 = service.add_version(session, key=key, body=body_v1, note="first")
        assert v1.version == 1
        service.activate(session, key=key, version=1)
        session.commit()

        active = service.get_active(session, key)
        assert active.version == 1
        assert active.body == body_v1
        assert active.checksum == service.checksum_for(body_v1)

        v2 = service.add_version(session, key=key, body="second ${name}")
        assert v2.version == 2
        service.activate(session, key=key, version=2)
        session.commit()

        active = service.get_active(session, key)
        assert active.version == 2

        stored_v1 = session.scalar(
            select(PromptVersion).where(PromptVersion.key == key, PromptVersion.version == 1)
        )
        assert stored_v1 is not None
        assert stored_v1.body == body_v1


def test_rollback_reactivates_older_version() -> None:
    key = _unique_key("rollback")
    with SessionLocal() as session:
        service.add_version(session, key=key, body="v1 body")
        service.add_version(session, key=key, body="v2 body")
        service.activate(session, key=key, version=2)
        session.commit()
        assert service.get_active(session, key).version == 2

        service.activate(session, key=key, version=1)
        session.commit()
        active = service.get_active(session, key)
        assert active.version == 1
        assert active.body == "v1 body"


def test_update_and_delete_are_rejected_by_trigger() -> None:
    key = _unique_key("immutable")
    with SessionLocal() as session:
        version = service.add_version(session, key=key, body="immutable body")
        session.commit()
        version_id = version.id

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text("UPDATE prompt_versions SET note = 'x' WHERE id = :id"),
                    {"id": version_id},
                )
            connection.rollback()

            with pytest.raises(DBAPIError):
                connection.execute(
                    text("DELETE FROM prompt_versions WHERE id = :id"),
                    {"id": version_id},
                )
            connection.rollback()

            remaining = connection.execute(
                text("SELECT count(*) FROM prompt_versions WHERE id = :id"),
                {"id": version_id},
            ).scalar_one()
            assert remaining == 1
    finally:
        engine.dispose()


def test_activating_version_of_another_key_is_rejected() -> None:
    key_a = _unique_key("fka")
    key_b = _unique_key("fkb")
    empty_key = _unique_key("empty")
    with SessionLocal() as session:
        version_a = service.add_version(session, key=key_a, body="a")
        version_b = service.add_version(session, key=key_b, body="b")
        service.add_version(session, key=empty_key, body="c")
        session.commit()
        version_a_id = version_a.id
        version_b_id = version_b.id

    with pytest.raises(PromptNotFoundError):
        with SessionLocal() as session:
            service.activate(session, key=empty_key, version=999)

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "INSERT INTO active_prompts (key, prompt_version_id) "
                        "VALUES (:key, :version_id)"
                    ),
                    {"key": key_b, "version_id": version_a_id},
                )
            connection.rollback()

            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "INSERT INTO active_prompts (key, prompt_version_id) "
                        "VALUES (:key, :version_id)"
                    ),
                    {"key": key_a, "version_id": version_b_id},
                )
            connection.rollback()
    finally:
        engine.dispose()


def _list_keys(client, bucket: str) -> set[str]:
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="llm-calls/"):
        for entry in page.get("Contents", []):
            keys.add(entry["Key"])
    return keys


def test_raw_call_log_in_minio_contains_prompt_ref() -> None:
    bucket = settings.minio_bucket_raw
    assert bucket
    assert settings.minio_secret_key is not None
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key.get_secret_value(),
        region_name="us-east-1",
    )
    before = _list_keys(s3, bucket)
    ref = PromptRef(key=f"log_{uuid4().hex}.system", version=7, checksum="abc789")
    chat = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(FakeTransport(evren=[chat_response()])),
        raw_logger=build_raw_logger(settings),
    )
    try:
        chat.complete([{"role": "user", "content": "hi"}], prompt_ref=ref)

        created = _list_keys(s3, bucket) - before
        assert len(created) == 1
        raw = s3.get_object(Bucket=bucket, Key=next(iter(created)))["Body"].read()
        payload = json.loads(raw)
        assert payload["prompt"] == {
            "key": ref.key,
            "version": 7,
            "checksum": "abc789",
        }
        assert payload["request"]["headers"]["Authorization"] == "***"

        decoded = raw.decode("utf-8")
        assert "test-only-minio-secret" not in decoded
        for secret in (
            settings.evren_api_key,
            settings.openrouter_api_key,
            settings.typesafe_api_key,
        ):
            if secret is not None:
                assert secret.get_secret_value() not in decoded
    finally:
        for key in _list_keys(s3, bucket) - before:
            s3.delete_object(Bucket=bucket, Key=key)
        chat.close()
        s3.close()

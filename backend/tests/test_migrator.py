"""Unit tests for the one-shot migrator entry point (``python -m app.migrator``)."""

from __future__ import annotations

import logging

from pydantic import SecretStr

from app.migrator import __main__ as migrator


class _FakeMinioClient:
    def close(self) -> None:
        pass


def _stub_migrator(monkeypatch) -> dict[str, int]:
    """Replace postgres/minio work and count Neo4j runner calls."""
    calls = {"neo4j": 0}

    def fake_neo4j() -> list[str]:
        calls["neo4j"] += 1
        return []

    # Skip logging setup so caplog (attached to the root logger) sees the lines.
    monkeypatch.setattr(migrator, "_configure_logging", lambda: None)
    monkeypatch.setattr(migrator, "upgrade_postgres", lambda: None)
    monkeypatch.setattr(migrator, "run_neo4j_migrations", fake_neo4j)
    monkeypatch.setattr(migrator, "create_client", lambda *args, **kwargs: _FakeMinioClient())
    monkeypatch.setattr(migrator, "ensure_bucket", lambda *args, **kwargs: False)
    monkeypatch.setattr(migrator.settings, "minio_bucket_raw", "bucket")
    monkeypatch.setattr(migrator.settings, "minio_endpoint", "http://minio:9000")
    monkeypatch.setattr(migrator.settings, "minio_access_key", "key")
    monkeypatch.setattr(migrator.settings, "minio_secret_key", SecretStr("secret"))
    return calls


def test_migrator_skips_neo4j_when_disabled(monkeypatch, caplog) -> None:
    calls = _stub_migrator(monkeypatch)
    monkeypatch.setattr(migrator.settings, "neo4j_enabled", False)

    with caplog.at_level(logging.INFO, logger="app.migrator"):
        assert migrator.main() == 0

    assert calls["neo4j"] == 0
    assert "neo4j: disabled (NEO4J_ENABLED=false), graph migrations skipped" in caplog.text


def test_migrator_runs_neo4j_when_enabled(monkeypatch, caplog) -> None:
    calls = _stub_migrator(monkeypatch)
    monkeypatch.setattr(migrator.settings, "neo4j_enabled", True)

    with caplog.at_level(logging.INFO, logger="app.migrator"):
        assert migrator.main() == 0

    assert calls["neo4j"] == 1
    assert "neo4j: applying migrations" in caplog.text

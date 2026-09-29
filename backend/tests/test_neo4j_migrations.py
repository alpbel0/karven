import hashlib
from pathlib import Path

import pytest

from app.migrator.neo4j import (
    ChecksumMismatchError,
    MigrationError,
    apply_migrations,
    discover_migrations,
    parse_filename,
    split_statements,
)


class FakeResult:
    def __init__(self, records: list[dict[str, str]]) -> None:
        self._records = records

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self, applied: dict[str, str] | None = None) -> None:
        self.applied = dict(applied or {})
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params):
        self.calls.append((query, params))
        if query.strip().startswith("MATCH (m:Migration)"):
            return FakeResult([{"version": v, "checksum": c} for v, c in self.applied.items()])
        return FakeResult([])


def _write(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_filename() -> None:
    assert parse_filename("0001_constraints.cypher") == ("0001", "constraints")
    assert parse_filename("0002_add_index_to_thing.cypher") == ("0002", "add_index_to_thing")


def test_parse_filename_rejects_bad_name() -> None:
    with pytest.raises(MigrationError):
        parse_filename("0001.cypher")


def test_split_statements_strips_comments() -> None:
    text = "// a comment\nCREATE X IF NOT EXISTS A;\nCONSTRAINT B;\n"

    assert split_statements(text) == ["CREATE X IF NOT EXISTS A", "CONSTRAINT B"]


def test_discover_migrations_orders_by_filename(tmp_path: Path) -> None:
    _write(tmp_path, "0002_second.cypher", "CREATE B IF NOT EXISTS;")
    _write(tmp_path, "0001_first.cypher", "CREATE A IF NOT EXISTS;")

    files = discover_migrations(tmp_path)

    assert [migration.version for migration in files] == ["0001", "0002"]
    assert [migration.name for migration in files] == ["first", "second"]
    assert files[0].statements == ("CREATE A IF NOT EXISTS",)


def test_discover_migrations_computes_checksum(tmp_path: Path) -> None:
    path = _write(tmp_path, "0001_first.cypher", "CREATE A IF NOT EXISTS;")

    files = discover_migrations(tmp_path)

    assert files[0].checksum == hashlib.sha256(path.read_bytes()).hexdigest()


def test_discover_migrations_rejects_duplicate_versions(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.cypher", "CREATE A IF NOT EXISTS;")
    _write(tmp_path, "0001_other.cypher", "CREATE B IF NOT EXISTS;")

    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_apply_migrations_applies_pending(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "0001_constraints.cypher",
        "CREATE CONSTRAINT c IF NOT EXISTS FOR (n:N) REQUIRE n.id IS UNIQUE;",
    )
    files = discover_migrations(tmp_path)
    session = FakeSession()

    applied = apply_migrations(session, files)

    assert applied == ["0001"]
    queries = [query for query, _ in session.calls]
    assert "CREATE CONSTRAINT c IF NOT EXISTS FOR (n:N) REQUIRE n.id IS UNIQUE" in queries
    record_calls = [
        params for query, params in session.calls if query.startswith("CREATE (m:Migration")
    ]
    assert record_calls == [
        {"version": "0001", "name": "constraints", "checksum": files[0].checksum}
    ]


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.cypher", "CREATE A IF NOT EXISTS;")
    files = discover_migrations(tmp_path)
    session = FakeSession({"0001": files[0].checksum})

    applied = apply_migrations(session, files)

    assert applied == []
    # Only the bookkeeping lookup ran; no statement or record was executed.
    assert len(session.calls) == 1


def test_apply_migrations_detects_checksum_mismatch(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.cypher", "CREATE A IF NOT EXISTS;")
    files = discover_migrations(tmp_path)
    session = FakeSession({"0001": "deadbeef"})

    with pytest.raises(ChecksumMismatchError):
        apply_migrations(session, files)

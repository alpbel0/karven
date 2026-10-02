"""Unit tests for the `turcat` CLI subcommand (stub connector, no network)."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.connectors.base import SOURCE_ERROR, ConnectorError
from app.connectors.tuik.__main__ import _cmd_turcat, build_parser
from app.connectors.tuik.turcat import SectorFetch
from app.connectors.tuik.turcat_parsers import SECTORS, parse_sector

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "turcat"

PAGE_KEY = {code: key for code, key, _title, _page_id in SECTORS}
FIX = {
    "TURCAT_REEL": (FIXTURES / "reel.turcat.raw.json").read_bytes(),
    "TURCAT_MALI": (FIXTURES / "mali.turcat.raw.json").read_bytes(),
    "TURCAT_FINANS": (FIXTURES / "finans.turcat.raw.json").read_bytes(),
    "TURCAT_DIS": (FIXTURES / "dis.turcat.raw.json").read_bytes(),
    "TURCAT_NUFUS": (FIXTURES / "nufus.turcat.raw.json").read_bytes(),
}


class _StubClient:
    max_concurrency = 4


class StubConnector:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.client = _StubClient()
        self._fail = fail or set()
        self.closed = False

    def fetch_sector(self, external_code: str) -> SectorFetch:
        if external_code in self._fail:
            raise ConnectorError(SOURCE_ERROR, f"{external_code} boom")
        return SectorFetch(parse_sector(FIX[external_code], external_code), f"key-{external_code}")

    def close(self) -> None:
        self.closed = True


def test_turcat_parser_supports_dry_run() -> None:
    args = build_parser().parse_args(["turcat", "--dry-run"])
    assert args.command == "turcat"
    assert args.dry_run is True
    assert build_parser().parse_args(["turcat"]).dry_run is False


def test_turcat_dry_run_reports_every_sector(capsys) -> None:
    connector = StubConnector()
    args = argparse.Namespace(dry_run=True)

    assert _cmd_turcat(None, connector, args) == 0

    out = capsys.readouterr().out
    assert "sector TURCAT_REEL: rows=21 indicators=20 groups=1 values=20 unparsed=0" in out
    assert "sector TURCAT_NUFUS: rows=1 indicators=1 groups=0 values=1 unparsed=0" in out
    assert "unparseable: row 65" in out
    assert "turcat (dry-run): 5 sectors ok, 0 failed; indicators=208 values=207 unparsed=29" in out
    assert "TURCAT_NUFUS:251" in out


def test_turcat_dry_run_groups_failures(capsys) -> None:
    connector = StubConnector(fail={"TURCAT_MALI"})
    args = argparse.Namespace(dry_run=True)

    assert _cmd_turcat(None, connector, args) == 0

    out = capsys.readouterr().out
    assert "turcat (dry-run): 4 sectors ok, 1 failed" in out
    assert "failed by kind: source_error (1): TURCAT_MALI" in out

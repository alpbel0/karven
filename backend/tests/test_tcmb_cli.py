"""Unit tests for the TCMB CLI subcommands (stub connector, no network/DB)."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

import pytest

from app.connectors.base import (
    NOT_FOUND,
    ROLE_OTHER,
    THROTTLED,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
)
from app.connectors.tcmb.__main__ import _cmd_catalog, _dry_run_fetch, build_parser


def test_build_parser_catalog() -> None:
    args = build_parser().parse_args(["catalog"])
    assert args.command == "catalog"
    assert args.dry_run is False
    assert args.limit is None
    assert args.group is None
    assert args.source == "tcmb"
    args = build_parser().parse_args(
        ["catalog", "--limit", "3", "--group", "bie_cli2", "--dry-run"]
    )
    assert args.limit == 3
    assert args.group == "bie_cli2"
    assert args.dry_run is True


def test_build_parser_source() -> None:
    assert build_parser().parse_args(["catalog", "--source", "hmb"]).source == "hmb"
    assert build_parser().parse_args(["fetch", "--source", "hmb"]).source == "hmb"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["catalog", "--source", "nope"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch", "--source", "nope"])


def test_build_parser_fetch() -> None:
    args = build_parser().parse_args(["fetch", "--series", "bie_dkefkytl:TP.DK.USD.A.EF.YTL"])
    assert args.command == "fetch"
    assert args.series == "bie_dkefkytl:TP.DK.USD.A.EF.YTL"
    assert args.start == "2000-01-01"
    args = build_parser().parse_args(
        [
            "fetch",
            "--dataset",
            "bie_dkefkytl",
            "--code",
            "SERIE=TP.DK.USD.A.EF.YTL",
            "--start",
            "2001-01-01",
        ]
    )
    assert args.dataset == "bie_dkefkytl"
    assert args.code == ["SERIE=TP.DK.USD.A.EF.YTL"]
    assert args.start == "2001-01-01"


class StubConnector:
    def __init__(
        self, *, result: FetchResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.closed = False

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    def close(self) -> None:
        self.closed = True


def _fetch_args(**overrides) -> argparse.Namespace:
    values = {
        "series": "bie_dkefkytl:TP.DK.USD.A.EF.YTL",
        "dataset": None,
        "code": [],
        "start": "2000-01-01",
        "dry_run": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _result() -> FetchResult:
    return FetchResult(
        external_code="TP.DK.USD.A.EF.YTL",
        points=[(date(2000, 1, 3), Decimal("0.5397")), (date(2026, 10, 5), Decimal("48.9357"))],
        raw_object_keys=["key"],
        channel="evds3",
    )


def test_dry_run_fetch_prints_points(capsys) -> None:
    connector = StubConnector(result=_result())
    assert _dry_run_fetch(connector, _fetch_args()) == 0
    out = capsys.readouterr().out
    assert "fetch bie_dkefkytl: points=2" in out
    assert "periods: 2000-01-03..2026-10-05" in out


def test_fetch_error_path_exits_one(capsys) -> None:
    connector = StubConnector(error=ConnectorError(NOT_FOUND, "no series"))
    assert _dry_run_fetch(connector, _fetch_args()) == 1
    err = capsys.readouterr().err
    assert "fetch: error: no series" in err


def test_fetch_throttled_stops_immediately(capsys) -> None:
    connector = StubConnector(error=ConnectorError(THROTTLED, "slow down"))
    assert _dry_run_fetch(connector, _fetch_args()) == 1
    err = capsys.readouterr().err
    assert "throttled: stop and report" in err


def test_fetch_bad_series_argument(capsys) -> None:
    connector = StubConnector(result=_result())
    assert _dry_run_fetch(connector, _fetch_args(series="bad")) == 1
    assert "bad --series" in capsys.readouterr().err


def test_fetch_bad_start(capsys) -> None:
    connector = StubConnector(result=_result())
    assert _dry_run_fetch(connector, _fetch_args(start="nope")) == 1
    assert "invalid --start" in capsys.readouterr().err


class _CatalogStub:
    def __init__(self, metas: list[DatasetMeta], failed_groups=None) -> None:
        self._metas = metas
        self.failed_groups = failed_groups or []
        self.source = "tcmb"
        self.closed = False

    def list_datasets(self):
        yield from self._metas

    def close(self) -> None:
        self.closed = True


def _meta(code: str, series: list[tuple[str, str, str]]) -> DatasetMeta:
    return DatasetMeta(
        external_code=code,
        name=code,
        dimensions=[
            DimensionMeta(
                code="SERIE",
                label="Seri",
                position=0,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(
                        code=serie_code,
                        label=serie_code,
                        attributes={"frequency": frequency, "aggregation": aggregation},
                    )
                    for serie_code, frequency, aggregation in series
                ],
            )
        ],
    )


def test_catalog_dry_run_counts(capsys) -> None:
    connector = _CatalogStub(
        [
            _meta("g1", [("a", "daily", "avg"), ("b", "daily", "avg")]),
            _meta("g2", [("c", "monthly", "last")]),
            _meta("empty", []),
        ]
    )
    args = argparse.Namespace(dry_run=True, limit=None, group=None)
    assert _cmd_catalog(None, connector, args) == 0
    out = capsys.readouterr().out
    assert "groups=3 series=3 empty=1" in out
    assert "source=tcmb" in out
    assert "daily=2, monthly=1" in out
    assert "avg=2, last=1" in out


def test_catalog_reports_failed_groups_and_exits_one(capsys) -> None:
    connector = _CatalogStub(
        [_meta("g1", [("a", "daily", "avg")])],
        failed_groups=[("g2", "timeout", "boom")],
    )
    args = argparse.Namespace(dry_run=True, limit=None, group=None)
    assert _cmd_catalog(None, connector, args) == 1
    err = capsys.readouterr().err
    assert "group g2 failed (timeout)" in err
    assert "catalog is incomplete" in err

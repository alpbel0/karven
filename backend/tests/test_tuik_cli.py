"""Unit tests for the TÜİK connector CLI (no database, no network)."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from typing import Any

from app.connectors.base import (
    EMPTY,
    NOT_FOUND,
    ConnectorError,
    DatasetMeta,
    DimensionMeta,
    FetchResult,
)
from app.connectors.tuik.__main__ import (
    _cmd_catalog,
    _dry_run_fetch,
    _verify_completeness,
    build_parser,
)
from app.connectors.tuik.connector import DataflowCatalog
from app.connectors.tuik.parsers import DataflowInfo


class _StubClient:
    max_concurrency = 2
    throttled = False


def _dataset(external_code: str) -> DatasetMeta:
    return DatasetMeta(
        external_code=external_code,
        name=external_code,
        dimensions=[
            DimensionMeta(
                code="REF_AREA",
                label="Reference area",
                position=0,
                role="geo",
                codes=[],
            ),
        ],
    )


class StubConnector:
    """Minimal stand-in for :class:`TuikConnector` (duck-typed)."""

    institution_code = "stub"
    institution_name = "Stub Institution"
    channel = "stub"

    def __init__(self, results: dict[str, Any]) -> None:
        self.client = _StubClient()
        self._results = results

    def dataflows(self) -> list[DataflowInfo]:
        return [
            DataflowInfo(
                dataflow_id=dataflow_id,
                version="1.0",
                agency="TR",
                title=dataflow_id,
                description=None,
                source_category=None,
            )
            for dataflow_id in self._results
        ]

    def dataset_meta(self, info: DataflowInfo) -> DatasetMeta:
        result = self._results[info.dataflow_id]
        if isinstance(result, Exception):
            raise result
        return result


def test_catalog_dry_run_continues_after_unexpected_error(capsys) -> None:
    connector = StubConnector({"DF_GOOD": _dataset("DF_GOOD"), "DF_BAD": ValueError("boom")})
    args = argparse.Namespace(dry_run=True, limit=None, dataflow=None, verify_completeness=False)

    assert _cmd_catalog(None, connector, args) == 0

    captured = capsys.readouterr()
    assert "catalog (dry-run): 1 dataflows ok, 1 failed, 2 processed" in captured.out
    assert "failed by kind: format_changed (1): DF_BAD" in captured.out
    assert "datasets: found=1 (dry-run, nothing written)" in captured.out
    assert "DF_BAD: format_changed: ValueError: boom" in captured.err


def test_catalog_dry_run_groups_every_failure_by_kind(capsys) -> None:
    connector = StubConnector(
        {
            "DF_NF": ConnectorError(NOT_FOUND, "gone"),
            "DF_EMPTY": ConnectorError(EMPTY, "nothing"),
        }
    )
    args = argparse.Namespace(dry_run=True, limit=None, dataflow=None, verify_completeness=False)

    assert _cmd_catalog(None, connector, args) == 0

    captured = capsys.readouterr()
    assert "failed by kind: empty (1): DF_EMPTY" in captured.out
    assert "failed by kind: not_found (1): DF_NF" in captured.out


def test_catalog_parser_flags() -> None:
    args = build_parser().parse_args(["catalog", "--dry-run"])
    assert args.dry_run is True
    assert args.verify_completeness is False
    assert build_parser().parse_args(["catalog"]).dry_run is False
    verify = build_parser().parse_args(["catalog", "--verify-completeness", "--dataflow", "DF_X"])
    assert verify.verify_completeness is True
    assert verify.dataflow == "DF_X"


def test_fetch_parser_supports_dataset_codes_and_series() -> None:
    args = build_parser().parse_args(
        ["fetch", "--dataset", "DF_X", "--code", "REF_AREA=TR", "--code", "FREQ=A"]
    )
    assert args.dataset == "DF_X"
    assert args.code == ["REF_AREA=TR", "FREQ=A"]
    assert build_parser().parse_args(["fetch", "--series", "DF_X:TR.A"]).series == "DF_X:TR.A"


class _FetchStub:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def dataset_meta(self, external_code: str) -> DatasetMeta:
        return DatasetMeta(
            external_code=external_code,
            name=external_code,
            dimensions=[
                DimensionMeta(code="REF_AREA", label="Reference area", position=0, role="geo"),
                DimensionMeta(code="TIME_PERIOD", label="Time", position=1, role="time"),
            ],
        )

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        self.calls.append((dataset_code, codes, order, start))
        return FetchResult(
            external_code=f"{dataset_code}:TR",
            points=[(date(2024, 1, 1), Decimal("1"))],
            raw_object_keys=[],
            channel="stub",
        )


def test_dry_run_fetch_uses_dataset_dimension_order(capsys) -> None:
    connector = _FetchStub()
    args = argparse.Namespace(series=None, dataset="DF_X", code=["REF_AREA=TR"], start="2000-01-01")

    assert _dry_run_fetch(connector, args) == 0

    assert connector.calls == [("DF_X", {"REF_AREA": "TR"}, ["REF_AREA"], date(2000, 1, 1))]
    output = capsys.readouterr().out
    assert "points=1" in output
    assert "2024-01-01..2024-01-01" in output


class _VerifyConnector:
    institution_code = "tuik"
    institution_name = "Türkiye İstatistik Kurumu"
    channel = "databrowser2"

    def __init__(self, results: dict[str, DataflowCatalog]) -> None:
        self._results = results

    def dataflows(self) -> list[DataflowInfo]:
        return [
            DataflowInfo(
                dataflow_id=dataflow_id,
                version="1.0",
                agency="TR",
                title=dataflow_id,
                description=None,
                source_category=None,
            )
            for dataflow_id in self._results
        ]

    def catalog_dataflow(self, info: DataflowInfo) -> DataflowCatalog:
        return self._results[info.dataflow_id]


class _FakeDataset:
    source_incomplete = False
    source_incomplete_note: str | None = None


class _FakeSession:
    def __init__(self, dataset: _FakeDataset) -> None:
        self.dataset = dataset
        self._calls = 0
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement: Any) -> Any:
        self._calls += 1
        if self._calls == 1:
            return None
        return self.dataset

    def add(self, obj: Any) -> None:
        obj.id = 7

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_verify_completeness_flags_a_mismatch(capsys) -> None:
    dataset = _FakeDataset()
    session = _FakeSession(dataset)
    info = DataflowInfo(
        dataflow_id="DF_UHTI_COGRAFI_C",
        version="1.0",
        agency="TR",
        title="UHTI",
        description=None,
        source_category=None,
    )
    connector = _VerifyConnector(
        {"DF_UHTI_COGRAFI_C": DataflowCatalog("DF_UHTI_COGRAFI_C", 6963, 6932, 0, [])}
    )
    args = argparse.Namespace(dataflow=None, limit=None, verify_completeness=True)

    assert _verify_completeness(session, connector, args, [info]) == 0

    assert dataset.source_incomplete is True
    assert dataset.source_incomplete_note == "source reports 6963, serves 6932"
    assert "1 flagged" in capsys.readouterr().out


def test_verify_completeness_leaves_matching_dataset_unflagged(capsys) -> None:
    dataset = _FakeDataset()
    session = _FakeSession(dataset)
    info = DataflowInfo(
        dataflow_id="DF_OK",
        version="1.0",
        agency="TR",
        title="OK",
        description=None,
        source_category=None,
    )
    connector = _VerifyConnector({"DF_OK": DataflowCatalog("DF_OK", 10, 10, 0, [])})
    args = argparse.Namespace(dataflow=None, limit=None, verify_completeness=True)

    assert _verify_completeness(session, connector, args, [info]) == 0
    assert dataset.source_incomplete is False
    assert "0 flagged" in capsys.readouterr().out

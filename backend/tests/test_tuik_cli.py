"""Unit tests for the TÜİK connector CLI (no database, no network)."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.connectors.base import (
    EMPTY,
    NOT_FOUND,
    TIMEOUT,
    ConnectorError,
    DatasetMeta,
    DimensionMeta,
    FetchResult,
)
from app.connectors.tuik import __main__ as tuik_cli
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

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        return []

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

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        return []

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


def _info(dataflow_id: str, *, listed: bool = True) -> DataflowInfo:
    return DataflowInfo(
        dataflow_id=dataflow_id,
        version="1.0",
        agency="TR",
        title=dataflow_id,
        description=None,
        source_category=None,
        listed=listed,
    )


class _UnlistedConnector:
    """Listed DF_LIVE plus one unlisted dataflow whose answer the test chooses."""

    institution_code = "tuik"
    institution_name = "Türkiye İstatistik Kurumu"
    channel = "databrowser2"

    def __init__(self, unlisted_result: Any, *, listed: tuple[str, ...] = ("DF_LIVE",)) -> None:
        self.client = _StubClient()
        self._listed = listed
        self._unlisted_result = unlisted_result

    def dataflows(self) -> list[DataflowInfo]:
        return [_info(dataflow_id) for dataflow_id in self._listed]

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        return [_info("DF_GONE", listed=False)]

    def dataset_meta(self, info: DataflowInfo) -> DatasetMeta:
        if not info.listed and isinstance(self._unlisted_result, Exception):
            raise self._unlisted_result
        return replace(_dataset(info.dataflow_id), attributes={"channel": "databrowser2"})


class _Dataset:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes


class _MarkerSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _run_catalog(monkeypatch, connector, existing: dict[str, _Dataset]):
    upserted: dict[str, DatasetMeta] = {}

    class _Institution:
        id = 1

    monkeypatch.setattr(tuik_cli, "upsert_institution", lambda *a, **k: _Institution())

    def fake_upsert(session, institution_id, meta):
        upserted[meta.external_code] = meta
        return "updated", None

    monkeypatch.setattr(tuik_cli, "upsert_dataset", fake_upsert)
    monkeypatch.setattr(
        tuik_cli,
        "_existing_dataset",
        lambda session, institution_id, dataflow_id: existing.get(dataflow_id),
    )
    args = argparse.Namespace(dry_run=False, limit=None, dataflow=None, verify_completeness=False)
    assert _cmd_catalog(_MarkerSession(), connector, args) == 0
    return upserted


def test_catalog_marks_an_unlisted_dataflow_that_still_answers(monkeypatch, capsys) -> None:
    connector = _UnlistedConnector(None)
    upserted = _run_catalog(monkeypatch, connector, {})

    today = tuik_cli.date.today().isoformat()
    assert upserted["DF_GONE"].attributes["unlisted_since"] == today
    assert "unlisted_since" not in upserted["DF_LIVE"].attributes
    assert "removed_at" not in upserted["DF_GONE"].attributes
    assert "dataflows: listed=1 unlisted=1 removed=0" in capsys.readouterr().out


def test_catalog_keeps_the_earliest_unlisted_since(monkeypatch) -> None:
    connector = _UnlistedConnector(None)
    existing = {
        "DF_GONE": _Dataset({"unlisted_since": "2026-01-05", "removed_at": "2026-02-01"}),
    }
    upserted = _run_catalog(monkeypatch, connector, existing)

    attributes = upserted["DF_GONE"].attributes
    assert attributes["unlisted_since"] == "2026-01-05"
    # it answers again, so the stale removed marker is not carried through
    assert "removed_at" not in attributes


def test_catalog_marks_unlisted_not_found_as_removed(monkeypatch, capsys, caplog) -> None:
    connector = _UnlistedConnector(ConnectorError(NOT_FOUND, "structure gone"))
    existing = {"DF_GONE": _Dataset({"channel": "databrowser2", "unlisted_since": "2026-01-05"})}
    with caplog.at_level(logging.WARNING, logger="app.connectors.tuik"):
        upserted = _run_catalog(monkeypatch, connector, existing)

    assert "DF_GONE" not in upserted  # dimensions untouched: no upsert at all
    attributes = existing["DF_GONE"].attributes
    assert attributes["removed_at"] == tuik_cli.date.today().isoformat()
    assert attributes["unlisted_since"] == "2026-01-05"
    assert attributes["channel"] == "databrowser2"
    assert any("no longer serves" in record.getMessage() for record in caplog.records)
    captured = capsys.readouterr()
    assert "dataflows: listed=1 unlisted=0 removed=1" in captured.out
    assert "1 dataflows ok, 0 failed, 2 processed" in captured.out


def test_catalog_keeps_the_earliest_removed_at(monkeypatch) -> None:
    connector = _UnlistedConnector(ConnectorError(NOT_FOUND, "gone"))
    existing = {"DF_GONE": _Dataset({"removed_at": "2026-03-01"})}
    _run_catalog(monkeypatch, connector, existing)

    assert existing["DF_GONE"].attributes["removed_at"] == "2026-03-01"


def test_catalog_unlisted_timeout_is_a_failure_not_removed(monkeypatch, capsys) -> None:
    connector = _UnlistedConnector(ConnectorError(TIMEOUT, "slow"))
    existing = {"DF_GONE": _Dataset({"channel": "databrowser2"})}
    _run_catalog(monkeypatch, connector, existing)

    assert "removed_at" not in existing["DF_GONE"].attributes
    captured = capsys.readouterr()
    assert "1 dataflows ok, 1 failed, 2 processed" in captured.out
    assert "removed=0" in captured.out
    assert "failed by kind: timeout (1): DF_GONE" in captured.out


def test_catalog_relisted_dataflow_clears_the_markers(monkeypatch) -> None:
    # DF_GONE is back in the live listing: a listed info upserts attributes
    # without either marker, so the wholesale replace clears them.
    connector = _UnlistedConnector(None, listed=("DF_LIVE", "DF_GONE"))
    connector.unlisted_dataflows = lambda: []  # type: ignore[method-assign]
    existing = {"DF_GONE": _Dataset({"unlisted_since": "2026-01-05", "removed_at": "2026-02-01"})}
    upserted = _run_catalog(monkeypatch, connector, existing)

    assert upserted["DF_GONE"].attributes == {"channel": "databrowser2"}


def test_catalog_dry_run_handles_unlisted_without_writing(capsys) -> None:
    connector = _UnlistedConnector(ConnectorError(NOT_FOUND, "gone"))
    args = argparse.Namespace(
        dry_run=True, limit=None, dataflow="DF_GONE", verify_completeness=False
    )

    assert _cmd_catalog(None, connector, args) == 0

    out = capsys.readouterr().out
    assert "dataflows: listed=0 unlisted=0 removed=1" in out
    assert "nothing written" in out


def test_dataflow_filter_finds_an_unlisted_dataflow(capsys) -> None:
    connector = _UnlistedConnector(None)
    args = argparse.Namespace(
        dry_run=True, limit=None, dataflow="TR,DF_GONE,1.0", verify_completeness=False
    )

    assert _cmd_catalog(None, connector, args) == 0

    assert "dataflows: listed=0 unlisted=1 removed=0" in capsys.readouterr().out


def test_known_dataflows_loader_only_takes_databrowser2_datasets() -> None:
    class _Row:
        def __init__(self, code: str, attributes: dict[str, Any]) -> None:
            self.external_code = code
            self.attributes = attributes
            self.name = f"{code} name"
            self.description = None
            self.source_category = "Cat"

    rows = [
        _Row("DF_A", {"channel": "databrowser2", "agency": "TR", "version": "1.0"}),
        _Row("TURCAT_X", {"channel": "turcat"}),
        _Row("CIP_1", {"channel": "cip", "agency": "TR", "version": "1.0"}),
        _Row("DF_NO_VERSION", {"channel": "databrowser2", "agency": "TR"}),
    ]

    class _Result:
        def scalars(self):
            return iter(rows)

    class _Session:
        def execute(self, statement):
            return _Result()

    known = tuik_cli.load_known_dataflows(_Session(), "tuik")

    assert [info.dataflow_id for info in known] == ["DF_A"]
    assert known[0].listed is False
    assert known[0].dataset_identifier == "TR,DF_A,1.0"
    assert known[0].source_category == "Cat"


# --- frequency-backfill (Task 2.4b Part 4) ----------------------------------


class _FreqResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FreqClient:
    """Fake databrowser2 client answering only FREQ codelists; records calls."""

    max_concurrency = 2
    throttled = False

    def __init__(self, by_identifier: dict[str, Any]) -> None:
        self._by_identifier = by_identifier
        self.calls: list[tuple[str, str]] = []

    def dataset_partial_codelist(self, dataset_id: str, dimension: str, criteria=None) -> Any:
        self.calls.append((dataset_id, dimension))
        assert dimension == "FREQ"
        return _FreqResponse(self._by_identifier[dataset_id])


class _BFDataset:
    def __init__(
        self,
        code: str,
        *,
        attributes: dict[str, Any] | None = None,
        dimensions: list[Any] | None = None,
    ) -> None:
        self.external_code = code
        self.attributes = attributes or {}
        self.dimensions = dimensions or []


class _BFDimension:
    def __init__(self, code: str, *, codes: list[Any] | None = None) -> None:
        self.code = code
        self.codes = codes or []


class _BFCode:
    def __init__(self, code: str, *, attributes: dict[str, Any] | None = None) -> None:
        self.code = code
        self.attributes = attributes or {}


class _BFSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _freq_identifier(code: str) -> dict[str, Any]:
    return {"channel": "databrowser2", "agency": "TR", "dataflow_id": code, "version": "1.0"}


def _codelist(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"criteria": [{"id": "FREQ", "values": values}], "obsCount": 1}


def test_can_build_series_matches_the_measurement() -> None:
    no_freq = _BFDataset("A")
    assert not tuik_cli._can_build_series(no_freq)
    assert tuik_cli._can_build_series(_BFDataset("A", attributes={"default_frequency": "annual"}))
    assert tuik_cli._can_build_series(_BFDataset("A", dimensions=[_BFDimension("FREQ")]))
    assert tuik_cli._can_build_series(
        _BFDataset(
            "A",
            dimensions=[
                _BFDimension("X", codes=[_BFCode("x", attributes={"frequency": "monthly"})])
            ],
        )
    )


def test_frequency_backfill_dry_run_writes_nothing(capsys) -> None:
    dataset = _BFDataset("DF_A", attributes=_freq_identifier("DF_A"))
    before = dict(dataset.attributes)
    client = _FreqClient(
        {"TR,DF_A,1.0": _codelist([{"id": "A2", "name": "Biennial", "isSelectable": True}])}
    )

    code = tuik_cli.run_frequency_backfill(
        [dataset], client, session=_BFSession(), dry_run=True, workers=2
    )

    assert dataset.attributes == before
    assert client.calls == [("TR,DF_A,1.0", "FREQ")]
    out = capsys.readouterr().out
    assert "DF_A single A2(Biennial) -> biennial" in out
    assert "special codes (1):" in out
    assert "DF_A: A2(Biennial) -> biennial" in out
    assert code == 0  # A2 resolves to a single frequency; only the special block flags it


def test_frequency_backfill_exit_code_zero_when_all_single(capsys) -> None:
    dataset = _BFDataset("DF_M", attributes=_freq_identifier("DF_M"))
    client = _FreqClient(
        {"TR,DF_M,1.0": _codelist([{"id": "M", "name": "Monthly", "isSelectable": True}])}
    )
    code = tuik_cli.run_frequency_backfill([dataset], client, session=None, dry_run=True, workers=1)
    assert code == 0
    assert "special codes" not in capsys.readouterr().out


def test_frequency_backfill_multiple_lists_all_codes_and_exits_one(capsys) -> None:
    dataset = _BFDataset("DF_MULTI", attributes=_freq_identifier("DF_MULTI"))
    client = _FreqClient(
        {
            "TR,DF_MULTI,1.0": _codelist(
                [
                    {"id": "M", "name": "Monthly", "isSelectable": True},
                    {"id": "A", "name": "Annual", "isSelectable": True},
                ]
            )
        }
    )
    code = tuik_cli.run_frequency_backfill([dataset], client, session=None, dry_run=True, workers=1)
    out = capsys.readouterr().out
    assert code == 1
    assert "multiple (1):" in out
    assert "DF_MULTI: M(Monthly), A(Annual)" in out
    assert "status: " in out


def test_frequency_backfill_query_error_exits_one(capsys) -> None:
    dataset = _BFDataset("DF_ERR", attributes=_freq_identifier("DF_ERR"))
    client = _FreqClient({})  # any call raises KeyError inside -> we pre-raise instead

    def boom(dataset_id: str, dimension: str, criteria=None):
        raise ConnectorError(NOT_FOUND, "gone")

    client.dataset_partial_codelist = boom  # type: ignore[method-assign]
    code = tuik_cli.run_frequency_backfill([dataset], client, session=None, dry_run=True, workers=1)
    out = capsys.readouterr().out
    assert code == 1
    assert "DF_ERR query_error" in out


def test_frequency_backfill_write_mode_applies_keep_drop_rule() -> None:
    # (1) multiple drops a stale default; (2) query_error keeps it.
    stale = _BFDataset(
        "DF_DROP",
        attributes={**_freq_identifier("DF_DROP"), "default_frequency": "monthly"},
    )
    keep = _BFDataset(
        "DF_KEEP",
        attributes={**_freq_identifier("DF_KEEP"), "default_frequency": "monthly"},
    )

    class _Client:
        max_concurrency = 1

        def __init__(self) -> None:
            self.calls: list[str] = []

        def dataset_partial_codelist(self, dataset_id, dimension, criteria=None):
            self.calls.append(dataset_id)
            if dataset_id == "TR,DF_DROP,1.0":
                return _FreqResponse(
                    _codelist(
                        [
                            {"id": "M", "name": "Monthly", "isSelectable": True},
                            {"id": "A", "name": "Annual", "isSelectable": True},
                        ]
                    )
                )
            raise ConnectorError(NOT_FOUND, "gone")

    session = _BFSession()
    client = _Client()
    tuik_cli.run_frequency_backfill(
        [stale, keep], client, session=session, dry_run=False, workers=1
    )

    assert session.commits == 2  # commit per dataset
    assert "default_frequency" not in stale.attributes
    assert stale.attributes["frequency_resolution"]["status"] == "multiple"
    assert keep.attributes["default_frequency"] == "monthly"
    assert keep.attributes["frequency_resolution"]["status"] == "query_error"


def test_frequency_backfill_write_mode_stores_single_default() -> None:
    dataset = _BFDataset("DF_S", attributes=_freq_identifier("DF_S"))
    client = _FreqClient(
        {"TR,DF_S,1.0": _codelist([{"id": "M", "name": "Monthly", "isSelectable": True}])}
    )
    code = tuik_cli.run_frequency_backfill(
        [dataset], client, session=_BFSession(), dry_run=False, workers=1
    )
    assert code == 0
    assert dataset.attributes["default_frequency"] == "monthly"
    assert dataset.attributes["frequency_source"]["origin"] == "hidden_freq_codelist"


def test_frequency_backfill_never_requests_observation_values() -> None:
    dataset = _BFDataset("DF_A", attributes=_freq_identifier("DF_A"))
    client = _FreqClient(
        {"TR,DF_A,1.0": _codelist([{"id": "M", "name": "Monthly", "isSelectable": True}])}
    )
    tuik_cli.run_frequency_backfill([dataset], client, session=None, dry_run=True, workers=1)
    # The only calls are hidden FREQ partial codelists; no /data or /download.
    assert client.calls == [("TR,DF_A,1.0", "FREQ")]


def test_frequency_backfill_parser_flags() -> None:
    args = build_parser().parse_args(["frequency-backfill", "--dry-run", "--limit", "5"])
    assert args.dry_run is True
    assert args.limit == 5
    assert args.workers is None
    assert args.dataflow is None

"""Unit tests for the data-fetch agent tools (metadata only, no values)."""

from __future__ import annotations

from datetime import date

import pytest

from app.fetching.agent import tools
from tests.fetch_agent_fakes import (
    Store,
    factory,
    make_dataset,
    make_failed_job,
    make_institution,
    make_observation,
    make_series,
)


def _catalog():
    store = Store()
    institution = make_institution(store, code="tcmb")
    dataset = make_dataset(
        store,
        institution,
        external_code="TP_DK",
        name="Döviz Kurları",
        channel="evds3",
        series_codes=("TP.DK.USD", "TP.DK.EUR"),
    )
    other = make_dataset(
        store,
        institution,
        external_code="TP_CLI",
        name="Tüketici Fiyat Endeksi",
        channel="evds3",
        dimension_code="SERIE",
        series_codes=("TP.CLI2",),
    )
    series = make_series(
        store,
        institution,
        dataset,
        external_code="TP_DK:TP.DK.USD",
        name="ABD Doları",
        frequency="daily",
        unit="TL",
        coverage_start=date(2000, 1, 1),
        coverage_end=date(2026, 10, 1),
    )
    for month in range(1, 4):
        make_observation(store, series, date(2026, month, 1), value=1000 + month)
    make_failed_job(
        store,
        institution,
        external_code="TP_DK:TP.DK.USD",
        dataset_code="TP_DK",
        codes={"SERIE": "TP.DK.USD"},
        error_reason="timeout: slow source",
        series_id=series.id,
    )
    return store, institution, dataset, other, series


def test_search_catalog_returns_metadata_only() -> None:
    store, _institution, _dataset, _other, _series = _catalog()
    session = factory(store)()

    by_dataset = tools.search_catalog(session, query="Kurları")
    assert by_dataset[0]["dataset"] == "TP_DK"
    assert by_dataset[0]["name"] == "Döviz Kurları"
    assert set(by_dataset[0]) == {
        "institution",
        "dataset",
        "name",
        "frequency",
        "coverage_start",
        "coverage_end",
    }

    by_series = tools.search_catalog(session, query="Dolar")
    assert by_series[0]["dataset"] == "TP_DK"
    assert by_series[0]["name"] == "ABD Doları"
    assert by_series[0]["frequency"] == "daily"

    filtered = tools.search_catalog(session, query="TP", institution="tcmb")
    assert {row["dataset"] for row in filtered} <= {"TP_DK", "TP_CLI"}
    assert tools.search_catalog(session, query="TP", institution=None, limit=1) == filtered[:1]


def test_search_catalog_rejects_unknown_institution() -> None:
    store, *_ = _catalog()
    session = factory(store)()
    with pytest.raises(ValueError):
        tools.search_catalog(session, query="TP", institution="nope")


def test_get_dataset_meta_lists_dimensions_and_codes() -> None:
    store, *_ = _catalog()
    session = factory(store)()

    meta = tools.get_dataset_meta(session, institution="tcmb", dataset="TP_DK")
    assert meta["channel"] == "evds3"
    assert meta["dimensions"] == [
        {"code": "SERIE", "name": "SERIE", "position": 0, "is_time": False}
    ]
    assert meta["codes"] is None

    with_codes = tools.get_dataset_meta(
        session, institution="tcmb", dataset="TP_DK", dimension="SERIE", code_query="USD"
    )
    assert with_codes["codes"] == [{"code": "TP.DK.USD", "label": "TP.DK.USD"}]

    with pytest.raises(ValueError):
        tools.get_dataset_meta(session, institution="tcmb", dataset="MISSING")
    with pytest.raises(ValueError):
        tools.get_dataset_meta(session, institution="tcmb", dataset="TP_DK", dimension="NOPE")


def test_get_series_meta_count_without_values() -> None:
    store, institution, dataset, _other, series = _catalog()
    session = factory(store)()

    meta = tools.get_series_meta(
        session, institution="tcmb", dataset="TP_DK", codes={"SERIE": "TP.DK.USD"}
    )
    assert meta["exists"] is True
    assert meta["observation_count"] == 3
    assert meta["unit"] == "TL"
    assert meta["frequency"] == "daily"
    assert meta["codes_valid"] is True
    assert meta["invalid_codes"] == []
    assert "value" not in meta
    assert set(meta) == {
        "exists",
        "external_code",
        "name",
        "unit",
        "frequency",
        "coverage_start",
        "coverage_end",
        "observation_count",
        "codes_valid",
        "invalid_codes",
    }


def test_get_series_meta_missing_row_is_not_absence_and_reports_valid_codes() -> None:
    store, institution, _dataset, _other, _series = _catalog()
    session = factory(store)()

    # TP_CLI is catalogued but has no series row: the lazy-creation note must be
    # present and the valid code must be recognised from the codelist.
    missing = tools.get_series_meta(
        session, institution="tcmb", dataset="TP_CLI", codes={"SERIE": "TP.CLI2"}
    )
    assert missing["exists"] is False
    assert missing["codes_valid"] is True
    assert missing["invalid_codes"] == []
    assert "note" in missing
    assert "created on the first successful fetch" in missing["note"]
    assert missing["external_code"] == "TP_CLI:TP.CLI2"
    assert "value" not in missing


def test_get_series_meta_flags_codes_not_in_the_codelist() -> None:
    store, institution, _dataset, _other, _series = _catalog()
    session = factory(store)()

    invalid = tools.get_series_meta(
        session, institution="tcmb", dataset="TP_DK", codes={"SERIE": "NOPE"}
    )
    assert invalid["codes_valid"] is False
    assert invalid["invalid_codes"] == ["NOPE"]
    assert invalid["exists"] is False
    assert "note" in invalid

    # A missing dimension is also an invalid request (not a raised error).
    incomplete = tools.get_series_meta(session, institution="tcmb", dataset="TP_DK", codes={})
    assert incomplete["codes_valid"] is False
    assert incomplete["invalid_codes"] == []


def test_get_job_history_lists_metadata() -> None:
    store, institution, _dataset, _other, _series = _catalog()
    session = factory(store)()
    make_failed_job(
        store,
        institution,
        external_code="TP_DK:TP.DK.EUR",
        dataset_code="TP_DK",
        codes={"SERIE": "TP.DK.EUR"},
        origin="agent_retry",
        agent_round=1,
        error_reason="source_error: 500",
    )

    history = tools.get_job_history(session, institution="tcmb", external_code="TP_DK:TP.DK.EUR")
    assert history[0]["status"] == "failed"
    assert history[0]["origin"] == "agent_retry"
    assert history[0]["agent_round"] == 1
    assert history[0]["reason"] == "source_error: 500"
    assert set(history[0]) == {
        "job_id",
        "status",
        "reason",
        "origin",
        "agent_round",
        "requested_at",
        "finished_at",
    }


def test_build_tools_wraps_every_read_only_tool() -> None:
    store, *_ = _catalog()
    session_factory = factory(store)
    built = tools.build_tools(session_factory)
    assert set(built) == {
        "search_catalog",
        "get_dataset_meta",
        "get_series_meta",
        "get_job_history",
    }
    schema, function = built["search_catalog"]
    assert schema["description"]
    assert function("Dolar")[0]["name"] == "ABD Doları"

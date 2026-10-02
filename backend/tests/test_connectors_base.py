"""Unit tests for the source-agnostic connector interface."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.connectors.base import (
    ERROR_KINDS,
    ConnectorError,
    InMemoryObjectStore,
    SeriesMeta,
    SourceConnector,
    build_series_definition,
    store_raw,
)
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, DatasetDimension, DimensionCode


def _dataset() -> Dataset:
    dataset = Dataset(institution_id=1, external_code="DF_X", name="Test dataset")
    dataset.dimensions = [
        DatasetDimension(
            code="REF_AREA",
            label="Reference area",
            position=0,
            role="geo",
            codes=[DimensionCode(code="TR", label="Türkiye")],
        ),
        DatasetDimension(
            code="FREQ",
            label="Frequency",
            position=1,
            role="frequency",
            codes=[
                DimensionCode(code="A", label="Annual"),
                DimensionCode(code="M", label="Monthly"),
            ],
        ),
        DatasetDimension(
            code="UNIT_MEASURE",
            label="Unit",
            position=2,
            role="other",
            codes=[DimensionCode(code="TTRY", label="Thousand TRY")],
        ),
        DatasetDimension(code="TIME_PERIOD", label="Time", position=3, role="time", codes=[]),
    ]
    return dataset


def test_build_series_definition_derives_fields() -> None:
    definition = build_series_definition(
        _dataset(), {"REF_AREA": "TR", "FREQ": "A", "UNIT_MEASURE": "TTRY"}
    )
    assert definition.external_code == "DF_X:TR.A.TTRY"
    assert definition.frequency == "annual"
    assert definition.unit == "Thousand TRY"
    assert definition.breakdown["FREQ"] == {"code": "A", "label": "Annual"}
    assert definition.dimension_codes == {"REF_AREA": "TR", "FREQ": "A", "UNIT_MEASURE": "TTRY"}
    assert "Türkiye" in definition.name


def test_build_series_definition_rejects_missing_dimension() -> None:
    with pytest.raises(SeriesDefinitionError):
        build_series_definition(_dataset(), {"REF_AREA": "TR", "FREQ": "A"})


def test_build_series_definition_rejects_unknown_dimension() -> None:
    with pytest.raises(SeriesDefinitionError):
        build_series_definition(
            _dataset(), {"REF_AREA": "TR", "FREQ": "A", "UNIT_MEASURE": "TTRY", "SEX": "1"}
        )


def test_build_series_definition_rejects_invalid_code() -> None:
    with pytest.raises(SeriesDefinitionError):
        build_series_definition(_dataset(), {"REF_AREA": "TR", "FREQ": "Q", "UNIT_MEASURE": "TTRY"})


def test_build_series_definition_rejects_removed_code() -> None:
    dataset = _dataset()
    dataset.dimensions[1].codes[0].attributes = {"removed_at": "2026-09-30T00:00:00Z"}
    with pytest.raises(SeriesDefinitionError):
        build_series_definition(dataset, {"REF_AREA": "TR", "FREQ": "A", "UNIT_MEASURE": "TTRY"})


def test_build_series_definition_requires_freq_dimension() -> None:
    dataset = _dataset()
    dataset.dimensions = [dimension for dimension in dataset.dimensions if dimension.code != "FREQ"]
    with pytest.raises(SeriesDefinitionError):
        build_series_definition(dataset, {"REF_AREA": "TR", "UNIT_MEASURE": "TTRY"})


def test_connector_error_requires_a_known_kind() -> None:
    with pytest.raises(ValueError):
        ConnectorError("melted", "boom")
    error = ConnectorError("timeout", "too slow", raw_object_key="sources/x")
    assert error.kind == "timeout"
    assert error.raw_object_key == "sources/x"
    assert error.message == "too slow"
    assert set(ERROR_KINDS) == {
        "not_found",
        "empty",
        "timeout",
        "format_changed",
        "source_error",
        "throttled",
    }


def test_source_connector_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        SourceConnector()  # type: ignore[abstract]


def test_series_meta_defaults_are_independent() -> None:
    first = SeriesMeta(external_code="a", name="a", frequency="monthly")
    second = SeriesMeta(external_code="b", name="b", frequency="annual")
    first.breakdown["x"] = 1
    assert second.breakdown == {}


def test_store_raw_key_shape_and_payload() -> None:
    store = InMemoryObjectStore()
    key = store_raw(
        "tuik",
        "DF_TUFE_SDMX_TT10",
        "databrowser2-csv",
        b"a,b\n1,2\n",
        "csv",
        store=store,
        now=datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC),
    )
    assert re.fullmatch(
        r"sources/tuik/2026/09/30/DF_TUFE_SDMX_TT10/databrowser2-csv-[0-9a-f]{32}\.csv", key
    )
    assert store.objects[key] == b"a,b\n1,2\n"


def test_store_raw_sanitizes_path_segments() -> None:
    store = InMemoryObjectStore()
    key = store_raw("tuik/x", "../catalog", "chan nel", b"{}", "json", store=store)
    assert ".." not in key
    assert key.startswith("sources/tuik_x/")
    assert "/catalog/" in key
    assert "chan_nel-" in key

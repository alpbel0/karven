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
    ensure_series,
    resolve_external_code,
    store_raw,
)
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, DatasetDimension, DimensionCode, Series


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
        "no_connector",
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


def test_preserve_channel_attributes_keeps_other_channels_keys() -> None:
    from app.connectors.base import _preserve_channel_attributes

    portal = {"id": "DF_X+V1.0", "downloadable": True, "seen_at": "2026-10-02"}
    dataset = Dataset(institution_id=1, external_code="DF_X", name="X")
    dataset.attributes = {"channel": "veriportali", "veriportali": portal}
    incoming = {"channel": "databrowser2", "version": "1.0"}
    merged = _preserve_channel_attributes(dataset, incoming)
    assert merged["channel"] == "databrowser2"
    assert merged["veriportali"] == portal
    # The incoming owner of the key always wins (the portal refresh path).
    replaced = _preserve_channel_attributes(dataset, {"veriportali": {"id": "new"}})
    assert replaced["veriportali"] == {"id": "new"}


def test_dataset_upsert_does_not_write_description() -> None:
    from app.connectors.base import _DATASET_FIELDS

    assert "description" not in _DATASET_FIELDS


def test_dataset_upsert_ignores_enrichment_flags() -> None:
    from app.connectors.base import _DATASET_FIELDS

    for flag in (
        "revizyon_tablosu",
        "arsiv",
        "cok_konulu_derleme",
        "donem_serisi",
        "donem_serisi_grubu",
        "mevsim_arindirilmis",
        "para_birimi",
        "nominal_mi",
        "flags_checked_at",
    ):
        assert flag not in _DATASET_FIELDS


def test_merge_dataset_attributes_preserves_enrichment_and_refreshes_source() -> None:
    from app.connectors.base import _merge_dataset_attributes

    dataset = Dataset(institution_id=1, external_code="DF_X", name="X")
    dataset.attributes = {
        "source_description": "old source",
        "period_series_candidate": "tuik::nufus",
        "period_series_rejected": True,
        "measure_enrich_note": "note",
        "dims_pending": True,
        "unrelated": "kept-from-incoming",
    }
    merged = _merge_dataset_attributes(
        dataset, {"channel": "databrowser2", "unrelated": "incoming"}, "new source"
    )
    assert merged["source_description"] == "new source"
    assert merged["period_series_candidate"] == "tuik::nufus"
    assert merged["period_series_rejected"] is True
    assert merged["measure_enrich_note"] == "note"
    assert merged["dims_pending"] is True
    assert merged["channel"] == "databrowser2"
    assert merged["unrelated"] == "incoming"


def test_merge_dataset_attributes_source_description_null_when_source_has_none() -> None:
    from app.connectors.base import _merge_dataset_attributes

    dataset = Dataset(institution_id=1, external_code="DF_X", name="X")
    dataset.attributes = {"source_description": "old source"}
    merged = _merge_dataset_attributes(dataset, {}, None)
    assert merged["source_description"] is None


def test_store_raw_sanitizes_path_segments() -> None:
    store = InMemoryObjectStore()
    key = store_raw("tuik/x", "../catalog", "chan nel", b"{}", "json", store=store)
    assert ".." not in key
    assert key.startswith("sources/tuik_x/")
    assert "/catalog/" in key
    assert "chan_nel-" in key


def _single_dim_dataset() -> Dataset:
    dataset = Dataset(institution_id=1, external_code="bie_dkefkytl", name="Efektif Kurlar")
    dataset.dimensions = [
        DatasetDimension(
            code="SERIE",
            label="Seri",
            position=0,
            role="other",
            codes=[
                DimensionCode(
                    code="TP.DK.USD.A.EF.YTL",
                    label="(USD) ABD Doları (Efektif Alış)",
                    attributes={"frequency": "daily", "unit": "Türk lirası", "aggregation": "avg"},
                )
            ],
        )
    ]
    return dataset


class _SeriesSession:
    """Minimal session double for ``ensure_series`` (no database)."""

    def __init__(self, series: Series | None = None) -> None:
        self._series = series
        self.added: list[object] = []

    def scalar(self, statement: object) -> Series | None:
        return self._series

    def add(self, obj: object) -> None:
        self.added.append(obj)
        self._series = obj  # type: ignore[assignment]

    def flush(self) -> None:
        return None


def test_resolve_external_code_single_dimension_keeps_dots() -> None:
    dataset = _single_dim_dataset()
    assert resolve_external_code(dataset, "bie_dkefkytl:TP.DK.USD.A.EF.YTL") == {
        "SERIE": "TP.DK.USD.A.EF.YTL"
    }
    assert resolve_external_code(dataset, "bie_dkefkytl:") is None
    assert resolve_external_code(dataset, "other:TP.DK.USD.A.EF.YTL") is None


def test_resolve_external_code_multiple_dimensions_still_splits_on_dots() -> None:
    assert resolve_external_code(_dataset(), "DF_X:TR.A.TTRY") == {
        "REF_AREA": "TR",
        "FREQ": "A",
        "UNIT_MEASURE": "TTRY",
    }


def test_build_series_definition_reads_aggregation_attribute() -> None:
    definition = build_series_definition(_single_dim_dataset(), {"SERIE": "TP.DK.USD.A.EF.YTL"})
    assert definition.frequency == "daily"
    assert definition.unit == "Türk lirası"
    assert definition.attributes["aggregation"] == "avg"


def test_ensure_series_copies_aggregation_on_create() -> None:
    session = _SeriesSession()
    series = ensure_series(session, _single_dim_dataset(), {"SERIE": "TP.DK.USD.A.EF.YTL"})
    assert series.attributes["aggregation"] == "avg"


def test_ensure_series_refreshes_aggregation_on_existing_row() -> None:
    existing = Series(
        institution_id=1,
        dataset_id=1,
        external_code="bie_dkefkytl:TP.DK.USD.A.EF.YTL",
        name="old",
        frequency="daily",
        attributes={"dataset_external_code": "bie_dkefkytl", "keep": "me", "aggregation": "last"},
    )
    session = _SeriesSession(existing)
    series = ensure_series(session, _single_dim_dataset(), {"SERIE": "TP.DK.USD.A.EF.YTL"})
    assert series is existing
    assert series.attributes["aggregation"] == "avg"
    assert series.attributes["keep"] == "me"

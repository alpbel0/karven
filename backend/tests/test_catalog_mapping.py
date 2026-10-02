"""Unit tests for catalog link mappings (no database, no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.catalog.mapping import (
    LinkMapping,
    MappingError,
    MonthDimensionPeriod,
    MonthOfYearPeriod,
    normalize_mapping,
    parse_mapping,
    resolve_target,
    set_link_mapping,
    to_storage,
)
from app.data.models import CatalogLink, Dataset


class FakeSession:
    """Minimal session stub: one target dataset and one crosswalk lookup."""

    def __init__(self, dataset: Dataset | None = None, region_code: str | None = None) -> None:
        self.dataset = dataset
        self.region_code = region_code

    def get(self, entity: Any, ident: int) -> Any:
        if entity is Dataset:
            return self.dataset
        return None

    def scalar(self, statement: Any) -> str | None:
        return self.region_code


def _target_dataset() -> Dataset:
    return Dataset(id=10, institution_id=1, external_code="DF_TARGET", name="Target")


def _region_mapping(to_dim: str = "REF_AREA") -> dict[str, Any]:
    return {
        "region": {
            "from_dim": "REF_AREA",
            "to_dim": to_dim,
            "from_scheme": "cip_plate",
            "to_scheme": "ibbs",
            "level": "province",
        }
    }


def _link(to_codes: dict[str, str], mapping: dict[str, Any]) -> CatalogLink:
    return CatalogLink(
        id=1,
        from_dataset_id=1,
        from_codes={"REF_AREA": "6"},
        to_dataset_id=10,
        to_codes=to_codes,
        mapping=mapping,
        relation="same_series",
        method="manual",
        status="accepted",
    )


def test_link_mapping_accepts_all_parts() -> None:
    mapping = LinkMapping.model_validate(
        {
            **_region_mapping(),
            "period": {"rule": "month_of_year", "month": 12},
            "scale": "1000",
        }
    )

    assert mapping.region is not None
    assert mapping.region.level == "province"
    assert isinstance(mapping.period, MonthOfYearPeriod)
    assert mapping.period.month == 12
    assert mapping.scale == Decimal("1000")


def test_link_mapping_defaults_level_to_province() -> None:
    mapping = LinkMapping.model_validate(
        {
            "region": {
                "from_dim": "REF_AREA",
                "to_dim": "IKAMET_YERI",
                "from_scheme": "cip_plate",
                "to_scheme": "ibbs",
            }
        }
    )
    assert mapping.region is not None
    assert mapping.region.level == "province"


def test_link_mapping_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        LinkMapping.model_validate({"unknown": {}})
    with pytest.raises(ValidationError):
        LinkMapping.model_validate(
            {
                "region": {
                    "from_dim": "REF_AREA",
                    "to_dim": "REF_AREA",
                    "from_scheme": "cip_plate",
                    "to_scheme": "ibbs",
                    "extra": "x",
                }
            }
        )


@pytest.mark.parametrize("bad", ["0", "-1", "-1000"])
def test_link_mapping_rejects_non_positive_scale(bad: str) -> None:
    with pytest.raises(ValidationError):
        LinkMapping.model_validate({"scale": bad})


def test_link_mapping_rejects_float_scale() -> None:
    with pytest.raises(ValidationError):
        LinkMapping.model_validate({"scale": 1000.5})


def test_link_mapping_rejects_bad_month() -> None:
    with pytest.raises(ValidationError):
        LinkMapping.model_validate({"period": {"rule": "month_of_year", "month": 13}})


def test_link_mapping_rejects_bad_month_dimension_format() -> None:
    with pytest.raises(ValidationError):
        LinkMapping.model_validate(
            {"period": {"rule": "month_dimension", "dim": "AY", "format": "X"}}
        )


def test_scale_stored_as_string_and_round_trips() -> None:
    mapping = LinkMapping.model_validate({"scale": "1000"})

    assert to_storage(mapping) == {"scale": "1000"}
    assert parse_mapping(to_storage(mapping)) == mapping
    assert normalize_mapping({"scale": "1000"}) == {"scale": "1000"}


def test_parse_mapping_reports_invalid_json_clearly() -> None:
    with pytest.raises(MappingError):
        parse_mapping({"scale": "not-a-number"})


def test_parse_mapping_treats_none_and_empty_as_no_mapping() -> None:
    assert parse_mapping(None) == LinkMapping()
    assert parse_mapping({}) == LinkMapping()


def test_resolve_target_translates_region_to_ref_area() -> None:
    link = _link({"FREQ": "M"}, _region_mapping())
    session = FakeSession(_target_dataset(), region_code="TR510")

    key = resolve_target(session, link, {"REF_AREA": "6"}, date(2025, 1, 1))

    assert key.dataset_code == "DF_TARGET"
    assert key.codes == {"FREQ": "M", "REF_AREA": "TR510"}
    assert key.period == date(2025, 1, 1)
    assert key.scale == Decimal(1)


def test_resolve_target_translates_region_to_ikamet_yeri() -> None:
    link = _link({"REF_AREA": "TR"}, _region_mapping(to_dim="IKAMET_YERI"))
    session = FakeSession(_target_dataset(), region_code="TR510")

    key = resolve_target(session, link, {"REF_AREA": "6"}, date(2025, 1, 1))

    assert key.codes == {"REF_AREA": "TR", "IKAMET_YERI": "TR510"}


def test_resolve_target_month_of_year_maps_annual_to_december() -> None:
    link = _link({}, {"period": {"rule": "month_of_year", "month": 12}})

    key = resolve_target(FakeSession(_target_dataset()), link, {}, date(2025, 1, 1))

    assert key.period == date(2025, 12, 1)


def test_resolve_target_month_dimension_takes_the_month() -> None:
    link = _link({}, {"period": {"rule": "month_dimension", "dim": "AY", "format": "M%02d"}})

    key = resolve_target(FakeSession(_target_dataset()), link, {}, date(2026, 8, 1))

    assert key.codes["AY"] == "M08"
    assert key.period == date(2026, 8, 1)
    assert isinstance(parse_mapping(link.mapping).period, MonthDimensionPeriod)


def test_resolve_target_applies_scale() -> None:
    link = _link({}, {"scale": "1000"})

    key = resolve_target(FakeSession(_target_dataset()), link, {}, date(2025, 1, 1))

    assert key.scale == Decimal("1000")


def test_resolve_target_missing_crosswalk_code_raises() -> None:
    link = _link({}, _region_mapping())

    with pytest.raises(MappingError):
        resolve_target(
            FakeSession(_target_dataset(), region_code=None),
            link,
            {"REF_AREA": "6"},
            date(2025, 1, 1),
        )


def test_resolve_target_missing_source_region_raises() -> None:
    link = _link({}, _region_mapping())

    with pytest.raises(MappingError):
        resolve_target(
            FakeSession(_target_dataset(), region_code="TR510"), link, {}, date(2025, 1, 1)
        )


def test_resolve_target_invalid_mapping_raises() -> None:
    link = _link({}, {"scale": "-1"})

    with pytest.raises(MappingError):
        resolve_target(FakeSession(_target_dataset()), link, {}, date(2025, 1, 1))


def test_resolve_target_missing_dataset_raises() -> None:
    link = _link({}, {})

    with pytest.raises(MappingError):
        resolve_target(FakeSession(None), link, {}, date(2025, 1, 1))


def test_set_link_mapping_normalizes_and_rejects_invalid() -> None:
    link = _link({}, {})

    mapping = set_link_mapping(link, {"scale": "1000"})

    assert link.mapping == {"scale": "1000"}
    assert mapping.scale == Decimal("1000")
    with pytest.raises(MappingError):
        set_link_mapping(link, {"scale": "-1"})
    assert link.mapping == {"scale": "1000"}

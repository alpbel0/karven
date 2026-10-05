"""Unit tests for the ``data_status`` agent tool (Task 2.5, no database)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.dialects import postgresql

from app.catalog.status import (
    STATUS_TOOL_LIMIT,
    allowed_transforms,
    build_status_tool,
    data_status,
    loaded_ranges,
    loaded_ranges_query,
)


class _NoDatabase:
    """A session that fails the test if anything queries it."""

    def execute(self, statement):  # noqa: ANN001, ANN201 - test double
        raise AssertionError("the database must not be touched")

    def scalars(self, statement):  # noqa: ANN001, ANN201 - test double
        raise AssertionError("the database must not be touched")


def _measure(measure_type: str | None, *, kumulatif: bool = False) -> dict:
    return {"measure_type": measure_type, "kumulatif": kumulatif}


def test_ranges_query_is_one_grouped_statement_without_values() -> None:
    sql = str(loaded_ranges_query([1, 2]).compile(dialect=postgresql.dialect())).lower()
    assert "group by" in sql
    assert sql.count("select") == 1
    assert "min(observations.period)" in sql
    assert "count(distinct observations.period)" in sql
    assert "value" not in sql


def test_loaded_ranges_empty_short_circuits() -> None:
    assert loaded_ranges(_NoDatabase(), []) == {}


@pytest.mark.parametrize(
    ("measure_type", "frequency", "expected"),
    [
        ("endeks", "monthly", ["level", "percent_change_period", "percent_change_annual"]),
        ("akim_tutar", "quarterly", ["level", "percent_change_period", "percent_change_annual"]),
        ("fiyat_kur", "daily", ["level", "percent_change_period"]),
        ("stok_adet", "annual", ["level", "percent_change_period"]),
        ("akim_adet", "biennial", ["level"]),
        ("akim_adet", "irregular", ["level"]),
        ("yillik_yuzde_degisim", "monthly", ["level"]),
        ("oran_pay", "monthly", ["level"]),
        ("dagilim_istatistigi", "annual", ["level"]),
    ],
)
def test_allowed_transforms_by_measure_and_frequency(
    measure_type: str, frequency: str, expected: list[str]
) -> None:
    assert allowed_transforms(_measure(measure_type), frequency) == expected


def test_cumulative_series_is_level_only() -> None:
    assert allowed_transforms(_measure("akim_tutar", kumulatif=True), "monthly") == ["level"]


def test_unknown_measure_gives_none_not_a_guess() -> None:
    assert allowed_transforms(None, "monthly") is None
    assert allowed_transforms({}, "monthly") is None
    assert allowed_transforms(_measure(None), "monthly") is None


def test_malformed_recipes_are_reported_without_touching_the_database() -> None:
    result = data_status(
        _NoDatabase(),
        [
            "not an object",
            {"institution": "tuik", "dataset": "X"},
            {"institution": "tuik", "dataset": "X", "codes": {"A": 1}},
        ],
    )
    assert [row["status"] for row in result["results"]] == ["invalid_request"] * 3


def test_more_than_the_limit_is_skipped_with_a_note() -> None:
    result = data_status(_NoDatabase(), ["bad"] * (STATUS_TOOL_LIMIT + 2))
    assert len(result["results"]) == STATUS_TOOL_LIMIT
    assert result["skipped"] == 2
    assert "skipped" in result["note"]


def test_tool_schema_and_bad_input() -> None:
    schema, function = build_status_tool(lambda: None)["data_status"]
    assert schema["required"] == ["recipes"]
    assert schema["properties"]["recipes"]["maxItems"] == STATUS_TOOL_LIMIT
    assert function([])["status"] == "invalid_request"
    assert function("x")["status"] == "invalid_request"  # type: ignore[arg-type]
    json.dumps(schema)

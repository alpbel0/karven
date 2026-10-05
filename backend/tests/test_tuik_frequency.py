"""Unit tests for the hidden-FREQ frequency resolution (Task 2.4b; no network)."""

from __future__ import annotations

from typing import Any

import pytest

from app.connectors.base import FORMAT_CHANGED, SOURCE_ERROR, ConnectorError
from app.connectors.tuik import frequency as freq


class _Client:
    """Fake codelist client returning a canned payload or raising."""

    def __init__(self, payload: Any = None, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[tuple[str, str, Any]] = []

    def dataset_partial_codelist(
        self, dataset_id: str, dimension: str, criteria: list[dict[str, Any]] | None = None
    ) -> Any:
        self.calls.append((dataset_id, dimension, criteria))
        if self._error is not None:
            raise self._error
        return _Response(self._payload)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _criterion(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"criteria": [{"id": "FREQ", "label": "CL_SIKLIK", "values": values}], "obsCount": 1}


def test_resolve_single_monthly() -> None:
    payload = _criterion([{"id": "M", "name": "Monthly", "isDefault": False, "isSelectable": True}])
    result = freq.resolve_frequency(payload)
    assert result.status == freq.SINGLE
    assert result.code == "M"
    assert result.frequency == "monthly"
    assert result.codes == [{"id": "M", "name": "Monthly"}]


def test_resolve_single_biennial_maps_to_biennial() -> None:
    payload = _criterion([{"id": "A2", "name": "Biennial", "isSelectable": True}])
    result = freq.resolve_frequency(payload)
    assert result.status == freq.SINGLE
    assert result.code == "A2"
    assert result.frequency == "biennial"


def test_resolve_multiple_keeps_every_code() -> None:
    payload = _criterion(
        [
            {"id": "M", "name": "Monthly", "isSelectable": True},
            {"id": "A", "name": "Annual", "isSelectable": True},
        ]
    )
    result = freq.resolve_frequency(payload)
    assert result.status == freq.MULTIPLE
    assert [code["id"] for code in result.codes] == ["M", "A"]
    assert result.frequency is None
    assert result.code is None


def test_resolve_empty_when_criterion_missing() -> None:
    result = freq.resolve_frequency({"criteria": [{"id": "OTHER", "values": []}]})
    assert result.status == freq.EMPTY
    assert "criterion missing" in (result.reason or "")


def test_resolve_empty_when_values_key_absent() -> None:
    result = freq.resolve_frequency({"criteria": [{"id": "FREQ"}]})
    assert result.status == freq.EMPTY
    assert result.reason == "values empty"


def test_resolve_empty_when_values_list_empty() -> None:
    result = freq.resolve_frequency(_criterion([]))
    assert result.status == freq.EMPTY
    assert result.reason == "values empty"


def test_resolve_empty_when_no_selectable_codes() -> None:
    payload = _criterion([{"id": "M", "name": "Monthly", "isSelectable": False}])
    result = freq.resolve_frequency(payload)
    assert result.status == freq.EMPTY
    assert result.reason == "no selectable codes"


def test_resolve_unknown_code() -> None:
    payload = _criterion([{"id": "X", "name": "Bogus", "isSelectable": True}])
    result = freq.resolve_frequency(payload)
    assert result.status == freq.UNKNOWN_CODE
    assert result.code == "X"
    assert "X" in (result.reason or "")


def test_resolve_query_error_from_connector_error() -> None:
    error = ConnectorError(SOURCE_ERROR, "boom")
    result = freq.resolve_frequency(error)
    assert result.status == freq.QUERY_ERROR
    assert "boom" in (result.reason or "")


def test_resolve_programming_error_is_not_a_query_error() -> None:
    with pytest.raises(TypeError):
        freq.resolve_frequency(TypeError("a bug, not a source failure"))


def test_as_attributes_single_has_default_frequency_and_source() -> None:
    resolution = freq.FrequencyResolution(
        status=freq.SINGLE,
        codes=[{"id": "M", "name": "Monthly"}],
        code="M",
        frequency="monthly",
    )
    attributes = freq.as_attributes(resolution, checked_at="2026-10-05T00:00:00+00:00")
    assert attributes["frequency_resolution"] == {
        "status": "single",
        "codes": [{"id": "M", "name": "Monthly"}],
        "reason": None,
        "checked_at": "2026-10-05T00:00:00+00:00",
    }
    assert attributes["default_frequency"] == "monthly"
    assert attributes["frequency_source"] == {
        "code": "M",
        "name": "Monthly",
        "origin": "hidden_freq_codelist",
        "checked_at": "2026-10-05T00:00:00+00:00",
    }


@pytest.mark.parametrize("status", ["multiple", "empty", "unknown_code", "query_error"])
def test_as_attributes_non_single_has_no_default_frequency(status: str) -> None:
    attributes = freq.as_attributes(
        freq.FrequencyResolution(status=status, reason="r"), checked_at="t"
    )
    assert attributes["frequency_resolution"]["status"] == status
    assert "default_frequency" not in attributes
    assert "frequency_source" not in attributes


def test_resolve_for_dataset_calls_the_freq_codelist() -> None:
    client = _Client(_criterion([{"id": "M", "name": "Monthly", "isSelectable": True}]))
    result = freq.resolve_for_dataset(client, "TR,DF_X,1.0")
    assert result.status == freq.SINGLE
    assert result.frequency == "monthly"
    assert client.calls == [("TR,DF_X,1.0", "FREQ", None)]


def test_resolve_for_dataset_wraps_connector_error() -> None:
    client = _Client(error=ConnectorError(FORMAT_CHANGED, "bad json"))
    result = freq.resolve_for_dataset(client, "TR,DF_X,1.0")
    assert result.status == freq.QUERY_ERROR
    assert "bad json" in (result.reason or "")


def test_resolve_for_dataset_programming_error_propagates() -> None:
    client = _Client(error=KeyError("bug"))
    with pytest.raises(KeyError):
        freq.resolve_for_dataset(client, "TR,DF_X,1.0")

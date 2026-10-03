"""Unit tests for the on-demand connector registry (no live connectors)."""

from __future__ import annotations

import pytest

import app.fetching.connectors as connectors
from app.connectors.base import NO_CONNECTOR, ConnectorError
from tests.fetch_fakes import Store, make_dataset, make_institution


@pytest.mark.parametrize(
    "institution_code,source",
    [("tcmb", "tcmb"), ("hmb", "hmb"), ("tuik", "tuik-evds")],
)
def test_evds3_source_mapping(monkeypatch, institution_code, source) -> None:
    store = Store()
    institution = make_institution(store, code=institution_code)
    dataset = make_dataset(store, institution, channel="evds3")
    captured: dict = {}
    monkeypatch.setattr(connectors, "MinioObjectStore", lambda *args, **kwargs: "STORE")

    def fake_build(source_key, *, store):
        captured["source"] = source_key
        captured["store"] = store
        return "CONNECTOR"

    monkeypatch.setattr(connectors, "build_connector", fake_build)

    connector, channel_kwargs = connectors.connector_for(institution, dataset)

    assert connector == "CONNECTOR"
    assert channel_kwargs == {}
    assert captured == {"source": source, "store": "STORE"}


def test_databrowser2_builds_the_tuik_connector(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store, code="tuik")
    dataset = make_dataset(store, institution, channel="databrowser2")
    monkeypatch.setattr(connectors, "MinioObjectStore", lambda *args, **kwargs: "STORE")
    monkeypatch.setattr(connectors, "_build_tuik", lambda store: ("TUIK", store))

    connector, channel_kwargs = connectors.connector_for(institution, dataset)

    assert connector == ("TUIK", "STORE")
    assert channel_kwargs == {}


def test_an_unsupported_channel_fails_with_a_typed_kind() -> None:
    store = Store()
    institution = make_institution(store, code="tuik")
    dataset = make_dataset(store, institution, channel="bi-trade")

    with pytest.raises(ConnectorError) as excinfo:
        connectors.connector_for(institution, dataset)

    assert excinfo.value.kind == NO_CONNECTOR
    assert excinfo.value.message == (
        "no on-demand connector for dataset DS_TEST (channel bi-trade)"
    )


def test_a_missing_channel_fails_with_a_typed_kind() -> None:
    store = Store()
    institution = make_institution(store, code="tuik")
    dataset = make_dataset(store, institution, channel=None)

    with pytest.raises(ConnectorError) as excinfo:
        connectors.connector_for(institution, dataset)

    assert excinfo.value.kind == NO_CONNECTOR
    assert excinfo.value.message == "no on-demand connector for dataset DS_TEST (channel None)"


def test_a_known_channel_with_an_unknown_institution_fails() -> None:
    store = Store()
    institution = make_institution(store, code="manual")
    dataset = make_dataset(store, institution, channel="evds3")

    with pytest.raises(ConnectorError) as excinfo:
        connectors.connector_for(institution, dataset)

    assert excinfo.value.kind == NO_CONNECTOR
    assert excinfo.value.message == "no on-demand connector for dataset DS_TEST (channel evds3)"

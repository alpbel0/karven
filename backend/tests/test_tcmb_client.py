"""Unit tests for the TCMB EVDS3 HTTP client (MockTransport, no network)."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.connectors.base import (
    FORMAT_CHANGED,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    InMemoryObjectStore,
)
from app.connectors.tcmb.client import EvdsClient
from app.connectors.tcmb.connector import TcmbConnector
from app.connectors.tcmb.parsers import SerieInfo


def _settings(**overrides: Any) -> Settings:
    base = {
        "tcmb_request_interval_s": 0.0,
        "tcmb_request_timeout_s": 1.0,
        "tcmb_max_retries": 1,
        "tcmb_retry_backoff_s": 1.0,
    }
    base.update(overrides)
    return Settings(**base)


class FakeTime:
    """Deterministic monotonic clock; sleep advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(
    handler,
    *,
    store=None,
    settings_obj: Settings | None = None,
    time: FakeTime | None = None,
) -> EvdsClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    clock = time.clock if time is not None else None
    sleeper = time.sleep if time is not None else None
    kwargs: dict[str, Any] = {"http_client": http_client, "store": store}
    if settings_obj is not None:
        kwargs["settings_obj"] = settings_obj
    if time is not None:
        kwargs["clock"] = clock
        kwargs["sleeper"] = sleeper
    return EvdsClient(**kwargs)


def _json_handler(payload: Any, *, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers)

    return handler


# --- request shape ---------------------------------------------------------


def test_get_data_posts_the_exact_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"items": []})

    client = _client(handler)
    serie = SerieInfo(
        code="TP.DK.USD.A.EF.YTL",
        group_code="bie_dkefkytl",
        name="x",
        name_en=None,
        frequency="daily",
        source_frequency="GÜNLÜK",
        aggregation="avg",
    )
    body = TcmbConnector._data_body(serie, serie.code, "1", date(2000, 1, 1), date(2026, 10, 5))
    assert body["groupSeperator"] is False
    assert body["decimal"] == "10"
    client.get_data(body)

    request = captured[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/fe")
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    sent = json.loads(request.content)
    assert sent == body
    assert sent["groupSeperator"] is False
    assert sent["decimal"] == "10"


def test_get_bounds_posts_the_dummy_frequency() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    client.get_bounds("TP.DK.USD.A.EF.YTL")
    sent = json.loads(captured[0].content)
    assert sent == {"frequency": 1, "series": ["TP.DK.USD.A.EF.YTL"], "datagroups": [None]}


# --- pacing ----------------------------------------------------------------


def test_requests_are_paced() -> None:
    time = FakeTime()
    client = _client(
        _json_handler([], headers={}),
        settings_obj=_settings(tcmb_request_interval_s=0.25),
        time=time,
    )
    client.get_catalog()
    client.get_catalog()
    assert time.sleeps == [0.25]


# --- raw storage -----------------------------------------------------------


def test_success_payloads_are_stored_with_channel_keys() -> None:
    store = InMemoryObjectStore()
    client = _client(_json_handler([], headers={}), store=store, settings_obj=_settings())
    client.get_catalog()
    client.get_serie_list("bie_cli2")
    client.get_bounds("X")
    client.get_data({"series": "X"})
    client.get_bounds("TP.CLI2.A01", group="bie_cli2")
    client.get_data({"series": "TP.CLI2.A01"}, group="bie_cli2")
    keys = sorted(store.objects)
    assert any(
        re.fullmatch(r"sources/tcmb/\d{4}/\d{2}/\d{2}/catalog/evds3-catalog-[0-9a-f]{32}\.json", k)
        for k in keys
    )
    assert any("/bie_cli2/evds3-serielist-" in k for k in keys)
    # store_raw sanitizes the "_series" segment to "series".
    assert any("/series/evds3-bounds-" in k for k in keys)
    # With a group the raw payload lands in that group's folder.
    assert any("/bie_cli2/evds3-bounds-" in k for k in keys)
    assert any("/bie_cli2/evds3-data-" in k for k in keys)
    assert any("/series/evds3-data-" in k for k in keys)


class _FailingStore:
    def put(self, key, payload, *, content_type="application/octet-stream"):  # noqa: ANN001
        raise RuntimeError("minio down")


def test_storage_failure_does_not_break_the_data_path() -> None:
    client = _client(_json_handler([], headers={}), store=_FailingStore(), settings_obj=_settings())
    response = client.get_catalog()
    assert response.raw_object_key is None
    assert response.json() == []


# --- failure mapping -------------------------------------------------------


def test_http_500_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            500, content=b"<html>bad</html>", headers={"content-type": "text/html"}
        )

    store = InMemoryObjectStore()
    client = _client(handler, store=store, settings_obj=_settings(tcmb_max_retries=3))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_data({"series": "X"})
    assert excinfo.value.kind == SOURCE_ERROR
    assert len(calls) == 1
    assert client.requests == 1
    assert excinfo.value.raw_object_key is not None
    assert "evds3-data-error" in excinfo.value.raw_object_key
    assert store.objects[excinfo.value.raw_object_key] == b"<html>bad</html>"


def test_http_400_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            400, content=b"<html>missing</html>", headers={"content-type": "text/html"}
        )

    client = _client(handler, settings_obj=_settings(tcmb_max_retries=3))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == SOURCE_ERROR
    assert len(calls) == 1


def test_throttle_honours_numeric_retry_after() -> None:
    time = FakeTime()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, content=b"slow")
        return httpx.Response(200, json=[])

    client = _client(handler, settings_obj=_settings(), time=time)
    assert client.get_catalog().json() == []
    assert 2.0 in time.sleeps
    assert client.throttled == 1
    assert client.retries == 1
    assert len(calls) == 2


def test_throttle_exhausted_is_throttled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow")

    client = _client(handler, settings_obj=_settings(tcmb_max_retries=1))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == THROTTLED
    assert client.throttled == 2


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_are_retried_then_source_error(status: int) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            status, content=b"<html>down</html>", headers={"content-type": "text/html"}
        )

    client = _client(handler, settings_obj=_settings(tcmb_max_retries=1))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == SOURCE_ERROR
    assert len(calls) == 2
    assert client.retries == 1


def test_timeout_is_retried_then_timeout() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("boom")

    client = _client(handler, settings_obj=_settings(tcmb_max_retries=1))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == TIMEOUT
    assert len(calls) == 2


def test_transport_error_is_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _client(handler, settings_obj=_settings(tcmb_max_retries=0))
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == TIMEOUT


def test_other_non_200_is_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")

    client = _client(handler, settings_obj=_settings())
    with pytest.raises(ConnectorError) as excinfo:
        client.get_catalog()
    assert excinfo.value.kind == SOURCE_ERROR


def test_200_invalid_json_is_format_changed_with_raw_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )

    store = InMemoryObjectStore()
    client = _client(handler, store=store, settings_obj=_settings())
    response = client.get_catalog()
    with pytest.raises(ConnectorError) as excinfo:
        response.json()
    assert excinfo.value.kind == FORMAT_CHANGED
    assert excinfo.value.raw_object_key == response.raw_object_key
    assert response.raw_object_key in store.objects

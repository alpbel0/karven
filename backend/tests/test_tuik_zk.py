"""Unit tests for the legacy ZK AU engine (no network; captured fixtures)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.connectors.base import SOURCE_ERROR, ConnectorError
from app.connectors.tuik.zk import (
    LegacyZkClient,
    SessionExpired,
    parse_report_grid,
    parse_turkish_number,
    resolve_grid,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "turizm"
BOOTSTRAP = (FIXTURES / "sinir_bootstrap.html").read_bytes()
YEAR_LIST = (FIXTURES / "year_list.au.txt").read_bytes()
GATE_LIST = (FIXTURES / "gate_list.au.txt").read_bytes()
ERROR_MODAL = (FIXTURES / "error_modal.au.txt").read_bytes()
REDIRECT = (FIXTURES / "redirect.au.txt").read_bytes()
REPORT = (FIXTURES / "sinir_nationality.report.html").read_bytes()


def _client(response_for, *, sleeps: list[float] | None = None) -> LegacyZkClient:
    transport = httpx.MockTransport(response_for)
    return LegacyZkClient(
        "sinir.zul",
        store=None,
        http_client=httpx.Client(transport=transport),
        sleeper=(sleeps.append if sleeps is not None else (lambda _s: None)),
        clock=lambda: 0.0,
    )


def test_bootstrap_builds_component_maps() -> None:
    client = _client(lambda _request: httpx.Response(200, content=BOOTSTRAP))
    try:
        client.bootstrap()
        assert {"Yabancı", "Vatandaş", "Aylık", "Giriş", "Milliyet-Kapı seçimi"} <= set(
            client.radios
        )
        assert "Raporu Oluştur" in client.buttons
        assert {"Havayolu", "Demiryolu", "Karayolu"} <= set(client.checkboxes)
        assert len(client.listboxes) == 8
    finally:
        client.close()


def test_event_body_shape_and_pacing() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=BOOTSTRAP)
        return httpx.Response(200, content=REDIRECT)

    client = _client(handler, sleeps=sleeps)
    try:
        client.bootstrap()
        radio = client.radios["Yabancı"]
        client.check_radio("Yabancı")
        client.select_items("box-1", ["item-1", "item-2"])
        client.click_button("Raporu Oluştur")
    finally:
        client.close()

    bodies = [request.content.decode() for request in requests if request.method == "POST"]
    assert f"cmd.0=onCheck&uuid.0={radio.id}&data.0=true" in bodies[0]
    assert "cmd.0=onSelect&uuid.0=box-1&data.0=item-1&data.0=item-2" in bodies[1]
    assert bodies[2].endswith(
        "cmd.0=onClick&uuid.0="
        + client.buttons["Raporu Oluştur"].id
        + "&data.0=49&data.0=10&data.0="
    )
    assert all(request.headers["referer"].endswith("sinir.zul") for request in requests)
    # Every request after the first waits out the pause.
    assert len(sleeps) >= 3
    assert all(0 < wait <= client.pause_s for wait in sleeps)


def test_parse_outer_list_header_and_items() -> None:
    client = _client(lambda _request: httpx.Response(200, content=YEAR_LIST))
    try:
        response = client._parse_response(YEAR_LIST.decode())  # noqa: SLF001 - pure parser
        assert len(response.outer) == 1
        outer = response.outer[0]
        assert outer.header == "Yıl Seçimi"
        assert len(outer.items) == 49
        assert outer.items[0].label == "2025"
        assert outer.items[0].id
        assert response.errors == ()
        assert response.redirect is None
    finally:
        client.close()


def test_parse_error_modal_surfaces_hatali() -> None:
    client = _client(lambda _request: httpx.Response(200, content=ERROR_MODAL))
    try:
        response = client._parse_response(ERROR_MODAL.decode())  # noqa: SLF001
        assert response.errors
        assert "seçiniz" in response.errors[0]
        assert response.redirect is None
    finally:
        client.close()


def test_parse_redirect_url() -> None:
    subset = REDIRECT.decode()
    client = _client(lambda _request: httpx.Response(200, content=REDIRECT))
    try:
        response = client._parse_response(subset)  # noqa: SLF001
        assert response.redirect is not None
        assert response.redirect.startswith("http://rapory.tuik.gov.tr/")
    finally:
        client.close()


def test_generate_report_raises_on_a_validation_modal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=BOOTSTRAP)
        return httpx.Response(200, content=ERROR_MODAL)

    client = _client(handler)
    try:
        client.bootstrap()
        with pytest.raises(ConnectorError) as excinfo:
            client.generate_report()
        assert excinfo.value.kind == SOURCE_ERROR
    finally:
        client.close()


def test_dead_session_raises_session_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=BOOTSTRAP)
        return httpx.Response(200, content=b"<html>login</html>")

    client = _client(handler)
    try:
        client.bootstrap()
        with pytest.raises(SessionExpired):
            client.check_radio("Yabancı")
    finally:
        client.close()


def test_gate_list_fixture_parses_three_hundred_style_items() -> None:
    response_text = GATE_LIST.decode()
    client = _client(lambda _request: httpx.Response(200, content=GATE_LIST))
    try:
        response = client._parse_response(response_text)  # noqa: SLF001
        outer = response.outer[0]
        assert outer.header == "Kapı Seçimi"
        assert len(outer.items) == 101
        assert outer.items[0].label.startswith("<<")
    finally:
        client.close()


# --- report grid ----------------------------------------------------------


def test_parse_turkish_number() -> None:
    assert parse_turkish_number("52.775.055") == Decimal("52775055")
    assert parse_turkish_number("1.234") == Decimal("1234")
    assert parse_turkish_number("1,23") == Decimal("1.23")
    assert parse_turkish_number("") is None
    assert parse_turkish_number("-") is None


def test_resolve_grid_expands_colspan_and_rowspan() -> None:
    rows = [
        [("A", 1, 2), ("B", 2, 1)],
        [("C", 1, 1)],
    ]
    grid = resolve_grid(rows)
    assert grid == [["A", "B", None], [None, "C", None]]


def test_parse_report_grid_resolves_and_cleans() -> None:
    grid = parse_report_grid(REPORT)
    assert grid.encoding == "windows-1254"
    assert "giriş yapan yabancı" in grid.title
    header = next(row for row in grid.rows if "Ocak" in row)
    assert header.count("Ocak") == 1
    assert "Toplam" in header
    assert any(
        row[0] == "Türkiye" and parse_turkish_number(row[4]) == Decimal("52775055")
        for row in grid.rows
    )


def test_parse_report_grid_missing_year_is_empty() -> None:
    grid = parse_report_grid(b"<html><table><tr><td>empty</td></tr></table></html>")
    assert grid.rows == [["empty"]]


def test_list_period_year_fixture() -> None:
    # A tiny guard that the real year fixture starts at 2025 (value-check anchor).
    response = LegacyZkClient._parse_response(YEAR_LIST.decode())  # noqa: SLF001
    years = [item.label for item in response.outer[0].items if item.label.isdigit()]
    assert years == sorted(years, reverse=True)
    assert date(2025, 1, 1) <= date(int(years[0]), 1, 1)

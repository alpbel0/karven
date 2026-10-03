"""Unit tests for the TCMB connector (fake client + fixtures, no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.connectors.base import EMPTY, FORMAT_CHANGED, NOT_FOUND, TIMEOUT, ConnectorError
from app.connectors.tcmb.connector import TcmbConnector

FIXTURES = Path(__file__).parent / "fixtures" / "tcmb"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CATALOG = _load("catalog.raw.json")
CLI2 = _load("serielist-bie_cli2.raw.json")
DKEF = _load("serielist-bie_dkefkytl.raw.json")
DBDIS = _load("serielist-bie_dbdisborc.raw.json")
BOUNDS_USD = _load("bounds-usd.raw.json")
BOUNDS_CLI2 = _load("bounds-cli2.raw.json")
BOUNDS_EMPTY = _load("bounds-empty.raw.json")
FE_USD = _load("fe-usd.raw.json")


class _Resp:
    def __init__(self, payload, key: str) -> None:
        self._payload = payload
        self.raw_object_key = key

    def json(self):
        return self._payload


class FakeClient:
    """Duck-typed EvdsClient serving captured fixtures."""

    def __init__(
        self,
        *,
        catalog=None,
        serie_lists=None,
        bounds=None,
        fe=None,
    ) -> None:
        self.catalog = CATALOG if catalog is None else catalog
        self.serie_lists = (
            {"bie_cli2": CLI2, "bie_dkefkytl": DKEF, "bie_dbdisborc": DBDIS}
            if serie_lists is None
            else serie_lists
        )
        self.bounds = (
            {"TP.DK.USD.A.EF.YTL": BOUNDS_USD, "TP.CLI2.A01": BOUNDS_CLI2}
            if bounds is None
            else bounds
        )
        self.fe = {"TP.DK.USD.A.EF.YTL": FE_USD} if fe is None else fe
        self.data_bodies: list[dict] = []
        self.closed = False

    def get_catalog(self):
        return _Resp(self.catalog, "key-catalog")

    def get_serie_list(self, group: str):
        return _Resp(self.serie_lists.get(group, []), f"key-serielist-{group}")

    def get_bounds(self, serie_code: str, **_kwargs):
        return _Resp(self.bounds[serie_code], f"key-bounds-{serie_code}")

    def get_data(self, body: dict, **_kwargs):
        self.data_bodies.append(body)
        return _Resp(self.fe[body["series"]], f"key-fe-{body['series']}")

    def close(self) -> None:
        self.closed = True


def _connector(**kwargs) -> TcmbConnector:
    return TcmbConnector(client=FakeClient(**kwargs))


# --- catalog ---------------------------------------------------------------


def test_list_datasets_shape() -> None:
    connector = _connector()
    metas = {meta.external_code: meta for meta in connector.list_datasets()}
    assert set(metas) == {"bie_cli2", "bie_dkefkytl"}

    cli2 = metas["bie_cli2"]
    assert cli2.name == "Bileşik Öncü Göstergeler Endeksi"
    assert cli2.source_category == "BİLEŞİK ÖNCÜ GÖSTERGELER ENDEKSİ (TCMB)"
    assert cli2.attributes["channel"] == "evds3"
    assert cli2.attributes["source_frequency"] == "AYLIK"
    assert "unit" not in cli2.attributes
    (dimension,) = cli2.dimensions
    assert dimension.code == "SERIE"
    assert dimension.label == "Seri"
    assert dimension.position == 0
    assert dimension.role == "other"
    assert [code.code for code in dimension.codes] == [
        "TP.CLI2.A01",
        "TP.CLI2.A02",
        "TP.CLI2.A03",
    ]
    code = dimension.codes[0]
    assert code.label == "MBONCU_SUE (Trend Kapsayan)"
    assert code.parent_code is None
    assert code.attributes["frequency"] == "monthly"
    assert code.attributes["aggregation"] == "avg"
    assert code.attributes["aggregations"] == ["avg", "first", "last", "max", "min"]
    assert code.attributes["source_frequency"] == "AYLIK"
    assert code.attributes["level"] == 1

    usd = next(
        c for c in metas["bie_dkefkytl"].dimensions[0].codes if c.code == "TP.DK.USD.A.EF.YTL"
    )
    assert usd.attributes["unit"] == "Türk lirası"
    assert usd.attributes["frequency"] == "daily"


def test_list_datasets_records_failed_groups_instead_of_skipping_silently() -> None:
    class _FailingClient(FakeClient):
        def get_serie_list(self, group: str):
            if group == "bie_cli2":
                raise ConnectorError(TIMEOUT, "boom")
            return super().get_serie_list(group)

    connector = TcmbConnector(client=_FailingClient())
    metas = list(connector.list_datasets())
    assert {meta.external_code for meta in metas} == {"bie_dkefkytl"}
    assert connector.failed_groups == [("bie_cli2", TIMEOUT, "boom")]


def test_list_datasets_empty_group_yields_no_series_flag() -> None:
    connector = _connector(serie_lists={"bie_cli2": [], "bie_dkefkytl": DKEF})
    metas = {meta.external_code: meta for meta in connector.list_datasets()}
    assert metas["bie_cli2"].attributes["no_series"] is True
    assert metas["bie_cli2"].dimensions[0].codes == []
    assert "no_series" not in metas["bie_dkefkytl"].attributes


def test_list_datasets_only_group_and_limit() -> None:
    only = _connector(serie_lists={"bie_cli2": CLI2, "bie_dkefkytl": DKEF})
    only._only_group = "bie_dkefkytl"
    assert [meta.external_code for meta in only.list_datasets()] == ["bie_dkefkytl"]

    limited = TcmbConnector(client=FakeClient(), limit=1)
    assert [meta.external_code for meta in limited.list_datasets()] == ["bie_cli2"]


def test_list_datasets_carries_category_path() -> None:
    metas = {meta.external_code: meta for meta in _connector().list_datasets()}
    cli2_path = metas["bie_cli2"].attributes["category_path"]
    assert [(entry["id"], entry["level"]) for entry in cli2_path] == [(15, 1), (1504, 2)]
    assert cli2_path[0]["title"] == "BÜYÜME, İSTİHDAM, KAMU MALİYESİ"
    assert cli2_path[0]["title_en"] == "GROWTH, EMPLOYMENT, PUBLIC FINANCE"


def test_hmb_source_institution_and_datasets() -> None:
    connector = TcmbConnector(source="hmb", client=FakeClient())
    assert connector.institution_code == "hmb"
    assert connector.institution_name == "T.C. Hazine ve Maliye Bakanlığı"

    metas = list(connector.list_datasets())
    assert [meta.external_code for meta in metas] == ["bie_dbdisborc"]
    meta = metas[0]
    codes = meta.dimensions[0].codes
    assert len(codes) == 53
    assert codes[0].code == "TP.DB.D01"
    assert codes[0].attributes["frequency"] == "quarterly"
    assert codes[0].attributes["aggregation"] == "last"
    assert [entry["id"] for entry in meta.attributes["category_path"]] == [0, 99970, 9997007]
    assert meta.attributes["category_path"][0]["title"] == "ARŞİV"


def test_hmb_fetch_series_works_without_cbrt_membership() -> None:
    connector = TcmbConnector(source="hmb", client=FakeClient())
    body = {
        "items": [
            {"Tarih": "2000-1Ç", "TP_DB_D01": "1.5"},
            {"Tarih": "2000-2Ç", "TP_DB_D01": None},
        ]
    }
    connector.client.fe["TP.DB.D01"] = body
    connector.client.bounds["TP.DB.D01"] = {**BOUNDS_USD, "frequency": "6"}
    result = connector.fetch_series("bie_dbdisborc", {"SERIE": "TP.DB.D01"})
    assert result.points == [(date(2000, 1, 1), Decimal("1.5"))]
    assert connector.client.data_bodies[-1]["frequency"] == "6"


def test_unknown_source_is_value_error() -> None:
    with pytest.raises(ValueError):
        TcmbConnector(source="nope", client=FakeClient())


def test_internal_client_uses_source_institution(monkeypatch) -> None:
    from app.connectors.base import InMemoryObjectStore

    captured: dict = {}

    class _SpyClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.connectors.tcmb.connector.EvdsClient", _SpyClient)
    connector = TcmbConnector(source="hmb", store=InMemoryObjectStore())
    assert captured["institution"] == "hmb"
    connector.close()


# --- fetch -----------------------------------------------------------------


def test_fetch_series_usd() -> None:
    connector = _connector()
    result = connector.fetch_series(
        "bie_dkefkytl", {"SERIE": "TP.DK.USD.A.EF.YTL"}, order=["SERIE"]
    )
    assert result.external_code == "TP.DK.USD.A.EF.YTL"
    assert result.channel == "evds3"
    assert result.source_updated_at is None
    assert result.raw_object_keys == ["key-fe-TP.DK.USD.A.EF.YTL"]
    assert len(result.points) == 10
    values = dict(result.points)
    assert values[date(2026, 10, 1)] == Decimal("48.8960")
    body = connector.client.data_bodies[-1]
    assert body["series"] == "TP.DK.USD.A.EF.YTL"
    assert body["aggregationTypes"] == "avg"
    assert body["frequency"] == "1"
    assert body["groupSeperator"] is False
    assert body["decimal"] == "10"
    assert body["startDate"] == "01-01-2000"


def test_fetch_series_monthly_starts_at_requested_start() -> None:
    # Bounds say the series reaches 1987-12, but the requested start is 2000-01-01.
    fe = {
        "items": [
            {"Tarih": "2000-01", "TP_CLI2_A01": "1.0"},
            {"Tarih": "2000-02", "TP_CLI2_A01": None},
            {"Tarih": "2000-03", "TP_CLI2_A01": "3.0"},
        ]
    }
    connector = _connector(fe={"TP.CLI2.A01": fe})
    result = connector.fetch_series(
        "bie_cli2", {"SERIE": "TP.CLI2.A01"}, order=["SERIE"], start=date(2000, 1, 1)
    )
    assert result.points == [
        (date(2000, 1, 1), Decimal("1.0")),
        (date(2000, 3, 1), Decimal("3.0")),
    ]
    body = connector.client.data_bodies[-1]
    assert body["startDate"] == "01-01-2000"
    assert body["frequency"] == "5"


def test_fetch_series_unknown_serie_is_not_found() -> None:
    connector = _connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("bie_dkefkytl", {"SERIE": "TP.DK.NOPE"})
    assert excinfo.value.kind == NOT_FOUND


def test_fetch_series_wrong_codes_is_not_found() -> None:
    connector = _connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("bie_dkefkytl", {"SERIE": "TP.DK.USD.A.EF.YTL", "OTHER": "x"})
    assert excinfo.value.kind == NOT_FOUND


def test_fetch_series_null_bounds_is_empty() -> None:
    # Keep the daily frequency so the cross-check passes and the null window is hit.
    empty = {**BOUNDS_USD, "maxStartDate": None, "minEndDate": None}
    connector = _connector(bounds={"TP.DK.USD.A.EF.YTL": empty})
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("bie_dkefkytl", {"SERIE": "TP.DK.USD.A.EF.YTL"})
    assert excinfo.value.kind == EMPTY


def test_fetch_series_frequency_mismatch_is_format_changed() -> None:
    connector = _connector(bounds={"TP.DK.USD.A.EF.YTL": {**BOUNDS_USD, "frequency": "5"}})
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("bie_dkefkytl", {"SERIE": "TP.DK.USD.A.EF.YTL"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_fetch_series_effective_start_uses_later_bounds_start() -> None:
    bounds = {
        "TP.DK.USD.A.EF.YTL": {
            **BOUNDS_USD,
            "maxStartDate": "02-06-2000",
        }
    }
    connector = _connector(bounds=bounds)
    connector.fetch_series("bie_dkefkytl", {"SERIE": "TP.DK.USD.A.EF.YTL"}, start=date(2000, 1, 1))
    assert connector.client.data_bodies[-1]["startDate"] == "02-06-2000"

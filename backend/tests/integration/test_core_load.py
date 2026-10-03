"""Integration tests for the core-series loader and Turcat GDP linker.

The EVDS/Turcat clients are fakes serving the REAL trimmed fixtures, so no
network is touched, but ``sync_catalog``/``ingest_series``/``load_core`` and
``link_turcat`` run against the isolated test database and MinIO. These tests
are marked ``integration``; they are not run by the unit suite.

The whole integration session shares one database and one MinIO bucket, and the
observations table blocks deletes/updates, so a module cannot clean up after
itself. Every assertion below is therefore order-independent: counts are
expressed as ``inserted + unchanged``, series are identified by external code,
and the module passes whether or not another module already catalogued
``bie_gsyhuretcar`` or ingested its B1GQ series.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.catalog.linking import (
    INDICATOR_DIMENSION,
    METHOD_MANUAL,
    RELATION_SAME_SERIES,
    STATUS_ACCEPTED,
)
from app.connectors.base import ingest_series, store_raw, sync_catalog
from app.connectors.tcmb.connector import SERIE_DIMENSION, TcmbConnector
from app.connectors.tuik.turcat import TurcatConnector, TurcatResponse, ingest_sector
from app.connectors.tuik.turcat_parsers import SECTORS, parse_turcat_value
from app.core.loader import (
    TURCAT_DATASET,
    core_status,
    link_turcat,
    load_core,
    turcat_gdp_link_count,
)
from app.core.registry import CORE_SERIES, GDP_DATASET, TURCAT_GDP_LINKS
from app.data.models import (
    CatalogLink,
    Dataset,
    Institution,
    Observation,
    Series,
)

pytestmark = pytest.mark.integration

FIXTURES_TCMB = Path(__file__).parent.parent / "fixtures" / "tcmb"
FIXTURES_TURCAT = Path(__file__).parent.parent / "fixtures" / "tuik" / "turcat"

USD = "TP.DK.USD.A.EF.YTL"
USD_GROUP = "bie_dkefkytl"
CLI2 = "TP.CLI2.A01"
CLI2_GROUP = "bie_cli2"
NON_CORE_CLI2 = "TP.CLI2.A02"
GSYH_GROUP = "bie_gsyhuretcar"
TURCAT_PAGE_KEY = {code: key for code, key, _title, _page_id in SECTORS}


def _load(name: str):
    return json.loads((FIXTURES_TCMB / name).read_text(encoding="utf-8"))


class _Resp:
    """Response double with the EVDS client shape (``.json()`` + raw key)."""

    def __init__(self, payload, key: str) -> None:
        self._payload = payload
        self.raw_object_key = key

    def json(self):
        return self._payload


class FakeEvdsClient:
    """Duck-typed EvdsClient serving one source's fixtures into real MinIO."""

    def __init__(self, *, institution: str, catalog, serie_lists, bounds, fe) -> None:
        self._institution = institution
        self.catalog = catalog
        self.serie_lists = serie_lists
        self.bounds = bounds
        self.fe = fe
        self.closed = False

    def _resp(self, dataset: str, channel: str, payload) -> _Resp:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        key = store_raw(self._institution, dataset, channel, body, "json")
        return _Resp(payload, key)

    def get_catalog(self) -> _Resp:
        return self._resp("catalog", "evds3-catalog", self.catalog)

    def get_serie_list(self, group: str) -> _Resp:
        return self._resp(group, "evds3-serielist", self.serie_lists.get(group, []))

    def get_bounds(self, serie_code: str, **_kwargs) -> _Resp:
        return self._resp("_series", "evds3-bounds", self.bounds[serie_code])

    def get_data(self, body: dict, **_kwargs) -> _Resp:
        return self._resp("_series", "evds3-data", self.fe[body["series"]])

    def close(self) -> None:
        self.closed = True


def _reel_gdp_values() -> dict[str, tuple[Decimal, Decimal]]:
    """Turcat indicator -> (latest, previous) integer values for the GDP rows."""
    rows = json.loads((FIXTURES_TURCAT / "reel.turcat.raw.json").read_text(encoding="utf-8"))
    values: dict[str, tuple[Decimal, Decimal]] = {}
    for row in rows:
        code = str(row.get("ID"))
        if code in {indicator for indicator, _ in TURCAT_GDP_LINKS}:
            latest = parse_turcat_value(row.get("EN_SON_YAYIMLANAN_VERI"))
            previous = parse_turcat_value(row.get("BIR_ONCEKI_DONEM_VERI"))
            assert latest is not None and previous is not None
            values[code] = (latest, previous)
    return values


def _gsyh_fe() -> dict[str, dict]:
    """EVDS payloads per GDP serie whose values round to the Turcat integers."""
    values = _reel_gdp_values()
    fixtures: dict[str, dict] = {}
    for indicator, serie_code in TURCAT_GDP_LINKS:
        latest, previous = values[indicator]
        column = serie_code.replace(".", "_")
        fixtures[serie_code] = {
            "items": [
                {
                    "Tarih": "2026-1Ç",
                    "UNIXTIME": {"$numberLong": "1767214800"},
                    column: f"{previous}.4119",
                },
                {
                    "Tarih": "2026-2Ç",
                    "UNIXTIME": {"$numberLong": "1774990800"},
                    column: f"{latest}.4119",
                },
            ]
        }
    return fixtures


def _cli2_fe(serie_code: str) -> dict:
    """The real CLI2 FE payload with the serie code rewritten to ``serie_code``.

    The fixture keys its value column and its ``seriesNames``/
    ``frequencyConversion`` maps by ``TP.CLI2.A01``; the fake serves every other
    CLI2 code from the same payload (as ``_gsyh_fe`` does for the GDP series)
    without inventing field names.
    """
    text = json.dumps(_load("fe-cli2.raw.json"), ensure_ascii=False)
    text = text.replace(CLI2, serie_code)
    text = text.replace(CLI2.replace(".", "_"), serie_code.replace(".", "_"))
    return json.loads(text)


def _tcmb_fake() -> FakeEvdsClient:
    cli2_bounds = _load("bounds-cli2.raw.json")
    bounds = {
        USD: _load("bounds-usd.raw.json"),
        CLI2: cli2_bounds,
        NON_CORE_CLI2: cli2_bounds,
    }
    fe = {
        USD: _load("fe-usd.raw.json"),
        CLI2: _load("fe-cli2.raw.json"),
        NON_CORE_CLI2: _cli2_fe(NON_CORE_CLI2),
    }
    return FakeEvdsClient(
        institution="tcmb",
        catalog=_load("catalog.raw.json"),
        serie_lists={
            USD_GROUP: _load(f"serielist-{USD_GROUP}.raw.json"),
            CLI2_GROUP: _load(f"serielist-{CLI2_GROUP}.raw.json"),
        },
        bounds=bounds,
        fe=fe,
    )


def _tuik_fake() -> FakeEvdsClient:
    gsyh_bounds = _load("bounds-gsyh.raw.json")
    serie_codes = [serie_code for _, serie_code in TURCAT_GDP_LINKS]
    return FakeEvdsClient(
        institution="tuik",
        catalog=_load("catalog-tuik.raw.json"),
        serie_lists={GSYH_GROUP: _load(f"serielist-{GSYH_GROUP}.raw.json")},
        bounds={serie_code: gsyh_bounds for serie_code in serie_codes},
        fe=_gsyh_fe(),
    )


class FakeTurcatClient:
    """Duck-typed TurcatClient serving the five real sector fixtures."""

    def __init__(self) -> None:
        self.fixtures = {
            key: (FIXTURES_TURCAT / f"{key.lower()}.turcat.raw.json").read_bytes()
            for key in TURCAT_PAGE_KEY.values()
        }

    def sector(self, page_key: str) -> TurcatResponse:
        content = self.fixtures[page_key]
        key = store_raw("tuik", page_key, f"turcat-{page_key}", content, "json")
        return TurcatResponse(status_code=200, content=content, raw_object_key=key)

    def close(self) -> None:  # pragma: no cover - nothing to close
        return None


def _catalog_all(session) -> None:
    tcmb = TcmbConnector(client=_tcmb_fake(), source="tcmb")
    tuik = TcmbConnector(client=_tuik_fake(), source="tuik-evds")
    sync_catalog(session, tcmb)
    sync_catalog(session, tuik)
    session.commit()


def _connector_for(source: str) -> TcmbConnector:
    if source == "tcmb":
        return TcmbConnector(client=_tcmb_fake(), source="tcmb")
    return TcmbConnector(client=_tuik_fake(), source="tuik-evds")


def test_core_load_flags_only_registry_and_link_turcat() -> None:
    from app.db.session import SessionLocal

    # --- catalog both EVDS sources and ingest one NON-core series ----------
    with SessionLocal() as session:
        _catalog_all(session)
        dataset = session.scalar(
            select(Dataset)
            .join(Institution, Institution.id == Dataset.institution_id)
            .where(
                Institution.code == "tcmb",
                Dataset.external_code == CLI2_GROUP,
            )
        )
        assert dataset is not None
        non_core = ingest_series(
            session,
            TcmbConnector(client=_tcmb_fake(), source="tcmb"),
            dataset=dataset,
            codes={SERIE_DIMENSION: NON_CORE_CLI2},
            start=date(2000, 1, 1),
        )
        session.commit()
        non_core_id = non_core.series_id

    with SessionLocal() as session:
        series = session.get(Series, non_core_id)
        assert series is not None
        assert series.is_core is False

    # --- first load: all 15 registry series land and are flagged -----------
    with SessionLocal() as session:
        first, failures = load_core(session, _connector_for)
    assert failures == []
    assert len(first) == 15
    assert sum(item.point_count for item in first) > 0
    # Every fetched point is either appended or already stored; this holds even
    # when another module (test_tcmb) already ingested a GDP series.
    assert all(item.inserted + item.unchanged == item.point_count for item in first)

    # --- second load is idempotent: no new observations --------------------
    with SessionLocal() as session:
        second, failures = load_core(session, _connector_for)
    assert failures == []
    assert all(item.inserted + item.unchanged == item.point_count for item in second)
    assert sum(item.inserted for item in second) == 0
    assert sum(item.unchanged for item in second) > 0

    with SessionLocal() as session:
        core_codes = {
            row.external_code
            for row in session.scalars(select(Series).where(Series.is_core.is_(True))).all()
        }
        # Flagged by external code, never by a count of all rows: the B1GQ series
        # may already exist from another integration module.
        assert core_codes == {core.external_code for core in CORE_SERIES}
        b1gq = session.scalar(
            select(Series).where(Series.external_code == f"{GDP_DATASET}:TP.GSYIH040.IFK.B1GQ")
        )
        assert b1gq is not None and b1gq.is_core is True
        assert NON_CORE_CLI2 not in {code.split(":", 1)[1] for code in core_codes}
        non_core = session.get(Series, non_core_id)
        assert non_core is not None and non_core.is_core is False

    # --- catalog and ingest the real Turcat reel rows ----------------------
    turcat = TurcatConnector(client=FakeTurcatClient())
    with SessionLocal() as session:
        sync_catalog(session, turcat)
        session.commit()
    with SessionLocal() as session:
        dataset = session.scalar(
            select(Dataset)
            .join(Institution, Institution.id == Dataset.institution_id)
            .where(Institution.code == "tuik", Dataset.external_code == TURCAT_DATASET)
        )
        assert dataset is not None
        fetch = turcat.fetch_sector(TURCAT_DATASET)
        ingest_sector(session, turcat, dataset, fetch)
        session.commit()

    # --- link-turcat: every Turcat period must equal round(EVDS) -----------
    with SessionLocal() as session:
        links, failures = link_turcat(session)
    assert failures == []
    assert len(links) == 13
    assert turcat_gdp_link_count_after_link() == 13

    # Re-run changes nothing (the accepted rows are never overwritten).
    with SessionLocal() as session:
        again, failures = link_turcat(session)
    assert failures == []
    assert len(again) == 13
    assert turcat_gdp_link_count_after_link() == 13

    # --- a tampered Turcat value makes exactly that pair refused -----------
    with SessionLocal() as session:
        turcat_series = session.scalar(
            select(Series)
            .join(Institution, Institution.id == Series.institution_id)
            .where(
                Institution.code == "tuik",
                Series.external_code == f"{TURCAT_DATASET}:5",
            )
        )
        assert turcat_series is not None
        wrong = Observation(
            series_id=turcat_series.id,
            period=date(2026, 4, 1),
            value=Decimal("-1"),
            fetched_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        session.add(wrong)
        session.commit()
    with SessionLocal() as session:
        links, failures = link_turcat(session)
    assert {failure.indicator for failure in failures} == {"5"}
    assert len(links) == 12

    # --- read back the catalog links ---------------------------------------
    with SessionLocal() as session:
        rows = session.scalars(
            select(CatalogLink).where(CatalogLink.relation == RELATION_SAME_SERIES)
        ).all()
        gdp_rows = [
            row
            for row in rows
            if row.from_codes.get(INDICATOR_DIMENSION) in dict(TURCAT_GDP_LINKS)
        ]
        assert len(gdp_rows) == 13
        for row in gdp_rows:
            assert row.status == STATUS_ACCEPTED
            assert row.method == METHOD_MANUAL
            assert row.mapping == {}
            assert set(row.from_codes) == {INDICATOR_DIMENSION}
            assert set(row.to_codes) == {SERIE_DIMENSION}
            assert row.to_codes[SERIE_DIMENSION].startswith("TP.GSYIH040.IFK.")

    # --- status sees the accepted links and the flagged series -------------
    with SessionLocal() as session:
        statuses = core_status(session)
        assert len(statuses) == 15
        assert all(item.is_core for item in statuses)
        assert turcat_gdp_link_count(session) == 13


def turcat_gdp_link_count_after_link() -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        return turcat_gdp_link_count(session)


def test_core_load_missing_dataset_points_at_catalog() -> None:
    """A core load against an uncatalogued dataset fails; it is never silent."""
    from app.db.session import SessionLocal

    tuik_code = f"tuik-missing-{uuid4().hex[:8]}"

    with SessionLocal() as session:
        institution = Institution(code=tuik_code, name="missing dataset test")
        session.add(institution)
        session.commit()

    connector = TcmbConnector(client=_tuik_fake(), source="tuik-evds")
    connector.institution_code = tuik_code

    with SessionLocal() as session:
        loads, failures = load_core(
            session, lambda _source: connector, only=f"{GDP_DATASET}:TP.GSYIH040.IFK.B1GQ"
        )
    assert loads == []
    assert len(failures) == 1
    assert "catalog" in failures[0].message


def test_status_is_well_formed_for_every_registry_series() -> None:
    """``core_status`` reports one well-formed line per registry series."""
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        statuses = core_status(session)
    assert len(statuses) == 15
    assert {item.external_code for item in statuses} == {
        core.external_code for core in CORE_SERIES
    }
    for item in statuses:
        assert item.observation_count >= 0

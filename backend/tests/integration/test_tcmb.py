"""Integration tests for the TCMB EVDS3 connector write path (isolated stack).

The EVDS3 client is a fake serving the REAL trimmed fixtures, so no network is
touched, but ``sync_catalog``/``ingest_series`` run against the real test
database and MinIO.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.connectors.base import ingest_series, store_raw, sync_catalog
from app.connectors.tcmb.connector import TcmbConnector
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    Institution,
    Observation,
    Series,
)
from app.data.observations import get_latest
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tcmb"
USD = "TP.DK.USD.A.EF.YTL"
USD_GROUP = "bie_dkefkytl"
USD_COLUMN = "TP_DK_USD_A_EF_YTL"
GSYH_GROUP = "bie_gsyhuretcar"
GSYH_SERIE = "TP.GSYIH040.IFK.B1GQ"
GSYH_COLUMN = "TP_GSYIH040_IFK_B1GQ"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Resp:
    """Response double with the EVDS3 client shape (``.json()`` + raw key)."""

    def __init__(self, payload, key: str) -> None:
        self._payload = payload
        self.raw_object_key = key

    def json(self):
        return self._payload


class FakeClient:
    """Duck-typed EvdsClient serving mutable fixture payloads into real MinIO."""

    def __init__(self) -> None:
        self.catalog = _load("catalog.raw.json")
        self.serie_lists = {
            "bie_cli2": _load("serielist-bie_cli2.raw.json"),
            USD_GROUP: _load(f"serielist-{USD_GROUP}.raw.json"),
            "bie_dbdisborc": _load("serielist-bie_dbdisborc.raw.json"),
        }
        self.bounds = {USD: _load("bounds-usd.raw.json")}
        self.fe = {USD: _load("fe-usd.raw.json")}
        self.closed = False

    def _resp(self, dataset: str, channel: str, payload) -> _Resp:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        key = store_raw("tcmb", dataset, channel, body, "json")
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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _set_usd_value(client: FakeClient, tarih: str, value: str) -> None:
    for item in client.fe[USD]["items"]:
        if item["Tarih"] == tarih:
            item[USD_COLUMN] = value
            return
    raise AssertionError(f"{tarih} not present in the USD payload")


def test_tcmb_catalog_ingest_revision_and_removal() -> None:
    client = FakeClient()
    connector = TcmbConnector(client=client)
    institution_code = _unique("tcmb-it")
    connector.institution_code = institution_code
    connector.institution_name = "TCMB integration"

    # --- catalog: datasets/dimensions/codes, no series ---------------------
    with SessionLocal() as session:
        first = sync_catalog(session, connector)
        session.commit()
    assert first.inserted == 2
    assert first.unchanged == 0

    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        institution_id = institution.id
        datasets = {
            dataset.external_code: dataset
            for dataset in session.scalars(
                select(Dataset).where(Dataset.institution_id == institution_id)
            ).all()
        }
        assert set(datasets) == {"bie_cli2", USD_GROUP}
        usd_dataset = datasets[USD_GROUP]
        assert usd_dataset.source_category == "TCMB DÖVİZ KURLARI"
        assert usd_dataset.attributes["channel"] == "evds3"
        assert usd_dataset.attributes["unit"] == "Türk lirası"

        (dimension,) = usd_dataset.dimensions
        assert dimension.code == "SERIE"
        assert dimension.role == "other"
        assert len(dimension.codes) == 78
        usd_code = next(code for code in dimension.codes if code.code == USD)
        assert usd_code.parent_code is None
        assert usd_code.attributes["frequency"] == "daily"
        assert usd_code.attributes["aggregation"] == "avg"
        assert usd_code.attributes["aggregations"] == ["avg", "first", "last", "max", "min"]
        assert usd_code.attributes["unit"] == "Türk lirası"
        assert usd_code.attributes["level"] == 1

        series_count = session.scalar(
            select(func.count()).select_from(Series).where(Series.institution_id == institution_id)
        )
        assert series_count == 0
        usd_dataset_id = usd_dataset.id

    # --- first ingest ------------------------------------------------------
    with SessionLocal() as session:
        dataset = session.get(Dataset, usd_dataset_id)
        assert dataset is not None
        result = ingest_series(
            session, connector, dataset=dataset, codes={"SERIE": USD}, start=date(2000, 1, 1)
        )
        session.commit()
    assert result.inserted == 10
    assert result.unchanged == 0
    assert result.point_count == 10
    assert result.period_start == date(2000, 1, 3)
    assert result.period_end == date(2026, 10, 5)

    with SessionLocal() as session:
        series = session.get(Series, result.series_id)
        assert series is not None
        assert series.frequency == "daily"
        assert series.attributes["aggregation"] == "avg"
        assert series.coverage_start == date(2000, 1, 3)
        assert series.coverage_end == date(2026, 10, 5)
        rows = session.scalars(
            select(Observation).where(Observation.series_id == result.series_id)
        ).all()
        assert len(rows) == 10
        assert all(row.value is not None for row in rows)
        assert all(row.fetched_at.tzinfo is not None for row in rows)
        assert all(row.fetched_at.utcoffset() is not None for row in rows)
        assert all(abs((datetime.now(UTC) - row.fetched_at).total_seconds()) < 300 for row in rows)
        assert all(
            row.raw_object_key and row.raw_object_key.startswith("sources/tcmb/") for row in rows
        )
        assert {row.period for row in rows} == {
            row.period for row in get_latest(session, result.series_id)
        }

    # --- second ingest: no changes ----------------------------------------
    with SessionLocal() as session:
        dataset = session.get(Dataset, usd_dataset_id)
        assert dataset is not None
        again = ingest_series(
            session, connector, dataset=dataset, codes={"SERIE": USD}, start=date(2000, 1, 1)
        )
        session.commit()
    assert again.inserted == 0
    assert again.unchanged == 10

    # --- third ingest: one changed value is a new revision -----------------
    _set_usd_value(client, "03-01-2000", "0.5500")
    with SessionLocal() as session:
        dataset = session.get(Dataset, usd_dataset_id)
        assert dataset is not None
        changed = ingest_series(
            session, connector, dataset=dataset, codes={"SERIE": USD}, start=date(2000, 1, 1)
        )
        session.commit()
    assert changed.inserted == 1
    assert changed.unchanged == 9

    with SessionLocal() as session:
        revisions = session.scalars(
            select(Observation).where(
                Observation.series_id == result.series_id,
                Observation.period == date(2000, 1, 3),
            )
        ).all()
        assert len(revisions) == 2  # the old row is kept, the new one appended
        total = session.scalar(
            select(func.count())
            .select_from(Observation)
            .where(Observation.series_id == result.series_id)
        )
        assert total == 11
        latest = {row.period: row.value for row in get_latest(session, result.series_id)}
        assert latest[date(2000, 1, 3)] == Decimal("0.5500")

    # --- removal: a code that leaves the source is flagged, not deleted ----
    client.serie_lists["bie_cli2"] = [
        row for row in client.serie_lists["bie_cli2"] if row["SERIE_CODE"] != "TP.CLI2.A03"
    ]
    second_connector = TcmbConnector(client=client)
    second_connector.institution_code = institution_code
    second_connector.institution_name = "TCMB integration"
    with SessionLocal() as session:
        sync_catalog(session, second_connector)
        session.commit()

    with SessionLocal() as session:
        cli2 = session.scalar(
            select(Dataset).where(
                Dataset.institution_id == institution_id,
                Dataset.external_code == "bie_cli2",
            )
        )
        assert cli2 is not None
        dimension = session.scalar(
            select(DatasetDimension).where(
                DatasetDimension.dataset_id == cli2.id,
                DatasetDimension.code == "SERIE",
            )
        )
        assert dimension is not None
        codes = {
            code.code: code
            for code in session.scalars(
                select(DimensionCode).where(DimensionCode.dimension_id == dimension.id)
            ).all()
        }
    assert set(codes) == {"TP.CLI2.A01", "TP.CLI2.A02", "TP.CLI2.A03"}
    assert codes["TP.CLI2.A03"].attributes.get("removed_at") is not None
    assert codes["TP.CLI2.A01"].attributes.get("removed_at") is None


def test_tcmb_hmb_catalog_is_separate_and_idempotent() -> None:
    client = FakeClient()

    tcmb_connector = TcmbConnector(client=client)
    tcmb_code = _unique("tcmb-it")
    tcmb_connector.institution_code = tcmb_code
    tcmb_connector.institution_name = "TCMB integration"

    hmb_connector = TcmbConnector(source="hmb", client=client)
    hmb_code = _unique("hmb-it")
    hmb_connector.institution_code = hmb_code
    hmb_connector.institution_name = "HMB integration"

    with SessionLocal() as session:
        tcmb_sync = sync_catalog(session, tcmb_connector)
        hmb_sync = sync_catalog(session, hmb_connector)
        session.commit()
    assert tcmb_sync.inserted == 2
    assert hmb_sync.inserted == 1
    assert hmb_sync.codes == 53

    with SessionLocal() as session:
        tcmb_inst = session.scalar(select(Institution).where(Institution.code == tcmb_code))
        hmb_inst = session.scalar(select(Institution).where(Institution.code == hmb_code))
        assert tcmb_inst is not None and hmb_inst is not None
        assert tcmb_inst.id != hmb_inst.id

        hmb_datasets = {
            dataset.external_code: dataset
            for dataset in session.scalars(
                select(Dataset).where(Dataset.institution_id == hmb_inst.id)
            ).all()
        }
        assert set(hmb_datasets) == {"bie_dbdisborc"}
        dataset = hmb_datasets["bie_dbdisborc"]
        assert dataset.attributes["category_path"] == [
            {"id": 0, "level": 1, "title": "ARŞİV", "title_en": "ARCHIVE"},
            {
                "id": 99970,
                "level": 2,
                "title": "ÖDEMELER DENGESİ VE DIŞ İSTATİSTİKLER (ARŞİV)",
                "title_en": "BALANCE OF PAYMENTS AND EXTERNAL STATISTICS (ARCHIVE)",
            },
            {
                "id": 9997007,
                "level": 3,
                "title": "TÜRKİYE BRÜT DIŞ BORÇ STOKU (HMB) (ARŞİV)",
                "title_en": "GROSS EXTERNAL DEBT STOCK OF TÜRKİE (MoFT) (ARCHIVE)",
            },
        ]
        (dimension,) = dataset.dimensions
        assert dimension.code == "SERIE"
        codes = {code.code: code for code in dimension.codes}
        assert len(codes) == 53
        assert codes["TP.DB.D01"].attributes["frequency"] == "quarterly"
        assert codes["TP.DB.D01"].attributes["aggregation"] == "last"

        series_count = session.scalar(
            select(func.count()).select_from(Series).where(Series.institution_id == hmb_inst.id)
        )
        assert series_count == 0

        tcmb_dataset_codes = {
            dataset.external_code
            for dataset in session.scalars(
                select(Dataset).where(Dataset.institution_id == tcmb_inst.id)
            ).all()
        }
        assert tcmb_dataset_codes == {"bie_cli2", USD_GROUP}

    # A second run of the same source changes nothing.
    with SessionLocal() as session:
        again = sync_catalog(session, hmb_connector)
        session.commit()
    assert again.inserted == 0
    assert again.updated == 0
    assert again.unchanged == 1


class TuikFakeClient:
    """Duck-typed EvdsClient serving the real TÜİK GYSH fixtures into MinIO.

    Raw payloads use the connector's ``tuik`` institution so the test exercises
    the same ``sources/tuik/<date>/<dataset>/evds3-<...>`` layout the live
    connector produces.
    """

    def __init__(self) -> None:
        self.catalog = _load("catalog-tuik.raw.json")
        self.serie_lists = {GSYH_GROUP: _load(f"serielist-{GSYH_GROUP}.raw.json")}
        self.bounds = {GSYH_SERIE: _load("bounds-gsyh.raw.json")}
        self.fe = {GSYH_SERIE: _load("fe-gsyh.raw.json")}
        self.closed = False

    def _resp(self, dataset: str, channel: str, payload) -> _Resp:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        key = store_raw("tuik", dataset, channel, body, "json")
        return _Resp(payload, key)

    def get_catalog(self) -> _Resp:
        return self._resp("catalog", "evds3-catalog", self.catalog)

    def get_serie_list(self, group: str) -> _Resp:
        return self._resp(group, "evds3-serielist", self.serie_lists.get(group, []))

    def get_bounds(self, serie_code: str, **_kwargs) -> _Resp:
        return self._resp(GSYH_GROUP, "evds3-bounds", self.bounds[serie_code])

    def get_data(self, body: dict, **_kwargs) -> _Resp:
        return self._resp(GSYH_GROUP, "evds3-data", self.fe[body["series"]])

    def close(self) -> None:
        self.closed = True


def test_tuik_evds_catalog_fetch_and_isolation_from_other_tuik_datasets() -> None:
    """The TÜİK EVDS3 source writes the real path without disturbing TÜİK rows."""
    client = TuikFakeClient()
    connector = TcmbConnector(source="tuik-evds", client=client)
    assert connector.institution_code == "tuik"
    assert connector.institution_name == "Türkiye İstatistik Kurumu"

    # A pre-existing TÜİK dataset (another channel) that must survive untouched.
    with SessionLocal() as session:
        institution = session.scalar(select(Institution).where(Institution.code == "tuik"))
        if institution is None:
            institution = Institution(code="tuik", name="Türkiye İstatistik Kurumu")
            session.add(institution)
            session.flush()
        pre_existing = Dataset(
            institution_id=institution.id,
            external_code="tuik-pre-existing",
            name="Pre-existing TÜİK dataset",
            attributes={"databrowser2": {"kept": True}},
        )
        session.add(pre_existing)
        session.commit()
        pre_existing_id = pre_existing.id
        institution_id = institution.id
        institution_name = institution.name
        # Snapshot the institution's datasets: another integration module may
        # already have added the GYSH dataset (and the Turcat datasets) to this
        # shared institution, so the isolation check must compare to pre-state.
        pre_dataset_codes = {
            row.external_code
            for row in session.scalars(
                select(Dataset).where(Dataset.institution_id == institution_id)
            ).all()
        }

    # --- catalog: only the allow-listed datagroup, no series ----------------
    with SessionLocal() as session:
        first = sync_catalog(session, connector)
        session.commit()
    # The dataset may already exist from another module; either way this sync
    # adds exactly the one allow-listed datagroup and updates nothing.
    assert first.inserted + first.unchanged == 1
    assert first.updated == 0
    assert first.codes == 14

    with SessionLocal() as session:
        institution = session.scalar(select(Institution).where(Institution.code == "tuik"))
        assert institution is not None
        assert institution.id == institution_id
        assert institution.name == institution_name  # the name is not renamed

        datasets = {
            dataset.external_code: dataset
            for dataset in session.scalars(
                select(Dataset).where(Dataset.institution_id == institution_id)
            ).all()
        }
        # The sync added only the allow-listed datagroup; every dataset already
        # present (including the pre-existing one) is still there.
        assert set(datasets) == pre_dataset_codes | {GSYH_GROUP}
        assert datasets["tuik-pre-existing"].id == pre_existing_id
        assert datasets["tuik-pre-existing"].name == "Pre-existing TÜİK dataset"
        assert datasets["tuik-pre-existing"].attributes == {"databrowser2": {"kept": True}}

        dataset = datasets[GSYH_GROUP]
        assert dataset.attributes["channel"] == "evds3"
        assert dataset.attributes["data_source_en"] == "TURKSTAT"
        (dimension,) = dataset.dimensions
        assert dimension.code == "SERIE"
        codes = {code.code: code for code in dimension.codes}
        assert len(codes) == 14
        assert codes[GSYH_SERIE].attributes["frequency"] == "quarterly"
        assert codes[GSYH_SERIE].attributes["aggregation"] == "last"
        gsyh_dataset_id = dataset.id

    # --- second catalog sync: nothing changes ------------------------------
    with SessionLocal() as session:
        again = sync_catalog(session, connector)
        session.commit()
    assert again.inserted == 0
    assert again.updated == 0
    assert again.unchanged == 1

    # The B1GQ series may already carry observations from another module; the
    # fetch below must append exactly ``inserted`` new revision rows on top.
    with SessionLocal() as session:
        existing_series = session.scalar(
            select(Series).where(
                Series.institution_id == institution_id,
                Series.external_code == f"{GSYH_GROUP}:{GSYH_SERIE}",
            )
        )
        pre_observations = (
            session.scalar(
                select(func.count())
                .select_from(Observation)
                .where(Observation.series_id == existing_series.id)
            )
            if existing_series is not None
            else 0
        )

    # --- fetch one series through the real write path ----------------------
    with SessionLocal() as session:
        dataset = session.get(Dataset, gsyh_dataset_id)
        assert dataset is not None
        result = ingest_series(
            session,
            connector,
            dataset=dataset,
            codes={"SERIE": GSYH_SERIE},
            start=date(1995, 1, 1),
        )
        session.commit()
    # Every fetched point is either appended or already stored.
    assert result.inserted + result.unchanged == result.point_count
    assert result.point_count == 4
    assert result.period_start == date(1995, 1, 1)
    assert result.period_end == date(2026, 4, 1)
    assert result.raw_object_key is not None
    assert result.raw_object_key.startswith("sources/tuik/")

    with SessionLocal() as session:
        series = session.get(Series, result.series_id)
        assert series is not None
        assert series.external_code == f"{GSYH_GROUP}:{GSYH_SERIE}"
        assert series.frequency == "quarterly"
        assert series.attributes["aggregation"] == "last"
        assert series.coverage_start == date(1995, 1, 1)
        assert series.coverage_end == date(2026, 4, 1)
        rows = session.scalars(
            select(Observation).where(Observation.series_id == result.series_id)
        ).all()
        # ``inserted`` rows were appended to whatever the series already had.
        assert len(rows) == pre_observations + result.inserted
        fetched_rows = [
            row for row in rows if row.raw_object_key == result.raw_object_key
        ]
        assert len(fetched_rows) == result.inserted
        assert all(
            row.raw_object_key and row.raw_object_key.startswith("sources/tuik/")
            for row in fetched_rows
        )
        latest = {row.period: row.value for row in get_latest(session, result.series_id)}
        assert latest[date(2026, 4, 1)] == Decimal("19869747341.4119000000")

    # --- the pre-existing TÜİK dataset is untouched after the fetch ---------
    with SessionLocal() as session:
        unchanged = session.get(Dataset, pre_existing_id)
        assert unchanged is not None
        assert unchanged.name == "Pre-existing TÜİK dataset"
        assert unchanged.attributes == {"databrowser2": {"kept": True}}

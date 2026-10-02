"""Unit tests for the Turcat connector (fake client + fixtures, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.connectors.base import build_series_definition
from app.connectors.tuik.turcat import (
    TurcatConnector,
    TurcatResponse,
    ingest_sector,
)
from app.data.models import Dataset, DatasetDimension, DimensionCode, Observation, Series

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "turcat"

FIX = {
    "Reel": (FIXTURES / "reel.turcat.raw.json").read_bytes(),
    "Mali": (FIXTURES / "mali.turcat.raw.json").read_bytes(),
    "Finans": (FIXTURES / "finans.turcat.raw.json").read_bytes(),
    "Dis": (FIXTURES / "dis.turcat.raw.json").read_bytes(),
    "Nufus": (FIXTURES / "nufus.turcat.raw.json").read_bytes(),
}


class FakeClient:
    """Duck-typed TurcatClient serving captured fixtures."""

    max_concurrency = 4

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    def sector(self, page_key: str) -> TurcatResponse:
        self.calls.append(page_key)
        return TurcatResponse(200, FIX[page_key], f"sources/tuik/{page_key}/raw.json")

    def close(self) -> None:
        self.closed = True


def _connector() -> tuple[TurcatConnector, FakeClient]:
    client = FakeClient()
    return TurcatConnector(client=client), client


def test_list_datasets_yields_five_sectors() -> None:
    connector, client = _connector()
    metas = list(connector.list_datasets())
    assert [meta.external_code for meta in metas] == [
        "TURCAT_REEL",
        "TURCAT_MALI",
        "TURCAT_FINANS",
        "TURCAT_DIS",
        "TURCAT_NUFUS",
    ]
    assert sorted(client.calls) == ["Dis", "Finans", "Mali", "Nufus", "Reel"]
    assert all(meta.dimensions and meta.dimensions[0].code == "INDICATOR" for meta in metas)


def test_dataset_meta_nufus() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta("TURCAT_NUFUS")
    assert meta.external_code == "TURCAT_NUFUS"
    assert meta.source_category == "Turcat / Nüfus"
    assert meta.coverage_start == date(2025, 1, 1)
    assert meta.obs_count == 1
    dimension = meta.dimensions[0]
    assert dimension.code == "INDICATOR"
    assert dimension.role == "other"
    (code,) = dimension.codes
    assert code.code == "251"
    assert code.label == "Nüfus"
    assert code.attributes["page"] == "Nüfus"
    assert code.attributes["frequency"] == "annual"
    assert code.attributes["unit"] == "Bin kişi"
    assert code.attributes["meta_url"].startswith("https://dsbb.imf.org/sdds/")
    assert code.attributes["data_url"].startswith("https://data.tuik.gov.tr/")


def test_dataset_meta_marks_group_codes() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta("TURCAT_REEL")
    group = next(code for code in meta.dimensions[0].codes if code.code == "1")
    assert group.attributes["group"] is True
    child = next(code for code in meta.dimensions[0].codes if code.code == "2")
    assert child.parent_code == "1"
    assert "group" not in child.attributes


def test_fetch_series_returns_latest_and_previous() -> None:
    connector, _ = _connector()
    result = connector.fetch_series("TURCAT_NUFUS", {"INDICATOR": "251"}, order=["INDICATOR"])
    assert result.external_code == "TURCAT_NUFUS:251"
    assert result.channel == "turcat"
    assert result.points == [
        (date(2025, 1, 1), Decimal("86092")),
        (date(2024, 1, 1), Decimal("85665")),
    ]
    assert result.raw_object_keys == ["sources/tuik/Nufus/raw.json"]


def _nufus_dataset() -> Dataset:
    dataset = Dataset(id=1, institution_id=1, external_code="TURCAT_NUFUS", name="Nüfus")
    dataset.dimensions = [
        DatasetDimension(
            code="INDICATOR",
            label="Gösterge",
            position=0,
            role="other",
            codes=[
                DimensionCode(
                    code="251",
                    label="Nüfus",
                    attributes={"frequency": "annual", "unit": "Bin kişi"},
                )
            ],
        )
    ]
    return dataset


def test_build_series_definition_reads_code_attributes() -> None:
    definition = build_series_definition(_nufus_dataset(), {"INDICATOR": "251"})
    assert definition.external_code == "TURCAT_NUFUS:251"
    assert definition.frequency == "annual"
    assert definition.unit == "Bin kişi"
    assert definition.breakdown == {"INDICATOR": {"code": "251", "label": "Nüfus"}}


class _FakeResult:
    def all(self) -> list:
        return []


class _FakeSession:
    """Minimal session double: creates one series, records observations."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self._series: Series | None = None
        self._next_id = 1

    def scalar(self, statement: object) -> None:
        return None

    def get(self, model: object, pk: object) -> Series | None:
        return self._series

    def execute(self, statement: object) -> _FakeResult:
        return _FakeResult()

    def add(self, obj: object) -> None:
        if isinstance(obj, Series):
            obj.id = self._next_id
            self._next_id += 1
            self._series = obj
        self.added.append(obj)

    def flush(self) -> None:
        return None


def test_ingest_sector_creates_series_and_two_observations() -> None:
    connector, _ = _connector()
    dataset = _nufus_dataset()
    fetch = connector.fetch_sector("TURCAT_NUFUS")
    session = _FakeSession()

    result = ingest_sector(
        session,
        connector,
        dataset,
        fetch,
        fetched_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.indicators == 1
    assert result.series == 1
    assert result.points == 2
    assert result.inserted == 2
    assert result.unchanged == 0
    observations = [obj for obj in session.added if isinstance(obj, Observation)]
    assert {obs.period for obs in observations} == {date(2025, 1, 1), date(2024, 1, 1)}
    assert all(obs.fetched_at == datetime(2026, 9, 30, tzinfo=UTC) for obs in observations)
    assert all(obs.raw_object_key == "sources/tuik/Nufus/raw.json" for obs in observations)
    series = session._series
    assert series is not None
    assert series.coverage_start == date(2024, 1, 1)
    assert series.coverage_end == date(2025, 1, 1)

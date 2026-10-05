"""Integration test: hidden-FREQ frequency backfill + preservation (Task 2.4b).

Written for the isolated ``karven-test`` stack; intentionally NOT run by the unit
suite. Run with ``uv run python scripts/integration.py``.

The test DB is shared, so each test uses unique external codes and deletes its
rows in a fixture teardown.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.catalog.enrich import run_rules
from app.connectors.base import (
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    upsert_dataset,
)
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.frequency import FrequencyResolution
from app.data.models import (
    Dataset,
    DatasetDimension,
    DatasetTag,
    DimensionCode,
    Institution,
    MeasureCombination,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest.fixture
def seeded_codes() -> list[str]:
    codes: list[str] = []
    yield codes
    if not codes:
        return
    with SessionLocal() as session:
        ids = list(
            session.scalars(sa.select(Dataset.id).where(Dataset.external_code.in_(codes))).all()
        )
        if not ids:
            return
        session.execute(sa.delete(MeasureCombination).where(MeasureCombination.dataset_id.in_(ids)))
        session.execute(sa.delete(DatasetTag).where(DatasetTag.dataset_id.in_(ids)))
        dimension_ids = list(
            session.scalars(
                sa.select(DatasetDimension.id).where(DatasetDimension.dataset_id.in_(ids))
            ).all()
        )
        if dimension_ids:
            session.execute(
                sa.delete(DimensionCode).where(DimensionCode.dimension_id.in_(dimension_ids))
            )
        session.execute(sa.delete(DatasetDimension).where(DatasetDimension.dataset_id.in_(ids)))
        session.execute(sa.delete(Dataset).where(Dataset.id.in_(ids)))
        session.commit()


def _institution(session, code: str) -> Institution:
    institution = session.scalar(sa.select(Institution).where(Institution.code == code))
    if institution is None:
        institution = Institution(code=code, name=code)
        session.add(institution)
        session.flush()
    return institution


class _FakeFreqClient:
    """A fake databrowser2 client: hidden FREQ codelist per identifier, no values."""

    max_concurrency = 1
    throttled = False

    def __init__(self, by_identifier: dict[str, FrequencyResolution]) -> None:
        self._by_identifier = by_identifier
        self.calls: list[tuple[str, str]] = []

    def dataset_partial_codelist(self, dataset_id, dimension, criteria=None):
        self.calls.append((dataset_id, dimension))

        class _Response:
            def __init__(self, resolution: FrequencyResolution) -> None:
                self._resolution = resolution

            def json(self):
                raise AssertionError("value-only fake")

        raise AssertionError("use resolve patch")


def _hidden_meta(code: str, *, agency: str = "TR", version: str = "1.0") -> DatasetMeta:
    """A hidden-FREQ dataset built by the connector path (no FREQ data dimension)."""
    return DatasetMeta(
        external_code=code,
        name="Hidden FREQ dataset",
        attributes={
            "channel": "databrowser2",
            "agency": agency,
            "dataflow_id": code,
            "version": version,
            "hidden_dimensions": ["FREQ"],
        },
        dimensions=[
            DimensionMeta(
                code="INDICATOR",
                label="Gösterge",
                position=0,
                role="other",
                codes=[DimensionCodeMeta(code="POP", label="Nüfus")],
            ),
            DimensionMeta(code="TIME_PERIOD", label="Zaman", position=1, role="time", codes=[]),
        ],
    )


class _ResolutionClient:
    """Fake client that returns a resolution directly (patched resolve)."""

    def __init__(self, resolutions: dict[str, FrequencyResolution]) -> None:
        self._resolutions = resolutions
        self.calls: list[str] = []

    def dataset_partial_codelist(self, dataset_id, dimension, criteria=None):
        self.calls.append(dataset_id)
        resolution = self._resolutions[dataset_id]

        class _Response:
            def json(self):
                raise AssertionError("value-only fake")

        self._last = resolution
        return _Response()


def test_backfill_then_refresh_preserves_and_drops(seeded_codes: list[str], monkeypatch) -> None:
    """Write mode sets a default, then a catalog refresh keeps or drops it."""
    good = _unique("FBGOOD")
    stale = _unique("FBSTALE")
    seeded_codes.extend([good, stale])

    with SessionLocal() as session:
        institution = _institution(session, "tuik")
        good_meta = _hidden_meta(good)
        upsert_dataset(session, institution.id, good_meta)
        stale_meta = _hidden_meta(stale)
        upsert_dataset(session, institution.id, stale_meta)
        # stale carries a (stale) stored single frequency before the backfill.
        stale_row = session.scalar(sa.select(Dataset).where(Dataset.external_code == stale))
        attributes = dict(stale_row.attributes)
        attributes["default_frequency"] = "monthly"
        attributes["frequency_source"] = {"code": "M", "origin": "hidden_freq_codelist"}
        stale_row.attributes = attributes
        session.commit()

    resolutions = {
        f"TR,{good},1.0": FrequencyResolution(
            status="single",
            codes=[{"id": "A2", "name": "Biennial"}],
            code="A2",
            frequency="annual",
        ),
        f"TR,{stale},1.0": FrequencyResolution(
            status="multiple",
            codes=[{"id": "M", "name": "Monthly"}, {"id": "A", "name": "Annual"}],
        ),
    }
    client = _ResolutionClient(resolutions)
    monkeypatch.setattr(tuik_cli, "resolve_for_dataset", lambda _client, ident: resolutions[ident])

    with SessionLocal() as session:
        datasets = list(
            session.scalars(sa.select(Dataset).where(Dataset.external_code.in_([good, stale])))
        )
        code = tuik_cli.run_frequency_backfill(
            datasets, client, session=session, dry_run=False, workers=1
        )
    assert code == 1  # `stale` is multiple
    assert client.calls == []  # resolve_for_dataset was monkeypatched

    with SessionLocal() as session:
        good_row = session.scalar(sa.select(Dataset).where(Dataset.external_code == good))
        assert good_row.attributes["default_frequency"] == "annual"
        assert good_row.attributes["frequency_source"]["code"] == "A2"
        stale_row = session.scalar(sa.select(Dataset).where(Dataset.external_code == stale))
        assert "default_frequency" not in stale_row.attributes
        assert stale_row.attributes["frequency_resolution"]["status"] == "multiple"

        # Second writer: a catalog refresh with no resolution keeps the good value
        # and does not resurrect the stale one.
        institution = _institution(session, "tuik")
        upsert_dataset(session, institution.id, _hidden_meta(good))
        session.commit()

    with SessionLocal() as session:
        good_row = session.scalar(sa.select(Dataset).where(Dataset.external_code == good))
        assert good_row.attributes["default_frequency"] == "annual"
        assert good_row.attributes["frequency_source"]["code"] == "A2"


def test_backfill_query_error_keeps_a_valid_default(seeded_codes: list[str], monkeypatch) -> None:
    code = _unique("FBERR")
    seeded_codes.append(code)
    with SessionLocal() as session:
        institution = _institution(session, "tuik")
        upsert_dataset(session, institution.id, _hidden_meta(code))
        row = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        attributes = dict(row.attributes)
        attributes["default_frequency"] = "monthly"
        row.attributes = attributes
        session.commit()

    resolutions = {f"TR,{code},1.0": FrequencyResolution(status="query_error", reason="gone")}
    monkeypatch.setattr(tuik_cli, "resolve_for_dataset", lambda _client, ident: resolutions[ident])
    with SessionLocal() as session:
        datasets = list(session.scalars(sa.select(Dataset).where(Dataset.external_code == code)))
        exit_code = tuik_cli.run_frequency_backfill(
            datasets, _ResolutionClient(resolutions), session=session, dry_run=False, workers=1
        )
    assert exit_code == 1
    with SessionLocal() as session:
        row = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        assert row.attributes["default_frequency"] == "monthly"
        assert row.attributes["frequency_resolution"]["status"] == "query_error"


def test_veri_yok_rule_persists_and_search_excludes(seeded_codes: list[str]) -> None:
    code = _unique("PORTAL")
    seeded_codes.append(code)
    with SessionLocal() as session:
        institution = _institution(session, "tuik")
        dataset = Dataset(
            institution_id=institution.id,
            external_code=code,
            name="İndirilemeyen Rapor",
            attributes={
                "channel": "veriportali",
                "veriportali": {"downloadable": False, "id": f"{code}+V1.0"},
            },
        )
        session.add(dataset)
        session.flush()
        session.commit()
        dataset_id = dataset.id

    with SessionLocal() as session:
        summary, _candidates = run_rules(session, dataset_code=code, dry_run=False)
        assert not summary.errors, summary.errors

    with SessionLocal() as session:
        row = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        assert row.veri_yok is True
        assert row.flags_checked_at is not None

    # The search candidate query excludes veri_yok datasets in both passes.
    from app.catalog import search
    from app.catalog.tree import load_tree

    tree = load_tree()
    with SessionLocal() as session:
        for arsiv in ("exclude", "only"):
            rows = search._load_candidate_datasets(
                session,
                leaf_ids=[tree.leaf_order[0]],
                branch_ids=[tree.branch_order[0]],
                arsiv=arsiv,
            )
            assert code not in {row[0].external_code for row in rows}

    # Flipping downloadable back true clears the flag on the next rules pass.
    with SessionLocal() as session:
        row = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        attributes = dict(row.attributes)
        attributes["veriportali"] = {**attributes["veriportali"], "downloadable": True}
        row.attributes = attributes
        session.commit()
    with SessionLocal() as session:
        run_rules(session, dataset_code=code, dry_run=False)
    with SessionLocal() as session:
        row = session.scalar(sa.select(Dataset).where(Dataset.id == dataset_id))
        assert row.veri_yok is False

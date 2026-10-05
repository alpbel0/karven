"""Integration test: run the enrichment rules/Jev pass on a small seeded catalog.

Written for the isolated ``karven-test`` stack; intentionally NOT run here. Run
with ``uv run python scripts/integration.py``.

The test DB is shared with every other test file, so each test:
- uses get-or-create institutions (never inserts a duplicate ``tuik``),
- owns unique dataset external codes (a uuid suffix) and deletes the rows it
  created in a fixture teardown,
- filters ``run_rules``/``JevPass`` to its own datasets, never the whole DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.catalog.enrich import run_rules
from app.catalog.enrich_jev import JevPass
from app.catalog.tree import load_tree
from app.connectors.base import (
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    upsert_dataset,
)
from app.data.models import (
    CatalogLink,
    Dataset,
    DatasetDimension,
    DatasetTag,
    DimensionCode,
    Institution,
    MeasureCombination,
)
from app.db.session import SessionLocal
from app.migrator.postgres import ALEMBIC_INI

pytestmark = pytest.mark.integration


class _FakeJev:
    """Deterministic stand-in for Jev: scripted answers per question id."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def decide(self, state, questions, *, prompt_ref=None):  # noqa: ANN001
        self.calls.append(questions)
        answers: dict[str, dict] = {}
        if "olcu" in questions:
            answers["olcu"] = {"type": "noul", "score": 0.95, "confidence": 0.95}
        if "tur" in questions:
            answers["tur"] = {
                "type": "choice",
                "choice": "akim_tutar",
                "confidence": 0.93,
                "probabilities": {"akim_tutar": 0.93, "stok_tutar": 0.04},
            }
        if "nitelik" in questions:
            answers["nitelik"] = {
                "type": "choice",
                "choice": "gerceklesen",
                "confidence": 0.97,
                "probabilities": {"gerceklesen": 0.97},
            }
        if "para" in questions:
            answers["para"] = {
                "type": "choice",
                "choice": "TRY",
                "confidence": 0.9,
                "probabilities": {"TRY": 0.9},
            }
        if "nominal" in questions:
            answers["nominal"] = {"type": "noul", "score": 0.1, "confidence": 0.9}
        if "donem" in questions:
            answers["donem"] = {"type": "noul", "score": 0.05, "confidence": 0.9}
        return {"answers": answers, "usage": {}, "provider": "fake", "model": "fake"}


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest.fixture
def seeded_codes() -> list[str]:
    """Collect the external codes a test creates and delete their rows after."""
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
        session.execute(
            sa.delete(MeasureCombination).where(MeasureCombination.dataset_id.in_(ids))
        )
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
        session.execute(sa.delete(DatasetTag).where(DatasetTag.dataset_id.in_(ids)))
        session.execute(
            sa.delete(CatalogLink).where(
                sa.or_(
                    CatalogLink.from_dataset_id.in_(ids),
                    CatalogLink.to_dataset_id.in_(ids),
                )
            )
        )
        session.execute(sa.delete(Dataset).where(Dataset.id.in_(ids)))
        session.commit()


def _institution(session, code: str) -> Institution:
    """Get-or-create: the shared DB already has ``tuik``/``tcmb`` rows."""
    institution = session.scalar(sa.select(Institution).where(Institution.code == code))
    if institution is None:
        institution = Institution(code=code, name=code)
        session.add(institution)
        session.flush()
    return institution


def _dataset(session, institution_id: int, code: str, name: str) -> Dataset:
    dataset = Dataset(institution_id=institution_id, external_code=code, name=name)
    session.add(dataset)
    session.flush()
    return dataset


def _dimension(
    session,
    dataset_id: int,
    code: str,
    role: str = "other",
    position: int = 0,
    codes: list[tuple[str, str, dict]] | None = None,
) -> None:
    dimension = DatasetDimension(
        dataset_id=dataset_id, code=code, label=code, position=position, role=role
    )
    session.add(dimension)
    session.flush()
    for code_value, label, attributes in codes or []:
        session.add(
            DimensionCode(
                dimension_id=dimension.id,
                code=code_value,
                label=label,
                attributes=attributes,
            )
        )
    session.flush()


def test_rules_seed_and_read_back(seeded_codes: list[str]) -> None:
    tuik_code = _unique("TU")
    tcmb_code = _unique("TC")
    plain_code = _unique("TN")
    seeded_codes.extend([tuik_code, tcmb_code, plain_code])

    with SessionLocal() as session:
        tuik = _institution(session, "tuik")
        tcmb = _institution(session, "tcmb")

        dataset = _dataset(session, tuik.id, tuik_code, "Sanayi Üretim Endeksi")
        # The type-bearing DEGISIM carries the change type (tier A wins); the
        # INDICATOR is a neutral title so the tiers are unambiguous.
        _dimension(
            session,
            dataset.id,
            "INDICATOR",
            position=0,
            codes=[("SAN", "Sanayi Üretim Endeksi", {})],
        )
        _dimension(
            session,
            dataset.id,
            "DEGISIM",
            position=1,
            codes=[
                ("IDX", "Index", {}),
                ("ANN", "Annual rate of change (%)", {}),
            ],
        )
        _dimension(
            session, dataset.id, "FREQ", role="frequency", position=2, codes=[("M", "Monthly", {})]
        )

        tcmb_dataset = _dataset(session, tcmb.id, tcmb_code, "Politika Faizi")
        _dimension(
            session,
            tcmb_dataset.id,
            "SERIE",
            codes=[
                (
                    "TP.FAIZ",
                    "Politika Faizi",
                    {"aggregation": "last", "unit": "Yüzde", "frequency": "monthly"},
                )
            ],
        )

        plain = _dataset(session, tuik.id, plain_code, "Nüfus")
        _dimension(
            session, plain.id, "FREQ", role="frequency", codes=[("A", "Annual", {})]
        )
        session.commit()

    candidates: dict = {}
    with SessionLocal() as session:
        for code in (tuik_code, tcmb_code, plain_code):
            summary, candidates = run_rules(session, dataset_code=code, dry_run=False)
            assert not summary.errors, summary.errors

        combos = session.scalars(
            sa.select(MeasureCombination)
            .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
            .where(Dataset.external_code.in_([tuik_code, tcmb_code, plain_code]))
        ).all()
        assert len(combos) == 4, [c.label for c in combos]
        tuik_labels = {
            combo.label: combo
            for combo in combos
            if combo.codes and "INDICATOR" in combo.codes
        }
        # Indicator first (dimension position order), not code order.
        assert set(tuik_labels) == {
            "Sanayi Üretim Endeksi | Index",
            "Sanayi Üretim Endeksi | Annual rate of change (%)",
        }
        assert tuik_labels["Sanayi Üretim Endeksi | Index"].measure_type == "endeks"
        annual = tuik_labels["Sanayi Üretim Endeksi | Annual rate of change (%)"]
        assert annual.measure_type == "yillik_yuzde_degisim"
        assert annual.attributes["type_rule_tier"] == "A"

        empty = [combo for combo in combos if combo.codes == {}]
        assert len(empty) == 1
        assert empty[0].label == "Nüfus"

    # No period-series candidate may point at the datasets this test created.
    assert all(
        identity[1] not in {tuik_code, tcmb_code, plain_code} for identity in candidates
    )

    with SessionLocal() as session:
        dataset = session.scalar(sa.select(Dataset).where(Dataset.external_code == tcmb_code))
        assert dataset is not None
        lazy = session.scalar(
            sa.select(MeasureCombination).where(MeasureCombination.dataset_id == dataset.id)
        )
        assert lazy.source_aggregation == "last"
        assert lazy.measure_type == "oran_pay"
        assert lazy.aggregation == "ortalama"
        assert lazy.aggregation_conflict is True


def test_jev_pass_over_seeded_catalog(seeded_codes: list[str]) -> None:
    """Run the fake-Jev pass over the seeded catalog; a second run asks nothing."""
    tuik_code = _unique("TJ")
    seeded_codes.append(tuik_code)
    fake = _FakeJev()

    with SessionLocal() as session:
        tuik = _institution(session, "tuik")
        dataset = _dataset(session, tuik.id, tuik_code, "Hammadde Alımları")
        _dimension(
            session,
            dataset.id,
            "INDICATOR",
            position=0,
            codes=[("TUTAR", "Alım Tutarı", {})],
        )
        _dimension(
            session, dataset.id, "FREQ", role="frequency", position=1, codes=[("M", "Monthly", {})]
        )
        session.commit()

    with SessionLocal() as session:
        summary, _candidates = run_rules(session, dataset_code=tuik_code, dry_run=False)
        assert not summary.errors, summary.errors

    run = JevPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=2,
        dataset=tuik_code,
    )
    result = run.run("all")
    assert result.errors == 0
    assert not result.aborted

    with SessionLocal() as session:
        dataset = session.scalar(sa.select(Dataset).where(Dataset.external_code == tuik_code))
        combination = session.scalar(
            sa.select(MeasureCombination).where(MeasureCombination.dataset_id == dataset.id)
        )
        assert combination.type_method == "jev"
        assert combination.measure_type == "akim_tutar"
        assert combination.aggregation == "toplam"
        assert combination.status in {"accepted", "review"}

    calls_before = len(fake.calls)
    second = JevPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=2,
        dataset=tuik_code,
    )
    second.run("types")
    assert len(fake.calls) == calls_before  # every type/nature already decided


def _meta(code: str, description: str | None) -> DatasetMeta:
    return DatasetMeta(
        external_code=code,
        name="Nüfus",
        description=description,
        dimensions=[
            DimensionMeta(
                code="INDICATOR",
                label="Gösterge",
                position=0,
                role="other",
                codes=[DimensionCodeMeta(code="POP", label="Nüfus", attributes={})],
            )
        ],
    )


def test_catalog_upsert_preserves_enrichment(seeded_codes: list[str]) -> None:
    """A later catalog refresh keeps enrichment description, keys and flags."""
    code = _unique("UP")
    seeded_codes.append(code)
    with SessionLocal() as session:
        institution = _institution(session, "tuik")
        upsert_dataset(session, institution.id, _meta(code, "Kaynak açıklama"))
        session.commit()

    with SessionLocal() as session:
        run_rules(session, dataset_code=code, dry_run=False)

    with SessionLocal() as session:
        dataset = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        assert dataset.description  # the enrichment template
        assert dataset.attributes["source_description"] == "Kaynak açıklama"
        attributes = dict(dataset.attributes)
        attributes["period_series_rejected"] = True
        dataset.attributes = attributes
        session.commit()

    with SessionLocal() as session:
        institution = session.scalar(sa.select(Institution).where(Institution.code == "tuik"))
        upsert_dataset(session, institution.id, _meta(code, "Yeni kaynak açıklama"))
        session.commit()

    with SessionLocal() as session:
        dataset = session.scalar(sa.select(Dataset).where(Dataset.external_code == code))
        assert dataset.attributes["source_description"] == "Yeni kaynak açıklama"
        assert dataset.attributes.get("period_series_rejected") is True
        assert dataset.description  # never overwritten by the catalog refresh


def test_migration_0018_downgrade_and_upgrade_round_trip() -> None:
    """Downgrade 0018 removes the enrichment tables/columns; head restores them."""
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    parent = script.get_revision("0018").down_revision
    command.downgrade(config, parent)
    try:
        with SessionLocal() as session:
            inspector = sa.inspect(session.get_bind())
            assert not inspector.has_table("measure_combinations")
            assert not inspector.has_table("dataset_tags")
            columns = {column["name"] for column in inspector.get_columns("datasets")}
            assert "revizyon_tablosu" not in columns
            assert "donem_serisi" not in columns
            dimension_columns = {
                column["name"] for column in inspector.get_columns("dataset_dimensions")
            }
            assert "is_measure" not in dimension_columns
    finally:
        command.upgrade(config, "head")
    with SessionLocal() as session:
        inspector = sa.inspect(session.get_bind())
        assert inspector.has_table("measure_combinations")
        assert inspector.has_table("dataset_tags")

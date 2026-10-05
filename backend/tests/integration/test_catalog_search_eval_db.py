"""Integration test: test-set validation against a real catalog (Task 2.6).

Nothing is committed: every test flushes inside one session and rolls back.
Run with ``uv run python scripts/integration.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.catalog.search_eval import validate_against_catalog
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _testset(*entries: dict) -> dict:
    return {"queries": [{"id": "q1", "group": "haber", "request": "x", "expected": list(entries)}]}


def test_validation_reports_missing_dataset_code_and_veri_yok() -> None:
    suffix = uuid4().hex[:10]
    with SessionLocal() as session:
        institution = Institution(code=f"ev-{suffix}", name="Eval Test")
        session.add(institution)
        session.flush()
        good = Dataset(
            institution_id=institution.id, external_code=f"GOOD-{suffix}", name="g", attributes={}
        )
        no_data = Dataset(
            institution_id=institution.id,
            external_code=f"NODATA-{suffix}",
            name="n",
            attributes={},
            veri_yok=True,
        )
        session.add_all([good, no_data])
        session.flush()
        dimension = DatasetDimension(
            dataset_id=good.id, code="INDICATOR", label="G", position=0, role="other"
        )
        session.add(dimension)
        session.flush()
        session.add(
            DimensionCode(dimension_id=dimension.id, code="POP", label="Nüfus", attributes={})
        )
        session.flush()

        code = institution.code
        ok = validate_against_catalog(
            session,
            _testset(
                {"institution": code, "dataset": good.external_code, "codes": {"INDICATOR": "POP"}}
            ),
        )
        problems = validate_against_catalog(
            session,
            _testset(
                {"institution": code, "dataset": "MISSING"},
                {"institution": code, "dataset": no_data.external_code},
                {
                    "institution": code,
                    "dataset": good.external_code,
                    "codes": {"INDICATOR": "WRONG"},
                },
                {"institution": code, "dataset": good.external_code, "codes": {"NOPE": "POP"}},
            ),
        )
        session.rollback()

    assert ok == []
    assert len(problems) == 4
    assert "not in the catalog" in problems[0]
    assert "veri_yok" in problems[1]
    assert "INDICATOR=WRONG" in problems[2]
    assert "NOPE=POP" in problems[3]

"""Integration tests for the source-independent ``documents`` table."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.tuik.yayin import YayinItem, load_documents
from app.data.models import Document
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _item(external_id: str, *, title: str | None = None, year: int | None = 2025) -> YayinItem:
    return YayinItem(
        external_id=external_id,
        title=title or f"Title {external_id}",
        subject="Gelir, Tüketim ve Yoksulluk",
        doc_type="Mikro Veri Seti",
        year=year,
        url=f"https://biruni.tuik.gov.tr/yayin/views/visitorPages/yayinGoruntuleme.zul?yayin_no={external_id}",
        attributes={"labels": ["Gelir, Tüketim ve Yoksulluk", title or f"Title {external_id}"]},
    )


def test_load_reload_round_trip_and_removed_marking() -> None:
    source = _unique("tuik_yayin_test")
    first = _item("1001")
    second = _item("1002")

    with SessionLocal() as session:
        load = load_documents(session, [first, second], source=source)
        session.commit()
    assert (load.inserted, load.updated, load.unchanged, load.removed) == (2, 0, 0, 0)

    # Reload the same rows: nothing changes, both are bumped but unchanged.
    with SessionLocal() as session:
        load = load_documents(session, [first, second], source=source)
        session.commit()
    assert (load.inserted, load.updated, load.unchanged, load.removed) == (0, 0, 2, 0)

    # Drop the second row in a COMPLETE crawl: it is marked removed, not deleted.
    with SessionLocal() as session:
        load = load_documents(session, [first], source=source, complete=True)
        session.commit()
    assert load.removed == 1

    with SessionLocal() as session:
        removed = session.scalar(
            sa.select(Document).where(
                Document.source == source, Document.external_id == second.external_id
            )
        )
        assert removed is not None
        assert removed.attributes.get("removed_at")

        # A partial crawl (complete=False) that misses the row must not re-mark it.
        load = load_documents(session, [], source=source, complete=False)
        session.commit()
        assert load.removed == 0

        # The row reappears: its marker is cleared and it counts as updated.
        load = load_documents(session, [first, second], source=source, complete=True)
        session.commit()
        assert load.updated == 1

        session.refresh(removed)
        assert "removed_at" not in removed.attributes


def test_documents_are_filterable_by_source_and_type() -> None:
    source = _unique("tuik_yayin_test")
    with SessionLocal() as session:
        load_documents(session, [_item("2001"), _item("2002", year=None)], source=source)
        session.commit()

    with SessionLocal() as session:
        count = session.scalar(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(Document.source == source, Document.doc_type == "Mikro Veri Seti")
        )
        null_year = session.scalar(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(Document.source == source, Document.year.is_(None))
        )
    assert count == 2
    assert null_year == 1

"""Integration tests for the first agent's database paths (Task 3.3).

Real PostgreSQL: migration 0021 (the tables and their CHECK constraints), the
one-transaction run write with its catalog-gap rows, the accepted-leaf-tag query
the tautology check uses, and the full ``run_first_agent`` flow with a scripted chat
client and fake catalog tools (no network, no LLM).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.data.models import (
    CatalogGap,
    Dataset,
    DatasetTag,
    FirstAgentIdea,
    FirstAgentRun,
    FirstAgentVisual,
    Institution,
    NewsArticle,
)
from app.db.session import SessionLocal
from app.first_agent import service
from app.first_agent.persist import RunMeta, save_run
from app.first_agent.state import Idea, RunState, Slot, Visual
from tests.first_agent_fakes import (
    FakeCatalog,
    ScriptedClient,
    Turn,
    call,
    make_deps,
    recipe,
    status_result,
)

pytestmark = pytest.mark.integration


def _news(session, *, text: str | None = "Benzine zam geldi.", status: str = "ok") -> int:
    news = NewsArticle(
        source="haberturk",
        external_id=f"https://example.test/{uuid4().hex}",
        url="https://example.test/x",
        title="Benzine zam",
        content_text=text,
        text_status=status,
        first_seen_at=datetime.now(UTC),
    )
    session.add(news)
    session.flush()
    return int(news.id)


def _slot(role: str, dataset: str) -> Slot:
    return Slot(
        role=role,
        recipe=recipe(dataset),
        info={"series_external_code": f"{dataset}:S", "series_name": dataset},
    )


def test_save_run_writes_candidates_gaps_and_selection_flags() -> None:
    with SessionLocal() as session:
        news_id = _news(session)
        state = RunState(news_id=news_id, searches=["brent petrol"])
        state.visuals = [
            Visual(1, "Benzin", "r", _slot("series", "FUEL"), {"text": "%4,2", "period": None}),
            Visual(2, "Brent", "r", Slot(role="series", gap="Brent petrol"), None),
        ]
        state.visuals[0].selected = True
        state.visuals[0].selection_reason = "haberin kendisi"
        state.ideas = [
            Idea(
                1,
                "Brent -> TÜFE",
                "m",
                "positive",
                "annual_pct_change",
                _slot("target", "CPI"),
                [Slot(role="driver", gap="Brent petrol")],
                "catalog_gap",
            ),
            Idea(
                2,
                "Kur -> TÜFE",
                "m",
                None,
                "annual_pct_change",
                _slot("target", "CPI"),
                [_slot("driver", "USD")],
                "ready",
            ),
        ]
        state.ideas[1].graph = {"status": "queued", "summary": "stub"}
        run_id = save_run(
            session,
            state,
            RunMeta(
                status="ok",
                summary="özet",
                selection={"visuals": {1: "haberin kendisi"}, "ideas": {}},
                prompt_key="first_agent.system",
                prompt_version=1,
                prompt_checksum="c",
                provider="evren",
                model="m",
                usage={"prompt_tokens": 3},
                llm_attempts=4,
                tool_calls=5,
            ),
        )
        session.commit()

        run = session.get(FirstAgentRun, run_id)
        assert (run.status, run.llm_attempts, run.tool_calls) == ("ok", 4, 5)
        assert run.selection == {"visuals": {"1": "haberin kendisi"}, "ideas": {}}
        visuals = session.scalars(
            sa.select(FirstAgentVisual)
            .where(FirstAgentVisual.run_id == run_id)
            .order_by(FirstAgentVisual.local_id)
        ).all()
        assert [(v.status, v.selected) for v in visuals] == [
            ("candidate", True),
            ("catalog_gap", False),
        ]
        assert visuals[1].series is None
        assert visuals[0].news_value == {"text": "%4,2", "period": None}
        ideas = session.scalars(
            sa.select(FirstAgentIdea)
            .where(FirstAgentIdea.run_id == run_id)
            .order_by(FirstAgentIdea.local_id)
        ).all()
        assert [(i.status, i.graph_status) for i in ideas] == [
            ("catalog_gap", None),
            ("ready", "queued"),
        ]
        assert ideas[0].drivers == [{"role": "driver", "missing": "Brent petrol"}]
        gaps = session.scalars(sa.select(CatalogGap).where(CatalogGap.run_id == run_id)).all()
        assert sorted((g.item_kind, g.item_local_id, g.role) for g in gaps) == [
            ("idea", 1, "driver"),
            ("visual", 2, "series"),
        ]
        assert all(g.searches == ["brent petrol"] and g.status == "open" for g in gaps)
        assert all(g.news_id == news_id for g in gaps)


def test_the_table_checks_reject_unknown_statuses() -> None:
    with SessionLocal() as session:
        news_id = _news(session)
        session.add(FirstAgentRun(news_id=news_id, status="weird"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_a_candidate_pair_is_unique_per_run() -> None:
    with SessionLocal() as session:
        news_id = _news(session)
        state = RunState(news_id=news_id)
        state.visuals = [Visual(1, "a", "r", _slot("series", "FUEL"), None)]
        run_id = save_run(session, state, RunMeta(status="ok"))
        session.add(FirstAgentVisual(run_id=run_id, local_id=1, title="dup", status="candidate"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_accepted_leaf_tags_only_returns_accepted_leaves() -> None:
    code = f"inst-{uuid4().hex[:8]}"
    with SessionLocal() as session:
        institution = Institution(code=code, name=code)
        session.add(institution)
        session.flush()
        dataset = Dataset(institution_id=institution.id, external_code="DS", name="DS")
        session.add(dataset)
        session.flush()
        for tag_id, level, status in (
            ("a_leaf", "leaf", "accepted"),
            ("b_branch", "branch", "accepted"),
            ("c_review", "leaf", "review"),
            ("d_rejected", "leaf", "rejected"),
        ):
            session.add(
                DatasetTag(
                    dataset_id=dataset.id,
                    tag_id=tag_id,
                    level=level,
                    source="jev",
                    status=status,
                )
            )
        session.commit()
        assert service.accepted_leaf_tags(session, code, "DS") == {"a_leaf"}
        assert service.accepted_leaf_tags(session, code, "NOPE") == set()


def test_run_first_agent_end_to_end_against_the_real_database() -> None:
    catalog = FakeCatalog(
        {
            "FUEL": status_result("tuik", "FUEL", "Benzin", measure_type="fiyat_kur"),
            "USD": status_result("tcmb", "USD", "USD/TRY", measure_type="fiyat_kur"),
            "CPI": status_result("tuik", "CPI", "TÜFE"),
        }
    )
    deps = make_deps(catalog)
    with SessionLocal() as session:
        news_id = _news(session)
        session.commit()
    turns = [
        Turn(
            tool_calls=[
                call("find_series", "a", request="aylık benzin fiyatı"),
                call(
                    "add_visual",
                    "b",
                    title="Benzin",
                    reason="Haberin konusu",
                    series=recipe("FUEL"),
                ),
                call(
                    "add_idea",
                    "c",
                    title="Kur -> TÜFE",
                    target=recipe("CPI"),
                    drivers=[recipe("USD")],
                    transform="annual_pct_change",
                    mechanism="m",
                ),
            ]
        ),
        Turn(tool_calls=[call("ask_graph_agent", "d", idea_id=1)]),
        Turn(content="hazır"),
    ]
    final = {
        "summary": "Özet.",
        "selected_visuals": [{"id": 1, "reason": "haber"}],
        "selected_ideas": [{"id": 1, "reason": "kuyrukta"}],
    }
    result = service.run_first_agent(
        news_id,
        chat_client_factory=lambda: ScriptedClient(turns, [final]),
        deps_factory=lambda factory: (deps, lambda: None),
    )
    assert result.status == "ok"
    with SessionLocal() as session:
        run = session.get(FirstAgentRun, result.run_id)
        assert run.news_id == news_id
        assert run.prompt_key == "first_agent.system"
        idea = session.scalar(sa.select(FirstAgentIdea).where(FirstAgentIdea.run_id == run.id))
        assert idea.selected is True
        assert idea.graph_status == "tested"

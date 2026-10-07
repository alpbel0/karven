"""CLI for the first agent (Task 3.3): ``python -m app.first_agent``.

Subcommands:

    seed-prompts
    run --news-id N
    show --run-id N
    gaps [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys

from app.first_agent import service
from app.first_agent.schema import SYSTEM_PROMPT_KEY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.first_agent",
        description="Read a news article and register visual candidates and relation ideas.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed-prompts", help="seed the default prompt body if unset")
    run = subparsers.add_parser("run", help="run the first agent on one news article")
    run.add_argument("--news-id", required=True, type=int, dest="news_id")
    show = subparsers.add_parser("show", help="print one stored run with its candidates")
    show.add_argument("--run-id", required=True, type=int, dest="run_id")
    gaps = subparsers.add_parser("gaps", help="list open catalog gaps")
    gaps.add_argument("--limit", type=int, default=50)
    return parser


def _cmd_seed_prompts() -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        service.ensure_default_prompt(session)
        session.commit()
    print(f"seed-prompts: {SYSTEM_PROMPT_KEY} active")
    return 0


def _cmd_run(news_id: int) -> int:
    result = service.run_first_agent(news_id)
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str, indent=2))
    return 0 if result.status == "ok" else 1


def _cmd_show(run_id: int) -> int:
    import sqlalchemy as sa

    from app.data.models import FirstAgentIdea, FirstAgentRun, FirstAgentVisual
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        run = session.get(FirstAgentRun, run_id)
        if run is None:
            print(f"show: no run {run_id}", file=sys.stderr)
            return 1
        visuals = session.scalars(
            sa.select(FirstAgentVisual)
            .where(FirstAgentVisual.run_id == run_id)
            .order_by(FirstAgentVisual.local_id)
        ).all()
        ideas = session.scalars(
            sa.select(FirstAgentIdea)
            .where(FirstAgentIdea.run_id == run_id)
            .order_by(FirstAgentIdea.local_id)
        ).all()
        payload = {
            "run": {
                column.name: getattr(run, column.name) for column in FirstAgentRun.__table__.columns
            },
            "visuals": [
                {c.name: getattr(row, c.name) for c in FirstAgentVisual.__table__.columns}
                for row in visuals
            ],
            "ideas": [
                {c.name: getattr(row, c.name) for c in FirstAgentIdea.__table__.columns}
                for row in ideas
            ],
        }
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
    return 0


def _cmd_gaps(limit: int) -> int:
    import sqlalchemy as sa

    from app.data.models import CatalogGap
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rows = session.scalars(
            sa.select(CatalogGap)
            .where(CatalogGap.status == "open")
            .order_by(CatalogGap.id.desc())
            .limit(limit)
        ).all()
        for row in rows:
            print(
                f"{row.id}\tnews={row.news_id}\trun={row.run_id}\t{row.item_kind} "
                f"{row.item_local_id}\t{row.role}\t{row.description}"
            )
        print(f"gaps: {len(rows)} open (limit={limit})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "seed-prompts": lambda: _cmd_seed_prompts(),
        "run": lambda: _cmd_run(args.news_id),
        "show": lambda: _cmd_show(args.run_id),
        "gaps": lambda: _cmd_gaps(args.limit),
    }
    try:
        return handlers[args.command]()
    except Exception as exc:  # noqa: BLE001 - CLI reports and fails cleanly
        print(f"{args.command}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

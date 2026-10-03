"""CLI for the data-fetch agent (Task 1.6): ``python -m app.fetching.agent``.

Subcommands:

    seed-prompts
    diagnose --job-id N [--no-dispatch]
    ask "<question>" [--context '<json>']
    failures [--limit N] [--outcome recorded|retry_requested|agent_error]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.fetching.agent import service
from app.fetching.agent.schema import ASK_PROMPT_KEY, SYSTEM_PROMPT_KEY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.fetching.agent",
        description="Diagnose failed fetches and answer data questions (Task 1.6).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("seed-prompts", help="seed the default prompt bodies if unset")

    diagnose = subparsers.add_parser("diagnose", help="diagnose one failed job")
    diagnose.add_argument("--job-id", required=True, type=int, dest="job_id")
    diagnose.add_argument(
        "--no-dispatch",
        action="store_true",
        dest="no_dispatch",
        help="record a retry without enqueueing it (the watchdog re-enqueues later)",
    )

    ask = subparsers.add_parser("ask", help="ask a metadata question")
    ask.add_argument("question")
    ask.add_argument("--context", default=None, help="optional JSON object context")

    failures = subparsers.add_parser("failures", help="list recent failure records")
    failures.add_argument("--limit", type=int, default=50)
    failures.add_argument(
        "--outcome",
        default=None,
        choices=["recorded", "retry_requested", "agent_error"],
    )

    return parser


def _cmd_seed_prompts() -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        service.ensure_default_prompt(session)
        session.commit()
    print(f"seed-prompts: {SYSTEM_PROMPT_KEY}, {ASK_PROMPT_KEY} active")
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from app.config import settings
    from app.db.session import SessionLocal

    result = service.diagnose_job(
        SessionLocal,
        args.job_id,
        chat_client_factory=service.default_chat_client_factory,
        max_rounds=settings.fetch_agent_max_rounds,
        dispatch_retry=not args.no_dispatch,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    context: dict[str, Any] | None = None
    if args.context:
        context = json.loads(args.context)
    answer = service.ask_fetch_agent(args.question, context)
    print(json.dumps(answer.__dict__, ensure_ascii=False, default=str))
    return 0


def _cmd_failures(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rows = service.list_failures(session, limit=args.limit, outcome=args.outcome)
    for row in rows:
        print(
            f"{row['id']}\tjob={row['job_id']}\tround={row['round']}\t"
            f"{row['institution']}:{row['external_code']}\t{row['category']}\t"
            f"{row['outcome']}\t{row['diagnosis'] or ''}\t{row['suggestion'] or ''}"
        )
    print(f"failures: {len(rows)} rows (limit={args.limit}, outcome={args.outcome})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "seed-prompts": lambda: _cmd_seed_prompts(),
        "diagnose": lambda: _cmd_diagnose(args),
        "ask": lambda: _cmd_ask(args),
        "failures": lambda: _cmd_failures(args),
    }
    try:
        return handlers[args.command]()
    except Exception as exc:  # noqa: BLE001 - CLI reports and fails cleanly
        print(f"{args.command}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

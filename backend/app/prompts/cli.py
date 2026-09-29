"""Command-line interface for the versioned prompt store."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.prompts import service
from app.prompts.errors import PromptError


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.prompts",
        description="Manage versioned prompts stored in PostgreSQL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="append a new version (does not activate)")
    add.add_argument("--key", required=True, help="prompt key, e.g. first_agent.system")
    add.add_argument("--file", required=True, type=Path, help="file holding the prompt body")
    add.add_argument("--note", default=None, help="optional note for this version")

    activate = subparsers.add_parser("activate", help="move the active pointer")
    activate.add_argument("--key", required=True, help="prompt key")
    activate.add_argument("--version", required=True, type=int, help="version to activate")

    listing = subparsers.add_parser("list", help="list versions and the active one")
    listing.add_argument("--key", default=None, help="restrict to one prompt key")

    show = subparsers.add_parser("show", help="show a version (default: active)")
    show.add_argument("--key", required=True, help="prompt key")
    show.add_argument("--version", default=None, type=int, help="version to show")

    return parser


def _cmd_add(session, args: argparse.Namespace) -> int:
    body = args.file.read_text(encoding="utf-8")
    version = service.add_version(session, key=args.key, body=body, note=args.note)
    session.commit()
    print(f"added {version.key} v{version.version} ({version.checksum}) - not active")
    return 0


def _cmd_activate(session, args: argparse.Namespace) -> int:
    service.activate(session, key=args.key, version=args.version)
    session.commit()
    print(f"activated {args.key} v{args.version}")
    return 0


def _cmd_list(session, args: argparse.Namespace) -> int:
    versions = service.list_versions(session, key=args.key)
    if not versions:
        print("no prompt versions found")
        return 0
    for version in versions:
        marker = " *" if version.active else ""
        note = f" - {version.note}" if version.note else ""
        print(f"{version.key} v{version.version}{marker}{note}")
    return 0


def _cmd_show(session, args: argparse.Namespace) -> int:
    view = service.show_prompt(session, key=args.key, version=args.version)
    print(view.body, end="" if view.body.endswith("\n") else "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    from app.db.session import SessionLocal

    handlers = {
        "add": _cmd_add,
        "activate": _cmd_activate,
        "list": _cmd_list,
        "show": _cmd_show,
    }
    with SessionLocal() as session:
        try:
            return handlers[args.command](session, args)
        except PromptError as exc:
            session.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1

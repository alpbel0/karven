#!/usr/bin/env python
"""End-to-end runner for the isolated integration test stack.

Usage (from ``backend/``)::

    uv run python scripts/integration.py

Starts the throwaway ``karven-test`` compose project (postgres, neo4j, redis,
minio), runs the migrator, starts the api, then runs ``pytest -m integration``
on the host using the test settings from ``compose.test.env``. The
``karven-test`` project is always torn down with ``down -v`` (even on failure
or Ctrl-C).

The exit code is pytest's exit code, or 1 if setup failed. Live (``karven``) is
never touched: different project name, ports, databases, bucket, and image tag.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
TEST_ENV_FILE = REPO_ROOT / "compose.test.env"
PROJECT_NAME = "karven-test"


def log(message: str) -> None:
    print(f"[integration] {message}", flush=True)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def run_step(command: list[str], env: dict[str, str] | None = None) -> int:
    log("run: " + " ".join(command))
    completed = subprocess.run(command, env=env, check=False)
    log(f"exit code: {completed.returncode}")
    return completed.returncode


def main() -> int:
    if not TEST_ENV_FILE.exists():
        log(f"missing test environment file: {TEST_ENV_FILE}")
        return 1

    test_env = parse_env_file(TEST_ENV_FILE)
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(TEST_ENV_FILE),
        "-p",
        PROJECT_NAME,
    ]
    pytest_env = {**os.environ, **test_env}

    try:
        log("step 1/4: start infrastructure (postgres, neo4j, redis, minio)")
        infra = [*compose, "up", "-d", "--build", "--wait", "postgres", "neo4j", "redis", "minio"]
        if run_step(infra) != 0:
            return 1

        log("step 2/4: run migrator")
        if run_step([*compose, "run", "--rm", "--build", "migrator"]) != 0:
            return 1

        log("step 3/4: start api and wait until healthy")
        if run_step([*compose, "up", "-d", "--wait", "api"]) != 0:
            return 1

        log("step 4/4: run integration tests")
        return run_step([sys.executable, "-m", "pytest", "-m", "integration"], env=pytest_env)
    finally:
        log("teardown: docker compose down -v (project karven-test only)")
        subprocess.run([*compose, "down", "-v"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())

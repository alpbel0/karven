"""Unit tests for the beat container liveness helper (no docker required)."""

from __future__ import annotations

import os

from app.fetching import healthcheck

BEAT = b"celery\x00-A\x00app.fetching.celery_app\x00beat\x00--loglevel\x00INFO"
WORKER = b"celery\x00-A\x00app.fetching.celery_app\x00worker\x00-Q\x00fetch"
OWN = b"python\x00-m\x00app.fetching.healthcheck\x00beat"


def _proc(tmp_path, entries):
    root = tmp_path / "proc"
    root.mkdir()
    for pid, cmdline in entries:
        directory = root / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(cmdline)
    return root


def test_no_beat_process_is_not_running(tmp_path) -> None:
    root = _proc(tmp_path, [(1, WORKER), (2, OWN)])

    assert healthcheck.beat_is_running(proc_root=root, own_pid=999) is False


def test_real_beat_process_is_running(tmp_path) -> None:
    root = _proc(tmp_path, [(1, WORKER), (7, BEAT)])

    assert healthcheck.beat_is_running(proc_root=root, own_pid=999) is True


def test_own_pid_is_skipped_even_with_the_beat_argv(tmp_path) -> None:
    root = _proc(tmp_path, [(7, BEAT)])

    assert healthcheck.beat_is_running(proc_root=root, own_pid=7) is False


def test_own_cmdline_form_does_not_match(tmp_path) -> None:
    root = _proc(tmp_path, [(os.getpid(), OWN)])

    assert healthcheck.beat_is_running(proc_root=root) is False


def test_missing_proc_root_is_not_running(tmp_path) -> None:
    assert healthcheck.beat_is_running(proc_root=tmp_path / "missing", own_pid=1) is False


def test_main_exit_codes(monkeypatch) -> None:
    monkeypatch.setattr(healthcheck, "beat_is_running", lambda **kwargs: False)
    assert healthcheck.main(["beat"]) == 1
    monkeypatch.setattr(healthcheck, "beat_is_running", lambda **kwargs: True)
    assert healthcheck.main(["beat"]) == 0
    assert healthcheck.main([]) == 2

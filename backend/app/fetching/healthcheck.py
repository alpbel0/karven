"""Container liveness probes for the fetching services (Task 1.5).

``python -m app.fetching.healthcheck beat`` exits 0 only while a real Celery beat
process is alive. It matches the contiguous argv signature unique to
``celery -A app.fetching.celery_app beat`` (``app.fetching.celery_app`` NUL
``beat``) and skips its own pid, so the healthcheck process can never match
itself. The worker's argv ends in ``worker`` and never matches.
"""

from __future__ import annotations

import os
import pathlib
import sys

#: Contiguous NUL-separated argv fragment unique to the beat command.
BEAT_MARKER = b"app.fetching.celery_app\x00beat"


def _cmdline(path: pathlib.Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def beat_is_running(
    *,
    proc_root: pathlib.Path | str = "/proc",
    own_pid: int | None = None,
) -> bool:
    """True when some process other than ``own_pid`` carries the beat argv."""
    if own_pid is None:
        own_pid = os.getpid()
    root = pathlib.Path(proc_root)
    if not root.is_dir():
        return False
    for entry in root.glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        cmdline = _cmdline(entry / "cmdline")
        if cmdline is not None and BEAT_MARKER in cmdline:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the requested service is alive; non-zero otherwise."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["beat"]:
        return 0 if beat_is_running() else 1
    print("usage: python -m app.fetching.healthcheck beat", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

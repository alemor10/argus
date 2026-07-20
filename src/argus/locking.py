"""One run at a time, per project root.

Two overlapping executions (a manual run racing the cron, or two cron lines
firing together) would interleave SQLite writes, double-post to Discord, and
clobber report files. An OS advisory lock (fcntl.flock, exclusive +
non-blocking) on <root>/.argus.lock serializes them: the second run refuses
crisply instead of corrupting the first. The lock is held for the whole
run — collection through delivery — and releases automatically when the
process exits, however it exits (flock is kernel-owned; a crashed holder
never leaves a stale lock behind).
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILENAME = ".argus.lock"


class LockHeldError(RuntimeError):
    """Another argus process holds the run lock for this project root."""


@contextmanager
def run_lock(root: Path) -> Iterator[None]:
    """Exclusive per-root run lock. Raises LockHeldError (with the holder's
    pid + start time, best-effort) when already held; never blocks."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_FILENAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            holder = os.read(fd, 256).decode("utf-8", "replace").strip()
        except OSError:
            pass
        os.close(fd)
        detail = f" (held by: {holder})" if holder else ""
        raise LockHeldError(
            f"another argus run is in progress — {path} is locked{detail}. "
            "Refusing to overlap; retry when it finishes."
        ) from None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"pid {os.getpid()} since {datetime.now(UTC).isoformat()}".encode())
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

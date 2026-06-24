"""
cli/lock.py — Per-project codeforge run lock.

Prevents two codeforge invocations from racing on the same project directory.
The LOCK file lives at <project_dir>/.codeforge/LOCK.

The lock is an advisory whole-file lock (fcntl.flock) held on an open file
descriptor for the lifetime of the run. The OS releases it automatically when the
process exits — including on crash — so there are no stale locks to reason about,
no PID-reuse false positives, and no acquire-time race. The file's contents
(pid + start time) are written purely for human diagnostics in the "already
running" message.

Linux/Unix only (fcntl). The tool targets Linux.
"""

from __future__ import annotations

import fcntl
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FILE = "LOCK"


class CodeforgeAlreadyRunningError(Exception):
    """Raised when the lock is already held by a live process."""


class CodeforgeLock:
    def __init__(self, project_dir: Path) -> None:
        self._lock_path = project_dir / ".codeforge" / _LOCK_FILE
        self._fd: int | None = None

    def acquire(self) -> None:
        """
        Acquire the lock via a non-blocking exclusive flock.

        - Lock free: take it, stamp pid + start time into the file, keep the fd open.
        - Lock held by a live process: raise CodeforgeAlreadyRunningError.

        A leftover LOCK file from a crashed run is NOT a live lock — flock on it
        succeeds because the dead process's lock was released by the OS.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.read(fd, 256).decode("utf-8", errors="replace").strip()
            os.close(fd)
            raise CodeforgeAlreadyRunningError(
                f"Codeforge already running ({holder or 'unknown'}). "
                f"If you're sure it is not, delete {self._lock_path} and retry."
            )

        # We hold the lock — record our identity for the diagnostic message above.
        os.ftruncate(fd, 0)
        stamp = f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}"
        os.write(fd, stamp.encode("utf-8"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        """Release the lock and remove the LOCK file. Safe to call if never acquired."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._lock_path.unlink(missing_ok=True)

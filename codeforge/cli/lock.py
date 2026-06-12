"""
cli/lock.py — Per-project codeforge run lock.

Prevents two codeforge invocations from racing on the same project directory.
The LOCK file lives at <project_dir>/.codeforge/LOCK and contains the PID of
the running process.

Stale locks (PID no longer alive) are cleared with a warning. Live locks
raise CodeforgeAlreadyRunningError so the user can investigate before retrying.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FILE = "LOCK"


class CodeforgeAlreadyRunningError(Exception):
    """Raised when a live LOCK file is detected."""


class CodeforgeLock:
    def __init__(self, project_dir: Path) -> None:
        self._lock_path = project_dir / ".codeforge" / _LOCK_FILE

    def acquire(self) -> None:
        """
        Acquire the lock.

        - If no LOCK file: write current PID and return.
        - If LOCK file exists with a live PID: raise CodeforgeAlreadyRunningError.
        - If LOCK file exists with a dead PID (stale): log a warning, clear, proceed.
        """
        if self._lock_path.exists():
            raw = self._lock_path.read_text().strip()
            try:
                pid = int(raw)
            except ValueError:
                logger.warning("LOCK file at %s contains non-integer PID %r — clearing", self._lock_path, raw)
                self._lock_path.unlink(missing_ok=True)
            else:
                if _pid_alive(pid):
                    raise CodeforgeAlreadyRunningError(
                        f"Codeforge already running (PID {pid}). "
                        f"If you're sure it is not running, delete {self._lock_path} and retry."
                    )
                logger.warning(
                    "Stale LOCK file (PID %d no longer alive) — clearing and continuing", pid
                )
                self._lock_path.unlink(missing_ok=True)

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        """Remove the LOCK file. Safe to call even if the file is already gone."""
        self._lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it.
        return True

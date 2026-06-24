"""Tests for the per-project run lock — see codeforge/cli/lock.py.

The lock is an advisory fcntl.flock held on an open fd: the OS releases it when the
process dies, so a leftover LOCK file from a crash is not a live lock, and there is no
PID-reuse false positive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeforge.cli.lock import CodeforgeAlreadyRunningError, CodeforgeLock


def test_acquire_creates_lock_and_blocks_second(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    lock1 = CodeforgeLock(project)
    lock1.acquire()
    try:
        assert (project / ".codeforge" / "LOCK").exists()
        # flock is per open-file-description, so a second acquire fails even in-process.
        with pytest.raises(CodeforgeAlreadyRunningError):
            CodeforgeLock(project).acquire()
    finally:
        lock1.release()


def test_reacquire_after_release(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    lock = CodeforgeLock(project)
    lock.acquire()
    lock.release()
    lock2 = CodeforgeLock(project)
    lock2.acquire()  # must succeed — prior lock released
    lock2.release()


def test_leftover_unlocked_lock_file_is_not_live(tmp_path: Path) -> None:
    """A crashed run leaves a LOCK file but no held flock — acquire must succeed
    instead of reporting a phantom 'already running'."""
    codeforge_dir = tmp_path / "p" / ".codeforge"
    codeforge_dir.mkdir(parents=True)
    (codeforge_dir / "LOCK").write_text("pid=99999 started=2020-01-01T00:00:00+00:00")
    lock = CodeforgeLock(tmp_path / "p")
    lock.acquire()
    lock.release()


def test_release_without_acquire_is_safe(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    CodeforgeLock(project).release()  # no fd held — must not raise


def test_lock_file_records_pid(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    lock = CodeforgeLock(project)
    lock.acquire()
    try:
        content = (project / ".codeforge" / "LOCK").read_text()
        assert "pid=" in content and "started=" in content
    finally:
        lock.release()

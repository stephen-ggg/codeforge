"""
tools/readonly.py — The read-only codebase tool implementations.

Pure Python (no ripgrep dependency) so the tools work and unit-test anywhere.
All functions operate within `root` (the source-repo root); read_file/list_dir
additionally jail their path argument. Results are bounded so a tool result can
never blow up the model's context.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from codeforge.tools.jail import JailError, resolve_safe

_MAX_FILE_BYTES = 1_000_000
_MAX_MATCHES = 200
_MAX_READ_LINES = 2000
_SKIP_DIRS = frozenset({
    ".git", ".codeforge", "run-logs", "project-state",
    "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
})


@dataclass
class ToolOutput:
    """Content returned to the model plus a short summary for the event log."""
    content: str
    summary: str


def _is_text(path: Path) -> bool:
    try:
        return b"\x00" not in path.read_bytes()[:4096]
    except OSError:
        return False


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield jailed, text-candidate files under root.

    search_code/find_references do not pass their results through resolve_safe the
    way read_file/list_dir do, so traversal must enforce the jail itself or they
    become a bypass. Two safeguards:
      * os.walk(followlinks=False) never descends into a symlinked directory (so a
        dir symlink pointing outside the repo can't be walked, and symlink loops
        can't hang the scan), and
      * every candidate file is run back through resolve_safe, which skips symlinked
        *files* that escape the root and denied locations (.env, .git, project-state…)
        — the exact set read_file refuses.
    """
    root = Path(root)
    # Resolve the (constant) root once; resolve_safe reuses it per file instead of
    # re-resolving it on every call.
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune skip dirs in place so os.walk never descends into them — which is the
        # only guard needed (the previous per-file rel.parts re-check could never fire,
        # since os.walk never visits a file inside a pruned dir).
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            try:
                # Resolves symlinks and rejects escapes / denied locations (e.g. .env).
                resolve_safe(root, rel.as_posix(), root_resolved=root_resolved)
            except JailError:
                continue
            if not path.is_file():
                continue
            yield path


def search_code(root: Path | str, query: str, glob: str | None = None) -> ToolOutput:
    """Regex-search every text file under root. Returns `path:line: text` matches."""
    root = Path(root)
    try:
        pattern = re.compile(query)
    except re.error as exc:
        return ToolOutput(f"invalid regular expression: {exc}", "regex error")

    matches: list[str] = []
    truncated = False
    for path in _iter_files(root):
        if glob and not path.match(glob):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES or not _is_text(path):
                continue
            rel = path.relative_to(root).as_posix()
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_MATCHES:
                        truncated = True
                        break
        except OSError:
            continue
        if truncated:
            break

    if not matches:
        return ToolOutput("(no matches)", "0 matches")
    text = "\n".join(matches)
    if truncated:
        text += f"\n... (truncated at {_MAX_MATCHES} matches)"
    return ToolOutput(text, f"{len(matches)} match(es)" + (" (truncated)" if truncated else ""))


def find_references(root: Path | str, symbol: str) -> ToolOutput:
    """Word-boundary search for a symbol across the repository."""
    return search_code(root, r"\b" + re.escape(symbol) + r"\b")


def read_file(
    root: Path | str,
    path: str,
    start: int | None = None,
    end: int | None = None,
) -> ToolOutput:
    """Read a text file (optionally a 1-based line range) with line numbers."""
    target = resolve_safe(root, path)  # raises JailError on escape/denied
    if not target.exists() or not target.is_file():
        return ToolOutput(f"file not found: {path}", "not found")
    if target.stat().st_size > _MAX_FILE_BYTES or not _is_text(target):
        return ToolOutput(f"file too large or binary: {path}", "skipped")

    lines = target.read_text(errors="replace").splitlines()
    lo = (start - 1) if start else 0
    hi = end if end else len(lines)
    lo = max(0, lo)
    hi = min(len(lines), hi)
    selected = lines[lo:hi]

    truncated = len(selected) > _MAX_READ_LINES
    if truncated:
        selected = selected[:_MAX_READ_LINES]

    numbered = "\n".join(f"{lo + i + 1}\t{ln}" for i, ln in enumerate(selected))
    summary = f"{len(selected)} line(s) from {path}" + (" (truncated)" if truncated else "")
    return ToolOutput(numbered or "(empty range)", summary)


def list_dir(root: Path | str, path: str = ".") -> ToolOutput:
    """List the entries of a directory (directories suffixed with '/')."""
    root_resolved = Path(root).resolve()
    target = resolve_safe(root, path, root_resolved=root_resolved)  # raises on escape/denied
    if not target.exists() or not target.is_dir():
        return ToolOutput(f"directory not found: {path}", "not found")

    entries: list[str] = []
    for child in sorted(target.iterdir()):
        if child.name in _SKIP_DIRS:
            continue
        try:
            # Skip denied locations (.env) and symlinks escaping the root, so the
            # listing can't even reveal the name of something read_file would refuse.
            resolve_safe(root, str(child), root_resolved=root_resolved)
        except JailError:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
    return ToolOutput("\n".join(entries) or "(empty)", f"{len(entries)} entr(ies)")

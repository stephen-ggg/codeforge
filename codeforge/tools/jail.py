"""
tools/jail.py — Read-only path sandbox for codebase tools.

resolve_safe() resolves a caller-supplied path against the source-repo root and
refuses anything that escapes the root or touches a denied location (.git,
.codeforge/ secrets, run-logs/, project-state/, environment files). The tool
layer ignores the artifact firewall, so this jail is the boundary that keeps a
tool-enabled agent from reading anything it shouldn't.
"""

from __future__ import annotations

from pathlib import Path

# Directory names that must never be read through a tool, even inside the repo.
_DENY_COMPONENTS = frozenset({".git", ".codeforge", "run-logs", "project-state"})


class JailError(Exception):
    """Raised when a path escapes the repo root or hits a denied location."""


def resolve_safe(root: Path | str, rel_path: str) -> Path:
    """Resolve rel_path against root and return it only if it is in-bounds.

    Raises:
        JailError: the path resolves outside root or touches a denied location.
    """
    root_resolved = Path(root).resolve()

    given = Path(rel_path)
    candidate = (
        given.resolve() if given.is_absolute() else (root_resolved / given).resolve()
    )

    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise JailError(f"path escapes repository root: {rel_path!r}")

    rel = candidate.relative_to(root_resolved) if candidate != root_resolved else Path()
    for part in rel.parts:
        if part in _DENY_COMPONENTS:
            raise JailError(f"path touches denied location {part!r}: {rel_path!r}")
        if part == ".env" or part.startswith(".env."):
            raise JailError(f"access to environment files is denied: {rel_path!r}")

    return candidate

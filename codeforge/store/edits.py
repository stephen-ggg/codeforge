"""
store/edits.py — Surgical edit application for continuation runs.

Shared by CommitWriter (writes to the source repo), TestRunner (stages files
into the sandbox), and ContextAssembler (delivers resolved file content to
review agents). For change_type == "modified", the coder emits a list of
Edit{old_string, new_string} instead of a whole-file rewrite; this module
applies them against the on-disk content, failing loudly on a no-match or an
ambiguous match so a bad edit never silently corrupts a file.
"""

from __future__ import annotations

from pathlib import Path

from codeforge.schemas.contracts import CodeArtifact, Edit


class EditError(Exception):
    """Raised when an edit cannot be applied unambiguously."""


def apply_edits(original: str, edits: list[Edit]) -> str:
    """Apply edits in order. Each old_string must match exactly once.

    Raises:
        EditError: an old_string is absent or matches more than once.
    """
    result = original
    for i, edit in enumerate(edits):
        count = result.count(edit.old_string)
        snippet = edit.old_string[:60].replace("\n", "\\n")
        if count == 0:
            raise EditError(f"edit {i}: old_string not found: {snippet!r}")
        if count > 1:
            raise EditError(
                f"edit {i}: old_string is ambiguous ({count} matches): {snippet!r}"
            )
        result = result.replace(edit.old_string, edit.new_string, 1)
    return result


def resolve_code_artifact_edits(artifact: CodeArtifact, source_root: Path) -> CodeArtifact:
    """Return a copy of artifact with edits-only files resolved to full content.

    For any file where change_type == "modified", content == "", and edits is
    non-empty, reads the original from source_root, applies the edits, and
    returns the file with full content populated. Files that already carry full
    content, are new, or are deleted pass through unchanged.

    If the source file is missing or edits cannot be applied cleanly, the file
    passes through as-is so the caller sees an incomplete file rather than a
    crash. This matches the behaviour of CommitWriter and TestRunner, which also
    read-and-apply at point of use.
    """
    resolved = []
    changed = False
    for f in artifact.files:
        if f.change_type == "modified" and f.edits and not f.content:
            src = source_root / f.path
            try:
                original = src.read_text(encoding="utf-8", errors="replace")
                resolved.append(f.model_copy(update={"content": apply_edits(original, f.edits)}))
                changed = True
            except (OSError, EditError):
                resolved.append(f)
        else:
            resolved.append(f)
    return artifact.model_copy(update={"files": resolved}) if changed else artifact

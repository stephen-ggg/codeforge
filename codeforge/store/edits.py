"""
store/edits.py — Surgical edit application for continuation runs.

Shared by CommitWriter (writes to the source repo) and TestRunner (stages files
into the sandbox). For change_type == "modified", the coder emits a list of
Edit{old_string, new_string} instead of a whole-file rewrite; this module
applies them against the on-disk content, failing loudly on a no-match or an
ambiguous match so a bad edit never silently corrupts a file.
"""

from __future__ import annotations

from codeforge.schemas.contracts import Edit


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

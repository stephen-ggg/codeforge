"""
Diff/patch edit semantics for continuation runs.

apply_edits must patch surgically and fail loudly on a no-match or an ambiguous
match, and the CodeFile/Edit schema must reject edits that str.replace would
mishandle (empty old_string) or that don't belong on a non-modified file.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeforge.schemas.contracts import CodeFile, Edit
from codeforge.store.edits import EditError, apply_edits


def test_apply_edits_patches_surgically() -> None:
    original = "def add(a, b):\n    return a + b\n"
    edits = [Edit(old_string="return a + b", new_string="return a + b  # sum")]
    assert apply_edits(original, edits) == "def add(a, b):\n    return a + b  # sum\n"


def test_apply_edits_sequential() -> None:
    original = "x = 1\ny = 2\n"
    edits = [Edit(old_string="x = 1", new_string="x = 10"), Edit(old_string="y = 2", new_string="y = 20")]
    assert apply_edits(original, edits) == "x = 10\ny = 20\n"


def test_apply_edits_no_match_raises() -> None:
    with pytest.raises(EditError, match="not found"):
        apply_edits("a = 1\n", [Edit(old_string="b = 2", new_string="b = 3")])


def test_apply_edits_ambiguous_raises() -> None:
    with pytest.raises(EditError, match="ambiguous"):
        apply_edits("dup\ndup\n", [Edit(old_string="dup", new_string="x")])


def test_edit_rejects_empty_old_string() -> None:
    with pytest.raises(ValidationError):
        Edit(old_string="", new_string="x")


def test_edit_rejects_noop() -> None:
    with pytest.raises(ValidationError):
        Edit(old_string="same", new_string="same")


def test_codefile_rejects_edits_on_new_file() -> None:
    with pytest.raises(ValidationError):
        CodeFile(
            path="src/a.py",
            content="x",
            language="python",
            change_type="new",
            edits=[Edit(old_string="x", new_string="y")],
        )


def test_codefile_allows_edits_on_modified() -> None:
    cf = CodeFile(
        path="src/a.py",
        content="",
        language="python",
        change_type="modified",
        edits=[Edit(old_string="x", new_string="y")],
    )
    assert cf.edits and cf.change_type == "modified"

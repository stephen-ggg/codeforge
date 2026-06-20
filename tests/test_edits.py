"""
Diff/patch edit semantics for continuation runs.

apply_edits must patch surgically and fail loudly on a no-match or an ambiguous
match, and the CodeFile/Edit schema must reject edits that str.replace would
mishandle (empty old_string) or that don't belong on a non-modified file.
resolve_code_artifact_edits must materialise edits-only files using the source
root so review agents always receive complete file bodies.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeforge.schemas.contracts import CodeArtifact, CodeFile, Edit, ModuleInterfaces
from codeforge.store.edits import EditError, apply_edits, resolve_code_artifact_edits


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


# ---------------------------------------------------------------------------
# resolve_code_artifact_edits
# ---------------------------------------------------------------------------

def _make_artifact(files: list[CodeFile]) -> CodeArtifact:
    return CodeArtifact(
        files=files,
        module_interfaces=ModuleInterfaces(files=[]),
        change_summary="test",
        criteria_addressed=[],
        interface_changes=[],
    )


def test_resolve_populates_content_from_disk(tmp_path: "Path") -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\ny = 2\n")

    artifact = _make_artifact([
        CodeFile(
            path="src/a.py",
            content="",
            language="python",
            change_type="modified",
            edits=[Edit(old_string="x = 1", new_string="x = 10")],
        )
    ])

    resolved = resolve_code_artifact_edits(artifact, tmp_path)
    assert resolved.files[0].content == "x = 10\ny = 2\n"


def test_resolve_leaves_full_content_files_unchanged(tmp_path: "Path") -> None:
    artifact = _make_artifact([
        CodeFile(
            path="lib/b.ts",
            content="export const x = 1;",
            language="typescript",
            change_type="new",
        )
    ])
    resolved = resolve_code_artifact_edits(artifact, tmp_path)
    assert resolved.files[0].content == "export const x = 1;"


def test_resolve_passes_through_on_missing_source_file(tmp_path: "Path") -> None:
    artifact = _make_artifact([
        CodeFile(
            path="missing.py",
            content="",
            language="python",
            change_type="modified",
            edits=[Edit(old_string="x", new_string="y")],
        )
    ])
    resolved = resolve_code_artifact_edits(artifact, tmp_path)
    # content stays empty — better than a crash
    assert resolved.files[0].content == ""


def test_resolve_passes_through_on_edit_error(tmp_path: "Path") -> None:
    (tmp_path / "c.py").write_text("z = 3\n")
    artifact = _make_artifact([
        CodeFile(
            path="c.py",
            content="",
            language="python",
            change_type="modified",
            edits=[Edit(old_string="NOT_THERE", new_string="x")],
        )
    ])
    resolved = resolve_code_artifact_edits(artifact, tmp_path)
    assert resolved.files[0].content == ""


def test_resolve_identity_when_nothing_to_resolve(tmp_path: "Path") -> None:
    artifact = _make_artifact([
        CodeFile(path="a.py", content="x = 1\n", language="python", change_type="new"),
        CodeFile(path="b.py", content="", language="python", change_type="deleted"),
    ])
    resolved = resolve_code_artifact_edits(artifact, tmp_path)
    assert resolved is artifact  # no copy when nothing changed

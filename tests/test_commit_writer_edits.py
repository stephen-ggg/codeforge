"""
CommitWriter continuation edit application.

_write_code_artifact must patch existing files surgically for change_type
"modified" with edits, leaving unrelated content untouched, and must fail loudly
when the target file is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeforge.agents.commit_writer import _write_code_artifact
from codeforge.schemas.contracts import CodeArtifact, CodeFile, Edit
from codeforge.store.edits import EditError


def _artifact(files: list[CodeFile]) -> CodeArtifact:
    return CodeArtifact(files=files, change_summary="s", criteria_addressed=[], interface_changes=[])


def test_modified_file_is_patched_in_place(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )

    artifact = _artifact([
        CodeFile(
            path="calc.py",
            content="",
            language="python",
            change_type="modified",
            edits=[Edit(
                old_string="def sub(a, b):\n    return a - b\n",
                new_string="def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
            )],
        )
    ])

    _write_code_artifact(tmp_path, artifact, "src")

    result = (src / "calc.py").read_text()
    assert "def add(a, b):" in result          # untouched
    assert "def mul(a, b):" in result          # added
    assert result.count("def sub") == 1        # not duplicated


def test_modified_missing_file_raises(tmp_path: Path) -> None:
    artifact = _artifact([
        CodeFile(
            path="ghost.py",
            content="",
            language="python",
            change_type="modified",
            edits=[Edit(old_string="a", new_string="b")],
        )
    ])
    with pytest.raises(EditError, match="missing file"):
        _write_code_artifact(tmp_path, artifact, "src")


def test_new_file_written_whole(tmp_path: Path) -> None:
    artifact = _artifact([
        CodeFile(path="new.py", content="print('hi')\n", language="python", change_type="new")
    ])
    _write_code_artifact(tmp_path, artifact, "src")
    assert (tmp_path / "src" / "new.py").read_text() == "print('hi')\n"

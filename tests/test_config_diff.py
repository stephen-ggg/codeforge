"""Tests for diff_config_snapshots — see codeforge/config/config_loader.py.

Pure function: compares two ConfigSnapshot.to_dict() outputs and reports the
field-level deltas the resume config-reconciliation path acts on.
"""

from __future__ import annotations

from codeforge.config.config_loader import diff_config_snapshots


def test_equal_dicts_no_changes() -> None:
    snap = {"retry_limits": {"test_loop": 2}, "global_ceiling": {"max_agent_calls_per_run": 40}}
    assert diff_config_snapshots(snap, dict(snap)) == []


def test_nested_change_reported_by_dotted_path() -> None:
    old = {"retry_limits": {"test_loop": 2, "code_review_loop": 3}}
    new = {"retry_limits": {"test_loop": 3, "code_review_loop": 3}}
    assert diff_config_snapshots(old, new) == [
        {"path": "retry_limits.test_loop", "old": 2, "new": 3}
    ]


def test_schema_version_excluded() -> None:
    old = {"schema_version": "1.0.0", "retry_limits": {"test_loop": 2}}
    new = {"schema_version": "2.0.0", "retry_limits": {"test_loop": 2}}
    assert diff_config_snapshots(old, new) == []


def test_lists_compared_whole() -> None:
    old = {"repos": {"codeforge_state": {"gitignore": ["a"]}}}
    new = {"repos": {"codeforge_state": {"gitignore": ["a", "b"]}}}
    assert diff_config_snapshots(old, new) == [
        {"path": "repos.codeforge_state.gitignore", "old": ["a"], "new": ["a", "b"]}
    ]


def test_key_present_on_one_side_reported_with_none() -> None:
    old: dict = {"tools": {"max_tool_turns": 12}}
    new: dict = {"tools": {"max_tool_turns": 12}, "extra": 1}
    assert diff_config_snapshots(old, new) == [{"path": "extra", "old": None, "new": 1}]

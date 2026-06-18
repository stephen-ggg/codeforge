"""
Stack-profile tests.

Covers the declarative stack-profile abstraction and the profile-driven seams in the
mechanical/routing layers. No Docker needed — the pure helpers are exercised directly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from codeforge.agents import test_runner as tr_mod
from codeforge.agents.test_runner import (
    TestRunner,
    _error_result,
    _expand_globs,
    _match_case_id,
    _runtime_version,
)
from codeforge.config.config_loader import load_config
from codeforge.orchestrator.routing import route_test_analysis_recoverable_error
from codeforge.orchestrator.state_machine import (
    _STACK_FRAGMENT_FOR_AGENT,
    _build_dep_fix_context,
)
from codeforge.schemas.contracts import RetryCounters
from codeforge.stacks.registry import available_profiles, get_profile


# --- Registry / profile ----------------------------------------------------

def test_both_builtin_profiles_present() -> None:
    assert set(available_profiles()) == {"python", "nextjs-supabase"}


def test_unknown_profile_raises_with_known_list() -> None:
    with pytest.raises(ValueError, match="Unknown stack profile"):
        get_profile("rust-actix")


def test_python_profile_reproduces_original_behaviour() -> None:
    p = get_profile("python")
    assert p.manifest_filename == "requirements.txt"
    assert p.default_sandbox_image == "python:3.12-slim"
    assert "pytest" in p.test_command
    assert p.test_manifest_filename == "requirements-test.txt"
    assert p.build_commands == []


def test_nextjs_profile_shape_and_fragments() -> None:
    p = get_profile("nextjs-supabase")
    assert p.manifest_filename == "package.json"
    assert p.default_sandbox_image == "node:20-bookworm"
    assert any("tsc" in c for c in p.build_commands)         # type-check gate
    assert "vitest" in p.test_command
    assert p.test_manifest_filename is None                  # single manifest
    assert {d["domain"] for d in p.seed_tech_decisions} == {"language", "framework", "database"}
    for key in ("coder", "test_designer", "reviewer", "architecture"):
        assert p.prompt_fragment(key), f"missing {key} fragment"


# --- Config wiring ----------------------------------------------------------

def test_config_defaults_to_python() -> None:
    snap = load_config(tempfile.mkdtemp(), require_env_vars=False)
    assert snap.stack_profile.id == "python"
    assert snap.test_runner.sandbox_image == "python:3.12-slim"  # filled from profile default


def test_config_selects_nextjs_and_fills_sandbox_image() -> None:
    d = Path(tempfile.mkdtemp())
    (d / ".codeforge").mkdir()
    (d / ".codeforge" / "codeforge.config.yaml").write_text("stack:\n  profile: nextjs-supabase\n")
    snap = load_config(d, require_env_vars=False)
    assert snap.stack_profile.id == "nextjs-supabase"
    assert snap.test_runner.sandbox_image == "node:20-bookworm"
    # to_dict carries the manifest filename so the dict-based validator can read it.
    assert snap.to_dict()["stack_profile"]["manifest_filename"] == "package.json"


# --- Profile-driven test_runner helpers -------------------------------------

def test_runtime_version_uses_profile_regex() -> None:
    assert _runtime_version("platform -- Python 3.12.13", r"Python (\d+\.\d+\.\d+)") == "3.12.13"
    assert _runtime_version("v20.11.0\n", r"v(\d+\.\d+\.\d+)") == "20.11.0"
    assert _runtime_version("anything", None) == ""


def test_expand_globs_recursive_literal_and_nested() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "lib").mkdir()
    (root / "lib" / "cards.ts").write_text("x")
    # The common Next.js case: a file two levels deep (app/api/cards/route.ts).
    (root / "app" / "api" / "cards").mkdir(parents=True)
    (root / "app" / "api" / "cards" / "route.ts").write_text("x")
    (root / "package.json").write_text("{}")
    found = set(_expand_globs(root, ["lib/**", "app/**", "package.json", "missing.txt"]))
    assert "lib/cards.ts" in found
    assert "app/api/cards/route.ts" in found       # nested path is staged
    assert "package.json" in found
    assert "missing.txt" not in found


def test_match_case_id_handles_pytest_and_vitest() -> None:
    lookup = {"tests/test_add.py": "TC-1", "lib/cards.test.ts": "TC-2"}
    # pytest dotted classname → path transform
    assert _match_case_id("tests.test_add", "test_x", lookup) == "TC-1"
    # vitest file-path classname → raw match
    assert _match_case_id("lib/cards.test.ts", "creates a card", lookup) == "TC-2"
    # no match → fallback
    assert _match_case_id("unknown", "t", lookup) == "unknown::t"


# --- source-tree assembly failure (B-1 regression) --------------------------

def test_error_result_allows_unclassified_phase() -> None:
    # _error_result must be callable without an error_phase — the source-assembly failure
    # path does exactly this. A required positional there was a guaranteed TypeError.
    res = _error_result("t0", "img", stderr="boom")
    assert res.overall_status == "error"
    assert res.error_phase is None


def test_run_returns_error_when_source_assembly_raises(monkeypatch) -> None:
    # When _resolve_code_entries raises (e.g. an EditError in continuation), run() must
    # return an error result, not raise — and it must do so before touching Docker.
    from codeforge.schemas.contracts import TestRunnerInput, TestSuite, CodeArtifact

    def _boom(_input, _profile):
        raise RuntimeError("edit conflict")

    monkeypatch.setattr(tr_mod, "_resolve_code_entries", _boom)

    snap = load_config(tempfile.mkdtemp(), require_env_vars=False)
    runner = TestRunner(snap)
    inp = TestRunnerInput(
        test_suite=TestSuite(test_cases=[], test_infrastructure=[], coverage_map=[]),
        code_artifact=CodeArtifact(files=[], change_summary="s", criteria_addressed=[], interface_changes=[]),
        run_config={},
    )
    res = runner.run(inp)
    assert res.overall_status == "error"
    assert "edit conflict" in res.stderr_tail


# --- build_failed routing ---------------------------------------------------

def test_build_failed_routes_back_to_coder() -> None:
    cfg = {"retry_limits": {"dependency_repair": 2}}
    out = route_test_analysis_recoverable_error("build_failed", RetryCounters(), cfg)
    assert out is not None
    assert out.decision == "retry_same_agent"
    assert out.next_state == "coding"


def test_dep_fix_context_distinguishes_build_from_dep() -> None:
    class _Build:
        error_phase = "build_failed"
        stderr_tail = "TS2345"

    class _Dep:
        error_phase = "runtime_dep_install_failed"
        stderr_tail = "npm err"

    assert _build_dep_fix_context(_Build())["trigger"] == "build_error"
    assert _build_dep_fix_context(_Dep())["trigger"] == "runtime_dep_error"


# --- Stack guidance fan-out -------------------------------------------------

def test_stack_fragment_mapping_covers_implementation_agents() -> None:
    assert _STACK_FRAGMENT_FOR_AGENT == {
        "architecture_designer": "architecture",
        "coder": "coder",
        "code_reviewer": "reviewer",
        "security_reviewer": "reviewer",
        "test_designer": "test_designer",
    }


def test_inject_stack_guidance_lands_fragment_in_context() -> None:
    # The fragment text the profile returns must reach the agent's user turn under the
    # _stack_guidance key. Verified without constructing a full StateMachine.
    from types import SimpleNamespace

    from codeforge.orchestrator.state_machine import StateMachine

    profile = SimpleNamespace(prompt_fragment=lambda key: f"GUIDANCE::{key}")
    fake_self = SimpleNamespace(_config=SimpleNamespace(stack_profile=profile))
    pkg = SimpleNamespace(state_documents={})

    StateMachine._inject_stack_guidance(fake_self, pkg, "coder")
    assert pkg.state_documents["_stack_guidance"] == "GUIDANCE::coder"

    # An agent with no mapping (test_analyst) gets nothing injected.
    pkg2 = SimpleNamespace(state_documents={})
    StateMachine._inject_stack_guidance(fake_self, pkg2, "test_analyst")
    assert "_stack_guidance" not in pkg2.state_documents

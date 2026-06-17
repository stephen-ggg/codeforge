"""
Read-only codebase tool tests.

Covers the two enforcement boundaries that keep continuation-mode tools safe:
  1. Path jail — escapes and denied locations are refused.
  2. Per-agent allowlist — the blind set (test_designer/test_analyst) gets no
     tools, and a forbidden agent that somehow reaches the executor is denied.
Plus the audit guarantee: every tool call emits a ToolCallEvent AND an
AccessEvent — for allowed and denied calls alike.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeforge.firewall.manifest import load_manifest
from codeforge.schemas.contracts import AccessEvent, CountersSnapshot
from codeforge.tools.executor import TOOL_SCHEMAS, ToolExecutor
from codeforge.tools.jail import JailError, resolve_safe
from codeforge.tools.readonly import list_dir, read_file, search_code


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
    )
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / ".env").write_text("SECRET=abc123")
    (tmp_path / ".codeforge").mkdir()
    (tmp_path / ".codeforge" / "secrets.yaml").write_text("token: nope")
    return tmp_path


# --------------------------------------------------------------------------- #
# Jail
# --------------------------------------------------------------------------- #

def test_jail_allows_in_bounds(repo: Path) -> None:
    assert resolve_safe(repo, "src/calc.py") == (repo / "src" / "calc.py").resolve()


@pytest.mark.parametrize("bad", ["../outside.txt", "../../etc/passwd", "/etc/passwd"])
def test_jail_rejects_escape(repo: Path, bad: str) -> None:
    with pytest.raises(JailError):
        resolve_safe(repo, bad)


@pytest.mark.parametrize("denied", [".env", ".codeforge/secrets.yaml", "run-logs/x", "project-state/y", ".git/config"])
def test_jail_rejects_denied_locations(repo: Path, denied: str) -> None:
    with pytest.raises(JailError):
        resolve_safe(repo, denied)


# --------------------------------------------------------------------------- #
# Read-only functions
# --------------------------------------------------------------------------- #

def test_search_code_finds_matches(repo: Path) -> None:
    out = search_code(repo, r"def \w+")
    assert "src/calc.py" in out.content
    assert "2 match" in out.summary


def test_read_file_with_range(repo: Path) -> None:
    out = read_file(repo, "src/calc.py", start=1, end=2)
    assert "def add" in out.content
    assert "subtract" not in out.content


def test_list_dir_skips_nothing_visible(repo: Path) -> None:
    out = list_dir(repo, "src")
    assert "calc.py" in out.content


# --------------------------------------------------------------------------- #
# Executor — allowlist + audit
# --------------------------------------------------------------------------- #

class FakeEventLog:
    def __init__(self) -> None:
        self.tool_calls: list[dict] = []
        self.access_events: list[AccessEvent] = []

    def emit_tool_call(self, **kwargs) -> None:  # noqa: ANN003
        self.tool_calls.append(kwargs)

    def emit_access_event(self, event: AccessEvent) -> None:
        self.access_events.append(event)


def _executor(repo: Path, agent_id: str, sink: FakeEventLog) -> ToolExecutor:
    return ToolExecutor(
        root=repo,
        agent_id=agent_id,  # type: ignore[arg-type]
        manifest=load_manifest(),
        event_log=sink,
        counters=CountersSnapshot(),
        assembly_id="asm-1",
    )


def test_blind_agents_get_no_tools(repo: Path) -> None:
    sink = FakeEventLog()
    for blind in ("test_designer", "test_analyst", "requirements_analyst"):
        assert _executor(repo, blind, sink).tool_schemas() is None


def test_enabled_agent_gets_tools(repo: Path) -> None:
    assert _executor(repo, "coder", FakeEventLog()).tool_schemas() == TOOL_SCHEMAS


def test_forbidden_agent_call_is_denied_and_logged(repo: Path) -> None:
    sink = FakeEventLog()
    ex = _executor(repo, "test_designer", sink)
    result = ex.execute("read_file", {"path": "src/calc.py"}, litellm_call_id="c1")

    assert "DENIED" in result
    assert len(sink.tool_calls) == 1 and sink.tool_calls[0]["decision"] == "deny"
    assert len(sink.access_events) == 1 and sink.access_events[0].decision == "deny"
    assert len(ex.access_events) == 1


def test_allowed_call_executes_and_logs(repo: Path) -> None:
    sink = FakeEventLog()
    ex = _executor(repo, "coder", sink)
    result = ex.execute("read_file", {"path": "src/calc.py"}, litellm_call_id="c2")

    assert "def add" in result
    assert sink.tool_calls[0]["decision"] == "allow"
    assert sink.tool_calls[0]["litellm_call_id"] == "c2"
    assert sink.access_events[0].decision == "allow"


def test_jail_escape_via_tool_is_denied_and_logged(repo: Path) -> None:
    sink = FakeEventLog()
    ex = _executor(repo, "coder", sink)
    result = ex.execute("read_file", {"path": "../../etc/passwd"}, litellm_call_id="c3")

    assert "DENIED" in result
    assert sink.tool_calls[0]["decision"] == "deny"
    assert sink.access_events[0].decision == "deny"


def test_secret_read_via_tool_is_denied(repo: Path) -> None:
    sink = FakeEventLog()
    ex = _executor(repo, "coder", sink)
    assert "DENIED" in ex.execute("read_file", {"path": ".env"}, litellm_call_id="c4")
    assert sink.tool_calls[0]["decision"] == "deny"

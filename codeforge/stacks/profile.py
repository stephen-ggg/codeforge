"""
stacks/profile.py — The StackProfile model.

A StackProfile is the single source of truth for everything that used to be hardcoded
to Python in the mechanical (test_runner) and prompt layers. It is loaded from a YAML
file under stacks/profiles/<id>.yaml; the matching prompt fragments live alongside it
under stacks/profiles/<id>/prompts/<agent>.md.

The profile is resolved at config-load time and stamped onto ConfigSnapshot, so every
agent invocation and the mechanical test runner read the same immutable profile for the
duration of a run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr


class StackProfile(BaseModel):
    """Declarative description of a target tech stack.

    Every field that the test runner or an agent prompt needs in order to target a
    language/framework lives here, so adding a stack is data, not code.
    """

    id: str                                     # "python", "nextjs-supabase"
    language_label: str                         # human label injected into prompts ("Python 3.12", "TypeScript")

    # --- Mechanical (test_runner) ---
    default_sandbox_image: str                  # used when test_runner.sandbox_image is unset
    manifest_filename: str                      # dependency manifest the coder must emit ("requirements.txt", "package.json")
    manifest_required: bool = True              # gate: fail the coder if the manifest is absent

    install_commands: list[str] = Field(default_factory=list)       # runtime deps; fail-fast; always run
    test_manifest_filename: str | None = None                       # extra test-only manifest (Python's requirements-test.txt)
    test_install_commands: list[str] = Field(default_factory=list)  # run only when test_manifest_filename is staged
    build_commands: list[str] = Field(default_factory=list)         # compile/type-check gate; runs after install, before tests

    test_command: str = ""                      # the command that runs the suite and emits a JUnit report
    results_path: str = "/workspace/results.xml"  # where test_command writes the JUnit XML
    workdir: str = "/workspace"                 # working dir for all container commands

    source_globs: list[str] = Field(default_factory=list)  # dirs/patterns to stage from source_root on continuation
    runtime_version_regex: str | None = None    # best-effort runtime version, matched against tool stdout

    # --- Decision layer ---
    seed_tech_decisions: list[dict[str, Any]] = Field(default_factory=list)  # locked TechDecisions pre-seeded for the stack

    # Directory containing this profile's prompt fragments (set by the registry at load time).
    _profile_dir: Path | None = PrivateAttr(default=None)

    def prompt_fragment(self, agent_key: str) -> str | None:
        """Return the stack guidance fragment for an agent, or None if the profile has none.

        agent_key is one of: coder, test_designer, reviewer, architecture.
        Reviewers (code + security) share the single `reviewer` fragment.
        """
        if self._profile_dir is None:
            return None
        path = self._profile_dir / "prompts" / f"{agent_key}.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def bind_dir(self, profile_dir: Path) -> "StackProfile":
        """Attach the on-disk profile directory so prompt fragments can be resolved."""
        self._profile_dir = profile_dir
        return self

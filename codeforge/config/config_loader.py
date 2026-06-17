"""
config/config_loader.py — Codeforge configuration loader.

Responsibilities:
  - Load codeforge/config/codeforge.config.yaml (installation default)
  - Load <project_dir>/.codeforge/codeforge.config.yaml (project-local)
  - Deep merge: project-local wins on any key present in both
  - Validate required fields
  - Read required env vars (fail fast if absent)
  - Return a typed ConfigSnapshot

The resolved snapshot is stamped immutably onto CodeforgeRun.config_snapshot at run start.
Changing the config mid-run has no effect.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Typed config shapes
# ---------------------------------------------------------------------------

class RetryLimitsConfig(BaseModel):
    code_review_loop: int = 3
    security_review_loop: int = 3
    test_loop: int = 2
    coder_validation_retries: int = 2
    architecture_validation_retries: int = 2
    infrastructure_retries: int = 3
    malformed_output_retries: int = 2
    codeforge_state_commit: int = 3
    source_code_commit: int = 3


class GlobalCeilingConfig(BaseModel):
    max_agent_calls_per_run: int = 40


class TestRunnerConfig(BaseModel):
    timeout_seconds: int = 300
    environment_vars: dict[str, str] = Field(default_factory=dict)
    sandbox_image: str = ""


class ThinkingConfig(BaseModel):
    """Extended-thinking settings for one agent.

    enabled=True is honoured only for Anthropic models (model string starting
    'claude-'); the router falls back to a <thinking> scratchpad for other providers.
    """
    enabled: bool = False
    budget_tokens: int = 8000


class AgentConfig(BaseModel):
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    fallback_model: str | None = None
    system_prompt: str = ""
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    metadata: dict[str, str] = Field(default_factory=dict)


class CodeforgeStateRepoConfig(BaseModel):
    remote: str
    branch: str = "main"
    gitignore: list[str] = Field(default_factory=list)


class SourceCodeRepoConfig(BaseModel):
    path: str
    remote: str
    default_branch: str = "main"
    branch_prefix: str = "codeforge/"
    pr_target: str = "main"
    auto_merge: bool = True
    output_dir: str = "src"


class ReposConfig(BaseModel):
    codeforge_state: CodeforgeStateRepoConfig
    source_code: SourceCodeRepoConfig


class ToolsConfig(BaseModel):
    """Read-only codebase tool settings (continuation runs)."""
    max_tool_turns: int = 12        # caps the per-agent-invocation tool loop


class ConfigSnapshot(BaseModel):
    """
    The immutable config snapshot stamped onto CodeforgeRun at run start.
    Mirrors CodeforgeRun.config_snapshot.
    """
    name: str
    schema_version: str
    retry_limits: RetryLimitsConfig
    global_ceiling: GlobalCeilingConfig
    confidence_thresholds: dict[str, float]
    test_runner: TestRunnerConfig
    agents: dict[str, AgentConfig]
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    repos: ReposConfig | None = None        # None in codeforge default; required in project config

    # Resolved at load time from environment
    anthropic_api_key: str = Field(default="", exclude=True)  # never serialised
    github_token: str = Field(default="", exclude=True)       # never serialised

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable dict suitable for CodeforgeRun.config_snapshot."""
        return self.model_dump(exclude={"anthropic_api_key", "github_token"})


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

# Path to the codeforge installation's default config, relative to this file
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "codeforge.config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file. Returns empty dict if file does not exist."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        result = yaml.safe_load(fh)
        return result if isinstance(result, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Deep merge two dicts. Override wins on any key present in both.
    Lists are replaced (not concatenated).
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(
    project_dir: str | Path,
    *,
    require_sandbox_image: bool = False,
    require_repos: bool = False,
    require_env_vars: bool = True,
) -> ConfigSnapshot:
    """
    Load and merge codeforge configuration.

    Args:
        project_dir: Path to the managed project directory (contains .codeforge/).
        require_sandbox_image: If True, raise ValueError when sandbox_image is not set.
            Defaults False so Stage 1 tests pass without a full deployment environment.
        require_repos: If True, raise ValueError when the repos block is absent.
            Set True in production; False for unit tests.
        require_env_vars: If True, raise EnvironmentError when required env vars are absent.
            Set False in unit tests that don't make LLM or GitHub calls.

    Returns:
        ConfigSnapshot: Fully resolved, typed configuration snapshot.

    Raises:
        EnvironmentError: Required env var absent (when require_env_vars=True).
        ValueError: Required config field missing or invalid.
        FileNotFoundError: Default config file not found (codeforge installation error).
    """
    project_dir = Path(project_dir)

    # 1. Load installation default
    if not _DEFAULT_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Codeforge default config not found at {_DEFAULT_CONFIG_PATH}. "
            "Is codeforge installed correctly?"
        )
    default_config = _load_yaml(_DEFAULT_CONFIG_PATH)

    # 2. Load project-local config (optional — project may not exist yet during init)
    project_config_path = project_dir / ".codeforge" / "codeforge.config.yaml"
    project_config = _load_yaml(project_config_path)

    # 3. Deep merge — project-local wins
    merged = _deep_merge(default_config, project_config)

    # 4. Parse into typed snapshot
    try:
        snapshot = _parse_config(merged)
    except Exception as exc:
        raise ValueError(f"Config validation error: {exc}") from exc

    # 5. Validate sandbox_image if required
    if require_sandbox_image and not snapshot.test_runner.sandbox_image:
        raise ValueError(
            "test_runner.sandbox_image must be set in the project config before running. "
            "Set it in .codeforge/codeforge.config.yaml."
        )

    # 6. Validate repos block if required
    if require_repos and snapshot.repos is None:
        raise ValueError(
            "The 'repos' block must be set in .codeforge/codeforge.config.yaml. "
            "Both codeforge_state.remote and source_code.remote are required."
        )

    # 7. Read required env vars
    if require_env_vars:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        github_token = os.environ.get("PIPELINE_GITHUB_TOKEN", "")

        missing = []
        if not anthropic_key:
            missing.append("ANTHROPIC_API_KEY")
        if not github_token:
            missing.append("PIPELINE_GITHUB_TOKEN")

        if missing:
            raise EnvironmentError(
                f"Required environment variable(s) not set: {', '.join(missing)}. "
                "Export them before running codeforge."
            )

        snapshot.anthropic_api_key = anthropic_key
        snapshot.github_token = github_token
    else:
        snapshot.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        snapshot.github_token = os.environ.get("PIPELINE_GITHUB_TOKEN", "")

    return snapshot


def _parse_config(raw: dict[str, Any]) -> ConfigSnapshot:
    """Parse a merged raw config dict into a typed ConfigSnapshot."""
    retry_limits = RetryLimitsConfig(**raw.get("retry_limits", {}))
    global_ceiling = GlobalCeilingConfig(**raw.get("global_ceiling", {}))
    confidence_thresholds: dict[str, float] = raw.get("confidence_thresholds", {})
    test_runner = TestRunnerConfig(**raw.get("test_runner", {}))

    raw_agents = raw.get("agents", {})
    agents: dict[str, AgentConfig] = {
        agent_id: AgentConfig(**agent_conf)
        for agent_id, agent_conf in raw_agents.items()
    }

    tools = ToolsConfig(**raw.get("tools", {}))

    repos: ReposConfig | None = None
    if "repos" in raw and raw["repos"]:
        raw_repos = raw["repos"]
        repos = ReposConfig(
            codeforge_state=CodeforgeStateRepoConfig(**raw_repos.get("codeforge_state", {})),
            source_code=SourceCodeRepoConfig(**raw_repos.get("source_code", {})),
        )

    return ConfigSnapshot(
        name=raw.get("name", "codeforge-v1"),
        schema_version=raw.get("schema_version", "1.0.0"),
        retry_limits=retry_limits,
        global_ceiling=global_ceiling,
        confidence_thresholds=confidence_thresholds,
        test_runner=test_runner,
        agents=agents,
        tools=tools,
        repos=repos,
    )

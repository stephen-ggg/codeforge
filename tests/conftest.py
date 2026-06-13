from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeforge.config.config_loader import ConfigSnapshot


@pytest.fixture
def minimal_config() -> ConfigSnapshot:
    path = Path(__file__).parent.parent / "codeforge" / "config" / "codeforge.config.yaml"
    with path.open() as f:
        return ConfigSnapshot(**yaml.safe_load(f))


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Root of the managed project. ProjectStateStore targets project_dir/project-state/."""
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture
def run_log_dir(tmp_path: Path) -> Path:
    """Root of run-logs/. ArtifactStore and EventLog write under run_log_dir/<run_id>/."""
    d = tmp_path / "run-logs"
    d.mkdir()
    return d

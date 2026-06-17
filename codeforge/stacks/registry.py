"""
stacks/registry.py — Built-in stack profile loader.

Profiles are data files under stacks/profiles/<id>.yaml. get_profile(id) parses one into
a StackProfile and binds its on-disk directory so prompt fragments resolve.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codeforge.stacks.profile import StackProfile

_PROFILES_DIR = Path(__file__).parent / "profiles"

DEFAULT_PROFILE_ID = "python"


def available_profiles() -> list[str]:
    """List the ids of all built-in profiles (one <id>.yaml per profile)."""
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))


def get_profile(profile_id: str) -> StackProfile:
    """Load a stack profile by id.

    Raises ValueError with the list of known profiles if the id is unknown, so a typo in
    `stack.profile` fails fast at config load rather than as an opaque runner error later.
    """
    yaml_path = _PROFILES_DIR / f"{profile_id}.yaml"
    if not yaml_path.is_file():
        known = ", ".join(available_profiles()) or "(none)"
        raise ValueError(
            f"Unknown stack profile {profile_id!r}. Known profiles: {known}. "
            f"Set stack.profile in .codeforge/codeforge.config.yaml."
        )
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    profile = StackProfile(**raw)
    # Bind the profile directory (profiles/<id>/) for prompt-fragment resolution.
    return profile.bind_dir(_PROFILES_DIR / profile_id)

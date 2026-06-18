"""
stacks/ — Declarative tech-stack profiles.

A StackProfile captures everything the pipeline previously hardcoded for Python:
the sandbox image, dependency manifest, install/build/test commands, source layout,
and the per-agent prompt guidance that makes the LLM agents target that stack.

The default profile is `python`, which reproduces the original Python-only behaviour
byte-for-byte. New stacks (e.g. `nextjs-supabase`) are added as data — a YAML profile
plus a directory of prompt fragments — with no changes to the orchestrator core.
"""

from codeforge.stacks.profile import StackProfile
from codeforge.stacks.registry import get_profile

__all__ = ["StackProfile", "get_profile"]

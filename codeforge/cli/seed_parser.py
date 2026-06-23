"""
cli/seed_parser.py — Best-effort extractor for .dc.html design files.

Produces a human-reviewable UIDesignState draft. Missing or ambiguous values
are scaffolded with "TODO: fill in" placeholders. The human MUST review and
edit the output before committing.
"""

from __future__ import annotations

import re
from pathlib import Path

from codeforge.schemas.contracts import (
    ComponentSpec,
    DesignToken,
    PhaseColor,
    UIDesignState,
)

_PLACEHOLDER = "TODO: fill in"


class SeedParser:
    def __init__(self, html_path: Path) -> None:
        self._path = html_path
        self._html = html_path.read_text(encoding="utf-8")

    def parse(self) -> UIDesignState:
        if "<x-dc>" not in self._html:
            raise ValueError(
                f"{self._path.name!r} does not appear to be a .dc.html file "
                "(no <x-dc> root element found)"
            )

        script = self._extract_script()
        style = self._extract_style()

        phase_colors = self._extract_phase_colors(script)
        design_tokens = self._extract_design_tokens(style, script)
        font_family = self._extract_font_family(style)
        components = self._extract_components()

        return UIDesignState(
            schema_version="1.0.0",
            design_source=self._path.name,
            seeded_at="seed",
            last_updated_run="seed",
            design_tokens=design_tokens,
            phase_colors=phase_colors,
            font_family=font_family,
            components=components,
        )

    def _extract_script(self) -> str:
        m = re.search(
            r'<script[^>]*type=["\']text/x-dc["\'][^>]*>(.*?)</script>',
            self._html,
            re.DOTALL,
        )
        return m.group(1) if m else ""

    def _extract_style(self) -> str:
        # Collect ALL <style> blocks — a .dc.html may have vendor resets before
        # the main design stylesheet.
        blocks = re.findall(r'<style[^>]*>(.*?)</style>', self._html, re.DOTALL)
        return "\n".join(blocks)

    @staticmethod
    def _balanced_block(text: str, start: int) -> str | None:
        """
        Return content within balanced braces starting just after the opening `{`
        at `start`. Returns None when braces are unbalanced (malformed/truncated input).
        """
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        if depth != 0:
            return None
        return text[start:i - 1]

    def _extract_phase_colors(self, script: str) -> list[PhaseColor]:
        """Extract phase colors from PH = { key: { name: '...', color: '...' }, ... }."""
        m = re.search(r'PH\s*=\s*\{', script)
        if not m:
            return []

        ph_block = self._balanced_block(script, m.end())
        if ph_block is None:
            return []  # malformed PH object — skip

        results: list[PhaseColor] = []
        for entry_m in re.finditer(r'(\w+)\s*:\s*\{', ph_block):
            key = entry_m.group(1)
            entry_block = self._balanced_block(ph_block, entry_m.end())
            if entry_block is None:
                continue
            color_m = re.search(r"color\s*:\s*['\"]([^'\"]+)['\"]", entry_block)
            if color_m:
                results.append(PhaseColor(phase_id=key, color=color_m.group(1)))

        return results

    def _extract_design_tokens(self, style: str, script: str) -> list[DesignToken]:
        tokens: list[DesignToken] = []

        # Strip CSS block comments before matching — /* ... */ comments would
        # otherwise match the custom-property regex and inject phantom tokens.
        style_clean = re.sub(r'/\*.*?\*/', '', style, flags=re.DOTALL)

        # CSS custom properties: --name: value
        for m in re.finditer(r'--([a-zA-Z0-9_-]+)\s*:\s*([^;}\n]+)', style_clean):
            tokens.append(DesignToken(
                name=m.group(1).strip(),
                value=m.group(2).strip(),
                usage=_PLACEHOLDER,
            ))

        # Named color constants in script: ERR = '...' and ORCH = '...'
        for const in ("ERR", "ORCH"):
            cm = re.search(rf"\b{const}\b\s*=\s*['\"]([^'\"]+)['\"]", script)
            if cm:
                tokens.append(DesignToken(
                    name=const.lower(),
                    value=cm.group(1),
                    usage=_PLACEHOLDER,
                ))

        return tokens

    def _extract_font_family(self, style: str) -> str:
        m = re.search(r'font-family\s*:\s*([^;}\n]+)', style)
        if m:
            return m.group(1).strip().rstrip(",")
        return _PLACEHOLDER

    def _extract_components(self) -> list[ComponentSpec]:
        """Scaffold components from HTML comment markers in the .dc.html."""
        components: list[ComponentSpec] = []

        # Look for <!-- ============ NAME ============ --> style markers
        for m in re.finditer(
            r'<!--\s*={4,}\s*([A-Z][A-Z ]+?)\s*={4,}\s*-->',
            self._html,
        ):
            raw_name = m.group(1).strip()
            # Convert "PHASE RAIL" → "PhaseRail"
            comp_id = "".join(word.capitalize() for word in raw_name.split())
            components.append(ComponentSpec(
                id=comp_id,
                status="not_started",
                description=_PLACEHOLDER,
                props=[_PLACEHOLDER],
                data_dependencies=[_PLACEHOLDER],
                interactions=[_PLACEHOLDER],
                notes=_PLACEHOLDER,
            ))

        if not components:
            # Fallback: scaffold a single placeholder component
            components.append(ComponentSpec(
                id="TODO_ComponentName",
                status="not_started",
                description=_PLACEHOLDER,
                props=[_PLACEHOLDER],
                data_dependencies=[_PLACEHOLDER],
                interactions=[_PLACEHOLDER],
                notes=_PLACEHOLDER,
            ))

        return components

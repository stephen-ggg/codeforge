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
        m = re.search(r'<style[^>]*>(.*?)</style>', self._html, re.DOTALL)
        return m.group(1) if m else ""

    def _extract_phase_colors(self, script: str) -> list[PhaseColor]:
        """Extract phase colors from PH = { key: { name: '...', color: '...' }, ... }."""
        # Locate the PH assignment block by finding PH = { and then collecting
        # up to the matching closing } using brace depth tracking.
        m = re.search(r'PH\s*=\s*\{', script)
        if not m:
            return []

        start = m.end()
        depth = 1
        i = start
        while i < len(script) and depth > 0:
            if script[i] == '{':
                depth += 1
            elif script[i] == '}':
                depth -= 1
            i += 1
        block = script[start:i - 1]

        results: list[PhaseColor] = []
        for entry in re.finditer(
            r"(\w+)\s*:\s*\{[^}]*color\s*:\s*['\"]([^'\"]+)['\"]",
            block,
        ):
            results.append(PhaseColor(phase_id=entry.group(1), color=entry.group(2)))

        return results

    def _extract_design_tokens(self, style: str, script: str) -> list[DesignToken]:
        tokens: list[DesignToken] = []

        # CSS custom properties: --name: value
        for m in re.finditer(r'--([a-zA-Z0-9_-]+)\s*:\s*([^;}\n]+)', style):
            tokens.append(DesignToken(
                name=m.group(1).strip(),
                value=m.group(2).strip(),
                usage=_PLACEHOLDER,
            ))

        # Named constants in script: ERR = '...' and ORCH = '...'
        for const in ("ERR", "ORCH"):
            m = re.search(rf"{const}\s*=\s*['\"]([^'\"]+)['\"]", script)
            if m:
                tokens.append(DesignToken(
                    name=const.lower(),
                    value=m.group(1),
                    usage=_PLACEHOLDER,
                ))

        # Body background from inline style
        m = re.search(r'background\s*:\s*(#[0-9a-fA-F]+)', style)
        if m:
            tokens.append(DesignToken(
                name="bg_base",
                value=m.group(1),
                usage="Page background",
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

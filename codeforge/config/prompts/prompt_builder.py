"""
prompts/prompt_builder.py — Deterministic prompt renderer.

Composes each agent's runtime system prompt from four sources so that no part of
the prompt is hand-maintained twice:

  partials/envelope.md      shared envelope rules (composed verbatim)
  agents/<id>/body.md       agent-specific behavioural content (hand-written)
  agents/<id>/example.json  one realistic, VALID example output (hand-written, but
                            validated against the Pydantic model at build time, so it
                            cannot silently drift from the schema)
  schemas/contracts.py      the output model → generated field reference + the input
                            tag section (from manifest.yaml)

Output: prompts/rendered/<id>.md — the file the runtime actually loads.

Run from the codeforge package root:
    python -m prompts.build            # render all agents
    python -m prompts.build --check    # fail if rendered output is stale (for CI)

Build-time guarantees (any violation fails the build):
  - every agent in manifest.yaml has a body.md and an example.json
  - every example.json validates against its declared output_schema
  - manifest thinking config agrees with codeforge.config.yaml
  - rendered output matches what these sources produce (under --check)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, TypeAdapter

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve().parent          # .../config/prompts  (adjust import below to match)
_PKG_ROOT = _THIS.parent                          # .../config
_MANIFEST = _THIS / "manifest.yaml"
_AGENTS_DIR = _THIS / "agents"
_RENDERED_DIR = _THIS / "rendered"
_PIPELINE_CONFIG = _PKG_ROOT / "codeforge.config.yaml"

# Import path to the contracts module. Adjust the one string below if your package
# name differs; everything else is path-independent.
_CONTRACTS_MODULE = "codeforge.schemas.contracts"


# ---------------------------------------------------------------------------
# Field-reference generator (proven against Pydantic v2.13)
# ---------------------------------------------------------------------------

def _resolve(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    result = defs.get(ref.split("/")[-1], {})
    return result if isinstance(result, dict) else {}


def _type_label(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in schema:
        resolved = _resolve(schema["$ref"], defs)
        title = resolved.get("title") or schema["$ref"].split("/")[-1]
        return str(title)
    if "anyOf" in schema:
        return " | ".join(_type_label(s, defs) for s in schema["anyOf"])
    if "enum" in schema:
        return " | ".join(repr(v) for v in schema["enum"])
    if "const" in schema:
        return repr(schema["const"])
    t = schema.get("type")
    if t == "array":
        return f"{_type_label(schema.get('items', {}), defs)}[]"
    if t == "null":
        return "null"
    return t or "object"


def field_reference(schema: dict[str, Any], *, max_depth: int = 3) -> list[tuple[str, str, str]]:
    """Return (path, type, required|optional) rows describing a JSON-schema object.

    Accepts a JSON Schema dict (from model_json_schema() or TypeAdapter.json_schema()).
    For a union schema (top-level "anyOf"), pass one branch at a time — see
    _render_field_reference, which splits unions into per-variant tables.
    """
    defs = schema.get("$defs", {})
    rows: list[tuple[str, str, str]] = []

    def walk(node: dict[str, Any], prefix: str, depth: int) -> None:
        if "$ref" in node:
            node = _resolve(node["$ref"], defs)
        props = node.get("properties", {})
        required = set(node.get("required", []))
        for name, sub in props.items():
            path = f"{prefix}.{name}" if prefix else name
            rows.append((path, _type_label(sub, defs), "required" if name in required else "optional"))
            target = _resolve(sub["$ref"], defs) if "$ref" in sub else sub
            if target.get("type") == "array":
                items = target.get("items", {})
                if "$ref" in items and depth < max_depth:
                    walk(_resolve(items["$ref"], defs), path + "[]", depth + 1)
            elif target.get("properties") and depth < max_depth:
                walk(target, path, depth + 1)

    walk(schema, "", 0)
    return rows


def _is_basemodel(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel)


def _as_basemodel(obj: Any) -> "type[BaseModel]":
    """Cast obj to type[BaseModel] after _is_basemodel returns True."""
    return obj  # type: ignore[no-any-return]


def _json_schema_for(schema_obj: Any) -> dict[str, Any]:
    """JSON schema for a BaseModel subclass or any type (union alias) via TypeAdapter."""
    if _is_basemodel(schema_obj):
        return dict(_as_basemodel(schema_obj).model_json_schema())
    result = TypeAdapter(schema_obj).json_schema()
    return dict(result)


def _validate_against(schema_obj: Any, data: dict[str, Any]) -> None:
    """Validate data against a BaseModel subclass or a union alias (raises on failure)."""
    if _is_basemodel(schema_obj):
        _as_basemodel(schema_obj).model_validate(data)
    else:
        TypeAdapter(schema_obj).validate_python(data)


def _render_field_reference(schema_obj: Any) -> str:
    schema = _json_schema_for(schema_obj)
    defs = schema.get("$defs", {})

    # Union (discriminated or smart): render one table per variant.
    if "anyOf" in schema and "properties" not in schema:
        blocks: list[str] = []
        for i, branch in enumerate(schema["anyOf"], start=1):
            resolved = _resolve(branch["$ref"], defs) if "$ref" in branch else branch
            title = resolved.get("title", f"Variant {i}")
            sub = {**resolved, "$defs": defs}
            blocks.append(f"**Variant {i} — `{title}`**\n\n{_render_rows(field_reference(sub))}")
        return "\n\n".join(blocks)

    return _render_rows(field_reference(schema))


def _render_rows(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| Field | Type | |", "|---|---|---|"]
    for path, typ, note in rows:
        lines.append(f"| `{path}` | `{typ}` | {note} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_inputs(inputs: list[dict[str, Any]]) -> str:
    lines = ["## Your inputs", "",
             "You receive the following inputs, each delimited by an XML tag in the user turn. "
             "Inputs marked *(optional)* are absent on the happy path or first invocation.", ""]
    for item in inputs:
        opt = " *(optional)*" if item.get("optional") else ""
        lines.append(f"- `<{item['tag']}>`{opt} — {item['desc']}")
    return "\n".join(lines)


def _render_output_section(schema_obj: Any, examples: list[dict[str, Any]]) -> str:
    if len(examples) == 1:
        ex_intro = ("### A complete, valid example\n\n"
                    "This is an illustrative example with realistic values — match its structure, "
                    "not its specific contents:\n\n")
        ex_body = "```json\n" + json.dumps(examples[0], indent=2, ensure_ascii=False) + "\n```"
    else:
        ex_intro = ("### Complete, valid examples\n\n"
                    "Your output must match exactly one of these shapes. Match structure, not "
                    "specific contents:\n\n")
        ex_body = "\n\n".join(
            "```json\n" + json.dumps(ex, indent=2, ensure_ascii=False) + "\n```"
            for ex in examples
        )
    return (
        "## Your output\n\n"
        "Your response payload must be a single JSON object matching the schema below. "
        "Produce the JSON only — your reasoning happens before it, not inside it.\n\n"
        "### Field reference\n\n"
        f"{_render_field_reference(schema_obj)}\n\n"
        f"{ex_intro}{ex_body}"
    )


_CLOSING_RULE = (
    "## Output format — strict\n\n"
    "Your JSON output object must be the final thing in your response. It must begin with `{` "
    "and end with `}`. Do not wrap it in markdown code fences. Do not add any text after it. "
    "Any prose belongs in your reasoning, which comes before the object — never after it."
)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _load_manifest() -> dict[str, Any]:
    with _MANIFEST.open(encoding="utf-8") as fh:
        return cast(dict[str, Any], yaml.safe_load(fh))


def _load_pipeline_thinking() -> dict[str, dict[str, Any]]:
    """Extract each agent's thinking block from codeforge.config.yaml for cross-check."""
    if not _PIPELINE_CONFIG.exists():
        return {}
    with _PIPELINE_CONFIG.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    out: dict[str, dict[str, Any]] = {}
    for agent_id, block in (cfg.get("agents") or {}).items():
        out[agent_id] = block.get("thinking", {"enabled": False})
    return out


def _get_schema_obj(contracts: Any, schema_name: str) -> Any:
    if not hasattr(contracts, schema_name):
        raise SystemExit(f"contracts module has no attribute '{schema_name}'")
    return getattr(contracts, schema_name)


def _load_examples(agent_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Load example.json, or all example.*.json files (sorted) for union agents."""
    single = agent_dir / "example.json"
    multi = sorted(agent_dir.glob("example.*.json"))
    if single.exists() and not multi:
        return [json.loads(single.read_text(encoding="utf-8"))]
    if multi:
        return [json.loads(p.read_text(encoding="utf-8")) for p in multi]
    raise SystemExit(f"[{agent_id}] no example.json or example.*.json in {agent_dir}")


def render_agent(
    agent_id: str,
    spec: dict[str, Any],
    contracts: Any,
    partial_text: str,
    pipeline_thinking: dict[str, dict[str, Any]],
) -> str:
    agent_dir = _AGENTS_DIR / agent_id
    body_path = agent_dir / "body.md"

    if not body_path.exists():
        raise SystemExit(f"[{agent_id}] missing body.md at {body_path}")

    schema_obj = _get_schema_obj(contracts, spec["output_schema"])
    examples = _load_examples(agent_dir, agent_id)

    # Validate every example against the schema — this is the anti-drift guarantee.
    for i, example in enumerate(examples):
        try:
            _validate_against(schema_obj, example)
        except Exception as exc:  # noqa: BLE001
            first = str(exc).splitlines()[0]
            raise SystemExit(
                f"[{agent_id}] example #{i + 1} does not validate against "
                f"{spec['output_schema']}: {first}\n"
                f"  → the schema changed; update the example to match."
            ) from exc

    # Cross-check thinking config between manifest and codeforge.config.yaml.
    man_think = spec.get("thinking", {"enabled": False})
    cfg_think = pipeline_thinking.get(agent_id)
    if cfg_think is not None:
        if bool(man_think.get("enabled")) != bool(cfg_think.get("enabled")):
            raise SystemExit(
                f"[{agent_id}] thinking.enabled disagrees: manifest={man_think.get('enabled')} "
                f"codeforge.config={cfg_think.get('enabled')}"
            )

    body_text = body_path.read_text(encoding="utf-8").strip()
    inputs_section = _render_inputs(spec["inputs"])
    output_section = _render_output_section(schema_obj, examples)

    parts = [
        body_text,          # role + behavioural rules + (if present) reasoning guidance
        partial_text,       # shared envelope
        inputs_section,     # generated from manifest
        output_section,     # generated field reference + validated example
        _CLOSING_RULE,      # last, for maximum positional weight
    ]
    return "\n\n---\n\n".join(p.strip() for p in parts) + "\n"


def build(check: bool = False) -> int:
    manifest = _load_manifest()
    defaults = manifest.get("defaults", {})
    pipeline_thinking = _load_pipeline_thinking()

    try:
        contracts = importlib.import_module(_CONTRACTS_MODULE)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"could not import {_CONTRACTS_MODULE}: {exc}\n"
            f"  → run from the codeforge package root, e.g. `python -m config.prompts.build`"
        ) from exc

    _RENDERED_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for agent_id, spec in manifest["agents"].items():
        partial_rel = spec.get("partial", defaults.get("partial"))
        partial_text = (_THIS / partial_rel).read_text(encoding="utf-8")
        # Strip the leading HTML build-note comment from the partial before composing.
        if partial_text.lstrip().startswith("<!--"):
            partial_text = partial_text.split("-->", 1)[1].strip()

        rendered = render_agent(agent_id, spec, contracts, partial_text, pipeline_thinking)
        out_path = _RENDERED_DIR / f"{agent_id}.md"

        if check:
            current = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            if _hash(current) != _hash(rendered):
                stale.append(agent_id)
        else:
            out_path.write_text(rendered, encoding="utf-8")
            print(f"rendered {agent_id} → {out_path.relative_to(_PKG_ROOT)}")

    if check:
        if stale:
            print("STALE rendered prompts (run `python -m codeforge.config.prompts.build`): " + ", ".join(stale))
            return 1
        print("all rendered prompts up to date")
    return 0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if rendered output is stale")
    args = ap.parse_args()
    sys.exit(build(check=args.check))

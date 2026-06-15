from __future__ import annotations

from codeforge.config.prompts.build import _type_label, field_reference
from codeforge.schemas.contracts import SecurityReport


def test_type_label_renders_fixed_tuple_as_bracketed_ints() -> None:
    """tuple[int, int] uses JSON-schema prefixItems, not items.

    Regression: it previously fell through to the 'object' fallback and rendered
    as `object[]`, which misled agents into emitting [{start, end}] objects.
    """
    schema = {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "integer"}],
        "minItems": 2,
        "maxItems": 2,
    }
    assert _type_label(schema, {}) == "[integer, integer]"


def test_security_finding_line_range_documented_as_tuple() -> None:
    """The rendered schema table for SecurityReport documents line_range as a
    fixed [int, int] tuple, never `object[]`."""
    rows = field_reference(SecurityReport.model_json_schema())
    line_range_rows = [(path, typ) for path, typ, _ in rows if path.endswith("line_range")]
    assert line_range_rows, "expected a line_range row in the SecurityReport field reference"
    for _, typ in line_range_rows:
        assert "object[]" not in typ
        assert "[integer, integer]" in typ

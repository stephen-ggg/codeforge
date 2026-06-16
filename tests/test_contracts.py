from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeforge.schemas.contracts import InterfaceSpec


def _function_spec(contract: dict) -> dict:
    return {
        "name": "add",
        "kind": "function",
        "owner_module": "Arithmetic",
        "contract": contract,
        "stability": "stable",
    }


def test_function_interface_requires_module_and_symbol() -> None:
    spec = InterfaceSpec.model_validate(
        _function_spec(
            {
                "module": "src.arithmetic",
                "symbol": "add",
                "signature": "add(a: float, b: float) -> float",
            }
        )
    )
    assert spec.contract["module"] == "src.arithmetic"
    assert spec.contract["symbol"] == "add"


@pytest.mark.parametrize(
    "contract",
    [
        {"symbol": "add"},                                  # module missing
        {"module": "src.arithmetic"},                       # symbol missing
        {"module": "", "symbol": "add"},                    # module empty
        {"module": "src.arithmetic", "symbol": "   "},      # symbol blank
        {"signature": "add(a: float, b: float) -> float"},  # populated but no module/symbol
    ],
)
def test_function_interface_rejects_missing_or_empty_fields(contract: dict) -> None:
    with pytest.raises(ValidationError):
        InterfaceSpec.model_validate(_function_spec(contract))


def test_non_function_interface_contract_stays_free_form() -> None:
    # The module/symbol requirement is scoped to function interfaces; other kinds
    # keep their free-form contract.
    spec = InterfaceSpec.model_validate(
        {
            "name": "GET /health",
            "kind": "http_endpoint",
            "owner_module": "Api",
            "contract": {"method": "GET", "path": "/health"},
            "stability": "stable",
        }
    )
    assert spec.kind == "http_endpoint"

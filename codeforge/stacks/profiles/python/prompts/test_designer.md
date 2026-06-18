## Stack: Python 3.12 — pytest

All test files live under `tests/` at the repo root, using **pytest** conventions:

```
tests/
  test_<behaviour_a>.py   ← TC-001: its own imports + its own test function(s)
  test_<behaviour_b>.py   ← TC-002: its own imports + its own test function(s)
  conftest.py             ← shared fixtures, if any (test_infrastructure)
requirements-test.txt     ← test-only dependencies (test_infrastructure)
```

- Name each test file with a distinct `test_`-prefixed name (e.g. `tests/test_add_valid.py`,
  `tests/test_add_invalid.py`).
- Import the symbol under test as exactly `from <module> import <symbol>`
  (e.g. `from src.arithmetic import add`). Never append the symbol to the module path —
  `from src.arithmetic.add import add` is wrong: `src.arithmetic` is the module, `add` is a
  name inside it.
- Put `import pytest` and the manifest import at the top of **every** test file.

**Test-only dependencies:** emit a `requirements-test.txt` `CodeFile` in `test_infrastructure`
listing exactly the test tooling you need (`pytest` itself, `pytest-mock`, `httpx`, …). The
runner installs it before running pytest. **Always include `pytest` itself.**

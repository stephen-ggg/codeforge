## Stack: Python 3.12

The language is already chosen and locked (Python 3.12, type hints throughout) — do not
re-decide it. Design within Python.

- `function` interfaces: `module` is a dotted import path under `src/` (e.g. `src.arithmetic`);
  `symbol` is a top-level name in that module (e.g. `add`). The Coder writes `src/arithmetic.py`
  and the Test Designer imports `from src.arithmetic import add`.
- Source code lives under `src/`; runtime dependencies are declared in `requirements.txt`.

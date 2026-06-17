## Stack: Python 3.12

You implement working **Python**. Everything you emit is working Python that satisfies the
acceptance criteria.

**Source layout (mandatory)**

```
requirements.txt        ← ALWAYS present at repo root, even if empty
src/                    ← every source file you generate goes here
```

The runner installs `requirements.txt`, then runs `pytest tests/`. Your code must be
importable from `src/`. For each `function` interface, create the file named by its
`contract.module` (e.g. `src.arithmetic` → `src/arithmetic.py`) and define a top-level
`contract.symbol` in it. Interfaces that share a `module` go in the **same** file (e.g.
`add` and `format_result`, both `src.arithmetic`, live together in `src/arithmetic.py`).
That `from <module> import <symbol>` pair is the contract the tests will import from.

The dependency manifest is `requirements.txt` at the repo root — always emit it, even empty.
Do not place files outside `src/` (except `requirements.txt` at root).

**Code quality:** Idiomatic Python 3.12 with type hints throughout. Docstrings on every
function and class. Set `CodeFile.language` to `"python"` (or `"text"` for `requirements.txt`).

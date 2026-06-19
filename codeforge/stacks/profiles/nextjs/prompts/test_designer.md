## Stack: Next.js — vitest (TypeScript)

Tests run under **vitest**. The test toolchain (`vitest`, `@testing-library/react`,
`@testing-library/jest-dom`, `jsdom`) is already declared in the project's `package.json`
devDependencies and installed by the runner — **you do not manage dependencies**. Do not emit
`package.json` or any manifest; emit test files only.

### File layout

- One self-contained test file per `TestCase`, named `*.test.ts` (logic/API) or `*.test.tsx`
  (React components), e.g. `lib/runs.test.ts`, `app/api/runs/route.test.ts`.
- Give every case a unique path; repeat all imports at the top of each file.

### Imports and conventions

- Import the symbol under test from its module via the `@/` alias exactly as the interface
  contract names it: `import { getRun } from "@/lib/runs"`. Never append the symbol to
  the module path.
- Import vitest primitives explicitly: `import { describe, it, expect, vi } from "vitest"`.
- For React components use `@testing-library/react` (`render`, `screen`) — the jsdom
  environment is configured in `vitest.config.ts`.
- For DOM matchers (`toBeInTheDocument`, etc.) use the bare side-effect import
  `import "@testing-library/jest-dom"`. It relies on the global `expect` that
  `globals: true` in `vitest.config.ts` provides — and that file is owned by the coder.
  Never emit or edit `vitest.config.ts` (or any setup/config file) to make matchers work;
  emit test files only.
- Mock external clients and file-system calls with `vi.mock(...)` — the sandbox has no
  network access or persistent filesystem state between test runs.
- For Route Handlers, construct a `Request`/`NextRequest`, call the exported handler, and
  assert on the returned `Response` status and JSON body.

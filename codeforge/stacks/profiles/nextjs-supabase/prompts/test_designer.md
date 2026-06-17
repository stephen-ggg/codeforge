## Stack: Next.js + Supabase — vitest (TypeScript)

Tests run under **vitest**. The test toolchain (`vitest`, `@testing-library/react`,
`@testing-library/jest-dom`, `jsdom`) is already declared in the project's `package.json`
devDependencies and installed by the runner — **you do not manage dependencies**. Do not emit
`package.json`, `requirements-test.txt`, or any manifest; emit test files only.

### File layout

- One self-contained test file per `TestCase`, named `*.test.ts` (logic/API) or `*.test.tsx`
  (React components), e.g. `lib/cards.test.ts`, `app/api/cards/route.test.ts`.
- Give every case a unique path; repeat all imports at the top of each file.

### Imports and conventions

- Import the symbol under test from its module via the `@/` alias exactly as the interface
  contract names it: `import { createCard } from "@/lib/cards"`. Never append the symbol to
  the module path.
- Import vitest primitives explicitly: `import { describe, it, expect, vi } from "vitest"`.
- For React components use `@testing-library/react` (`render`, `screen`) — the jsdom
  environment is configured in `vitest.config.ts`.
- **Mock Supabase** — never hit a real backend. `vi.mock("@/lib/supabase", ...)` (or whichever
  module exports the client) and assert against the mock. The sandbox has no database.
- For Route Handlers, construct a `Request`/`NextRequest`, call the exported handler, and
  assert on the returned `Response` status and JSON body.

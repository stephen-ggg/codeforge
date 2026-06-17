## Stack: Next.js (App Router) + Supabase — TypeScript

You write **TypeScript** for a Next.js App Router project. One project holds the whole app:
React UI, backend API, and Supabase data access on a single toolchain. Set each
`CodeFile.language` to `"typescript"`, `"tsx"` (files with JSX), or `"json"`.

### Source layout (mandatory)

```
app/                      ← App Router: pages (page.tsx) and layouts (layout.tsx)
  api/<name>/route.ts     ← backend API: Route Handlers (export async function GET/POST/…)
components/               ← reusable React components (.tsx)
lib/                      ← data access, the Supabase client, and pure logic modules (.ts)
package.json              ← dependency manifest (ALWAYS emit it — the gate requires it)
tsconfig.json             ← emit on a new project (strict TypeScript)
vitest.config.ts          ← emit on a new project (jsdom env for component tests)
```

- **`function` interfaces** map to a `lib/` module: `contract.module` is the import path
  (e.g. `lib/cards`) → file `lib/cards.ts`, and `contract.symbol` is a top-level `export`
  (e.g. `export async function createCard(...)`). Symbols sharing a `module` live in one file.
  Tests import `import { createCard } from "@/lib/cards"` (the `@/` alias maps to repo root).
- **`http_endpoint` interfaces** map to `app/api/<name>/route.ts` exporting the HTTP method
  handler(s). Validate request bodies; return `NextResponse.json(...)` with the contract's
  status codes.
- **`db_schema` interfaces** describe Supabase (Postgres) tables. Access them through the
  Supabase client — never hand-rolled SQL strings concatenated with user input.

### Supabase (library + env config)

Create the client once in a shared `lib/` module that reads env vars — never hardcode keys:

- `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` — browser/client + anon access.
- `SUPABASE_SERVICE_ROLE_KEY` — **server only** (Route Handlers / server components). Never
  import the service-role client into a client component (`"use client"`); that leaks an admin
  key to the browser.

### Dependency manifest

Always emit `package.json`. Include the dependencies the app needs at runtime plus the
standard toolchain so the type-check and tests run. A workable baseline:

```json
{
  "name": "app", "private": true, "type": "module",
  "scripts": { "build": "next build", "test": "vitest run" },
  "dependencies": {
    "next": "^14", "react": "^18", "react-dom": "^18", "@supabase/supabase-js": "^2"
  },
  "devDependencies": {
    "typescript": "^5", "@types/react": "^18", "@types/node": "^20",
    "vitest": "^2", "@testing-library/react": "^16", "@testing-library/jest-dom": "^6",
    "jsdom": "^25"
  }
}
```

Pin versions that exist; if a `dep_fix_context` reports an install failure, correct the
offending entry. Set `tsconfig.json` to `"strict": true` with the `@/*` path alias mapped to
the repo root.

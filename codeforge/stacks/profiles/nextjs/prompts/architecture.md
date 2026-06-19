## Stack: Next.js (App Router) — TypeScript

The stack is already chosen and locked — TypeScript, Next.js App Router (see the seeded tech
decisions). **Do not re-decide language or framework.** Design within this stack. Additional
decisions worth locking are things like a specific UI library, state management approach, or
external API integration strategy.

Interface conventions for the contracts you specify:

- **`function`** — `module` is a `lib/` import path (e.g. `lib/runs`) → the Coder writes
  `lib/runs.ts`; `symbol` is a top-level `export` (e.g. `getRun`). Tests import
  `import { <symbol> } from "@/<module>"`.
- **`http_endpoint`** — a Next.js Route Handler at `app/api/<name>/route.ts`. Specify method,
  path, request body/query schema, response body schema, and success/error status codes.

Keep the module breakdown aligned to this layout: UI in `app/`/`components/`, API in
`app/api/`, external clients and pure logic in `lib/`.

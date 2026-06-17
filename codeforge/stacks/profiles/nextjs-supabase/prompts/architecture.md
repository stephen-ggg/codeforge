## Stack: Next.js (App Router) + Supabase — TypeScript

The stack is already chosen and locked — TypeScript, Next.js App Router, Supabase via
`@supabase/supabase-js` (see the seeded tech decisions). **Do not re-decide language,
framework, or datastore.** Design within this stack. Additional decisions worth locking are
things like the concrete table schema or an auth strategy.

Interface conventions for the contracts you specify:

- **`function`** — `module` is a `lib/` import path (e.g. `lib/cards`) → the Coder writes
  `lib/cards.ts`; `symbol` is a top-level `export` (e.g. `createCard`). Tests import
  `import { <symbol> } from "@/<module>"`.
- **`http_endpoint`** — a Next.js Route Handler at `app/api/<name>/route.ts`. Specify method,
  path, request body/query schema, response body schema, and success/error status codes.
- **`db_schema`** — a Supabase (Postgres) table: name, each column with type and constraints,
  primary/foreign keys, and any RLS expectation.

Keep the module breakdown aligned to this layout: UI in `app/`/`components/`, API in
`app/api/`, data access and pure logic in `lib/`.

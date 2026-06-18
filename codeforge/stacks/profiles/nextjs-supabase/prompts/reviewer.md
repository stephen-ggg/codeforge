## Stack: Next.js + Supabase — TypeScript

Review idiomatic, strictly-typed TypeScript for a Next.js App Router app. Expect:

- No `any` escape hatches where a real type is knowable; exported functions fully typed.
- React: client components marked `"use client"`; no server-only code (or server env vars)
  imported into client components.
- API Route Handlers validate input and return the contract's status codes.

**Supabase / security (weigh these in the security review):**

- **`SUPABASE_SERVICE_ROLE_KEY` must never reach the client.** It may only be read in Route
  Handlers / server components. A service-role client imported into a `"use client"` module,
  or the key referenced under a `NEXT_PUBLIC_` name, is a **critical** finding (admin key
  exposed to the browser).
- All keys/URLs come from environment variables — never hardcoded.
- Data access goes through the Supabase client (parameterised), not string-built SQL.
- Row-Level-Security is assumed on the database; flag code paths that rely on the anon key for
  privileged writes that RLS would normally block.

## Stack: Next.js — TypeScript

Review idiomatic, strictly-typed TypeScript for a Next.js App Router app. Expect:

- No `any` escape hatches where a real type is knowable; exported functions fully typed.
- React: client components marked `"use client"`; no server-only code (or server env vars)
  imported into client components.
- API Route Handlers validate input and return the contract's status codes.

**Security (weigh these in the security review):**

- Secrets and API keys must come from environment variables — never hardcoded.
- Server-only env vars (no `NEXT_PUBLIC_` prefix) must never be read in `"use client"` modules.
- Route Handlers that accept user-supplied data must validate and sanitise inputs before use.

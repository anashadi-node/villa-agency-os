# ADR-0004 — Runtime, API, and Frontend Stack Direction

- Status: **ACCEPTED** (owner decision, 2026-09-28)
- Date: 2026-09-22 · accepted 2026-09-28
- Decision type: controlled open decision from Master Spec §38/§52

## Context

Master Spec leaves exact backend/frontend framework open but constrains architecture: modular monolith, PostgreSQL, REST/OpenAPI-style API, strict authz, reliable jobs/outbox, object storage, environment separation.

Existing repo uses Python stdlib modules and an internal `http.server` dashboard. This is strong for deterministic kernel/testing but not enough for production API/web/mobile ecosystem.

## Decision

Keep Python domain kernel. Add a production API layer around it. Do not expose current dashboard as public production API.

Recommended direction for implementation planning:

- Backend language: Python, preserving current domain modules.
- Backend shape: modular monolith with semantic command handlers.
- API: `/api/v1` REST/OpenAPI-style semantic commands.
- Frontend: production web app consumes backend allowed-actions/API, not direct DB.
- Current dashboard: internal/admin/dev view only until replaced/wrapped by production auth.

## Alternatives considered

| Alternative | Verdict | Reason |
|---|---|---|
| Keep stdlib `http.server` as production public app | Reject | Lacks production auth/session/MFA, CSRF/session controls, API structure. |
| Rewrite all backend in new framework immediately | Reject | Discards tested domain invariants. |
| Add API wrapper around existing kernel | Accept | Lowest risk, reuse tested modules, aligns with Master Spec. |

## Required follow-up before coding

- Select exact API framework only after comparing maintainability, security, OpenAPI support, testing, and operational cost.
- Define API error catalogue and idempotency/correlation middleware.
- Define frontend tech with accessibility and permission/state regression testing.

---

## Amendment — 2026-09-26: API contract standard (v1.1 §22, §23, §24)

v1.1 §22/§23/§24 pin the API shape more tightly than the original ADR. Recorded decisions:

1. **Versioned REST/application API under `/api/v1`**; a breaking change requires a new version.
2. **Semantic commands, never generic status PATCH** — e.g.
   `POST /reservations/{id}/tentative-hold/request`, `POST /holds/{id}/confirm`,
   `POST /client-payments/{id}/verify-funds`, `POST /payment-requests/{id}/approve`,
   `POST /reservations/{id}/confirm-booking`.
3. **Every sensitive command runs the same chain**: authentication → permission → scope → state →
   invariant → policy → approval/step-up → transaction → event → audit.
4. **Money crosses the API as a decimal string + ISO-4217 currency** while storage stays integer
   minor units (§13 fixed precision). Timestamps ISO-8601 UTC with business timezone retained;
   country ISO 3166-1 alpha-2; language BCP 47.
5. **Cursor pagination**, server-side filtering/search, always authorization-scoped.
6. **Structured error codes** from the §24 catalogue, plus `correlation_id` and structured
   details/blockers — the UI must never parse free-text messages (this is what makes ADR-0011's
   translation of message text safe).
7. **Optimistic concurrency** via version/ETag-equivalent; a stale write returns
   `409 CONCURRENT_MODIFICATION` and must not overwrite (see ADR-0014 §4).
8. **`Idempotency-Key` mandatory** for high-risk and retry-prone commands; **correlation id
   propagates** API → domain event → job → integration → audit.

Note: the dashboard's current in-process HTTP surface is **not** this API. It remains the
internal read-only + approval surface until the API layer is built.

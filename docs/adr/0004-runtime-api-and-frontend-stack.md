# ADR-0004 — Runtime, API, and Frontend Stack Direction

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22
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

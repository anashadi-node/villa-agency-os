# ADR-0005 — Identity, Auth, Session, MFA, RBAC, and Scope

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22

## Context

Master Spec requires RBAC + resource scope + state + data classification + policy. MFA is mandatory for Owner, Finance, and System Admin; high-risk actions require step-up.

Existing `dashboard.py` uses simple bearer token mapping to role. It is good for local/internal dashboard testing but insufficient for production.

## Decision

Replace production auth/session authority with a first-class Identity & Security module. Keep current dashboard token model only as local/dev/internal fallback until production web/API auth is ready.

## Production requirements

- Users table / external auth mapping.
- Roles, permissions, agency/resource scope.
- Sessions with rotation/revocation and idle/absolute timeouts.
- MFA for Owner/Finance/System Admin.
- Step-up authentication for high-risk finance/security/bank actions.
- Audit all sensitive access and permission changes.
- AI Service identity has explicit deny list and no human authority.

## Migration note

Do not grant `system_admin` finance god-mode. System admin must not automatically read bank/passport/payment authority.

## Tests required before implementation

- Unauthorized page/API access denied.
- Cross-agency access denied.
- Finance/bank action without MFA or step-up denied.
- Auditor read-only enforced.
- AI service denied for finance/bank/permission/audit/secrets/passport.

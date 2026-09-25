# ADR-0005 — Identity, Auth, Session, MFA, RBAC, and Scope

- Status: **ACCEPTED** (owner decision, 2026-09-26)
- Date: 2026-09-22 · accepted 2026-09-26

## Decision taken (2026-09-26)

The owner accepted this ADR and chose **TOTP authenticator apps** as the MFA factor
— offline, no vendor, no per-message cost. The following values are the ones the
implementation uses; changing them is an ADR change, not a config tweak:

| Parameter | Value | Why |
|---|---|---|
| MFA required for | `owner`, `finance` | the two roles that can move money |
| Second factor | TOTP (RFC 6238), 6 digits, 30 s step, SHA-1 | maximum authenticator compatibility |
| Drift tolerance | ±1 step (90 s window) | tolerates clock skew; wider windows make guessing practical |
| Idle session timeout | 30 minutes | a walked-away browser stops being a session |
| Absolute session timeout | 12 hours | an attacker cannot inherit a session indefinitely |
| Step-up window | 10 minutes | money/bank actions require a *fresh* factor |
| Step-up applies to | `refund`, `payout`, `bank_change` | irreversible money and bank-detail changes |
| TOTP secret storage | `secret_ref` only, resolved from env/vault | the database never holds the shared secret |

### Implementation status

- Done: `src/totp.py` (pinned to the RFC 4226/6238 vectors), `src/identity.py`
  (sessions, enrolment, replay guard, step-up), `auth_sessions`/`mfa_factors`/
  `auth_events` (append-only) in the schema, `Principal.agency_id` + per-page
  cross-agency refusal in `src/dashboard.py`.
- Done (ADR's required tests): unauthorized denied, cross-agency denied, auditor
  read-only, high-risk action without MFA/step-up denied.
- Outstanding: the AI-service identity with its explicit deny list; wiring
  `SessionService` into the dashboard's HTTP entry point as the token authority;
  a real audit of permission changes (role grants) once roles are stored rather
  than configured.

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

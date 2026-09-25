# ADR-0010 — Deployment, Observability, and Quality Gates

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22

## Context

Master Spec requires DEVELOPMENT → STAGING → PRODUCTION, CI/CD quality gates, restore drill, security checks, API contract tests, migration tests, critical E2E, permission/state tests, and visual regression for critical screens.

Existing repo has strong `make check`: compile, pytest, secret scan, unsafe scan, schema parity. This must remain but is not sufficient for Master Spec production.

## Decision

Keep current `make check` as baseline gate and extend quality gates incrementally.

## Required environments

- Development: synthetic/minimized data.
- Staging: production-like schema/integrations with safe credentials/test providers.
- Production: reviewed deploy only; agents do not receive production secrets by default.

## Required gates

1. Build/import check.
2. Lint/type checks where stack supports it.
3. Unit tests.
4. Integration tests.
5. API contract tests.
6. DB migration tests.
7. Secret scan.
8. Dependency/security checks.
9. Critical E2E tests.
10. Frontend permission/state tests.
11. Visual regression for critical screens.
12. Backup restore drill.

## Observability requirements

- Correlation ID through API, jobs, integrations, audit.
- Structured errors and logs with redaction.
- Metrics for holds, payments, approvals, incidents, outbox, webhooks, auth failures.
- Alerting for stale data, failed jobs, unauthorized access, finance exceptions.

## Tests required

- Production preflight refuses demo data.
- Backup restore produces usable critical workflow.
- Logs redact secrets/bank/passport/API keys.
- Broken outbox/job surfaces observable alert.

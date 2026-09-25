# ADR-0006 — Database Migration and Parallel State Strategy

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22

## Context

Existing schema has 34 tables and a strong SQLite⇄PostgreSQL twin discipline. Master Spec requires `agency_id`, supplier route, parallel reservation states, bank profile versions, payment requests, supplier finance, transactional outbox, and production auth tables.

The largest conflict is current `bookings.status` vs Master Spec requirement for parallel reservation states.

## Decision

Use additive migrations first. Do not destructively rewrite `bookings` until mapping tests and dry-run migration reports exist.

## Migration sequence

1. Add `agencies` and tenant scope.
2. Add supplier, villa-supplier relationship, contract/rate version model.
3. Add parallel state columns/tables while keeping legacy status read-only for compatibility.
4. Add state transition records per state dimension.
5. Add bank profile versions and payment target fingerprint.
6. Add supplier finance/AP tables.
7. Add outbox/integration inbox tables.
8. Deprecate old single status only after all consumers use parallel states.

## Required safeguards

- Every migration has SQLite and PostgreSQL variants or generated twin parity where appropriate.
- Backfill produces audit report.
- Ambiguous legacy rows go to manual review, not guessed conversion.
- Production migrations are reviewed and tested in staging; no autonomous destructive production migration.

## Tests required

See `docs/WAVE0_MIGRATION_TEST_PLAN.md`.

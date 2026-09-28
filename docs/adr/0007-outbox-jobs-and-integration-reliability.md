# ADR-0007 — Transactional Outbox, Jobs, and Integration Reliability

- Status: **ACCEPTED** (owner decision, 2026-09-28)
- Date: 2026-09-22 · accepted 2026-09-28

## Context

Master Spec requires critical state mutation and outbox event insertion in the same DB transaction. Existing repo has connector/outbound concepts and scheduler jobs, but no first-class `outbox_events` table.

## Decision

Introduce a transactional outbox and idempotent publisher before production integrations and public website/mobile flows.

## Required objects

- `outbox_events`
- publisher worker
- delivery attempts / dead-letter state
- correlation ID propagation
- idempotent consumer contract
- integration inbox for webhook/source idempotency where needed

## Rules

- Business state commit must not depend on notification success.
- Notification failure must produce retry/dead-letter, not rollback booking/payment truth.
- Duplicate webhook/retry must produce one logical event/financial entry.
- SOURCE_UNAVAILABLE must not be mapped to NOT_AVAILABLE or PAYMENT_FAILED without evidence.

## Tests required

- State mutation + outbox insert atomic.
- Failed publish retry-safe.
- Duplicate publish idempotent.
- Provider outage preserves business transaction and marks delivery pending/failed.

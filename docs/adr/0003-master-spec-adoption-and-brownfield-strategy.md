# ADR-0003 — Master Spec Adoption and Brownfield Strategy

- Status: **PROPOSED — pending owner review**
- Date: 2026-09-22
- Source of truth: `Villa Agency OS — Hermes Super Dev Suite Master Specification`, version 1.0 Final Architecture Freeze

## Context

A new authoritative Master Specification defines Villa Agency OS V1 Production Core. Existing repo already contains a tested deterministic reservation/finance/audit kernel, but misses multiple Master Spec domains: tenant scoping, supplier route, parallel states, supplier finance, bank profile versioning, transactional outbox, production auth, API, and AI Gateway.

## Decision

Adopt the Master Specification as the target architecture and migrate the existing project using **brownfield controlled migration**, not full rebuild.

## Rationale

Existing modules enforce important invariants already:

- no double booking;
- money as integer minor units;
- append-only audit hash chain;
- HMAC webhook verification;
- approval dual control;
- untrusted inbound content handling;
- grounded outbound message approvals.

Replacing them blindly would increase defect risk. However, retaining the current architecture unchanged would preserve Master Spec gaps. Therefore the correct path is reuse + targeted refactor.

## Consequences

- Existing working invariants must be protected by tests before migration.
- New Master Spec features must be added incrementally by wave.
- No destructive migration without staging proof and rollback.
- No production launch until P0 Master Spec gaps are closed.

## Non-negotiable migration guardrails

1. Master Spec wins over old docs/code when conflict exists.
2. Existing code wins only when it enforces the same invariant safely.
3. No rewrite for style.
4. No production-facing auth using current simple dashboard token model.
5. No supplier payout before bank profile versioning and payment request controls.
6. No AI tool before AI Gateway deny registry.

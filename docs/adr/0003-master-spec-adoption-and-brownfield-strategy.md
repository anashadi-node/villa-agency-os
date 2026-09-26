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

---

## Amendment — 2026-09-26: Master Spec **v1.1** supersedes v1.0

The owner supplied `villa-agency-os-hermes-master-specification-v1.1.html`
(`Version: 1.1 Final Architecture Freeze — AI Reservation Autonomy + U.S. English + Multi-Rail
Payments`, sha256 `0751dfdc89968495c4707505a575ea077456fb72437cf1d0262d145339388da3`), and
instructed that it **replaces every earlier version** as the authoritative source of truth.

Consequences for this ADR:

1. Every reference to "the Master Spec" in this repository now means **v1.1**. The v1.0 file
   remains on disk as history only and must not be used for new decisions.
2. The brownfield strategy (adopt, migrate incrementally, reuse what is safe) is unchanged, and
   the audit findings already recorded stay valid — v1.1 does not require a redesign.
3. v1.1 is a **controlled requirement change**: the delta is registered in
   `docs/MASTER_SPEC_V1_1_DELTA.md` (12 findings, each with existing condition, gap, change and
   test) and the new decisions it forces are ADR-0011…0014 plus amendments to ADR-0004 and
   ADR-0009.
4. The spec's own instruction governs sequencing: *build incrementally by wave; do not proceed by
   assuming unfinished security/finance controls will be added later.* Therefore WAVE 1 items
   that v1.1 makes foundational (Policy Engine, AI Service identity, 12-role RBAC, transactional
   outbox) are prerequisites to WAVE 6 finance work.

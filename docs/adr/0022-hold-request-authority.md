# ADR-0022 — A hold request needs an identity: §56 enforcement on the path that takes inventory out of sale

- **Status**: ACCEPTED (recorded with WAVE 4-B-2, 2026-09-26)
- **Spec references**: §7 (Reservation & Hold Lifecycle), §56 (AI tool envelope and risk tiers)
- **Supersedes**: nothing. **Amends**: ADR-0020 — it closes the authority gap ADR-0020 recorded
  against `request_hold()`, without touching the lifecycle rules that ADR froze.

## Context

§7 opens the reservation flow with a tentative hold request, and §56 names *"tentative-hold
requests"* among the actions an AI may take only under policy control. `hold.request_tentative` is
already seeded in `ai_tools` as MEDIUM with `policy_key = security.ai.medium_tool_allowance`.

The seed row was inert. Measured on 2026-09-26, before this ADR:

| Question | Before | After |
|---|---|---|
| Does `request_hold()` take an actor? | no — no `actor`, no `actor_role`, no `principal_id` | required identity |
| Is there an enforcement point for `hold.request_tentative`? | none — the `ai_tools` row had no caller | `AiService.authorize(principal_id, "hold.request_tentative")` |
| Does the hold record who asked? | no — `holds` has no requester column, and the `hold.requested` audit event fell back to the kernel's own actor | the audit event names the caller |
| Can a caller skip the gate by omitting an argument? | n/a | no — identity is required, so omission is a `TypeError`, not an admission |

A probe called the kernel directly and the hold was **created**: `status = CONFIRMED`, the slot
occupied, no authorisation consulted and no one recorded as having asked for it.

That is the overbooking path. WAVE 4-B made hold *occupation* fail-closed; a hold that can be taken
without a permission is the same class of loss arriving through a door WAVE 4-B did not close,
because it closed expiry, extension and release — not the request.

The hole was real but unreachable: `request_hold(` has no production caller in `src/` or `scripts/`.
That is why it is cheap to close now, and why closing it later would have cost a migration of live
behaviour rather than eight test call sites.

## Decision

### 1. Identity is a required argument, not an optional one

`request_hold(*, ..., actor, actor_role, principal_id=None)`. `actor` and `actor_role` are
keyword-only and required; `principal_id` is optional because a human actor has no principal to name.

A gate a caller can skip by omitting an argument is not a gate, so there is no default identity. The
cost is visible and accepted: eight existing call sites in tests were updated to pass a human actor,
and **no production code changed, because there was none to change**.

### 2. One gate implementation for every §56 tool

`_require_availability_request_authority()` became `_require_tool_authority(..., tool=...)`. Two
gated paths now exist, and a second copy of the rule is a second place to forget the principal
requirement. The refusal codes are unchanged: `MACHINE_PRINCIPAL_REQUIRED` when a machine actor names
no principal, and `AI_MEDIUM_TOOL_NOT_ALLOWED` when the tool is not in a published allowance.

### 3. The gate runs outside the transaction and before the idempotency lookup

The same placement ADR-0021 chose, for the same reason: the permission is about *taking the slot*, so
a replay of a refused request is refused too. A withdrawn allowance therefore closes replays as well
as first calls. `AiService` writes its own audit row and commits, so nesting it in
`BEGIN IMMEDIATE` is not an option.

### 4. The gate is deny-first, so the machine path is closed today

`hold.request_tentative` is not in any published allowance. A machine actor is therefore refused —
the intended behaviour rather than a stub. §56 classes this action as policy-controlled, so the owner
opens it by publishing a policy value, with no code change.

### 5. The audit event names the caller

`_audit(..., actor=actor)` on the `hold.requested` event, following the availability path. The
honest nuance: `audit_events.actor_role` carries the *kernel's* role (`self.actor_role`), not the
caller's, on both gated paths. That is a pre-existing convention and this ADR does not change it —
changing it would rewrite the meaning of every historical row.

### 6. The idempotency fingerprint does not include the actor

`request_hash` stays over (unit, window, guest, policy). Adding the actor would turn a legitimate
replay by a different actor into a *different* request, i.e. an overlap refusal on a slot the same
hold already covers. Idempotency is about "this hold request", not "this hold request by this actor".

### 7. No new column on `holds`

Attribution lives in the append-only audit chain, which already records who did what. A mutable
`requested_by` column would be a second, weaker answer to the same question, and a hold's state is
already derived from its event log (ADR-0020). The asymmetry with `availability_checks` — which does
store `requested_by` — is deliberate: that table *is* the request, whereas a hold is a claim whose
history is the audit chain.

### 8. The auto-recorded level-B evidence is not attributed to the caller

When `request_hold(evidence_id=None)` records its own `manual_ops` / level-B capture, it means "no
external source was consulted" — a platform fact. Attributing it to the requester would imply the
requester asserted availability, which is a different and false statement. ADR-0020 recorded the
missing gate call as a gap; this ADR closes the *authority* gap and leaves that one alone on purpose.

## Consequences

**What this opens**

- §56's hold tool has an enforcement point for the first time: publishing one value opens the machine
  path, and publishing an empty list closes it again, with no code change.
- The hold path is attributable end to end: the `hold.requested` event names the caller.
- Both gated paths share one implementation, so the principal requirement cannot drift between them.
- The refused machine path is demonstrable: a machine actor is turned away while a human is admitted.

**What this does not do (honest gaps)**

1. **`hold.request_tentative` is still unpublished**, so the machine path stays refused until the
   owner decides — the same outstanding owner decision already recorded for the allowance package.
2. **No rate limit on hold requests.** The probe measured 50 identical requests admitted 50 times on
   the availability path, and the hold path has no cap either. §7 names no number, so inventing one
   would be policy by guesswork; it is recorded as an owner decision, not silently chosen.
3. **`holds` still stores no requester column.** A report that wants "who asked" must read the audit
   chain. Stated rather than papered over.
4. **`publish(approved_by=...)` still records a name without verifying the role** — unchanged by this
   ADR, and already recorded as a gap.
5. **`REQUESTED`, `AWAITING_SUPPLIER` and `RELEASE_REQUESTED` remain unreachable** (ADR-0020's gap):
   `request_hold()` still enters at `CONFIRMED`.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Optional `actor` with a default | A default identity is an admission without attribution: omitting the argument would bypass the gate. Eight test call sites were cheaper to update than a permanent bypass. |
| A second gate helper for the hold path | Two copies of one security rule drift, and the copy is where the principal requirement gets forgotten. |
| Storing the requester on `holds` | A mutable column duplicating the audit chain, against ADR-0020's derive-from-log rule. |
| Gating inside the transaction | `AiService` writes and commits its own audit row; nesting that in `BEGIN IMMEDIATE` is the hazard ADR-0021 avoided. |
| Refusing machine actors outright (the `option_sets.present()` precedent) | §56 classes tentative holds as policy-controlled, not human-only. Outright refusal would need rework the moment the owner publishes the allowance. |
| Putting the actor in `request_hash` | Turns a legitimate replay into an overlap refusal for the slot the same hold already occupies. |
| Threading the actor into the auto-recorded evidence | Would misattribute a platform fact ("no source was consulted") to a human, implying an assertion nobody made. |

## Verification

- `tests/test_wave4b2_hold_request_authority.py` — 11 tests: a human actor may still request a hold
  (positive control); a machine actor is refused while the allowance is unpublished **and nothing is
  written**; a machine actor must name its principal (`MACHINE_PRINCIPAL_REQUIRED`); the gate runs
  before the idempotency lookup, so a suspended principal cannot replay its way through; publishing
  `hold.request_tentative` admits a machine actor; allowing `availability.request` alone does **not**
  open holds (precision); a suspended principal is refused even with the allowance (kill switch); a
  refusal leaves an `AI_TOOL_REFUSED` row and no `hold.requested` row; the audit row names the actor
  who asked; identity is a required argument, so omission raises `TypeError` instead of being
  admitted; and a refused request takes no inventory, so a human can still take the same slot.
- Existing call sites given the identity they now need: `test_reservation_kernel.py`,
  `test_wave1b_tenant_scoping.py`, `test_wave1f_idempotency_tenant.py`,
  `test_wave4b_hold_lifecycle.py`.
- Full suite: **2771 distinct / 0 failures / 4 skips** (baseline 2760 → +11, no regressions).
- No schema change: 58 tables / 50 triggers unchanged; PG twin `--check` clean.
- `make check` → **PRE-DEPLOY GATE: PASS** (secret-scan and unsafe-scan 0 findings).

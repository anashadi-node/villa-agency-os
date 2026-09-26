# ADR-0026 — The discount engine: two funding kinds, two margin guards, one human escape hatch

- **Status**: Accepted
- **Date**: 2026-09-26
- **Spec**: §11 Pricing, Quote & Discount · §15 Approval Engine · §16 Policy Engine · §21 AI Explicit Deny Registry · §56 tool tiers
- **Wave**: WAVE 4-D

## Context

§11 asks a discount to be five things at once — *rule evaluation, authority, margin guard, approval,
audit* — and adds three clauses that are easy to read past:

1. **Supplier-funded and agency-funded discounts are separated.**
2. **The margin floor carries a percentage *and* an absolute guard; its override needs human authority.**
3. §16 lists *discount authority* and *margin floors* as **policy**, and §15 states plainly that
   "threshold adalah configuration, bukan hard-coded".

What the platform had was one flat ceiling: `calculate_quote()` computed
`ceiling = after_los.pct(rules.max_discount_pct)` and refused anything above it. One undifferentiated
`discount_minor`, no funding party, no authority check, no margin floor, no approval, no audit trail.
`max_discount_pct` also lives on the per-quote `PolicyRules` snapshot, so it is a *quote* parameter,
not the versioned §16 policy value §11 calls for.

Two catalogue entries had already been declared and left unpublished:
`commercial.discount.authority_roles` (ROLE_SET) and `commercial.margin_floor_pct` (PCT). Nothing
read them.

## Decision

### 1. `src/discounts.py` — a discount is a decision, not a number

`DiscountEngine.evaluate(request) -> DiscountDecision` with exactly three outcomes:
`APPLIED`, `REFUSED` (authority), `REQUIRES_OVERRIDE` (margin floor breached). The decision carries
the amount, the margins before/after, the floors it was measured against, the authority list it was
checked against, and a content fingerprint that excludes its own identity — so a replayed request
hashes the same.

### 2. The two kinds are separated by *whose money moves*

- `SUPPLIER_FUNDED` — the supplier pays. The agency's margin is untouched, so **no floor applies and
  no margin basis is demanded**.
- `AGENCY_FUNDED` — the agency pays. This is the only kind the floor governs.

That asymmetry *is* §11's separation clause, expressed as behaviour rather than as a label.

### 3. Authority and both floors are read from published policy, never from a literal

`commercial.discount.authority_roles`, `commercial.margin_floor_pct`, `commercial.margin_floor_minor`.
An unpublished value raises `DISCOUNT_POLICY_NOT_PUBLISHED` naming the key — **the engine refuses; it
never substitutes a default**. A test also asserts the module carries no role list of its own.

### 4. A new catalogue key: `commercial.margin_floor_minor` (COUNT)

§11 requires a percentage **and** an absolute guard, and only the percentage had been declared.
Registered in `policy_defs` and seeded in `src/schema/001_initial.sql` (twin regenerated).

### 5. The margin basis is an input, not an inference

The platform has **no supplier-cost basis**: `rate_versions.nightly_minor` is the *selling* rate and
`contract_versions` carries no amount at all. So a caller must state the revenue and the margin it is
asking the engine to protect; `MARGIN_BASIS_REQUIRED` is raised instead of inventing a figure. This is
the seam where §12 contracts and the finance wave will supply a real basis.

### 6. A breach is neither applied nor self-escalated

Nothing moves, and `margin_after_minor` reports the state that stands while `margin_if_applied_minor`
reports what the guest asked for — an approver needs both. `request_override()` then raises an
`ApprovalRequest`: `subject_type="discount"` (already a valid §15 subject), `action="margin_override.approve"`,
`required_role` = the top of the platform's own role vocabulary (§18, read from `approvals.ROLE_RANK`),
`risk_level="HIGH"`, `required_approvals=1`.

**No `money` is attached to that approval, on purpose.** `_check_dual_control` demands two signatures
for anything carrying money; §15's dual-control list is refunds, payouts and bank changes — *not*
discounts. Attaching the amount would silently turn a one-signature policy decision into two.

### 7. The engine exposes no way to approve

§21 denies `discount.approve` and `margin_override.approve` to AI. The capability is therefore not
built into the engine at all: a test walks every attribute and fails on any `approve*` method.

### 8. Every outcome is audited

`DISCOUNT_APPLIED`, `DISCOUNT_REFUSED`, `DISCOUNT_MARGIN_FLOOR_BREACHED`, `DISCOUNT_OVERRIDE_REQUESTED`
— one row each, inside the caller's transaction.

### 9. Latent defect fixed: a DENY that matched nothing

`ai_service` enforces §21 by comparing the **tool name** against the registry verbatim
(`if definition.tool in denied`). The registry named `discount.approve`; the catalogue registers
`discount.approve_out_of_policy`. **The DENY matched no tool.** The capability was held only by the
closed default allowance — one permissive HIGH allowance away from being callable.

Fixed as **revision 2** of `security.ai.denied_actions` (append-only; the v1 list is preserved, the
new entry sits beside `discount.approve` so the pairing is visible), and locked by a guard file that
maps each §21 capability to the tool that carries it, refuses to accept a registry entry that denies
nothing, and fails if the amendment stops being the revision in force.

## What this ADR does *not* do

- **It does not choose the values.** Authority roles, floor percentage and floor amount are the
  owner's; until published the engine refuses. Demonstrated end-to-end on a fresh database.
- **It does not wire the engine into `calculate_quote()`.** The quote path keeps its existing
  `max_discount_pct` ceiling until a later wave routes discounts through the engine; changing the
  pricing contract is its own wave with its own tests.
- **It does not fix two adjacent findings.** `incident.compensate_serious` and `legal.dispute` are
  HIGH tier with `policy_key = NULL` and are *not* named in the registry — §21 does not name them, so
  deciding their treatment is an owner call, reported rather than silently changed.

## Consequences

- A discount can no longer be granted by anyone, at any size, for any reason: authority is a published
  list, the floor has two halves, and the escape hatch is a human signature with an audit row.
- The demo dataset now has three unpublished §11 keys by design: the platform refuses discounted
  quoting until the owner publishes. This is the fail-closed behaviour working, not a gap.
- The margin basis being an input is a visible seam. It will look like ceremony until §12 contracts
  land; it is the alternative to a fabricated margin.

## Tests

- `tests/test_wave4d_discount_engine.py` — **41 tests**: vocabulary and separation, authority from the
  store, both floors (boundary at exactly the floor, one minor unit below it), fail-closed on each
  unpublished key, the supplier-funded exemption, no-approve surface, escalation and self-approval
  refusal, audit rows for every outcome, deterministic fingerprints.
- `tests/test_deny_registry_covers_spec_denials.py` — **6 tests**: every §21 capability denied, every
  tool that carries one denied, the amendment in force, no unaccounted registry entry.

Suite total after this wave: **2894** distinct tests, 0 failing.

## Files

- `src/discounts.py` (new) — the engine.
- `src/schema/001_initial.sql` + `src/schema/001_initial.pg.sql` (regenerated) — the
  `commercial.margin_floor_minor` catalogue row and the `security.ai.denied_actions` revision 2.
- `tests/test_wave4d_discount_engine.py`, `tests/test_deny_registry_covers_spec_denials.py` (new).

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Hard-code a default floor (e.g. 20% / 1,000,000) | §15 and §16: thresholds are configuration. A default is a policy the owner never made. |
| Apply the discount and flag the breach afterwards | A discount is money leaving. Fail-closed means the floor holds until a human says otherwise. |
| Treat supplier-funded and agency-funded as one amount with a flag | The flag would be decoration: the margin guard must not run for money the agency is not paying. |
| Attach `money` to the override approval | Trips §15 dual control and demands two signatures for a decision §15 does not list. |
| Rename the tool to `discount.approve` instead of amending the registry | Renames a registered tool and breaks the allowance vocabulary; the registry is what enforcement reads. |

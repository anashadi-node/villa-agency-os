# ADR-0050 — The kernel enforces §9's commercial gate: a booking needs the version the guest accepted

- Status: Accepted
- Date: 2026-10-02
- Closes: the gap ADR-0048 recorded (a booking could be written from an offer that was only sent)
- Refines: §9 (Booking Confirmation Invariant) · §11 (commercial versions) · ADR-0045/0049

## Context

ADR-0048 wrote the amend/cancel worker, and on the way found that `confirm_from_hold` — the only
writer of a `bookings` row in the estate — enforced hold status and expiry, unit and policy match,
VOID and expiry, capacity, the amount range and overlap, but **never §9's commercial gate**:
acceptance was not checked before a booking was written. A quote the guest had merely been *sent*
could become a contract. That was recorded rather than patched, because it changes *who may create
a booking* and every confirmation test confirmed without an acceptance.

The moment to patch it is now, and precisely because of what the recon showed: `src/` has **no
caller** of `confirm_from_hold` outside its own definition — the operator CLI that will drive it is
still the open item — so the gate lands before a live path depends on the old behaviour. Only the
tests exercise it.

## Decision

**1. The gate lives in the kernel, in the transaction, before the booking row.** After the quote is
resolved and its unit/policy/void/expiry are checked, `confirm_from_hold` now reads the quote's
accepted versions and refuses when there is none. `code="CONFIRM_NOT_ACCEPTED"`, `quote_id` in the
payload, nothing written: the refusal happens above the insert, so no partial row and no ledger
entry can exist.

**2. A yes for other dates is not a yes for these.** If the quote has an accepted version whose
`unit_id`/`check_in`/`check_out` do not cover **this hold's** stay, the refusal is separate —
`code="CONFIRM_ACCEPTED_OTHER_STAY"` — so an operator reading the log learns *which* mistake was
made (nobody answered, or somebody answered something else) instead of a generic "not accepted".

**3. The kernel reads the version row, not the quotation module.** The kernel is a lower layer and
already validates `quote_ref` with a direct read of the `quotes` table; the same style applies
here (no `quotation` import, no layering inversion, no cycle). What the kernel needs is exactly one
fact — "an accepted version of this quote exists and it is for this stay" — and `quotation_versions`
carries it (`status='ACCEPTED'`). The three snapshots and the `quotation_acceptances` row remain the
quotation module's richer record of the same yes, pinned by that module's own tests.

**4. The kernel tests now start from a yes.** `persist_quote()` in `tests/test_reservation_kernel.py`
records the accepted version by default (with `accept=False` for the refusal tests), so the seventeen
existing confirmations keep testing what they were written to test — booking mechanics — on a valid
commercial basis. The new `TestCommercialGate` class pins the two refusals and the positive control.

## Consequences

- §9's commercial gate is no longer a gap: an unanswered offer cannot become a contract, and the
  ADR-0048 note that made the gap visible stays accurate for the *state at that time*.
- The remaining §9 limb is the **financial** gate (`required_client_funds_verified`): the estate
  records `amount_paid_minor` on the booking and validates its range, but a *verified funds*
  decision still has no owner module. That is the next recorded gap, not silently assumed.
- Budget-wise the gate costs one indexed read per confirmation, inside the existing transaction.

## Verification

`tests/test_reservation_kernel.py` 71 → 74 tests (two refusals + the positive control) ·
`tests/test_reservation_workers.py` unchanged at 54, both its confirmations already accept first ·
`make check` 3353 passed, 4 skipped; SECRET-SCAN and UNSAFE-SCAN 0 findings; twin parity OK;
PRE-DEPLOY GATE PASS.

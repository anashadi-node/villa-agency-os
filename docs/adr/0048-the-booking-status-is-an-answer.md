# ADR-0048 — The booking status is an answer, and §9's commercial gate has no enforcer yet

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: platform (what the booking worker may be)
- Related: ADR-0045 (a worker asks the door then delegates), ADR-0047 (follow-up as task, hold as slot), master spec §6, §7, §9, §11, §56

## Context

§9 is the **booking confirmation invariant**: `BOOKING_CONFIRMED` is valid only when all applicable
gates are met — the *commercial* gate (the client accepted the applicable commercial version), the
*inventory* gate (an explicit supplier/villa confirmation securing the inventory) and the *financial*
gate (`required_client_funds_verified == true`, plus the supplier-side conditions the policy or
contract requires).

§56 gives the AI exactly one booking capability: `booking.status`, LOW and read-only. There is **no**
capability for confirming a booking, and the architecture's Agent 3 list says why: the service
"coordinates booking workflow after applicable gates are satisfied", may autonomously do "status
communication", and **must not** "claim requested hold is confirmed", "claim reported payment is
received" or "fabricate supplier confirmation".

So RES-W08 is an answer, and the one act that creates a booking stays where it already is:
`ReservationKernel.confirm_from_hold`, called by a human and recorded under that human's actor.

## Decision

1. **RES-W08 reads the three gates from the modules that own them** rather than restating any rule:
   - *commercial* — `QuotationService.current()` for the live version of that stay plus
     `QuotationService.acceptance()` (§11 owns versions and acceptances);
   - *inventory* — the hold row, because §6 makes the **supplier's** confirmation the source of
     truth: `HOLD_CONFIRMED` secures the slot, `RELEASE_CONFIRMED`/`EXPIRED_*`/`CONVERTED` do not.
     A booking secures it while its status occupies the slot (`booking_states.OCCUPYING_STATES`,
     the same predicate the partial index uses);
   - *financial* — the amount and the human who confirmed it, with one honest sentence
     (`FINANCIAL_GATE_NOTE`) saying that §9's `required_client_funds_verified` and the
     policy-specific supplier prerequisites have no home in this estate yet.
2. **The answer cannot be mistaken for a confirmation.** `booking_id` and `status` stay empty until
   a booking row exists; `missing` names §9's own gates that are not satisfied; and a hold the guest
   released leaves `inventory.secures_slot` false. The refusal for "nothing to report" happens
   *before* the door, so a no-op leaves no audit row — the same ordering as RES-W06/RES-W07.
3. **The finding this increment surfaces, deliberately not patched here:** `confirm_from_hold`
   enforces hold state and expiry, quote↔hold unit and policy agreement, `VOID`/expiry, capacity,
   the amount range and slot overlap — but **not** §9's commercial gate. Nothing checks that the
   applicable quotation version was accepted before the booking is written. §0 of the master spec is
   explicit that code contradicting a LOCKED rule must be fixed, and the kernel is the domain's
   final enforcement point — but patching the confirmation path changes who may create a booking,
   and the existing confirmation tests confirm without an acceptance today. That needs its own ADR
   and a migration of those tests, so this increment **makes the gap visible** instead of quietly
   relying on it: a booking whose version was never accepted now answers
   `commercial.accepted == False`, with `commercial` listed in `missing`.

## Consequences

- The booking answer is a **READ**, so it works with no owner allowance and even under §17's
  `READ_ONLY` — the guest can be told where things stand while every MEDIUM act stays closed.
- The financial gate reads as open on every answer until the estate has a home for §9's verification
  fact. That is the honest state, and closing it is a policy/increment change, not a wording change.
- Next increments, in order: (a) enforce the commercial gate in `confirm_from_hold` with its own ADR
  and test migration; (b) bind quotes and holds to the conversation (§9's `quote_reference` /
  `hold_reference`) so a worker need not be handed the hold id; (c) RES-W09…W11
  (amend/cancel assistance, pre-arrival communication, human handoff), each following ADR-0045.

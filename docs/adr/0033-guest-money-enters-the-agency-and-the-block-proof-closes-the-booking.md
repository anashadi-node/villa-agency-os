# ADR-0033 — Guest money enters the agency, and the villa's block proof closes the booking

- Status: Accepted
- Date: 2026-09-28 (owner decision) · written 2026-09-30
- Deciders: owner
- Amended by: ADR-0034 (the three hold stages, and what each one is in the platform)
- Spec: Master Spec §13, §14, §24, §58; AI Workforce Architecture V1.1 §2A.4, §11, §15, §16

## Context

D1 in `docs/GO_LIVE_DECISIONS.md` asks two things at once: **which payment provider**, and **who
receives the guest's money** (the merchant-of-record model). The owner decided the second half on
2026-09-28 and described the flow that follows it. The first half is still open, so **D1 stays
OPEN** — this ADR records the half that is decided.

What is already true on the ground, verified rather than assumed:

- `payments.py` carries a single `provider` plus `provider_intent_id`. ADR-0012 locked the four
  method categories (CARD, BANK_TRANSFER, PAYPAL, STABLECOIN) as a provider-neutral *capability*
  and deliberately left the vendor choice open (§38/§49).
- `src/booking_states.py` already contains every state this flow needs: `HOLD_ACTIVE` →
  `DEPOSIT_PAID` / `PAID_IN_FULL` → `CONFIRMED`, and the guard table already demands evidence on
  all three — `("*", "CONFIRMED")`, `("*", "DEPOSIT_PAID")` and `("*", "PAID_IN_FULL")` each
  require `GUARD_EVIDENCE`. **No new state is needed.**
- `src/reservation_kernel.py` sets `HOLD_CONFIRMED` only together with supplier evidence, so the
  invariant `HOLD_REQUESTED != HOLD_CONFIRMED` is already enforced in code.
- The AI Workforce Architecture the owner supplied describes this exact handoff at §2A.4:
  *Finance Admin → Reservation, "Authoritative payment state changes", passed as "Verified
  state/reference through domain event", and explicitly **not** "Payment credentials or sensitive
  evidence"*.

## Decision

1. **The agency is the merchant of record.** Guest money is received by the agency and never by
   the villa; the villa is paid by the agency. The agency is therefore the party that can refund,
   reconcile, and hold the contract with the guest.

2. **The flow, in the owner's order:**

   | # | Step | Who | What the platform records |
   |---|---|---|---|
   | 1 | The guest pays | guest → **agency account** | payment attempt, then verified receipt |
   | 2 | Handle it and verify receipt from authoritative evidence (bank/provider), never from a customer claim or a screenshot | **Finance** | booking → `DEPOSIT_PAID` or `PAID_IN_FULL` (evidence guard) |
   | 3 | Report to Reservation that payment is accepted | **Finance → Reservation** | a structured domain event (§14), not a chat message |
   | 4 | Send the guest the booking confirmation — the unit is booked | **Reservation → guest** | outbound message tied to the booking |
   | 5 | Send the villa management/owner the update — booked and confirmed, no longer a hold | **Reservation → villa** | outbound message tied to the booking |
   | 6 | Return proof (a letter or an email) that the unit is blocked by the agency | **villa → agency** | evidence row → booking → `CONFIRMED` |

3. **Step 4 has a precondition: the hold must be `HOLD_CONFIRMED`, not `HOLD_REQUESTED`.** A request
   is not a confirmation — the invariant already says so, and telling a guest "booked" on an
   unconfirmed hold is precisely the double-booking the project charter rates *high / high*. The
   supplier evidence that makes a hold real is required before any guest is told the unit is taken.

4. **The villa's block proof is a tracked obligation with a deadline, not a hope.** It becomes a
   task or case with a due time. If it does not arrive, the booking stays out of `CONFIRMED` and
   the case escalates to the Operations/Reservation Manager. Without that, the platform has told a
   guest "booked" and left nothing closing the loop.

## Alternatives considered

- **Villa as merchant of record** — rejected by the owner. It takes the agency out of the money
  path, so refunds, reconciliation and the guest contract all become the villa's to control.
- **Hybrid (deposit to the agency, balance to the villa)** — rejected: two money paths to reconcile
  for a single booking, and the guest gets two payees.
- **A new booking state such as `PAID_PENDING_BLOCK`** — rejected. `DEPOSIT_PAID` and `PAID_IN_FULL`
  already mean exactly "money verified, booking not yet closed", and the existing `GUARD_EVIDENCE`
  on `CONFIRMED` already demands the closing evidence. A new state would duplicate a distinction
  the graph already draws, and would force a DDL `CHECK` change, a PostgreSQL twin regeneration and
  a migration — cost with no new truth.

## Consequences

- **One money path, one closing-evidence rule**, and both are already expressible in the state
  graph. The flow needs wiring (events, messages, the block-proof task), not a schema change.
- **D1 stays OPEN because the provider is not named.** No adapter can be built against an unnamed
  PSP, bank or custodian; until the owner names one per category, the adapters refuse rather than
  fall back (ADR-0012, Security & operational impact).
- **The merchant-of-record consequences are now the agency's**, and this ADR records the decision
  without clearing them: **D5** (PBJT source and who remits it), **D11** (refund and chargeback
  authority) and **D8** (legal review — whether the agency may hold guest funds) all become the
  agency's obligations, not the villa's.
- **Tests to add when the flow is built:** verified payment moves the booking to `DEPOSIT_PAID`; the
  guest confirmation is refused while the hold is only `HOLD_REQUESTED`; a missing block proof past
  its deadline leaves the booking out of `CONFIRMED` and raises a case; and the Finance → Reservation
  event carries the state reference but no payment credentials or sensitive evidence.

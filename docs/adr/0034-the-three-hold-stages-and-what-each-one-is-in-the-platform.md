# ADR-0034 — The three hold stages the business uses, and what each one is in the platform

- Status: Accepted
- Date: 2026-09-28
- Deciders: owner
- Amends: ADR-0033 (the money flow), ADR-0020 (the hold lifecycle), ADR-0019 (vocabulary discipline)
- Spec: Master Spec §7, §11, Appendix A; AI Workforce Architecture V1.1 §16

## Context

The owner described the reservation journey in three stages (2026-09-28):

1. **Tentative Holding** — waiting for the guest to agree and to commit to paying.
2. **Request Hold** — the guest is paying into the agency's account, by whatever method.
3. **Holding Booking Confirmed / Villa Booked** — the funds are confirmed received in the
   agency's account.

And the failure path: if the guest does not pay or cancels, **the Reservation division sends the
villa management/owner a release-hold letter or email**.

The platform already has two vocabularies this journey has to land on, and neither was built from
it — which is exactly the drift ADR-0019 and ADR-0020 exist to prevent:

- **`src/hold_states.py`** — the *supplier-side* hold, nine states: `REQUESTED`,
  `AWAITING_SUPPLIER`, `CONFIRMED`, `REJECTED`, `EXTENSION_REQUESTED`, `RELEASE_REQUESTED`,
  `RELEASE_CONFIRMED`, `EXPIRED_PENDING_VERIFICATION`, `CONVERTED`.
- **`src/booking_states.py`** — the *booking*, 32 states, of which this journey uses
  `HOLD_REQUESTED`, `HOLD_ACTIVE`, `HOLD_EXPIRED`, `PAYMENT_PENDING`, `DEPOSIT_PAID`,
  `PAID_IN_FULL`, `CONFIRMED`, `CANCELLED`.

## Decision

1. **The three stages are business-facing labels, not new states.** They name the journey for the
   reservations team and the guest; the platform's canonical states stay as they are. **No new
   state, no schema change, no migration** — the whole journey is already in the graph.

2. **The mapping:**

   | Business stage | Booking state | Hold state | What must be true |
   |---|---|---|---|
   | **1. Tentative Holding** | `HOLD_REQUESTED` | `REQUESTED` / `AWAITING_SUPPLIER` | the unit is spoken for while the guest decides; the villa has **not** confirmed the hold yet |
   | **2. Request Hold** | `HOLD_ACTIVE` → `PAYMENT_PENDING` | `CONFIRMED` | **the villa has confirmed the hold** — money is never accepted against an unconfirmed hold |
   | **3. Holding Booking Confirmed / Villa Booked** | `DEPOSIT_PAID` / `PAID_IN_FULL` → `CONFIRMED` | `CONVERTED` | funds verified in the agency's account from authoritative evidence (ADR-0033); `CONFIRMED` additionally requires the villa's block proof |
   | **cancelled / never paid** | `CANCELLED` | `RELEASE_REQUESTED` → `RELEASE_CONFIRMED` | Reservation sends the villa the release letter or email |

3. **The invariant gets sharper, and it is the reason stage 2 is a gate rather than a label:**
   a guest paying is only safe once the villa has **confirmed** the hold. `HOLD_REQUESTED !=
   HOLD_CONFIRMED` already says this, and stage 2 is where it bites — accepting money against a
   hold that is still `REQUESTED` or `AWAITING_SUPPLIER` means collecting from a guest for a unit
   the villa may never have agreed to hold. That is the double-booking the project charter rates
   *high / high*.

4. **One place where the label and the state disagree, and it is named rather than smoothed over.**
   Stage 3 is called *"Booking Confirmed"*, but the platform's `CONFIRMED` state is reached only
   when the villa's block proof is in hand (ADR-0033 step 6). Money verified on its own is
   `DEPOSIT_PAID` or `PAID_IN_FULL`. So when the reservations team says "Confirmed" at stage 3, the
   platform is still at `DEPOSIT_PAID`/`PAID_IN_FULL` until the proof arrives. **Anyone reading the
   two side by side must know that:** a report that counts `CONFIRMED` as "money in" undercounts
   paid bookings, and a report that counts `DEPOSIT_PAID` as "fully booked" overcounts. The mapping
   is recorded here so no report has to guess.

5. **The release path is a supplier round-trip, not a local decision.** A guest who never pays or
   cancels puts the hold through `RELEASE_REQUESTED` → `RELEASE_CONFIRMED`. This matches the rule
   §7 already enforces: **an expiry is not a release** — a lapsed hold lands in
   `EXPIRED_PENDING_VERIFICATION` and keeps occupying its slot until an authoritative source closes
   it (ADR-0020 rule 6). So "the guest never paid" and "the guest cancelled" take the same road, and
   the villa's answer is what closes it. The letter or email to the villa is the evidence, and the
   ADR-0033 rule applies unchanged: **a tracked obligation with a deadline**, not a hope.

## Alternatives considered

- **Add three new hold states (`TENTATIVE`, `REQUEST_HOLD`, `BOOKED`).** Rejected: they would
  restate distinctions the two graphs already draw, and each addition forces a DDL `CHECK` change,
  a PostgreSQL twin regeneration, a migration and a re-pin of the restatements ADR-0020 already
  pins. Cost with no new truth — and a second vocabulary for the same facts.
- **Rename the canonical states to the business words.** Rejected: `REQUESTED` / `AWAITING_SUPPLIER`
  / `CONFIRMED` are the supplier's words from §7 Appendix A, and the hold table is where the
  supplier's answer is recorded. Renaming would put the business's vocabulary into the supplier's
  record.
- **Treat "the guest never paid" as an expiry rather than a release.** Rejected: it would free the
  slot on the platform's own say-so while the villa may still be holding the unit.

## Consequences

- **Nothing to migrate.** Every stage the owner named is already a state in the graph; this ADR
  names the mapping so the reservations team, the dashboard labels and the reports can share one.
- **`docs/BOOKING_STATE_MACHINE.md` gains the stage table**, so the operator-facing document shows
  the journey in the business's words next to the canonical states.
- **Dashboard labels to add**: the inventory calendar marks every occupying hold as `HOLD`
  (ADR-0020). The three stage labels belong there, so the reservations team sees the stage it is
  actually in rather than a generic `HOLD` that hides whether the villa has answered.
- **Still open:** D1's provider half; the release letter/email has no template or task yet; and
  stage 2's gate needs the same test as ADR-0033 — a payment is refused while the hold is only
  `REQUESTED`.

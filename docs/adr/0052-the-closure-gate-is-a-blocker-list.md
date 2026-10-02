# ADR-0052 — §30's closure gate is a blocker list, and it fails closed on facts nobody records

- Status: Accepted
- Date: 2026-10-03
- Deciders: platform + Mas Anas (2026-10-03: enforce the money invariant at §30's booking closure;
  "verified client funds" = a `SUCCEEDED` payment on the booking's invoice)

## Context

§9 makes three gates a condition of `BOOKING_CONFIRMED`: the accepted commercial version, the
secured inventory, and the verified client funds. ADR-0050 enforced the first at the kernel's
confirmation. The third **cannot** be enforced there, and the reason is structural rather than a
choice:

    invoices.booking_id NOT NULL  →  a payment can only exist after the booking exists

so at the moment `confirm_from_hold` runs there is no invoice to settle and therefore no funds to
verify. §30 is where the specification puts the money invariant for the rest of the booking's life:

    BOOKING_CLOSED hanya bila: guest checked out · open guest issues = 0 · financial reconciliation
    complete · supplier reconciliation complete · security deposit resolved · incidents closed ·
    required documents complete.
    Jika tidak, gunakan POST_STAY_PENDING dan tampilkan blocker list.

## Decision

**1. The gate is a judgement, not a guard.** §30 asks the platform to *show* the blocker list, so the
verdict is data — `booking_closure.ClosureVerdict` with one `ClosureCheck` per condition — and the
kernel's `close_booking` refusal carries it (`BOOKING_NOT_CLOSABLE`, `blockers=[...]`). The
reason/evidence/approval guard vocabulary of `booking_states` cannot carry a list, and inventing a
guard for seven conditions would hide which one failed.

**2. Financial reconciliation, as this estate can see it.** The latest `ISSUED` invoice for the
booking, settled by payments whose `status` is `SUCCEEDED` (the schema ties `settled_at` to that
status — Mas Anas's definition, chosen over requiring a human reconciliation case), and **no `OPEN`
`reconciliation_cases` row** against that invoice: disputed money is not reconciled money. A booking
that totals zero needs no invoice and is not blocked by its absence.

**3. Guest issues are the guest's, not the agency's operations.** Open cases for the booking plus
live `GUEST_FOLLOWUP` tasks — the kind RES-W06/RES-W09 file for a guest. A `CLEANING`,
`MAINTENANCE` or `CHECKOUT_INSPECTION` task is the agency's own work and must **not** block closure,
or the closure gate becomes a permanent blocker of its own making.

**4. Incidents are attributed by reference.** `incidents` has no `booking_id`; it carries
`affected_refs` (JSON). An open incident that names this booking blocks closure; one that does not,
or one whose refs cannot be parsed, cannot be attributed to this booking — and attribution is not
guessed.

**5. The facts with no owner fail closed and are named.** Three §30 conditions have no module
recording them: **supplier settlement** (`supplier_route` owns contracts and rates, not money), the
**security deposit's resolution** (the booking stores the amount held, not whether it was returned,
forfeited or applied), and the **required-documents list** (no policy key declares which kinds a
closed booking needs). Each reports itself as a blocker with the reason spelled out.

**Consequence, deliberately.** `close_booking` therefore **cannot succeed in production** until
those three facts have owners; the gate is real and the facts are missing. That is the honest
reading of a closure gate: a booking closed on a fact nobody recorded cannot be reopened honestly,
and a gate that guessed would be worse than no gate. What ships today is §30's blocker list — the
thing the spec asks the platform to show — plus a writer that refuses with it.

**6. `COMPLETED → CLOSED`, and `POST_STAY_PENDING` is deferred.** The transition is added to the
kernel's own fail-closed set. §30 also names `POST_STAY_PENDING` as the state the blockers are shown
in; that status does not exist in the schema, and adding a booking status means touching the DDL
CHECK, `booking_states`, the twin PostgreSQL schema and the sync test. A state nothing can enter is
worse than no state, so today the blockers travel in the verdict and the refusal, and the state waits
for the post-stay writer that would enter it.

**7. Closing twice is a no-op; refusals write nothing.** Same discipline as every other kernel
writer (ADR-0048/0050): the verdict is computed inside the transaction, and a refusal leaves
`status`, state events and audit untouched.

## Consequences

**Positive.** The money invariant has one home, it is auditable, and the refusal tells an operator
exactly what is unfinished. The gate reads each fact where its owner records it and judges no
provenance of its own. The three gaps are now visible in the product's own vocabulary rather than in
a document.

**Negative.** No booking can be closed yet (see decision 5). Operators see three blockers they cannot
clear, which is correct and uncomfortable in the right way.

**Tickets.** (a) Supplier settlement needs an owner module (settlement facts between agency and
supplier, with evidence); (b) the security deposit needs a resolution fact (returned / forfeited /
applied, by whom, with evidence); (c) the required-documents list needs a policy key (e.g.
`required_document_kinds` in the §11 payload) so a tenant declares it; (d) `POST_STAY_PENDING` lands
with the post-stay writer; (e) RES-W08's financial reading can now cite §30's reconciliation instead
of the booking row's amount alone.

## Tests

`tests/test_booking_closure.py` (11 tests): the seven conditions in §30's order with the exact
blocker tuple, the three unowned facts failing closed by name, a settled invoice satisfied and
reopened by a dispute or a short payment, an open case / live follow-up / open incident blocking and
their closed counterparts not blocking, attribution of an incident that names another booking, a held
deposit blocking until the amount is zero, the evaluator refusing an unknown booking, the kernel
refusing with the blocker list while writing nothing, and the kernel writing the closure once (state
event, audit, idempotent replay).

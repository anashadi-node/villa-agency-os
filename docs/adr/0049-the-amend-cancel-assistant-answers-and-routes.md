# ADR-0049 — The amend/cancel assistant answers and routes; a person decides

- Status: Accepted
- Date: 2026-10-02 · written 2026-10-02
- Deciders: platform (what the amend/cancel worker may do without a human)
- Refines: ADR-0045 (a capability worker asks the door then delegates) · ADR-0048 (an answer may
  not make a claim the platform has no basis for)
- References: §9 (Booking Confirmation Invariant) · §11 (commercial versions) · §13 (conversation
  ladder) · §17 (kill switch) · §21 (deny registry) · §56 (the closed capability catalogue)

## Context

The workforce architecture names the ninth reservation capability **RES-W09 — Amend / Cancel
Assist**. §56 (the closed catalogue the identity suite pins) has no `cancel.*` and no `amend.*`
name at all, and deliberately so: a cancellation moves money (the ladder, a refund, a penalty) and
an amendment re-prices an offer. What the catalogue does carry is

- `villa.answer_verified` — LOW/READ, "verified answers"; and
- `request.coordinate_special` — MEDIUM, "special-request coordination".

`request.coordinate_special` had **no caller anywhere in the estate** (ADR-0023 and ADR-0024 both
record it as a §56 tool with no enforcement point), and §9's own MEDIUM band is described as
"special-request coordination … require explicit policy-controlled tools".

## Decision

**1. The question is a read; the decision is not ours.** `cancellation_terms()` answers what the
terms the guest *accepted* say about cancelling, through the LOW/READ `villa.answer_verified` (the
same catalogue name RES-W04 answers a price with — one name, two questions) and therefore works
even in the §17 READ_ONLY kill switch, writing nothing. The binding ladder is read from the
**TERMS snapshot of the accepted version** (§11), never from the policy in force today: quoting
today's policy is how a guest is shown a term nobody signed. Three honest edges are answers, not
refusals: no accepted version ⇒ `binding is None` and a note that there is nothing binding to
read; an accepted version with no ladder recorded ⇒ `TERMS_NOTE_NO_LADDER`; a notice inside every
threshold ⇒ `applicable is None` with `TERMS_NOTE_NO_STEP`, because *no step applies* is not
*0 % refund* and a missing rule must never be turned into a money figure.

**2. The request is routed, not decided.** `special_request()` files the guest's change as a
`GUEST_FOLLOWUP` ops task (booking as the subject once one exists, otherwise the case) through
MEDIUM `request.coordinate_special`, deny-first until the owner publishes
`security.ai.medium_tool_allowance`. The worker does **not** raise an approval, and the reason is
structural rather than cautious: `approvals._approver` refuses a machine principal outright ("an
approval is a human act"), `roles.ROLE_RANK` gives `ai_service` no rank, so a `requested_role`
could only be filled by impersonating a person — and every tool that would move money
(`refund.approve`, `refund.execute`, …) is HIGH and in the §21 deny registry. The same reasoning
already governs the collections sweep: the machine opens the task, a person decides.

**3. The task's runbook is required, and that is a recorded gap.** `ops_tasks.kind` is a closed
CHECK vocabulary of eight values and the row has no narrative column, so a routed cancellation
cannot be named by the row itself. The worker therefore refuses to file work with an empty
runbook (`RES_WORKER_SPECIAL_REQUEST_NO_STEPS`): the runbook is the only field that can say what
was asked. A first-class request object — a `CANCELLATION_REQUEST`/`AMENDMENT_REQUEST` kind, or a
request table a decision can attach to — needs its own ADR and schema change; this increment
records the gap instead of guessing at it.

**4. A hold's quote is not the accepted version.** The kernel prices a hold's window into a quote
row of its own, so following `holds.quote_id` would never find the guest's yes. When only a hold
exists the accepted version is found by walking the **stay's** version chain through
`quotation.versions(...)` and taking the last `ACCEPTED` one; when the booking names its
`quote_ref`, `quotation.accepted(quote_id)` reads it directly (highest accepted version governs
if a re-negotiation left two). Both paths go through the module that owns the versions.

**5. Two new reads, both additive.** `policy_engine.cancellation_step(ladder, notice_days=…)` is
the ladder rule in the module that validates the ladder (largest `min_days_before` the notice
meets; order-insensitive, because the payload is validated for duplicates and never for order),
and `quotation.accepted(quote_id)` is "the version the yes was recorded against". Neither writes
anything.

## Consequences

- §56's `request.coordinate_special` finally has an enforcement point, closing part of the
  coverage gap ADR-0023/0024 recorded; the catalogue stays closed — no name was invented.
- A cancellation request is as legible as its runbook until the request vocabulary exists; that
  is the honest cost of not inventing a state the platform cannot yet express.
- An amendment the AI *is* allowed to price (a new window through RES-W03→RES-W05) is unaffected:
  this act exists for the part a person must coordinate.
- The terms answer carries no money by construction — its binding payload has exactly eight keys,
  none of them an amount, so no caller can quote a refund the finance module never computed.

# ADR-0021 — The check-availability request: a state the platform asks for needs a row to be in

- **Status**: ACCEPTED (recorded with WAVE 4-A-2, 2026-09-26)
- **Spec references**: §7 (Reservation & Hold Lifecycle), §56 (AI tool envelope and risk tiers),
  Appendix A (availability vocabulary)
- **Supersedes**: nothing. **Amends**: ADR-0019 — it completes the vocabulary that ADR-0019 froze
  and left unreachable.

## Context

§7 opens the reservation flow with:

```
CLIENT INQUIRY → CHECK AVAILABILITY → AVAILABLE → TENTATIVE HOLD REQUESTED → ...
```

Appendix A lists seven availability states. Two of them — `CHECK_REQUESTED` and `CHECKING` — are
states the *platform* drives; no source reports them as a result. ADR-0019 recorded them honestly
as "vocabulary without a flow": `availability_evidence` stores only the answers a source reports,
so nothing in the repository could enter either state. The vocabulary was correct and inert.

The gap was measurable rather than stylistic:

| Question | Before | After |
|---|---|---|
| Can the platform record that it asked a source? | no | `availability_checks` |
| Can a subject's state be `CHECK_REQUESTED`? | no | `AvailabilityService.state_for()` |
| Can a subject's state be `CHECKING`? | no | `AvailabilityService.state_for()` |
| Is the outbound question gated? | n/a | `availability.request`, MEDIUM, §56 |

§56 settles the last row: it names *"availability requests"* among the actions an AI may take only
under policy control, and `availability.request` is already seeded in `ai_tools` as MEDIUM with
`policy_key = security.ai.medium_tool_allowance`. The flow therefore could not be built as an
ungated write path even if that had been simpler.

## Decision

### 1. A request is a row plus an append-only log

`availability_checks` holds the question (unit, window, who asked, when). `availability_check_events`
holds the history (`CHECK_REQUESTED` → `CHECKING` → `ANSWERED`). The state is **derived from the
newest event and never stored in a mutable column**: a stored copy can drift from the log it claims
to summarise, and the log is the one that has to win. Both tables are append-only by unconditional
trigger.

The cost of that choice is visible and accepted: because the open state is derived, a *partial
unique index* cannot express "one open request per window" — a partial index can only test stored
columns. Two open requests for the same slot can therefore coexist. That is a duplicate **question**,
not a duplicate claim: the slot is still guarded by the hold and booking paths, which are the ones
that can overbook. `idempotency_key` covers the accidental retry, which is the realistic case.

### 2. `ANSWERED` is a request state, not an availability state

Appendix A does not contain `ANSWERED`, and it must not: it describes the *request*, which is
finished, whereas the availability state describes the *subject*, which is answered. Once a request
is answered, the request stops having an opinion and `derive_check_state()` returns whatever the
cited evidence says *now* — which is how an answer that has since aged reports
`RECONFIRMATION_REQUIRED` without anything being rewritten.

`tests/test_wave4a2_availability_checks.py` pins the boundary in both directions: `ANSWERED` is in
`CHECK_STATES` and absent from `AVAILABILITY_STATES`.

### 3. One transition writer

`ReservationKernel._transition_check()` is the only code that changes a check's state, and it
consults `availability.CHECK_TRANSITIONS`. `begin_check()` and `answer_check()` are thin callers, so
the map cannot be bypassed by a second writer with its own rules. This mirrors the hold path
(ADR-0020) deliberately: the same class of bug appeared there, and the same shape prevents it.

Two moves are legal, and both are justified rather than permissive:

| Move | Why |
|---|---|
| `CHECK_REQUESTED` → `ANSWERED` | An integration that answers in the same call has nothing to mark in between. Forcing a `CHECKING` step would record a transition that never happened. |
| `CHECK_REQUESTED` → `CHECKING` → `ANSWERED` | The asynchronous case: the question is with the source and an answer is outstanding. |
| `ANSWERED` → anything | Refused. Re-asking is a new request, not an edit. |

### 4. Answering cannot invent an answer — and freshness is not checked here

`answer_check()` requires the evidence to exist, to be the agency's, and to belong to the same unit.
It does **not** check freshness. Freshness is a property of reading (ADR-0019), so refusing to record
what a source actually said because the answer has since aged would lose the fact that it was ever
said. The aged answer reports `RECONFIRMATION_REQUIRED` instead, which is the more informative
sentence.

`answer_check()` is also **not** machine-gated. Recording a source's report is not the outbound act
§56 governs; what authorises a quote or a hold is `gate_availability()` at the point of use, and that
gate reads the evidence, not the request.

### 5. §56 is enforced, not assumed

`request_availability_check()` calls `AiService.authorize(principal_id, "availability.request")` for
machine actors. The gate is deny-first, so **today the machine path is refused**:
`security.ai.medium_tool_allowance` is unpublished, and unpublished means nothing is allowed. That
refusal is the intended behaviour rather than a stub — the flow is complete, and only its policy
value is missing. The owner opens it by publishing the allowance, with no code change.

A human actor is admitted directly. A machine actor that does not name a registered principal is
refused with `MACHINE_PRINCIPAL_REQUIRED`: naming an AI is not identifying one.

### 6. Subject state: answer first, request second

`AvailabilityService.state_for(unit_id, check_in, check_out)` reports:

1. a **usable** answer (`AVAILABLE`, `NOT_AVAILABLE`, `AVAILABLE_BUT_RESTRICTED`) — a fresh "yes"
   stays a "yes" even while somebody asks again, because hiding a valid answer behind an in-flight
   request would be the worse report;
2. otherwise an open request **for exactly this window** — "we are asking" says more than "we have
   nothing", and a request for other dates says nothing about these;
3. otherwise the unusable answer (stale → `RECONFIRMATION_REQUIRED`);
4. otherwise `UNKNOWN`.

## Consequences

**What this opens**

- The §7 flow is walkable end to end: request → `CHECK_REQUESTED` → `CHECKING` → answer →
  evidence-derived state → hold. `CHECK_REQUESTED` and `CHECKING` are reachable as *both* request
  states and subject states.
- Every Appendix A state is now reachable by some path.
- The machine path is policy-controlled by construction: publishing one value opens it.

**What this does not do (honest gaps)**

1. **Evidence is unit-level.** A row carries occupancy ranges, not a window, so rule 1 of
   `state_for()` reads the unit's newest capture and the window narrows only the requests. That is a
   limitation of the existing evidence model, stated in the docstring rather than papered over.
2. **No cancel, no expiry on a request.** §7 names neither, so neither was invented. An unanswered
   request stays open, and `state_for()` will keep reporting `CHECK_REQUESTED`/`CHECKING` for that
   window. It never authorises anything, so the failure mode is a stale *question*, not a stale
   permission.
3. **`CHECK_REQUESTED` as a subject state is only reachable while no usable answer exists.** A unit
   that already has a fresh answer reports the answer. The request state remains visible through
   `check_state(check_id)`.
4. **The MEDIUM allowance is still unpublished**, so the machine path stays closed until the owner
   decides — the same outstanding owner decision already recorded for `quotation.send` (WAVE 4-C).
5. **No dashboard surface yet.** The states are reachable through the service API and tested; wiring
   them into the operator views belongs with the availability/commercial views, not here.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Mutable `status` column on the request | It needs a conditional trigger to stay append-only, and the PG twin generator ignores trigger `WHEN` clauses — the two dialects would silently diverge. |
| Storing the answer on the request row (`answered_evidence_id`) | Requires an `UPDATE`, i.e. rewriting the request row to record what happened; the log records who answered and when, in the same transaction. |
| Forcing `CHECKING` before `ANSWERED` | Records a transition that never happened for a synchronous integration. |
| Refusing machine actors outright (the `option_sets.present()` precedent, `VERIFICATION_REQUIRED`) | §56 classes availability requests as *policy-controlled*, not human-only. Refusing outright would permanently block a permitted action and need rework the moment the owner publishes the allowance. |
| A second module for the request flow | ADR-0019 made `src/availability.py` the single home of the vocabulary; a second deriver would be a second answer to the same question. The writer lives in the kernel because that is where the transaction, the audit chain and the idempotency ledger already are. |

## Verification

- `tests/test_wave4a2_availability_checks.py` — 72 tests, covering the vocabulary, the schema
  (including the append-only triggers and the "an answer must cite evidence" CHECK), the flow,
  the transition refusals, the subject-state precedence rules, tenant isolation, the §56 gate
  in both directions (refused while unpublished; admitted by publishing, with no code change),
  and one test that walks all seven Appendix A states so the claim in this ADR is checked
  rather than asserted.
- `tests/test_wave1e_agency_scope.py` — `availability_checks` added to the root-table guard,
  `availability_check_events` to the derived-scope guard.
- Schema: 56 → 58 tables, 46 → 50 triggers; PG twin regenerated and `--check` clean.

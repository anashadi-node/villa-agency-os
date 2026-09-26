# ADR-0019 — Availability state vocabulary and the restricted state

- **Status:** ACCEPTED (recorded with WAVE 4-A, 2026-09-26)
- **Spec refs:** §7 (Reservation & Hold Lifecycle), §11 (Pricing, Quote & Discount),
  §16 (Policy Engine), §50 (Wave 4 — Availability & Commercial), Appendix A (Canonical
  Status Vocabulary)
- **Supersedes:** the brownfield availability result vocabulary
  (`result IN ('AVAILABLE','OCCUPIED','UNKNOWN')`)

## Context

The brownfield table `availability_evidence` recorded a source's answer as one of three
values: `AVAILABLE`, `OCCUPIED`, `UNKNOWN`. Three things were wrong with that:

1. **§11 forbids the collapse it caused.** §11 requires that a min-stay or occupancy
   restriction produce *"an explicit restricted state, bukan NOT_AVAILABLE"*. With three
   values there was nowhere to put that state, so a supplier saying "yes, but minimum
   three nights" had to be recorded as either an unqualified yes (the restriction is lost)
   or as `OCCUPIED` (a lie the operator cannot act on).
2. **`OCCUPIED` is not a canonical value.** Appendix A fixes the availability vocabulary:
   `UNKNOWN`, `CHECK_REQUESTED`, `CHECKING`, `AVAILABLE`, `NOT_AVAILABLE`,
   `AVAILABLE_BUT_RESTRICTED`, `RECONFIRMATION_REQUIRED`. `OCCUPIED` appears nowhere in it.
3. **There was no way to say "ask again".** Appendix A carries
   `RECONFIRMATION_REQUIRED`; the brownfield vocabulary had no such state, so a stale
   answer could only be read as a current one or discarded as unknown.

No production data existed at the time of this decision (`data/` held no database files),
so widening the vocabulary required no migration and destroyed no history.

## Decision

1. **One home for the vocabulary.** `src/availability.py` owns `AVAILABILITY_STATES`
   (Appendix A verbatim, in its order), `SOURCE_RESULTS`, `REQUEST_STATES` and
   `RESTRICTION_KEYS`. `reservation_kernel.EVIDENCE_RESULTS` is now an alias of
   `SOURCE_RESULTS`, not a second copy.

2. **`SOURCE_RESULTS` is the subset a source may report:** `AVAILABLE`, `NOT_AVAILABLE`,
   `AVAILABLE_BUT_RESTRICTED`, `UNKNOWN`. `CHECK_REQUESTED` and `CHECKING` are states of
   *our* request to a supplier, not answers from one, so a source may not claim them —
   they are named in `REQUEST_STATES` and no flow produces them yet.

3. **The platform state is derived, never stored.** `derive_state()` computes it from the
   evidence row. A stored copy would be a second source of truth that drifts from the
   evidence it describes; the evidence table therefore gains no `state` column.

4. **Freshness is checked before the result.** An out-of-date "yes" is not a current
   "yes": expired `valid_until` or an age past the level's window yields
   `RECONFIRMATION_REQUIRED` (reason `EVIDENCE_EXPIRED` / `EVIDENCE_STALE`), never
   `AVAILABLE`. This is fail-closed in the same direction the gates already were.

5. **A restricted state must state its restriction.** `availability_evidence.restrictions`
   (nullable JSON) carries `min_stay_nights` and/or `max_occupancy`. The invariant
   `ck_evidence_restriction_stated` makes a restricted row without a payload impossible to
   store, and `record_evidence()` refuses first with
   `EVIDENCE_RESTRICTION_REQUIRED` / `EVIDENCE_INVALID_RESTRICTIONS` (unknown key, or a
   value that is not a positive integer). Only a restricted result may carry a payload.

6. **The gate gives the restriction its own denial.** `gates.CODE_AVAILABILITY_RESTRICTED`
   (`AVAILABILITY_RESTRICTED`) replaces what would have been `UNIT_OCCUPIED`, and the
   decision echoes the payload so the operator sees *what* the supplier restricted. The
   `UNIT_OCCUPIED` denial is kept for `NOT_AVAILABLE`, which is a different fact.

## Consequences

- `OCCUPIED` is gone from the schema, the module vocabulary and the tests. A caller that
  passes it now gets `EVIDENCE_INVALID_RESULT` naming the canonical values.
- A restricted evidence **denies** hold and confirm (fail-closed) rather than evaluating
  `min_stay_nights` against the requested stay. The refinement — allow the stay when it
  satisfies the restriction — needs the stay in the gate's inputs and is recorded here as
  a follow-up, not silently skipped.
- `CHECK_REQUESTED` / `CHECKING` are vocabulary without a flow. The availability-check
  request flow (§7's "CHECK AVAILABILITY" step: request to the supplier, await, then record
  the answer as evidence) is the next WAVE 4 slice; until it exists, a state in
  `REQUEST_STATES` is unreachable by construction.
- `RECONFIRMATION_REQUIRED` is derived, so it is visible to the operator without any job
  having to write it; `sweep`-style jobs keep working on evidence age as before.

## Verification

- `tests/test_wave4a_availability_state.py` — 28 tests: the vocabulary equals Appendix A
  verbatim and in order; the DDL pins exactly `SOURCE_RESULTS`; the state is derived from
  the stored row; stale and expired evidence both yield `RECONFIRMATION_REQUIRED` with
  distinct reasons; a restricted row without a payload is refused by the schema *and* by
  the writer; an unknown or non-positive restriction is refused; a restricted evidence
  denies with `AVAILABILITY_RESTRICTED` (not `UNIT_OCCUPIED`) and names the restriction;
  no source file or schema still speaks of `OCCUPIED`.
- `tests/test_gates.py` (DDL pin and the denial cases), `tests/test_reservation_kernel.py`,
  `tests/test_schema.py`, `tests/test_matching.py`, `tests/test_quote_calculator.py`,
  `tests/test_observability.py` — green.
- PostgreSQL twin regenerated (`generate_pg_twin.py --write` then `--check`).

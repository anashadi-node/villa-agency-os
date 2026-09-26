# ADR-0016 — Matching authority and option sets

- Status: **ACCEPTED** (recorded with WAVE 3-B, 2026-09-26)
- Date: 2026-09-26
- Spec: Master Spec v1.1 §5, §6, §18, §34, §39, §50 (WAVE 3), §56, Appendix B

## Context

WAVE 3-B covers the last two slices of §50's WAVE 3 list: matching and option sets.
`src/matching.py` and the `match_runs`/`match_candidates` tables already existed, so this
wave had to decide how the *existing* matcher relates to the requirement layer WAVE 3-A
introduced, and how a client-facing offer is modelled without inventing structure the spec
does not describe.

Two facts forced the shape of the answer:

- §6's source-of-truth matrix names *current trip requirements* as the authority for a stay.
  The matcher was ranking on `intake.accepted_facts` — the extraction-proposal layer — which
  contradicts §12's "AI_EXTRACTED ≠ VERIFIED" and the WAVE 3-A decision that requirements,
  not proposals, are confirmed truth.
- The PostgreSQL twin generator (`scripts/generate_pg_twin.py`) translates every trigger into
  an unconditional `villa_reject_mutation()` and ignores `WHEN` clauses. A guard expressed as
  a conditional trigger would therefore mean one thing in SQLite and another in PostgreSQL.

## Decision

1. **Matching ranks on confirmed requirements (§6).** `facts=None` loads
   `CrmService.confirmed_facts(case_id)`, which returns the ACTIVE `inquiry_requirements`
   typed by `value_type`. Accepted intake proposals alone are *not* enough: a case with
   proposals but no confirmed requirement fails closed with `MISSING_FACTS` and writes
   nothing. `intake_fields` keeps its role as the review queue, not as criteria.

2. **The run records which source it used.** `criteria.provenance.facts` is
   `crm.inquiry_requirements` when the facts were loaded, and `caller` when they were passed
   in. A static "requirements" label would have been a lie for every caller-supplied run.

3. **Money requirements must be whole minor units.** `value_type='money'` converts to an
   integer number of minor units and refuses a fractional value with `MONEY_NOT_INTEGRAL`
   instead of rounding it — the platform's money rule is integer minor units plus an
   ISO-4217 currency, so a fractional minor amount is a defect, not a rounding opportunity.

4. **The matching path is tenant-scoped.** `match()` and `explain()` take
   `agency_id` (default `agency_default`, the repo-wide compatibility default). Inventory
   requires *both* the unit and its property to belong to the agency — a unit whose owning
   property disagrees is incoherent and is excluded rather than trusted. A case outside the
   agency raises `MATCH_CASE_NOT_FOUND`; a run outside the agency is reported as
   `MATCH_RUN_NOT_FOUND`, i.e. indistinguishable from a missing one.

5. **An option set carries no Appendix B reference.** Appendix B assigns references to seven
   entities (`INQ-`, `QT-`, `RSV-`, `INV-`, `PR-`, `INC-`, `APR-`) and an option set is not
   among them. Minting an `OPT-` prefix would be inventing an identifier scheme the spec does
   not define, so a set is identified by its inquiry plus its version.

6. **Option sets are append-only, and presentation is its own row.** `option_sets`,
   `option_set_items` and `option_set_presentations` carry unconditional immutability triggers,
   so nothing a guest has been shown can be edited afterwards — the guarantee is in the
   database, not in this module's diligence. There is deliberately **no mutable `status`
   column**: "presented" is derived from the existence of a presentation row. This is also
   what keeps SQLite and PostgreSQL identical, because the triggers stay unconditional
   (see the twin-generator constraint above). A change to a set is a new version
   (`uq_option_sets_version`), which is the same append-only shape as `contract_versions`,
   `rate_versions` and `match_runs`.

7. **Presenting an option set is human authority (§56).** Sending options to a guest is a
   client communication, which §56 classes as medium risk and reserves for policy-controlled
   tools. No policy engine exists yet, so `present()` fails closed for machine identities
   (`MACHINE_ACTOR_REFUSED`) instead of pretending to gate them. Building a set from a
   deterministic search is low risk under §56 and stays open to any actor — who is recorded.

8. **Provenance to a match run is optional, and no score is invented.** §34 forbids an opaque
   magic score; it does not forbid a human offering a villa the ranking never produced. An item
   may therefore cite `source_match_run_id` (which must belong to the same inquiry) or omit it,
   and this module stores the order the agency chose — never a ranking of its own.

## Consequences

- WAVE 4 pricing reads `inquiry_requirements` through the same `confirmed_facts` accessor, so
  the authority rule is shared rather than re-implemented.
- Because an option set is immutable, a "builder" UI edits a draft client-side and saves a new
  version; there is no partial-edit API by design.
- Open: the policy engine that §56 requires for AI-initiated client communication is still not
  built (WAVE 1 foundation). `present()` refuses machine actors in the meantime.
- Open: option-set items are not priced. Quotation snapshots and comparison figures belong to
  Pricing & Quotation (WAVE 4).
- `option_set_items.property_id` is not restricted to `property_type='villa'`: the domain is
  villa-led, but an agency may legitimately offer an apartment or a homestay.

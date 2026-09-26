# ADR-0025 — Quotation versioning, the three snapshots, and the acceptance record (§11)

- **Status**: ACCEPTED (recorded with WAVE 4-C, 2026-09-26)
- **Spec references**: §11 (Pricing, Quote & Discount — immutable versioning and the acceptance clause), §6 (source-of-truth matrix: "Quoted terms → Quotation Version + immutable snapshots"), §56 (medium-risk policy-controlled tools), §7 (CLIENT DECISION → CLIENT INVOICE)
- **Amends**: nothing. **Supersedes**: nothing. WAVE 4-D (discounts) and the rate-version half of §11 remain open.
- **Test evidence**: `tests/test_wave4c_quotation_versions.py` (41 tests)

## Context

§11 states four things about a quotation, and only the first was true of this platform before this wave:

> *"Quotation memakai immutable versioning V1/V2/V3; sent/accepted versions tidak di-overwrite. Quote acceptance menyimpan quotation_version_id, price_snapshot_id, terms_snapshot_id, policy_snapshot_id, accepted_at."*

Six measured facts decided the shape.

1. **The priced offer already existed; the *record* did not.** `quotes` holds the amounts, lines, `policy_version_id` + `policy_fingerprint`, `rate_card_id`, a `fingerprint` and a status, and `quote_calculator.calculate_quote()` produces exactly that row. There was **no version chain and no acceptance table**: a stay could be re-priced and nothing recorded what the guest had actually been shown.
2. **Nothing enforced immutability.** `quotes` carries no trigger at all, so "a sent version is not overwritten" was a sentence in the specification and not a property of the system.
3. **The vocabulary already existed.** `contract_versions` and `rate_versions` both use `SUPERSEDED` for a version a newer one replaced, and `quotes.status` already uses `DRAFT/SENT/ACCEPTED/EXPIRED/VOID`. The quotation chain therefore invents no word: it is the `quotes` vocabulary plus the house's own `SUPERSEDED`.
4. **§6 names snapshots, not views.** The source-of-truth matrix says "Quoted terms → Quotation Version + immutable snapshots", so the acceptance has to point at frozen rows rather than at whatever the live tables say later.
5. **The calculation already carries its own hash.** `quote_calculator.Quote.fingerprint` covers the inputs and the amounts, so the price snapshot can cite the calculation's fingerprint instead of inventing a second one.
6. **The schema loader refuses `CASE … END` inside a trigger body, by design** ("this schema does not use it, and guessing would be worse than failing"). The first version of the freeze trigger used one and broke `split_statements` — the condition belongs in the trigger's `WHEN` clause.

## Decision

1. **Three tables.** `quotation_versions` (the chain), `quotation_snapshots` (the record of one version), `quotation_acceptances` (the guest's yes). The **lineage is the stay** — `(agency_id, unit_id, check_in, check_out)` — which is the identity `quotes` already uses (`ix_quotes_unit_window`), so re-quoting the same dates is V2 of the same quotation rather than a second, unrelated offer.
2. **The vocabulary is reused, not invented**: `DRAFT`, `SENT`, `ACCEPTED`, `SUPERSEDED`, `VOID`. `SUPERSEDED` is the word the contract and rate chains already use; the module restates the same set the CHECK constraint enforces, and a test pins the two together.
3. **Immutability is a database property.** `trg_quotation_versions_frozen_content` freezes every content column of a version whose status is `SENT` or `ACCEPTED` — only `status` may still move forward — and `DELETE` is refused on all three tables. A module that forgot would not lose the guarantee, which is the same reasoning ADR-0016 §6 used for option sets.
4. **Snapshots are written when the version is *sent*, one per kind** (`UNIQUE (quotation_version_id, kind)`), each with a payload, a SHA-256 fingerprint, and a `source_ref` naming the row it came from. The three come from the sources that own the facts:
   - **PRICE** — the stored quote: amounts, lines, nights, guests, `quote_fingerprint`, expiry.
   - **TERMS** — what binds the guest: deposit %, service fee, tax, max discount, quote validity, minimum stay, cancellation ladder.
   - **POLICY** — the identity and provenance of the priced policy version (`policy_version_id`, its fingerprint, effective window, `source_doc_ref`, `approved_by`), so "which policy said this was allowed" survives the publication of the next one.
5. **An accepted version cannot gain a snapshot** (`trg_quotation_snapshots_after_accept`): the record has to be complete *before* the guest answers, never assembled afterwards.
6. **The acceptance stores §11's five references exactly** — `quotation_version_id`, `price_snapshot_id`, `terms_snapshot_id`, `policy_snapshot_id`, `accepted_at` — plus who recorded it, with `UNIQUE (quotation_version_id)` so the guest's yes is recorded once.
7. **Sending is the §56 gate**, on the catalogue's own `quotation.send` tool (MEDIUM, "Quotation sending"), through the shared `ai_service.require_tool_authority` (ADR-0024). Deny-first: while the allowance does not name the tool a machine actor is refused with `AI_MEDIUM_TOOL_NOT_ALLOWED` *and* an `AI_TOOL_REFUSED` audit row, and nothing is written — no snapshot, no state change.
8. **Accepting is not a §56 tool.** The catalogue names no "accept" capability, and the act being recorded is the guest's decision rather than the platform's. What the platform guarantees is the *record*: the version plus the three snapshots that already existed.
9. **`void()` exists because the vocabulary must be reachable.** ADR-0016 §6 refused to declare `NOT_REQUESTED` precisely because no row can be in that state; a `VOID` in the CHECK that nothing could write would be the same kind of claim. Withdrawal is not a deletion: the row, its snapshots and its audit trail stay readable.
10. **One transition writer** (`_move`) driven by an explicit table, mirroring `hold_states.py`. An unknown state has no outgoing edges, so a typo refuses rather than passes.
11. **Superseding happens at send, and only over a version that is still `SENT`.** `supersedes_id` records the version the new draft *will* replace (the newest **sent** one — an unsent draft was never an offer), and the status actually moves only when the new version is sent. An accepted or voided version is history; §11 forbids overwriting it, so the chain records what happened rather than what the newest version would prefer.

## Consequences

**What this opens**

- The guest's agreement is now a record with five references, so a dispute can be settled against what was sent rather than against what today's tables happen to say.
- Re-quoting is V2: V1 stays readable, and the freeze is enforced by the database rather than by discipline.
- Sending has a real §56 enforcement point, so the owner can open machine sending by publishing `quotation.send` on the allowance — no code change.

**What this does not do (honest gaps)**

1. **The lineage is the stay, not the inquiry.** `quotes` carries no `case_id` in this schema, so a quotation is not yet tied to the guest conversation. WAVE 4-A-2 linked availability *requests* to cases; quotes were never linked, and inventing the link here would assert a relationship the data does not have.
2. **`quotes` itself is still mutable.** The *quotation version* freezes, but the priced row underneath carries no immutability trigger: a direct UPDATE changes what a *future* version would snapshot. Closing that means either a trigger on `quotes` or making the version chain the only reader path — a decision with its own blast radius (the kernel's `persist_quote` and the booking flow read it).
3. **`EXPIRED` is not a state of the chain.** Quote validity travels as `expires_at` inside the TERMS snapshot, and nothing sweeps an expired version into a terminal state. §11 does not ask for that transition, and inventing a state with no writer is what Decision 9 argues against.
4. **Discounts are untouched** (WAVE 4-D). The price snapshot records the discount that was applied; nothing evaluates, authorises or margin-guards one yet.
5. **Nothing forbids a second offer for a settled stay.** The chain permits sending V3 over an accepted V1; no rule refuses it. Whether a settled stay may be re-quoted at all is a commercial decision, not a schema one.
6. **The acceptance has no evidence artefact.** It names an actor and a timestamp, not the guest's message or click. §6's "client funds received" requires an authorised human-verified settlement, and the same evidentiary bar has not yet been applied to the acceptance itself.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Add a `version` column to `quotes` and version it in place | The freeze would live in the same row it has to protect, and the three snapshots §11 names would still have nowhere to go. |
| Store the snapshots as JSON columns on the version row | §11's acceptance has to name three *different* snapshot ids; one wide row cannot be referenced that way, and a single UPDATE would rewrite all three. |
| Enforce immutability in `quotation.py` only | A guarantee that depends on a module remembering is the guarantee the next refactor removes. ADR-0016 §6 already put the same rule in the DDL for option sets. |
| Take the snapshots at accept time | "What the guest agreed to" would be assembled *after* they agreed. §11's list is a record, not a re-derivation. |
| Derive the price snapshot from the live quote on read | Then the record is a view, and gap 2 above becomes a silent rewrite of history instead of a documented gap. |
| Add an `EXPIRED` transition or a `SENT → EXPIRED` sweep | Nothing in §11 asks for it, and a state nothing can write is a claim the platform cannot deliver (ADR-0016 §6, `NOT_REQUESTED`). |
| Use `CASE … END` inside the freeze trigger | `schema_loader.split_statements` refuses it by design; the condition belongs in the `WHEN` clause. Recorded because it cost a cycle. |
| Gate `accept()` behind a §56 tool too | §56's catalogue names no accept capability, and the recorded act is the guest's decision. Inventing a tool would assert authority the specification does not describe. |
| Delete a draft instead of voiding it | A draft can have snapshots and audit rows; deleting it would leave the chain with a hole and an audit trail pointing at nothing. |

## Verification

- `tests/test_wave4c_quotation_versions.py` — 43 tests, in five groups: the chain (V1/V2/V3, the lineage, cross-agency refusals), sending (three snapshots with their sources and fingerprints, what each payload holds, sending twice, superseding, `current`), the §56 gate (human admitted, machine refused while unpublished, principal required, publishing `quotation.send` opens it, another tool does not, nothing written on refusal), the §11 immutability (content UPDATE refused by the trigger while a status-only UPDATE is allowed, a draft is *not* frozen, DELETE refused, snapshots append-only, and the record keeps the price the guest agreed to after `quotes` itself is changed), and acceptance (the five references, recorded once, a draft refused, a superseded version refused, an accepted version cannot gain a snapshot, append-only).
- Two tests pin the module's vocabulary to the schema's CHECK constraint, so a state added on one side without the other fails the suite.
- Full suite and `make check` → PASS. The PostgreSQL twin was regenerated and matches. Schema: **61 tables / 57 triggers / 53 indexes** (was 58 / 50 / 47).

### Correction after the first cut

- The first cut shipped `ck_quotation_versions_sent` as `(sent_at IS NULL AND status IN ('DRAFT','VOID')) OR (sent_at IS NOT NULL AND status IN ('SENT','ACCEPTED','SUPERSEDED'))`. That refuses `SENT → VOID` — a transition the module's own table offers and `void()` performs — so withdrawing an offer that had already been sent raised a raw `IntegrityError` instead of doing its job. The suite did not catch it: every void test withdrew a *draft*.
- It surfaced from the fresh-checkout audit, not from the tests: a probe replayed the constraint verbatim and got `CHECK constraint failed` on `UPDATE t SET status='VOID'` for a sent row.
- The constraint now states the invariant directly — a draft has never been sent, a version that was sent carries the timestamp, and a void version is either case, because a withdrawal must not erase the send. Two tests cover the sent-withdrawal path, including that `sent_at` survives it and the snapshots stay readable.
- Recorded rather than tidied away: a vocabulary and its CHECK can disagree in a direction no green suite sees, and "the tests pass" is not the same claim as "every transition the table offers is reachable".

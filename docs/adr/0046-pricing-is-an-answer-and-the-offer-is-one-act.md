# ADR-0046 — Pricing is an answer and the offer is one act; the §56 catalogue is not ours to extend

- Status: Accepted
- Date: 2026-10-02
- Deciders: platform (where the rules live). The offer's risk tier stays the owner's: sending is
  MEDIUM and stays behind a published allowance.
- Related: ADR-0045 (a capability worker asks the door then delegates), ADR-0025 (§56 tools),
  ADR-0035 (§9 employee)

## Context

§9 gives the reservation employee two steps between ranking and holding: *Pricing/Rate
Calculation* and *Interface Quotation*. Four things were missing between the pieces that already
existed:

- `quote_calculator.calculate_quote` was tested and had **no production caller**.
- `policy_versions` could be **written** but not **read back**: nothing in `src/` constructed a
  `PolicyVersion`, so a tenant's version in force had no object to price against.
- The two selection rules pricing needs — *which policy covers this stay*, *which rate card does*
  — existed only as **private** helpers inside `matching` (`_policy_for`, `_rate_card_for`).
- The §56 catalogue named neither step.

**The first attempt was wrong, and the platform said so.** It added two catalogue rows,
`quote.calculate` and `quotation.draft` (both LOW), plus their `TOOL_EFFECTS` entries and a
migration. The identity suite refused it:

```
test_every_spec_56_capability_is_registered_with_its_tier
assert set(registered) == {n for names in SPEC_56_TIERS.values() for n in names}
```

The catalogue is not a place to record what the code can do: it **is** §56's list, pinned. A tool
§56 does not name cannot be granted to an AI — so inventing one does not widen the AI's
authority, it makes the platform's own vocabulary lie. Both rows were removed.

## Decision

**Pricing is an answer, and §56 already names it.**
`villa.answer_verified` (LOW, READ) is "verified villa questions and answers" — and §11 says the
commercial truth of a price comes from verified rate/contract versions rather than from memory.
That is the same instruction read twice: the AI may *say* what this villa's own rates produce.
`ReservationKernel.quote_stay` is therefore a **derivation that records nothing**; it takes the
policy version from `matching.policy_for`, the card from `matching.rate_card_for`, and the
arithmetic from `quote_calculator`. The priced stay becomes a row only through `persist_quote`,
which the offer path calls after its own door.

**The offer is one act, and §56 names that too.**
`quotation.send` (MEDIUM, owner allow-listed) is the capability; RES-W05 is one gated step that
prices, records, opens the version and sends it. "Draft but do not send" is not a separate
capability — §56 does not name it — and drafting alone would be a step whose only purpose is to
be sent. The door is asked **before** the first write, so a refused offer leaves no quote row,
no version and nothing a guest could be shown.

**A step §56 does not name is either folded into the act §56 names, or read as the capability it
honestly is.** Not invented as a third name. Recording that rule here is the point of this ADR:
the next §9 step must ask "which §56 capability is this?" and, if the answer is "none", either
delegate to the act that has one or refuse to build the step.

**The two selection rules become public, under their existing behaviour.**
`matching.policy_for` and `matching.rate_card_for` (renamed from their underscore forms; the
policy query now selects the whole row). Public because the ranking's minimum-stay basis and the
quote's price must come from **one** chosen card and version. `matching` keeps the rule; the
kernel imports it — the rule does not move to a second home.

**`PolicyVersion.from_row` is the inverse of `as_row`.**
Every field passes the same validation the writer passed, so a stored row cannot become an object
that prices a stay. The stored `fingerprint` column is deliberately **not** read: a fingerprint
is derived, and a column that disagrees with the rules it describes is what a caller wants to
notice. `from_row(as_row())` is identity — pinned by test.

## Consequences

- §56's vocabulary is unchanged: 26 names, and the suite that pins it passes.
- The §17 ladder now has a pair that reads as a sentence: **READ_ONLY still answers a price and
  cannot start an offer** — the answer changes nothing, the offer writes a record and a version.
  RES-W04 stays available with the kill switch on; RES-W05 refuses with `AI_MODE_REFUSED`.
- The offer is deny-first until the owner publishes `security.ai.medium_tool_allowance` for
  `quotation.send` — the same posture as RES-W03's `availability.request`, and for the same
  reason: an act toward a supplier or a guest is the owner's to enable, not the platform's.
- The answer and the recorded price come from the same computation over the same basis, so the
  figure the AI tells and the figure it records cannot disagree — RES-W04 and RES-W05 both call
  `quote_stay`; neither computes anything itself.
- Absent bases refuse by name (`QUOTE_NO_RATE_CARD`, `QUOTE_NO_POLICY`, `UNIT_NOT_FOUND`), never
  at zero and never by guessing a season. A drifted policy row refuses with a code rather than
  being priced on.
- **Known limit, named for the next increment:** neither worker binds the quote to the
  conversation that produced it. §9's conversation state has a `quote_reference` field and the
  ledger here does not carry it yet, so the chain "this offer came from this conversation's
  pricing step" is the caller's today. Recording the reference is what makes it enforceable, and
  it is the natural first move of the hold step.
- §9 progress: five of eleven workers. The remaining six need conversation follow-up, holds,
  bookings, amendments, pre-arrival and handoff — modules that mostly exist, so those increments
  should be workers plus tests rather than new domains. Each of them starts by asking which §56
  capability it is.

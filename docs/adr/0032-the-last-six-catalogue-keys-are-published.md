# ADR-0032 — The last six catalogue keys are published

- Status: Accepted
- Date: 2026-09-28
- Deciders: owner
- Amends: ADR-0017, ADR-0029 and ADR-0031 (all three counted the unpublished catalogue keys)

## Context

ADR-0017 built the key catalogue and left nine of its keys unpublished on purpose. ADR-0029
published the three discount values, ADR-0030 added the FX pair and ADR-0031 published it — and
each of them left the same six keys open: the hold reminder, the reconfirmation freshness, the
payment thresholds, the booking financial gates, the incident escalation ladder and the retention
periods. None of the six was read by a module, so no gate stood silently open; the engine refused
with `POLICY_VALUE_NOT_PUBLISHED`, which is the honest state and not a defect.

The owner decided all six on 2026-09-28. The values are runtime state and live in the pilot
database (`data/villa.db`), which is not in git, so this ADR records them: a decision that only
exists there is a decision that can be lost.

## Decision

| Key | Value | What it means |
|---|---|---|
| `availability.hold.reminder_minutes` | `{"minutes_before_expiry":120}` | remind the team 2 h before a hold expires |
| `availability.reconfirmation.freshness_hours` | `48` | an availability answer older than 2 days is reconfirmed with the supplier |
| `finance.approval.payment_thresholds_minor` | `{"levels":[{"min_minor":0,"roles":["finance","manager"]}],"owner_approval":false,"owner_reports":["daily","weekly","monthly"]}` | every payment needs Finance **and** Manager; the owner does not approve payments — they read the periodic report and correct what looks wrong |
| `finance.booking.financial_gates` | `{"threshold_source":"property_contract","above_threshold_pct":50,"at_or_below_threshold_pct":100,"basis":"contract_rate"}` | the day threshold comes from each villa's contract: above it a 50% deposit, at or below it the full payment |
| `operations.incident.escalation_minutes_by_severity` | `{"S1":5,"S2":20,"S3":40,"S4":80}` | escalate within 5 / 20 / 40 / 80 minutes by severity |
| `retention.records.retention_days_by_class` | `{"P0":"unlimited","P1":3650,"P2":3650,"P3":7,"P4":0,"anchor":{"P3":"check_out"}}` | public information unlimited; guest and administrative transaction data 10 years; identity documents and private credentials 7 days after check-out; P4 never stored |

Each revision carries `approved_by = owner_anas`, the owner's decision as its `source_doc_ref`, and
a window from 2026-09-28. The catalogue is now complete: **25 of 25 keys published**, and
`make policy-status` reports `0 key(s) waiting on an owner decision`.

## Readings taken, so they can be checked

Five of the six answers needed a reading, and each is recorded here rather than buried inside a
value:

- **P3's seven days are counted from check-out, not from creation** — hence `anchor.P3`. A
  retention job that counted from creation would delete the passport while the guest is still in
  the villa.
- **The owner's first wording said payment data is destroyed 7 days after check-out.** The
  repository's own `docs/RETENTION_AND_INCIDENT_RESPONSE.md` §1 and the tax bookkeeping obligation
  require transaction records for ten years, so the conflict was put back to the owner instead of
  being encoded. The owner confirmed the narrower reading: **card documents, passports and private
  credentials are the 7-day class; administrative payment and transaction records are the 10-year
  class.** Encoding the first wording would have recorded a rule that destroys the platform's own
  tax evidence.
- **`P4` is `0` and `P0` is `"unlimited"` because the classification document already decides
  them** — `docs/DATA_CLASSIFICATION_AND_ACCESS.md` §1 says prohibited data (card/CVV/OTP,
  credentials) is never stored and public property information is free of a limit. A day count in
  either place would have been invented.
- **`finance.booking.financial_gates` carries `threshold_source: property_contract`** because the
  owner named the same 45–60 day band in both branches. The band is per villa; the policy states
  the rule and the contract states the number, so the value names its source instead of choosing a
  day count nobody decided.
- **`finance.approval.payment_thresholds_minor` has a single level from `min_minor: 0`** because
  the owner's rule has no lower bound: every payment is dual-approved, and the owner holds no
  approval tier.

**The engine refused two of the first attempts, correctly.**
`availability.hold.reminder_minutes` is declared `JSON`, not `MINUTES`, so `120` was refused
`POLICY_VALUE_INVALID`; and `source_doc_ref` is capped at 200 characters, so two long decision
texts were refused `INVALID_FIELD`. Neither a wrong shape nor an over-long reference could be
written — the fail-closed design doing its job *before* the data, not after.

## Consequences

- **No behaviour changed.** The only files under `src/` and `scripts/` that name any of the six are
  the two schema twins that *declare* the catalogue; no module reads them. Publishing changes what
  the engine *would* decide, not what it does today. The finance and availability gates that read
  them are still to be built.
- **The incident ladder sits inside the response SLA, and they are two clocks.**
  `docs/RETENTION_AND_INCIDENT_RESPONSE.md` §3 sets response at 15 min / 1 h / 4 h / 1 day; this
  value escalates *within* 5 / 20 / 40 / 80 minutes. Escalation is the "tell a manager" clock and
  the SLA is the "fix it" clock — the ladder is deliberately the tighter of the two, and both now
  have a home.
- **Retention became a decidable input; execution is still open.** D9 in
  `docs/GO_LIVE_DECISIONS.md` stays OPEN: this value says *how long*, D9 says *who deletes* and by
  which job. The `retention_until` column and the daily `purge_expired` job that §1 of the
  retention document requires do not exist yet, so the 7-day rule is a decision the platform cannot
  yet carry out — and a rule that cannot be carried out is not yet a rule.
- **Still open:** D1–D14 in `docs/GO_LIVE_DECISIONS.md`; the retention execution job (D9); and the
  twelve-role vocabulary ADR-0017 left open.

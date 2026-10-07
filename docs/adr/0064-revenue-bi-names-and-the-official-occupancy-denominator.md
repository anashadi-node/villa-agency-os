# ADR-0064 — Revenue & BI: canonical worker names and the official occupancy denominator

- Status: **ACCEPTED** (owner delegated Q1 and Q5 of the Agent 6 research to this analysis, 2026-10-07,
  with the instruction to decide as the agency's own management and marketing expert; the owner then
  approved slice 1 proceeding on these definitions)
- Date: 2026-10-07
- Deciders: Mas Anas (owner) · Q1/Q5 authored in `docs/RESEARCH_AGENT6_REVENUE_BI_2026-10-07.md` §10
- Spec: §10 · §18 (metric definitions) · §20 (workforce) · §27 · ADR-0011 (language) · ADR-0014 ·
  ADR-0035 (dashboard roles) · ADR-0061 (tool declarations)

Two decisions were blocking Agent 6 slice 1. Both are commercial judgements, not technical ones, so
they are made here as an agency manager/marketing decision and recorded with the evidence behind them.

---

## Part 1 — Q5: the twelve Revenue & BI workers, named

The canonical list `RBI-W01…RBI-W12` does not exist anywhere in the repository (`docs/AI_WORKFORCE_
TRACEABILITY.md:87` names only the range). Three documents constrain the names and must not be broken:
the 12-slot range, the ten capabilities paraphrased in `docs/STATUS_AND_GAP_MAP.md:216`, and five
numbers already cited by `docs/RESEARCH_AI_WORKFORCE_SKILLS_2026-10-06.md` (W04 demand, W09 forecast,
W10 margin, W11 anomaly, W12 executive report). The list below is those three merged, and those five
numbers keep their positions.

| Code | Worker id (code) | Display name (report-facing) | Owns | Metrics |
| --- | --- | --- | --- | --- |
| RBI-W01 | `revenue_kpi` | KPI Pendapatan | Occupancy, length of stay, cancellations | M1, M6, M8 |
| RBI-W02 | `conversion` | Konversi & Funnel | Inquiry → booking conversion, per channel | M7 × `reservation_kpi.rates` |
| RBI-W03 | `pace_leadtime` | Pace & Lead Time | Pickup curve, booking lead time | M4, M5 |
| RBI-W04 | `demand_intel` | Intel Permintaan | Seasonality and day-of-week demand; **internal data only** | M4 series |
| RBI-W05 | `pricing_intel` | Intel Harga & Diskon | Rate spread, discount depth | M12, M13 |
| RBI-W06 | `villa_performance` | Performa Villa | Ranking per villa with the cause behind the number | derived |
| RBI-W07 | `partner_performance` | Performa Mitra | Supplier/partner performance | derived |
| RBI-W08 | `channel_performance` | Performa Kanal | Channel economics | M7 derived |
| RBI-W09 | `forecast` | Ramalan | Projection **with interval and observation threshold** | M15 |
| RBI-W10 | `margin` | Margin (parsial) | Invoice revenue − recorded supplier cost, labelled partial | M10, M11 |
| RBI-W11 | `anomaly` | Anomali | z-score/MAD over daily series, evidence-listed | M14 |
| RBI-W12 | `exec_report` | Laporan Eksekutif | Bundle M1–M16, every number with lineage | M16 |

Rules that come with the names:

1. `*-Wxx` are **activities of one agent**, not new agents (§20.1) — the twelve names never become
   twelve principals. One principal: `revenue_bi_ai`.
2. Reporting line is **directly to GM + Owner** (`docs/AI_WORKFORCE_TRACEABILITY.md:87`), and the agent
   holds **no write authority** — it computes, compares, forecasts, and hands the numbers to a human.
3. A **forecast is never a fact**: every M15 output carries an interval, the label "ramalan", and the
   observation threshold; below the threshold the answer is `None`, not a number.
4. W09 and W10 are built **last** on purpose: they are where a system most easily invents confidence.
5. The code ids in column two are the canonical identifiers; a display name may change without a code
   change, and `tests/test_revenue_bi_agent.py` asserts the twelve codes match this table.

---

## Part 2 — Q1: the official occupancy denominator

### What the decision has to survive

Occupancy drives pricing intensity, owner reporting and staffing. Two ways to get it wrong:

- **Denominator too large** (raw calendar nights, including villas not yet onboarded or off-market) →
  occupancy is understated → the agency discounts and over-hires against a number that is not real.
- **Denominator too small** (only nights with captured availability evidence) → occupancy is
  overstated, and — worse — the number *moves when evidence capture improves*, which destroys the
  comparability the trend depends on.

In managed-villa practice the denominator is **unit-nights under management**, with out-of-order and
owner-blocked nights removed; that is the number a manager can defend to an owner, and the same
denominator feeds RevPAR so occupancy and RevPAR never disagree about supply.

### Evidence measured before deciding (2026-10-07)

| Check | Result |
| --- | --- |
| Rows in `availability_evidence` (dev database `data/villa.db`) | **0** |
| Rows whose `occupancy_ranges` is not `'[]'` | **0** |
| Code that **reads** `occupancy_ranges` | **none** — `reservation_kernel` only writes it |
| Rows in the whole test corpus with a non-empty `occupancy_ranges` | **0** — every fixture writes `'[]'` |
| `units` (dev database) | 3, all `status='ACTIVE'` |

Choosing evidence-based availability as the official denominator today would therefore make **every**
occupancy figure `None` — the metric would be dead on arrival — and the inner keys of `occupancy_ranges`
(`Mapping[str, str]`) are defined by no producer, so parsing it would mean inventing a convention.

### Decision

1. **Official (v1): managed-inventory occupancy.**
   `occupancy_managed = booked_nights / managed_unit_nights`, where `managed_unit_nights` counts the
   nights inside the window for every unit with `status = 'ACTIVE'` and `created_at` on or before the
   window end. Vocabulary is read from `gates.UNIT_STATUSES`, never re-declared.
2. **Numerator: nights actually sold**, taken from bookings whose status is in
   `booking_states.OCCUPYING_STATES` — the owner of "these dates are locked and sold" — with each
   booking's nights **clipped to the window** so a stay straddling a boundary is counted once, in part.
3. **Excluded nights are reported, never dropped.** `MAINTENANCE` and `SUSPENDED` unit-nights appear as
   `unavailable_unit_nights` beside the ratio. `units.status` has no history, so the exclusion is a
   current-snapshot approximation; the metric's caveat says exactly that.
4. **Companion denominator, defined now and published only with coverage.** `occupancy_sellable =
   booked_nights / sellable_nights`, with `sellable_nights` from `availability_evidence.occupancy_ranges`,
   published **only when coverage ≥ 0.70** (covered unit-nights ÷ total managed unit-nights). Until a
   producer defines the range keys, v1 publishes `None` with the measured reason
   (`evidence_coverage_rows_non_empty = 0`), so the day ranges start arriving the report shows it rather
   than staying silently empty.
5. **One denominator per series.** The two occupancies are never mixed in one trend line and never
   averaged; every published figure carries its denominator label and coverage. RevPAR (M3) uses the
   official denominator, so the two metrics cannot disagree about supply.
6. **Out of scope, stated:** bedroom-level occupancy (the platform holds unit-level inventory only) and
   seasonal supply changes inside a window beyond unit activation.

### Consequences

- Every occupancy number a tenant sees is computable from the kernel's own vocabulary today, and stays
  comparable across periods.
- A villa under renovation reads as "not supply" rather than as "terrible occupancy" — the commercial
  truth a Bali portfolio manager reports to owners.
- The evidence-based number stays a defined, gated promise: it will appear when the availability
  evidence column is actually populated, and not one day earlier.

## Reversibility

Part 1 is a naming table; Part 2 changes no stored value. Switching the official denominator to the
evidence path later is a label change plus the range parser — it does not invalidate any historical
figure, because both series are computed from stored facts.

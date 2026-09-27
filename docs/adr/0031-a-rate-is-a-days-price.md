# ADR-0031 — A published rate is one day's price, and the feed that renews it

- **Status:** accepted (2026-09-28 WIB)
- **Supersedes:** nothing. Amends ADR-0030 (which introduced the conversion this depends on).
- **Decided by:** owner (Mas Anas), in session: *"Kurs USD-IDR harus dibuat sesuai dengan harga
  nyata realtime per hari itu"*, and *"batas umur kurs: 2 hari"*.

## Context

ADR-0030 gave the §11 absolute margin floor a currency and made a booking in another currency
convert the floor through `commercial.fx.rates`. Two things were left open, and both were
deliberately left failing closed:

1. **Where the rate comes from.** Nothing published it. `commercial.fx.rates` was a key with no
   writer, so every rupiah booking refused the discount — correct, but not usable.
2. **How old a rate may be.** A rate is a *price on a day*, not a constant. A number typed once
   and never renewed still converts, still compares, and still *looks* right: the floor is simply
   measured at a rate no market has quoted since. The failure is silent, and it fails open — a
   rate that drifts far enough turns a floor breach into a pass. The owner's answer is a limit:
   two days.

A third question surfaced while publishing, and it is the one most likely to bite in operations:
**which "today"?** The platform dates every revision by its own clock (UTC). The operator's day
is WIB (UTC+7). Between 00:00 and 07:00 WIB those two disagree.

## Decision

1. **`scripts/refresh_fx_rate.py` fetches the day's real rate and publishes it.** It reads a
   public provider (`open.er-api.com` by default, `--url` to point elsewhere), takes the rate as
   the provider wrote it — an exact `Decimal`, never a binary float — and records the provider's
   own quote time in `source_doc_ref`, so the number can be re-read and audited later.

2. **The tool refuses more than it publishes.** Every one of these is a refusal, not a warning:
   a payload with no quote time; a provider-reported failure; a payload based on another
   currency; a rate that is not an exact positive decimal; a quote the provider itself dates
   more than `--max-feed-age-hours` old; a quote dated in the future; and a move larger than
   `--max-move-pct` from the last published rate (the guard against a bad payload — a human
   reviews that one, a schedule must not publish it).

3. **`commercial.fx.max_age_hours` is the owner's limit, and it is policy, not a constant.**
   Published at **48** (two days). `src/fx.require_fresh` applies it in whole days — a revision
   is dated by a *day*, so a limit in hours cannot be honoured to the hour, and where the two
   disagree the **tighter day wins** (36 h refuses from the second day, 48 h from the third).
   An unpublished limit refuses every conversion: the engine will not invent one.

4. **The freshness gate sits on the conversion path only.** A booking already in the floor's
   declared currency asks for no rate and therefore no limit — there is nothing to date. This is
   why the check lives in `DiscountEngine.margin_floor` rather than in `FxTable`.

5. **Revisions are dated by the platform clock (UTC), never by the operator's local day.**
   `--effective-from` defaults to `SystemClock().today()`. Publishing with a local day east of
   UTC produces a revision that starts *tomorrow* by the platform's reckoning: today it is
   invisible (so the conversion refuses), and on the day it appears the freshness gate can refuse
   it as future-dated. The store will not backdate the correction either — a new revision may not
   start before the newest one (`POLICY_BACKDATED_REVISION`), and `policy_revisions` rejects
   UPDATE and DELETE by trigger. **The date is not a detail to fix later; it cannot be fixed
   later.**

6. **The rate table keeps its other pairs.** A refresh updates the pair it fetched and carries
   every other published pair forward — reading the last revision *by version*, not the revision
   *effective today*. A tool that asks the resolver for "today" goes blind to its own last
   revision whenever that revision starts tomorrow, and then drops the sibling pairs on the way
   past.

## Consequences

- Rupiah bookings convert the floor at a rate that is real, dated, and traceable to the document
  it came from. The verified figure: a 25.00 USD floor at the 2026-09-27 quote of 17,897.896326
  is **447,448 IDR**.
- **A stopped feed fails closed after two days.** Discounts on bookings in another currency stop
  being applied, with `FX_RATE_STALE` naming the pair, the day the rate took effect, and the
  published limit. This is the intended failure: a floor measured at a rate nobody renewed is
  worse than a refused discount, because the refusal is visible and the drift is not.
- The daily job is the owner's operational responsibility; the platform cannot distinguish "the
  market did not move" from "nobody ran the job", and does not pretend to.
- Cost of being wrong is asymmetric by design: publishing a bad rate affects a *minimum*, so the
  payload guards (age, band, exact decimal) sit in front of it, and a refused fetch publishes
  nothing at all.
- Two limits now govern the same number and must be kept apart: `--max-feed-age-hours` (how stale
  the *provider's* quote may be before the tool refuses to publish it) and
  `commercial.fx.max_age_hours` (how stale a *published* rate may be before the engine refuses to
  use it). The first guards the writer, the second guards the reader.

## Notes

- **The unpublished-key count is six again.** The catalogue holds 25 keys; 19 are published. This
  ADR publishes `commercial.fx.rates` (the seventh ADR-0030 counted) and adds-and-publishes
  `commercial.fx.max_age_hours`, so ADR-0017, ADR-0029 and ADR-0030 have been corrected to point
  here. The six that remain are `availability.hold.reminder_minutes`,
  `availability.reconfirmation.freshness_hours`, `finance.approval.payment_thresholds_minor`,
  `finance.booking.financial_gates`, `operations.incident.escalation_minutes_by_severity` and
  `retention.records.retention_days_by_class` — all of them keys no production code reads yet, so
  publishing a value would change no behaviour. `make policy-status` is the live list.
- The provider's quote is a *day's* close, so the value changes once a day; re-running the tool
  inside the same day reports `UNCHANGED` and publishes no new revision.
- `scripts/refresh_fx_rate.py` writes to the database named by `VILLA_DB` and refuses to guess one,
  like every other writer here.

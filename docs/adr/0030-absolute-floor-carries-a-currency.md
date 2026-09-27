# ADR-0030 — The absolute margin floor carries a currency

- Status: Accepted
- Date: 2026-09-27
- Deciders: owner
- Amends: ADR-0029 (it published the absolute floor and named this limit as the open item),
  ADR-0026 (the engine it built)

## Context

ADR-0029 published the owner's absolute guard as `commercial.margin_floor_minor = 2500`, meaning
USD 25,00. A count of minor units names no currency. The engine compared that number straight
against `margin_after_minor` in the booking's own currency, so the guard was only correct while
every booking shared one currency.

Bali is priced in USD; a Yogyakarta booking is priced in IDR, and IDR has no minor units — a
rupiah is a whole minor unit. A rupiah booking left with 300.000 IDR of margin is about USD 18,46,
below the owner's USD 25, yet the old comparison read `300000 > 2500` and applied the discount. The
guard was not too weak: it was measuring the wrong thing, silently, in the direction that gives
money away.

The owner asked for the conversion step and for Yogyakarta bookings to be able to use IDR.

## Decision

1. **New catalogue key `commercial.margin_floor_currency` (TEXT)** — the ISO-4217 currency the
   absolute floor is denominated in. Published as `USD`, which transcribes the owner's existing
   decision rather than adding a number: the floor was always USD 25.
2. **New catalogue key `commercial.fx.rates` (JSON)** — `{"BASE/QUOTE": "rate"}`, read as *one unit
   of BASE is worth this many QUOTE*. **No rate is published yet**: that value is the owner's to
   decide, and until it exists an IDR booking refuses rather than converts on an invented number.
3. **New module `src/fx.py`** — conversion built on `primitives.Money`, so both currencies and both
   amounts are validated by the primitive that already owns money:
   - exact decimals only; `decimal_of` refuses floats, and `fx` re-raises that as `FxRateInvalid`
     so a caller catching `FxError` cannot miss a rate that arrived as a binary approximation;
   - default rounding is `ROUND_CEILING` because the callers convert a **minimum**, and a minimum
     rounded down hands out margin the owner never allowed;
   - a missing pair refuses (`FX_RATE_NOT_PUBLISHED`) and lists what *is* published — nothing here
     assumes parity, which is what would read rupiah as dollars;
   - an inverted pair is derived (`1 / rate`) and flagged `derived=True`, so the direction actually
     relied on can be published when the exact number matters.
4. **`src/discounts.py`** — `margin_floor(currency=...)` returns the floor *in the currency of the
   booking it will guard*: unchanged when the currencies match, converted through the published
   table otherwise. `MarginFloor` carries `currency`, plus `base_minor`/`base_currency` when it was
   converted, and the decision and its audit row record both the number the owner published and what
   it became. The booking's currency is now validated through `Money`, so an unknown ISO-4217 code
   refuses before any comparison happens.
5. **Migration `004_currency_aware_margin_floor.sql`** adds the two catalogue rows to databases that
   already exist; `001_initial.sql` seeds them for databases created from now on. Values stay
   runtime state, published through `scripts/publish_policy_value.py`.

## Consequences

- **Two new fail-closed paths.** A floor whose currency was never declared refuses
  (`DISCOUNT_POLICY_NOT_PUBLISHED`), and a booking in another currency with no published pair
  refuses (`FX_RATE_NOT_PUBLISHED`). Neither path assumes parity; both name what they are waiting
  for.
- **IDR bookings cannot discount until the owner publishes a rate.** That is the intended
  trade-off: a refusal is recoverable, a wrong number is not. Bali (USD) is unaffected — measured on
  a copy of the pilot database, a USD 1.500 sale with a USD 100 discount and the published floor of
  2.500 USD → `APPLIED`.
- **With `{"USD/IDR": "16250"}` published on a copy**, the IDR booking that used to pass now behaves:
  the floor becomes 406.250 IDR, so a booking left with 300.000 IDR → `REQUIRES_OVERRIDE`, and one
  left with 380.000 IDR → `REQUIRES_OVERRIDE` as well — 380.000 IDR is 25,33% of that sale, so the
  percentage guard is satisfied, and it is USD ~23,38, below the owner's USD 25. That second case is
  the whole point: the converted absolute guard doing the work the percentage cannot.
- **`canonical_json` accepts a float**, so a rate published as `0.1` would be stored; `fx` refuses it
  at read time (pinned by
  `tests/test_fx.py::TestNothingHereAssumesParity::test_a_rate_is_not_a_float_even_when_the_json_holds_one`).
  The float refusal is a real guard, not defence in depth.
- **This does not replace a supplier cost basis.** The caller still states revenue and margin
  (ADR-0026 open item); the currency step only makes the comparison honest.
- **One more key waits on the owner:** the catalogue now has seven unpublished keys rather than six,
  and `commercial.fx.rates` is one of them. `make policy-status` lists them; the engines that read
  them keep refusing until a decision is published.

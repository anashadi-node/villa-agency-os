# ADR-0012 — Provider-neutral multi-rail payments

- Status: **PROPOSED — pending owner acceptance**
- Date: 2026-09-26
- Spec: Master Spec v1.1 §58, §59, §61 (**LOCKED capability**), §13, §14, §24

## Context

v1.1 locks the *capability* to accept **CARD, BANK_TRANSFER, PAYPAL, STABLECOIN (USDC/USDT)**
through **provider-neutral adapters**, with the canonical invariant
**PAYMENT INITIATED/REPORTED ≠ FUNDS VERIFIED/SETTLED**. Which providers to use stays an open
decision in §49/§38 — so the core model must not encode one.

Existing state: `payments.py` carries a single `provider` + `provider_intent_id`, and
`payments.method` allows only `HOSTED_PAGE, BANK_TRANSFER, CASH, OTHER`. There is no asset/
network, fee, or settlement-verification field, and no adapter boundary.

## Decision

1. **Core stays provider-neutral.** `payments` keeps a canonical `method_category`
   (`CARD`, `BANK_TRANSFER`, `PAYPAL`, `STABLECOIN`), plus `provider` as an *attribute* of the
   attempt, never as a branch in business logic.
2. **One adapter interface** — `PaymentRailAdapter` (protocol): `create_intent`,
   `parse_webhook`, `verify_webhook`, `fetch_settlement`, `refund_request`. Adapters are the
   only place provider SDKs may appear. No provider import in `payments.py` or domain modules.
3. **Canonical money fields**: gross amount/currency, fees when the provider reports them,
   settlement amount/currency, verification source + status + timestamps. Storage remains
   integer minor units; the API exposes decimal strings with ISO-4217 (§23).
4. **Minimum PCI exposure**: hosted fields/checkout and tokenization only. Never store PAN,
   CVV/CVC, PIN, or magnetic-stripe data — the existing `assert_no_card_data` guard is extended
   to logs and error payloads.
5. **Every webhook is untrusted until proven**: signature/authenticity, replay protection,
   idempotency, amount/currency validation, and binding to an invoice/payment intent. A
   provider success page or a customer screenshot is never settlement evidence.
6. **Refunds remain human-controlled workflows** on every rail.
7. **Legacy rows are migrated by an explicit mapping, not a guess.** Historical `HOSTED_PAGE`
   rows do not reveal which rail was used; the vocabulary is widened and the canonical
   category for old rows is backfilled from a documented mapping the owner reviews. Until then
   old values stay readable and no new row may use them.

## Alternatives considered

- **One provider's model as the internal model** — rejected: it converts a vendor choice into a
  business-model change, and §38 says the vendor choice is still open.
- **A generic `other` rail with free-text method** — rejected: it makes reporting and
  reconciliation unsound.
- **Store card data to avoid provider dependency** — rejected: PCI scope explosion, §59 forbids it.

## Security & operational impact

- New attack surface: webhooks. Controls above are mandatory, not advisory.
- Adapters must be individually allowlisted per environment; an unconfigured rail refuses
  rather than falling back.
- Operational cost: per-provider reconciliation logic remains, but it is isolated.

## Migration / reversibility

Additive: vocabulary widening + new nullable columns + a `method_category` backfill. Reversible
by dropping the new columns and restoring the old CHECK, provided no new-rail row was written.

## Tests required (§61)

- Duplicate card webhook and duplicate PayPal webhook → one financial entry.
- Unallocated bank receipt → `UNALLOCATED_RECEIPT`, human confirms the match.
- Partial and overpayment; overpayment is not automatic revenue.
- Browser/provider success without verified settlement → no `FUNDS_RECEIVED`.
- Rail unavailable (provider outage) → payment pending/failed, **core booking business
  transaction preserved** (SOURCE_UNAVAILABLE ≠ PAYMENT_FAILED).
- Card field written through any path → refused, nothing stored, logs clean.

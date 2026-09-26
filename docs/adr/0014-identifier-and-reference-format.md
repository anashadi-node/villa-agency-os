# ADR-0014 — Identifier and human-readable reference format

- Status: **PROPOSED — pending owner acceptance**
- Date: 2026-09-26
- Spec: Master Spec v1.1 §39, §23, Appendix B

## Context

v1.1 §39 requires "UUID/ULID internal IDs + separate human-readable references", and Appendix B
fixes the reference patterns:

```
INQ-YYYY-NNNNN   QT-YYYY-NNNNN   RSV-YYYY-NNNNN   INV-YYYY-NNNNN
PR-YYYY-NNNNN    INC-YYYY-NNNNN  APR-YYYY-NNNNN
```

Measured state: identifiers are opaque prefixed tokens (`task_ab12cd34`, `hold_ef56…`) generated
by `IdFactory` (`prefix_<hex>`); only `INV-` exists as a human reference (`invoice_number`,
derived per agency per month). `INQ-`, `QT-`, `RSV-`, `PR-`, `INC-`, `APR-` do not exist.
`version` columns exist only on `contract_versions`, `rate_versions`, `policy_versions`,
`rate_cards`, `documents` (plus `bookings.version_lock`).

## Decision

1. **Keep opaque prefixed tokens as the internal primary key** — this is a *recorded deviation*
   from the literal "UUID/ULID" wording, and the reason is concrete: the prefixed token is
   already globally unique, type-identifying (a `hold_…` can never be confused with a
   `book_…`), URL-safe under the existing `_REF_TOKEN` rule, and re-typed by every current FK.
   Swapping to ULID would rewrite every key in a system with no production data but 5,900+
   passing tests, for zero behavioural gain. If the owner prefers literal ULID compliance, this
   decision is cheap to reverse **now** and expensive later — hence it is recorded before WAVE 2
   writes new tables.
2. **References are added on top**, not as identity: a `reference` column per entity that needs
   one, generated per agency per year per Appendix B, unique per agency, **never** an
   authorization boundary (v1.1 Appendix B) and never the value a client is trusted on alone.
3. **Reference generation is one module**, deterministic, with a per-agency sequence stored so
   two agencies never contend and a year rollover is explicit.
4. **Optimistic concurrency**: mutable business records gain a `version` column; a stale write
   returns `409 CONCURRENT_MODIFICATION` (§23, §24) instead of overwriting.
5. **API money is a decimal string + ISO-4217 currency** (§23) while storage stays integer minor
   units (§13 fixed precision) — the two are compatible and this pairing is the decision.

## Alternatives considered

- **Adopt ULID everywhere now** — deferred, not rejected: it is a mechanical rename with a real
  test-fix cost and no functional benefit today. The owner can override cheaply at this point.
- **Use the human reference as the primary key** — rejected: references change format over time
  and are guessable; §Appendix B forbids treating them as authority.
- **Float money in the API** — rejected: §13/§23 require fixed precision.

## Security & operational impact

- References are user-visible; they must not leak volume or sequence across agencies (per-agency
  sequences, not global).
- Concurrency version checks reduce silent-overwrite risk on reservations and invoices.

## Migration / reversibility

Additive columns + one generator module. Reversible while no production data depends on the
references.

## Tests required

- Reference format per entity (regex per Appendix B) and per-agency/per-year uniqueness.
- Two agencies generating the same period get independent sequences.
- Stale write → 409 and the stored row unchanged.
- Money round-trip: decimal string → integer minor units → decimal string, lossless, multi-currency.

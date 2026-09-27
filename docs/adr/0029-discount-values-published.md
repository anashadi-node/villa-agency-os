# ADR-0029 — The three discount values are published

- Status: Accepted
- Date: 2026-09-27
- Deciders: owner
- Amends: ADR-0026 (the engine — it pinned both guards and left them unpublished), ADR-0017 and
  ADR-0018 (both counted the unpublished catalogue keys)

## Context

ADR-0026 built the discount engine and pinned both halves of the §11 margin guard, but left the
three catalogue keys it reads unpublished, so the engine refused every request rather than assume
a value — deny-first by construction. A refusal names the key it is waiting for, so the platform
was never running on a guess; it was simply closed.

The owner has now decided all three. The values are runtime state and live in the pilot database
(`data/villa.db`), not in source, which is why this ADR records them: the database is not in git,
and a decision that only exists there is a decision that can be lost.

## Decision

| Key | Value | What it means |
|---|---|---|
| `commercial.discount.authority_roles` | `["owner","manager"]` | who may give a discount without asking the owner |
| `commercial.margin_floor_pct` | `20` | after a discount the margin must be ≥ 20% of the selling price |
| `commercial.margin_floor_minor` | `2500` | after a discount the margin must be ≥ **USD 25,00**, whatever the percentage says |

Each carries `approved_by = owner_anas`, the owner's decision as its source document, and an
effective window from 2026-09-27. The percentage is written as a decimal text value and the
absolute floor as an integer count of minor units — USD 25 is `2500`, not `25`.

## Consequences

- The engine decides instead of refusing. Measured on a copy of the pilot database:
  - a USD 100 discount on a USD 1.500 sale, leaving USD 350 of margin (23,33%) → `APPLIED`;
  - a USD 200 discount, leaving 16,67% → `REQUIRES_OVERRIDE` / `MARGIN_FLOOR_BREACHED`;
  - a USD 26 discount on a small booking, leaving exactly 20,00% **but only USD 24** → refused:
    the absolute floor doing the work the percentage cannot;
  - exactly USD 25 behind (20,00%) → `APPLIED`. The boundary is inclusive.
- `manager` can discount; `supervisor` — the retired name (ADR-0028) — is refused
  `DISCOUNT_AUTHORITY_REFUSED`, so the rename is enforced by the engine and not only by the tests.
- **Two things were still open when this ADR was written, and both have since moved:**
  1. The absolute floor was compared as a raw count in the booking's own currency, so one number was
     only correct while bookings shared a currency. Bali (USD) was fine. A Yogyakarta booking in IDR
     compared IDR minor units against a USD number, which is meaningless. A per-currency floor, or a
     conversion step with a published rate, had to be built before IDR bookings could discount. The
     owner asked for exactly this — **built in ADR-0030**, which adds
     `commercial.margin_floor_currency`, `commercial.fx.rates` and `src/fx.py`.
  2. Six catalogue keys remained unpublished, so the finance, availability and incident gates that
     read them still refuse rather than run on an assumed value. **ADR-0030 adds a seventh**
     (`commercial.fx.rates`), because converting the floor needs a published rate. The count is
     runtime state: `make policy-status` reports it, and it is authoritative over this sentence.

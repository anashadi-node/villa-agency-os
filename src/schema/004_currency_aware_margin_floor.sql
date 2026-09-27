-- Migration 004: the currency of the §11 absolute margin floor, and the rates to convert it.
--
-- 001_initial.sql seeds the catalogue when the database file is created, so these two rows are
-- also added there. A database created before they existed cannot gain them by being reopened:
-- schema_loader.apply_schema refuses to re-apply the initial schema ("write a migration instead
-- of re-applying the initial schema"). Both statements are idempotent — re-running changes nothing.
--
-- Why they are needed. `commercial.margin_floor_minor` is one number, and one number carries no
-- currency. Without `commercial.margin_floor_currency` the discount engine cannot tell whether
-- 2500 means USD 25.00 or IDR 2,500, so it refuses every request (POLICY_UNKNOWN_KEY via the
-- resolver) rather than comparing a rupiah margin against a dollar number — a comparison that
-- would *pass* an absolute floor the booking never actually cleared. `commercial.fx.rates` is
-- what lets a booking in another currency be measured against the same floor; with no rate
-- published for the pair, the engine refuses instead of assuming parity.
--
-- Only catalogue rows are inserted here. The values are owner decisions and are published
-- through scripts/publish_policy_value.py, never seeded by a migration.

INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at)
SELECT 'commercial.margin_floor_currency', 'COMMERCIAL', 'TEXT', 'MEDIUM', 'owner', '§11', 'ISO-4217 currency commercial.margin_floor_minor is denominated in. A booking in another currency has the floor converted through commercial.fx.rates before the §11 guard is applied.', '2026-09-27T00:00:00+00:00'
WHERE NOT EXISTS (SELECT 1 FROM policy_defs WHERE policy_key = 'commercial.margin_floor_currency');

INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at)
SELECT 'commercial.fx.rates', 'COMMERCIAL', 'JSON', 'MEDIUM', 'owner', '§11', 'Published FX rates as {"BASE/QUOTE": "rate"}: one unit of BASE is worth this many QUOTE. Used to express the absolute margin floor in the currency of the booking. An unpublished pair refuses — the engine never assumes parity.', '2026-09-27T00:00:00+00:00'
WHERE NOT EXISTS (SELECT 1 FROM policy_defs WHERE policy_key = 'commercial.fx.rates');

-- Migration 005: how old a published FX rate may be before a conversion refuses.
--
-- 001_initial.sql seeds the catalogue when the database file is created, so this row is also
-- added there. A database created before it existed cannot gain it by being reopened:
-- schema_loader.apply_schema refuses to re-apply the initial schema ("write a migration instead
-- of re-applying the initial schema"). The statement is idempotent — re-running changes nothing.
--
-- Why it is needed. `commercial.fx.rates` is a day's price of one currency in another, and
-- src/fx.py uses it to express the §11 absolute floor in the currency of the booking. A rate
-- published months ago is still a *number*, so nothing in the comparison would look wrong: the
-- floor would simply be measured at a rate no market has quoted since, and a booking would pass
-- a guard the owner meant to be current. This key is the owner's limit on that age. An
-- unpublished limit refuses every conversion (POLICY_UNKNOWN_KEY via the resolver) — the
-- intended fail-closed state, not a bug.
--
-- The unit is hours because the store has no DAYS type and the owner states the limit that way
-- (2 days = 48). A revision is dated by a calendar day, so src/fx.py applies the limit in whole
-- days and the tighter day wins.
--
-- Only the catalogue row is inserted here. The value is an owner decision and is published
-- through scripts/publish_policy_value.py, never seeded by a migration.

INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at)
SELECT 'commercial.fx.max_age_hours', 'COMMERCIAL', 'HOURS', 'MEDIUM', 'owner', '§11', 'How old the effective revision of commercial.fx.rates may be before a conversion refuses. The rate is a day''s price, so the limit is applied in whole days and the tighter day wins (48 h refuses from the third day). An unpublished limit refuses every conversion.', '2026-09-28T00:00:00+00:00'
WHERE NOT EXISTS (SELECT 1 FROM policy_defs WHERE policy_key = 'commercial.fx.max_age_hours');

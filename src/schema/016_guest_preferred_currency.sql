-- Migration 016: guests.preferred_currency, a display default inferred at intake (ADR-0060).
--
-- 001_initial.sql creates the column for a new database; this file exists for databases that
-- already exist, because schema_loader.apply_schema refuses to re-apply the initial schema.
--
-- Why this column and what it is not: the marketing site shows no price at all today (a human
-- confirms and sends every quote); this column only remembers which of the agency's two
-- supported display currencies (IDR, USD) a guest's apparent country suggested at intake, so
-- the dashboard can pre-select the right one for staff. It never changes what a quote is
-- actually priced in (`quote_calculator`'s `policy.currency`) and never moves money — ADR-0036
-- still requires a human hand and GM approval for that. DEFAULT 'USD' keeps every existing
-- row valid without guessing a country for guests who were already on file.

ALTER TABLE guests ADD COLUMN preferred_currency TEXT NOT NULL DEFAULT 'USD'
    CHECK (preferred_currency IN ('IDR','USD'));

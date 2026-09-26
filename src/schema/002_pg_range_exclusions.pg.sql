-- Villa Agency OS — PostgreSQL-only hardening (002).
--
-- NOT part of the SQLite/PostgreSQL twin pair. The twin (001_initial.sql /
-- 001_initial.pg.sql) must stay dialect-for-dialect comparable, and SQLite has no
-- equivalent of a range exclusion constraint, so the strongest possible
-- production guarantee is expressed here, in PostgreSQL only.
--
-- WHY THIS EXISTS
-- The unique indexes in 001 catch "same unit, same arrival date". They do NOT
-- catch a partial overlap (hold 1-3 Oct vs hold 2-4 Oct on one unit), because
-- uniqueness cannot express a range test. The kernel rejects partial overlaps
-- inside its write transaction, but a direct write (migration script, psql
-- session, bug) could still create one. An EXCLUDE constraint makes that
-- impossible at the storage level: overlapping rows are rejected by the engine,
-- even under concurrency.
--
-- WHY AN IMMUTABLE HELPER IS REQUIRED
-- The schema stores dates as ISO-8601 TEXT (a deliberate cross-dialect choice).
-- PostgreSQL refuses index expressions that are not IMMUTABLE, and ``text::date``
-- is STABLE (it depends on DateStyle for ambiguous inputs). The pattern CHECKs
-- below pin the input to unambiguous ISO-8601, which is exactly the precondition
-- that makes the IMMUTABLE promise in ``villa_iso_date`` sound. Keep the two
-- statements together.
--
-- Apply after 001:
--     psql "$VILLA_PG_DSN" -v ON_ERROR_STOP=1 -f src/schema/001_initial.pg.sql
--     psql "$VILLA_PG_DSN" -v ON_ERROR_STOP=1 -f src/schema/002_pg_range_exclusions.pg.sql

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ISO-8601 dates only: the precondition for the immutable cast below.
ALTER TABLE holds
    ADD CONSTRAINT ck_holds_check_in_iso CHECK (check_in ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE holds
    ADD CONSTRAINT ck_holds_check_out_iso CHECK (check_out ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE bookings
    ADD CONSTRAINT ck_bookings_check_in_iso CHECK (check_in ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE bookings
    ADD CONSTRAINT ck_bookings_check_out_iso CHECK (check_out ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');

-- Unambiguous by construction: only called on values the CHECKs above accepted.
CREATE OR REPLACE FUNCTION villa_iso_date(value text)
RETURNS date
LANGUAGE sql
IMMUTABLE
STRICT
AS $$ SELECT value::date $$;

-- One ACTIVE hold per unit per range: [check_in, check_out) half-open, so a
-- back-to-back stay (3-5 Oct after 1-3 Oct) remains legal.
ALTER TABLE holds
    ADD CONSTRAINT ex_holds_no_overlap
    EXCLUDE USING gist (
        unit_id WITH =,
        daterange(villa_iso_date(check_in), villa_iso_date(check_out), '[)') WITH &&
    )
    WHERE (status IN ('REQUESTED','AWAITING_SUPPLIER','CONFIRMED','EXTENSION_REQUESTED','RELEASE_REQUESTED','EXPIRED_PENDING_VERIFICATION'));

-- One occupying booking per unit per range. The status list must stay identical
-- to ``OCCUPYING_BOOKING_STATES`` in src/reservation_kernel.py and to the partial
-- unique index in 001; tests/test_schema.py asserts all three agree.
-- ADR-0002 (Opsi A): CANCELLATION_REQUESTED, CANCELLATION_REVIEW and DISPUTED are
-- occupying too, so PostgreSQL refuses a re-sale on those dates as well.
ALTER TABLE bookings
    ADD CONSTRAINT ex_bookings_no_overlap
    EXCLUDE USING gist (
        unit_id WITH =,
        daterange(villa_iso_date(check_in), villa_iso_date(check_out), '[)') WITH &&
    )
    WHERE (status IN ('CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY','CANCELLATION_REQUESTED','CANCELLATION_REVIEW','DISPUTED'));

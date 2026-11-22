-- Villa Agency OS — PostgreSQL-only hardening (021).
--
-- CROSS-TABLE OCCUPANCY: a HOLD and a BOOKING cannot both occupy the same unit's dates.
--
-- WHY THIS EXISTS
-- 002 gives each table its own EXCLUDE constraint. Neither constraint can see the other
-- table, so this state was accepted by the database and committed (reproduced 2026-11-22
-- on PostgreSQL 16.14):
--
--     holds    unit_X  CONFIRMED      2026-10-01 .. 2026-10-04
--     bookings unit_X  CONFIRMED      2026-10-02 .. 2026-10-05
--
-- Two independent reservations, one unit, overlapping dates, no error. The kernel refuses
-- this in its write transaction, but a direct write (psql, migration, bug, another service)
-- is not the kernel. An EXCLUDE constraint cannot express it, because the two windows live
-- in different tables.
--
-- WHY TRIGGERS PLUS AN ADVISORY LOCK, AND NOT ONE OR THE OTHER
-- A trigger alone is not enough across connections: under READ COMMITTED two transactions
-- each running the check before either commits both see nothing and both commit. A
-- transaction-scoped advisory lock is taken before the check and released by PostgreSQL at
-- commit or rollback, so the second transaction's check runs after the first one's rows are
-- visible. A deferred constraint trigger then re-checks at COMMIT while the same locks are
-- still held: that is the moment the invariant must hold, and it is also the only place a
-- multi-row statement can be judged as a whole.
--
-- WHY ONE LOCK PER OCCUPIED NIGHT AND NOT ONE PER UNIT
-- The first cut keyed the lock on (agency_id, unit_id). That was both too wide and too
-- narrow: too wide because it queued a back-to-back booking behind an unrelated one on the
-- same unit (the database refused the second writer's statement, and CI caught it as
-- test_back_to_back_stays_do_not_overlap_and_do_not_wait), too narrow because a second
-- agency_id bought its own lock and could claim the same physical unit. The key is now
-- (unit_id, occupied night): two windows that overlap share at least one night and
-- therefore serialise, while back-to-back windows [a,b) and [b,c) share none and proceed.
-- Nights are taken in ascending order, so two writers that each write ONE row and share
-- several nights always ask for them in the same order and cannot deadlock. That guarantee
-- does not extend to a statement that writes several rows: the trigger fires per row, so the
-- rows are visited in the executor's order and one statement can ask for a later night before
-- an earlier one, inverting against another statement. Multi-row writers (bulk UPDATE,
-- INSERT ... SELECT, and sweep_expired_holds in src/reservation_kernel.py) remain a known
-- deadlock risk — RISK-M1, still OPEN, and not closed by this migration.
--
-- WHAT COUNTS AS OCCUPYING
-- The lists below restate OCCUPYING_HOLD_STATES (src/hold_states.py) and
-- OCCUPYING_BOOKING_STATES (src/reservation_kernel.py), and are identical to the WHERE
-- clauses of the two EXCLUDE constraints in 002. tests/test_schema.py already pins those
-- three restatements to each other; tests/test_pg_cross_table_occupancy.py pins these.
--
-- THE ONE LEGITIMATE OVERLAP
-- ReservationKernel.confirm_booking() sets the booking to CONFIRMED and only then converts
-- the hold to CONVERTED, in one transaction. Mid-transaction the same reservation occupies
-- its slot twice. That is allowed, and only that: the hold must be the booking's own
-- (hold_ref = hold_id, same agency, same unit, same dates), it must be in the transition
-- state CONFIRMED, and no other occupying booking may claim it. Every other overlap is a
-- conflict between independent reservations. At COMMIT the linked hold must no longer be
-- occupying — otherwise the transaction wrote an invalid final state and is refused.
--
-- Apply after 001 and 002:
--     psql "$VILLA_PG_DSN" -v ON_ERROR_STOP=1 -f src/schema/021_pg_cross_table_occupancy.pg.sql

-- Occupying statuses, restated once so the trigger functions cannot drift from each other.
CREATE OR REPLACE FUNCTION villa_occupying_states(side text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = public, pg_catalog
AS $$
    SELECT CASE side
        WHEN 'hold' THEN ARRAY[
            'REQUESTED', 'AWAITING_SUPPLIER', 'CONFIRMED', 'EXTENSION_REQUESTED',
            'RELEASE_REQUESTED', 'EXPIRED_PENDING_VERIFICATION'
        ]::text[]
        WHEN 'booking' THEN ARRAY[
            'CONFIRMED', 'BALANCE_DUE', 'BALANCE_OVERDUE', 'PRE_ARRIVAL', 'IN_STAY',
            'CANCELLATION_REQUESTED', 'CANCELLATION_REVIEW', 'DISPUTED'
        ]::text[]
        ELSE ARRAY[]::text[]
    END
$$;

-- The cross-table check itself. Returns a description of the conflicting row, or NULL.
-- ``p_side`` is the table the candidate row lives in ('hold' or 'booking').
CREATE OR REPLACE FUNCTION villa_cross_table_conflict(
    p_unit_id text,
    p_check_in text,
    p_check_out text,
    p_side text,
    p_self_id text,
    p_hold_ref text
)
RETURNS text
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_window daterange := daterange(
        villa_iso_date(p_check_in), villa_iso_date(p_check_out), '[)'
    );
    v_found text;
BEGIN
    IF p_side = 'hold' THEN
        -- A booking that names this very hold is the reservation this hold belongs to; its
        -- overlap is the conversion, not a conflict. Dates and unit must match as well, so
        -- a stale hold_ref cannot launder a different stay.
        SELECT b.booking_id INTO v_found
        FROM public.bookings b
        WHERE b.unit_id = p_unit_id
          AND b.status = ANY (villa_occupying_states('booking'))
          AND daterange(villa_iso_date(b.check_in), villa_iso_date(b.check_out), '[)') && v_window
          AND NOT (
              b.hold_ref IS NOT DISTINCT FROM p_self_id
              AND p_self_id IS NOT NULL
              AND b.check_in = p_check_in
              AND b.check_out = p_check_out
          )
        LIMIT 1;
        RETURN CASE WHEN v_found IS NULL THEN NULL ELSE 'booking:' || v_found END;
    END IF;

    IF p_side = 'booking' THEN
        SELECT h.hold_id INTO v_found
        FROM public.holds h
        WHERE h.unit_id = p_unit_id
          AND h.status = ANY (villa_occupying_states('hold'))
          AND daterange(villa_iso_date(h.check_in), villa_iso_date(h.check_out), '[)') && v_window
          AND NOT (
              p_hold_ref IS NOT NULL
              AND h.hold_id = p_hold_ref
              AND h.status = 'CONFIRMED'
              AND h.check_in = p_check_in
              AND h.check_out = p_check_out
              AND NOT EXISTS (
                  SELECT 1 FROM public.bookings b2
                  WHERE b2.hold_ref = h.hold_id
                    AND b2.agency_id = h.agency_id
                    AND b2.booking_id IS DISTINCT FROM p_self_id
                    AND b2.status = ANY (villa_occupying_states('booking'))
              )
          )
        LIMIT 1;
        RETURN CASE WHEN v_found IS NULL THEN NULL ELSE 'hold:' || v_found END;
    END IF;

    RAISE EXCEPTION 'unknown occupancy side: %', p_side USING ERRCODE = '22023';
END
$$;

-- Refuse to install onto a database that is already double-booked. A guard that only protects
-- future writes certifies a lie about the rows it was installed over: the contradictory pair
-- commits once, forever, and every later check passes. Fail here, name the pair, let a human
-- decide how to unwind it.
DO $$
DECLARE
    v_pair text;
BEGIN
    SELECT 'hold ' || h.hold_id || ' (' || h.check_in || '..' || h.check_out || ') overlaps'
           || ' booking ' || b.booking_id || ' (' || b.check_in || '..' || b.check_out || ')'
      INTO v_pair
      FROM public.holds h
      JOIN public.bookings b
        ON b.unit_id = h.unit_id
       AND daterange(villa_iso_date(b.check_in), villa_iso_date(b.check_out), '[)')
        && daterange(villa_iso_date(h.check_in), villa_iso_date(h.check_out), '[)')
     WHERE h.status = ANY (villa_occupying_states('hold'))
       AND b.status = ANY (villa_occupying_states('booking'))
     LIMIT 1;
    IF v_pair IS NOT NULL THEN
        RAISE EXCEPTION 'refusing to install the cross-table guard: existing overlap %', v_pair
            USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                  HINT = 'resolve the contradictory occupancy first, then re-run the migration';
    END IF;
END
$$;

-- Lock every night the row occupies, ascending. Half-open [check_in, check_out): the night
-- of check_out belongs to the next guest, which is what makes back-to-back legal. Empty or
-- inverted ranges produce no lock only because 002 already refuses them before a row exists
-- (ck_holds_stay / ck_bookings_stay); this function never decides validity.
--
-- The key is rendered with to_char(), and the calendar is stepped in timestamp rather than
-- timestamptz. Neither is decoration: a date cast with || would be rendered in the session's
-- DateStyle ('SQL, DMY' makes it 01/06/2027), so two sessions with different DateStyle would
-- mint different keys for one night, never contend, and both commit an overlap. Stepping in
-- timestamptz instead would resolve generate_series to the session TimeZone. Both settings
-- are per session and neither is pinned anywhere in this repository.
CREATE OR REPLACE FUNCTION villa_lock_occupied_nights(
    p_unit_id text,
    p_check_in text,
    p_check_out text
)
RETURNS void
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_night date;
BEGIN
    FOR v_night IN
        SELECT night::date
          FROM generate_series(
                   villa_iso_date(p_check_in)::timestamp,
                   (villa_iso_date(p_check_out) - 1)::timestamp,
                   interval '1 day'
               ) AS night
         ORDER BY night
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'villa-occupancy-night:' || p_unit_id || ':' || to_char(v_night, 'YYYY-MM-DD'),
                0
            )
        );
    END LOOP;
END
$$;

-- Immediate guard: serialise the nights this row occupies, then refuse an independent
-- cross-table overlap.
CREATE OR REPLACE FUNCTION villa_hold_occupancy_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_conflict text;
BEGIN
    IF NEW.status <> ALL (villa_occupying_states('hold')) THEN
        RETURN NEW;  -- not occupying: nothing to protect, no lock to take
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.agency_id = NEW.agency_id
       AND OLD.status = NEW.status
       AND OLD.unit_id = NEW.unit_id
       AND OLD.check_in = NEW.check_in
       AND OLD.check_out = NEW.check_out THEN
        RETURN NEW;  -- an update that cannot change the occupied slot
    END IF;
    -- Both tables call the same helper with the same arguments, so a HOLD and a BOOKING on
    -- one unit/night contend for one lock. Keyed on the unit alone (never the agency): the
    -- unit is the physical resource, and 002's EXCLUDE is keyed the same way.
    PERFORM villa_lock_occupied_nights(NEW.unit_id, NEW.check_in, NEW.check_out);
    v_conflict := villa_cross_table_conflict(
        NEW.unit_id, NEW.check_in, NEW.check_out, 'hold', NEW.hold_id, NULL
    );
    IF v_conflict IS NOT NULL THEN
        RAISE EXCEPTION
            'unit % is already occupied: hold % (%) overlaps %',
            NEW.unit_id, NEW.hold_id, NEW.status, v_conflict
            USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                  TABLE = 'holds';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION villa_booking_occupancy_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_conflict text;
BEGIN
    IF NEW.status <> ALL (villa_occupying_states('booking')) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.agency_id = NEW.agency_id
       AND OLD.status = NEW.status
       AND OLD.unit_id = NEW.unit_id
       AND OLD.check_in = NEW.check_in
       AND OLD.check_out = NEW.check_out THEN
        RETURN NEW;
    END IF;
    PERFORM villa_lock_occupied_nights(NEW.unit_id, NEW.check_in, NEW.check_out);
    v_conflict := villa_cross_table_conflict(
        NEW.unit_id, NEW.check_in, NEW.check_out, 'booking', NEW.booking_id, NEW.hold_ref
    );
    IF v_conflict IS NOT NULL THEN
        RAISE EXCEPTION
            'unit % is already occupied: booking % (%) overlaps %',
            NEW.unit_id, NEW.booking_id, NEW.status, v_conflict
            USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                  TABLE = 'bookings';
    END IF;
    RETURN NEW;
END
$$;

-- Commit-time invariant. A row-at-a-time BEFORE trigger cannot judge a multi-row statement
-- as a whole, and it cannot see a row another transaction is about to commit. This runs at
-- COMMIT, while this transaction still holds every night lock it took.
CREATE OR REPLACE FUNCTION villa_occupancy_commit_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_conflict text;
    v_status text;
    v_unit text;
    v_check_in text;
    v_check_out text;
    v_hold_ref text;
BEGIN
    -- Read the row as it stands NOW, not as it stood when the event was queued. A deferred
    -- trigger carries the NEW of its own event: a hold inserted CONFIRMED and then converted
    -- in the same transaction still arrives here with NEW.status = 'CONFIRMED', and judging
    -- that stale tuple would refuse the very conversion this design exists to allow.
    IF TG_TABLE_NAME = 'bookings' THEN
        SELECT b.status, b.unit_id, b.check_in, b.check_out, b.hold_ref
          INTO v_status, v_unit, v_check_in, v_check_out, v_hold_ref
          FROM public.bookings b WHERE b.booking_id = NEW.booking_id;
    ELSE
        SELECT h.status, h.unit_id, h.check_in, h.check_out, NULL
          INTO v_status, v_unit, v_check_in, v_check_out, v_hold_ref
          FROM public.holds h WHERE h.hold_id = NEW.hold_id;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;  -- inserted and deleted in this transaction: nothing to assert
    END IF;

    IF TG_TABLE_NAME = 'bookings' THEN
        IF v_status <> ALL (villa_occupying_states('booking')) THEN
            RETURN NULL;
        END IF;
        -- The linked hold must already have stopped occupying. Booking CONFIRMED and its
        -- hold still CONFIRMED at commit is the invalid final state: one slot, two owners.
        IF v_hold_ref IS NOT NULL THEN
            SELECT h.status INTO v_status FROM public.holds h WHERE h.hold_id = v_hold_ref;
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'booking % names hold % which does not exist: an occupying booking needs'
                    ' a real hold', NEW.booking_id, v_hold_ref
                    USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                          TABLE = 'bookings';
            END IF;
            IF v_status = ANY (villa_occupying_states('hold')) THEN
                RAISE EXCEPTION
                    'booking % is occupying while its hold % is still %: convert or release'
                    ' the hold in the same transaction',
                    NEW.booking_id, v_hold_ref, v_status
                    USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                          TABLE = 'bookings';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.holds h
                 WHERE h.hold_id = v_hold_ref
                   AND h.unit_id = v_unit
                   AND h.check_in = v_check_in
                   AND h.check_out = v_check_out
            ) THEN
                RAISE EXCEPTION
                    'booking % converts hold % for a different unit or window: a conversion'
                    ' releases exactly the slot the hold owned', NEW.booking_id, v_hold_ref
                    USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                          TABLE = 'bookings';
            END IF;
            -- restore the booking's own status for the generic check below
            SELECT b.status INTO v_status FROM public.bookings b WHERE b.booking_id = NEW.booking_id;
        END IF;
        v_conflict := villa_cross_table_conflict(
            v_unit, v_check_in, v_check_out, 'booking', NEW.booking_id, v_hold_ref
        );
        IF v_conflict IS NOT NULL THEN
            RAISE EXCEPTION
                'committed state would leave unit % double-booked: booking % overlaps %',
                v_unit, NEW.booking_id, v_conflict
                USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                      TABLE = 'bookings';
        END IF;
        RETURN NULL;
    END IF;

    IF v_status <> ALL (villa_occupying_states('hold')) THEN
        RETURN NULL;
    END IF;
    -- p_self_id is NULL on purpose: the linked-booking exemption belongs to the immediate
    -- trigger, where it only forgives the ordering of one conversion. At COMMIT the pair is
    -- either resolved (this row is CONVERTED, returned above) or it is a double occupancy —
    -- including a converted hold re-opened while its booking occupies.
    v_conflict := villa_cross_table_conflict(
        v_unit, v_check_in, v_check_out, 'hold', NULL, NULL
    );
    IF v_conflict IS NOT NULL THEN
        RAISE EXCEPTION
            'committed state would leave unit % double-booked: hold % overlaps %',
            v_unit, NEW.hold_id, v_conflict
            USING ERRCODE = '23P01', CONSTRAINT = 'ex_holds_bookings_no_overlap',
                  TABLE = 'holds';
    END IF;
    RETURN NULL;
END
$$;

-- Refuse to install the guard over data that already violates it. Silently adding a
-- constraint to a database with live contradictions hides the very problem this migration
-- exists to surface; PCC-T24 requires the refusal.
DO $$
DECLARE
    v_conflicts integer;
BEGIN
    SELECT count(*) INTO v_conflicts
    FROM public.holds h
    JOIN public.bookings b
      ON b.unit_id = h.unit_id
     AND daterange(villa_iso_date(b.check_in), villa_iso_date(b.check_out), '[)')
      && daterange(villa_iso_date(h.check_in), villa_iso_date(h.check_out), '[)')
    WHERE h.status = ANY (villa_occupying_states('hold'))
      AND b.status = ANY (villa_occupying_states('booking'));
    IF v_conflicts > 0 THEN
        RAISE EXCEPTION
            'refusing to install the cross-table guard: % occupying hold/booking pair(s)'
            ' already overlap. Resolve them with evidence first (they are double bookings).',
            v_conflicts
            USING ERRCODE = '23514', CONSTRAINT = 'ex_holds_bookings_no_overlap';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_holds_cross_table_occupancy ON public.holds;
CREATE TRIGGER trg_holds_cross_table_occupancy
    BEFORE INSERT OR UPDATE ON public.holds
    FOR EACH ROW EXECUTE FUNCTION villa_hold_occupancy_guard();

DROP TRIGGER IF EXISTS trg_bookings_cross_table_occupancy ON public.bookings;
CREATE TRIGGER trg_bookings_cross_table_occupancy
    BEFORE INSERT OR UPDATE ON public.bookings
    FOR EACH ROW EXECUTE FUNCTION villa_booking_occupancy_guard();

DROP TRIGGER IF EXISTS trg_holds_occupancy_at_commit ON public.holds;
CREATE CONSTRAINT TRIGGER trg_holds_occupancy_at_commit
    AFTER INSERT OR UPDATE ON public.holds
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION villa_occupancy_commit_guard();

DROP TRIGGER IF EXISTS trg_bookings_occupancy_at_commit ON public.bookings;
CREATE CONSTRAINT TRIGGER trg_bookings_occupancy_at_commit
    AFTER INSERT OR UPDATE ON public.bookings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION villa_occupancy_commit_guard();

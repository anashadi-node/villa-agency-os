"""PostgreSQL cross-table occupancy: HOLD and BOOKING cannot own one slot twice.

PACK VILLA-PG-CONCURRENCY-001 / ADR-0075.

What the database used to accept (reproduced before this file existed, PostgreSQL 16.14):
an occupying hold on 2026-10-01..2026-10-04 and an occupying booking on 2026-10-02..2026-10-05
on the same unit, both committed, no error. 002's two EXCLUDE constraints each see only their
own table, so neither can express a rule about the other. 021 adds a trigger pair plus a
transaction-scoped advisory lock, and a deferred constraint trigger that re-checks at COMMIT.

These tests run against real PostgreSQL with separate connections: concurrency claims come
from two connections racing through a barrier, not from sequential calls pretending to race.
Every test reads committed rows afterwards — an exception proves a statement was refused, not
that the database is in a legal state.
"""

from __future__ import annotations

import pathlib
import secrets
import sys
import threading
import unittest
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_pg_migrations import apply_twin, connect, disposable  # noqa: E402
from test_pg_overlap_concurrency import (  # noqa: E402
    FINGERPRINT,
    STATE_TS,
    _driver,
    make_insert,
    new_unit,
    seed_world,
)

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "src" / "schema"
PG_002 = SCHEMA / "002_pg_range_exclusions.pg.sql"
PG_021 = SCHEMA / "021_pg_cross_table_occupancy.pg.sql"

CONFLICT_SQLSTATE = "23P01"

# A booking that names the hold it is converting. The shared make_insert() cannot express
# hold_ref, and rewriting its SQL text in place was the kind of cleverness that hides a typo.
BOOKING_WITH_HOLD_SQL = (
    "INSERT INTO bookings (booking_id, hold_ref, property_id, unit_id, check_in, check_out,"
    " nights, status, guest_ref, adults, children, infants, currency, policy_version_id,"
    " policy_fingerprint, total_minor, payment_status, idempotency_key, created_at, updated_at)"
    " VALUES (%(id)s, %(hold_ref)s, 'prop_cc', %(unit)s, %(ci)s, %(co)s, 2, %(status)s,"
    " 'guest_cc', 2, 0, 0, 'IDR', 'policy_cc', %(fp)s, 1000000, 'UNPAID', %(idem)s, %(ts)s, %(ts)s)"
)


def booking_with_hold(
    unit: str, check_in: str, check_out: str, hold_id: str, *, status: str = "CONFIRMED"
) -> tuple[str, dict[str, Any], str]:
    row_id = "bk_" + secrets.token_hex(5)
    return (
        BOOKING_WITH_HOLD_SQL,
        {
            "id": row_id,
            "hold_ref": hold_id,
            "unit": unit,
            "ci": check_in,
            "co": check_out,
            "status": status,
            "fp": FINGERPRINT,
            "idem": "idem_" + row_id,
            "ts": STATE_TS,
        },
        row_id,
    )


def install_cross_table_guard(dsn: str) -> None:
    """Apply 002 then 021. The loader installs 001 only, so both PG-only files are explicit."""
    psycopg = _driver()
    from psycopg import sql  # type: ignore

    import schema_loader as SL

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        for path in (PG_002, PG_021):
            for statement in SL.split_statements(path.read_text(encoding="utf-8")):
                body = SL._strip_leading_comments(statement)
                if body:
                    conn.execute(sql.SQL(body))


def only_cross_table_guard(dsn: str) -> None:
    """Apply 021 alone: for the database that must be refused before 002 exists."""
    psycopg = _driver()
    from psycopg import sql  # type: ignore

    import schema_loader as SL

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        for statement in SL.split_statements(PG_021.read_text(encoding="utf-8")):
            body = SL._strip_leading_comments(statement)
            if body:
                conn.execute(sql.SQL(body))


def guarded_world(dsn: str) -> None:
    apply_twin(dsn)
    install_cross_table_guard(dsn)
    seed_world(dsn)


class GuardCase(unittest.TestCase):
    """One disposable database per test; the guard is installed before any row is written."""

    def setUp(self) -> None:
        self._ctx = disposable()
        self.dsn = self._ctx.__enter__()
        guarded_world(self.dsn)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    # -- helpers -------------------------------------------------------------------
    def write(self, statement: tuple[str, dict, str], *, expect: str = "commit") -> str:
        """Run one insert on its own connection. Returns 'commit' or the sqlstate."""
        psycopg = _driver()
        sql_text, params, _ = statement
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(sql_text, params)
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", "") or type(error).__name__)
                self.last_error = str(error)
        if expect == "commit":
            self.assertEqual(outcome, "commit", f"expected commit, got {outcome}: {self.last_error if outcome != 'commit' else ''}")
        else:
            self.assertEqual(outcome, CONFLICT_SQLSTATE, f"expected 23P01, got {outcome}")
        return outcome

    def rows(self, unit: str) -> list[tuple[str, str, str, str, str]]:
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            return [
                tuple(row)
                for row in conn.execute(
                    "SELECT 'hold', hold_id, status, check_in, check_out FROM holds"
                    " WHERE unit_id = %s UNION ALL"
                    " SELECT 'booking', booking_id, status, check_in, check_out FROM bookings"
                    " WHERE unit_id = %s ORDER BY 1, 4",
                    (unit, unit),
                ).fetchall()
            ]

    def occupying_count(self, unit: str) -> int:
        return len([row for row in self.rows(unit) if row[2] not in ("CANCELLED", "CONVERTED")])


# ============================================================ PCC-T01 . T02 . T03 . T05
class TestIndependentReservations(GuardCase):
    def test_T01_overlapping_hold_and_booking_cannot_both_commit(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2026-10-01", "2026-10-04"))
        self.write(make_insert("booking", unit, "2026-10-02", "2026-10-05"), expect="refuse")
        committed = self.rows(unit)
        self.assertEqual(len(committed), 1, f"two occupying rows committed: {committed}")
        self.assertEqual(committed[0][0], "hold")

    def test_T02_hold_then_booking_is_refused_with_exclusion_sqlstate(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2027-05-01", "2027-05-04"))
        self.write(make_insert("booking", unit, "2027-05-03", "2027-05-06"), expect="refuse")
        self.assertIn("occupied", self.last_error)

    def test_T03_booking_then_hold_is_refused_the_same_way(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("booking", unit, "2027-06-01", "2027-06-04"))
        self.write(make_insert("hold", unit, "2027-06-04", "2027-06-07"), expect="commit")
        # back-to-back is legal; now the overlapping direction
        self.write(make_insert("hold", unit, "2027-06-02", "2027-06-05"), expect="refuse")

    def test_T05_reverse_transaction_ordering_keeps_the_invariant(self) -> None:
        """The booking is written first inside an open transaction; the hold cannot slip in."""
        unit = new_unit(self.dsn)
        psycopg = _driver()
        booking = make_insert("booking", unit, "2027-07-01", "2027-07-04")
        hold = make_insert("hold", unit, "2027-07-02", "2027-07-05")
        with psycopg.connect(self.dsn, connect_timeout=10) as first:
            first.execute(booking[0], booking[1])  # not committed yet
            with psycopg.connect(self.dsn, connect_timeout=10) as second:
                # The hold must wait for the booking's transaction, not read past it. A bounded
                # lock_timeout keeps a real deadlock from turning into a hung test.
                second.execute("SET lock_timeout = '3s'")
                second_error = "committed"
                try:
                    second.execute(hold[0], hold[1])
                    second.commit()
                    second_committed = True
                except psycopg.Error as error:
                    second.rollback()
                    second_error = str(error)
                    second_committed = False
            first.commit()
        self.assertFalse(second_committed, second_error)
        self.assertFalse(second_committed, "a hold committed while a booking held the same slot")
        self.assertEqual(self.occupying_count(unit), 1, self.rows(unit))


# ============================================================ PCC-T06 . T07 . T08 . T09
class TestLegitimateConversion(GuardCase):
    def test_T06_T07_confirmation_converts_the_hold_and_keeps_one_owner(self) -> None:
        unit = new_unit(self.dsn)
        psycopg = _driver()
        hold = make_insert("hold", unit, "2026-11-01", "2026-11-04", status="CONFIRMED")
        booking = booking_with_hold(unit, "2026-11-01", "2026-11-04", hold[2])
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
            conn.execute(*booking[:2])
            conn.execute(
                "UPDATE holds SET status = 'CONVERTED', closed_at = %(ts)s,"
                " close_reason = 'converted' WHERE hold_id = %(id)s",
                {"ts": "2026-09-22", "id": hold[2]},
            )
            conn.commit()
        rows = {row[1]: row[2] for row in self.rows(unit)}
        self.assertEqual(rows[hold[2]], "CONVERTED")
        self.assertEqual(rows[booking[2]], "CONFIRMED")

    def test_T08_both_linked_rows_occupying_at_commit_is_refused(self) -> None:
        unit = new_unit(self.dsn)
        psycopg = _driver()
        hold = make_insert("hold", unit, "2027-01-01", "2027-01-04")
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(*booking_with_hold(unit, "2027-01-01", "2027-01-04", hold[2])[:2])
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", ""))
                self.assertIn("still CONFIRMED", str(error))
        self.assertEqual(outcome, CONFLICT_SQLSTATE)
        self.assertEqual([row[0] for row in self.rows(unit)], ["hold"])

    def test_T09_a_foreign_hold_reference_buys_no_exemption(self) -> None:
        """A converted hold from another reservation is not a licence to overlap."""
        foreign_unit = new_unit(self.dsn)
        unit = new_unit(self.dsn)
        psycopg = _driver()
        foreign = make_insert("hold", foreign_unit, "2027-02-01", "2027-02-04", status="CONVERTED")
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(foreign[0], foreign[1])
            conn.commit()
        self.write(make_insert("hold", unit, "2027-02-01", "2027-02-04"))
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(*booking_with_hold(unit, "2027-02-01", "2027-02-04", foreign[2])[:2])
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", ""))
        self.assertEqual(outcome, CONFLICT_SQLSTATE)
        self.assertEqual([row[0] for row in self.rows(unit)], ["hold"])


# ============================================================ PCC-T10 . T11 . T12 . T13
class TestWindowBoundaries(GuardCase):
    def test_T10_other_units_may_overlap(self) -> None:
        first, second = new_unit(self.dsn), new_unit(self.dsn)
        self.write(make_insert("hold", first, "2026-10-01", "2026-10-04"))
        self.write(make_insert("booking", second, "2026-10-01", "2026-10-04"))
        self.assertEqual(len(self.rows(first)) + len(self.rows(second)), 2)

    def test_T11_back_to_back_windows_are_valid(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2026-10-01", "2026-10-03"))
        self.write(make_insert("booking", unit, "2026-10-03", "2026-10-05"))
        self.assertEqual(self.occupying_count(unit), 2)

    def test_T12_promotion_into_an_occupying_status_is_checked(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2026-12-01", "2026-12-04"))
        booking = make_insert("booking", unit, "2026-12-02", "2026-12-05", status="CANCELLED")
        self.write(booking)
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(
                    "UPDATE bookings SET status = 'CONFIRMED' WHERE booking_id = %s",
                    (booking[2],),
                )
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", ""))
        self.assertEqual(outcome, CONFLICT_SQLSTATE)

    def test_T13_moving_unit_or_dates_cannot_bypass_the_guard(self) -> None:
        occupied = new_unit(self.dsn)
        second = new_unit(self.dsn)
        self.write(make_insert("hold", occupied, "2027-03-01", "2027-03-04"))
        self.write(make_insert("hold", second, "2027-04-01", "2027-04-04"))
        booking = make_insert("booking", second, "2027-03-01", "2027-03-04")
        self.write(booking)
        psycopg = _driver()
        for statement, params in (
            # dragging the row onto an occupied unit
            ("UPDATE bookings SET unit_id = %s WHERE booking_id = %s", (occupied, booking[2])),
            # keeping the unit, sliding the dates onto an occupied window
            (
                "UPDATE bookings SET check_in = '2027-04-02', check_out = '2027-04-05'"
                " WHERE booking_id = %s",
                (booking[2],),
            ),
        ):
            with psycopg.connect(self.dsn, connect_timeout=10) as conn:
                try:
                    conn.execute(statement, params)
                    conn.commit()
                    outcome = "commit"
                except psycopg.Error as error:
                    conn.rollback()
                    outcome = str(getattr(error, "sqlstate", ""))
            self.assertEqual(outcome, CONFLICT_SQLSTATE, statement)


# ============================================================ PCC-T14 . T15 . T16 . T17
class TestConcurrencyAndRollback(GuardCase):
    def test_T04_two_simultaneous_connections_let_exactly_one_win(self) -> None:
        unit = new_unit(self.dsn)
        psycopg = _driver()
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def writer(kind: str, statement: tuple[str, dict, str]) -> None:
            with psycopg.connect(self.dsn, connect_timeout=10) as conn:
                barrier.wait()
                try:
                    conn.execute(statement[0], statement[1])
                    conn.commit()
                    outcomes[kind] = "commit"
                except psycopg.Error as error:
                    conn.rollback()
                    outcomes[kind] = str(getattr(error, "sqlstate", "") or type(error).__name__)

        threads = [
            threading.Thread(
                target=writer, args=(kind, make_insert(kind, unit, *window))
            )
            for kind, window in (
                ("hold", ("2026-10-01", "2026-10-04")),
                ("booking", ("2026-10-02", "2026-10-05")),
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(outcomes.values()), [CONFLICT_SQLSTATE, "commit"], outcomes)
        self.assertEqual(self.occupying_count(unit), 1, self.rows(unit))

    def test_T14_rollback_releases_the_claim(self) -> None:
        unit = new_unit(self.dsn)
        psycopg = _driver()
        hold = make_insert("hold", unit, "2027-04-01", "2027-04-04")
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
            conn.rollback()
        self.assertEqual(self.rows(unit), [])
        self.write(make_insert("booking", unit, "2027-04-01", "2027-04-04"))

    def test_T15_a_stale_snapshot_cannot_smuggle_a_conflict(self) -> None:
        """Reader opens a transaction, sees nothing, then writes after the writer committed."""
        unit = new_unit(self.dsn)
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as stale:
            stale.execute("SELECT count(*) FROM holds WHERE unit_id = %s", (unit,)).fetchone()
            self.write(make_insert("hold", unit, "2027-08-01", "2027-08-04"))
            try:
                stale.execute(*make_insert("booking", unit, "2027-08-02", "2027-08-05")[:2])
                stale.commit()
                outcome = "commit"
            except psycopg.Error as error:
                stale.rollback()
                outcome = str(getattr(error, "sqlstate", ""))
        self.assertEqual(outcome, CONFLICT_SQLSTATE, "stale snapshot won the race")
        self.assertEqual(self.occupying_count(unit), 1)

    def test_T16_repeated_races_never_leave_two_occupying_rows(self) -> None:
        psycopg = _driver()
        for round_index in range(5):
            unit = new_unit(self.dsn)
            barrier = threading.Barrier(2)
            results: list[bool] = []
            statements = {
                "hold": make_insert("hold", unit, "2027-09-01", "2027-09-04"),
                "booking": make_insert("booking", unit, "2027-09-02", "2027-09-05"),
            }

            def writer(kind: str) -> None:
                statement, params, _ = statements[kind]
                with psycopg.connect(self.dsn, connect_timeout=10) as conn:
                    barrier.wait()
                    try:
                        conn.execute(statement, params)
                        conn.commit()
                        results.append(True)
                    except psycopg.Error:
                        conn.rollback()
                        results.append(False)

            threads = [threading.Thread(target=writer, args=(kind,)) for kind in statements]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(results), 1, f"round {round_index}: {results}")
            self.assertEqual(self.occupying_count(unit), 1, f"round {round_index}: {self.rows(unit)}")

    def test_T17_a_disagreeing_agency_id_buys_no_exemption(self) -> None:
        """The unit is the physical resource; 002 keys on unit_id alone and so must 021.

        A row that names a foreign agency_id while occupying another tenant's unit is refused
        with the same 23P01 as any other double booking. Tenant isolation is not weakened by
        that: a different tenant on a different unit still commits normally.
        """
        occupied = new_unit(self.dsn)
        other = new_unit(self.dsn)
        psycopg = _driver()
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(
                "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
                " VALUES ('agency_other', 'Other tenant', 'ACTIVE', '2026-09-21', '2026-09-21')"
                " ON CONFLICT DO NOTHING"
            )
        self.write(make_insert("hold", occupied, "2027-10-01", "2027-10-04"))
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(
                    "INSERT INTO bookings (booking_id, hold_ref, agency_id, property_id, unit_id,"
                    " check_in, check_out, nights, status, guest_ref, adults, children, infants,"
                    " currency, policy_version_id, policy_fingerprint, total_minor, payment_status,"
                    " idempotency_key, created_at, updated_at)"
                    " VALUES ('bk_foreign', NULL, 'agency_other', 'prop_cc', %s, '2027-10-02',"
                    " '2027-10-05', 3, 'CONFIRMED', 'guest_cc', 2, 0, 0, 'IDR', 'policy_cc', %s,"
                    " 900000, 'UNPAID', 'idem_bk_foreign', '2026-09-21', '2026-09-21')",
                    (occupied, "0" * 64),
                )
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", "") or type(error).__name__)
        self.assertEqual(outcome, CONFLICT_SQLSTATE, "a foreign agency_id claimed an occupied unit")

        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(
                "INSERT INTO bookings (booking_id, hold_ref, agency_id, property_id, unit_id,"
                " check_in, check_out, nights, status, guest_ref, adults, children, infants,"
                " currency, policy_version_id, policy_fingerprint, total_minor, payment_status,"
                " idempotency_key, created_at, updated_at)"
                " VALUES ('bk_other', NULL, 'agency_other', 'prop_cc', %s, '2027-10-01',"
                " '2027-10-04', 3, 'CONFIRMED', 'guest_cc', 2, 0, 0, 'IDR', 'policy_cc', %s,"
                " 900000, 'UNPAID', 'idem_bk_other', '2026-09-21', '2026-09-21')",
                (other, "0" * 64),
            )
            conn.commit()
        self.assertEqual(len(self.rows(other)), 1, "a different unit was blocked")
        self.assertEqual(self.occupying_count(occupied), 1)


class TestReviewRegressions(GuardCase):
    """Each test pins a seam a bounded adversarial review found in the first cut."""

    def test_T25_a_converted_hold_cannot_be_reopened_while_its_booking_occupies(self) -> None:
        """B1: the linked-booking exemption must not apply at COMMIT."""
        unit = new_unit(self.dsn)
        hold = make_insert("hold", unit, "2027-07-01", "2027-07-04")
        self.write(hold)
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(*booking_with_hold(unit, "2027-07-01", "2027-07-04", hold[2])[:2])
            conn.execute(
                "UPDATE holds SET status = 'CONVERTED', closed_at = '2026-09-22',"
                " close_reason = 'converted' WHERE hold_id = %s",
                (hold[2],),
            )
            conn.commit()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            try:
                conn.execute(
                    "UPDATE holds SET status = 'CONFIRMED', closed_at = NULL, close_reason = NULL"
                    " WHERE hold_id = %s",
                    (hold[2],),
                )
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", "") or type(error).__name__)
        self.assertEqual(outcome, CONFLICT_SQLSTATE, "a converted hold was re-opened onto its booking")
        self.assertEqual(self.occupying_count(unit), 1, self.rows(unit))

    def test_T26_a_temp_table_shadow_cannot_hide_the_public_tables(self) -> None:
        """B2: PostgreSQL searches pg_temp first for relation names, so search_path alone is
        not a defence; every relation the guard reads is schema-qualified instead."""
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2028-01-01", "2028-01-04"))
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(
                "CREATE TEMP TABLE holds (hold_id text, agency_id text, unit_id text,"
                " check_in text, check_out text, status text)"
            )
            conn.execute(
                "CREATE TEMP TABLE bookings (booking_id text, hold_ref text, agency_id text,"
                " unit_id text, check_in text, check_out text, status text)"
            )
            try:
                conn.execute(
                    "INSERT INTO public.bookings (booking_id, hold_ref, agency_id, property_id,"
                    " unit_id, check_in, check_out, nights, status, guest_ref, adults, children,"
                    " infants, currency, policy_version_id, policy_fingerprint, total_minor,"
                    " payment_status, idempotency_key, created_at, updated_at)"
                    " VALUES ('bk_shadow', NULL, 'agency_default', 'prop_cc', %s, '2028-01-02',"
                    " '2028-01-05', 3, 'CONFIRMED', 'guest_cc', 2, 0, 0, 'IDR', 'policy_cc', %s,"
                    " 900000, 'UNPAID', 'idem_bk_shadow', '2026-09-21', '2026-09-21')",
                    (unit, "0" * 64),
                )
                conn.commit()
                outcome = "commit"
            except psycopg.Error as error:
                conn.rollback()
                outcome = str(getattr(error, "sqlstate", "") or type(error).__name__)
        self.assertEqual(outcome, CONFLICT_SQLSTATE, "temp-table shadowing bypassed the guard")

    def test_T27_insert_then_delete_in_one_transaction_is_not_a_conflict(self) -> None:
        """L1: the deferred event of a row that no longer exists must not assert."""
        unit = new_unit(self.dsn)
        hold = make_insert("hold", unit, "2029-02-01", "2029-02-04")
        self.write(hold)
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            booking = booking_with_hold(unit, "2029-02-01", "2029-02-04", hold[2])
            conn.execute(*booking[:2])
            conn.execute("DELETE FROM bookings WHERE booking_id = %s", (booking[2],))
            conn.commit()
        self.assertEqual(self.occupying_count(unit), 1)


class TestGuardIntegrity(GuardCase):
    def test_T18_same_table_exclusions_still_fire(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2027-11-01", "2027-11-04"))
        self.write(make_insert("hold", unit, "2027-11-02", "2027-11-05"), expect="refuse")

    def test_T19_dropping_the_guard_is_detected_by_the_integrity_gate(self) -> None:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
        import pg_migrations as PGM

        psycopg = _driver()
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("DROP TRIGGER trg_holds_cross_table_occupancy ON holds")
        missing = PGM.verify_guards(connect(self.dsn))
        self.assertTrue(
            any("trg_holds_cross_table_occupancy" in item for item in missing),
            f"the dropped trigger was not reported: {missing}",
        )

    def test_T20_legitimate_transitions_still_commit(self) -> None:
        unit = new_unit(self.dsn)
        psycopg = _driver()
        hold = make_insert("hold", unit, "2027-12-01", "2027-12-04", status="REQUESTED")
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
            conn.commit()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute("UPDATE holds SET status = 'CONFIRMED' WHERE hold_id = %s", (hold[2],))
            conn.execute(
                "UPDATE holds SET status = 'CONVERTED', closed_at = '2026-09-22',"
                " close_reason = 'converted' WHERE hold_id = %s",
                (hold[2],),
            )
            conn.commit()
        self.assertEqual([row[2] for row in self.rows(unit)], ["CONVERTED"])
        self.write(make_insert("booking", unit, "2027-12-01", "2027-12-04"))

    def test_T21_the_formerly_strict_xfail_now_passes(self) -> None:
        """The assertion that used to be an acknowledged gap, without the xfail marker."""
        unit = new_unit(self.dsn)
        psycopg = _driver()
        hold = make_insert("hold", unit, "2027-03-01", "2027-03-04")
        booking = make_insert("booking", unit, "2027-03-02", "2027-03-05")
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute(hold[0], hold[1])
            with pytest.raises(psycopg.errors.Error):
                conn.execute(booking[0], booking[1])


# ============================================================ PCC-T22 . T23 . T24
class TestStatementsAndMigration(GuardCase):
    def test_T22_multi_row_statements_cannot_bypass_the_check(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2028-01-01", "2028-01-04"))
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            # INSERT ... SELECT creating one legal and one illegal row: the statement is atomic.
            with pytest.raises(psycopg.Error):
                conn.execute(
                    "INSERT INTO bookings (booking_id, hold_ref, property_id, unit_id, check_in,"
                    " check_out, nights, status, guest_ref, adults, children, infants, currency,"
                    " policy_version_id, policy_fingerprint, total_minor, payment_status,"
                    " idempotency_key, created_at, updated_at)"
                    " SELECT 'bk_multi_' || g, NULL, 'prop_cc', %s,"
                    "        (DATE '2028-01-05' + g * 0), (DATE '2028-01-08' + g * 0), 3,"
                    "        'CONFIRMED', 'g', 2, 0, 0, 'IDR', 'policy_cc', %s, 1000000, 'UNPAID',"
                    "        'idem_multi_' || g, '2026-09-21', '2026-09-21'"
                    " FROM generate_series(1, 2) AS g",
                    (unit, "0" * 64),
                )
                conn.commit()
        self.assertEqual(self.occupying_count(unit), 1, self.rows(unit))

    def test_T23_applying_the_migration_twice_does_not_duplicate_guards(self) -> None:
        psycopg = _driver()
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            before = conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
                " AND tgrelid IN ('holds'::regclass, 'bookings'::regclass)"
            ).fetchone()[0]
        only_cross_table_guard(self.dsn)  # 021 uses CREATE OR REPLACE, so re-running is safe
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            after = conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
                " AND tgrelid IN ('holds'::regclass, 'bookings'::regclass)"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(after, 4)

    def test_T24_the_migration_refuses_a_database_with_live_contradictions(self) -> None:
        """Install order matters: contradictory rows must exist before 021, then 021 must stop."""
        unit = new_unit(self.dsn)
        psycopg = _driver()
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as conn:
            for name, table in (
                ("trg_holds_cross_table_occupancy", "holds"),
                ("trg_bookings_cross_table_occupancy", "bookings"),
                ("trg_holds_occupancy_at_commit", "holds"),
                ("trg_bookings_occupancy_at_commit", "bookings"),
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            conn.execute(*make_insert("hold", unit, "2028-02-01", "2028-02-04")[:2])
            conn.execute(*make_insert("booking", unit, "2028-02-02", "2028-02-05")[:2])
        with pytest.raises(Exception) as caught:
            only_cross_table_guard(self.dsn)
        self.assertIn("existing overlap", str(caught.value).lower())
        self.assertEqual(self.occupying_count(unit), 2, "the contradictory rows should still be there")


class TestNightScopedLocking(GuardCase):
    """PACK VILLA-PG-CONCURRENCY-RECOVERY-002C: the lock is one per occupied night.

    CI already holds the kernel-level proof
    (tests/test_pg_overlap_concurrency.py::test_back_to_back_stays_do_not_overlap_and_do_not_wait),
    but that module refuses to run outside GitHub, so the property had no test a developer
    could run — which is how a unit-wide lock reached a green local suite. These two tests
    pin both halves of the distinction here: adjacency proceeds, overlap queues.
    """

    def _open_transaction(self, unit: str, check_in: str, check_out: str):
        psycopg = _driver()
        conn = psycopg.connect(self.dsn, connect_timeout=10)
        statement, params, _ = make_insert("hold", unit, check_in, check_out)
        conn.execute(statement, params)
        return conn

    def test_T27_adjacent_stay_does_not_queue_behind_an_open_transaction(self) -> None:
        """[06-01, 06-03) and [06-03, 06-05) share no night, so the second never waits."""
        unit = new_unit(self.dsn)
        holder = self._open_transaction(unit, "2027-06-01", "2027-06-03")
        try:
            psycopg = _driver()
            with psycopg.connect(self.dsn, connect_timeout=10) as adjacent:
                adjacent.execute("SET LOCAL lock_timeout = '2s'")
                statement, params, _ = make_insert("hold", unit, "2027-06-03", "2027-06-05")
                adjacent.execute(statement, params)  # must not wait on the open transaction
                adjacent.commit()
            holder.commit()  # the first stay is legitimate too, not a casualty of the test
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.occupying_count(unit), 2)

    def test_T29_the_lock_key_does_not_depend_on_the_session_date_style(self) -> None:
        """Two sessions, two DateStyle settings, one night — still one lock.

        Rendering a date by string concatenation follows the session's DateStyle, so a writer
        under 'SQL, DMY' and a writer under the default would mint different keys for the same
        night, never queue, and both commit an overlap. The holder here deliberately runs under
        the non-default style; the overlapping booking must still wait.
        """
        unit = new_unit(self.dsn)
        psycopg = _driver()
        holder = psycopg.connect(self.dsn, connect_timeout=10)
        statement, params, _ = make_insert("hold", unit, "2027-07-01", "2027-07-03")
        holder.execute("SET DateStyle = 'SQL, DMY'")
        holder.execute(statement, params)
        try:
            with psycopg.connect(self.dsn, connect_timeout=10) as overlapping:
                overlapping.execute("SET DateStyle = 'ISO, MDY'")
                overlapping.execute("SET LOCAL lock_timeout = '1s'")
                spec = make_insert("booking", unit, "2027-07-02", "2027-07-04")
                with self.assertRaises(psycopg.Error) as caught:
                    overlapping.execute(spec[0], spec[1])
                self.assertEqual(caught.exception.sqlstate, "55P03")
                overlapping.rollback()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.occupying_count(unit), 0)

    def test_T28_overlapping_booking_waits_for_the_open_hold(self) -> None:
        """The converse: a shared night is a shared lock, so the booking queues."""
        unit = new_unit(self.dsn)
        holder = self._open_transaction(unit, "2027-06-01", "2027-06-03")
        try:
            psycopg = _driver()
            with psycopg.connect(self.dsn, connect_timeout=10) as overlapping:
                overlapping.execute("SET LOCAL lock_timeout = '1s'")
                statement, params, _ = make_insert("booking", unit, "2027-06-02", "2027-06-04")
                with self.assertRaises(psycopg.Error) as caught:
                    overlapping.execute(statement, params)
                self.assertEqual(caught.exception.sqlstate, "55P03")
                overlapping.rollback()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.occupying_count(unit), 0)


# ============================================================ PACK-004C — canonical batch prelocking
#
# The module under test is an application-path mitigation, not a database guarantee: these tests
# pin both halves. The successful half (canonical order, dedup, dedup'd lock count, atomicity,
# capacity refusal before mutation, classification) and the negative half (a writer that skips the
# module still deadlocks — T004C-10), because a suite that only proved the first half would let
# somebody claim RISK-M1 is closed.

import hashlib  # noqa: E402
import time  # noqa: E402

import pg_batch_prelock as PB  # noqa: E402

APPROVED_021_SHA256 = "ec9af60372d956216594d5605d151fd65a12bcf232a00a40fed73fb753550b62"
APPROVED_002_FIXTURE_SHA256 = hashlib.sha256(
    (SCHEMA / "002_pg_range_exclusions.approved.json").read_bytes()
).hexdigest()


def _writer_connection(dsn: str) -> Any:
    psycopg = _driver()
    return psycopg.connect(
        dsn, connect_timeout=10, options="-c lock_timeout=5000 -c statement_timeout=20000"
    )


def _prelocked_writer(
    dsn: str,
    slots: list[tuple[str, str, str]],
    statements: list[tuple[str, dict, str]],
    gate: threading.Barrier | None,
    out: dict[str, str],
    label: str,
) -> None:
    """One racing writer that DOES use the module: prelock the whole batch, then insert."""
    try:
        with _writer_connection(dsn) as conn:
            if gate is not None:
                gate.wait(20)

            def operation(cursor: Any) -> None:
                for sql_text, params, _rid in statements:
                    cursor.execute(sql_text, params)

            PB.prelock_and_run(conn, slots, operation)
        out[label] = "commit"
    except PB.ClassifiedDatabaseError as error:
        out[label] = str(error.sqlstate or error.code)
    except PB.PrelockError as error:
        out[label] = error.code
    except BaseException as error:  # noqa: BLE001 - sqlstate string is the observed outcome
        out[label] = str(getattr(error, "sqlstate", type(error).__name__))


def _bypass_writer(
    dsn: str,
    statements: list[tuple[str, dict, str]],
    gate: threading.Barrier,
    mid: threading.Barrier,
    out: dict[str, str],
    label: str,
) -> None:
    """One racing writer that SKIPS the module, two statements, synchronised between them."""
    try:
        with _writer_connection(dsn) as conn:
            gate.wait(20)
            for index, (sql_text, params, _rid) in enumerate(statements):
                if index:
                    mid.wait(20)
                conn.execute(sql_text, params)
            conn.commit()
        out[label] = "commit"
    except BaseException as error:  # noqa: BLE001
        out[label] = str(getattr(error, "sqlstate", type(error).__name__))


def _race(readers: list[tuple[str, tuple]]) -> dict[str, str]:
    out: dict[str, str] = {}
    threads = []
    for entry in readers:
        label, (fn, args) = entry
        threads.append(
            threading.Thread(target=fn, args=tuple(args) + (out, label), daemon=True)
        )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(45)
    return out


class TestCanonicalBatchPrelocking(GuardCase):
    """PACK-004C: FR-PG-004C-01 — deterministic occupancy lock acquisition."""

    def test_T004C_00_approved_sql_and_fixture_are_unchanged(self) -> None:
        """This pack is additive: if either digest moves, the pack broke its own scope."""
        self.assertEqual(hashlib.sha256(PG_021.read_bytes()).hexdigest(), APPROVED_021_SHA256)
        self.assertEqual(
            hashlib.sha256((SCHEMA / "002_pg_range_exclusions.approved.json").read_bytes()).hexdigest(),
            APPROVED_002_FIXTURE_SHA256,
        )

    def test_T004C_01_complete_lock_set_is_canonicalized(self) -> None:
        """Units out of order, nights out of order: the plan and the locks are both sorted."""
        # The plan orders by unit_id, so make the expected order explicit rather than relying on
        # the random suffix of new_unit() sorting the way the calls were written.
        first, second = sorted([new_unit(self.dsn), new_unit(self.dsn)])
        plan = PB.canonical_plan([
            (second, "2030-03-05", "2030-03-08"),
            (first, "2030-03-01", "2030-03-03"),
        ])
        expected = sorted(
            [(first, _dt_date("2030-03-01")), (first, _dt_date("2030-03-02")),
             (second, _dt_date("2030-03-05")), (second, _dt_date("2030-03-06")),
             (second, _dt_date("2030-03-07"))],
            key=lambda item: (item[0], item[1]),
        )
        self.assertEqual(list(plan.nights), expected)
        self.assertEqual(plan.ranges, ((first, "2030-03-01", "2030-03-03"),
                                       (second, "2030-03-05", "2030-03-08")))
        with _writer_connection(self.dsn) as conn:
            PB.prelock(conn, plan.stays)
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchone()[0]
            conn.rollback()
        self.assertEqual(held, plan.lock_count, "one lock per unit-night, no extras")

    def test_T004C_02_overlapping_slot_identities_are_deduplicated(self) -> None:
        unit = new_unit(self.dsn)
        plan = PB.canonical_plan([
            (unit, "2030-04-01", "2030-04-04"),
            (unit, "2030-04-02", "2030-04-05"),
            (unit, "2030-04-01", "2030-04-04"),
        ])
        self.assertEqual(plan.lock_count, 4, "three stays, four distinct nights")
        with _writer_connection(self.dsn) as conn:
            PB.prelock(conn, plan.stays)
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchone()[0]
            conn.rollback()
        self.assertEqual(held, 4)

    def test_T004C_02b_a_forged_plan_cannot_lock_less_than_it_declares(self) -> None:
        """A hand-built plan must not become a subset pretending to be the complete set."""
        unit = new_unit(self.dsn)
        plan = PB.canonical_plan([(unit, "2030-04-10", "2030-04-13")])
        partial = PB.LockPlan(
            stays=plan.stays, nights=plan.nights, ranges=((unit, "2030-04-10", "2030-04-11"),)
        )
        unordered = PB.LockPlan(
            stays=plan.stays, nights=tuple(reversed(plan.nights)), ranges=plan.ranges
        )
        with _writer_connection(self.dsn) as conn:
            for forged in (partial, unordered):
                with self.assertRaises(PB.UnsupportedOperation, msg=str(forged.ranges)):
                    PB.lock_batch(conn, forged)
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchone()[0]
            conn.rollback()
        self.assertEqual(held, 0, "a refused plan must not take a single lock")

    def test_T004C_03_opposite_order_writers_never_deadlock(self) -> None:
        """The PACK-004A shape: two writers, two units, inverted declaration order."""
        outcomes: list[dict[str, str]] = []
        for _ in range(3):
            left, right = new_unit(self.dsn), new_unit(self.dsn)
            window = ("2030-05-01", "2030-05-03")
            a_slots = [(left, *window), (right, *window)]
            b_slots = [(right, *window), (left, *window)]
            a_rows = [make_insert("hold", left, *window), make_insert("hold", right, *window)]
            b_rows = [make_insert("hold", right, *window), make_insert("hold", left, *window)]
            gate = threading.Barrier(2)
            outcomes.append(_race([
                ("A", (_prelocked_writer, (self.dsn, a_slots, a_rows, gate))),
                ("B", (_prelocked_writer, (self.dsn, b_slots, b_rows, gate))),
            ]))
            self.assertEqual(self.occupying_count(left), 1)
            self.assertEqual(self.occupying_count(right), 1)
            self.assertEqual(self.cross_table_overlaps(), 0)

        for outcome in outcomes:
            self.assertNotIn("40P01", outcome.values(), f"deadlock under prelocking: {outcome}")
            self.assertEqual(sorted(outcome.values()).count("commit"), 1, outcome)
            loser = [value for value in outcome.values() if value != "commit"][0]
            self.assertIn(loser, {"23P01", "23505"}, outcome)

    def test_T004C_04_multi_row_operation_stays_atomic(self) -> None:
        """One conflicting row refuses the whole batch: the free unit stays empty."""
        free, busy = new_unit(self.dsn), new_unit(self.dsn)
        self.write(make_insert("hold", busy, "2030-06-01", "2030-06-03"))
        rows = [make_insert("hold", free, "2030-06-01", "2030-06-03"),
                make_insert("hold", busy, "2030-06-02", "2030-06-04")]
        with _writer_connection(self.dsn) as conn:
            with self.assertRaises(PB.ClassifiedDatabaseError) as caught:
                PB.prelock_and_run(
                    conn,
                    [(free, "2030-06-01", "2030-06-03"), (busy, "2030-06-02", "2030-06-04")],
                    lambda cursor: [cursor.execute(sql, params) for sql, params, _ in rows],
                )
        self.assertEqual(caught.exception.sqlstate, CONFLICT_SQLSTATE)
        self.assertEqual(caught.exception.details["classification"], "EXCLUSION_VIOLATION")
        self.assertEqual(self.occupying_count(free), 0, "the free unit must not keep a partial write")
        self.assertEqual(self.occupying_count(busy), 1)

    def test_T004C_05_back_to_back_stays_remain_nonblocking(self) -> None:
        unit = new_unit(self.dsn)
        first = make_insert("hold", unit, "2030-07-01", "2030-07-03")
        second = make_insert("hold", unit, "2030-07-03", "2030-07-05")
        gate = threading.Barrier(2)
        started = time.perf_counter()
        outcome = _race([
            ("A", (_prelocked_writer, (self.dsn, [(unit, "2030-07-01", "2030-07-03")], [first], gate))),
            ("B", (_prelocked_writer, (self.dsn, [(unit, "2030-07-03", "2030-07-05")], [second], gate))),
        ])
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(outcome, {"A": "commit", "B": "commit"})
        self.assertLess(elapsed_ms, 2000, f"adjacent stays queued: {elapsed_ms:.0f} ms")
        self.assertEqual(self.occupying_count(unit), 2)

    def test_T004C_06_same_unit_overlap_remains_rejected(self) -> None:
        unit = new_unit(self.dsn)
        self.write(make_insert("hold", unit, "2030-08-01", "2030-08-04"))
        rows = [make_insert("hold", unit, "2030-08-02", "2030-08-05")]
        with _writer_connection(self.dsn) as conn:
            with self.assertRaises(PB.ClassifiedDatabaseError) as caught:
                PB.prelock_and_run(conn, [(unit, "2030-08-02", "2030-08-05")],
                                   lambda cursor: cursor.execute(rows[0][0], rows[0][1]))
        self.assertEqual(caught.exception.sqlstate, CONFLICT_SQLSTATE)
        self.assertEqual(self.occupying_count(unit), 1)

    def test_T004C_07_hold_to_booking_conversion_stays_valid(self) -> None:
        unit = new_unit(self.dsn)
        hold = make_insert("hold", unit, "2030-09-01", "2030-09-04", status="CONFIRMED")
        booking = booking_with_hold(unit, "2030-09-01", "2030-09-04", hold[2])

        def convert(cursor: Any) -> None:
            cursor.execute(hold[0], hold[1])
            cursor.execute(*booking[:2])
            cursor.execute(
                "UPDATE holds SET status = 'CONVERTED', closed_at = %(ts)s,"
                " close_reason = 'converted' WHERE hold_id = %(id)s",
                {"ts": "2026-09-22", "id": hold[2]},
            )

        with _writer_connection(self.dsn) as conn:
            PB.prelock_and_run(conn, [(unit, "2030-09-01", "2030-09-04")], convert)
        statuses = {row[1]: row[2] for row in self.rows(unit)}
        self.assertEqual(statuses[hold[2]], "CONVERTED")
        self.assertEqual(statuses[booking[2]], "CONFIRMED")
        self.assertEqual(self.occupying_count(unit), 1)
        self.assertEqual(self.cross_table_overlaps(), 0)

    def test_T004C_08_limits_fail_closed_before_any_mutation(self) -> None:
        units = [new_unit(self.dsn) for _ in range(20)]
        with _writer_connection(self.dsn) as conn:
            # rows: 21 > 20
            with self.assertRaises(PB.BatchCapacityRefused) as rows_refused:
                PB.prelock_and_run(
                    conn, [(units[0], "2030-10-01", "2030-10-02")] * 21, lambda cursor: None
                )
            self.assertEqual(rows_refused.exception.code, "BATCH_CAPACITY_REFUSED")
            # nights: a 366-night stay
            with self.assertRaises(PB.BatchCapacityRefused):
                PB.prelock_and_run(conn, [(units[0], "2031-01-01", "2032-01-02")], lambda cursor: None)
            # distinct locks: 20 rows x 260 nights = 5 200 > 5 000 while each stay is legal
            wide = [(unit, "2033-01-01", "2033-09-18") for unit in units]
            self.assertEqual(PB.canonical_plan([(units[0], "2033-01-01", "2033-09-18")]).lock_count, 260)
            with self.assertRaises(PB.BatchCapacityRefused) as locks_refused:
                PB.prelock_and_run(conn, wide, lambda cursor: None)
            self.assertEqual(locks_refused.exception.details["distinct_locks"], 5200)
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchone()[0]
            conn.rollback()
        self.assertEqual(held, 0, "a refused batch must not take a single lock")
        self.assertEqual(self.cross_table_overlaps(), 0)

    def test_T004C_09_sqlstates_receive_their_contract_classification(self) -> None:
        class FakeError(Exception):
            def __init__(self, sqlstate: str) -> None:
                super().__init__(sqlstate)
                self.sqlstate = sqlstate

        expected = {
            "40P01": ("DEADLOCK", "retry_candidate"),
            "55P03": ("LOCK_UNAVAILABLE", "conditional_retry_candidate"),
            "57014": ("QUERY_CANCELED", "fail_closed"),
            "53200": ("SHARED_MEMORY_EXHAUSTED", "fail_closed"),
            "23P01": ("EXCLUSION_VIOLATION", "domain_conflict"),
            "23505": ("UNIQUE_VIOLATION", "inspect_idempotency_or_conflict"),
            "40001": ("SERIALIZATION_FAILURE", "report_separately"),
            "XX000": ("UNMAPPED", "no_retry"),
        }
        for sqlstate, (name, hint) in expected.items():
            classification = PB.classify(FakeError(sqlstate))
            self.assertEqual((classification.name, classification.retry_hint), (name, hint))
        with self.assertRaises(PB.ClassifiedDatabaseError) as wrapped:
            raise PB.ClassifiedDatabaseError(FakeError("55P03"))
        self.assertEqual(wrapped.exception.sqlstate, "55P03")
        self.assertEqual(wrapped.exception.details["classification"], "LOCK_UNAVAILABLE")

    def test_T004C_09b_unsafe_transaction_states_are_refused(self) -> None:
        psycopg = _driver()
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as autocommit:
            with self.assertRaises(PB.UnsafeTransactionState):
                PB.prelock(autocommit, [(new_unit(self.dsn), "2030-11-01", "2030-11-03")])
        with _writer_connection(self.dsn) as conn:
            conn.execute("SELECT 1")  # transaction now open
            with self.assertRaises(PB.UnsafeTransactionState):
                PB.prelock(conn, [(new_unit(self.dsn), "2030-11-01", "2030-11-03")])
            conn.rollback()

    def test_T004C_09c_invalid_slots_are_rejected_before_sql(self) -> None:
        unit = new_unit(self.dsn)
        bad = [
            ("", "2030-12-01", "2030-12-03"),
            (" padded", "2030-12-01", "2030-12-03"),
            (unit, "2030-12-03", "2030-12-03"),
            (unit, "2030-12-03", "2030-12-01"),
            (unit, "2027-02-30", "2027-03-02"),
            (unit, "12/01/2030", "2030-12-03"),
        ]
        for slot in bad:
            with self.assertRaises(PB.SlotRejected, msg=str(slot)):
                PB.canonical_plan([slot])
        with self.assertRaises(PB.SlotRejected):
            PB.canonical_plan([])

    def test_T004C_10_a_writer_that_skips_the_module_still_deadlocks(self) -> None:
        """Negative control. Without this, a green suite would be a claim nobody tested."""
        observed: list[str] = []
        for _ in range(2):
            left, right = new_unit(self.dsn), new_unit(self.dsn)
            window = ("2031-02-01", "2031-02-04")
            gate, mid = threading.Barrier(2), threading.Barrier(2)
            outcome = _race([
                ("A", (_bypass_writer, (self.dsn, [make_insert("hold", left, *window),
                                                   make_insert("hold", right, *window)], gate, mid))),
                ("B", (_bypass_writer, (self.dsn, [make_insert("hold", right, *window),
                                                   make_insert("hold", left, *window)], gate, mid))),
            ])
            observed.append(outcome.get("A", "") + "/" + outcome.get("B", ""))
            self.assertEqual(self.cross_table_overlaps(), 0)
        self.assertTrue(
            any("40P01" in value for value in observed),
            f"expected the unmitigated shape to deadlock, saw {observed}",
        )

    def cross_table_overlaps(self) -> int:
        psycopg = _driver()
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            return conn.execute(
                "SELECT count(*) FROM holds h JOIN bookings b ON b.unit_id = h.unit_id"
                " AND daterange(villa_iso_date(h.check_in), villa_iso_date(h.check_out), '[)')"
                "  && daterange(villa_iso_date(b.check_in), villa_iso_date(b.check_out), '[)')"
                " WHERE h.status = ANY(villa_occupying_states('hold'))"
                " AND b.status = ANY(villa_occupying_states('booking'))"
            ).fetchone()[0]


def _dt_date(value: str) -> Any:
    import datetime

    return datetime.date.fromisoformat(value)

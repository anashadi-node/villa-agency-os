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

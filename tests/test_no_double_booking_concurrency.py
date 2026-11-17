"""Concurrency tests for the no-double-booking guarantee.

Every other test in ``test_reservation_kernel.py`` is sequential: it proves that a
*second* attempt is refused **after** the first one has committed. That is not the
same claim as "two guests clicking at the same instant cannot both get the slot",
which is the guarantee the North Star actually states.

These tests put two (and three) real writers on one file-backed database, release
them through a ``threading.Barrier``, and assert the outcome of the *race*:

* exactly one writer holds the slot;
* the losers fail with a domain error, not with a silent success;
* the losers leave no partial evidence and no partial hold behind;
* the winners are actually concurrent (the positive control proves the harness can
  produce two simultaneous successes when the units are different, so a
  one-winner result cannot be an artefact of accidental serialisation).

``:memory:`` cannot be used here: SQLite gives each connection a private in-memory
database, so a second writer would see an empty world and the test would pass for
the wrong reason. Hence a real file, WAL, and the kernel's own ``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

import booking_support
from test_reservation_kernel import NOW, KernelTestCase

import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory


def _open_writer(db_path: str, index) -> RK.ReservationKernel:
    """A second, independent connection — the way a separate request handler connects."""
    return RK.ReservationKernel.open(
        db_path,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=f"writer{index}"),
    )


class ConcurrencyTestCase(KernelTestCase):
    """The shared fixture, but on a real file so two connections can race."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="villa_race_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmpdir, ignore_errors=True))
        self.db_path = str(Path(self._tmpdir) / "race.db")
        super().setUp()

    # -- helpers ----------------------------------------------------------- #
    def race(self, writers: int, bodies, opener=None) -> list:
        """Run ``bodies[i]`` in its own thread released by one barrier.

        Returns one slot per writer holding either the result or the exception. The
        barrier is what makes the calls simultaneous: without it the operating system
        could serialise them and the test would prove nothing. ``opener`` swaps the
        writer class (PACK-06 negative control); the default is the real kernel.
        """
        barrier = threading.Barrier(writers)
        results: list = [None] * writers
        errors: list = [None] * writers
        open_writer = opener or _open_writer
        # Fresh id seeds per race: two races in one test must never mint the same ids.
        self._races = getattr(self, "_races", 0) + 1
        race_no = self._races

        def run(index: int) -> None:
            kernel = open_writer(self.db_path, f"{race_no}x{index}")
            try:
                barrier.wait(timeout=30)
                results[index] = bodies[index](kernel, index)
            except BaseException as exc:  # noqa: BLE001 - the point is to record it
                errors[index] = exc
            finally:
                kernel.close()

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "a writer thread never finished")
        return list(zip(results, errors))

    def row_count(self, table: str) -> int:
        return self.kernel.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    #: The exact set the schema's partial unique index treats as occupying a slot
    #: (``uq_holds_active_slot`` in ``src/schema/001_initial.sql``). Reading the set from
    #: the schema instead of inventing one is what keeps this assertion honest.
    BLOCKING = (
        "REQUESTED",
        "AWAITING_SUPPLIER",
        "CONFIRMED",
        "EXTENSION_REQUESTED",
        "RELEASE_REQUESTED",
        "EXPIRED_PENDING_VERIFICATION",
    )

    def blocking_holds(self, unit_id: str = "unit_1") -> int:
        marks = ",".join("?" * len(self.BLOCKING))
        return self.kernel.conn.execute(
            f"SELECT COUNT(*) AS n FROM holds WHERE unit_id = ? AND status IN ({marks})",
            (unit_id, *self.BLOCKING),
        ).fetchone()["n"]

    def hold_body(self, unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03", key_tag=""):
        def body(kernel: RK.ReservationKernel, index: int):
            return kernel.request_hold(
                unit_id=unit_id,
                check_in=check_in,
                check_out=check_out,
                guest_ref="guest_abc123",
                policy_version_id="policy_v1",
                idempotency_key=f"idem_hold_{key_tag}{index:04d}",
                actor="user:owner-1",
                actor_role="owner",
            )

        return body

    # -- PACK-06 invariant checks: read the database, not the exceptions --------- #
    #: Tables whose rows would change if a reservation race ever touched money.
    MONEY_TABLES = (
        "invoices", "payments", "payment_events", "ledger_entries",
        "supplier_settlements", "deposit_resolutions",
    )

    def money_snapshot(self) -> dict:
        return {table: self.row_count(table) for table in self.MONEY_TABLES}

    def occupants(self, unit_id: str = "unit_1") -> list:
        """Every row that occupies the unit under the kernel's own baseline vocabulary."""
        conn = self.kernel.conn
        holds = ",".join("?" * len(RK.OCCUPYING_HOLD_STATES))
        books = ",".join("?" * len(RK.OCCUPYING_BOOKING_STATES))
        rows = [
            ("hold", r["hold_id"], r["check_in"], r["check_out"])
            for r in conn.execute(
                f"SELECT hold_id, check_in, check_out FROM holds"
                f" WHERE unit_id = ? AND status IN ({holds})",
                (unit_id, *RK.OCCUPYING_HOLD_STATES),
            )
        ]
        rows += [
            ("booking", r["booking_id"], r["check_in"], r["check_out"])
            for r in conn.execute(
                f"SELECT booking_id, check_in, check_out FROM bookings"
                f" WHERE unit_id = ? AND status IN ({books})",
                (unit_id, *RK.OCCUPYING_BOOKING_STATES),
            )
        ]
        return rows

    def overlapping_pairs(self, unit_id: str = "unit_1") -> list:
        """Pairs of occupying rows whose half-open ranges intersect (must always be empty)."""
        rows = self.occupants(unit_id)
        return [
            (a, b)
            for i, a in enumerate(rows)
            for b in rows[i + 1:]
            if a[2] < b[3] and b[2] < a[3]
        ]

    def assert_invariant(self, *unit_ids: str) -> None:
        for unit_id in unit_ids or ("unit_1",):
            self.assertEqual(
                self.overlapping_pairs(unit_id), [],
                f"double booking: overlapping occupants on {unit_id}",
            )

    def raw_booking(self, booking_id, unit_id, check_in, check_out, status):
        """A booking row written straight into the table (no kernel), as the existing
        ADR-0002 test does, so a refusal can only come from the kernel's own check."""
        self.kernel.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
            " nights, status, guest_ref, adults, children, infants, currency,"
            " policy_version_id, policy_fingerprint, quote_ref, hold_ref, total_minor,"
            " amount_paid_minor, security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at, confirmed_at, cancelled_at)"
            " VALUES (?, 'prop_1', ?, ?, ?, 2, ?, 'guest_abc123', 2, 0, 0, 'IDR',"
            " 'policy_v1', ?, NULL, NULL, 1000000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
            (booking_id, unit_id, check_in, check_out, status, "0" * 64,
             f"idem_{booking_id}", NOW, NOW, NOW),
        )

    def reserve_paid(self, *, check_in, check_out, tag, unit_id="unit_1"):
        """Hold + accepted quote + reservation + verified payment: a booking in
        ``PAID_IN_FULL`` that only needs the villa's block proof to be confirmed."""
        quote = self.persist_quote(
            unit_id=unit_id, check_in=check_in, check_out=check_out, quote_id=f"quote_{tag}"
        )
        hold = self.kernel.request_hold(
            unit_id=unit_id, check_in=check_in, check_out=check_out,
            guest_ref="guest_abc123", policy_version_id="policy_v1",
            idempotency_key=f"idem_hold_{tag}", quote_ref=quote.quote_id,
            actor="user:owner-1", actor_role="owner",
        )
        reserved = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key=f"idem_book_{tag}", command_id=f"cmd_book_{tag}", adults=2,
        )
        payment_id = booking_support.pay_booking(self.kernel, reserved.booking_id, key=f"pay_{tag}")
        self.kernel.mark_paid(
            booking_id=reserved.booking_id, payment_id=payment_id,
            command_id=f"cmd_paid_{tag}", actor="user:finance-1", actor_role="finance",
        )
        return hold, quote, reserved


class UnguardedKernel(RK.ReservationKernel):
    """NEGATIVE CONTROL ONLY (PACK-06 J): the real kernel minus its overlap query.

    It exists so the suite can prove it *detects* a violation: if the harness and the
    invariant check were blind, the guarded kernel would "pass" for the wrong reason.
    The schema's partial unique indexes are left in place on purpose.
    """

    def _assert_no_overlap(self, *args, **kwargs):
        return None


def _open_unguarded(db_path: str, index) -> RK.ReservationKernel:
    return UnguardedKernel.open(
        db_path, clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed=f"unguarded{index}")
    )


class TestRaceForOneSlot(ConcurrencyTestCase):
    def test_two_writers_racing_for_one_slot_produce_exactly_one_hold(self):
        outcomes = self.race(2, [self.hold_body(), self.hold_body()])
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]

        self.assertEqual(len(wins), 1, f"expected exactly one winner, got {len(wins)}")
        self.assertEqual(len(losses), 1, "the losing writer must fail, not silently succeed")
        self.assertIsInstance(losses[0], DomainError)
        self.assertEqual(losses[0].code, "SLOT_ALREADY_HELD")
        self.assertEqual(self.blocking_holds(), 1)
        self.assertEqual(self.row_count("holds"), 1)

    def test_a_refused_race_leaves_no_partial_evidence_behind(self):
        self.race(2, [self.hold_body(), self.hold_body()])
        self.assertEqual(self.blocking_holds(), 1)
        self.assertEqual(self.row_count("availability_evidence"), 1)
        self.assertEqual(self.row_count("audit_events"), 1)

    def test_three_overlapping_windows_produce_exactly_one_hold(self):
        bodies = [
            self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
            self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
            self.hold_body(check_in="2026-10-02", check_out="2026-10-05"),
        ]
        outcomes = self.race(3, bodies)
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]

        self.assertEqual(len(wins), 1, f"expected exactly one winner, got {len(wins)}")
        self.assertEqual(len(losses), 2)
        for error in losses:
            self.assertIsInstance(error, DomainError)
            self.assertEqual(error.code, "SLOT_ALREADY_HELD")
        self.assertEqual(self.row_count("holds"), 1)


class TestHarnessPositiveControl(ConcurrencyTestCase):
    """Without this, "one winner" could mean the threads never actually overlapped."""

    def test_two_writers_on_different_units_both_win(self):
        outcomes = self.race(
            2,
            [self.hold_body(unit_id="unit_1"), self.hold_body(unit_id="unit_2")],
        )
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]

        self.assertEqual(losses, [], f"no writer should fail here, got {losses}")
        self.assertEqual(len(wins), 2, "the harness must be able to produce two simultaneous wins")
        self.assertEqual({w.unit_id for w in wins}, {"unit_1", "unit_2"})
        self.assertEqual(self.row_count("holds"), 2)

    def test_two_writers_on_back_to_back_windows_both_win(self):
        """Half-open intervals: 01→03 and 03→05 touch but do not overlap."""
        outcomes = self.race(
            2,
            [
                self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
                self.hold_body(check_in="2026-10-03", check_out="2026-10-05"),
            ],
        )
        wins = [r for r, e in outcomes if e is None]
        self.assertEqual(len(wins), 2, "back-to-back stays are legal and must both commit")
        self.assertEqual(self.row_count("holds"), 2)


class TestRacingIdempotency(ConcurrencyTestCase):
    def test_same_idempotency_key_racing_yields_one_row_and_one_replay(self):
        """A double-click with the same key must not create two holds."""

        def body(kernel: RK.ReservationKernel, index: int):
            return kernel.request_hold(
                unit_id="unit_1",
                check_in="2026-10-01",
                check_out="2026-10-03",
                guest_ref="guest_abc123",
                policy_version_id="policy_v1",
                idempotency_key="idem_hold_same_key",
                actor="user:owner-1",
                actor_role="owner",
            )

        outcomes = self.race(2, [body, body])
        self.assertEqual([e for _r, e in outcomes if e is not None], [])
        holds = [r for r, _e in outcomes]
        self.assertEqual(len(holds), 2)
        self.assertEqual(len({h.hold_id for h in holds}), 1, "the replay must return the same hold")
        self.assertEqual(sum(1 for h in holds if h.replayed), 1, "exactly one call is a replay")
        self.assertEqual(self.row_count("holds"), 1)


class TestRaceAgainstConfirmation(ConcurrencyTestCase):
    def test_a_hold_reserved_while_another_writer_races_still_books_once(self):
        """ADR: the slot must not slip between hold and booking under concurrency.

        Audit 2026-11-13 (K2) retired the one-click ``confirm_from_hold``: a booking now
        waits in ``PAYMENT_PENDING`` on its hold, is paid by Finance and closed with the
        villa's block proof. The race is unchanged in substance — a second writer asking
        for overlapping nights while the first one is being booked must lose — so the
        winner has to walk the real three-step road, not a shortcut around it.
        """
        quote = self.persist_quote()
        hold = self.kernel.request_hold(
            unit_id="unit_1",
            check_in="2026-10-01",
            check_out="2026-10-03",
            guest_ref="guest_abc123",
            policy_version_id="policy_v1",
            idempotency_key="idem_hold_0000",
            quote_ref=quote.quote_id,
            actor="user:owner-1",
            actor_role="owner",
        )

        def book(kernel: RK.ReservationKernel, index: int):
            return booking_support.confirm_through_payment(
                kernel,
                hold_id=hold.hold_id,
                quote_id=quote.quote_id,
                idempotency_key="idem_book_0001",
                command_id="cmd_0001",
                adults=2,
            )

        outcomes = self.race(2, [book, self.hold_body(check_in="2026-10-02", check_out="2026-10-04")])
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]

        self.assertEqual(len(wins), 1, f"exactly one writer may succeed, got {wins}")
        self.assertEqual(len(losses), 1)
        self.assertIsInstance(losses[0], DomainError)
        # The racer can only lose through the holds (or, if the booking landed first,
        # through the bookings it now occupies), and both reasons are refusals.
        self.assertIn(losses[0].code, {"SLOT_ALREADY_HELD", "SLOT_ALREADY_BOOKED"}, losses[0].message)
        self.assertEqual(self.row_count("bookings"), 1, "the confirmation must still land")
        self.assertEqual(self.blocking_holds(), 0, "no hold may stay occupying alongside the booking")
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT status FROM holds WHERE hold_id = ?", (hold.hold_id,)
            ).fetchone()["status"],
            "CONVERTED",
            "a confirmed hold becomes CONVERTED so the booking owns the slot",
        )
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT status FROM bookings WHERE hold_ref = ?", (hold.hold_id,)
            ).fetchone()["status"],
            "CONFIRMED",
        )


class TestPreConfirmedBookingKeepsItsSlot(ConcurrencyTestCase):
    """Audit 2026-11-13 (K2): ``PAYMENT_PENDING`` occupies the slot *through its hold*.

    The booking row itself is not in ``OCCUPYING_BOOKING_STATES`` (that stays a pair with
    the schema's partial unique index), so the guarantee rests on the hold: it may not be
    released while a booking still depends on it, and a rival writer must be refused.
    """

    def _reserve(self) -> tuple:
        quote = self.persist_quote()
        hold = self.kernel.request_hold(
            unit_id="unit_1",
            check_in="2026-10-01",
            check_out="2026-10-03",
            guest_ref="guest_abc123",
            policy_version_id="policy_v1",
            idempotency_key="idem_hold_0000",
            quote_ref=quote.quote_id,
            actor="user:owner-1",
            actor_role="owner",
        )
        booking = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_reserve_0001",
            command_id="cmd_reserve_0001",
            adults=2,
            actor="user:owner-1",
            actor_role="reservation_agent",
        )
        self.assertEqual(booking.status, "PAYMENT_PENDING")
        return hold, booking

    def test_releasing_the_hold_of_an_unpaid_booking_is_refused(self):
        hold, booking = self._reserve()
        with self.assertRaises(DomainError) as caught:
            self.kernel.release_hold(hold.hold_id, reason="owner changed their mind", actor="user:owner-1")
        self.assertEqual(caught.exception.code, "HOLD_BACKS_BOOKING")
        self.assertEqual(caught.exception.details["booking_id"], booking.booking_id)
        # The slot is still occupied, so the nights cannot be sold twice.
        self.assertEqual(self.blocking_holds(), 1)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT status FROM holds WHERE hold_id = ?", (hold.hold_id,)
            ).fetchone()["status"],
            "CONFIRMED",
        )

    def test_a_rival_hold_is_refused_while_the_booking_awaits_payment(self):
        hold, _booking = self._reserve()
        with self.assertRaises(DomainError) as caught:
            self.kernel.request_hold(
                unit_id="unit_1",
                check_in="2026-10-02",
                check_out="2026-10-04",
                guest_ref="guest_other01",
                policy_version_id="policy_v1",
                idempotency_key="idem_hold_rival",
                actor="user:owner-1",
                actor_role="owner",
            )
        self.assertEqual(caught.exception.code, "SLOT_ALREADY_HELD")

    def test_two_writers_racing_to_free_and_take_the_slot_both_lose(self):
        """The proof that matters: a release and a rival hold, released simultaneously.

        Writer A tries to free the hold while writer B tries to sell the same nights.
        Both must be refused, because the booking awaiting payment outranks both — this
        is the multi-connection version of the guard, not just the single-connection one.
        """
        hold, booking = self._reserve()

        def release(kernel: RK.ReservationKernel, index: int):
            return kernel.release_hold(
                hold.hold_id, reason="room taken offline", actor="user:owner-1"
            )

        outcomes = self.race(2, [release, self.hold_body(check_in="2026-10-02", check_out="2026-10-04")])
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]

        self.assertEqual(wins, [], "neither freeing nor reselling the nights may succeed")
        self.assertEqual(len(losses), 2)
        self.assertEqual({e.code for e in losses}, {"HOLD_BACKS_BOOKING", "SLOT_ALREADY_HELD"})
        # One hold, one booking, one occupied slot: nothing was created to paper over it.
        self.assertEqual(self.row_count("holds"), 1)
        self.assertEqual(self.row_count("bookings"), 1)
        self.assertEqual(self.blocking_holds(), 1)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT status FROM bookings WHERE booking_id = ?", (booking.booking_id,)
            ).fetchone()["status"],
            "PAYMENT_PENDING",
            "the unpaid booking must survive the race untouched",
        )

    def test_cancelling_the_booking_asks_for_the_hold_to_be_released(self):
        """A cancellation stops depending on the hold, so the release may proceed.

        It is a *request* (``RELEASE_REQUESTED``), not a release: the slot is freed only
        after the supplier's release is confirmed, and until then the hold keeps occupying.
        """
        hold, booking = self._reserve()
        self.kernel.cancel_booking(
            booking_id=booking.booking_id,
            reason="guest withdrew before paying",
            command_id="cmd_cancel_0001",
            actor="user:owner-1",
            actor_role="owner",
        )
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT status FROM holds WHERE hold_id = ?", (hold.hold_id,)
            ).fetchone()["status"],
            "RELEASE_REQUESTED",
        )
        self.assertEqual(self.blocking_holds(), 1, "an unverified release still occupies the slot")
        # And now the release is no longer blocked by a booking.
        self.kernel.release_hold(hold.hold_id, reason="supplier confirmed", actor="user:owner-1")
        self.assertEqual(self.blocking_holds(), 0)


# --------------------------------------------------------------------------- #
# PACK-06 — SQLite reservation concurrency safety (scenarios A–J)
#
# Every assertion below reads the *database* after the race. An exception that says
# "refused" is not evidence that nothing was written, so each test also checks row
# counts, the occupancy invariant, and that no money table moved.
# --------------------------------------------------------------------------- #
class Pack06Case(ConcurrencyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.money_before = self.money_snapshot()

    def split(self, outcomes):
        wins = [r for r, e in outcomes if e is None]
        losses = [e for r, e in outcomes if e is not None]
        return wins, losses

    def assert_only_domain_losses(self, losses, codes):
        """A loser must lose through a domain refusal — never a raw SQLite error."""
        for error in losses:
            self.assertIsInstance(error, DomainError, f"non-domain failure leaked: {error!r}")
            self.assertIn(error.code, codes, error.message)

    def assert_no_side_effects_on_money_or_tenant(self):
        self.assertEqual(self.money_snapshot(), self.money_before, "a race touched a money table")
        for table in ("holds", "bookings", "availability_evidence", "audit_events", "idempotency_keys"):
            columns = {r["name"] for r in self.kernel.conn.execute(f"PRAGMA table_info({table})")}
            if "agency_id" not in columns:
                continue
            agencies = {
                r["agency_id"]
                for r in self.kernel.conn.execute(f"SELECT DISTINCT agency_id FROM {table}")
            }
            self.assertLessEqual(agencies, {self.kernel.agency_id}, f"foreign tenant rows in {table}")

    def confirm_body(self, booking_id, proof, command_id):
        def body(kernel: RK.ReservationKernel, index: int):
            return kernel.confirm_booking(
                booking_id=booking_id, block_evidence_id=proof, command_id=command_id,
                actor="user:owner-1", actor_role="owner",
            )

        return body


class TestPartialOverlapRaces(Pack06Case):
    """A — the case the unique index cannot see: ``uq_holds_active_slot`` keys on
    ``(unit_id, check_in)`` only, so windows with different check-in dates are
    guarded by the kernel's overlap query alone."""

    def test_partial_overlap_pair_has_one_winner_and_a_clean_database(self):
        outcomes = self.race(
            2,
            [
                self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
                self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
            ],
        )
        wins, losses = self.split(outcomes)
        self.assertEqual((len(wins), len(losses)), (1, 1))
        self.assert_only_domain_losses(losses, {"SLOT_ALREADY_HELD"})
        self.assert_invariant()
        self.assertEqual(self.row_count("holds"), 1)
        self.assertEqual(self.row_count("availability_evidence"), 1, "loser left evidence behind")
        self.assertEqual(self.row_count("audit_events"), 1, "loser left an audit row behind")
        self.assert_no_side_effects_on_money_or_tenant()

    def test_partial_overlap_pair_repeated_never_yields_two_winners(self):
        """One lucky interleaving proves little; ten distinct pairs of windows do more."""
        start = date(2026, 1, 1)
        for round_no in range(10):
            with self.subTest(round=round_no):
                base = start + timedelta(days=round_no * 10)
                first = (base, base + timedelta(days=2))
                second = (base + timedelta(days=1), base + timedelta(days=3))
                outcomes = self.race(
                    2,
                    [
                        self.hold_body(check_in=first[0].isoformat(), check_out=first[1].isoformat(), key_tag=f"r{round_no}"),
                        self.hold_body(check_in=second[0].isoformat(), check_out=second[1].isoformat(), key_tag=f"r{round_no}"),
                    ],
                )
                wins, losses = self.split(outcomes)
                self.assertEqual(len(wins), 1)
                self.assert_only_domain_losses(losses, {"SLOT_ALREADY_HELD"})
        self.assert_invariant()
        self.assertEqual(self.row_count("holds"), 10)

    def test_a_staircase_of_six_overlapping_windows_never_double_books(self):
        """Windows [d, d+2) for six consecutive d: at most three can coexist (every other
        one). Whatever subset wins, no two winners may intersect."""
        bodies = []
        for day in range(1, 7):
            bodies.append(
                self.hold_body(check_in=f"2026-10-{day:02d}", check_out=f"2026-10-{day + 2:02d}")
            )
        wins, losses = self.split(self.race(6, bodies))
        self.assertGreaterEqual(len(wins), 1)
        self.assertLessEqual(len(wins), 3, "more winners than non-overlapping windows exist")
        self.assert_only_domain_losses(losses, {"SLOT_ALREADY_HELD"})
        self.assert_invariant()
        self.assertEqual(self.row_count("holds"), len(wins))
        self.assertEqual(self.row_count("audit_events"), len(wins))
        self.assert_no_side_effects_on_money_or_tenant()


class TestHoldVersusBookingRaces(Pack06Case):
    """B and I — a hold racing a booking, and every status the baseline calls occupying."""

    def test_a_hold_racing_the_confirmation_of_an_overlapping_booking_loses(self):
        _hold, _quote, reserved = self.reserve_paid(
            check_in="2026-10-01", check_out="2026-10-03", tag="b1"
        )
        proof = booking_support.block_proof(self.kernel, "unit_1")
        outcomes = self.race(
            2,
            [
                self.confirm_body(reserved.booking_id, proof, "cmd_confirm_b1"),
                self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
            ],
        )
        wins, losses = self.split(outcomes)
        self.assertEqual(len(wins), 1, f"expected the confirmation alone to win, got {outcomes}")
        self.assertEqual(wins[0].status, "CONFIRMED")
        self.assert_only_domain_losses(losses, {"SLOT_ALREADY_HELD", "SLOT_ALREADY_BOOKED"})
        self.assertEqual(len(losses), 1)
        self.assert_invariant()
        self.assertEqual(self.row_count("holds"), 1, "the refused hold left a row behind")
        self.assertEqual(self.blocking_holds(), 0)
        self.assertEqual(
            self.kernel.get_booking(reserved.booking_id)["status"], "CONFIRMED"
        )

    def test_holds_racing_a_confirmed_booking_all_lose(self):
        _hold, _quote, reserved = self.reserve_paid(
            check_in="2026-10-01", check_out="2026-10-03", tag="b2"
        )
        self.kernel.confirm_booking(
            booking_id=reserved.booking_id,
            block_evidence_id=booking_support.block_proof(self.kernel, "unit_1"),
            command_id="cmd_confirm_b2", actor="user:owner-1", actor_role="owner",
        )
        holds_before = self.row_count("holds")
        wins, losses = self.split(
            self.race(
                3,
                [
                    self.hold_body(check_in="2026-09-30", check_out="2026-10-02"),
                    self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
                    self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
                ],
            )
        )
        self.assertEqual(wins, [])
        self.assert_only_domain_losses(losses, {"SLOT_ALREADY_BOOKED"})
        self.assertEqual(self.row_count("holds"), holds_before)
        self.assert_invariant()

    # -- I: the occupying vocabulary, read from the schema, not invented here ---- #
    @staticmethod
    def _schema_text() -> str:
        return (Path(RK.__file__).parent / "schema" / "001_initial.sql").read_text()

    @classmethod
    def _index_states(cls, index_name: str) -> set:
        match = re.search(rf"{index_name}\b.*?WHERE status IN \(([^)]*)\)", cls._schema_text(), re.DOTALL)
        assert match, f"{index_name} not found in the schema"
        return set(re.findall(r"'([A-Z_]+)'", match.group(1)))

    @classmethod
    def _booking_status_domain(cls) -> set:
        table = re.search(r"CREATE TABLE bookings \(.*?\n\);", cls._schema_text(), re.DOTALL)
        assert table, "bookings table not found in the schema"
        check = re.search(r"status\s+TEXT\s+NOT NULL CHECK \(status IN \((.*?)\)\)", table.group(0), re.DOTALL)
        assert check, "bookings.status CHECK not found in the schema"
        return set(re.findall(r"'([A-Z_]+)'", check.group(1)))

    def test_the_kernels_occupying_sets_match_the_schemas_partial_indexes(self):
        self.assertEqual(self._index_states("uq_holds_active_slot"), set(RK.OCCUPYING_HOLD_STATES))
        self.assertEqual(self._index_states("uq_bookings_active_slot"), set(RK.OCCUPYING_BOOKING_STATES))
        self.assertEqual(set(self.BLOCKING), set(RK.OCCUPYING_HOLD_STATES))

    def test_every_occupying_booking_status_refuses_racing_overlapping_holds(self):
        base = date(2026, 1, 5)
        for i, status in enumerate(RK.OCCUPYING_BOOKING_STATES):
            with self.subTest(status=status):
                start = base + timedelta(days=i * 10)
                self.raw_booking(
                    f"book_occ_{i}", "unit_1", start.isoformat(),
                    (start + timedelta(days=2)).isoformat(), status,
                )
                before = self.row_count("holds")
                wins, losses = self.split(
                    self.race(
                        2,
                        [
                            self.hold_body(
                                check_in=(start + timedelta(days=1)).isoformat(),
                                check_out=(start + timedelta(days=3)).isoformat(),
                                key_tag=f"o{i}",
                            ),
                            self.hold_body(
                                check_in=(start - timedelta(days=1)).isoformat(),
                                check_out=(start + timedelta(days=1)).isoformat(),
                                key_tag=f"o{i}",
                            ),
                        ],
                    )
                )
                self.assertEqual(wins, [], f"{status} did not hold its slot")
                self.assert_only_domain_losses(losses, {"SLOT_ALREADY_BOOKED"})
                self.assertEqual(self.row_count("holds"), before)
        self.assert_invariant()

    def test_a_booking_status_that_does_not_occupy_lets_exactly_one_racer_through(self):
        occupying = set(RK.OCCUPYING_BOOKING_STATES)
        free = sorted(self._booking_status_domain() - occupying)
        self.assertGreater(len(free), 15, "schema parse looks wrong")
        base = date(2026, 1, 5)
        for i, status in enumerate(free):
            with self.subTest(status=status):
                start = base + timedelta(days=i * 10)
                self.raw_booking(
                    f"book_free_{i}", "unit_1", start.isoformat(),
                    (start + timedelta(days=2)).isoformat(), status,
                )
                wins, losses = self.split(
                    self.race(
                        2,
                        [
                            self.hold_body(
                                check_in=(start + timedelta(days=1)).isoformat(),
                                check_out=(start + timedelta(days=3)).isoformat(),
                                key_tag=f"f{i}",
                            ),
                            self.hold_body(
                                check_in=start.isoformat(),
                                check_out=(start + timedelta(days=2)).isoformat(),
                                key_tag=f"f{i}",
                            ),
                        ],
                    )
                )
                self.assertEqual(len(wins), 1, f"{status} should not block the slot")
                self.assert_only_domain_losses(losses, {"SLOT_ALREADY_HELD"})
        self.assert_invariant()


class TestConfirmationVillasAndDuplicates(Pack06Case):
    """C, D, E, F at the booking level."""

    def test_two_writers_confirming_one_booking_confirm_it_once(self):  # C
        _h, _q, reserved = self.reserve_paid(check_in="2026-10-01", check_out="2026-10-03", tag="c1")
        proof = booking_support.block_proof(self.kernel, "unit_1")
        body = self.confirm_body(reserved.booking_id, proof, "cmd_confirm_c1")
        wins, losses = self.split(self.race(2, [body, body]))
        self.assertEqual(losses, [], f"the loser of a double confirm must replay, not fail: {losses}")
        self.assertEqual([w.status for w in wins], ["CONFIRMED", "CONFIRMED"])
        self.assertEqual(sum(1 for w in wins if w.replayed), 1, "exactly one call is a replay")
        events = [
            e for e in self.kernel.state_events(reserved.booking_id) if e["to_state"] == "CONFIRMED"
        ]
        self.assertEqual(len(events), 1, "the confirmation was journalled twice")
        audits = [
            a for a in self.kernel.audit_trail(entity_id=reserved.booking_id)
            if a["action"] == "booking.confirmed"
        ]
        self.assertEqual(len(audits), 1)
        self.assertTrue(self.kernel.verify_audit_chain())
        self.assert_invariant()

    def test_two_villas_confirming_at_once_both_win(self):  # D
        _h1, _q1, r1 = self.reserve_paid(check_in="2026-10-01", check_out="2026-10-03", tag="d1")
        _h2, _q2, r2 = self.reserve_paid(
            check_in="2026-10-01", check_out="2026-10-03", tag="d2", unit_id="unit_2"
        )
        p1 = booking_support.block_proof(self.kernel, "unit_1")
        p2 = booking_support.block_proof(self.kernel, "unit_2")
        wins, losses = self.split(
            self.race(
                2,
                [
                    self.confirm_body(r1.booking_id, p1, "cmd_confirm_d1"),
                    self.confirm_body(r2.booking_id, p2, "cmd_confirm_d2"),
                ],
            )
        )
        self.assertEqual(losses, [], f"different villas must not block each other: {losses}")
        self.assertEqual({w.unit_id for w in wins}, {"unit_1", "unit_2"})
        self.assert_invariant("unit_1", "unit_2")
        self.assertEqual(self.row_count("bookings"), 2)

    def test_back_to_back_booking_and_hold_both_win(self):  # E
        _h, _q, reserved = self.reserve_paid(check_in="2026-10-01", check_out="2026-10-03", tag="e1")
        proof = booking_support.block_proof(self.kernel, "unit_1")
        wins, losses = self.split(
            self.race(
                2,
                [
                    self.confirm_body(reserved.booking_id, proof, "cmd_confirm_e1"),
                    self.hold_body(check_in="2026-10-03", check_out="2026-10-05"),
                ],
            )
        )
        self.assertEqual(losses, [], f"check-out day is a legal check-in: {losses}")
        self.assertEqual(len(wins), 2)
        self.assert_invariant()

    def _hold_and_quote(self, tag):
        quote = self.persist_quote(quote_id=f"quote_{tag}")
        hold = self.kernel.request_hold(
            unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
            guest_ref="guest_abc123", policy_version_id="policy_v1",
            idempotency_key=f"idem_hold_{tag}", quote_ref=quote.quote_id,
            actor="user:owner-1", actor_role="owner",
        )
        return hold, quote

    def _reserve_body(self, hold, quote, key, command_id):
        def body(kernel: RK.ReservationKernel, index: int):
            return kernel.reserve_from_hold(
                hold_id=hold.hold_id, quote_id=quote.quote_id, idempotency_key=key,
                command_id=command_id, adults=2,
            )

        return body

    def test_the_same_idempotency_key_reserving_twice_creates_one_booking(self):  # F
        hold, quote = self._hold_and_quote("f1")
        body = self._reserve_body(hold, quote, "idem_book_f1", "cmd_book_f1")
        wins, losses = self.split(self.race(2, [body, body]))
        self.assertEqual(losses, [], f"an idempotent retry must replay, not fail: {losses}")
        self.assertEqual(len({w.booking_id for w in wins}), 1)
        self.assertEqual(sum(1 for w in wins if w.replayed), 1)
        self.assertEqual(self.row_count("bookings"), 1)
        self.assertEqual(
            len(self.kernel.state_events(wins[0].booking_id)), 1, "state journalled twice"
        )

    def test_two_different_keys_reserving_one_hold_create_one_booking(self):  # F / duplicate op
        hold, quote = self._hold_and_quote("f2")
        wins, losses = self.split(
            self.race(
                2,
                [
                    self._reserve_body(hold, quote, "idem_book_f2a", "cmd_book_f2a"),
                    self._reserve_body(hold, quote, "idem_book_f2b", "cmd_book_f2b"),
                ],
            )
        )
        # The second caller is answered with the booking the first one made (the kernel
        # looks the hold up inside its own write transaction); it must never mint another.
        self.assertEqual(losses, [], f"a duplicate reserve should replay, not fail: {losses}")
        self.assertEqual(self.row_count("bookings"), 1, f"duplicate booking on one hold: {wins}")
        self.assertEqual(len({w.booking_id for w in wins}), 1)
        self.assertEqual(sum(1 for w in wins if w.replayed), 1)
        self.assertEqual(len(self.kernel.state_events(wins[0].booking_id)), 1)
        self.assert_invariant()

    def test_one_key_reused_for_different_windows_is_refused_not_merged(self):  # F
        def body(window):
            def run(kernel: RK.ReservationKernel, index: int):
                return kernel.request_hold(
                    unit_id="unit_1", check_in=window[0], check_out=window[1],
                    guest_ref="guest_abc123", policy_version_id="policy_v1",
                    idempotency_key="idem_hold_shared", actor="user:owner-1", actor_role="owner",
                )

            return run

        wins, losses = self.split(
            self.race(2, [body(("2026-10-01", "2026-10-03")), body(("2026-11-01", "2026-11-03"))])
        )
        self.assertEqual(len(wins), 1)
        self.assert_only_domain_losses(losses, {"IDEMPOTENCY_CONFLICT"})
        self.assertEqual(self.row_count("holds"), 1)


class FailingAuditKernel(RK.ReservationKernel):
    """Fails *after* the hold row is written, to prove the rollback takes it back.

    ``entered`` is set the moment the hold row exists inside the open write
    transaction; ``linger`` keeps that transaction (and SQLite's write lock) open a
    moment longer so a second writer is genuinely blocked by it before it rolls back.
    """

    rows_visible_before_failure = 0
    linger = 0.0
    entered: threading.Event | None = None

    def _audit(self, conn, *args, **kwargs):
        if kwargs.get("action") == "hold.requested" or (args and args[0] == "hold.requested"):
            seen = conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"]
            if seen:
                self.rows_visible_before_failure = seen
                if self.entered is not None:
                    self.entered.set()
                time.sleep(self.linger)
                raise RuntimeError("injected failure after the hold was written")
        return super()._audit(conn, *args, **kwargs)


class TestRollbackAndBusyHandling(Pack06Case):
    """G and H."""

    def _failing_kernel(self, seed: str) -> FailingAuditKernel:
        return FailingAuditKernel.open(
            self.db_path, clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed=seed)
        )

    def _doomed_hold(self, kernel, key):
        return kernel.request_hold(
            unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
            guest_ref="guest_abc123", policy_version_id="policy_v1",
            idempotency_key=key, actor="user:owner-1", actor_role="owner",
        )

    def test_a_writer_that_fails_mid_transaction_leaves_no_partial_commit(self):  # G
        failing = self._failing_kernel("failing")
        try:
            with self.assertRaises(RuntimeError):
                self._doomed_hold(failing, "idem_hold_g1")
            self.assertGreaterEqual(
                failing.rows_visible_before_failure, 1,
                "the failure fired before any write — this test would prove nothing",
            )
            self.assertFalse(failing._in_tx, "the kernel is still inside a dead transaction")
        finally:
            failing.close()
        for table in ("holds", "availability_evidence", "audit_events", "idempotency_keys"):
            self.assertEqual(self.row_count(table), 0, f"{table} kept a rolled-back row")
        self.assertEqual(self.blocking_holds(), 0)

    def test_a_racer_blocked_by_a_doomed_writer_wins_the_slot_after_the_rollback(self):  # G
        failing = self._failing_kernel("failing2")
        failing.linger = 0.4
        failing.entered = threading.Event()
        ready = threading.Event()
        result: dict = {}

        def healthy_thread():
            kernel = _open_writer(self.db_path, 7)
            try:
                kernel.conn.execute("PRAGMA busy_timeout = 10000")
                ready.set()
                self.assertTrue(failing.entered.wait(timeout=30))  # doomed now holds the write lock
                begun = time.monotonic()
                result["healthy"] = self.hold_body(check_in="2026-10-02", check_out="2026-10-04")(kernel, 7)
                result["waited"] = time.monotonic() - begun
            except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
                result["healthy"] = exc
            finally:
                kernel.close()

        thread = threading.Thread(target=healthy_thread, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=30))
        try:
            with self.assertRaises(RuntimeError):
                self._doomed_hold(failing, "idem_hold_g2a")
        finally:
            failing.close()
        thread.join(timeout=60)
        healthy = result["healthy"]
        self.assertNotIsInstance(healthy, BaseException, f"the blocked racer must succeed: {healthy!r}")
        self.assertGreaterEqual(result["waited"], 0.2, "the racer was never actually blocked by the doomed writer")
        self.assertEqual(self.row_count("holds"), 1)
        self.assertEqual(self.row_count("audit_events"), 1)
        self.assert_invariant()
        # The doomed key was rolled back with everything else: a retry is a fresh attempt
        # that loses honestly to the racer's hold, not a phantom replay of a ghost.
        with self.assertRaises(DomainError) as ctx:
            self._doomed_hold(self.kernel, "idem_hold_g2a")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def _lock_the_file(self) -> sqlite3.Connection:
        """Hold SQLite's write lock from a separate connection (opened *after* the
        kernel, which writes during ``open``)."""
        blocker = sqlite3.connect(self.db_path, isolation_level=None, timeout=5, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        return blocker

    def test_a_locked_database_times_out_cleanly_and_writes_nothing(self):  # H
        kernel = _open_writer(self.db_path, 1)
        self.addCleanup(kernel.close)
        kernel.conn.execute("PRAGMA busy_timeout = 200")
        blocker = self._lock_the_file()
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            self.hold_body()(kernel, 1)
        elapsed = time.monotonic() - started
        self.assertIn("locked", str(ctx.exception).lower())
        self.assertGreaterEqual(elapsed, 0.15, "it must wait for the timeout, not fail at once")
        self.assertLess(elapsed, 5.0)
        self.assertFalse(kernel._in_tx, "a timed-out BEGIN must not poison the kernel")
        blocker.execute("ROLLBACK")
        self.assertEqual(self.row_count("holds"), 0)
        self.assertEqual(self.row_count("idempotency_keys"), 0)
        # The same request, same key, now succeeds: nothing half-recorded survived.
        outcome = self.hold_body()(kernel, 1)
        self.assertFalse(outcome.replayed)
        self.assertEqual(self.row_count("holds"), 1)
        self.assert_invariant()

    def test_a_brief_lock_is_waited_out_and_the_request_still_succeeds(self):  # H
        kernel = _open_writer(self.db_path, 2)
        self.addCleanup(kernel.close)
        kernel.conn.execute("PRAGMA busy_timeout = 5000")
        blocker = self._lock_the_file()
        timer = threading.Timer(0.3, lambda: blocker.execute("ROLLBACK"))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        outcome = self.hold_body()(kernel, 2)
        waited = time.monotonic() - started
        timer.join(timeout=5)
        self.assertGreaterEqual(waited, 0.2, "the request was never actually blocked")
        self.assertEqual(outcome.status, "CONFIRMED")
        self.assertEqual(self.row_count("holds"), 1)
        self.assert_invariant()


class TestNegativeControl(Pack06Case):
    """J — prove the harness and the invariant check can see a violation."""

    def test_the_invariant_check_flags_a_double_booking_when_the_guard_is_removed(self):
        outcomes = self.race(
            2,
            [
                self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
                self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
            ],
            opener=_open_unguarded,
        )
        wins, losses = self.split(outcomes)
        self.assertEqual(losses, [], f"the unguarded kernel should let both through: {losses}")
        self.assertEqual(len(wins), 2)
        pairs = self.overlapping_pairs()
        self.assertEqual(len(pairs), 1, "the invariant check failed to see a real double booking")
        with self.assertRaises(AssertionError):
            self.assert_invariant()

    def test_the_same_race_on_the_real_kernel_is_clean(self):
        """Paired with the test above: identical workload, only the guard differs."""
        outcomes = self.race(
            2,
            [
                self.hold_body(check_in="2026-10-01", check_out="2026-10-03"),
                self.hold_body(check_in="2026-10-02", check_out="2026-10-04"),
            ],
        )
        self.assertEqual(len(self.split(outcomes)[0]), 1)
        self.assertEqual(self.overlapping_pairs(), [])

    def test_the_writers_really_run_at_the_same_time(self):
        """Wall-clock proof: the busy intervals of the two bodies must intersect."""
        spans: list = [None, None]

        def timed(index):
            inner = self.hold_body(
                check_in=f"2026-10-0{index + 1}", check_out=f"2026-10-0{index + 3}"
            )

            def body(kernel, i):
                begun = time.monotonic()
                try:
                    return inner(kernel, i)
                finally:
                    spans[index] = (begun, time.monotonic())

            return body

        self.race(2, [timed(0), timed(1)])
        (a0, a1), (b0, b1) = spans
        self.assertLess(max(a0, b0), min(a1, b1), "the two writers never overlapped in time")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

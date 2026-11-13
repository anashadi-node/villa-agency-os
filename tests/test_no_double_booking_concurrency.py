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

import tempfile
import threading
import unittest
from pathlib import Path

import reservation_kernel as RK
import booking_support
from primitives import DomainError, FrozenClock, IdFactory
from test_reservation_kernel import NOW, KernelTestCase


def _open_writer(db_path: str, index: int) -> RK.ReservationKernel:
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
    def race(self, writers: int, bodies) -> list:
        """Run ``bodies[i]`` in its own thread released by one barrier.

        Returns one slot per writer holding either the result or the exception. The
        barrier is what makes the calls simultaneous: without it the operating system
        could serialise them and the test would prove nothing.
        """
        barrier = threading.Barrier(writers)
        results: list = [None] * writers
        errors: list = [None] * writers

        def run(index: int) -> None:
            kernel = _open_writer(self.db_path, index)
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

    def hold_body(self, unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03"):
        def body(kernel: RK.ReservationKernel, index: int):
            return kernel.request_hold(
                unit_id=unit_id,
                check_in=check_in,
                check_out=check_out,
                guest_ref="guest_abc123",
                policy_version_id="policy_v1",
                idempotency_key=f"idem_hold_{index:04d}",
                actor="user:owner-1",
                actor_role="owner",
            )

        return body


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

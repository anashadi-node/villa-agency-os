"""The stay itself: ``CONFIRMED → PRE_ARRIVAL → IN_STAY → COMPLETED``.

Why this file exists
--------------------
``close_booking`` (§30) refuses until the booking is ``COMPLETED``, and the closure gate is
the last step of every booking. Until these commands existed, **nothing in the repository
moved a booking past ``CONFIRMED``**: ``cancel_booking`` and ``close_booking`` were the only
writers of ``bookings.status``, so the gate was implemented, tested — and unreachable. The
one test that reached a completed booking did it with a raw ``UPDATE``, which is the shape of
a missing command rather than a fixture quirk.

So these tests pin the three steps, the refusals around them, and the one property that
matters most: the new commands do **not** open §30. A stay that completes is still not a
booking a machine may close without the facts.
"""

from __future__ import annotations

import unittest

import reservation_kernel as RK
from primitives import DomainError
from test_reservation_kernel import KernelTestCase

FRONT_DESK = "user:front_desk"
FRONT_DESK_ROLE = "staff"


class StayProgressTestCase(KernelTestCase):
    """A confirmed booking, ready for the stay to happen."""

    def setUp(self):
        super().setUp()
        _, self.booking = self.confirm()
        self.booking_id = self.booking.booking_id

    def confirm(self, **overrides):
        """Confirm one booking — the same two commands the confirmation tests use."""
        quote = overrides.pop("quote", None) or self.persist_quote()
        hold = overrides.pop("hold", None) or self.hold(quote_ref=quote.quote_id)
        params = dict(
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key="idem_book_0001",
            command_id="cmd_0001",
            adults=2,
        )
        params.update(overrides)
        return quote, self.kernel.confirm_from_hold(**params)

    # -- helpers ------------------------------------------------------------ #

    def status(self) -> str:
        return str(self.kernel.get_booking(self.booking_id)["status"])

    def walk(self) -> None:
        """Take the booking all the way to ``COMPLETED`` the way the desk would."""
        self.kernel.prepare_arrival(
            booking_id=self.booking_id, command_id="cmd_stay_prep_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.kernel.check_in(
            booking_id=self.booking_id, command_id="cmd_stay_in_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.kernel.complete_stay(
            booking_id=self.booking_id, command_id="cmd_stay_done_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )

    def transitions(self) -> list[tuple[str, str, str]]:
        return [
            (event["from_state"], event["to_state"], event["command"])
            for event in self.kernel.state_events(self.booking_id)
        ]


class TestTheThreeSteps(StayProgressTestCase):
    def test_the_desk_walks_a_confirmed_booking_to_completed(self):
        self.assertEqual(self.status(), "CONFIRMED")
        self.kernel.prepare_arrival(
            booking_id=self.booking_id, command_id="cmd_stay_prep_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.assertEqual(self.status(), "PRE_ARRIVAL")
        self.kernel.check_in(
            booking_id=self.booking_id, command_id="cmd_stay_in_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.assertEqual(self.status(), "IN_STAY")
        self.kernel.complete_stay(
            booking_id=self.booking_id, command_id="cmd_stay_done_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.assertEqual(self.status(), "COMPLETED")

    def test_every_step_is_journalled_with_the_command_that_did_it(self):
        self.walk()
        self.assertEqual(
            self.transitions(),
            [
                ("HOLD_ACTIVE", "CONFIRMED", "confirm_booking"),
                ("CONFIRMED", "PRE_ARRIVAL", "prepare_arrival"),
                ("PRE_ARRIVAL", "IN_STAY", "check_in"),
                ("IN_STAY", "COMPLETED", "complete_stay"),
            ],
        )

    def test_every_step_is_audited_and_the_chain_verifies(self):
        self.walk()
        actions = [row["action"] for row in self.kernel.audit_trail()]
        self.assertIn("booking.prepare_arrival", actions)
        self.assertIn("booking.check_in", actions)
        self.assertIn("booking.complete_stay", actions)
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_the_state_journal_names_the_person_who_acted(self):
        self.walk()
        last = self.kernel.state_events(self.booking_id)[-1]
        self.assertEqual(last["actor"], FRONT_DESK)
        self.assertEqual(last["actor_role"], FRONT_DESK_ROLE)


class TestTheStepsAreGuarded(StayProgressTestCase):
    def test_a_step_cannot_be_skipped(self):
        """Checking a guest in who has not arrived is refused, and nothing moves."""
        with self.assertRaises(DomainError) as caught:
            self.kernel.check_in(
                booking_id=self.booking_id, command_id="cmd_stay_in_01",
                actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
            )
        self.assertEqual(caught.exception.code, "BOOKING_TRANSITION_REFUSED")
        self.assertEqual(self.status(), "CONFIRMED")

    def test_completing_a_stay_that_never_started_is_refused(self):
        self.kernel.prepare_arrival(
            booking_id=self.booking_id, command_id="cmd_stay_prep_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        with self.assertRaises(DomainError) as caught:
            self.kernel.complete_stay(
                booking_id=self.booking_id, command_id="cmd_stay_done_01",
                actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
            )
        self.assertEqual(caught.exception.code, "BOOKING_TRANSITION_REFUSED")
        self.assertEqual(self.status(), "PRE_ARRIVAL")

    def test_a_short_command_id_is_refused(self):
        with self.assertRaises(DomainError):
            self.kernel.check_in(
                booking_id=self.booking_id, command_id="short",
                actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
            )

    def test_an_unknown_booking_is_refused(self):
        with self.assertRaises(DomainError):
            self.kernel.check_in(
                booking_id="book_does_not_exist", command_id="cmd_stay_in_01",
                actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
            )


class TestReplaysAreNoOps(StayProgressTestCase):
    def test_repeating_a_step_leaves_one_journal_row(self):
        self.kernel.prepare_arrival(
            booking_id=self.booking_id, command_id="cmd_stay_prep_01",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        before = len(self.kernel.state_events(self.booking_id))
        self.kernel.prepare_arrival(
            booking_id=self.booking_id, command_id="cmd_stay_prep_02",
            actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
        )
        self.assertEqual(len(self.kernel.state_events(self.booking_id)), before)
        self.assertEqual(self.status(), "PRE_ARRIVAL")


class TestTheNewCommandsDoNotOpenTheClosureGate(StayProgressTestCase):
    def test_a_completed_stay_is_still_not_closable_without_the_facts(self):
        """The whole point: reaching ``COMPLETED`` is necessary, not sufficient.

        ``close_booking`` runs §30's own verdict, so a stay that finished with no supplier
        settlement, no deposit resolution and no documents recorded stays blocked. A new
        command that could close a booking by moving it far enough would be a hole.
        """
        self.walk()
        with self.assertRaises(DomainError) as caught:
            self.kernel.close_booking(
                booking_id=self.booking_id, command_id="cmd_close_0001",
                reason="stay is over", actor=FRONT_DESK, actor_role=FRONT_DESK_ROLE,
            )
        self.assertEqual(caught.exception.code, "BOOKING_NOT_CLOSABLE")
        self.assertEqual(self.status(), "COMPLETED")

    def test_the_default_gate_carries_the_three_edges(self):
        """The kernel's own fail-closed gate must know the stay, or nothing can pass it."""
        for edge in (
            ("CONFIRMED", "PRE_ARRIVAL", "prepare_arrival"),
            ("PRE_ARRIVAL", "IN_STAY", "check_in"),
            ("IN_STAY", "COMPLETED", "complete_stay"),
        ):
            with self.subTest(edge=edge):
                self.assertIn(edge, RK._KERNEL_TRANSITIONS)


if __name__ == "__main__":
    unittest.main()

"""Audit 2026-11-13, finding K2: ``CONFIRMED`` needs verified money and the villa's block proof.

Before the fix ``confirm_from_hold`` wrote ``CONFIRMED`` with ``amount_paid_minor=0``
and recorded the hold's own availability evidence as the confirmation proof — over a
``HOLD_ACTIVE -> CONFIRMED`` edge the canonical state machine does not have. ADR-0033
listed the tests to write "when the flow is built"; these are they.
"""

from __future__ import annotations

import booking_states as BS
import reservation_kernel as RK
from booking_support import block_proof, pay_booking
from primitives import DomainError
from test_reservation_kernel import KernelTestCase


class TestPaidThenConfirmed(KernelTestCase):
    def reserve(self, key="idem_k2_0001"):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        booking = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id, quote_id=quote.quote_id,
            idempotency_key=key, command_id="cmd_k2_0001", adults=2,
        )
        return quote, hold, booking

    def code(self, call, **kwargs):
        with self.assertRaises(DomainError) as ctx:
            call(**kwargs)
        return ctx.exception.code

    # -- the old door is shut ------------------------------------------------ #
    def test_confirm_from_hold_is_retired(self):
        quote = self.persist_quote()
        hold = self.hold(quote_ref=quote.quote_id)
        self.assertEqual(
            self.code(self.kernel.confirm_from_hold, hold_id=hold.hold_id, quote_id=quote.quote_id,
                      idempotency_key="idem_old_0001", command_id="cmd_old_0001", adults=2),
            "CONFIRM_REQUIRES_PAYMENT_AND_BLOCK_PROOF",
        )
        self.assertEqual(self.row_count("bookings"), 0)

    def test_there_is_no_hold_to_confirmed_edge_anywhere(self):
        self.assertFalse(RK._default_transition_gate("HOLD_ACTIVE", "CONFIRMED", "confirm_booking"))
        self.assertFalse(BS.can_transition("HOLD_ACTIVE", "CONFIRMED"))
        # Every edge the payment/confirmation path uses exists in the canonical graph.
        # (The kernel's direct ``* -> CANCELLED`` edges from CONFIRMED and later states do
        # not; that pre-existing gap is reported separately, not changed here.)
        for frm, to, cmd in RK._KERNEL_TRANSITIONS:
            if cmd not in ("reserve_booking", "mark_paid", "confirm_booking"):
                continue
            with self.subTest(edge=(frm, to, cmd)):
                self.assertTrue(BS.can_transition(frm, to), f"kernel edge {frm}->{to} not in booking_states")

    # -- reservation keeps the slot without claiming it is confirmed ------- #
    def test_reservation_awaits_payment_and_the_hold_keeps_the_slot(self):
        _, hold, booking = self.reserve()
        self.assertEqual(booking.status, "PAYMENT_PENDING")
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], RK.HOLD_CONFIRMED)
        self.assertIsNone(self.kernel.get_booking(booking.booking_id)["confirmed_at"])
        with self.assertRaises(DomainError) as ctx:
            self.hold(check_in="2026-10-02", check_out="2026-10-04", key="idem_hold_0009")
        self.assertEqual(ctx.exception.code, "SLOT_ALREADY_HELD")

    def test_confirmation_before_payment_is_refused(self):
        _, _, booking = self.reserve()
        proof = block_proof(self.kernel, "unit_1")
        self.assertEqual(
            self.code(self.kernel.confirm_booking, booking_id=booking.booking_id,
                      block_evidence_id=proof, command_id="cmd_k2_conf"),
            "PAYMENT_REQUIRED",
        )

    # -- the money must be verified, and enough --------------------------- #
    def test_a_payment_of_another_booking_cannot_pay_this_one(self):
        _, _, first = self.reserve()
        quote2 = self.persist_quote(unit_id="unit_2", quote_id="quote_u2")
        hold2 = self.hold(unit_id="unit_2", key="idem_hold_u2", quote_ref=quote2.quote_id)
        second = self.kernel.reserve_from_hold(
            hold_id=hold2.hold_id, quote_id=quote2.quote_id,
            idempotency_key="idem_k2_u2", command_id="cmd_k2_u2", adults=2,
        )
        other_payment = pay_booking(self.kernel, second.booking_id)
        self.assertEqual(
            self.code(self.kernel.mark_paid, booking_id=first.booking_id,
                      payment_id=other_payment, command_id="cmd_k2_paid"),
            "PAYMENT_NOT_FOR_BOOKING",
        )

    def test_an_unsettled_payment_is_not_money(self):
        import invoicing as INV
        import payments as PAY

        _, _, booking = self.reserve()
        row = self.kernel.get_booking(booking.booking_id)
        invoice = INV.InvoiceService(self.kernel.conn, clock=self.kernel.clock, ids=self.kernel.ids).issue(
            booking_id=booking.booking_id, currency="IDR", policy_fingerprint=row["policy_fingerprint"],
            lines=[{"kind": "NIGHTLY", "description": "Stay", "quantity": 1,
                    "unit_amount_minor": row["total_minor"]}],
            idempotency_key="inv_k2_pending",
        )
        intent = PAY.PaymentService(self.kernel.conn, clock=self.kernel.clock, ids=self.kernel.ids).create_intent(
            invoice_id=invoice.invoice_id, amount_minor=row["total_minor"], currency="IDR",
            idempotency_key="intent_k2_pending", created_by="user:finance-1",
        )
        self.assertEqual(
            self.code(self.kernel.mark_paid, booking_id=booking.booking_id,
                      payment_id=str(intent.payment_id), command_id="cmd_k2_paid"),
            "PAYMENT_NOT_VERIFIED",
        )

    def test_a_payment_below_the_deposit_does_not_move_the_booking(self):
        _, _, booking = self.reserve()
        small = pay_booking(self.kernel, booking.booking_id, amount_minor=1_000, key="k2_small")
        self.assertEqual(
            self.code(self.kernel.mark_paid, booking_id=booking.booking_id,
                      payment_id=small, command_id="cmd_k2_paid"),
            "PAYMENT_BELOW_DEPOSIT",
        )
        self.assertEqual(self.kernel.get_booking(booking.booking_id)["status"], "PAYMENT_PENDING")

    def test_a_deposit_is_enough_to_confirm(self):
        _, hold, booking = self.reserve()
        deposit = booking.deposit_due_minor
        payment = pay_booking(self.kernel, booking.booking_id, amount_minor=deposit, key="k2_dep")
        paid = self.kernel.mark_paid(booking_id=booking.booking_id, payment_id=payment, command_id="cmd_k2_paid")
        self.assertEqual(paid.status, "DEPOSIT_PAID")
        confirmed = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_evidence_id=block_proof(self.kernel, "unit_1"),
            command_id="cmd_k2_conf",
        )
        self.assertEqual(confirmed.status, "CONFIRMED")
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], RK.HOLD_CONVERTED)
        again = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_evidence_id=block_proof(self.kernel, "unit_1"),
            command_id="cmd_k2_conf2",
        )
        self.assertTrue(again.replayed)

    # -- the block proof must be a real, later, authoritative answer -------- #
    def paid(self):
        _, _, booking = self.reserve()
        payment = pay_booking(self.kernel, booking.booking_id)
        self.kernel.mark_paid(booking_id=booking.booking_id, payment_id=payment, command_id="cmd_k2_paid")
        return booking

    def test_proof_for_another_unit_is_refused(self):
        booking = self.paid()
        self.assertEqual(
            self.code(self.kernel.confirm_booking, booking_id=booking.booking_id,
                      block_evidence_id=block_proof(self.kernel, "unit_2"), command_id="cmd_k2_conf"),
            "BLOCK_PROOF_REQUIRED",
        )

    def test_proof_that_does_not_authorise_confirmation_is_refused(self):
        booking = self.paid()
        weak = self.kernel.record_evidence(
            unit_id="unit_1", source_type="manager_written", evidence_level="B",
            result="available", can_confirm=False,
        )
        self.assertEqual(
            self.code(self.kernel.confirm_booking, booking_id=booking.booking_id,
                      block_evidence_id=weak, command_id="cmd_k2_conf"),
            "BLOCK_PROOF_NOT_AUTHORITATIVE",
        )

    def test_proof_older_than_the_reservation_is_refused(self):
        early = block_proof(self.kernel, "unit_1")
        self.kernel.clock.set("2026-09-21T01:00:00+00:00")
        booking = self.paid()
        self.assertEqual(
            self.code(self.kernel.confirm_booking, booking_id=booking.booking_id,
                      block_evidence_id=early, command_id="cmd_k2_conf"),
            "BLOCK_PROOF_PREDATES_BOOKING",
        )

    # -- cancelling before confirmation asks the villa to release ---------- #
    def test_cancel_before_confirmation_requests_the_release(self):
        _, hold, booking = self.reserve()
        self.kernel.cancel_booking(booking_id=booking.booking_id, reason="guest withdrew", command_id="cmd_k2_cxl")
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], RK.HOLD_RELEASE_REQUESTED)
        with self.assertRaises(DomainError):  # still occupied until the villa confirms the release
            self.hold(check_in="2026-10-01", check_out="2026-10-03", key="idem_hold_0010")
        self.kernel.release_hold(hold.hold_id, reason="villa confirmed release")
        again = self.hold(check_in="2026-10-01", check_out="2026-10-03", key="idem_hold_0011")
        self.assertEqual(again.status, RK.HOLD_CONFIRMED)

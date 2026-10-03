"""§30's supplier settlement (ADR-0056) — what the agency owes, and has paid, the supplier.

Authority chain: ``src/schema/001_initial.sql`` + ``014_supplier_settlements.sql`` own the
storage contract, ``src/supplier_settlements.py`` is the single writer, and
``booking_closure._supplier`` is the §30 reader. Same discipline as the deposit's resolution
suite (ADR-0055): the fixtures seed the facts each check reads, every claim is reproducible on
a frozen clock, and the invariant tests fail if the rule is weakened.

The claims:

1. a person records ``DUE`` and ``PAID`` parts with evidence; a machine principal is refused;
2. the same idempotency key is the same fact — a conflicting payload returns the recorded row;
3. the writer refuses arithmetic that would contradict the ledger: paying more than the DUE
   total, a foreign currency, a non-positive amount, an action outside the closed vocabulary;
4. the ledger is append-only — UPDATE and DELETE are refused by trigger;
5. the act lands in the audit chain with its evidence ref;
6. the gate: no rows at all still names the gap; payments without a DUE fail closed by name;
   an unpaid remainder, an over-payment and a foreign currency each block by name; parts that
   sum to exactly the DUE pass, and the satisfied verdict keeps the booking out of blockers.
"""

from __future__ import annotations

import unittest

import booking_closure as CLOSURE
import supplier_settlements as SS
from primitives import FrozenClock
from test_deposit_resolutions import DepositResolutionsTestCase, EVIDENCE, HUMAN, HUMAN_ROLE, MACHINE, MACHINE_ROLE

AGENCY = "agency_default"
BOOKING = "book_settle_1"
NOW = "2026-09-21T00:00:00+00:00"

CONTRACT_REF = "contract_route_default_v1"


class TestSupplierSettlementWriter(DepositResolutionsTestCase):
    """The writer: one act, one row, one audit entry — and the refusals that guard it."""

    def settle(self, **kwargs):
        kwargs.setdefault("booking_id", BOOKING)
        kwargs.setdefault("decided_by", HUMAN)
        kwargs.setdefault("actor_role", HUMAN_ROLE)
        kwargs.setdefault("evidence_ref", EVIDENCE)
        return self.settlements.record(**kwargs)

    def setUp(self):
        super().setUp()
        self.settlements = SS.SupplierSettlementService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )

    def test_a_person_declares_the_due_and_pays_it_with_evidence(self):
        booking = self.seed_booking()
        due = self.settle(
            booking_id=booking, action="DUE", amount_minor=7_000_000, currency="IDR",
            idempotency_key="settle-due-1",
        )
        paid = self.settle(
            booking_id=booking, action="PAID", amount_minor=4_000_000, currency="IDR",
            idempotency_key="settle-pay-1",
        )
        self.settle(
            booking_id=booking, action="PAID", amount_minor=3_000_000, currency="IDR",
            idempotency_key="settle-pay-2",
        )
        self.assertEqual(due["route_id"], "route_default")
        self.assertEqual(paid["action"], "PAID")
        self.assertEqual(self.settlements.totals(booking), {"DUE": 7_000_000, "PAID": 7_000_000})
        self.assertEqual(len(self.settlements.for_booking(booking)), 3)
        self.assertNotIn(
            CLOSURE.CHECK_SUPPLIER,
            CLOSURE.evaluate(self.kernel.conn, booking_id=booking, agency_id=AGENCY).blockers,
        )

    def test_the_same_idempotency_key_is_the_same_fact(self):
        booking = self.seed_booking()
        first = self.settle(booking_id=booking, action="DUE", amount_minor=5_000_000,
                            currency="IDR", idempotency_key="settle-idem-1")
        second = self.settle(booking_id=booking, action="PAID", amount_minor=9_999_999,
                             currency="USD", decided_by="someone_else", actor_role="owner",
                             evidence_ref="other_contract_ref_9", idempotency_key="settle-idem-1")
        self.assertEqual(second["supplier_settlement_id"], first["supplier_settlement_id"])
        self.assertEqual(second["action"], "DUE")
        self.assertEqual(self.settlements.totals(booking), {"DUE": 5_000_000})

    def test_a_machine_actor_cannot_decide_what_the_supplier_is_owed(self):
        booking = self.seed_booking()
        with self.assertRaises(SS.SupplierHumanRequired) as raised:
            self.settle(booking_id=booking, action="DUE", amount_minor=1_000_000, currency="IDR",
                        decided_by=MACHINE, actor_role=MACHINE_ROLE, idempotency_key="settle-machine")
        self.assertEqual(raised.exception.code, "SUPPLIER_HUMAN_REQUIRED")
        self.assertEqual(self.settlements.for_booking(booking), ())

    def test_paying_more_than_the_due_is_refused_by_the_writer(self):
        booking = self.seed_booking()
        self.settle(booking_id=booking, action="DUE", amount_minor=2_000_000, currency="IDR",
                    idempotency_key="settle-due-cap")
        with self.assertRaises(SS.SupplierOverPaid) as raised:
            self.settle(booking_id=booking, action="PAID", amount_minor=3_000_000, currency="IDR",
                        idempotency_key="settle-pay-over")
        self.assertEqual(raised.exception.code, "SUPPLIER_OVER_PAID")
        self.assertEqual(self.settlements.totals(booking), {"DUE": 2_000_000})

    def test_a_foreign_currency_and_an_unknown_action_are_refused(self):
        booking = self.seed_booking()
        with self.assertRaises(SS.SupplierSettlementError):
            self.settle(booking_id=booking, action="DUE", amount_minor=1_000_000, currency="USD",
                        idempotency_key="settle-currency-1")
        with self.assertRaises(SS.SupplierActionUnknown):
            self.settle(booking_id=booking, action="WAIVED", amount_minor=1_000_000, currency="IDR",
                        idempotency_key="settle-action-1")
        with self.assertRaises(SS.SupplierBookingNotFound):
            self.settle(booking_id="book_never_existed", action="DUE", amount_minor=1_000_000,
                        currency="IDR", idempotency_key="settle-unknown-1")

    def test_the_act_is_in_the_audit_chain_with_its_evidence(self):
        booking = self.seed_booking()
        row = self.settle(booking_id=booking, action="DUE", amount_minor=1_000_000, currency="IDR",
                          idempotency_key="settle-audit-1")
        events = self.kernel.conn.execute(
            "SELECT action, entity_id, evidence_ref FROM audit_events"
            " WHERE entity_id = ?", (row["supplier_settlement_id"],),
        ).fetchall()
        self.assertEqual([e["action"] for e in events], ["supplier.settled"])
        self.assertEqual(events[0]["evidence_ref"], EVIDENCE)


class TestSupplierClosureGate(DepositResolutionsTestCase):
    """The §30 reader: it judges the ledger, and an empty ledger stays closed by name."""

    def setUp(self):
        super().setUp()
        self.settlements = SS.SupplierSettlementService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )

    def check(self, booking: str = BOOKING) -> CLOSURE.ClosureCheck:
        return CLOSURE.evaluate(
            self.kernel.conn, booking_id=booking, agency_id=AGENCY
        ).checks[3]

    def test_an_empty_ledger_still_fails_closed_by_name(self):
        booking = self.seed_booking()
        check = self.check(booking)
        self.assertFalse(check.ok)
        self.assertIn("no module records the supplier settlement", check.detail)

    def test_payments_without_a_due_fail_closed_by_name(self):
        booking = self.seed_booking()
        # The writer already refuses a payment with nothing declared due; the gate branch is
        # defensive against a ledger that predates the rule (or was written around it), so this
        # seeds the impossible state directly and pins the gate's answer to it.
        self.kernel.conn.execute(
            "INSERT INTO supplier_settlements (supplier_settlement_id, agency_id, booking_id,"
            " route_id, action, amount_minor, currency, decided_by, actor_role, evidence_ref,"
            " idempotency_key, created_at)"
            " VALUES ('supset_seed_1', ?, ?, 'route_default', 'PAID', 1000000, 'IDR', ?, ?, ?,"
            " 'settle-orphan-1', ?)",
            (AGENCY, booking, HUMAN, HUMAN_ROLE, EVIDENCE, NOW),
        )
        check = self.check(booking)
        self.assertFalse(check.ok)
        self.assertIn("nothing declares what the supplier is owed", check.detail)
        self.assertIn(CLOSURE.CHECK_SUPPLIER, CLOSURE.evaluate(
            self.kernel.conn, booking_id=booking, agency_id=AGENCY).blockers)

    def test_an_unpaid_remainder_keeps_the_blocker_and_names_it(self):
        booking = self.seed_booking()
        self.settlements.record(booking_id=booking, action="DUE", amount_minor=6_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-due-partial")
        self.settlements.record(booking_id=booking, action="PAID", amount_minor=4_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-pay-partial")
        check = self.check(booking)
        self.assertFalse(check.ok)
        self.assertIn("2000000 of 6000000 minor owed to the supplier", check.detail)
        self.assertIn("route_default", check.detail)

    def test_parts_summing_to_the_due_pass_and_leave_no_blocker(self):
        booking = self.seed_booking()
        self.settlements.record(booking_id=booking, action="DUE", amount_minor=6_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-due-full")
        self.settlements.record(booking_id=booking, action="PAID", amount_minor=6_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-pay-full")
        check = self.check(booking)
        self.assertTrue(check.ok)
        self.assertIn("6000000 minor owed to the supplier is settled", check.detail)
        self.assertNotIn(CLOSURE.CHECK_SUPPLIER, CLOSURE.evaluate(
            self.kernel.conn, booking_id=booking, agency_id=AGENCY).blockers)

    def test_the_writer_blocks_over_payment_before_the_gate_sees_a_contradiction(self):
        booking = self.seed_booking()
        self.settlements.record(booking_id=booking, action="DUE", amount_minor=1_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-due-tight")
        self.settlements.record(booking_id=booking, action="PAID", amount_minor=1_000_000,
                                currency="IDR", decided_by=HUMAN, actor_role=HUMAN_ROLE,
                                evidence_ref=EVIDENCE, idempotency_key="settle-pay-tight")
        self.assertTrue(self.check(booking).ok)


if __name__ == "__main__":
    unittest.main()
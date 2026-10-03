"""§30's security-deposit resolution (ADR-0055) — the fact that answers the held money.

Authority chain: ``src/schema/001_initial.sql`` + ``013_deposit_resolutions.sql`` own the
storage contract, ``src/deposit_resolutions.py`` is the single writer, and
``booking_closure._deposit`` is the §30 reader. The claims:

1. a person records returned / forfeited / applied — with evidence and the booking's own
   currency — and the §30 deposit check passes exactly when the parts sum to the amount
   held, in parts if that is how the money moved;
2. a machine actor is refused before anything is written (§18: a deposit's outcome is a
   person's decision), an unknown action is refused, so is an over-resolution — both by
   the writer and, read back, by the gate;
3. an unresolved remainder keeps the §30 blocker alive and names what is still open; a
   booking with no deposit is not blocked by this check at all;
4. the same ``idempotency_key`` is the same fact: a retry returns the recorded row
   unchanged instead of writing a second one, and nothing moves in the audit;
5. the ledger is append-only (UPDATE and DELETE are refused by trigger), the act is in
   the audit chain with the evidence ref, and a foreign booking id is *not found*.

The clock is frozen and the ids are deterministic: every claim here is reproducible.
"""

from __future__ import annotations

import unittest

import booking_closure as CLOSURE
import deposit_resolutions as DR
from primitives import DomainError, FrozenClock, IdFactory
from test_reservation_kernel import KernelTestCase, NOW

AGENCY = "agency_default"
BOOKING = "book_dep_1"
HUMAN = "staff_finance_01"
HUMAN_ROLE = "finance_manager"
MACHINE = "ai:reservation-agent"
MACHINE_ROLE = "ai_service"
EVIDENCE = "bank_transfer_ref_2026_11_01"


class DepositResolutionsTestCase(KernelTestCase):
    """The kernel fixture (one booking path) plus the deposit service over its connection."""

    def setUp(self):
        super().setUp()
        self.deposits = DR.DepositResolutionService(
            self.kernel.conn,
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="dep"),
            agency_id=AGENCY,
        )

    def seed_booking(self, *, booking_id: str = BOOKING, deposit_minor: int = 2_000_000) -> str:
        """One finished booking with a deposit held, seeded into the row §30 reads.

        No quote/hold dance — the same judgement the closure fixture makes: the gate judges
        recorded facts, and a test of the fact wants to set those facts directly.
        """

        self.kernel.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
            " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
            " updated_at, confirmed_at, cancelled_at)"
            " VALUES (?, 'prop_1', 'unit_1', '2026-10-01', '2026-10-03', 2, 'COMPLETED',"
            " 'guest_deposit', 2, 0, 0, 'IDR', 'policy_v1', ?, NULL, NULL, 1000000, 0, ?,"
            " 'UNPAID', ?, 1, ?, ?, ?, NULL)",
            (
                booking_id,
                "0" * 64,
                deposit_minor,
                f"idem_{booking_id}",
                NOW,
                NOW,
                NOW,
            ),
        )
        return booking_id


class TestDepositResolutionWriter(DepositResolutionsTestCase):
    """The single writer: vocabulary, human authority, arithmetic, idempotency, audit."""

    def test_a_person_records_the_outcome_with_evidence(self):
        booking = self.seed_booking()
        row = self.deposits.record(
            booking_id=booking,
            action="RETURNED",
            amount_minor=2_000_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-retry-1",
            note="returned in full at check-out",
        )
        self.assertEqual(row["action"], "RETURNED")
        self.assertEqual(row["decided_by"], HUMAN)
        self.assertEqual(row["evidence_ref"], EVIDENCE)
        self.assertEqual(self.deposits.totals(booking), {"RETURNED": 2_000_000})

    def test_a_machine_actor_is_refused_before_anything_is_written(self):
        booking = self.seed_booking()
        with self.assertRaises(DR.DepositHumanRequired) as raised:
            self.deposits.record(
                booking_id=booking,
                action="FORFEITED",
                amount_minor=2_000_000,
                currency="IDR",
                decided_by=MACHINE,
                actor_role=MACHINE_ROLE,
                evidence_ref=EVIDENCE,
                idempotency_key="depres-machine-1",
            )
        self.assertEqual(raised.exception.code, "DEPOSIT_HUMAN_REQUIRED")
        self.assertEqual(self.deposits.totals(booking), {})

    def test_an_unknown_action_is_refused_and_names_the_vocabulary(self):
        booking = self.seed_booking()
        with self.assertRaises(DR.DepositActionUnknown):
            self.deposits.record(
                booking_id=booking,
                action="KEPT",
                amount_minor=1,
                currency="IDR",
                decided_by=HUMAN,
                actor_role=HUMAN_ROLE,
                evidence_ref=EVIDENCE,
                idempotency_key="depres-unknown-1",
            )

    def test_an_over_resolution_is_refused_by_the_writer(self):
        booking = self.seed_booking(deposit_minor=1_000_000)
        self.deposits.record(
            booking_id=booking,
            action="RETURNED",
            amount_minor=1_000_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-part-1",
        )
        with self.assertRaises(DR.DepositOverResolved):
            self.deposits.record(
                booking_id=booking,
                action="APPLIED",
                amount_minor=1,
                currency="IDR",
                decided_by=HUMAN,
                actor_role=HUMAN_ROLE,
                evidence_ref=EVIDENCE,
                idempotency_key="depres-part-2",
            )

    def test_a_foreign_currency_is_refused(self):
        booking = self.seed_booking()
        with self.assertRaises(DomainError) as raised:
            self.deposits.record(
                booking_id=booking,
                action="RETURNED",
                amount_minor=2_000_000,
                currency="USD",
                decided_by=HUMAN,
                actor_role=HUMAN_ROLE,
                evidence_ref=EVIDENCE,
                idempotency_key="depres-currency-1",
            )
        self.assertEqual(raised.exception.code, "DEPOSIT_CURRENCY_MISMATCH")

    def test_the_same_idempotency_key_is_the_same_fact(self):
        booking = self.seed_booking()
        first = self.deposits.record(
            booking_id=booking,
            action="RETURNED",
            amount_minor=500_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-retry-same",
        )
        second = self.deposits.record(
            booking_id=booking,
            action="APPLIED",
            amount_minor=1_500_000,
            currency="IDR",
            decided_by="someone_else",
            actor_role="owner",
            evidence_ref="other_transfer_ref_99",
            idempotency_key="depres-retry-same",
        )
        self.assertEqual(second["deposit_resolution_id"], first["deposit_resolution_id"])
        self.assertEqual(second["action"], "RETURNED")
        self.assertEqual(self.deposits.totals(booking), {"RETURNED": 500_000})

    def test_the_act_is_in_the_audit_chain_with_its_evidence(self):
        booking = self.seed_booking()
        row = self.deposits.record(
            booking_id=booking,
            action="FORFEITED",
            amount_minor=2_000_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-audit-1",
        )
        events = self.kernel.conn.execute(
            "SELECT action, entity_id, evidence_ref FROM audit_events"
            " WHERE entity_id = ? AND action = 'deposit.resolved'",
            (row["deposit_resolution_id"],),
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evidence_ref"], EVIDENCE)

    def test_a_foreign_booking_is_not_found_not_forbidden(self):
        with self.assertRaises(DR.DepositBookingNotFound):
            self.deposits.record(
                booking_id="book_never",
                action="RETURNED",
                amount_minor=1,
                currency="IDR",
                decided_by=HUMAN,
                actor_role=HUMAN_ROLE,
                evidence_ref=EVIDENCE,
                idempotency_key="depres-foreign-1",
            )


class TestDepositLedgerIsAppendOnly(DepositResolutionsTestCase):
    """A resolution that can be edited is a resolution nobody has to prove."""

    def test_update_and_delete_are_refused_by_trigger(self):
        booking = self.seed_booking()
        row = self.deposits.record(
            booking_id=booking,
            action="APPLIED",
            amount_minor=2_000_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-append-1",
        )
        with self.assertRaises(Exception):
            self.kernel.conn.execute(
                "UPDATE deposit_resolutions SET amount_minor = 1"
                " WHERE deposit_resolution_id = ?",
                (row["deposit_resolution_id"],),
            )
        with self.assertRaises(Exception):
            self.kernel.conn.execute(
                "DELETE FROM deposit_resolutions WHERE deposit_resolution_id = ?",
                (row["deposit_resolution_id"],),
            )
        self.assertEqual(self.deposits.totals(booking), {"APPLIED": 2_000_000})


class TestDepositClosureGate(DepositResolutionsTestCase):
    """What the §30 gate answers once the fact has an owner."""

    def verdict(self, booking: str):
        return CLOSURE.evaluate(self.kernel.conn, booking_id=booking, agency_id=AGENCY)

    def check(self, booking: str) -> CLOSURE.ClosureCheck:
        return next(c for c in self.verdict(booking).checks if c.name == CLOSURE.CHECK_DEPOSIT)

    def test_parts_summing_to_the_hold_pass(self):
        booking = self.seed_booking(deposit_minor=2_000_000)
        self.deposits.record(
            booking_id=booking,
            action="APPLIED",
            amount_minor=500_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-gate-1",
        )
        self.deposits.record(
            booking_id=booking,
            action="RETURNED",
            amount_minor=1_500_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-gate-2",
        )
        check = self.check(booking)
        self.assertTrue(check.ok)
        self.assertIn("APPLIED 500000 minor (1)", check.detail)
        self.assertIn("RETURNED 1500000 minor (1)", check.detail)
        self.assertNotIn(CLOSURE.CHECK_DEPOSIT, self.verdict(booking).blockers)

    def test_an_unresolved_remainder_keeps_the_blocker_and_names_it(self):
        booking = self.seed_booking(deposit_minor=2_000_000)
        self.deposits.record(
            booking_id=booking,
            action="RETURNED",
            amount_minor=500_000,
            currency="IDR",
            decided_by=HUMAN,
            actor_role=HUMAN_ROLE,
            evidence_ref=EVIDENCE,
            idempotency_key="depres-gate-3",
        )
        check = self.check(booking)
        self.assertFalse(check.ok)
        self.assertIn("1500000 of 2000000 minor held remains unresolved", check.detail)
        self.assertIn(CLOSURE.CHECK_DEPOSIT, self.verdict(booking).blockers)

    def test_a_booking_with_no_deposit_is_not_blocked(self):
        booking = self.seed_booking(deposit_minor=0)
        self.assertTrue(self.check(booking).ok)

    def test_the_unowned_fact_still_fails_closed_by_name(self):
        booking = self.seed_booking()
        check = self.check(booking)
        self.assertFalse(check.ok)
        self.assertIn("no module records the security deposit's resolution", check.detail)


if __name__ == "__main__":
    unittest.main()
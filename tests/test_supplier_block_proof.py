"""ADR-0073 acceptance: a booking closes only on a supplier block that names it.

The old contract accepted ``availability_evidence`` — a unit-level answer with no
booking, no stay window and no supplier — so an AVAILABLE answer for the villa could
close a booking for dates nobody blocked. These tests pin the replacement: a proof row
that binds one authenticated supplier answer to one booking, one unit and one stay,
verified by a named human, and re-checked inside ``confirm_booking``'s transaction.

Every test drives the real kernel and the real schema; nothing here asserts against a
mock, and no test writes a booking status directly.
"""

from __future__ import annotations

import sqlite3

import reservation_kernel as RK
from booking_support import (
    block_proof,
    ensure_supplier,
    pay_booking,
    supplier_block_proof,
)
from primitives import DomainError
from test_reservation_kernel import KernelTestCase, NOW


class SupplierProofCase(KernelTestCase):
    """One reserved booking, plus shortcuts that never skip the kernel."""

    def reserve(
        self,
        *,
        key: str = "idem_e_0001",
        unit_id: str = "unit_1",
        check_in: str = "2026-10-01",
        check_out: str = "2026-10-03",
    ):
        quote = self.persist_quote(
            unit_id=unit_id, check_in=check_in, check_out=check_out, quote_id=f"quote_{key}"
        )
        hold = self.hold(
            unit_id=unit_id,
            check_in=check_in,
            check_out=check_out,
            key=f"hold_{key}",
            quote_ref=quote.quote_id,
        )
        booking = self.kernel.reserve_from_hold(
            hold_id=hold.hold_id,
            quote_id=quote.quote_id,
            idempotency_key=key,
            command_id=f"cmd_{key}",
            adults=2,
        )
        return hold, booking

    def paid(self, *, amount_minor=None, **kwargs):
        hold, booking = self.reserve(**kwargs)
        payment = pay_booking(
            self.kernel, booking.booking_id, amount_minor=amount_minor,
            key=f"pay_{booking.booking_id}",
        )
        self.kernel.mark_paid(
            booking_id=booking.booking_id,
            payment_id=payment,
            command_id=f"cmd_paid_{booking.booking_id}",
        )
        return hold, booking

    def code(self, call, *args, **kwargs) -> str:
        with self.assertRaises(DomainError) as ctx:
            call(*args, **kwargs)
        return ctx.exception.code

    def snapshots(self, booking_id: str) -> tuple:
        conn = self.kernel.conn
        return (
            conn.execute(
                "SELECT status, amount_paid_minor, confirmed_at FROM bookings WHERE booking_id = ?",
                (booking_id,),
            ).fetchone()[:],
            conn.execute("SELECT COUNT(*) FROM booking_state_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM supplier_block_proofs").fetchone()[0],
        )


class TestTheProofBindsTheBooking(SupplierProofCase):
    def test_a_valid_proof_for_the_right_booking_confirms_it(self):  # A, K
        hold, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)

        outcome = self.kernel.confirm_booking(
            booking_id=booking.booking_id,
            block_proof_id=proof,
            command_id="cmd_e_confirm_1",
            actor="user:reservation-1",
            actor_role="reservation",
        )

        self.assertEqual(outcome.status, "CONFIRMED")
        row = self.kernel.get_booking(booking.booking_id)
        self.assertIsNotNone(row["confirmed_at"])
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], RK.HOLD_CONVERTED)
        proof_row = self.kernel.get_supplier_block_proof(proof)
        self.assertEqual(proof_row["booking_id"], booking.booking_id)
        self.assertEqual(proof_row["unit_id"], booking.unit_id)
        self.assertEqual(proof_row["check_in"], booking.check_in)
        self.assertEqual(proof_row["check_out"], booking.check_out)

    def test_a_deposit_is_enough_when_the_proof_is_real(self):  # K
        _, booking = self.reserve(key="idem_e_dep")
        deposit = booking.deposit_due_minor
        payment = pay_booking(self.kernel, booking.booking_id, amount_minor=deposit, key="pay_dep")
        paid = self.kernel.mark_paid(
            booking_id=booking.booking_id, payment_id=payment, command_id="cmd_paid_dep"
        )
        self.assertEqual(paid.status, "DEPOSIT_PAID")
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        outcome = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_confirm_dep"
        )
        self.assertEqual(outcome.status, "CONFIRMED")

    def test_a_proof_for_another_booking_is_refused(self):  # B
        _, first = self.paid(key="idem_e_b1")
        _, second = self.paid(
            key="idem_e_b2", unit_id="unit_2", check_in="2026-10-10", check_out="2026-10-12"
        )
        proof = supplier_block_proof(self.kernel, first.booking_id)
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=second.booking_id,
                block_proof_id=proof,
                command_id="cmd_confirm_b2",
            ),
            "BLOCK_PROOF_NOT_FOR_BOOKING",
        )
        self.assertNotEqual(self.kernel.get_booking(second.booking_id)["status"], "CONFIRMED")

    def test_a_proof_from_another_tenant_is_invisible(self):  # C
        _, booking = self.paid();
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        self.kernel.conn.execute(
            "INSERT OR IGNORE INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_other', 'Other Agency', 'ACTIVE', ?, ?)",
            (NOW, NOW),
        )
        foreign_evidence = block_proof(self.kernel, "unit_1")
        self.kernel.conn.execute(
            "INSERT INTO supplier_block_proofs (proof_id, agency_id, booking_id, unit_id,"
            " check_in, check_out, supplier_id, relationship_id, evidence_id,"
            " verification_method, verified_by, verified_at, expires_at, created_at,"
            " revoked_at, revoked_by, revoke_reason)"
            " SELECT 'proof_other_tenant', 'agency_other', booking_id, unit_id, check_in,"
            " check_out, supplier_id, relationship_id, ?, verification_method,"
            " verified_by, verified_at, expires_at, created_at, ?, 'user:other',"
            " 'other tenant withdrew it'"
            " FROM supplier_block_proofs WHERE proof_id = ?",
            (foreign_evidence, NOW, proof),
        )
        before = self.snapshots(booking.booking_id)
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id="proof_other_tenant",
                command_id="cmd_confirm_tenant",
            ),
            "BLOCK_PROOF_REQUIRED",
        )
        self.assertEqual(self.snapshots(booking.booking_id), before)

    def test_a_proof_for_another_unit_is_refused(self):  # D
        _, booking = self.paid()
        self.assertEqual(
            self.code(
                supplier_block_proof,
                self.kernel,
                booking.booking_id,
                evidence_id=block_proof(self.kernel, "unit_2"),
            ),
            "BLOCK_PROOF_WRONG_UNIT",
        )

    def test_a_proof_for_other_dates_is_refused(self):  # E
        _, booking = self.paid()
        self.assertEqual(
            self.code(
                supplier_block_proof,
                self.kernel,
                booking.booking_id,
                check_in="2026-10-02",
                check_out="2026-10-04",
            ),
            "BLOCK_PROOF_WRONG_DATES",
        )


class TestTheProofMustStillBeGood(SupplierProofCase):
    def test_expired_evidence_cannot_become_a_proof(self):  # F
        _, booking = self.paid()
        stale = self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="manager_written",
            evidence_level="B",
            result="available",
            can_confirm=True,
            ttl_hours=1,
        )
        self.kernel.clock.set("2026-09-21T03:00:00+00:00")
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id, evidence_id=stale),
            "BLOCK_PROOF_EXPIRED",
        )

    def test_a_proof_that_expires_before_confirmation_is_refused(self):  # F (freshness)
        _, booking = self.paid()
        proof = supplier_block_proof(
            self.kernel, booking.booking_id, expires_at="2026-09-21T01:00:00+00:00"
        )
        self.kernel.clock.set("2026-09-21T02:00:00+00:00")
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_confirm_stale",
            ),
            "BLOCK_PROOF_EXPIRED",
        )
        self.assertNotEqual(self.kernel.get_booking(booking.booking_id)["status"], "CONFIRMED")

    def test_a_revoked_proof_is_refused_and_keeps_its_reason(self):  # F (revocation)
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        self.kernel.revoke_supplier_block_proof(
            proof_id=proof,
            reason="villa withdrew the block",
            actor="user:manager_1",
            actor_role="reservation_manager",
        )
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_confirm_revoked",
            ),
            "BLOCK_PROOF_REVOKED",
        )
        row = self.kernel.conn.execute(
            "SELECT revoked_at, revoked_by, revoke_reason FROM supplier_block_proofs"
            " WHERE proof_id = ?",
            (proof,),
        ).fetchone()
        self.assertIsNotNone(row["revoked_at"])
        self.assertEqual(row["revoked_by"], "user:manager_1")
        self.assertEqual(row["revoke_reason"], "villa withdrew the block")
        self.assertIsNone(self.kernel.supplier_block_proof_for_booking(booking.booking_id))

    def test_evidence_without_confirmation_authority_is_refused(self):  # G
        _, booking = self.paid()
        weak = self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="manager_written",
            evidence_level="B",
            result="available",
            can_confirm=False,
        )
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id, evidence_id=weak),
            "BLOCK_PROOF_NOT_AUTHORITATIVE",
        )

    def test_a_proof_without_a_named_verifier_is_refused(self):  # G (human verification)
        _, booking = self.paid()
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id, verified_by="ab"),
            "BLOCK_PROOF_VERIFIER_REQUIRED",
        )

    def test_an_unknown_verification_method_is_refused(self):  # G (method vocabulary)
        _, booking = self.paid()
        self.assertEqual(
            self.code(
                supplier_block_proof,
                self.kernel,
                booking.booking_id,
                verification_method="A_GUESS",
            ),
            "BLOCK_PROOF_INVALID_METHOD",
        )

    def test_a_suspended_supplier_cannot_authorise(self):  # H
        _, booking = self.paid()
        supplier_id, relationship_id = ensure_supplier(
            self.kernel, unit_id="unit_1", property_id="prop_1"
        )
        self.kernel.conn.execute(
            "UPDATE suppliers SET status = 'SUSPENDED' WHERE supplier_id = ?", (supplier_id,)
        )
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id),
            "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED",
        )
        self.kernel.conn.execute(
            "UPDATE suppliers SET status = 'ACTIVE' WHERE supplier_id = ?", (supplier_id,)
        )
        self.kernel.conn.execute(
            "UPDATE villa_supplier_relationships SET status = 'SUSPENDED'"
            " WHERE villa_supplier_relationship_id = ?",
            (relationship_id,),
        )
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id),
            "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED",
        )

    def test_a_supplier_suspended_after_the_proof_stops_the_confirmation(self):  # H (re-check)
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        self.kernel.conn.execute(
            "UPDATE suppliers SET status = 'SUSPENDED' WHERE supplier_id = ?",
            (self.kernel.get_supplier_block_proof(proof)["supplier_id"],),
        )
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_confirm_suspended",
            ),
            "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED",
        )

    def test_one_supplier_answer_cannot_back_two_bookings(self):  # I
        _, first = self.paid(key="idem_e_i1")
        _, second = self.paid(
            key="idem_e_i2", unit_id="unit_1", check_in="2026-10-20", check_out="2026-10-22"
        )
        shared = block_proof(self.kernel, "unit_1")
        supplier_block_proof(self.kernel, first.booking_id, evidence_id=shared)
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, second.booking_id, evidence_id=shared),
            "BLOCK_PROOF_EVIDENCE_REUSED",
        )
        with self.assertRaises(sqlite3.IntegrityError):  # the constraint, not just the check
            self.kernel.conn.execute(
                "INSERT INTO supplier_block_proofs (proof_id, agency_id, booking_id, unit_id,"
                " check_in, check_out, supplier_id, relationship_id, evidence_id,"
                " verification_method, verified_by, verified_at, expires_at, created_at)"
                " SELECT 'proof_dup', agency_id, ?, unit_id, check_in, check_out, supplier_id,"
                " relationship_id, evidence_id, verification_method, verified_by, verified_at,"
                " expires_at, created_at FROM supplier_block_proofs WHERE booking_id = ?",
                (second.booking_id, first.booking_id),
            )

    def test_a_closed_booking_cannot_accumulate_proofs(self):  # lifecycle
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_confirm_1"
        )
        self.assertEqual(
            self.code(supplier_block_proof, self.kernel, booking.booking_id),
            "BLOCK_PROOF_BOOKING_CLOSED",
        )


class TestConfirmationIsAtomicAndIdempotent(SupplierProofCase):
    def test_payment_pending_cannot_be_confirmed(self):  # J
        _, booking = self.reserve(key="idem_e_j")
        proof = supplier_block_proof(self.kernel, booking.booking_id)  # recording is allowed
        before = self.snapshots(booking.booking_id)
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_confirm_unpaid",
            ),
            "PAYMENT_REQUIRED",
        )
        self.assertEqual(self.snapshots(booking.booking_id), before)

    def test_duplicate_confirmation_does_not_duplicate_the_transition(self):  # L, N
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        first = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_confirm_1"
        )
        second = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_confirm_2"
        )
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        events = [
            e
            for e in self.kernel.state_events(booking.booking_id)
            if e["to_state"] == "CONFIRMED"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evidence_ref"], proof)
        audits = [
            a
            for a in self.kernel.audit_trail(entity_id=booking.booking_id)
            if a["action"] == "booking.confirmed"
        ]
        self.assertEqual(len(audits), 1)
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_a_refused_confirmation_leaves_no_partial_state(self):  # M, O
        hold, booking = self.paid()
        other = supplier_block_proof(self.kernel, booking.booking_id)
        self.kernel.revoke_supplier_block_proof(
            proof_id=other,
            reason="wrong dates on the letter",
            actor="user:ops-1",
            actor_role="reservation_agent",
        )
        before = self.snapshots(booking.booking_id)
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=other,
                command_id="cmd_confirm_refused",
            ),
            "BLOCK_PROOF_REVOKED",
        )
        self.assertEqual(self.snapshots(booking.booking_id), before)
        self.assertEqual(self.kernel.get_hold(hold.hold_id)["status"], RK.HOLD_CONFIRMED)
        self.assertEqual(self.kernel.get_booking(booking.booking_id)["status"], "PAID_IN_FULL")


class TestConfirmationDistrustsTheRow(SupplierProofCase):
    """A proof that never went through the kernel must not close a booking.

    ``record_supplier_block_proof`` refuses bad input, but the row can also arrive by a
    stray INSERT or UPDATE — a migration script, a support console, a future service that
    forgets the rule. The re-checks inside ``confirm_booking`` are the only defence there,
    and until now no test executed them. These tests write the bad row with SQL and ask
    the kernel to confirm.
    """

    def evidence_row(
        self,
        *,
        evidence_id: str,
        unit_id: str = "unit_1",
        level: str = "B",
        result: str = "AVAILABLE",
        can_confirm: int = 1,
        captured_at: str = NOW,
        valid_until: str = "2026-12-31T00:00:00+00:00",
    ) -> str:
        self.kernel.conn.execute(
            "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level,"
            " source_type, source_ref, captured_at, valid_until, result, occupancy_ranges,"
            " can_quote, can_hold, can_confirm, created_at)"
            " VALUES (?, ?, ?, 'manager_written', 'villa_block_letter', ?, ?, ?, '[]', 1, 1, ?, ?)",
            (evidence_id, unit_id, level, captured_at, valid_until, result, can_confirm, NOW),
        )
        self.kernel.conn.commit()
        return evidence_id

    def raw_proof(
        self,
        *,
        booking_id: str,
        evidence_id: str,
        proof_id: str = "proof_raw_1",
        unit_id: str = "unit_1",
        check_in: str = "2026-10-01",
        check_out: str = "2026-10-03",
        expires_at: str = "2026-12-31T00:00:00+00:00",
    ) -> str:
        self.kernel.conn.execute(
            "INSERT INTO supplier_block_proofs (proof_id, booking_id, unit_id, check_in,"
            " check_out, supplier_id, relationship_id, evidence_id, verification_method,"
            " verified_by, verified_at, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SUPPLIER_EMAIL', 'user:ops-1', ?, ?, ?)",
            (
                proof_id,
                booking_id,
                unit_id,
                check_in,
                check_out,
                f"sup_{unit_id}",
                f"vsr_{unit_id}",
                evidence_id,
                NOW,
                expires_at,
                NOW,
            ),
        )
        self.kernel.conn.commit()
        return proof_id

    def seed_supplier(self, unit_id: str = "unit_1") -> None:
        ensure_supplier(self.kernel, unit_id=unit_id, property_id="prop_1")

    def test_confirm_refuses_a_proof_pointing_at_another_unit(self):  # re-check
        self.seed_supplier("unit_2")
        _, booking = self.paid()
        evidence = self.evidence_row(evidence_id="ev_raw_unit", unit_id="unit_2")
        proof = self.raw_proof(booking_id=booking.booking_id, evidence_id=evidence, unit_id="unit_2")
        before = self.snapshots(booking.booking_id)

        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_raw_unit",
            ),
            "BLOCK_PROOF_WRONG_UNIT",
        )
        self.assertEqual(self.snapshots(booking.booking_id), before)

    def test_confirm_refuses_a_proof_for_a_different_stay(self):
        self.seed_supplier()
        _, booking = self.paid()
        evidence = self.evidence_row(evidence_id="ev_raw_dates")
        proof = self.raw_proof(
            booking_id=booking.booking_id,
            evidence_id=evidence,
            check_in="2026-10-02",
            check_out="2026-10-04",
        )

        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_raw_dates",
            ),
            "BLOCK_PROOF_WRONG_DATES",
        )
        self.assertEqual(self.kernel.get_booking(booking.booking_id)["status"], "PAID_IN_FULL")

    def test_confirm_refuses_evidence_that_may_not_confirm(self):
        self.seed_supplier()
        _, booking = self.paid()
        evidence = self.evidence_row(evidence_id="ev_raw_weak", level="C", can_confirm=0)
        proof = self.raw_proof(booking_id=booking.booking_id, evidence_id=evidence)

        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_raw_weak",
            ),
            "BLOCK_PROOF_NOT_AUTHORITATIVE",
        )

    def test_confirm_refuses_evidence_older_than_the_booking(self):
        self.seed_supplier()
        _, booking = self.paid()
        evidence = self.evidence_row(
            evidence_id="ev_raw_old", captured_at="2026-08-01T00:00:00+00:00"
        )
        proof = self.raw_proof(booking_id=booking.booking_id, evidence_id=evidence)

        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_raw_old",
            ),
            "BLOCK_PROOF_PREDATES_BOOKING",
        )

    def test_a_restricted_answer_is_not_a_block(self):
        """AVAILABLE_BUT_RESTRICTED says the dates carry conditions. It must not close."""
        self.seed_supplier()
        _, booking = self.paid()
        evidence = self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="manager_written",
            evidence_level="B",
            result="AVAILABLE_BUT_RESTRICTED",
            restrictions={"min_stay_nights": 5},
            can_confirm=True,
            source_ref="villa_block_letter",
        )

        self.assertEqual(
            self.code(
                self.kernel.record_supplier_block_proof,
                booking_id=booking.booking_id,
                evidence_id=evidence,
                supplier_id="sup_unit_1",
                relationship_id="vsr_unit_1",
                check_in=booking.check_in,
                check_out=booking.check_out,
                verification_method="MANAGER_WRITTEN",
                verified_by="user:ops-1",
                actor="user:ops-1",
                verifier_role="reservation_agent",
                idempotency_key="idem_restricted_0001",
            ),
            "BLOCK_PROOF_DATES_RESTRICTED",
        )

    def test_a_live_proof_cannot_be_repointed_by_sql(self):
        """uq_sbp_evidence only guards INSERT; the trigger guards the row."""
        self.seed_supplier()
        _, first = self.paid(key="idem_e_keep")
        proof = supplier_block_proof(self.kernel, first.booking_id)
        _, second = self.paid(key="idem_e_steal", check_in="2026-11-01", check_out="2026-11-03")

        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.kernel.conn.execute(
                "UPDATE supplier_block_proofs SET booking_id = ? WHERE proof_id = ?",
                (second.booking_id, proof),
            )
        self.assertIn("immutable", str(ctx.exception))
        self.kernel.conn.rollback()
        self.assertEqual(self.kernel.get_supplier_block_proof(proof)["booking_id"], first.booking_id)

    def test_a_proof_cannot_be_deleted(self):
        self.seed_supplier()
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)

        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.kernel.conn.execute("DELETE FROM supplier_block_proofs WHERE proof_id = ?", (proof,))
        self.assertIn("append-only", str(ctx.exception))
        self.kernel.conn.rollback()
        self.assertIsNotNone(self.kernel.get_supplier_block_proof(proof))

    def test_revocation_is_the_only_update_the_row_allows(self):
        self.seed_supplier()
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)

        self.kernel.revoke_supplier_block_proof(
            proof_id=proof,
            reason="supplier pulled the block",
            actor="user:ops-1",
            actor_role="reservation_agent",
        )
        row = self.kernel.get_supplier_block_proof(proof)
        self.assertIsNotNone(row["revoked_at"])
        self.assertEqual(row["revoke_reason"], "supplier pulled the block")

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "UPDATE supplier_block_proofs SET revoke_reason = 'rewritten' WHERE proof_id = ?",
                (proof,),
            )
        self.kernel.conn.rollback()


class TestTheWriteHasAnOwnerAndAKey(SupplierProofCase):
    """E3: who may sign, what a retry means, and what the trail records."""

    def _seed(self) -> None:
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")

    def _prove(self, booking, *, key="idem_e3_0001", **overrides):
        options = dict(
            booking_id=booking.booking_id,
            evidence_id=overrides.pop("evidence_id", None) or block_proof(self.kernel, "unit_1"),
            supplier_id="sup_unit_1",
            relationship_id="vsr_unit_1",
            check_in=booking.check_in,
            check_out=booking.check_out,
            verification_method="MANAGER_WRITTEN",
            verified_by="user:ops-1",
            actor="user:ops-1",
            verifier_role="reservation_agent",
            idempotency_key=key,
        )
        options.update(overrides)
        return self.kernel.record_supplier_block_proof(**options)

    def _audit(self, action: str):
        return [
            row for row in self.kernel.audit_trail() if row["action"] == action
        ]

    def test_a_machine_principal_cannot_sign_a_human_verification(self):
        self._seed()
        _, booking = self.paid()
        self.assertEqual(
            self.code(self._prove, booking, verifier_role="ai_service"),
            "BLOCK_PROOF_MACHINE_VERIFIER",
        )
        self.assertEqual(
            self.code(
                self._prove, booking, actor="ai:planner", verified_by="ai:planner",
                idempotency_key="idem_e3_0002",
            ),
            "BLOCK_PROOF_MACHINE_VERIFIER",
        )
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM supplier_block_proofs"
            ).fetchone()[0],
            0,
        )

    def test_verified_by_must_name_the_acting_human(self):
        self._seed()
        _, booking = self.paid()
        self.assertEqual(
            self.code(self._prove, booking, verified_by="user:someone_else"),
            "BLOCK_PROOF_VERIFIER_MISMATCH",
        )

    def test_an_unnamed_role_cannot_verify(self):
        self._seed()
        _, booking = self.paid()
        for role in ("kernel", "root", "banana"):
            self.assertEqual(
                self.code(self._prove, booking, verifier_role=role),
                "BLOCK_PROOF_VERIFIER_ROLE_UNKNOWN",
                role,
            )
        # ``system`` is a machine id, so the machine refusal speaks first.
        self.assertEqual(
            self.code(self._prove, booking, verifier_role="system"),
            "BLOCK_PROOF_MACHINE_VERIFIER",
        )

    def test_the_same_key_and_payload_replays_instead_of_writing_twice(self):
        self._seed()
        _, booking = self.paid()
        evidence = block_proof(self.kernel, "unit_1")
        first = self._prove(booking, evidence_id=evidence, key="idem_e3_replay")
        second = self._prove(booking, evidence_id=evidence, key="idem_e3_replay")

        self.assertEqual(first, second)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM supplier_block_proofs"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(len(self._audit("supplier_block.recorded")), 1)

    def test_the_same_key_with_a_different_payload_is_refused(self):
        self._seed()
        _, booking = self.paid()
        self._prove(booking, key="idem_e3_conflict")
        self.assertEqual(
            self.code(
                self._prove,
                booking,
                key="idem_e3_conflict",
                verification_method="MANAGER_CALL",
            ),
            "IDEMPOTENCY_CONFLICT",
        )

    def test_recording_leaves_one_audit_row_and_a_valid_chain(self):
        self._seed()
        _, booking = self.paid()
        proof = self._prove(booking)

        rows = self._audit("supplier_block.recorded")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], proof)
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_revocation_is_audited_once_and_retrying_is_a_no_op(self):
        self._seed()
        _, booking = self.paid()
        proof = self._prove(booking)

        self.kernel.revoke_supplier_block_proof(
            proof_id=proof,
            reason="villa pulled the block",
            actor="user:ops-1",
            actor_role="reservation_agent",
        )
        self.kernel.revoke_supplier_block_proof(
            proof_id=proof,
            reason="villa pulled the block",
            actor="user:ops-1",
            actor_role="reservation_agent",
        )

        rows = self._audit("supplier_block.revoked")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], proof)
        self.assertTrue(self.kernel.verify_audit_chain())

    def test_a_machine_principal_cannot_revoke(self):
        self._seed()
        _, booking = self.paid()
        proof = self._prove(booking)
        self.assertEqual(
            self.code(
                self.kernel.revoke_supplier_block_proof,
                proof_id=proof,
                reason="not yours to revoke",
                actor="ai:planner",
                actor_role="ai_service",
            ),
            "BLOCK_PROOF_MACHINE_VERIFIER",
        )
        self.assertIsNone(self.kernel.get_supplier_block_proof(proof)["revoked_at"])

    def test_a_machine_principal_cannot_confirm_a_booking(self):
        self._seed()
        _, booking = self.paid()
        proof = self._prove(booking)
        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_machine_confirm",
                actor="ai:planner",
                actor_role="ai_service",
            ),
            "MACHINE_PRINCIPAL_REFUSED",
        )
        self.assertNotEqual(
            self.kernel.get_booking(booking.booking_id)["status"], "CONFIRMED"
        )


class TestExpiryIsInstantsNotStrings(SupplierProofCase):
    """E3-F: a timestamp is a moment, not a byte string."""

    def _seed(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")

    def _proof(self, booking, expires_at, *, key):
        return self.kernel.record_supplier_block_proof(
            booking_id=booking.booking_id,
            evidence_id=block_proof(
                self.kernel, valid_until="2027-06-01T00:00:00+00:00"
            ),
            supplier_id="sup_unit_1",
            relationship_id="vsr_unit_1",
            check_in=booking.check_in,
            check_out=booking.check_out,
            verification_method="MANAGER_WRITTEN",
            verified_by="user:ops-1",
            actor="user:ops-1",
            verifier_role="reservation_agent",
            idempotency_key=key,
            expires_at=expires_at,
        )

    def test_an_offset_expiry_is_the_same_instant_as_utc(self):
        self._seed()
        _, booking = self.paid()
        # Evidence runs to 2027-06-01T00:00:00+00:00; this is exactly that instant,
        # written in another offset. A byte comparison would call it *after* it.
        proof = self._proof(booking, "2027-05-31T19:00:00-05:00", key="idem_tz_0001")
        row = self.kernel.get_supplier_block_proof(proof)
        self.assertEqual(row["expires_at"], "2027-06-01T00:00:00+00:00")

    def test_an_offset_expiry_past_the_evidence_is_refused(self):
        self._seed()
        _, booking = self.paid()
        self.assertEqual(
            self.code(self._proof, booking, "2027-06-01T09:00:01+09:00", key="idem_tz_0002"),
            "BLOCK_PROOF_INVALID_EXPIRY",
        )

    def test_a_malformed_expiry_is_refused(self):
        self._seed()
        _, booking = self.paid()
        for bad in ("next tuesday", "", "2027-06-01", "2027-06-01T00:00:00"):
            with self.assertRaises(DomainError, msg=bad):
                self._proof(booking, bad, key=f"idem_tz_bad_{len(bad)}_{bad[:2]}")
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM supplier_block_proofs"
            ).fetchone()[0],
            0,
        )

    def test_revoking_expired_and_replacing_still_needs_a_valid_answer(self):
        self._seed()
        _, booking = self.paid()
        proof = self._proof(booking, "2027-06-01T00:00:00+00:00", key="idem_tz_0003")
        self.kernel.revoke_supplier_block_proof(
            proof_id=proof, reason="wrong letter", actor="user:ops-1",
            actor_role="reservation_agent",
        )
        replacement = self._proof(booking, "2027-06-01T00:00:00+00:00", key="idem_tz_0004")
        self.assertNotEqual(proof, replacement)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM supplier_block_proofs WHERE revoked_at IS NULL"
            ).fetchone()[0],
            1,
        )


class TestAKernelCannotLendSomebodyElsesAuthority(SupplierProofCase):
    """E3-B: the identity is the one the kernel is bound to, not one a call supplies."""

    def _reprove(self, kernel, booking, *, key, **overrides):
        options = dict(
            booking_id=booking.booking_id,
            evidence_id=block_proof(kernel, unit_id=booking.unit_id),
            supplier_id="sup_unit_1",
            relationship_id="vsr_unit_1",
            check_in=booking.check_in,
            check_out=booking.check_out,
            verification_method="MANAGER_WRITTEN",
            verified_by="user:ops-1",
            idempotency_key=key,
        )
        options.update(overrides)
        return kernel.record_supplier_block_proof(**options)

    def test_a_machine_bound_kernel_cannot_sign_with_human_arguments(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")
        _, booking = self.paid()
        machine = RK.ReservationKernel(
            self.kernel.conn,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            actor="ai:planner",
            actor_role="ai_service",
        )
        self.assertEqual(
            self.code(
                self._reprove,
                machine,
                booking,
                key="idem_bound_machine",
                actor="user:ops-1",
                verifier_role="reservation_agent",
            ),
            "BLOCK_PROOF_MACHINE_VERIFIER",
        )
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM supplier_block_proofs"
            ).fetchone()[0],
            0,
        )

    def test_a_bound_kernel_cannot_sign_as_somebody_else(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")
        _, booking = self.paid()
        bound = RK.ReservationKernel(
            self.kernel.conn,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            actor="user:ops-1",
            actor_role="reservation_agent",
        )
        self.assertEqual(
            self.code(
                self._reprove,
                bound,
                booking,
                key="idem_bound_third_party",
                verified_by="user:someone_else",
                actor="user:someone_else",
                verifier_role="reservation_manager",
            ),
            "BLOCK_PROOF_ACTOR_NOT_BOUND",
        )

    def test_a_machine_bound_kernel_cannot_confirm_even_without_arguments(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        machine = RK.ReservationKernel(
            self.kernel.conn,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            actor="ai:planner",
            actor_role="ai_service",
        )
        self.assertEqual(
            self.code(
                machine.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_machine_bound_confirm",
            ),
            "MACHINE_PRINCIPAL_REFUSED",
        )

    def test_a_proof_signed_by_a_machine_cannot_unlock_confirmation(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")
        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        # A row that arrived another way — a direct INSERT, or data written before this
        # contract. The confirmation gate reads what the row *says*, not what the writer
        # meant, and it must not accept a machine's signature. The kernel-written proof
        # is revoked first so the one-live-proof index still holds.
        self.kernel.revoke_supplier_block_proof(
            proof_id=proof,
            reason="replaced by a pre-contract row",
            actor="user:ops-1",
            actor_role="reservation_agent",
        )
        stamp = "2026-09-21T08:00:00+00:00"
        self.kernel.conn.execute(
            "INSERT INTO supplier_block_proofs (proof_id, agency_id, booking_id, unit_id,"
            " check_in, check_out, supplier_id, relationship_id, evidence_id,"
            " verification_method, verified_by, verified_at, expires_at, created_at)"
            " VALUES ('proof_legacy_machine', ?, ?, ?, ?, ?, 'sup_unit_1', 'vsr_unit_1', ?,"
            " 'MANAGER_WRITTEN', 'ai:planner', ?, ?, ?)",
            (
                self.kernel.agency_id,
                booking.booking_id,
                booking.unit_id,
                booking.check_in,
                booking.check_out,
                self.kernel.conn.execute(
                    "SELECT evidence_id FROM availability_evidence LIMIT 1"
                ).fetchone()[0],
                stamp,
                "2027-06-01T00:00:00+00:00",
                stamp,
            ),
        )
        self.kernel.conn.commit()

        self.assertEqual(
            self.code(
                self.kernel.confirm_booking,
                booking_id=booking.booking_id,
                block_proof_id="proof_legacy_machine",
                command_id="cmd_machine_signed_confirm",
            ),
            "BLOCK_PROOF_VERIFIER_NOT_HUMAN",
        )

    def test_a_retry_after_the_booking_moved_on_answers_with_the_original_proof(self):
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")
        _, booking = self.paid()
        evidence = block_proof(self.kernel, unit_id="unit_1")
        proof = self._reprove(
            self.kernel, booking, key="idem_after_confirm", evidence_id=evidence,
            actor="user:ops-1", verifier_role="reservation_agent",
        )
        self.kernel.confirm_booking(
            booking_id=booking.booking_id,
            block_proof_id=proof,
            command_id="cmd_confirm_before_retry",
            actor="user:reservation-1",
            actor_role="reservation_agent",
        )

        replay = self._reprove(
            self.kernel, booking, key="idem_after_confirm", evidence_id=evidence,
            actor="user:ops-1", verifier_role="reservation_agent",
        )

        self.assertEqual(replay, proof)

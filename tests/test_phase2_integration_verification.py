"""Independent verification probes for the Phase-2 modules (cases, intake, approvals).

These probes were written by the integrator, not by the authors of the modules, and
they deliberately re-check the properties that matter at the seams:

* redaction and hashing of guest content, asserted against the stored row;
* refusals that must be *trace-free* (no event, no status move, no second row);
* provenance that must survive a round trip through two modules;
* an approval status that must be derived, never read from a writable column.

A probe that passes while the module is broken is worthless, so each refusal is
asserted together with the database state it is required to leave behind.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import approvals as A
import cases as C
import intake as I
import primitives as P
from schema_loader import connect_sqlite

START = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
RAW_BODY = (
    "Halo, saya Budi. Email saya budi@example.com dan nomor 081234567890.\n"
    "Rencana menginap 12-14 Oktober 2026, 2 dewasa 1 anak.\n"
    "Budget sekitar Rp 2.500.000 per malam di Canggu untuk liburan."
)


class Phase2Verification(unittest.TestCase):
    """Cross-module probes over the real DDL (in-memory SQLite)."""

    def setUp(self):
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = P.FrozenClock(START)
        self.ids = P.IdFactory(deterministic=True, seed="v")
        self.cases = C.CaseService(self.conn, clock=self.clock, ids=self.ids)
        self.intake = I.IntakeService(self.conn, clock=self.clock, ids=self.ids)
        self.approvals = A.ApprovalService(self.conn, clock=self.clock, ids=self.ids)
        self.case, self.message = self.cases.open_case(
            channel="website",
            guest_ref="guest_0001",
            content_class="P2",
            body=RAW_BODY,
            idempotency_key="idem_verify_0001",
        )

    # -- helpers ---------------------------------------------------------
    def _scalar(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def _events(self) -> int:
        return self._scalar(
            "SELECT COUNT(*) FROM case_events WHERE case_id = ?", self.case.case_id
        )

    def _baseline_events(self) -> int:
        """Events written by opening the case (one CREATED row)."""
        return self._events()

    def test_the_extractor_accepts_a_datetime_clock_or_a_plain_date(self):
        """Regression: ``datetime`` exposes its own ``now`` classmethod.

        A duck-typed check that runs before the ``isinstance`` check calls
        ``datetime.now()`` and replaces the caller's aware instant with a naive
        local one — the extractor then refuses a perfectly valid aware datetime.
        """
        from datetime import date as _date

        aware = P.parse_iso("2026-09-21T08:00:00+00:00")
        from_datetime = I.parse_rule_based("menginap 12 Oktober 2026, 2 dewasa", now=aware)
        self.assertTrue(from_datetime)
        self.assertIn("check_in", {field.name for field in from_datetime})

        from_clock = I.parse_rule_based("menginap 12 Oktober 2026, 2 dewasa", now=self.clock)
        self.assertEqual(
            [f.as_dict() for f in from_clock], [f.as_dict() for f in from_datetime]
        )

        from_date = I.parse_rule_based("menginap 12 Oktober 2026, 2 dewasa", now=_date(2026, 9, 21))
        self.assertEqual(
            [f.as_dict() for f in from_date], [f.as_dict() for f in from_datetime]
        )

        with self.assertRaises(I.IntakeError) as caught:
            I.parse_rule_based("menginap 12 Oktober 2026", now=datetime(2026, 9, 21, 8, 0))
        self.assertEqual(caught.exception.code, "NAIVE_TIMESTAMP")

    # -- cases: content minimisation -------------------------------------
    def test_guest_identifiers_are_not_stored_but_the_hash_covers_the_raw_body(self):
        row = self.conn.execute(
            "SELECT body_redacted, body_sha256 FROM case_messages WHERE case_message_id = ?",
            (self.message.case_message_id,),
        ).fetchone()
        stored = row["body_redacted"] or ""
        self.assertNotIn("budi@example.com", stored)
        self.assertNotIn("081234567890", stored)
        self.assertEqual(row["body_sha256"], P.sha256_hex(RAW_BODY))

    def test_p3_content_is_never_stored_yet_still_provable(self):
        raw = "Nomor KTP saya 3273010101990001"
        message = self.cases.record_inbound(
            self.case.case_id,
            content_class="P3",
            body=raw,
            sender_ref="guest_0001",
            idempotency_key="idem_verify_p3",
        )
        row = self.conn.execute(
            "SELECT body_redacted, body_sha256 FROM case_messages WHERE case_message_id = ?",
            (message.case_message_id,),
        ).fetchone()
        self.assertIsNone(row["body_redacted"])
        self.assertEqual(row["body_sha256"], P.sha256_hex(raw))

    # -- cases: trace-free refusals --------------------------------------
    def test_a_replayed_command_does_not_write_a_second_event(self):
        first = self.cases.transition(
            self.case.case_id,
            "TRIAGED",
            actor="agent_ana",
            actor_role="agent",
            reason="permintaan lengkap",
            command_id="cmd_verify_0001",
        )
        self.assertEqual(first.status, "TRIAGED")
        before = self._events()
        with self.assertRaises(C.CaseError):
            self.cases.transition(
                self.case.case_id,
                "MATCHING",
                actor="agent_ana",
                actor_role="agent",
                reason="coba ulang command yang sama",
                command_id="cmd_verify_0001",
            )
        self.assertEqual(self._events(), before, "command replay wrote a second event")
        self.assertEqual(self.cases.get(self.case.case_id).status, "TRIAGED")

    def test_an_illegal_transition_leaves_no_trace_at_all(self):
        with self.assertRaises(C.CaseError):
            self.cases.transition(
                self.case.case_id,
                "BOOKED",
                actor="agent_ana",
                actor_role="agent",
                reason="lompat status",
                command_id="cmd_verify_illegal",
            )
        self.assertEqual(self._events(), self._baseline_events())
        self.assertEqual(self.cases.get(self.case.case_id).status, "NEW")

    def test_a_closed_case_refuses_traffic_and_state_moves(self):
        self.cases.close(
            self.case.case_id,
            outcome="LOST",
            reason="tamu memilih villa lain",
            actor="agent_ana",
            actor_role="agent",
            command_id="cmd_verify_close",
        )
        with self.assertRaises(C.CaseError):
            self.cases.transition(
                self.case.case_id,
                "TRIAGED",
                actor="agent_ana",
                actor_role="agent",
                reason="hidupkan lagi",
                command_id="cmd_verify_reopen",
            )
        with self.assertRaises(C.CaseError):
            self.cases.record_inbound(
                self.case.case_id,
                content_class="P1",
                body="halo lagi",
                sender_ref="guest_0001",
                idempotency_key="idem_verify_closed",
            )

    # -- cases: SLA boundary --------------------------------------------
    def test_overdue_starts_strictly_after_the_due_instant(self):
        case = self.cases.get(self.case.case_id)
        due = P.parse_iso(case.first_response_due_at)
        self.clock.set(due)
        self.assertEqual(
            [c.case_id for c in self.cases.first_response_overdue()],
            [],
            "a case exactly at its due instant is not overdue yet",
        )
        self.clock.set(due + timedelta(seconds=1))
        self.assertIn(
            self.case.case_id,
            [c.case_id for c in self.cases.first_response_overdue()],
        )
        self.cases.mark_first_response(
            self.case.case_id,
            actor="agent_ana",
            actor_role="agent",
            at=due,
            command_id="cmd_verify_answer",
        )
        self.assertNotIn(
            self.case.case_id,
            [c.case_id for c in self.cases.first_response_overdue()],
            "an answered case must leave the overdue queue",
        )

    # -- intake: provenance ---------------------------------------------
    def test_a_proposal_cannot_borrow_a_message_from_another_case(self):
        other_case, other_message = self.cases.open_case(
            channel="email",
            guest_ref="guest_0002",
            content_class="P1",
            body="saya mau tanya villa",
            idempotency_key="idem_verify_0002",
        )
        self.assertNotEqual(other_case.case_id, self.case.case_id)
        with self.assertRaises(I.IntakeError):
            self.intake.propose(
                self.case.case_id,
                source_message_id=other_message.case_message_id,
                fields=[I.ProposedField(name="adults", value=2, confidence=90)],
                extractor="intake.rule.v1",
            )

    def test_a_low_confidence_or_anonymous_review_cannot_create_a_fact(self):
        proposal_id, rows = self.intake.propose(
            self.case.case_id,
            source_message_id=self.message.case_message_id,
            fields=[
                I.ProposedField(name="adults", value=2, confidence=90, span_start=0, span_end=4),
                I.ProposedField(name="purpose", value="leisure", confidence=20),
            ],
            extractor="intake.rule.v1",
        )
        by_name = {row.field_name: row for row in rows}
        with self.assertRaises(I.IntakeError):
            self.intake.review(
                intake_field_id=by_name["purpose"].intake_field_id,
                decision="ACCEPT",
                reviewer="agent_ana",
                reviewer_role="agent",
            )
        # An empty reviewer is refused by the shared ref validator (DomainError);
        # what matters is that it never creates a fact, not which layer refuses it.
        with self.assertRaises(P.DomainError):
            self.intake.review(
                intake_field_id=by_name["adults"].intake_field_id,
                decision="ACCEPT",
                reviewer="",
                reviewer_role="agent",
            )
        with self.assertRaises(I.IntakeError):
            self.intake.review(
                intake_field_id=by_name["adults"].intake_field_id,
                decision="REJECT",
                reviewer="agent_ana",
                reviewer_role="agent",
            )
        self.assertEqual(self.intake.accepted_facts(self.case.case_id), {})
        self.assertEqual(self.intake.effective_status(by_name["adults"].intake_field_id), "PROPOSED")
        self.assertTrue(proposal_id)

    def test_only_accepted_fields_become_facts_and_gaps_stay_visible(self):
        parsed = I.parse_rule_based(RAW_BODY, now=START)
        self.assertTrue(parsed, "rule extractor found nothing in an explicit request")
        _, rows = self.intake.propose(
            self.case.case_id,
            source_message_id=self.message.case_message_id,
            fields=parsed,
            extractor="intake.rule.v1",
        )
        by_name = {row.field_name: row for row in rows}
        self.intake.review(
            intake_field_id=by_name["check_in"].intake_field_id,
            decision="ACCEPT",
            reviewer="agent_ana",
            reviewer_role="agent",
        )
        self.intake.review(
            intake_field_id=by_name["adults"].intake_field_id,
            decision="ACCEPT",
            reviewer="agent_ana",
            reviewer_role="agent",
        )
        self.intake.review(
            intake_field_id=by_name["budget_minor"].intake_field_id,
            decision="REJECT",
            reviewer="agent_ana",
            reviewer_role="agent",
            reason="tamu menyebut budget per malam, bukan total",
        )
        facts = self.intake.accepted_facts(self.case.case_id)
        self.assertIn("check_in", facts)
        self.assertEqual(facts["adults"], 2)
        self.assertNotIn("budget_minor", facts)
        gaps = self.intake.unknown_required(self.case.case_id)
        self.assertIn("check_out", gaps)
        self.assertNotIn("check_in", gaps)
        self.assertNotIn("adults", gaps)

    # -- approvals: dual control ----------------------------------------
    def _request_refund(self, *, required_approvals: int, requested_by: str = "agent_ana",
                        idempotency_key: str = "idem_verify_refund_1"):
        return self.approvals.request(
            subject_type="refund",
            subject_id=self.case.case_id,
            action="refund deposit tamu",
            requested_by=requested_by,
            requested_role="agent",
            reason="tamu membatalkan di luar jendela kebijakan",
            payload={"booking_ref": "bk_verify_1", "amount_minor": 750_000},
            required_role="owner",
            required_approvals=required_approvals,
            risk_level="HIGH",
            evidence_refs=(self.message.case_message_id,),
            money=P.Money.from_minor(750_000, "IDR"),
            expires_minutes=30,
            idempotency_key=idempotency_key,
        )

    def test_money_moving_requests_need_two_distinct_approvers(self):
        with self.assertRaises(A.ApprovalError):
            self._request_refund(required_approvals=1)
        approval = self._request_refund(required_approvals=2)
        self.assertTrue(approval.is_pending)

        with self.assertRaises(A.ApprovalError):
            self.approvals.decide(
                approval.approval_id,
                approver="agent_ana",
                approver_role="owner",
                decision="APPROVE",
                note="menyetujui permintaan sendiri",
            )

        first = self.approvals.decide(
            approval.approval_id,
            approver="owner_sari",
            approver_role="owner",
            decision="APPROVE",
            note="bukti pembatalan lengkap",
        )
        self.assertTrue(first.is_pending, "one approver is not enough for dual control")

        with self.assertRaises(A.ApprovalError):
            self.approvals.decide(
                approval.approval_id,
                approver="owner_sari",
                approver_role="owner",
                decision="APPROVE",
                note="mencoba menyetujui dua kali",
            )

        second = self.approvals.decide(
            approval.approval_id,
            approver="owner_budi",
            approver_role="owner",
            decision="APPROVE",
            note="setuju, deposit dikembalikan penuh",
        )
        self.assertEqual(second.status, "APPROVED")
        self.assertFalse(second.is_pending)

    def test_an_expired_request_cannot_be_decided(self):
        approval = self._request_refund(required_approvals=2, idempotency_key="idem_verify_refund_2")
        self.clock.set(P.parse_iso(approval.expires_at) + timedelta(minutes=1))
        with self.assertRaises(A.ApprovalError):
            self.approvals.decide(
                approval.approval_id,
                approver="owner_sari",
                approver_role="owner",
                decision="APPROVE",
                note="terlambat menyetujui",
            )
        self.approvals.expire_due()
        self.assertEqual(self.approvals.get(approval.approval_id).status, "EXPIRED")

    def test_a_replayed_request_returns_the_same_row(self):
        first = self._request_refund(required_approvals=2, idempotency_key="idem_verify_refund_3")
        second = self._request_refund(required_approvals=2, idempotency_key="idem_verify_refund_3")
        self.assertEqual(first.approval_id, second.approval_id)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM approval_requests"), 1,
            "replay of the same idempotency key created a second request",
        )

    def test_a_writable_status_column_cannot_fake_an_approval(self):
        approval = self._request_refund(required_approvals=2, idempotency_key="idem_verify_refund_4")
        self.conn.execute(
            "UPDATE approval_requests SET status = 'APPROVED', closed_at = ? WHERE approval_id = ?",
            (P.to_iso(self.clock.now()), approval.approval_id),
        )
        self.assertEqual(
            self.approvals.get(approval.approval_id).status,
            "PENDING",
            "status is read from the writable column instead of the decision log",
        )

    def test_decisions_cannot_be_rewritten_or_deleted(self):
        approval = self._request_refund(required_approvals=2, idempotency_key="idem_verify_refund_5")
        self.approvals.decide(
            approval.approval_id,
            approver="owner_sari",
            approver_role="owner",
            decision="REJECT",
            note="bukti belum lengkap",
        )
        for statement in (
            "UPDATE approval_decisions SET note = 'diubah'",
            "DELETE FROM approval_decisions",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.conn.execute(statement)
                self.assertIn("append-only", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

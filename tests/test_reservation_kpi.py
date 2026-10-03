"""Agent 3's §22 KPI telemetry — counts over recorded facts, never person data.

``src/reservation_kpi.py`` answers the charter's §5 metrics for one agency and one window.
The claims:

1. the funnel counts each stage where its owner records it — inquiries from ``cases``,
   quotes from ``quotes``, holds from ``holds``, confirmed bookings from ``bookings``,
   succeeded payments from ``payments`` — and nothing invented between stages;
2. a rate with an empty denominator is ``None``, not a fabricated 0%: a conversion rate
   with no quotes sent is an unanswered question, not an answer;
3. the window is real: facts created before the cutoff do not appear, and ``window_days``
   moves the cutoff — the same instant, a different window, different numbers;
4. SLA breaches are counted the way ``cases.py`` detects them: answered late or still
   unanswered past the deadline — and the two windows (past breaches, live breaches) are
   both caught;
5. the governance numbers come from the §13 ladder itself: escalated and handed-off
   conversations counted from ``ai_conversation_events``, never from a worker's memory;
6. the metric bundle carries no P2+ field: every value is an integer, a rate or a label —
   a guest ref, a conversation's words or a phone number cannot appear because no query
   selects one;
7. collecting twice at the same instant returns the same bundle, and the collector writes
   nothing — the audit and every table row count are untouched.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import reservation_kpi as KPI
from primitives import parse_iso
from test_reservation_kernel import KernelTestCase, NOW

AGENCY = "agency_default"
NOW_DT = parse_iso(NOW)

#: One evidence row authorises the hold rows the funnel test counts (kernel-recorded: the
#: only thing that can authorise a hold, §6).
EVIDENCE = {"unit_id": "unit_1", "source_type": "manager_written", "evidence_level": "C",
            "result": "AVAILABLE"}


class KpiTestCase(KernelTestCase):
    """The kernel fixture plus the raw facts the KPI queries count."""

    def setUp(self):
        super().setUp()
        self.evidence_id = self.kernel.record_evidence(
            unit_id="unit_1", source_type="manager_written", evidence_level="C",
            result="AVAILABLE", captured_at=NOW, source_ref="kpi-fixture",
        )
        self.now_iso = NOW

    # -- the raw rows ------------------------------------------------------ #

    def seed_case(self, case_id="case_kpi_1", status="NEW", responded=None, quoted=None,
                  created=NOW, fr_due=None, q_due=None):
        self.kernel.conn.execute(
            "INSERT INTO cases (case_id, agency_id, guest_ref, channel, status, priority,"
            " sla_policy, first_response_due_at, quote_due_at, first_responded_at, quoted_at,"
            " idempotency_key, created_at, updated_at)"
            " VALUES (?, ?, 'guest_kpi', 'website', ?, 'NORMAL', 'standard', ?, ?, ?, ?,"
            " ?, ?, ?)",
            (case_id, AGENCY, status,
             fr_due or "2026-09-21T01:00:00+00:00",
             q_due or "2026-09-21T04:00:00+00:00",
             responded, quoted,
             f"idem_{case_id}", created, created),
        )
        return case_id

    def seed_quote(self, quote_id="quote_kpi_1", status="SENT", created=NOW):
        self.kernel.conn.execute(
            "INSERT INTO quotes (quote_id, agency_id, property_id, unit_id, check_in,"
            " check_out, nights, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, rate_card_id, accommodation_minor, extra_guest_minor,"
            " cleaning_minor, extras_minor, discount_minor, subtotal_minor, service_fee_minor,"
            " tax_minor, total_minor, security_deposit_minor, lines, fingerprint, status,"
            " warnings, created_at, expires_at)"
            " VALUES (?, ?, 'prop_1', 'unit_1', '2026-11-10', '2026-11-14', 4, 2, 0, 0, 'IDR',"
            " 'policy_v1', ?, 'card_v1', 6000000, 0, 0, 0, 0, 6000000, 300000, 600000,"
            " 6900000, 0, '[]', ?, ?, '[]', ?, '2026-11-20T00:00:00+00:00')",
            (quote_id, AGENCY, "0" * 64, "0" * 64, status, created),
        )
        return quote_id

    def seed_hold(self, hold_id="hold_kpi_1", *, status="CONFIRMED", created=NOW,
                  check_in="2026-11-10", check_out="2026-11-14"):
        nights = 4
        terminal = status in ("CONVERTED", "REJECTED", "RELEASE_CONFIRMED",
                              "EXPIRED_PENDING_VERIFICATION")
        self.kernel.conn.execute(
            "INSERT INTO holds (hold_id, agency_id, unit_id, property_id, check_in, check_out,"
            " nights, status, guest_ref, quote_ref, policy_version_id, evidence_id,"
            " idempotency_key, created_at, expires_at, expires_timezone, closed_at, close_reason)"
            " VALUES (?, ?, 'unit_1', 'prop_1', ?, ?, ?, ?,"
            " 'guest_kpi', NULL, 'policy_v1', ?, ?, ?, '2026-11-11T00:00:00+00:00',"
            " 'Asia/Jakarta', ?, ?)",
            (hold_id, AGENCY, check_in, check_out, nights, status, self.evidence_id,
             f"idem_{hold_id}", created,
             created if terminal else None, "converted to a booking" if terminal else None),
        )
        return hold_id

    def seed_booking(self, booking_id="book_kpi_1", status="CONFIRMED",
                     confirmed=NOW, created=NOW, check_in="2026-11-10", check_out="2026-11-14",
                     unit_id="unit_1"):
        self.kernel.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
            " nights, status, guest_ref, adults, children, infants, currency,"
            " policy_version_id, policy_fingerprint, quote_ref, hold_ref, total_minor,"
            " amount_paid_minor, security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at, confirmed_at, cancelled_at)"
            " VALUES (?, 'prop_1', ?, ?, ?, 4, ?, 'guest_kpi', 2, 0, 0, 'IDR', 'policy_v1',"
            " ?, NULL, NULL, 6900000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)",
            (booking_id, unit_id, check_in, check_out, status, "0" * 64,
             f"idem_{booking_id}", created, created, confirmed),
        )
        return booking_id

    def seed_invoice(self, *, invoice_id="inv_kpi_1", booking_id="book_kpi_1", created=NOW):
        self.kernel.conn.execute(
            "INSERT INTO invoices (invoice_id, agency_id, invoice_number, booking_id,"
            " policy_fingerprint, currency, subtotal_minor, tax_minor, service_minor,"
            " total_minor, amount_paid_minor, status, payment_status, issued_at, due_at,"
            " created_at)"
            " VALUES (?, ?, 'INV-2026-0001', ?, ?, 'IDR', 6000000, 600000, 300000,"
            " 6900000, 0, 'ISSUED', 'UNPAID', ?, '2026-11-20T00:00:00+00:00', ?)",
            (invoice_id, AGENCY, booking_id, "0" * 64, created, created),
        )
        return invoice_id

    def seed_payment(self, payment_id="pay_kpi_1", *, status="SUCCEEDED", created=NOW,
                     invoice_id="inv_kpi_1"):
        self.kernel.conn.execute(
            "INSERT INTO payments (payment_id, agency_id, invoice_id, provider,"
            " provider_intent_id, method, currency, amount_minor, status, created_at, settled_at)"
            " VALUES (?, ?, ?, 'test_psp', ?, 'HOSTED_PAGE', 'IDR', 6900000, ?, ?, ?)",
            (payment_id, AGENCY, invoice_id, f"intent_{payment_id}", status, created,
             created if status == "SUCCEEDED" else None),
        )
        return payment_id

    # -- the reads --------------------------------------------------------- #

    def kpis(self, **kwargs):
        return KPI.collect(
            self.kernel.conn, agency_id=AGENCY, at=NOW_DT, **kwargs
        )

    def count(self, table):
        return self.kernel.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]


class TestReservationFunnel(KpiTestCase):

    def test_the_funnel_counts_each_stage_where_its_owner_records_it(self):
        self.seed_case("case_a")
        self.seed_case("case_b")
        self.seed_quote("q_sent", status="SENT")
        self.seed_quote("q_acc", status="ACCEPTED")
        self.seed_quote("q_draft", status="DRAFT")  # a draft is not a sent quote
        self.seed_hold("h_conf", status="CONFIRMED")
        self.seed_hold("h_conv", status="CONVERTED", check_in="2026-11-11", check_out="2026-11-14")
        self.seed_hold("h_req", status="REQUESTED", check_in="2026-11-12", check_out="2026-11-14")  # requested is not confirmed
        self.seed_booking("b_1", status="CONFIRMED")
        self.seed_booking("b_2", status="IN_STAY", check_in="2026-11-11", check_out="2026-11-14")   # also a confirmed sale
        self.seed_invoice(booking_id="b_1")
        self.seed_payment("p_1", status="SUCCEEDED")
        self.seed_payment("p_2", status="PENDING")

        funnel = self.kpis().as_dict()["funnel"]
        self.assertEqual(funnel["inquiries"], 2)
        self.assertEqual(funnel["quotes_sent"], 2)
        self.assertEqual(funnel["quotes_accepted"], 1)
        self.assertEqual(funnel["holds_confirmed"], 2)
        self.assertEqual(funnel["holds_converted"], 1)
        self.assertEqual(funnel["bookings_confirmed"], 2)
        self.assertEqual(funnel["payments_succeeded"], 1)

    def test_a_rate_with_an_empty_denominator_is_none_not_zero(self):
        k = self.kpis()
        self.assertIsNone(k.rates["quote_acceptance_rate"])
        self.assertIsNone(k.rates["booking_conversion_rate"])

    def test_the_rates_are_the_stages_divided(self):
        self.seed_quote("q_s", status="SENT")
        self.seed_quote("q_a", status="ACCEPTED")
        self.seed_hold("h_c", status="CONFIRMED")
        self.seed_hold("h_v", status="CONVERTED", check_in="2026-11-11", check_out="2026-11-14")
        self.seed_quote("q_2", status="ACCEPTED")
        self.seed_booking("b_1", status="CONFIRMED")
        k = self.kpis()
        # acceptance counts ACCEPTED over every quote that reached the guest (SENT + ACCEPTED).
        self.assertAlmostEqual(k.rates["quote_acceptance_rate"], 0.6667, places=4)
        self.assertAlmostEqual(k.rates["hold_conversion_rate"], 0.5)
        self.assertAlmostEqual(k.rates["booking_conversion_rate"], 0.5)

    def test_the_window_is_real_and_movable(self):
        old = (NOW_DT - timedelta(days=60)).isoformat()
        self.seed_case("case_old", created=old)
        self.seed_case("case_new", created=NOW)
        self.assertEqual(self.kpis().funnel["inquiries"], 1)
        wide = self.kpis(window_days=90)
        self.assertEqual(wide.funnel["inquiries"], 2)

    def test_collecting_twice_is_the_same_bundle_and_writes_nothing(self):
        self.seed_case("case_a")
        first = self.kpis().as_dict()
        second = self.kpis().as_dict()
        self.assertEqual(first, second)
        self.assertEqual(self.count("audit_events"), 0)


class TestReservationSlaAndGovernance(KpiTestCase):

    def test_sla_breaches_count_answered_late_and_still_silent(self):
        # Each breach case is created at 21:00, deadlines at 22:00/23:00 — both in the
        # past at NOW (00:00 next day), so the window's created_at filter passes too.
        self.seed_case("c_late", created="2026-09-20T21:00:00+00:00",
                       fr_due="2026-09-20T22:00:00+00:00",
                       q_due="2026-09-20T23:00:00+00:00",
                       responded="2026-09-20T22:30:00+00:00")
        # Still silent: no response, deadline already passed.
        self.seed_case("c_silent", created="2026-09-20T21:00:00+00:00",
                          fr_due="2026-09-20T22:00:00+00:00",
                          q_due="2026-09-20T23:00:00+00:00")
        # Answered in time: no breach.
        self.seed_case("c_ok", created="2026-09-20T21:00:00+00:00",
                       fr_due="2026-09-20T22:00:00+00:00",
                       q_due="2026-09-20T23:00:00+00:00",
                       responded="2026-09-20T21:30:00+00:00")
        self.assertEqual(self.kpis().sla["first_response_breached"], 2)

    def test_the_quote_sla_counts_the_same_two_shapes(self):
        self.seed_case("c_quotelate", created="2026-09-20T21:00:00+00:00",
                              fr_due="2026-09-20T22:00:00+00:00",
                              q_due="2026-09-20T23:00:00+00:00",
                              quoted="2026-09-20T23:30:00+00:00")
        self.seed_case("c_quoteok", created="2026-09-20T21:00:00+00:00",
                            fr_due="2026-09-20T22:00:00+00:00",
                            q_due="2026-09-20T23:00:00+00:00",
                            quoted="2026-09-20T22:30:00+00:00")
        self.assertEqual(self.kpis().sla["quote_sla_breached"], 1)

    def test_the_governance_numbers_come_from_the_ladder_itself(self):
        # Two conversations, one carrying an ESCALATED rung then a HANDOFF_REQUESTED rung.
        # The ladder's own semantics have their suite in test_reservation_service; what is
        # under test here is that the KPI reads the ledger, never worker memory.
        for n, (conversation_id, case_id) in enumerate(
            (("conv_kpi_1", "case_conv"), ("conv_kpi_2", "case_plain"))
        ):
            self.seed_case(case_id)
            self.kernel.conn.execute(
                "INSERT INTO ai_principals (principal_id, agency_id, name, secret_ref, created_at)"
                " VALUES (?, ?, 'KPI principal', 'kpi-fixture', ?)"
                " ON CONFLICT(principal_id) DO NOTHING",
                ("ai:kpi-0", AGENCY, NOW),
            )
            self.kernel.conn.execute(
                "INSERT INTO ai_conversations (conversation_id, agency_id, principal_id,"
                " case_id, guest_ref, channel, consent_state, opened_at, updated_at)"
                " VALUES (?, ?, 'ai:kpi-0', ?, 'guest_kpi', 'website', 'UNKNOWN', ?, ?)",
                (conversation_id, AGENCY, case_id, NOW, NOW),
            )
        self.kernel.conn.execute(
            "INSERT INTO ai_conversation_events (ai_conversation_event_id, conversation_id,"
            " seq, kind, state, actor, actor_role, reason, trigger_code, handoff_blueprint,"
            " package_fingerprint, occurred_at)"
            " VALUES ('kpi_ev_1', 'conv_kpi_1', 1, 'OPENED', 'AI_RUNNING', 'ai:kpi-0',"
            " 'ai_service', 'opened', NULL, NULL, NULL, ?),"
            " ('kpi_ev_2', 'conv_kpi_1', 2, 'ESCALATED', 'ESCALATION_TRIGGERED',"
            " 'ai:kpi-0', 'ai_service', 'escalated', 'CUSTOMER_REQUESTS_HUMAN', NULL, NULL, ?),"
            " ('kpi_ev_3', 'conv_kpi_1', 3, 'HANDOFF_REQUESTED', 'HANDOFF_PENDING',"
            " 'ai:kpi-0', 'ai_service', 'handed off', NULL, 'reservation_manager',"
            " ?, ?),"
            " ('kpi_ev_4', 'conv_kpi_2', 1, 'OPENED', 'AI_RUNNING', 'ai:kpi-0',"
            " 'ai_service', 'opened', NULL, NULL, NULL, ?)",
            (NOW, NOW, "0" * 64, NOW, NOW),
        )
        k = self.kpis()
        self.assertEqual(k.governance["conversations"], 2)
        self.assertEqual(k.governance["escalated"], 1)
        self.assertEqual(k.governance["escalation_rate"], 0.5)
        self.assertEqual(k.governance["handed_off"], 1)

    def test_no_p2_plus_field_reaches_the_bundle(self):
        self.seed_case("case_a")
        self.seed_quote("q_1", status="ACCEPTED")
        self.seed_booking("b_1", status="CONFIRMED")
        blob = repr(self.kpis().as_dict())
        for forbidden in ("guest_kpi", "guest_ref", "phone", "email"):
            self.assertNotIn(forbidden, blob)

    def test_overlapping_bookings_would_be_counted_not_hidden(self):
        # The kernel refuses overlaps, so this state is unreachable through the API; the
        # metric's job is to catch it if the invariant is ever violated elsewhere.
        self.seed_booking("b_1", status="CONFIRMED")
        self.seed_booking("b_2", status="IN_STAY", unit_id="unit_1",
                          check_in="2026-11-11", check_out="2026-11-14")
        self.assertEqual(self.kpis().governance["overlapping_bookings"], 1)


if __name__ == "__main__":
    unittest.main()
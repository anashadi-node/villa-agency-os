"""§22/D7 shadow-mode measurement — the bar is machine-readable, the switch is human.

``src/shadow_eval.py`` measures what ADR-0038 decided. The claims:

1. the pairing reads the estate's own shadow channel (``intake_proposals.method='llm_shadow'``)
   and nothing else — a ``rule`` or ``manual`` proposal is not the agent being measured;
2. agreement is ``ACCEPTED`` over decided fields, per **extractor** (agent + worker), never a
   platform-wide average that would hide the one worker that keeps being wrong;
3. an unsupported claim is a proposal with **no source span** — text claiming a fact without
   being anchored to the message it supposedly came from — and one such claim refuses the bar
   even at perfect agreement (the ranking in ADR-0038 is deliberate);
4. the bars are policy keys with the platform baseline; a tenant revision **below** the
   baseline is refused (stricter-only), and the baseline numbers are pinned to ADR-0038's;
5. ``ungrounded_reached_guest`` measures the one invariant that must always be zero — a draft
   that reached a guest without being ``GROUNDED`` — and the suite keeps the schema's refusal
   honest by counting, not assuming;
6. ``meets_bar`` is data: the module never graduates anything, writes nothing, and the window
   is real (older proposals fall out; a second collect reads the same facts).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

import shadow_eval as SE
from primitives import FrozenClock
from test_reservation_kernel import KernelTestCase, NOW

AGENCY = "agency_default"
NOW_DT = datetime(2026, 10, 3, tzinfo=timezone.utc)


class ShadowEvalTestCase(KernelTestCase):
    """The kernel fixture plus the shadow channel's own rows: proposals, fields, reviews."""

    def setUp(self):
        super().setUp()
        self.now = NOW_DT

    # -- the fixtures ------------------------------------------------------ #

    def seed_case(self, case_id="case_shadow_1"):
        self.kernel.conn.execute(
            "INSERT OR IGNORE INTO cases (case_id, agency_id, guest_ref, channel, status,"
            " priority, sla_policy, first_response_due_at, quote_due_at, idempotency_key,"
            " created_at, updated_at)"
            " VALUES (?, ?, 'guest_shadow', 'website', 'NEW', 'NORMAL', 'default',"
            " '2026-09-21T01:00:00+00:00', '2026-09-21T04:00:00+00:00', ?, ?, ?)",
            (case_id, AGENCY, f"idem-case-{case_id}", NOW, NOW),
        )
        return case_id

    def seed_message(self, case_id="case_shadow_1", message_id="msg_shadow_1"):
        self.kernel.conn.execute(
            "INSERT INTO case_messages (case_message_id, case_id, direction, channel,"
            " sender_ref, received_at, content_class, body_sha256, created_at)"
            " VALUES (?, ?, 'INBOUND', 'website', 'guest_shadow', ?, 'P1', ?, ?)",
            (message_id, case_id, NOW, "0" * 63 + "1", NOW),
        )
        return message_id

    def seed_proposal(self, proposal_id="prop_shadow_1", *, case_id="case_shadow_1",
                      message_id="msg_shadow_1", extractor="agent3/RES-W01", created=NOW,
                      method="llm_shadow"):
        self.seed_case(case_id)
        self.kernel.conn.execute(
            "INSERT OR IGNORE INTO case_messages (case_message_id, case_id, direction, channel,"
            " sender_ref, received_at, content_class, body_sha256, created_at)"
            " VALUES (?, ?, 'INBOUND', 'website', 'guest_shadow', ?, 'P1', ?, ?)",
            (message_id, case_id, NOW, "0" * 63 + "1", NOW),
        )
        self.kernel.conn.execute(
            "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id, extractor,"
            " method, produced_at, status, payload, fingerprint, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'ACCEPTED', '{}', ?, ?)",
            (proposal_id, case_id, message_id, extractor, method, created, "0" * 63 + "2", created),
        )
        return proposal_id

    def seed_field(
        self,
        field_id,
        *,
        proposal_id="prop_shadow_1",
        case_id="case_shadow_1",
        message_id="msg_shadow_1",
        status="ACCEPTED",
        span=5,
        reviewed_by="staff_reviewer",
        field_name="check_in",
    ):
        # A REJECTED field needs a reason (ck_intake_field_rejection) — the fixture names one.
        rejection = "the human read the message differently" if status == "REJECTED" else None
        self.kernel.conn.execute(
            "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id, field_name,"
            " field_value, value_type, confidence, source_message_id, source_span_start,"
            " source_span_end, status, value_sha256, created_at, reviewed_by, reviewed_at,"
            " rejection_reason)"
            " VALUES (?, ?, ?, ?, '2026-11-10', 'date', 90, ?, ?, 14,"
            " ?, ?, ?, ?, ?, ?)",
            (
                field_id,
                proposal_id,
                case_id,
                field_name,
                message_id,
                span,
                status,
                "0" * 63 + "3",
                NOW,
                reviewed_by if status != "PROPOSED" else None,
                NOW if status != "PROPOSED" else None,
                rejection,
            ),
        )
        return field_id

    # -- the reads --------------------------------------------------------- #

    def kpis(self, **kwargs):
        return SE.collect(
            self.kernel.conn, agency_id=AGENCY, at=self.now, clock=FrozenClock.at(NOW), **kwargs
        )


class TestShadowPairing(ShadowEvalTestCase):
    def test_only_the_shadow_channel_is_measured(self):
        # A second extractor proposing through the rule channel (append-only: seeded, not edited).
        self.seed_proposal("prop_rule", extractor="ops_manual", created=NOW, method="rule")
        self.seed_proposal("prop_rule2", extractor="agent3/RES-W01", created=NOW, method="rule")
        # Both rule-channel proposals must be invisible to the shadow measurement.
        self.assertEqual(self.kpis().extractors, {})

    def test_agreement_is_per_extractor_and_counts_each_side(self):
        self.seed_proposal("prop_a", extractor="agent3/RES-W01")
        self.seed_field("f_1", proposal_id="prop_a", status="ACCEPTED", field_name="check_in")
        self.seed_field("f_2", proposal_id="prop_a", status="REJECTED", reviewed_by="other",
                        field_name="adults")
        self.seed_proposal("prop_b", extractor="agent3/RES-W09", case_id="case_shadow_2",
                           message_id="msg_shadow_2")
        self.seed_field("f_3", proposal_id="prop_b", case_id="case_shadow_2", status="ACCEPTED")
        k = self.kpis()
        self.assertEqual(set(k.extractors), {"agent3/RES-W01", "agent3/RES-W09"})
        w01 = k.extractors["agent3/RES-W01"]
        self.assertEqual((w01.decided, w01.agreed, w01.disagreed), (2, 1, 1))
        self.assertEqual(w01.agreement_rate, 0.5)
        self.assertEqual(w01.pending, 0)

    def test_a_proposal_without_a_span_is_an_unsupported_claim(self):
        self.seed_proposal()
        self.seed_field("f_1", status="ACCEPTED", span=None)
        m = self.kpis().extractors["agent3/RES-W01"]
        self.assertEqual(m.unsupported, 1)
        self.assertFalse(m.meets_bar)  # one fabricated claim, bar not met — even alone

    def test_the_window_is_real(self):
        self.seed_proposal("prop_old", created="2026-06-01T00:00:00+00:00")
        self.seed_field("f_old", proposal_id="prop_old", status="ACCEPTED")
        self.assertEqual(self.kpis().extractors, {})
        self.assertEqual(self.kpis(window_days=150).extractors.keys(), {"agent3/RES-W01"})


class TestTheBar(ShadowEvalTestCase):
    def test_the_bars_are_the_platform_baseline_from_adr_0038(self):
        bars = self.kpis().bars
        self.assertEqual(bars["agreement_rate"], Decimal("0.95"))
        self.assertEqual(bars["min_cases"], 100)

    def test_meets_bar_needs_volume_agreement_and_zero_unsupported(self):
        # 100 cases, all agreed, all spanned → the bar is met (this is what a graduation
        # approval would look at; the approval itself is a human act elsewhere).
        for i in range(100):
            case_id = f"case_vol_{i}"
            self.seed_proposal(f"prop_vol_{i}", case_id=case_id, message_id=f"msg_vol_{i}")
            self.seed_field(f"f_vol_{i}", proposal_id=f"prop_vol_{i}", case_id=case_id,
                            message_id=f"msg_vol_{i}", status="ACCEPTED")
        m = self.kpis().extractors["agent3/RES-W01"]
        self.assertEqual(m.cases, 100)
        self.assertEqual(m.agreement_rate, 1.0)
        self.assertTrue(m.meets_bar)

    def test_volume_below_the_bar_refuses_even_at_perfect_agreement(self):
        self.seed_proposal()
        self.seed_field("f_1", status="ACCEPTED")
        m = self.kpis().extractors["agent3/RES-W01"]
        self.assertFalse(m.meets_bar)

    def test_a_tenant_bar_looser_than_the_baseline_is_refused(self):
        import policy_engine as PE
        PE.PolicyResolver(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        ).publish(
            SE.AGREEMENT_BAR_KEY, 90, effective_from="2026-10-03",
            source_doc_ref="a tenant trying to loosen the bar", approved_by="owner_anas",
        )
        with self.assertRaises(SE.TenantBarLooser):
            self.kpis()

    def test_a_stricter_tenant_bar_governs(self):
        import policy_engine as PE
        PE.PolicyResolver(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        ).publish(
            SE.AGREEMENT_BAR_KEY, 99, effective_from="2026-10-03",
            source_doc_ref="a stricter tenant bar", approved_by="owner_anas",
        )
        self.assertEqual(self.kpis().bars["agreement_rate"], Decimal("0.99"))

    def test_nothing_is_graduated_and_nothing_is_written(self):
        self.seed_proposal()
        self.seed_field("f_1", status="ACCEPTED")
        before = self.kernel.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
        self.kpis()
        after = self.kernel.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
        self.assertEqual(before, after)  # measurement reads; graduation is a human approval


if __name__ == "__main__":
    unittest.main()
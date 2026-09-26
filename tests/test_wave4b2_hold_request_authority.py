"""WAVE 4-B-2 — §56 authority on the hold *request*.

WAVE 4-B closed the hold *lifecycle* (expiry, extension, release). This closes
the hold *request*, which had no gate at all.

§56 lists "tentative-hold requests" as a medium-risk, policy-controlled action,
but ``request_hold()`` took no actor: the kernel could not gate it, and the row
it wrote named nobody. A machine actor could therefore take inventory out of
sale with no permission and no attribution — the one path in the §7 flow that
still had no enforcement point.

The gate runs before the idempotency lookup, exactly as the availability-request
gate does (ADR-0021): the permission is about *taking the slot*, so a replay is
gated too. Identity is a required argument, not an optional one, because a gate
that a caller can skip by omitting an argument is not a gate.
"""

from __future__ import annotations

import unittest

import ai_service as AIS
import policy_engine as PE
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-21T00:00:00+00:00"
#: A policy window is a calendar date; the resolver compares days, not instants.
EFFECTIVE_FROM = "2026-09-21"
AGENCY = "agency_default"
MACHINE_ACTOR = "ai:reservation_agent"
MACHINE_ROLE = "ai"
HUMAN_ACTOR = "user:owner-1"
HUMAN_ROLE = "owner"
ALLOWANCE = "security.ai.medium_tool_allowance"
HOLD_TOOL = "hold.request_tentative"
AVAILABILITY_TOOL = "availability.request"
SOURCE = "§56 AI Reservation Agent Autonomy (LOCKED) — medium-risk policy-controlled tools"


def make_policy():
    return PE.PolicyVersion(
        policy_version_id="policy_v1",
        property_id="prop_1",
        unit_scope="property",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=PE.PolicyRules(
            currency="IDR",
            deposit_pct="30",
            service_fee_pct="5",
            tax_pbjt_pct="10",
            max_discount_pct="15",
            quote_valid_hours=48,
            hold_ttl_minutes=720,
            max_hold_extensions=2,
            payload={
                "extra_guest_fee_minor": 250_000,
                "cleaning_fee_minor": 500_000,
                "security_deposit_minor": 2_000_000,
                "min_stay_nights": 2,
                "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
            },
        ),
        created_at=NOW,
        source_doc_ref="policy/2026-public.pdf",
        approved_by=HUMAN_ACTOR,
    )


class HoldAuthorityTestCase(unittest.TestCase):
    """One property, one unit, one policy, one registered machine principal."""

    def setUp(self):
        self.kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="w4b2"),
            agency_id=AGENCY,
        )
        self.addCleanup(self.kernel.close)
        self.kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
        self.kernel.upsert_unit(
            unit_id="unit_1", property_id="prop_1", name="Villa unit_1",
            base_capacity=2, max_guests=6,
        )
        self.kernel.insert_policy_version(make_policy().as_row())
        self.service = AIS.AiService(
            self.kernel.conn,
            policy=PE.PolicyResolver(self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY),
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="w4b2ai"),
            agency_id=AGENCY,
        )
        self.principal = self.service.register_principal(
            "Reservation Agent",
            secret_ref="secret_ref_reservation_agent",
            actor=HUMAN_ACTOR,
        )

    # -- helpers ----------------------------------------------------------- #
    def evidence(self):
        return self.kernel.record_evidence(
            unit_id="unit_1", source_type="property_api", evidence_level="A",
            result="AVAILABLE", ttl_hours=24,
        )

    def hold(self, *, actor, actor_role, principal_id=None, key="idem_hold_0001",
             check_in="2026-10-01", check_out="2026-10-03"):
        return self.kernel.request_hold(
            unit_id="unit_1",
            check_in=check_in,
            check_out=check_out,
            guest_ref="guest_abc123",
            policy_version_id="policy_v1",
            idempotency_key=key,
            evidence_id=self.evidence(),
            actor=actor,
            actor_role=actor_role,
            principal_id=principal_id,
        )

    def machine_hold(self, key="idem_hold_0001", **kwargs):
        return self.hold(
            actor=MACHINE_ACTOR, actor_role=MACHINE_ROLE,
            principal_id=self.principal.principal_id, key=key, **kwargs,
        )

    def publish(self, tools, *, effective_from=EFFECTIVE_FROM):
        PE.PolicyResolver(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        ).publish(
            ALLOWANCE, list(tools), effective_from=effective_from,
            source_doc_ref=SOURCE, approved_by=HUMAN_ACTOR,
        )

    def hold_count(self):
        return self.kernel.conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"]

    def audit_actions(self):
        rows = self.kernel.conn.execute(
            "SELECT action FROM audit_events ORDER BY seq"
        ).fetchall()
        return [row["action"] for row in rows]

    # -- the human path keeps working -------------------------------------- #
    def test_a_human_actor_may_request_a_hold_without_a_policy_value(self):
        outcome = self.hold(actor=HUMAN_ACTOR, actor_role=HUMAN_ROLE)
        self.assertEqual(outcome.status, "CONFIRMED")
        self.assertEqual(self.hold_count(), 1)

    # -- the machine path is deny-first ------------------------------------ #
    def test_a_machine_actor_is_refused_while_the_allowance_is_unpublished(self):
        with self.assertRaises(DomainError) as caught:
            self.machine_hold()
        self.assertEqual(caught.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.hold_count(), 0)

    def test_a_machine_actor_must_name_the_principal_it_acts_as(self):
        with self.assertRaises(DomainError) as caught:
            self.hold(actor=MACHINE_ACTOR, actor_role=MACHINE_ROLE)
        self.assertEqual(caught.exception.code, "MACHINE_PRINCIPAL_REQUIRED")
        self.assertEqual(self.hold_count(), 0)

    def test_a_refused_machine_request_takes_no_inventory(self):
        with self.assertRaises(DomainError):
            self.machine_hold()
        outcome = self.hold(actor=HUMAN_ACTOR, actor_role=HUMAN_ROLE, key="idem_hold_0002")
        self.assertEqual(outcome.status, "CONFIRMED")

    # -- ordering and precision -------------------------------------------- #
    def test_the_gate_runs_before_the_idempotency_lookup_so_a_replay_is_gated_too(self):
        self.publish([HOLD_TOOL])
        self.machine_hold()
        self.assertEqual(self.hold_count(), 1)
        self.service.suspend(self.principal.principal_id, actor=HUMAN_ACTOR, reason="owner review")
        with self.assertRaises(DomainError) as caught:
            self.machine_hold()
        self.assertEqual(caught.exception.code, "AI_PRINCIPAL_NOT_ACTIVE")
        self.assertEqual(self.hold_count(), 1)

    def test_publishing_the_hold_tool_admits_a_machine_actor(self):
        self.publish([HOLD_TOOL])
        outcome = self.machine_hold()
        self.assertEqual(outcome.status, "CONFIRMED")
        self.assertEqual(self.hold_count(), 1)

    def test_allowing_the_availability_tool_alone_does_not_open_holds(self):
        self.publish([AVAILABILITY_TOOL])
        with self.assertRaises(DomainError) as caught:
            self.machine_hold()
        self.assertEqual(caught.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.hold_count(), 0)

    def test_a_suspended_principal_is_refused_even_with_the_allowance(self):
        self.publish([HOLD_TOOL])
        self.service.suspend(self.principal.principal_id, actor=HUMAN_ACTOR, reason="owner review")
        with self.assertRaises(DomainError) as caught:
            self.machine_hold()
        self.assertEqual(caught.exception.code, "AI_PRINCIPAL_NOT_ACTIVE")
        self.assertEqual(self.hold_count(), 0)

    # -- the trace --------------------------------------------------------- #
    def test_a_refusal_leaves_an_audit_row_and_writes_no_hold(self):
        with self.assertRaises(DomainError):
            self.machine_hold()
        actions = self.audit_actions()
        self.assertIn("AI_TOOL_REFUSED", actions)
        self.assertNotIn("hold.requested", actions)

    def test_the_hold_audit_row_names_the_actor_who_asked(self):
        self.hold(actor=HUMAN_ACTOR, actor_role=HUMAN_ROLE)
        row = self.kernel.conn.execute(
            "SELECT actor FROM audit_events WHERE action = 'hold.requested'"
        ).fetchone()
        self.assertIsNotNone(row, "the hold request left no audit row")
        self.assertEqual(row["actor"], HUMAN_ACTOR)

    # -- the gate cannot be skipped by omission ---------------------------- #
    def test_identity_is_a_required_argument_so_the_gate_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            self.kernel.request_hold(
                unit_id="unit_1",
                check_in="2026-10-01",
                check_out="2026-10-03",
                guest_ref="guest_abc123",
                policy_version_id="policy_v1",
                idempotency_key="idem_hold_0001",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

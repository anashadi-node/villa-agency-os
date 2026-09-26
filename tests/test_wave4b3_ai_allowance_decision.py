"""WAVE 4-B-3 — the §56 allowance decision, pinned as executable statements.

The owner's decision of 2026-09-26 (§56): publish ``security.ai.medium_tool_allowance``
with **one** tool — ``availability.request``, the only §56 MEDIUM tool that has an
enforcement point today — and bound it to a window instead of to forever.

Four claims follow from that decision, and each is a test here rather than a sentence in
a document:

1. a fresh database ships **closed** — the schema seeds no permission, so deny-first is
   the default posture rather than an accident of who published what;
2. the published list opens exactly the tool it names, and nothing else;
3. the window closes the opening by itself: nobody has to remember;
4. withdrawing it is one revision, and the decision it replaced stays readable;
5. a deny-registry action stays denied even under a maximally permissive allowance.

Claim 3 is tested on a **shared file database read by two clocks**, not by publishing a
window in the past: that is what "time passed" actually looks like, and it does not
depend on the rules for backdated revisions.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ai_service as AIS
import policy_engine as PE
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

AGENCY = "agency_default"
ALLOWANCE = "security.ai.medium_tool_allowance"
MACHINE_ACTOR = "ai:reservation_agent"
MACHINE_ROLE = "ai"
HUMAN_ACTOR = "user:owner-1"

#: The owner's window (decision of 2026-09-26): opens the next day, closes a month later.
WINDOW_FROM = "2026-09-27"
WINDOW_TO = "2026-10-26"
BEFORE_THE_WINDOW = "2026-09-26T00:00:00+00:00"
INSIDE_THE_WINDOW = "2026-10-01T00:00:00+00:00"
AFTER_THE_WINDOW = "2026-11-01T00:00:00+00:00"

OWNER_SOURCE = (
    "§56 AI Reservation Agent Autonomy (LOCKED) — medium-risk policy-controlled tools "
    "(owner decision 2026-09-26)"
)

#: Every §56 MEDIUM tool, so "opens one" can be checked against the whole tier.
MEDIUM_TOOLS = (
    "availability.request",
    "hold.request_tentative",
    "quotation.send",
    "request.coordinate_special",
    "comm.client_selected",
    "comm.supplier_selected",
)


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
        created_at=INSIDE_THE_WINDOW,
        source_doc_ref="policy/2026-public.pdf",
        approved_by=HUMAN_ACTOR,
    )


class AllowanceDecisionTestCase(unittest.TestCase):
    """One property, one unit, one policy, one registered machine principal."""

    def open_kernel(self, *, clock: str, seed: str, db_path: str | None = None):
        kernel = RK.ReservationKernel.open(
            db_path or ":memory:",
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=seed),
            agency_id=AGENCY,
        )
        self.addCleanup(kernel.close)
        return kernel

    def build(self, *, clock: str, seed: str, db_path: str | None = None):
        kernel = self.open_kernel(clock=clock, seed=seed, db_path=db_path)
        kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
        kernel.upsert_unit(
            unit_id="unit_1", property_id="prop_1", name="Villa unit_1",
            base_capacity=2, max_guests=6,
        )
        kernel.insert_policy_version(make_policy().as_row())
        service = AIS.AiService(
            kernel.conn,
            policy=PE.PolicyResolver(kernel.conn, clock=FrozenClock.at(clock), agency_id=AGENCY),
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}ai"),
            agency_id=AGENCY,
        )
        principal = service.register_principal(
            "Reservation Agent",
            secret_ref="secret_ref_reservation_agent",
            actor=HUMAN_ACTOR,
        )
        return kernel, service, principal

    # -- helpers ----------------------------------------------------------- #
    def publish(self, kernel, tools, *, clock, effective_from=WINDOW_FROM,
                effective_to=WINDOW_TO):
        return PE.PolicyResolver(
            kernel.conn, clock=FrozenClock.at(clock), agency_id=AGENCY
        ).publish(
            ALLOWANCE, list(tools), effective_from=effective_from,
            effective_to=effective_to, source_doc_ref=OWNER_SOURCE,
            approved_by=HUMAN_ACTOR,
        )

    def ask(self, kernel, *, actor, actor_role, principal_id=None, key="idem_check_0001"):
        return kernel.request_availability_check(
            unit_id="unit_1", check_in="2026-10-01", check_out="2026-10-03",
            actor=actor, actor_role=actor_role, principal_id=principal_id,
            idempotency_key=key,
        )

    def machine_ask(self, kernel, principal, key="idem_check_0001"):
        return self.ask(
            kernel, actor=MACHINE_ACTOR, actor_role=MACHINE_ROLE,
            principal_id=principal.principal_id, key=key,
        )

    def check_count(self, kernel):
        return kernel.conn.execute(
            "SELECT COUNT(*) AS n FROM availability_checks"
        ).fetchone()["n"]

    def revisions(self, kernel):
        rows = kernel.conn.execute(
            "SELECT version FROM policy_revisions WHERE policy_key = ? ORDER BY version",
            (ALLOWANCE,),
        ).fetchall()
        return [row["version"] for row in rows]

    # -- 1. the default posture -------------------------------------------- #
    def test_a_fresh_database_ships_with_the_machine_path_closed(self):
        """The schema seeds values the platform needs to run; a permission is not one."""
        kernel, _service, principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3fresh")
        resolver = PE.PolicyResolver(
            kernel.conn, clock=FrozenClock.at(INSIDE_THE_WINDOW), agency_id=AGENCY
        )
        with self.assertRaises(PE.PolicyValueNotPublished) as caught:
            resolver.resolve(ALLOWANCE)
        self.assertEqual(caught.exception.code, "POLICY_VALUE_NOT_PUBLISHED")
        with self.assertRaises(DomainError) as refused:
            self.machine_ask(kernel, principal)
        self.assertEqual(refused.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.check_count(kernel), 0)

    def test_a_human_actor_is_admitted_without_the_allowance(self):
        """The allowance governs machines; a person asking a source needs no policy value."""
        kernel, _service, _principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3human")
        self.ask(kernel, actor=HUMAN_ACTOR, actor_role="owner")
        self.assertEqual(self.check_count(kernel), 1)

    # -- 2. the published list means what it says --------------------------- #
    def test_the_decision_opens_the_tool_it_names(self):
        kernel, _service, principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3open")
        self.publish(kernel, ["availability.request"], clock=INSIDE_THE_WINDOW)
        self.machine_ask(kernel, principal)
        self.assertEqual(self.check_count(kernel), 1)

    def test_the_other_five_medium_tools_stay_refused(self):
        kernel, service, principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3five")
        self.publish(kernel, ["availability.request"], clock=INSIDE_THE_WINDOW)
        others = [tool for tool in MEDIUM_TOOLS if tool != "availability.request"]
        self.assertEqual(len(others), 5)
        for tool in others:
            with self.subTest(tool=tool):
                with self.assertRaises(DomainError) as refused:
                    service.authorize(principal.principal_id, tool)
                self.assertEqual(refused.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")

    # -- 3. the window bounds it ------------------------------------------- #
    def test_the_window_closes_the_opening_by_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "shared.db")
            inside, _service, principal = self.build(
                clock=INSIDE_THE_WINDOW, seed="w4b3window", db_path=db_path
            )
            self.publish(inside, ["availability.request"], clock=INSIDE_THE_WINDOW)
            self.machine_ask(inside, principal)
            self.assertEqual(self.check_count(inside), 1)

            later = self.open_kernel(
                clock=AFTER_THE_WINDOW, seed="w4b3later", db_path=db_path
            )
            resolver = PE.PolicyResolver(
                later.conn, clock=FrozenClock.at(AFTER_THE_WINDOW), agency_id=AGENCY
            )
            with self.assertRaises(PE.PolicyValueNotPublished):
                resolver.resolve(ALLOWANCE)
            with self.assertRaises(DomainError) as refused:
                self.machine_ask(later, principal, key="idem_check_0002")
            self.assertEqual(refused.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
            self.assertEqual(self.check_count(later), 1)

    def test_the_day_before_the_window_the_machine_path_is_still_closed(self):
        kernel, _service, principal = self.build(clock=BEFORE_THE_WINDOW, seed="w4b3before")
        self.publish(kernel, ["availability.request"], clock=BEFORE_THE_WINDOW)
        with self.assertRaises(DomainError) as refused:
            self.machine_ask(kernel, principal)
        self.assertEqual(refused.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")

    # -- 4. withdrawal ------------------------------------------------------ #
    def test_withdrawal_closes_it_again_and_the_decision_stays_readable(self):
        kernel, _service, principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3revoke")
        self.publish(kernel, ["availability.request"], clock=INSIDE_THE_WINDOW)
        self.machine_ask(kernel, principal)
        self.assertEqual(self.check_count(kernel), 1)

        self.publish(kernel, [], clock=INSIDE_THE_WINDOW)
        with self.assertRaises(DomainError) as refused:
            self.machine_ask(kernel, principal, key="idem_check_0002")
        self.assertEqual(refused.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.check_count(kernel), 1)
        self.assertEqual(self.revisions(kernel), [1, 2])

    # -- 5. the allowance is not a master key ------------------------------- #
    def test_a_denied_action_stays_denied_under_a_permissive_allowance(self):
        kernel, service, principal = self.build(clock=INSIDE_THE_WINDOW, seed="w4b3deny")
        self.publish(kernel, list(MEDIUM_TOOLS), clock=INSIDE_THE_WINDOW)
        with self.assertRaises(DomainError) as refused:
            service.authorize(principal.principal_id, "payment.execute")
        self.assertEqual(refused.exception.code, "AI_DENIED_ACTION")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

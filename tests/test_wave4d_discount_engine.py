"""WAVE 4-D — the discount engine (§11 Pricing/Quote/Discount, §16 policy, §15 approval, §21 deny).

§11 requires a discount to be: rule evaluation + authority + margin guard + approval + audit,
with supplier-funded and agency-funded discounts kept apart, and a margin floor that carries
**both** a percentage and an absolute guard whose override needs human authority.

What these tests pin, and why:

* the two funding kinds are separated — a supplier-funded discount is the supplier's money and
  must not consume agency margin; an agency-funded one does;
* the authority list is read from published policy (`commercial.discount.authority_roles`), never
  from a literal in this module, and an unpublished list refuses everybody (§16: thresholds are
  configuration, not hard-coded);
* the margin floor is read from published policy as a pair — `commercial.margin_floor_pct` and
  `commercial.margin_floor_minor` — and an unpublished floor refuses rather than defaulting;
* a breach is not applied and not self-approved: the engine can only *ask* a human (§15), and it
  exposes no method that answers for one (§21 denies `discount.approve` / `margin_override.approve`
  to AI);
* every outcome — applied, refused, override requested — is audited.
"""

from __future__ import annotations

import json
import unittest

import approvals as AP
import discounts as DE
import policy_engine as PE
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory, canonical_json

AGENCY = "agency_default"
AUTHORITY_KEY = DE.AUTHORITY_KEY
FLOOR_PCT_KEY = DE.FLOOR_PCT_KEY
FLOOR_MINOR_KEY = DE.FLOOR_MINOR_KEY

NOW = "2026-09-26T08:00:00+00:00"
WINDOW_FROM = "2026-09-01"
WINDOW_TO = "2026-12-31"
SOURCE = "§11 Pricing, Quote & Discount — owner decision recorded for the pilot"

OWNER = "human_owner_anas"
SUPERVISOR = "human_manager_dewi"
AGENT = "human_agent_rina"

#: A booking that sells for 100,000,000 with 30,000,000 of agency margin in it.
REVENUE = 100_000_000
MARGIN_BEFORE = 30_000_000


class DiscountEngineHarness(unittest.TestCase):
    """One in-memory kernel per test, with policy published explicitly or deliberately withheld."""

    def build(self, *, at: str = NOW, seed: str = "w4d", authority=("owner", "manager"),
              floor_pct="20", floor_minor=1_000_000):
        kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(at),
            ids=IdFactory(deterministic=True, seed=seed),
            agency_id=AGENCY,
        )
        self.addCleanup(kernel.close)
        self.kernel = kernel
        self.conn = kernel.conn
        self.clock = FrozenClock.at(at)
        self.resolver = PE.PolicyResolver(self.conn, clock=self.clock, agency_id=AGENCY)
        if authority is not None:
            self.publish(AUTHORITY_KEY, canonical_json(list(authority)))
        if floor_pct is not None:
            self.publish(FLOOR_PCT_KEY, str(floor_pct))
        if floor_minor is not None:
            self.publish(FLOOR_MINOR_KEY, str(floor_minor))
        self.engine = DE.DiscountEngine(
            self.conn,
            clock=self.clock,
            ids=IdFactory(deterministic=True, seed=seed + "-engine"),
            agency_id=AGENCY,
        )
        return self.engine

    def publish(self, key: str, value: str) -> None:
        self.resolver.publish(
            key,
            value,
            effective_from=WINDOW_FROM,
            effective_to=WINDOW_TO,
            source_doc_ref=SOURCE,
            approved_by=OWNER,
        )

    def request(self, *, kind: str = "AGENCY_FUNDED", amount: int = 1_000_000,
                role: str = "owner", revenue=REVENUE, margin_before=MARGIN_BEFORE,
                currency: str = "IDR", reason: str = "returning guest, low season") -> DE.DiscountRequest:
        return DE.DiscountRequest(
            kind=kind,
            amount_minor=amount,
            currency=currency,
            reason=reason,
            actor=OWNER if role == "owner" else AGENT,
            actor_role=role,
            revenue_minor=revenue,
            margin_before_minor=margin_before,
            subject_ref="quote_w4d_0001",
        )

    def audit_actions(self) -> list:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT action FROM audit_events WHERE entity_type = ? ORDER BY rowid",
                (DE.ENTITY_TYPE,),
            )
        ]


# --------------------------------------------------------------------------------------
# §11 — the two funding kinds are separated
# --------------------------------------------------------------------------------------
class TestFundingKindsAreSeparated(DiscountEngineHarness):
    def test_the_two_kinds_the_spec_names_are_declared_and_distinct(self):
        self.assertEqual(
            tuple(DE.DISCOUNT_KINDS), ("SUPPLIER_FUNDED", "AGENCY_FUNDED")
        )
        self.assertEqual(len(set(DE.DISCOUNT_KINDS)), 2)

    def test_an_unknown_kind_is_refused_rather_than_assumed(self):
        self.build()
        with self.assertRaises(DE.DiscountKindInvalid) as caught:
            self.engine.evaluate(self.request(kind="MANAGER_GOODWILL"))
        self.assertEqual(caught.exception.code, "DISCOUNT_KIND_INVALID")

    def test_a_supplier_funded_discount_does_not_consume_agency_margin(self):
        """The supplier pays for it, so the agency's margin is untouched — that is the separation."""
        self.build()
        decision = self.engine.evaluate(self.request(kind="SUPPLIER_FUNDED", amount=5_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        self.assertEqual(decision.margin_before_minor, MARGIN_BEFORE)
        self.assertEqual(decision.margin_after_minor, MARGIN_BEFORE)

    def test_an_agency_funded_discount_comes_out_of_the_agency_margin(self):
        self.build()
        decision = self.engine.evaluate(self.request(kind="AGENCY_FUNDED", amount=5_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        self.assertEqual(decision.margin_after_minor, MARGIN_BEFORE - 5_000_000)

    def test_the_decision_says_which_party_paid_for_the_discount(self):
        self.build()
        agency = self.engine.evaluate(self.request(kind="AGENCY_FUNDED", amount=1_000_000))
        supplier = self.engine.evaluate(self.request(kind="SUPPLIER_FUNDED", amount=1_000_000))
        self.assertEqual(agency.kind, "AGENCY_FUNDED")
        self.assertEqual(supplier.kind, "SUPPLIER_FUNDED")


# --------------------------------------------------------------------------------------
# §11 + §16 — authority comes from published policy
# --------------------------------------------------------------------------------------
class TestAuthorityIsPolicy(DiscountEngineHarness):
    def test_a_role_inside_the_published_set_may_grant(self):
        self.build()
        decision = self.engine.evaluate(self.request(role="manager", amount=1_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)

    def test_a_role_outside_the_published_set_is_refused(self):
        self.build(authority=("owner",))
        decision = self.engine.evaluate(self.request(role="agent", amount=1_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_REFUSED)
        self.assertEqual(decision.code, "DISCOUNT_AUTHORITY_REFUSED")

    def test_the_refusal_names_the_roles_that_could_have_granted_it(self):
        self.build(authority=("owner", "accountant"))
        decision = self.engine.evaluate(self.request(role="agent", amount=1_000_000))
        self.assertEqual(tuple(decision.authority_roles), ("owner", "accountant"))

    def test_an_unpublished_authority_list_refuses_everybody(self):
        """Fail closed: no owner decision on record means no discount, not a guess."""
        self.build(authority=None)
        with self.assertRaises(DE.DiscountPolicyNotPublished) as caught:
            self.engine.evaluate(self.request(role="owner", amount=1_000_000))
        self.assertEqual(caught.exception.code, "DISCOUNT_POLICY_NOT_PUBLISHED")
        self.assertEqual(caught.exception.details["policy_key"], AUTHORITY_KEY)

    def test_behaviour_follows_the_store_not_the_module(self):
        """Publish a different list and the same request flips — proof the rule lives in policy."""
        self.build(authority=("owner",))
        refused = self.engine.evaluate(self.request(role="accountant", amount=1_000_000))
        self.assertEqual(refused.outcome, DE.OUTCOME_REFUSED)
        self.publish(AUTHORITY_KEY, canonical_json(["owner", "accountant"]))
        allowed = self.engine.evaluate(self.request(role="accountant", amount=1_000_000))
        self.assertEqual(allowed.outcome, DE.OUTCOME_APPLIED)

    def test_the_module_carries_no_role_list_of_its_own(self):
        """A literal role list in the engine would silently outlive an owner's change of mind."""
        for name in dir(DE):
            if name.startswith("_"):
                continue
            value = getattr(DE, name)
            if not isinstance(value, (list, tuple, set, frozenset)):
                continue
            lowered = {str(item).lower() for item in value}
            self.assertFalse(
                lowered & {"owner", "manager", "accountant", "agent"},
                f"{name} looks like a role list baked into the engine — it must read the store",
            )

    def test_the_override_goes_to_the_top_of_the_platform_role_vocabulary(self):
        """§11 asks for human authority; the rank comes from the platform's §18 vocabulary."""
        self.assertEqual(DE.override_role(), max(AP.ROLE_RANK, key=AP.ROLE_RANK.get))


# --------------------------------------------------------------------------------------
# §11 — the margin floor carries a percentage AND an absolute guard
# --------------------------------------------------------------------------------------
class TestMarginFloorGuards(DiscountEngineHarness):
    def test_a_discount_that_keeps_the_margin_above_both_floors_is_applied(self):
        # floor: 20% of 100,000,000 = 20,000,000, and 1,000,000 absolute.
        # margin after = 30,000,000 - 5,000,000 = 25,000,000 -> above both.
        self.build(floor_pct="20", floor_minor=1_000_000)
        decision = self.engine.evaluate(self.request(amount=5_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        self.assertEqual(decision.margin_after_minor, 25_000_000)

    def test_the_percentage_floor_alone_can_hold_a_discount(self):
        """9,000,000 off leaves 21,000,000 = 21% — still above 20%, so this one is allowed."""
        self.build(floor_pct="20", floor_minor=1_000_000)
        self.assertEqual(self.engine.evaluate(self.request(amount=9_000_000)).outcome,
                         DE.OUTCOME_APPLIED)

    def test_the_percentage_floor_binds_when_the_absolute_floor_is_generous(self):
        """11,000,000 off leaves 19,000,000 = 19% < 20%, though it is far above the absolute floor."""
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=11_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_REQUIRES_OVERRIDE)
        self.assertEqual(decision.code, "MARGIN_FLOOR_BREACHED")

    def test_the_absolute_floor_binds_when_the_percentage_floor_is_off(self):
        """With a 0% floor only the absolute guard is left: 29,500,000 off leaves 500,000."""
        self.build(floor_pct="0", floor_minor=1_000_000)
        decision = self.engine.evaluate(self.request(amount=29_500_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_REQUIRES_OVERRIDE)
        self.assertEqual(decision.code, "MARGIN_FLOOR_BREACHED")

    def test_exactly_on_the_percentage_floor_is_allowed(self):
        """A floor is a floor: landing on it is inside policy, one unit below is not."""
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=10_000_000))  # leaves exactly 20,000,000
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        self.assertEqual(decision.margin_after_minor, 20_000_000)

    def test_one_minor_unit_below_the_absolute_floor_is_refused(self):
        """29,000,001 off would leave 999,999 — one minor unit short of the absolute floor."""
        self.build(floor_pct="0", floor_minor=1_000_000)
        decision = self.engine.evaluate(self.request(amount=29_000_001))
        self.assertEqual(decision.outcome, DE.OUTCOME_REQUIRES_OVERRIDE)
        self.assertEqual(decision.code, "MARGIN_FLOOR_BREACHED")
        self.assertEqual(decision.margin_after_minor, MARGIN_BEFORE)      # nothing moved
        self.assertEqual(decision.margin_if_applied_minor, 999_999)       # what was asked for

    def test_the_decision_carries_the_floors_it_was_measured_against(self):
        self.build(floor_pct="20", floor_minor=1_000_000)
        decision = self.engine.evaluate(self.request(amount=1_000_000))
        self.assertEqual(str(decision.floor_pct), "20")
        self.assertEqual(decision.floor_minor, 1_000_000)

    def test_an_unpublished_percentage_floor_refuses_an_agency_funded_discount(self):
        self.build(floor_pct=None, floor_minor=1_000_000)
        with self.assertRaises(DE.DiscountPolicyNotPublished) as caught:
            self.engine.evaluate(self.request(amount=1_000_000))
        self.assertEqual(caught.exception.details["policy_key"], FLOOR_PCT_KEY)

    def test_an_unpublished_absolute_floor_refuses_an_agency_funded_discount(self):
        self.build(floor_pct="20", floor_minor=None)
        with self.assertRaises(DE.DiscountPolicyNotPublished) as caught:
            self.engine.evaluate(self.request(amount=1_000_000))
        self.assertEqual(caught.exception.details["policy_key"], FLOOR_MINOR_KEY)

    def test_a_supplier_funded_discount_needs_no_floor_at_all(self):
        """No agency money moves, so there is nothing for a margin floor to guard."""
        self.build(authority=("owner",), floor_pct=None, floor_minor=None)
        decision = self.engine.evaluate(self.request(kind="SUPPLIER_FUNDED", amount=9_000_000,
                                                     margin_before=None))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        self.assertIsNone(decision.margin_after_minor)

    def test_the_engine_refuses_to_invent_a_margin_basis(self):
        """The platform has no supplier cost yet, so the basis is an input, never a default."""
        self.build()
        with self.assertRaises(DE.MarginBasisRequired) as caught:
            self.engine.evaluate(self.request(margin_before=None))
        self.assertEqual(caught.exception.code, "MARGIN_BASIS_REQUIRED")

    def test_the_engine_refuses_to_invent_the_revenue_the_floor_is_a_share_of(self):
        self.build()
        with self.assertRaises(DE.MarginBasisRequired) as caught:
            self.engine.evaluate(self.request(revenue=None))
        self.assertEqual(caught.exception.code, "MARGIN_BASIS_REQUIRED")

    def test_a_negative_margin_basis_is_refused_as_input(self):
        self.build()
        with self.assertRaises(DomainError):
            self.engine.evaluate(self.request(margin_before=-1))


# --------------------------------------------------------------------------------------
# §11 + §15 — a breach is not applied and not self-approved
# --------------------------------------------------------------------------------------
class TestOverrideNeedsAHuman(DiscountEngineHarness):
    def test_a_breach_moves_no_money(self):
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=15_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_REQUIRES_OVERRIDE)
        self.assertEqual(decision.applied_minor, 0)
        self.assertEqual(decision.margin_after_minor, MARGIN_BEFORE)

    def test_the_engine_exposes_no_way_to_approve(self):
        """§21 denies discount.approve and margin_override.approve to AI: it may ask, not answer."""
        self.build()
        for name in dir(self.engine):
            if name.startswith("_"):
                continue
            self.assertFalse(
                name.startswith("approve"),
                f"DiscountEngine must not expose {name!r} — only a human may approve a discount",
            )

    def test_the_engine_can_raise_the_override_for_a_human_to_decide(self):
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=15_000_000))
        approval = self.engine.request_override(
            decision,
            requested_by=OWNER,
            requested_role="owner",
            reason="guest is a repeat client and the villa would otherwise sit empty",
        )
        self.assertEqual(approval.subject_type, "discount")
        self.assertEqual(approval.action, DE.OVERRIDE_ACTION)
        self.assertEqual(approval.status, "PENDING")
        row = self.conn.execute(
            "SELECT required_role, risk_level FROM approval_requests WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        self.assertEqual(row[0], "owner")
        self.assertEqual(row[1], "HIGH")

    def test_an_applied_discount_cannot_be_escalated(self):
        """There is nothing to override once the discount already fits inside policy."""
        self.build()
        decision = self.engine.evaluate(self.request(amount=1_000_000))
        self.assertEqual(decision.outcome, DE.OUTCOME_APPLIED)
        with self.assertRaises(DomainError):
            self.engine.request_override(
                decision, requested_by=OWNER, requested_role="owner", reason="just in case"
            )

    def test_the_requester_cannot_decide_their_own_override(self):
        """§15: separation of duties. The engine hands the request to someone else."""
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=15_000_000))
        approval = self.engine.request_override(
            decision, requested_by=OWNER, requested_role="owner", reason="repeat guest"
        )
        service = AP.ApprovalService(self.conn, clock=self.clock, agency_id=AGENCY)
        with self.assertRaises(AP.SelfApprovalForbidden):
            service.decide(
                approval.approval_id,
                approver=OWNER,
                approver_role="owner",
                decision="APPROVE",
                note="self-approval attempt",
            )

    def test_a_different_human_with_authority_can_decide_it(self):
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=15_000_000))
        approval = self.engine.request_override(
            decision, requested_by=AGENT, requested_role="owner", reason="repeat guest"
        )
        service = AP.ApprovalService(self.conn, clock=self.clock, agency_id=AGENCY)
        decided = service.decide(
            approval.approval_id,
            approver=OWNER,
            approver_role="owner",
            decision="APPROVE",
            note="approved: long-standing client",
        )
        self.assertEqual(decided.status, "APPROVED")

    def test_a_discount_that_breaches_the_floor_is_never_auto_escalated(self):
        """The engine reports; a human decides. Nothing is raised on its own initiative."""
        self.build(floor_pct="20", floor_minor=1)
        before = self.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
        self.engine.evaluate(self.request(amount=15_000_000))
        after = self.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
        self.assertEqual(after, before)


# --------------------------------------------------------------------------------------
# §11 — every outcome is audited
# --------------------------------------------------------------------------------------
class TestEveryOutcomeIsAudited(DiscountEngineHarness):
    def test_an_applied_discount_is_audited(self):
        self.build()
        self.engine.evaluate(self.request(amount=1_000_000))
        self.assertIn(DE.ACTION_APPLIED, self.audit_actions())

    def test_a_refused_discount_is_audited_too(self):
        self.build(authority=("owner",))
        self.engine.evaluate(self.request(role="agent", amount=1_000_000))
        self.assertIn(DE.ACTION_REFUSED, self.audit_actions())

    def test_an_override_request_is_audited(self):
        self.build(floor_pct="20", floor_minor=1)
        decision = self.engine.evaluate(self.request(amount=15_000_000))
        self.engine.request_override(
            decision, requested_by=OWNER, requested_role="owner", reason="repeat guest"
        )
        self.assertIn(DE.ACTION_OVERRIDE_REQUESTED, self.audit_actions())

    def test_the_audit_row_records_the_amount_the_kind_and_the_floors(self):
        self.build(floor_pct="20", floor_minor=1_000_000)
        decision = self.engine.evaluate(self.request(amount=1_000_000))
        row = self.conn.execute(
            "SELECT entity_id, after_state FROM audit_events WHERE action = ? AND entity_type = ?",
            (DE.ACTION_APPLIED, DE.ENTITY_TYPE),
        ).fetchone()
        self.assertEqual(row[0], decision.discount_id)
        payload = json.loads(row[1])
        self.assertEqual(payload["kind"], "AGENCY_FUNDED")
        self.assertEqual(payload["amount_minor"], 1_000_000)
        self.assertEqual(payload["margin_after_minor"], MARGIN_BEFORE - 1_000_000)
        self.assertEqual(payload["floor_minor"], 1_000_000)

    def test_the_audit_row_names_the_role_that_granted_it(self):
        self.build()
        self.engine.evaluate(self.request(role="manager", amount=1_000_000))
        row = self.conn.execute(
            "SELECT actor_role FROM audit_events WHERE action = ? AND entity_type = ?",
            (DE.ACTION_APPLIED, DE.ENTITY_TYPE),
        ).fetchone()
        self.assertEqual(row[0], "manager")


# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------
class TestDecisionsAreDeterministic(DiscountEngineHarness):
    def test_the_same_request_twice_gives_the_same_decision(self):
        self.build()
        first = self.engine.evaluate(self.request(amount=1_000_000))
        second = self.engine.evaluate(self.request(amount=1_000_000))
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(first.outcome, second.outcome)

    def test_the_fingerprint_moves_when_the_amount_moves(self):
        self.build()
        first = self.engine.evaluate(self.request(amount=1_000_000))
        second = self.engine.evaluate(self.request(amount=2_000_000))
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_the_fingerprint_moves_when_the_funding_party_moves(self):
        self.build()
        agency = self.engine.evaluate(self.request(kind="AGENCY_FUNDED", amount=1_000_000))
        supplier = self.engine.evaluate(self.request(kind="SUPPLIER_FUNDED", amount=1_000_000))
        self.assertNotEqual(agency.fingerprint(), supplier.fingerprint())

    def test_each_decision_gets_its_own_identity(self):
        self.build()
        first = self.engine.evaluate(self.request(amount=1_000_000))
        second = self.engine.evaluate(self.request(amount=1_000_000))
        self.assertNotEqual(first.discount_id, second.discount_id)


if __name__ == "__main__":
    unittest.main()

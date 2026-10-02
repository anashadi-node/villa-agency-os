"""Stage 4 — the §9 AI Reservation Service and the §13 handoff ladder, pinned as claims.

The service has exactly two jobs (see :mod:`reservation_service`): hold one conversation's
context in one isolated row, and walk the §13 ladder one rung at a time. Every claim below is
about one of those two jobs being **enforced** rather than intended:

1. a conversation starts running, and it is the only one this inquiry has on this channel;
2. a foreign inquiry, a foreign conversation and an unknown channel are refused — isolation is
   a lookup, not a promise;
3. the ladder cannot be skipped, and a rung out of order changes nothing;
4. escalation silences the AI *before* the human arrives (the fail-closed reading of §13);
5. only ``AI_RUNNING`` allows a write, on every rung, through one predicate;
6. taking control, resolving and allowing a resume are human acts;
7. a handoff to a role nobody holds is refused (D3: roles start ``UNASSIGNED``);
8. the escalation vocabulary is closed, and an unknown trigger writes nothing;
9. §9's confidence threshold is deny-first: unpublished escalates, below escalates,
   at the threshold may act — and a fraction is never mistaken for a percentage;
10. the handoff package carries *reviewed* facts and a digest that follows the records it
    references, and that digest is what the rung keeps;
11. escalation announces itself through the outbox and opens an incident for the triggers
    that have playbooks;
12. the event log is append-only, and every rung lands in the audit chain.

The clock is frozen and the ids are deterministic: every claim here is reproducible, and none
of them depends on how long a test took to run.
"""

from __future__ import annotations

import unittest

import ai_gateway as AG
import ai_service as AIS
import cases as CS
import exception_engine as EE
import intake as IN
import policy_engine as PE
import reservation_kernel as RK
import reservation_service as RS
from audit import AuditLog
from outbox import OutboxService
from primitives import DomainError, FrozenClock, IdFactory

AT = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
OTHER_AGENCY = "agency_other"
HUMAN = "user:owner-1"
OPS = "user:ops-1"
OPS_ROLE = "reservation_manager"
MACHINE = "ai:reservation_agent"
MACHINE_ROLE = "ai"
CONFIDENCE_KEY = RS.MIN_CONFIDENCE_KEY
OWNER_SOURCE = "§9 Agent 3 — AI Reservation Service (owner decision 2026-10-02)"


class ReservationServiceTestCase(unittest.TestCase):
    """One agency, one inquiry, one registered AI principal, one conversation per test."""

    # -- fixtures ---------------------------------------------------------- #

    def build(self, *, clock: str = AT, seed: str = "res", agency: str = AGENCY):
        kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}k"),
            agency_id=agency,
        )
        self.addCleanup(kernel.close)
        conn = kernel.conn
        case, message = CS.CaseService(
            conn,
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}c"),
            agency_id=agency,
        ).open_case(
            channel="whatsapp_business",
            guest_ref="guest_ref_0001",
            content_class="P0",
            body="Do you have a villa in Canggu for four nights in November?",
            idempotency_key=f"idem_{seed}_case_0001",
        )
        self.addCleanup(lambda: None)
        policy = PE.PolicyResolver(conn, clock=FrozenClock.at(clock), agency_id=agency)
        ai = AIS.AiService(
            conn,
            policy=policy,
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}ai"),
            agency_id=agency,
        )
        principal = ai.register_principal(
            "Reservation Agent", secret_ref="secret_ref_reservation_agent", actor=HUMAN
        )
        audit = AuditLog(conn, clock=FrozenClock.at(clock), ids=IdFactory(deterministic=True, seed=f"{seed}a"))
        outbox = OutboxService(
            conn, clock=FrozenClock.at(clock), ids=IdFactory(deterministic=True, seed=f"{seed}o"), agency_id=agency
        )
        exceptions = EE.ExceptionEngine(
            conn,
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}x"),
            agency_id=agency,
            outbox=outbox,
        )
        gateway = AG.AiGateway(
            ai,
            audit=audit,
            outbox=outbox,
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}g"),
            agency_id=agency,
        )
        service = RS.ReservationService(
            conn,
            gateway=gateway,
            policy=policy,
            clock=FrozenClock.at(clock),
            ids=IdFactory(deterministic=True, seed=f"{seed}s"),
            audit=audit,
            outbox=outbox,
            exceptions=exceptions,
            agency_id=agency,
            principal_id=principal.principal_id,
        )
        return _Fixture(
            kernel=kernel,
            conn=conn,
            case=case,
            message=message,
            policy=policy,
            ai=ai,
            principal=principal,
            audit=audit,
            outbox=outbox,
            exceptions=exceptions,
            gateway=gateway,
            service=service,
            clock=clock,
        )

    def opened(self, **kwargs):
        """A fixture with one conversation already open."""

        fixture = self.build(**kwargs)
        fixture.conversation = fixture.service.open_conversation(
            case_id=fixture.case.case_id,
            guest_ref="guest_ref_0001",
            channel="whatsapp_business",
        )
        return fixture

    def escalated(self, *, trigger: str = "AVAILABILITY_CONFLICT", **kwargs):
        fixture = self.opened(**kwargs)
        fixture.service.escalate(
            fixture.conversation.conversation_id,
            trigger=trigger,
            reason="the unit the guest asked for has two conflicting availability records",
        )
        return fixture

    def handed_off(self, **kwargs):
        fixture = self.escalated(**kwargs)
        fixture.package = fixture.service.hand_off(
            fixture.conversation.conversation_id,
            reason="nobody in this conversation can resolve a conflicting source of truth",
        )
        return fixture

    def staffed(self, fixture, *, blueprint: str = OPS_ROLE, role_id: str = "operations_manager"):
        """Put a named person on the blueprint the handoff targets (the tenant's own record)."""

        fixture.conn.execute(
            "INSERT INTO agency_roles (role_id, agency_id, label, based_on, status, updated_by,"
            " updated_role, updated_at, created_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, 'owner', ?, ?)",
            (role_id, fixture.service.agency_id, "Operations Manager", blueprint, HUMAN, fixture.clock, fixture.clock),
        )
        fixture.conn.execute(
            "INSERT INTO agency_role_holders (holder_id, agency_id, role_id, holder_ref, status,"
            " updated_by, updated_at, created_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
            (f"holder_{role_id}", fixture.service.agency_id, role_id, "staff_0001", HUMAN, fixture.clock, fixture.clock),
        )
        fixture.conn.commit()
        return fixture

    def set_consent(self, fixture, state: str = "GRANTED"):
        return fixture.service.set_consent(
            fixture.conversation.conversation_id,
            consent_state=state,
            reason="the guest said so in their own words on the phone",
            actor=OPS,
            actor_role=OPS_ROLE,
        )

    def accept_requirement(self, fixture, *, name: str = "check_in", value: str = "2026-11-10"):
        """One intake field the way the platform actually produces one: proposed, then reviewed."""

        intake = IN.IntakeService(
            fixture.conn, clock=FrozenClock.at(fixture.clock), ids=IdFactory(deterministic=True, seed="int")
        )
        _proposal_id, rows = intake.propose(
            fixture.case.case_id,
            source_message_id=fixture.message.case_message_id,
            fields=[IN.ProposedField(name=name, value=value, confidence=92, span_start=4, span_end=14)],
            extractor="rule_extractor",
            method="rule",
        )
        intake.review(
            intake_field_id=rows[0].intake_field_id,
            decision="ACCEPT",
            reviewer=OPS,
            reviewer_role=OPS_ROLE,
        )
        return rows[0]

    # -- 1. the ledger ------------------------------------------------------ #

    def test_a_new_conversation_starts_running_with_one_event(self):
        fixture = self.opened()
        conversation = fixture.conversation
        self.assertEqual(conversation.state, "AI_RUNNING")
        self.assertTrue(conversation.writes_allowed)
        self.assertFalse(conversation.with_human)
        self.assertEqual(conversation.channel, "whatsapp_business")
        self.assertEqual(conversation.consent_state, "UNKNOWN")
        self.assertEqual(
            [(event.seq, event.kind, event.state) for event in fixture.service.events(conversation.conversation_id)],
            [(1, "OPENED", "AI_RUNNING")],
        )

    def test_the_second_conversation_on_the_same_inquiry_and_channel_is_refused(self):
        """Two AI threads on one guest conversation is the §13 conflicting-messages failure."""

        fixture = self.opened()
        with self.assertRaises(RS.ConversationExists) as caught:
            fixture.service.open_conversation(
                case_id=fixture.case.case_id,
                guest_ref="guest_ref_0001",
                channel="whatsapp_business",
            )
        self.assertEqual(caught.exception.code, "RES_CONVERSATION_EXISTS")
        self.assertEqual(len(fixture.service.for_case(fixture.case.case_id)), 1)

    def test_a_second_channel_gets_its_own_conversation(self):
        """The isolation unit is (inquiry, channel): the same guest on email is a new thread."""

        fixture = self.opened()
        email = fixture.service.open_conversation(
            case_id=fixture.case.case_id, guest_ref="guest_ref_0001", channel="email"
        )
        self.assertNotEqual(email.conversation_id, fixture.conversation.conversation_id)
        self.assertEqual(len(fixture.service.for_case(fixture.case.case_id)), 2)

    # -- 2. isolation ------------------------------------------------------- #

    def test_a_foreign_inquiry_cannot_be_opened(self):
        fixture = self.build(seed="iso")
        fixture.conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, 'Other Agency', 'ACTIVE', ?, ?)",
            (OTHER_AGENCY, fixture.clock, fixture.clock),
        )
        fixture.conn.commit()
        other, _other_message = CS.CaseService(
            fixture.conn,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="other"),
            agency_id=OTHER_AGENCY,
        ).open_case(
            channel="email",
            guest_ref="guest_ref_0002",
            content_class="P0",
            body="A villa please.",
            idempotency_key="idem_other_case_0001",
        )
        with self.assertRaises(RS.ConversationNotFound) as caught:
            fixture.service.open_conversation(
                case_id=other.case_id, guest_ref="guest_ref_0002", channel="email"
            )
        self.assertEqual(caught.exception.code, "RES_CONVERSATION_NOT_FOUND")

    def test_another_tenants_conversation_is_not_found(self):
        """A foreign id reads as *unknown*, never as *forbidden*: nothing about it leaks."""

        fixture = self.opened(seed="iso2")
        foreign = RS.ReservationService(
            fixture.conn,
            gateway=fixture.gateway,
            policy=fixture.policy,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="foreign"),
            agency_id=OTHER_AGENCY,
            principal_id=fixture.principal.principal_id,
        )
        with self.assertRaises(RS.ConversationNotFound) as caught:
            foreign.get(fixture.conversation.conversation_id)
        self.assertEqual(caught.exception.code, "RES_CONVERSATION_NOT_FOUND")

    def test_unknown_channel_and_consent_are_refused(self):
        fixture = self.opened()
        with self.assertRaises(RS.UnknownChannel) as caught:
            fixture.service.open_conversation(
                case_id=fixture.case.case_id, guest_ref="guest_ref_0003", channel="carrier_pigeon"
            )
        self.assertEqual(caught.exception.code, "RES_UNKNOWN_CHANNEL")
        with self.assertRaises(RS.UnknownConsentState):
            fixture.service.set_consent(
                fixture.conversation.conversation_id,
                consent_state="PROBABLY",
                reason="the guest seemed happy about it",
                actor=OPS,
                actor_role=OPS_ROLE,
            )

    # -- 3. the ladder ------------------------------------------------------ #

    def test_a_rung_cannot_be_skipped(self):
        fixture = self.opened()
        with self.assertRaises(RS.RungRefused) as caught:
            fixture.service.hand_off(fixture.conversation.conversation_id, reason="just hand it over")
        self.assertEqual(caught.exception.code, "RES_RUNG_REFUSED")
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "AI_RUNNING")

    def test_a_rung_out_of_order_changes_nothing(self):
        """A refused rung must leave no event and no audit row behind."""

        fixture = self.handed_off()
        before = len(fixture.service.events(fixture.conversation.conversation_id))
        with self.assertRaises(RS.RungRefused):
            fixture.service.begin_handling(fixture.conversation.conversation_id, actor=OPS, actor_role=OPS_ROLE)
        self.assertEqual(len(fixture.service.events(fixture.conversation.conversation_id)), before)
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "HANDOFF_PENDING")

    def test_escalating_twice_is_refused(self):
        fixture = self.escalated()
        with self.assertRaises(RS.RungRefused):
            fixture.service.escalate(
                fixture.conversation.conversation_id, trigger="SERIOUS_COMPLAINT", reason="also this"
            )

    def test_every_rung_is_recorded_once_with_its_actor(self):
        fixture = self.staffed(self.handed_off())
        conversation_id = fixture.conversation.conversation_id
        service = fixture.service
        service.take_control(conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE)
        service.resolve(conversation_id, reason="the guest rebooked directly", actor=OPS, actor_role=OPS_ROLE)
        service.allow_resume(
            conversation_id, reason="the guest asked the assistant again", actor=OPS, actor_role=OPS_ROLE
        )
        service.resume(conversation_id)
        events = service.events(conversation_id)
        self.assertEqual(
            [event.kind for event in events],
            [
                "OPENED",
                "ESCALATED",
                "HANDOFF_REQUESTED",
                "HUMAN_TAKEN",
                "WRITES_SUSPENDED",
                "HANDLING_STARTED",
                "RESOLVED",
                "RESUME_ALLOWED",
                "RESUMED",
            ],
        )
        self.assertEqual([event.seq for event in events], list(range(1, 10)))
        self.assertEqual(events[1].trigger_code, "AVAILABILITY_CONFLICT")
        self.assertEqual(events[1].actor, RS.DEFAULT_ACTOR)
        self.assertEqual(events[3].actor, OPS)
        self.assertEqual(events[5].state, "HUMAN_HANDLING")
        self.assertEqual(service.state(conversation_id), "AI_RUNNING")

    # -- 4. the write rule -------------------------------------------------- #

    def test_escalation_silences_the_ai_before_the_human_arrives(self):
        """Between the trigger and the handoff is when an unguarded message costs the most."""

        fixture = self.escalated()
        with self.assertRaises(RS.WritesSuspended) as caught:
            fixture.service.require_write(
                fixture.conversation.conversation_id, what="compose a follow-up"
            )
        self.assertEqual(caught.exception.code, "RES_WRITES_SUSPENDED")
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))

    def test_only_the_running_rung_allows_a_write(self):
        """The write rule is one predicate over the §13 states, not a habit in each worker."""

        self.assertEqual([state for state in RS.CONVERSATION_STATES if state == RS.WRITE_STATE], ["AI_RUNNING"])
        fixture = self.handed_off()
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))
        self.staffed(fixture)
        fixture.service.take_control(
            fixture.conversation.conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE
        )
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))
        fixture.service.resolve(
            fixture.conversation.conversation_id, reason="done", actor=OPS, actor_role=OPS_ROLE
        )
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))
        fixture.service.allow_resume(
            fixture.conversation.conversation_id, reason="safe again", actor=OPS, actor_role=OPS_ROLE
        )
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))
        fixture.service.resume(fixture.conversation.conversation_id)
        self.assertTrue(fixture.service.may_write(fixture.conversation.conversation_id))

    # -- 5. human acts ------------------------------------------------------ #

    def test_a_machine_principal_cannot_take_control_or_allow_a_resume(self):
        fixture = self.staffed(self.handed_off())
        conversation_id = fixture.conversation.conversation_id
        with self.assertRaises(RS.HumanRequired) as caught:
            fixture.service.take_control(
                conversation_id, human_ref="staff_0001", actor=MACHINE, actor_role=MACHINE_ROLE
            )
        self.assertEqual(caught.exception.code, "RES_HUMAN_REQUIRED")
        fixture.service.take_control(
            conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE
        )
        fixture.service.resolve(conversation_id, reason="done", actor=OPS, actor_role=OPS_ROLE)
        with self.assertRaises(RS.HumanRequired):
            fixture.service.allow_resume(
                conversation_id, reason="the AI may have it back", actor=MACHINE, actor_role=MACHINE_ROLE
            )
        self.assertEqual(fixture.service.state(conversation_id), "RESOLUTION")

    def test_taking_control_without_naming_a_person_is_refused(self):
        fixture = self.staffed(self.handed_off())
        with self.assertRaises(RS.HumanRequired):
            fixture.service.take_control(
                fixture.conversation.conversation_id, human_ref="staff_0001"
            )

    def test_a_handoff_to_an_unstaffed_role_is_refused(self):
        """D3: every role starts UNASSIGNED, so the platform must not hand a guest to a void."""

        fixture = self.handed_off()
        with self.assertRaises(RS.HandoffRoleUnstaffed) as caught:
            fixture.service.take_control(
                fixture.conversation.conversation_id,
                human_ref="staff_0001",
                actor=OPS,
                actor_role=OPS_ROLE,
            )
        self.assertEqual(caught.exception.code, "RES_HANDOFF_ROLE_UNSTAFFED")
        self.assertEqual(fixture.service.staffed_blueprints(), ())
        self.staffed(fixture)
        self.assertEqual(fixture.service.staffed_blueprints(), (OPS_ROLE,))
        conversation = fixture.service.take_control(
            fixture.conversation.conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE
        )
        self.assertEqual(conversation.state, "HUMAN_HANDLING")

    def test_a_disabled_role_does_not_count_as_staffed(self):
        fixture = self.staffed(self.handed_off())
        fixture.conn.execute("UPDATE agency_role_holders SET status = 'INACTIVE'")
        fixture.conn.commit()
        self.assertEqual(fixture.service.staffed_blueprints(), ())
        with self.assertRaises(RS.HandoffRoleUnstaffed):
            fixture.service.take_control(
                fixture.conversation.conversation_id,
                human_ref="staff_0001",
                actor=OPS,
                actor_role=OPS_ROLE,
            )

    def test_a_different_principal_cannot_resume_somebody_elses_thread(self):
        fixture = self.staffed(self.handed_off())
        conversation_id = fixture.conversation.conversation_id
        fixture.service.take_control(
            conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE
        )
        fixture.service.resolve(conversation_id, reason="done", actor=OPS, actor_role=OPS_ROLE)
        fixture.service.allow_resume(conversation_id, reason="safe", actor=OPS, actor_role=OPS_ROLE)
        other = AIS.AiService(
            fixture.conn,
            policy=fixture.policy,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="other-principal"),
            agency_id=AGENCY,
        ).register_principal("Second Agent", secret_ref="secret_ref_second_agent", actor=HUMAN)
        intruder = RS.ReservationService(
            fixture.conn,
            gateway=fixture.gateway,
            policy=fixture.policy,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="intruder"),
            agency_id=AGENCY,
            principal_id=other.principal_id,
        )
        with self.assertRaises(RS.ReservationServiceError) as caught:
            intruder.resume(conversation_id)
        self.assertEqual(caught.exception.code, "RES_WRONG_PRINCIPAL")

    # -- 6. the escalation vocabulary --------------------------------------- #

    def test_an_unknown_trigger_is_refused_and_writes_nothing(self):
        """A free-text reason is not a trigger anybody can count, route or test."""

        fixture = self.opened()
        with self.assertRaises(RS.UnknownTrigger) as caught:
            fixture.service.escalate(
                fixture.conversation.conversation_id,
                trigger="the guest sounded annoyed",
                reason="tone",
            )
        self.assertEqual(caught.exception.code, "RES_UNKNOWN_TRIGGER")
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "AI_RUNNING")
        self.assertEqual(len(fixture.service.events(fixture.conversation.conversation_id)), 1)

    def test_every_trigger_names_a_blueprint_and_the_material_ones_go_to_the_gm(self):
        self.assertEqual(sorted(RS.BLUEPRINT_OF_TRIGGER), sorted(RS.ESCALATION_TRIGGERS))
        self.assertEqual(RS.BLUEPRINT_OF_TRIGGER["LEGAL_THREAT"], "gm")
        self.assertEqual(RS.BLUEPRINT_OF_TRIGGER["SAFETY_OR_EMERGENCY"], "gm")
        self.assertEqual(RS.BLUEPRINT_OF_TRIGGER["FUNDS_AFTER_HOLD_EXPIRY"], "finance_manager")
        self.assertEqual(RS.BLUEPRINT_OF_TRIGGER["SERIOUS_COMPLAINT"], "reservation_manager")

    # -- 7. §9 uncertainty -------------------------------------------------- #

    def test_an_unpublished_confidence_threshold_escalates_instead_of_guessing(self):
        fixture = self.opened()
        with self.assertRaises(RS.EscalationRaised) as caught:
            fixture.service.check_confidence(
                fixture.conversation.conversation_id,
                confidence_pct=99,
                what="answer a rate question",
            )
        self.assertEqual(caught.exception.code, "RES_ESCALATED")
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "ESCALATION_TRIGGERED")
        events = fixture.service.events(fixture.conversation.conversation_id)
        self.assertEqual(events[-1].trigger_code, "AI_UNCERTAINTY")
        self.assertIn(CONFIDENCE_KEY, events[-1].reason)

    def test_confidence_below_the_published_threshold_escalates(self):
        fixture = self.opened()
        self.publish(fixture, "90")
        with self.assertRaises(RS.EscalationRaised) as caught:
            fixture.service.check_confidence(
                fixture.conversation.conversation_id,
                confidence_pct=84,
                what="promise a discount",
            )
        self.assertEqual(caught.exception.details.get("threshold_pct"), "90")
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "ESCALATION_TRIGGERED")

    def test_confidence_at_the_threshold_may_act(self):
        fixture = self.opened()
        self.publish(fixture, "90")
        fixture.service.check_confidence(
            fixture.conversation.conversation_id, confidence_pct=90, what="confirm the dates"
        )
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "AI_RUNNING")

    def test_a_fraction_is_never_mistaken_for_a_percentage(self):
        """``0.9`` meaning "0.9%" would be catastrophic under-confidence; refused, not guessed."""

        fixture = self.opened()
        self.publish(fixture, "90")
        with self.assertRaises(DomainError) as caught:
            fixture.service.check_confidence(
                fixture.conversation.conversation_id, confidence_pct=0.9, what="act on a guess"
            )
        self.assertEqual(caught.exception.code, "RES_INVALID_PERCENTAGE")
        self.assertEqual(fixture.service.state(fixture.conversation.conversation_id), "AI_RUNNING")

    def test_confidence_is_checked_only_while_the_ai_is_running(self):
        fixture = self.escalated()
        with self.assertRaises(RS.WritesSuspended):
            fixture.service.check_confidence(
                fixture.conversation.conversation_id, confidence_pct=99, what="act anyway"
            )

    def publish(self, fixture, value: str):
        return fixture.policy.publish(
            CONFIDENCE_KEY,
            value,
            effective_from="2026-10-01",
            effective_to="2026-12-31",
            source_doc_ref=OWNER_SOURCE,
            approved_by=HUMAN,
        )

    # -- 8. the handoff package --------------------------------------------- #

    def test_the_package_carries_reviewed_facts_and_still_missing_requirements(self):
        fixture = self.escalated(seed="pkg")
        self.accept_requirement(fixture, name="check_in", value="2026-11-10")
        package = fixture.service.handoff_package(fixture.conversation.conversation_id)
        self.assertEqual(str(package.requirements.get("check_in")), "2026-11-10")
        self.assertIn("check_out", package.unknown_required)
        self.assertNotIn("check_out", package.requirements)
        self.assertTrue(any("clarify" in action for action in package.pending_actions))
        self.assertEqual(package.handoff_blueprint, "reservation_manager")
        self.assertEqual(package.trigger_code, "AVAILABILITY_CONFLICT")

    def test_an_unreviewed_field_is_not_a_fact(self):
        """§16: only a reviewed field is a fact. A proposal is a proposal."""

        fixture = self.escalated(seed="pkg2")
        IN.IntakeService(
            fixture.conn, clock=FrozenClock.at(fixture.clock), ids=IdFactory(deterministic=True, seed="int2")
        ).propose(
            fixture.case.case_id,
            source_message_id=fixture.message.case_message_id,
            fields=[IN.ProposedField(name="check_out", value="2026-11-14", confidence=88, span_start=4, span_end=14)],
            extractor="rule_extractor",
            method="rule",
        )
        package = fixture.service.handoff_package(fixture.conversation.conversation_id)
        self.assertNotIn("check_out", package.requirements)
        self.assertIn("check_out", package.unknown_required)

    def test_the_rung_keeps_the_digest_of_what_was_handed_over(self):
        fixture = self.handed_off(seed="pkg3")
        events = fixture.service.events(fixture.conversation.conversation_id)
        self.assertEqual(events[-1].package_fingerprint, fixture.package.fingerprint)
        again = fixture.service.handoff_package(
            fixture.conversation.conversation_id,
            reason="nobody in this conversation can resolve a conflicting source of truth",
        )
        self.assertEqual(again.fingerprint, fixture.package.fingerprint)
        self.assertEqual(len(fixture.service.events(fixture.conversation.conversation_id)), len(events))

    def test_the_package_digest_follows_the_records_it_references(self):
        """Same inputs, same digest; a fact that changed changes the digest. It is derived."""

        fixture = self.escalated(seed="pkg4")
        first = fixture.service.handoff_package(fixture.conversation.conversation_id)
        self.assertEqual(first.fingerprint, fixture.service.handoff_package(fixture.conversation.conversation_id).fingerprint)
        self.accept_requirement(fixture, name="check_in", value="2026-11-10")
        second = fixture.service.handoff_package(fixture.conversation.conversation_id)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_the_package_is_readable_while_the_ai_is_suspended(self):
        fixture = self.handed_off(seed="pkg5")
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))
        package = fixture.service.handoff_package(fixture.conversation.conversation_id)
        self.assertEqual(package.state, "HANDOFF_PENDING")
        reference = fixture.conn.execute(
            "SELECT reference FROM cases WHERE case_id = ?", (fixture.case.case_id,)
        ).fetchone()["reference"]
        self.assertEqual(package.case_reference, reference)

    # -- 9. announcing the escalation --------------------------------------- #

    def test_escalation_announces_itself_and_opens_an_incident(self):
        fixture = self.escalated(seed="ann", trigger="AVAILABILITY_CONFLICT")
        # rowid, not created_at: the clock is frozen, so two rows share an instant and only the
        # insertion order is a fact.
        outbox_rows = fixture.conn.execute(
            "SELECT event_type FROM outbox_events ORDER BY rowid"
        ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in outbox_rows],
            [RS.ESCALATED_EVENT, "incident.failure_recorded"],
        )
        incidents = fixture.conn.execute("SELECT playbook, severity FROM incidents").fetchall()
        self.assertEqual([row["playbook"] for row in incidents], ["VENUE_OUTAGE"])
        self.assertEqual([row["severity"] for row in incidents], ["SEV2"])

    def test_the_handoff_announces_itself_once(self):
        fixture = self.handed_off(seed="ann2")
        outbox_rows = fixture.conn.execute(
            "SELECT event_type FROM outbox_events ORDER BY rowid"
        ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in outbox_rows],
            [RS.ESCALATED_EVENT, "incident.failure_recorded", RS.HANDOFF_REQUESTED_EVENT],
        )
        self.assertEqual(len(fixture.service.events(fixture.conversation.conversation_id)), 3)

    def test_only_the_triggers_with_playbooks_open_an_incident(self):
        fixture = self.escalated(seed="ann3", trigger="LEGAL_THREAT")
        incidents = fixture.conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()
        self.assertEqual(incidents["n"], 0)
        self.assertEqual(
            sorted(RS.INCIDENT_OF_TRIGGER),
            ["AVAILABILITY_CONFLICT", "FUNDS_AFTER_HOLD_EXPIRY", "SAFETY_OR_EMERGENCY"],
        )
        self.assertEqual(set(RS.INCIDENT_OF_TRIGGER.values()) <= set(EE.ROUTE_CATEGORIES), True)

    # -- 10. the record ------------------------------------------------------ #

    def test_the_event_log_is_append_only(self):
        fixture = self.opened(seed="ao")
        with self.assertRaises(Exception):
            fixture.conn.execute("UPDATE ai_conversation_events SET state = 'AI_RUNNING'")
        with self.assertRaises(Exception):
            fixture.conn.execute("DELETE FROM ai_conversation_events")
        self.assertEqual(len(fixture.service.events(fixture.conversation.conversation_id)), 1)

    def test_every_rung_lands_in_the_audit_chain(self):
        fixture = self.staffed(self.handed_off(seed="aud"))
        conversation_id = fixture.conversation.conversation_id
        fixture.service.take_control(
            conversation_id, human_ref="staff_0001", actor=OPS, actor_role=OPS_ROLE
        )
        rows = fixture.conn.execute(
            "SELECT action, actor FROM audit_events WHERE entity_id = ? ORDER BY rowid", (conversation_id,)
        ).fetchall()
        self.assertEqual(len(rows), len(fixture.service.events(conversation_id)))
        self.assertEqual(rows[0]["action"], "reservation.conversation.opened")
        self.assertIn("reservation.conversation.human_taken", [row["action"] for row in rows])
        fixture.audit.verify()

    def test_consent_is_recorded_by_a_person_with_a_reason(self):
        fixture = self.opened(seed="con")
        conversation = self.set_consent(fixture, "GRANTED")
        self.assertEqual(conversation.consent_state, "GRANTED")
        with self.assertRaises(RS.HumanRequired):
            fixture.service.set_consent(
                conversation.conversation_id,
                consent_state="WITHDRAWN",
                reason="the model's guess",
                actor=MACHINE,
                actor_role=MACHINE_ROLE,
            )
        self.assertEqual(fixture.service.get(conversation.conversation_id).consent_state, "GRANTED")

    def test_a_reference_is_attached_only_after_the_record_exists(self):
        """The pointer follows the record, so the conversation never links to a maybe."""

        fixture = self.opened(seed="ref")
        with self.assertRaises(Exception):
            fixture.service.attach_reference(
                fixture.conversation.conversation_id, kind="hold", ref="hold_does_not_exist", actor=OPS, actor_role=OPS_ROLE
            )
        self.assertIsNone(fixture.service.get(fixture.conversation.conversation_id).hold_ref)
        with self.assertRaises(RS.UnknownReference):
            fixture.service.attach_reference(
                fixture.conversation.conversation_id, kind="vibe", ref="ref_000001", actor=OPS, actor_role=OPS_ROLE
            )

    def test_consent_survives_a_suspension_and_is_not_the_ais_to_lose(self):
        fixture = self.escalated(seed="con2")
        self.set_consent(fixture, "WITHDRAWN")
        self.assertEqual(fixture.service.get(fixture.conversation.conversation_id).consent_state, "WITHDRAWN")
        self.assertFalse(fixture.service.may_write(fixture.conversation.conversation_id))


class _Fixture:
    """A built world, one attribute per collaborator. A small bag, deliberately not a mock."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.conversation = None
        self.package = None


if __name__ == "__main__":
    unittest.main()

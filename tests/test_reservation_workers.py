"""Stage 4 — the §9 capability workers (RES-W01…W03), pinned as claims.

A worker has three steps and no fourth: **guard the thread, ask the door, delegate**. The claims
below are about each step being enforced rather than intended:

1. the inquiry worker records *proposed* fields and accepts nothing (§16: a person reviews);
2. the door is asked *before* the domain module writes — a refused tool leaves no row, and a
   refusal is what the audit shows, not a log written after the fact;
3. a worker refuses an empty proposal before asking anyone, and a foreign message id is refused
   by the module that owns message provenance;
4. the matching worker ranks this tenant's inventory, reads the §6 authority for the stay, and
   refuses (writing nothing) when a required fact is missing;
5. another tenant's inventory is never considered, and the run says which facts it ranked;
6. the availability worker asks about the inquiry's **confirmed** stay, derives its idempotency
   key from the question (same question replays, a different window does not) and never invents
   a window;
7. a MEDIUM tool is refused until the owner publishes the §56 allowance, and is refused again
   when the principal's §17 mode is narrowed — the door refuses, not the worker;
8. no worker acts on a conversation a human owns (§13), and none of them leaves a row behind;
9. what lands in the audit is the worker code and an operational sentence — never what the guest
   wrote (§56 + §8).

The clock is frozen, the ids are deterministic and the database is an in-memory one: every claim
here is reproducible, and none of them depends on how long a test took to run.
"""

from __future__ import annotations

import unittest

import ai_gateway as AG
import ai_service as AIS
import crm as CRM
import intake as IN
import reservation_workers as RW
from primitives import DomainError, FrozenClock, IdFactory

import test_reservation_service as RST
from test_matching import seed_property, seed_unit

AGENCY = RST.AGENCY
HUMAN = RST.HUMAN
OPS = RST.OPS

#: §56's allowance for MEDIUM tools, unpublished until an owner writes it down. The key is read
#: from :mod:`ai_service` rather than restated, so this file cannot drift from the one it tests.
ALLOWANCE_KEY = AIS.MEDIUM_ALLOWANCE_KEY

#: A word from the fixture's guest message ("Do you have a villa in Canggu for four nights in
#: November?"). Used to prove the audit never carries guest text.
GUEST_WORD = "Canggu"

STAY_IN = "2026-11-10"
STAY_OUT = "2026-11-14"


class ReservationWorkersTestCase(unittest.TestCase):
    """One tenant, one inquiry, one AI principal — and the three workers that act for it.

    The reservation fixture is shared rather than re-written: this class drives
    :class:`test_reservation_service.ReservationServiceTestCase` as a builder and copies the
    cleanups over, so the two suites cannot drift apart on how a conversation is set up.
    """

    def setUp(self):
        self.scaffold = RST.ReservationServiceTestCase()
        self.addCleanup(self.scaffold.doCleanups)

    # -- fixtures ---------------------------------------------------------- #

    def build(self, **kwargs):
        """A tenant with an inquiry, a registered principal and no conversation yet."""

        fixture = self.scaffold.build(**kwargs)
        fixture.workers = RW.ReservationWorkers(fixture.service)
        return fixture

    def running(self, **kwargs):
        """The same, with one conversation already running (the only state a worker acts in)."""

        fixture = self.scaffold.opened(**kwargs)
        fixture.workers = RW.ReservationWorkers(fixture.service)
        return fixture

    def handed_off(self, **kwargs):
        """The same, escalated and handed to a human: the AI has been silenced (§13)."""

        fixture = self.scaffold.handed_off(**kwargs)
        fixture.workers = RW.ReservationWorkers(fixture.service)
        return fixture

    def inventory(self, fixture, *, unit_id: str = "unit_1", property_id: str = "prop_1") -> str:
        """One sellable property with one active unit, for the tenant this fixture belongs to."""

        seed_property(fixture.conn, property_id)
        seed_unit(fixture.conn, unit_id, property_id=property_id)
        return unit_id

    def confirm_stay(
        self,
        fixture,
        *,
        check_in: str = STAY_IN,
        check_out: str = STAY_OUT,
        adults: int = 2,
    ):
        """Confirm the inquiry's stay through the §6 authority — CRM, not the proposal layer."""

        crm = CRM.CrmService(
            fixture.conn,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="crm"),
            agency_id=AGENCY,
        )
        for name, value, kind in (
            ("check_in", check_in, "date"),
            ("check_out", check_out, "date"),
            ("adults", str(adults), "integer"),
        ):
            crm.record_requirement(
                fixture.case.case_id,
                field_name=name,
                field_value=value,
                value_type=kind,
                recorded_by=OPS,
            )
        return crm

    def allow_medium(self, fixture, tools=(RW.AVAILABILITY_TOOL,)):
        """The owner publishes the §56 allowance. Nothing MEDIUM moves until they do."""

        return fixture.policy.publish(
            ALLOWANCE_KEY,
            list(tools),
            effective_from="2026-10-01",
            effective_to="9999-12-31",
            source_doc_ref="§56 medium-risk policy-controlled tools",
            approved_by=HUMAN,
        )

    @staticmethod
    def fields(*specs):
        """Proposed fields the way a model-derived extraction hands them over."""

        if not specs:
            specs = (("adults", 2),)
        return [
            IN.ProposedField(name=name, value=value, confidence=88, span_start=4, span_end=12)
            for name, value in specs
        ]

    @staticmethod
    def count(fixture, table: str) -> int:
        return int(fixture.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def audit_reasons(fixture, *, action: str, entity_type: str, entity_id: str) -> list[str]:
        return [
            str(entry.reason)
            for entry in fixture.audit.for_entity(entity_type, entity_id)
            if entry.action == action
        ]

    @staticmethod
    def door_rows(fixture, *, tool: str) -> list:
        """The rows the *gateway* writes when it lets a tool through — one per call."""

        return [
            entry
            for entry in fixture.audit.for_entity("ai_principal", fixture.principal.principal_id)
            if entry.action == AG.AUTHORISED_ACTION and entry.tool == tool
        ]

    @staticmethod
    def allowed_reasons(fixture, *, tool: str) -> list[str]:
        """The identity layer's own row: where ``intent`` is recorded and never consulted."""

        return [
            str(entry.reason)
            for entry in fixture.audit.for_entity("ai_tool", tool)
            if entry.action == "AI_TOOL_ALLOWED"
        ]

    # -- 1. RES-W01: recording the inquiry ---------------------------------- #

    def test_the_inquiry_worker_records_proposed_fields_and_accepts_nothing(self):
        fixture = self.running(seed="w01")
        proposal_id, rows = fixture.workers.record_inquiry(
            fixture.conversation.conversation_id,
            source_message_id=fixture.message.case_message_id,
            fields=self.fields(("adults", 2), ("check_in", STAY_IN)),
        )

        self.assertTrue(proposal_id.startswith("prop"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(str(row.status) for row in rows), ["PROPOSED", "PROPOSED"]
        )
        self.assertEqual(self.count(fixture, "intake_proposals"), 1)
        self.assertEqual(self.count(fixture, "intake_fields"), 2)

    def test_the_inquiry_worker_asks_the_door_and_the_door_is_in_the_audit(self):
        fixture = self.running(seed="w01b")
        fixture.workers.record_inquiry(
            fixture.conversation.conversation_id,
            source_message_id=fixture.message.case_message_id,
            fields=self.fields(),
        )
        rows = self.door_rows(fixture, tool=RW.INTAKE_TOOL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trace_id, fixture.conversation.conversation_id)
        self.assertEqual(rows[0].actor, fixture.principal.principal_id)
        self.assertEqual(len(self.allowed_reasons(fixture, tool=RW.INTAKE_TOOL)), 1)

    def test_a_read_only_principal_cannot_record_anything_and_writes_no_row(self):
        """The door first. ``inquiry.intake`` is a WRITE, so READ_ONLY does not reach it."""

        fixture = self.running(seed="w01c")
        fixture.gateway.set_mode(
            fixture.principal.principal_id, AIS.AI_STATUS_READ_ONLY, actor=HUMAN, actor_role="owner"
        )

        with self.assertRaises(DomainError) as raised:
            fixture.workers.record_inquiry(
                fixture.conversation.conversation_id,
                source_message_id=fixture.message.case_message_id,
                fields=self.fields(),
            )
        self.assertEqual(raised.exception.code, "AI_MODE_REFUSED")
        self.assertEqual(self.count(fixture, "intake_proposals"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.INTAKE_TOOL), [])

    def test_an_empty_proposal_is_refused_before_the_door_is_asked(self):
        fixture = self.running(seed="w01d")
        with self.assertRaises(DomainError) as raised:
            fixture.workers.record_inquiry(
                fixture.conversation.conversation_id,
                source_message_id=fixture.message.case_message_id,
                fields=[],
            )
        self.assertEqual(raised.exception.code, "RES_WORKER_EMPTY_PROPOSAL")
        self.assertEqual(self.count(fixture, "intake_proposals"), 0)
        # Nothing was even asked: there is no authorisation to read in the audit.
        self.assertEqual(self.door_rows(fixture, tool=RW.INTAKE_TOOL), [])

    def test_a_message_from_another_inquiry_cannot_be_the_source(self):
        """Provenance is intake's rule; the worker must not restate it, only survive it."""

        fixture = self.running(seed="w01e")
        other = self.build(seed="w01e-other")
        with self.assertRaises(DomainError):
            fixture.workers.record_inquiry(
                fixture.conversation.conversation_id,
                source_message_id=other.message.case_message_id,
                fields=self.fields(),
            )
        self.assertEqual(self.count(fixture, "intake_proposals"), 0)

    # -- 2. RES-W02: searching the inventory -------------------------------- #

    def test_the_matching_worker_ranks_this_tenants_inventory(self):
        fixture = self.running(seed="w02")
        self.inventory(fixture)
        self.confirm_stay(fixture)

        result = fixture.workers.search_villas(fixture.conversation.conversation_id)

        self.assertEqual(result.case_id, fixture.case.case_id)
        self.assertEqual(result.considered_count, 1)
        self.assertEqual(result.eligible_count, 1)
        self.assertEqual(self.count(fixture, "match_runs"), 1)
        self.assertEqual(self.count(fixture, "match_candidates"), 1)
        self.assertTrue(result.candidates[0].reasons)

    def test_the_matching_worker_ranks_against_the_confirmed_stay(self):
        """The §6 authority, not the proposal layer: CRM's dates are the ones ranked."""

        fixture = self.running(seed="w02b")
        self.inventory(fixture)
        self.confirm_stay(fixture)

        result = fixture.workers.search_villas(fixture.conversation.conversation_id)

        self.assertEqual(str(result.criteria.get("check_in"))[:10], STAY_IN)
        self.assertEqual(str(result.criteria.get("check_out"))[:10], STAY_OUT)
        self.assertEqual(int(result.criteria.get("adults")), 2)

    def test_the_matching_worker_refuses_before_writing_when_a_fact_is_missing(self):
        fixture = self.running(seed="w02c")
        self.inventory(fixture)

        with self.assertRaises(DomainError):
            fixture.workers.search_villas(fixture.conversation.conversation_id)
        self.assertEqual(self.count(fixture, "match_runs"), 0)
        self.assertEqual(self.count(fixture, "match_candidates"), 0)

    def test_another_tenants_inventory_is_never_considered(self):
        fixture = self.running(seed="w02d")
        self.inventory(fixture, unit_id="unit_mine", property_id="prop_mine")
        fixture.conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_other', 'Other Agency', 'ACTIVE', ?, ?)",
            (str(fixture.clock), str(fixture.clock)),
        )
        fixture.conn.execute(
            "INSERT INTO properties (property_id, name, property_type, region, province, timezone,"
            " status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
            " SELECT 'prop_theirs', 'Theirs', 'villa', 'Ubud', 'Bali', 'Asia/Makassar',"
            " 'SELLABLE', 'ACTIVATED', created_at, created_at, updated_at FROM properties"
            " WHERE property_id = 'prop_mine'"
        )
        fixture.conn.execute(
            "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
            " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
            " SELECT 'unit_theirs', 'agency_other', 'prop_theirs', 'Theirs', unit_type, bedrooms,"
            " bathrooms, base_capacity, max_guests, status, created_at, updated_at FROM units"
            " WHERE unit_id = 'unit_mine'"
        )
        self.confirm_stay(fixture)

        result = fixture.workers.search_villas(fixture.conversation.conversation_id)

        self.assertEqual(result.considered_count, 1)
        self.assertEqual([candidate.unit_id for candidate in result.candidates], ["unit_mine"])

    # -- 3. RES-W03: asking a source about availability --------------------- #

    def test_the_availability_worker_asks_about_the_confirmed_stay(self):
        fixture = self.running(seed="w03")
        self.inventory(fixture)
        self.allow_medium(fixture)
        self.confirm_stay(fixture)

        outcome = fixture.workers.request_availability(
            fixture.conversation.conversation_id, unit_id="unit_1"
        )

        self.assertEqual(outcome.unit_id, "unit_1")
        self.assertEqual(outcome.check_in[:10], STAY_IN)
        self.assertEqual(outcome.check_out[:10], STAY_OUT)
        self.assertEqual(outcome.state, "CHECK_REQUESTED")
        self.assertFalse(outcome.replayed)
        self.assertEqual(self.count(fixture, "availability_checks"), 1)

    def test_the_same_question_replays_instead_of_asking_a_second_time(self):
        fixture = self.running(seed="w03b")
        self.inventory(fixture)
        self.allow_medium(fixture)
        self.confirm_stay(fixture)

        first = fixture.workers.request_availability(
            fixture.conversation.conversation_id, unit_id="unit_1"
        )
        second = fixture.workers.request_availability(
            fixture.conversation.conversation_id, unit_id="unit_1"
        )

        self.assertEqual(first.check_id, second.check_id)
        self.assertTrue(second.replayed)
        self.assertEqual(self.count(fixture, "availability_checks"), 1)

    def test_a_different_window_is_a_different_question(self):
        fixture = self.running(seed="w03c")
        self.inventory(fixture)
        self.allow_medium(fixture)
        self.confirm_stay(fixture)

        fixture.workers.request_availability(
            fixture.conversation.conversation_id, unit_id="unit_1"
        )
        other = fixture.workers.request_availability(
            fixture.conversation.conversation_id, unit_id="unit_1", check_out="2026-11-20"
        )

        self.assertEqual(other.check_out[:10], "2026-11-20")
        self.assertFalse(other.replayed)
        self.assertEqual(self.count(fixture, "availability_checks"), 2)

    def test_a_medium_tool_is_refused_until_the_owner_publishes_the_allowance(self):
        """§56 deny-first: absence of a published allowance is not permission."""

        fixture = self.running(seed="w03d")
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            fixture.workers.request_availability(
                fixture.conversation.conversation_id, unit_id="unit_1"
            )
        self.assertEqual(raised.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.count(fixture, "availability_checks"), 0)

    def test_the_availability_worker_will_not_guess_the_window(self):
        fixture = self.running(seed="w03e")
        self.inventory(fixture)
        self.allow_medium(fixture)

        with self.assertRaises(RW.MissingRequirements) as raised:
            fixture.workers.request_availability(
                fixture.conversation.conversation_id, unit_id="unit_1"
            )
        self.assertEqual(raised.exception.code, "RES_WORKER_MISSING_REQUIREMENTS")
        self.assertIn("check_in", raised.exception.details.get("missing", []))
        self.assertEqual(self.count(fixture, "availability_checks"), 0)

    def test_a_degraded_principal_cannot_ask_even_with_the_allowance_published(self):
        fixture = self.running(seed="w03f")
        self.inventory(fixture)
        self.allow_medium(fixture)
        self.confirm_stay(fixture)
        fixture.gateway.set_mode(
            fixture.principal.principal_id, AIS.AI_STATUS_DEGRADED, actor=HUMAN, actor_role="owner"
        )

        with self.assertRaises(DomainError) as raised:
            fixture.workers.request_availability(
                fixture.conversation.conversation_id, unit_id="unit_1"
            )
        self.assertEqual(raised.exception.code, "AI_MODE_REFUSED")
        self.assertEqual(self.count(fixture, "availability_checks"), 0)

    # -- 4. §13 holds for every worker, reads included ---------------------- #

    def test_no_worker_acts_on_a_conversation_a_human_owns(self):
        fixture = self.handed_off(seed="guard")
        self.inventory(fixture)
        self.allow_medium(fixture)
        self.confirm_stay(fixture)
        conversation_id = fixture.conversation.conversation_id

        attempts = {
            RW.INQUIRY_WORKER: lambda: fixture.workers.record_inquiry(
                conversation_id,
                source_message_id=fixture.message.case_message_id,
                fields=self.fields(),
            ),
            RW.MATCHING_WORKER: lambda: fixture.workers.search_villas(conversation_id),
            RW.AVAILABILITY_WORKER: lambda: fixture.workers.request_availability(
                conversation_id, unit_id="unit_1"
            ),
        }
        for worker, attempt in attempts.items():
            with self.subTest(worker=worker):
                with self.assertRaises(DomainError) as raised:
                    attempt()
                self.assertEqual(raised.exception.code, "RES_WRITES_SUSPENDED")

        self.assertEqual(self.count(fixture, "intake_proposals"), 0)
        self.assertEqual(self.count(fixture, "match_runs"), 0)
        self.assertEqual(self.count(fixture, "availability_checks"), 0)

    # -- 5. the tools the workers hold --------------------------------------- #

    def test_the_tools_the_workers_hold_are_the_tools_the_catalogue_declares(self):
        """A typo in a tool name fails here, in review — not at the door, in production."""

        fixture = self.build(seed="catalogue")
        declared = {
            str(row["tool"])
            for row in fixture.conn.execute("SELECT tool FROM ai_tools").fetchall()
        }
        for worker, tool in (
            (RW.INQUIRY_WORKER, RW.INTAKE_TOOL),
            (RW.MATCHING_WORKER, RW.SEARCH_TOOL),
            (RW.AVAILABILITY_WORKER, RW.AVAILABILITY_TOOL),
        ):
            with self.subTest(worker=worker):
                self.assertIn(tool, declared)

    # -- 6. what the audit carries ------------------------------------------ #

    def test_the_audit_carries_the_worker_code_and_never_the_guest_words(self):
        fixture = self.running(seed="audit")
        fixture.workers.record_inquiry(
            fixture.conversation.conversation_id,
            source_message_id=fixture.message.case_message_id,
            fields=self.fields(),
        )

        reasons = self.allowed_reasons(fixture, tool=RW.INTAKE_TOOL)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith(RW.INQUIRY_WORKER))
        every_reason = " ".join(
            str(row["reason"])
            for row in fixture.conn.execute("SELECT reason FROM audit_events").fetchall()
        )
        self.assertNotIn(GUEST_WORD, every_reason)

    def test_the_worker_reports_the_principal_the_conversation_already_carries(self):
        fixture = self.running(seed="pid")
        self.assertEqual(fixture.workers.service.principal(), fixture.principal.principal_id)
        self.assertEqual(fixture.conversation.principal_id, fixture.principal.principal_id)


if __name__ == "__main__":
    unittest.main()

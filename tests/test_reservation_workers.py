"""Stage 4 — the §9 capability workers (RES-W01…W09), pinned as claims.

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
   wrote (§56 + §8);
10. the follow-up worker files an **internal task** and contacts nobody — ``messages`` refuses a
   machine author, so "the AI followed up" cannot mean "the AI wrote to the guest" (§56's LOW
   band: internal task/reminder);
11. the hold worker takes one unit out of sale only through the same door and the owning module's
   own gate, deriving the window from the confirmed stay, the guest from the case row and the
   policy version from the lookup the ranking and the price already used;
12. the booking status worker **answers and never confirms**: §9's three gates are read out
   from the modules that own them, ``booking_id`` stays empty until a human's confirmation made
   one, and a hold that was released does not secure the slot;
13. the amend/cancel assistant **answers and routes, never decides**: the binding cancellation
   ladder is read from the TERMS snapshot of the version the guest actually accepted (§11 — never
   today's policy), a notice inside every threshold reports *no step applies* rather than a zero
   refund, and a change request becomes a ``GUEST_FOLLOWUP`` task under a runbook that names it,
   because the queue's closed ``kind`` vocabulary cannot.

The clock is frozen, the ids are deterministic and the database is an in-memory one: every claim
here is reproducible, and none of them depends on how long a test took to run.
"""

from __future__ import annotations

import unittest

import ai_gateway as AG
import ai_service as AIS
import crm as CRM
import hold_states as HS
import intake as IN
import messages as MS
import ops_tasks as OT
import reservation_workers as RW
from primitives import DomainError, FrozenClock, IdFactory, parse_iso

import test_reservation_service as RST
from test_matching import seed_property, seed_unit
from test_wave4c_quotation_versions import policy as version_fixture

AGENCY = RST.AGENCY
HUMAN = RST.HUMAN
OPS = RST.OPS

#: The role a person holds when they record the guest's yes or release a hold.
RESERVATION_ROLE = "reservation_manager"

#: §56's allowance for MEDIUM tools, unpublished until an owner writes it down. The key is read
#: from :mod:`ai_service` rather than restated, so this file cannot drift from the one it tests.
ALLOWANCE_KEY = AIS.MEDIUM_ALLOWANCE_KEY

#: A word from the fixture's guest message ("Do you have a villa in Canggu for four nights in
#: November?"). Used to prove the audit never carries guest text.
GUEST_WORD = "Canggu"

STAY_IN = "2026-11-10"
STAY_OUT = "2026-11-14"

#: A follow-up deadline the caller owns: the workers publish no SLA, so the tests name one to
#: prove the deadline travels through untouched (and that a replay cannot move it).
FOLLOWUP_DUE = "2026-11-03T09:00:00+07:00"
FOLLOWUP_DUE_LATER = "2026-11-05T09:00:00+07:00"
FOLLOWUP_ASSIGNEE = "staff_014"

#: The clock the RES-W09 terms answer is read at: 40 days before ``STAY_IN``, so which ladder
#: step applies is a fact of the fixture, not of the day the suite happens to run. The estate's
#: ``at=`` arguments are datetimes, never ISO strings — ``to_iso`` refuses the string it is
#: handed, which is why the tests hand it a parsed one.
TERMS_AT = parse_iso("2026-10-01T09:00:00+07:00")

#: The runbook a routed request carries. The queue's ``kind`` cannot name a cancellation, so the
#: worker refuses to file work a person could not read — and the steps are the caller's, never
#: the guest's words.
SPECIAL_STEPS = ("confirm the guest's change request with a manager",)

#: The rate card a stay is priced from: one property, one season covering 2026, IDR. The rows
#: are spelled here because ``insert_rate_card`` takes database rows, not dataclasses — the
#: dataclass itself is built by ``quote_calculator`` inside the kernel.
CARD_ROW = {
    "rate_card_id": "card_v1",
    "property_id": "prop_1",
    "unit_scope": "property",
    "name": "Public 2026",
    "version": 1,
    "effective_from": "2026-01-01",
    "effective_to": "2026-12-31",
    "currency": "IDR",
    "weekly_discount_pct": "0",
    "monthly_discount_pct": "0",
    "source_doc_ref": "rates/2026-public.pdf",
    "approved_by": "user:owner-1",
    "created_at": RST.AT,
}

SEASON_ROW = {
    "season_id": "season_regular",
    "rate_card_id": "card_v1",
    "name": "Regular",
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
    "nightly_rate_minor": 1_500_000,
    "min_stay_nights": 1,
    "priority": 0,
    "created_at": RST.AT,
}


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
        adults: int | None = 2,
    ):
        """Confirm the inquiry's stay through the §6 authority — CRM, not the proposal layer.

        ``adults=None`` confirms the dates and no guest count, which is the state a pricing
        attempt must refuse rather than fill in.
        """

        crm = CRM.CrmService(
            fixture.conn,
            clock=FrozenClock.at(fixture.clock),
            ids=IdFactory(deterministic=True, seed="crm"),
            agency_id=AGENCY,
        )
        facts = [("check_in", check_in, "date"), ("check_out", check_out, "date")]
        if adults is not None:
            facts.append(("adults", str(adults), "integer"))
        for name, value, kind in facts:
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

    def versions_in_force(self, fixture, *, policy: bool = True, card: bool = True):
        """The policy version and the rate card a stay must be priced from (§11's basis).

        Both are stored through the kernel, which is the module that owns those tables, so the
        test cannot seed a shape the platform would refuse to write.
        """

        kernel = fixture.workers.kernel()
        if policy:
            kernel.insert_policy_version(version_fixture().as_row())
        if card:
            kernel.insert_rate_card(CARD_ROW, [SEASON_ROW])
        return kernel

    def priced(self, fixture, **kwargs):
        """Price the inquiry's stay for the seeded unit through the RES-W04 worker."""

        kwargs.setdefault("unit_id", "unit_1")
        return fixture.workers.price_stay(fixture.conversation.conversation_id, **kwargs)

    def sent(self, fixture, **kwargs):
        """Make the offer through the RES-W05 worker: price, record, open, send."""

        kwargs.setdefault("unit_id", "unit_1")
        return fixture.workers.send_quote(fixture.conversation.conversation_id, **kwargs)

    def held(self, fixture, **kwargs):
        """Ask for the tentative hold through the RES-W07 worker."""

        kwargs.setdefault("unit_id", "unit_1")
        return fixture.workers.request_hold(fixture.conversation.conversation_id, **kwargs)

    def offering(self, fixture, *, accept: bool = True):
        """Send the offer through RES-W05 and, when asked, record the guest's yes (§11)."""

        quotation = self.sent(fixture)
        if accept:
            fixture.workers.quotation().accept(
                quotation.quotation_version_id, actor=HUMAN, actor_role=RESERVATION_ROLE
            )
        return quotation

    def status_answer(self, fixture, **kwargs):
        """Ask the RES-W08 worker where the reservation stands."""

        kwargs.setdefault("hold_id", None)
        return fixture.workers.booking_status(
            fixture.conversation.conversation_id, **kwargs
        )

    def terms_answer(self, fixture, **kwargs):
        """Ask the RES-W09 worker what the accepted terms say about cancelling."""

        kwargs.setdefault("at", TERMS_AT)
        return fixture.workers.cancellation_terms(
            fixture.conversation.conversation_id, **kwargs
        )

    def special_request(self, fixture, **kwargs):
        """Route one guest change request through the RES-W09 worker."""

        kwargs.setdefault("request_kind", "CANCELLATION")
        kwargs.setdefault("slot", "cancel-nov")
        kwargs.setdefault("assignee", FOLLOWUP_ASSIGNEE)
        kwargs.setdefault("due_at", FOLLOWUP_DUE)
        kwargs.setdefault("checklist", SPECIAL_STEPS)
        return fixture.workers.special_request(
            fixture.conversation.conversation_id, **kwargs
        )

    @staticmethod
    def quote_row(fixture, quote_id: str):
        return fixture.conn.execute(
            "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)
        ).fetchone()

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
            RW.FOLLOWUP_WORKER: lambda: fixture.workers.follow_up(
                conversation_id,
                slot="no-answer-yet",
                due_at=FOLLOWUP_DUE,
                assignee=FOLLOWUP_ASSIGNEE,
            ),
            RW.HOLD_WORKER: lambda: fixture.workers.request_hold(
                conversation_id, unit_id="unit_1"
            ),
            RW.BOOKING_WORKER: lambda: fixture.workers.booking_status(conversation_id),
            RW.SPECIAL_REQUEST_WORKER: lambda: self.special_request(fixture),
            RW.SPECIAL_REQUEST_WORKER
            + " /terms": lambda: self.terms_answer(fixture),
        }
        for worker, attempt in attempts.items():
            with self.subTest(worker=worker):
                with self.assertRaises(DomainError) as raised:
                    attempt()
                self.assertEqual(raised.exception.code, "RES_WRITES_SUSPENDED")

        self.assertEqual(self.count(fixture, "intake_proposals"), 0)
        self.assertEqual(self.count(fixture, "match_runs"), 0)
        self.assertEqual(self.count(fixture, "availability_checks"), 0)
        self.assertEqual(self.count(fixture, "ops_tasks"), 0)
        self.assertEqual(self.count(fixture, "holds"), 0)
        self.assertEqual(self.count(fixture, "approval_requests"), 0)

    # -- 5. RES-W04: answer what the stay costs from the versions in force --- #

    def test_the_pricing_worker_answers_the_confirmed_stay_from_the_versions_in_force(self):
        """The answer is a figure pinned to the basis in force — and it writes nothing."""

        fixture = self.running(seed="w04")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        quote = self.priced(fixture)

        self.assertEqual(quote.unit_id, "unit_1")
        self.assertEqual(quote.check_in, STAY_IN)
        self.assertEqual(quote.check_out, STAY_OUT)
        self.assertEqual(quote.adults, 2)
        self.assertEqual(quote.nights, 4)
        self.assertGreater(quote.total.amount_minor, 0)
        self.assertEqual(quote.policy_version_id, "policy_v1")
        self.assertEqual(quote.rate_card_id, "card_v1")
        # §56 names the *answer*, so answering must not record a priced stay: the row is born in
        # RES-W05, after that step's door. Nothing here may leave a trace a guest could be shown.
        self.assertEqual(self.count(fixture, "quotes"), 0)
        rows = self.door_rows(fixture, tool=RW.PRICING_TOOL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trace_id, fixture.conversation.conversation_id)
        self.assertTrue(self.allowed_reasons(fixture, tool=RW.PRICING_TOOL)[0].startswith(RW.PRICING_WORKER))

    def test_the_same_basis_answers_the_same_price(self):
        """Pricing is a function of the versions in force — not of the clock or of an id."""

        fixture = self.running(seed="w04b")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        first = self.priced(fixture)
        second = self.priced(fixture)

        self.assertNotEqual(first.quote_id, second.quote_id)
        self.assertEqual(first.total.amount_minor, second.total.amount_minor)
        self.assertEqual(first.policy_version_id, second.policy_version_id)
        self.assertEqual(first.rate_card_id, second.rate_card_id)
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_the_pricing_worker_answers_for_the_confirmed_guest_count(self):
        """The count comes from the §6 authority in typed form — text in, number out."""

        fixture = self.running(seed="w04c")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture, adults=3)

        self.assertEqual(self.priced(fixture).adults, 3)

    def test_the_pricing_worker_will_not_price_a_stay_without_a_confirmed_guest_count(self):
        """Dates but no count is still no stay to price: the worker refuses before the door."""

        fixture = self.running(seed="w04d")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture, adults=None)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture)

        self.assertEqual(raised.exception.details["worker"], RW.PRICING_WORKER)
        self.assertEqual(list(raised.exception.details["missing"]), ["adults"])
        self.assertEqual(self.count(fixture, "quotes"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.PRICING_TOOL), [])

    def test_the_pricing_worker_will_not_price_a_stay_without_dates(self):
        """With nothing confirmed the shared window rule refuses, and it names *this* worker."""

        fixture = self.running(seed="w04d2")
        self.inventory(fixture)
        self.versions_in_force(fixture)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture)

        self.assertEqual(raised.exception.details["worker"], RW.PRICING_WORKER)
        self.assertEqual(list(raised.exception.details["missing"]), ["check_in", "check_out"])
        self.assertEqual(self.count(fixture, "quotes"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.PRICING_TOOL), [])

    def test_pricing_refuses_a_stay_no_rate_card_covers(self):
        """An absent basis is refused by name, never priced at zero."""

        fixture = self.running(seed="w04e")
        self.inventory(fixture)
        self.versions_in_force(fixture, card=False)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture)

        self.assertEqual(raised.exception.code, "QUOTE_NO_RATE_CARD")
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_pricing_refuses_a_stay_no_policy_covers(self):
        fixture = self.running(seed="w04f")
        self.inventory(fixture)
        self.versions_in_force(fixture, policy=False)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture)

        self.assertEqual(raised.exception.code, "QUOTE_NO_POLICY")
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_pricing_refuses_a_unit_this_tenant_does_not_have(self):
        fixture = self.running(seed="w04g")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture, unit_id="unit_9")

        self.assertEqual(raised.exception.code, "UNIT_NOT_FOUND")
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_a_read_only_mode_still_answers_a_price_and_still_writes_nothing(self):
        """READ_ONLY reaches what changes nothing — and pricing is exactly that (§17)."""

        fixture = self.running(seed="w04h")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        fixture.gateway.set_mode(
            fixture.principal.principal_id,
            AIS.AI_STATUS_READ_ONLY,
            actor=HUMAN,
            actor_role="owner",
            reason="drill",
        )

        self.assertGreater(self.priced(fixture).total.amount_minor, 0)
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_a_suspended_principal_cannot_answer_a_price(self):
        """The ladder's lower rungs stop it: §17 leaves a suspended AI no voice at all."""

        fixture = self.running(seed="w04h2")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        fixture.gateway.set_mode(
            fixture.principal.principal_id,
            AIS.AI_STATUS_SUSPENDED,
            actor=HUMAN,
            actor_role="owner",
            reason="drill",
        )

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture)

        self.assertEqual(raised.exception.code, "AI_PRINCIPAL_NOT_ACTIVE")
        self.assertEqual(self.count(fixture, "quotes"), 0)

    def test_a_caller_cannot_hand_the_price_a_guest_count_it_would_have_to_repair(self):
        """The worker passes the caller's value on; the request object is what refuses it."""

        fixture = self.running(seed="w04i")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.priced(fixture, adults="three")

        self.assertEqual(raised.exception.code, "GUESTS_INVALID")
        self.assertEqual(self.count(fixture, "quotes"), 0)

    # -- 6. RES-W05: make the offer stand ------------------------------------ #

    def test_the_offer_worker_prices_records_and_sends_in_one_gated_step(self):
        """§56 names one act for the offer; with the allowance published, all four parts land."""

        fixture = self.running(seed="w05")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))

        quotation = self.sent(fixture)

        self.assertEqual(quotation.status, "SENT")
        self.assertEqual(quotation.version, 1)
        self.assertIsNotNone(quotation.sent_at)
        self.assertEqual(quotation.check_in, STAY_IN)
        self.assertEqual(quotation.check_out, STAY_OUT)
        self.assertEqual(self.count(fixture, "quotes"), 1)
        self.assertEqual(self.count(fixture, "quotation_versions"), 1)
        price_row = fixture.conn.execute("SELECT * FROM quotes").fetchone()
        self.assertEqual(str(price_row["agency_id"]), AGENCY)
        self.assertEqual(str(price_row["policy_version_id"]), "policy_v1")
        self.assertEqual(str(price_row["rate_card_id"]), "card_v1")
        rows = self.door_rows(fixture, tool=RW.QUOTE_TOOL)
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.allowed_reasons(fixture, tool=RW.QUOTE_TOOL)[0].startswith(RW.QUOTATION_WORKER))

    def test_the_offer_worker_refuses_before_it_writes_anything_with_no_allowance(self):
        """Deny-first: no published allowance, no quote row, no version, nothing to show a guest."""

        fixture = self.running(seed="w05b")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.sent(fixture)

        self.assertEqual(raised.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.count(fixture, "quotes"), 0)
        self.assertEqual(self.count(fixture, "quotation_versions"), 0)

    def test_a_read_only_mode_cannot_send_an_offer_even_with_the_allowance(self):
        """The other half of the §17 pair: the answer still comes, the offer does not start."""

        fixture = self.running(seed="w05c")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        fixture.gateway.set_mode(
            fixture.principal.principal_id,
            AIS.AI_STATUS_READ_ONLY,
            actor=HUMAN,
            actor_role="owner",
            reason="drill",
        )

        with self.assertRaises(DomainError) as raised:
            self.sent(fixture)

        self.assertEqual(raised.exception.code, "AI_MODE_REFUSED")
        self.assertEqual(self.count(fixture, "quotes"), 0)
        self.assertEqual(self.count(fixture, "quotation_versions"), 0)

    def test_sending_again_opens_the_next_version_and_supersedes_the_one_the_guest_held(self):
        """The stay is the lineage, and only a *sent* version takes the previous one out."""

        fixture = self.running(seed="w05d")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))

        first = self.sent(fixture)
        second = self.sent(fixture)

        self.assertEqual((first.version, second.version), (1, 2))
        self.assertEqual(self.count(fixture, "quotation_versions"), 2)
        self.assertEqual(self.count(fixture, "quotation_versions WHERE status = 'SENT'"), 1)
        self.assertEqual(
            self.count(fixture, "quotation_versions WHERE status = 'SUPERSEDED'"), 1
        )
        self.assertEqual(str(second.supersedes_id), first.quotation_version_id)
        # Two prices, because each offer records the stay it was made from.
        self.assertEqual(self.count(fixture, "quotes"), 2)

    def test_the_offer_worker_will_not_make_an_offer_without_a_confirmed_guest_count(self):
        """No count, no offer — and the refusal happens before the door, so nothing is recorded."""

        fixture = self.running(seed="w05e")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture, adults=None)

        with self.assertRaises(DomainError) as raised:
            self.sent(fixture)

        self.assertEqual(raised.exception.details["worker"], RW.QUOTATION_WORKER)
        self.assertEqual(list(raised.exception.details["missing"]), ["adults"])
        self.assertEqual(self.count(fixture, "quotes"), 0)
        self.assertEqual(self.count(fixture, "quotation_versions"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.QUOTE_TOOL), [])

    # -- RES-W06: the routine follow-up is our work, not a message ---------- #

    def test_the_followup_worker_files_an_internal_task_and_contacts_nobody(self):
        """§56's ``followup.routine`` is LOW because nothing leaves the building.

        The claim is a pair: the work lands on the agency's own queue, *and* no message exists
        afterwards. A draft would need a human author (``messages`` refuses a machine one), so
        "the AI followed up" can only mean "the AI put the follow-up where a person will see it".
        """

        fixture = self.running(seed="w06")
        task, created = fixture.workers.follow_up(
            fixture.conversation.conversation_id,
            slot="quote_offer",
            due_at=FOLLOWUP_DUE,
            assignee=FOLLOWUP_ASSIGNEE,
        )
        self.assertTrue(created)
        self.assertEqual(task.kind, OT.KIND_GUEST_FOLLOWUP)
        self.assertEqual(task.case_id, fixture.case.case_id)
        self.assertEqual(task.assignee, FOLLOWUP_ASSIGNEE)
        self.assertEqual(task.status, OT.STATUS_OPEN)
        # The deadline travels as an instant: the queue stores it in UTC, so the test compares
        # instants rather than spellings (the caller's +07:00 is the same moment).
        self.assertEqual(parse_iso(task.due_at), parse_iso(FOLLOWUP_DUE))
        self.assertEqual(self.count(fixture, "message_drafts"), 0)
        self.assertEqual(len(self.allowed_reasons(fixture, tool=RW.FOLLOWUP_TOOL)), 1)
        self.assertTrue(
            self.allowed_reasons(fixture, tool=RW.FOLLOWUP_TOOL)[0].startswith(RW.FOLLOWUP_WORKER)
        )

    def test_the_same_slot_files_one_followup_and_a_new_deadline_is_refused(self):
        """The key comes from the work, not from the clock — and the deadline is not ours to move.

        Same slot and same deadline replays (one task, ``created=False``); the same slot with a
        *different* deadline is refused by the queue rather than silently rescheduled, because a
        date someone already agreed to is not this worker's to change.
        """

        fixture = self.running(seed="w06-replay")
        conversation_id = fixture.conversation.conversation_id
        first, created = fixture.workers.follow_up(
            conversation_id, slot="no-answer-yet", due_at=FOLLOWUP_DUE, assignee=FOLLOWUP_ASSIGNEE
        )
        again, created_again = fixture.workers.follow_up(
            conversation_id, slot="no-answer-yet", due_at=FOLLOWUP_DUE, assignee=FOLLOWUP_ASSIGNEE
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.task_id, again.task_id)
        self.assertEqual(self.count(fixture, "ops_tasks"), 1)

        with self.assertRaises(OT.DuplicateIdempotencyKey):
            fixture.workers.follow_up(
                conversation_id,
                slot="no-answer-yet",
                due_at=FOLLOWUP_DUE_LATER,
                assignee=FOLLOWUP_ASSIGNEE,
            )
        self.assertEqual(self.count(fixture, "ops_tasks"), 1)

    def test_the_followup_worker_will_not_name_the_owner_itself(self):
        """A task without an owner is an unassigned promise: the caller names one, or nothing runs.

        The refusal happens *before* the door, so a refused attempt is indistinguishable from one
        that never happened — the same promise the estate's other fail-closed paths make.
        """

        fixture = self.running(seed="w06-assignee")
        with self.assertRaises(DomainError):
            fixture.workers.follow_up(
                fixture.conversation.conversation_id,
                slot="no-answer-yet",
                due_at=FOLLOWUP_DUE,
                assignee="  ",
            )
        self.assertEqual(self.count(fixture, "ops_tasks"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.FOLLOWUP_TOOL), [])

    def test_a_message_draft_cannot_be_authored_by_the_ai_which_is_why_followup_is_a_task(self):
        """Why RES-W06 files a task: ``messages`` refuses a machine author.

        Pinned here because it is the load-bearing fact behind that design. If a future change ever
        lets a machine author a draft, this test fails and the follow-up worker's shape deserves a
        second look — the AI would then be able to do the one thing §56's LOW band never named:
        write to the guest under a human's name.
        """

        fixture = self.running(seed="w06-authorship")
        messages = MS.MessageService(
            fixture.conn, clock=fixture.clock, ids=IdFactory(deterministic=True, seed="msg")
        )
        with self.assertRaises(MS.InvalidActor):
            messages.draft(
                case_id=fixture.case.case_id,
                template="quote_offer",
                channel="whatsapp_business",
                recipient_ref="guest_ref_001",
                bindings={},
                created_by=fixture.service.actor,
                created_by_role=fixture.service.actor_role,
                reason="the AI tries to write to the guest",
            )
        self.assertEqual(self.count(fixture, "message_drafts"), 0)

    # -- RES-W07: a tentative hold takes a slot out of sale ----------------- #

    def test_the_hold_worker_takes_the_confirmed_stay_out_of_sale(self):
        """MEDIUM, owner allow-listed — and everything it needs comes from a record, not a caller.

        The window is the inquiry's confirmed stay, the guest is the case's own reference and the
        policy version is the one the ranking and the price used. A hold therefore cannot be taken
        under rules no other step agreed to, and the expiry carries the property's timezone (§7).
        """

        fixture = self.running(seed="w07")
        self.inventory(fixture)
        self.confirm_stay(fixture)
        self.versions_in_force(fixture, card=False)  # a hold needs a policy, not a price
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))

        outcome = fixture.workers.request_hold(
            fixture.conversation.conversation_id, unit_id="unit_1"
        )
        row = fixture.conn.execute(
            "SELECT * FROM holds WHERE hold_id = ?", (outcome.hold_id,)
        ).fetchone()
        self.assertEqual(row["status"], HS.HOLD_CONFIRMED)
        self.assertEqual((row["check_in"], row["check_out"]), (STAY_IN, STAY_OUT))
        self.assertEqual(row["guest_ref"], fixture.case.guest_ref)
        self.assertEqual(row["policy_version_id"], "policy_v1")
        self.assertEqual(row["unit_id"], "unit_1")
        self.assertEqual(row["expires_timezone"], "Asia/Makassar")
        self.assertFalse(outcome.replayed)
        self.assertEqual(self.count(fixture, "holds"), 1)
        self.assertEqual(self.count(fixture, "message_drafts"), 0)
        # Two gates, and the audit shows both. The *door* (this module's) is the gateway row that
        # carries the worker code; the owning module then re-checks the same actor through
        # ``ai_service`` before it writes, and that check leaves the second row. Neither is a
        # second opinion about the other: the door decides whether the AI may act at all, the
        # kernel decides whether this hold is legal.
        self.assertEqual(len(self.door_rows(fixture, tool=RW.HOLD_TOOL)), 1)
        reasons = self.allowed_reasons(fixture, tool=RW.HOLD_TOOL)
        self.assertEqual(len(reasons), 2)
        self.assertEqual(
            len([reason for reason in reasons if reason.startswith(RW.HOLD_WORKER)]), 1
        )

    def test_the_hold_worker_refuses_before_it_writes_anything_with_no_allowance(self):
        """Nothing MEDIUM moves until the owner publishes the allowance — not even a hold."""

        fixture = self.running(seed="w07-denied")
        self.inventory(fixture)
        self.confirm_stay(fixture)
        self.versions_in_force(fixture, card=False)

        with self.assertRaises(DomainError):
            fixture.workers.request_hold(fixture.conversation.conversation_id, unit_id="unit_1")
        self.assertEqual(self.count(fixture, "holds"), 0)

    def test_the_same_window_is_one_hold_question(self):
        """A retry asks once: the kernel replays its own row instead of taking a second slot."""

        fixture = self.running(seed="w07-replay")
        self.inventory(fixture)
        self.confirm_stay(fixture)
        self.versions_in_force(fixture, card=False)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        conversation_id = fixture.conversation.conversation_id

        first = fixture.workers.request_hold(conversation_id, unit_id="unit_1")
        again = fixture.workers.request_hold(conversation_id, unit_id="unit_1")
        self.assertEqual(first.hold_id, again.hold_id)
        self.assertTrue(again.replayed)
        self.assertEqual(self.count(fixture, "holds"), 1)

    def test_the_hold_worker_will_not_guess_the_window(self):
        """No confirmed stay, no hold — and the refusal names the worker that actually asked."""

        fixture = self.running(seed="w07-nowindow")
        self.inventory(fixture)
        self.versions_in_force(fixture, card=False)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))

        with self.assertRaises(RW.MissingRequirements) as raised:
            fixture.workers.request_hold(fixture.conversation.conversation_id, unit_id="unit_1")
        self.assertIn(RW.HOLD_WORKER, str(raised.exception))
        self.assertEqual(self.count(fixture, "holds"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.HOLD_TOOL), [])

    def test_a_hold_under_a_stay_no_policy_covers_is_refused(self):
        """The version in force is looked up, never assumed: no policy, no hold."""

        fixture = self.running(seed="w07-nopolicy")
        self.inventory(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))

        with self.assertRaises(RW.NoPolicyInForce):
            fixture.workers.request_hold(fixture.conversation.conversation_id, unit_id="unit_1")
        self.assertEqual(self.count(fixture, "holds"), 0)

    def test_the_hold_worker_reports_the_unit_it_does_not_have(self):
        """An unknown unit is the kernel's refusal, not a second opinion written here."""

        fixture = self.running(seed="w07-nounit")
        self.inventory(fixture)
        self.confirm_stay(fixture)
        self.versions_in_force(fixture, card=False)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))

        with self.assertRaises(DomainError) as raised:
            fixture.workers.request_hold(
                fixture.conversation.conversation_id, unit_id="unit_elsewhere"
            )
        self.assertEqual(raised.exception.code, "UNIT_NOT_FOUND")
        self.assertEqual(self.count(fixture, "holds"), 0)

    # -- RES-W08: the booking status ---------------------------------------- #

    def test_the_booking_worker_reads_the_gates_and_claims_nothing(self):
        """§9's invariant as a readout: the gates are checked, the booking is not claimed.

        With the version accepted and the hold confirmed, two of §9's three gates are satisfied and
        the third — verified client funds — has no home on this platform yet, so the answer says so
        instead of implying it. Nothing in the answer calls this a booking: it is not one until a
        human confirms it, and §9 reads the same way.
        """

        fixture = self.running(seed="w08")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        self.offering(fixture)

        answer = self.status_answer(fixture, hold_id=hold.hold_id)

        self.assertTrue(answer.commercial["accepted"])
        self.assertTrue(answer.inventory["secures_slot"])
        self.assertEqual(answer.inventory["status"], HS.HOLD_CONFIRMED)
        self.assertEqual(answer.missing, (RW.GATE_FINANCIAL,))
        self.assertIsNone(answer.booking_id)
        self.assertIsNone(answer.status)
        self.assertFalse(answer.occupies_slot)
        self.assertEqual(answer.financial["note"], RW.FINANCIAL_GATE_NOTE)
        self.assertFalse(answer.financial["recorded"])
        self.assertEqual(answer.as_dict()["missing"], [RW.GATE_FINANCIAL])
        self.assertEqual(len(self.door_rows(fixture, tool=RW.BOOKING_TOOL)), 1)
        reasons = self.allowed_reasons(fixture, tool=RW.BOOKING_TOOL)
        self.assertTrue(reasons[0].startswith(RW.BOOKING_WORKER))
        self.assertEqual(self.count(fixture, "bookings"), 0)

    def test_the_booking_worker_reports_the_booking_a_human_confirmed(self):
        """Once a human confirms, the row is the answer — and an unpaid one still shows the gap.

        The confirmation itself is not the worker's: the kernel writes the booking under the human
        actor, the case is linked to it through :mod:`cases`, and only then does the answer have a
        booking to report. Confirmed with nothing paid, §9's financial gate stays open, and the
        answer keeps saying so.
        """

        fixture = self.running(seed="w08b")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        quotation = self.offering(fixture)
        booking = fixture.workers.kernel().confirm_from_hold(
            hold_id=hold.hold_id,
            quote_id=quotation.quote_id,
            idempotency_key="idem_book_w08",
            command_id="cmd_book_w08",
            adults=2,
            actor=HUMAN,
            actor_role=RESERVATION_ROLE,
        )
        fixture.workers.cases().link_booking(
            fixture.case.case_id,
            booking.booking_id,
            actor=HUMAN,
            command_id="cmd_link_w08",
        )

        answer = self.status_answer(fixture, hold_id=hold.hold_id)

        self.assertEqual(answer.booking_id, booking.booking_id)
        self.assertEqual(answer.status, HS.HOLD_CONFIRMED.replace("HOLD_", ""))
        self.assertTrue(answer.occupies_slot)
        self.assertEqual(answer.inventory["source"], "booking")
        self.assertTrue(answer.inventory["secures_slot"])
        self.assertEqual(answer.missing, (RW.GATE_FINANCIAL,))
        self.assertFalse(answer.financial["recorded"])
        self.assertEqual(answer.financial["amount_paid_minor"], 0)

    def test_a_released_hold_does_not_secure_the_slot(self):
        """§9's inventory gate is the supplier's confirmation — a released slot is not secured.

        The guest let the hold go. The answer must not tell them the slot is still theirs, and the
        command's absence of a booking is what makes that structurally true: no row, no claim.
        """

        fixture = self.running(seed="w08c")
        self.inventory(fixture)
        self.versions_in_force(fixture, card=False)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        fixture.workers.kernel().release_hold(
            hold.hold_id, reason="guest chose another villa", actor=HUMAN
        )

        answer = self.status_answer(fixture, hold_id=hold.hold_id)

        self.assertEqual(answer.inventory["status"], HS.HOLD_RELEASE_CONFIRMED)
        self.assertFalse(answer.inventory["secures_slot"])
        self.assertEqual(
            answer.missing,
            (RW.GATE_COMMERCIAL, RW.GATE_INVENTORY, RW.GATE_FINANCIAL),
        )
        self.assertIsNone(answer.booking_id)
        self.assertFalse(answer.occupies_slot)
        self.assertEqual(self.count(fixture, "bookings"), 0)

    def test_the_booking_worker_refuses_when_there_is_nothing_to_report(self):
        """No booking on the case and no hold named: refuse, do not guess which hold is meant."""

        fixture = self.running(seed="w08d")

        with self.assertRaises(RW.MissingRequirements) as raised:
            self.status_answer(fixture)

        self.assertIn(RW.BOOKING_WORKER, str(raised.exception))
        self.assertEqual(self.door_rows(fixture, tool=RW.BOOKING_TOOL), [])

    # -- 9. RES-W09: amend / cancel assist ---------------------------------- #

    def test_the_terms_answer_reads_the_ladder_of_the_accepted_version(self):
        """The binding ladder, not today's policy — read-only, and never a money figure.

        §11's rule: what a guest agreed to is the version they accepted, so the ladder comes out of
        that version's TERMS snapshot. The answer names the version it read, says which step the
        sitting notice falls in, and carries no amount at all — a refund is the finance module's
        fact and a person's decision.
        """

        fixture = self.running(seed="w09a")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        quotation = self.offering(fixture)

        answer = self.terms_answer(fixture, hold_id=hold.hold_id)

        self.assertEqual(answer.worker, RW.SPECIAL_REQUEST_WORKER)
        self.assertEqual(
            answer.binding["quotation_version_id"], quotation.quotation_version_id
        )
        self.assertEqual(answer.binding["status"], "ACCEPTED")
        self.assertEqual(answer.binding["source"], "hold")
        self.assertEqual(len(answer.binding["terms_fingerprint"]), 64)
        self.assertEqual(
            set(answer.binding),
            {
                "quotation_version_id",
                "quote_id",
                "version",
                "status",
                "check_in",
                "source",
                "accepted_at",
                "terms_fingerprint",
            },
        )
        self.assertEqual(answer.notice_days, 40)
        self.assertEqual(answer.ladder, ({"min_days_before": 0, "refund_pct": "100"},))
        self.assertEqual(answer.applicable, {"min_days_before": 0, "refund_pct": "100"})
        self.assertEqual(answer.note, RW.TERMS_NOTE_ACCEPTED)
        self.assertEqual(
            answer.as_dict()["applicable"], {"min_days_before": 0, "refund_pct": "100"}
        )
        # A read leaves nothing behind: no task, no approval, no booking.
        self.assertEqual(self.count(fixture, "ops_tasks"), 0)
        self.assertEqual(self.count(fixture, "approval_requests"), 0)
        self.assertEqual(self.count(fixture, "bookings"), 0)
        self.assertTrue(
            self.allowed_reasons(fixture, tool=RW.TERMS_TOOL)[-1].startswith(
                RW.SPECIAL_REQUEST_WORKER
            )
        )

    def test_the_terms_answer_will_not_invent_terms_without_an_acceptance(self):
        """An offer that was sent but never accepted binds nobody: the answer says so."""

        fixture = self.running(seed="w09b")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        self.offering(fixture, accept=False)

        answer = self.terms_answer(fixture, hold_id=hold.hold_id)

        self.assertIsNone(answer.binding)
        self.assertIsNone(answer.ladder)
        self.assertIsNone(answer.applicable)
        self.assertIsNone(answer.notice_days)
        self.assertEqual(answer.note, RW.TERMS_NOTE_NO_VERSION)
        # Reading for a yes that does not exist must not create one.
        self.assertEqual(self.count(fixture, "quotation_acceptances"), 0)

    def test_a_cancellation_request_becomes_readable_work_and_decides_nothing(self):
        """The AI routes the request; a person decides it. Nothing else moves.

        The task's subject is the booking, the runbook names what was asked (the queue's ``kind``
        cannot), and the three things a cancellation would actually need — an approval, a message,
        a booking status change — are all still absent when the worker returns.
        """

        fixture = self.running(seed="w09c")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)
        self.allow_medium(fixture, tools=(RW.HOLD_TOOL,))
        hold = self.held(fixture)
        self.allow_medium(fixture, tools=(RW.QUOTE_TOOL,))
        quotation = self.offering(fixture)
        booking = fixture.workers.kernel().confirm_from_hold(
            hold_id=hold.hold_id,
            quote_id=quotation.quote_id,
            idempotency_key="idem_book_w09",
            command_id="cmd_book_w09",
            adults=2,
            actor=HUMAN,
            actor_role=RESERVATION_ROLE,
        )
        fixture.workers.cases().link_booking(
            fixture.case.case_id,
            booking.booking_id,
            actor=HUMAN,
            command_id="cmd_link_w09",
        )
        self.allow_medium(fixture, tools=(RW.SPECIAL_REQUEST_TOOL,))

        task, created = self.special_request(fixture)

        self.assertTrue(created)
        self.assertEqual(task.kind, OT.KIND_GUEST_FOLLOWUP)
        self.assertEqual(task.booking_id, booking.booking_id)
        self.assertEqual(task.assignee, FOLLOWUP_ASSIGNEE)
        self.assertEqual(task.status, OT.STATUS_OPEN)
        self.assertEqual(self.count(fixture, "ops_tasks"), 1)
        # The decision is not here: no approval was raised, no message written, no state moved.
        self.assertEqual(self.count(fixture, "approval_requests"), 0)
        self.assertEqual(self.count(fixture, "message_drafts"), 0)
        self.assertEqual(
            fixture.workers.kernel().get_booking(booking.booking_id)["status"],
            booking.status,
        )
        rows = self.door_rows(fixture, tool=RW.SPECIAL_REQUEST_TOOL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trace_id, fixture.conversation.conversation_id)
        self.assertTrue(
            self.allowed_reasons(fixture, tool=RW.SPECIAL_REQUEST_TOOL)[0].startswith(
                RW.SPECIAL_REQUEST_WORKER
            )
        )
        # The same request twice is the same piece of work …
        same, created_again = self.special_request(fixture)
        self.assertFalse(created_again)
        self.assertEqual(same.task_id, task.task_id)
        self.assertEqual(self.count(fixture, "ops_tasks"), 1)
        # … while a different change on the same slot is a different piece of work.
        _, created_other = self.special_request(fixture, request_kind="AMENDMENT")
        self.assertTrue(created_other)
        self.assertEqual(self.count(fixture, "ops_tasks"), 2)

    def test_a_change_request_is_deny_first_until_the_owner_allows_the_tool(self):
        """§56 deny-first again: with no published allowance the request is refused, and nothing
        is written."""

        fixture = self.running(seed="w09d")
        self.inventory(fixture)
        self.versions_in_force(fixture)
        self.confirm_stay(fixture)

        with self.assertRaises(DomainError) as raised:
            self.special_request(fixture)
        self.assertEqual(raised.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(self.count(fixture, "ops_tasks"), 0)

    def test_the_special_request_refuses_an_unknown_kind_and_an_empty_runbook(self):
        """Two refusals that happen before the door: a name nobody can work, and work nobody
        can read."""

        fixture = self.running(seed="w09e")

        with self.assertRaises(RW.MissingRequirements) as unknown:
            self.special_request(fixture, request_kind="BANANA")
        self.assertEqual(unknown.exception.code, "RES_WORKER_UNKNOWN_REQUEST_KIND")
        with self.assertRaises(RW.MissingRequirements) as unreadable:
            self.special_request(fixture, checklist=())
        self.assertEqual(
            unreadable.exception.code, "RES_WORKER_SPECIAL_REQUEST_NO_STEPS"
        )
        self.assertEqual(self.count(fixture, "ops_tasks"), 0)
        self.assertEqual(self.door_rows(fixture, tool=RW.SPECIAL_REQUEST_TOOL), [])

    # -- 7. the tools the workers hold --------------------------------------- #

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
            (RW.PRICING_WORKER, RW.PRICING_TOOL),
            (RW.QUOTATION_WORKER, RW.QUOTE_TOOL),
            (RW.FOLLOWUP_WORKER, RW.FOLLOWUP_TOOL),
            (RW.HOLD_WORKER, RW.HOLD_TOOL),
            (RW.BOOKING_WORKER, RW.BOOKING_TOOL),
            (RW.SPECIAL_REQUEST_WORKER, RW.TERMS_TOOL),
            (RW.SPECIAL_REQUEST_WORKER, RW.SPECIAL_REQUEST_TOOL),
        ):
            with self.subTest(worker=worker):
                self.assertIn(tool, declared)

    # -- 8. what the audit carries ------------------------------------------ #

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

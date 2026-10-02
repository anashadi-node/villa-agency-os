"""WAVE 4-A-2 — the §7 check-availability request flow.

§7 opens with ``CLIENT INQUIRY → CHECK AVAILABILITY → AVAILABLE → TENTATIVE HOLD
REQUESTED → ...``, and Appendix A lists ``CHECK_REQUESTED`` and ``CHECKING`` among
the availability states. ADR-0019 recorded them honestly as "vocabulary without a
flow": nothing in the repository could *enter* those states, because
``availability_evidence`` only holds answers a source reports. This suite pins the
missing half — the request itself.

What is pinned here:

1. The flow exists and its states are reachable: a check is requested
   (``CHECK_REQUESTED``), the platform starts working it (``CHECKING``), and an
   answer closes it — after which the reported state is the answer's, not the
   request's.
2. The request is append-only: its state lives in an event log, never in a
   mutable column, so nothing can rewrite what happened.
3. ``ANSWERED`` is a *request* state, not an availability state. The two
   vocabularies share a table but not a meaning, and the boundary is pinned.
4. §56 is enforced rather than assumed. ``availability.request`` is a MEDIUM
   policy-controlled tool, so a machine actor is refused until the owner
   publishes the controlling policy, and admitted by publishing it — no code
   change either way.
5. Answering cannot invent an answer: the evidence must exist, belong to the same
   unit, and be this agency's. Staleness is *not* checked at write time — it is a
   property of reading (ADR-0019) — so a check answered with stale evidence
   reports ``RECONFIRMATION_REQUIRED`` instead of being refused on the way in.
"""

from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path

import availability as AV
import reservation_kernel as RK
from ai_service import (
    AiMediumToolNotAllowed,
    AiService,
    UnknownAiPrincipal,
)
from policy_engine import PolicyResolver
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-26T00:00:00+00:00"
#: Both dates sit after 2026-10-02, because the deny registry already has a revision
#: starting that day (the owner's four guardrails, ADR-0039) and publishing "earlier" is
#: refused as backdated — the engine is right, so the fixture moved.
LATER = "2026-10-03"  # the allowance becomes effective here
AFTER_LATER = "2026-10-04T00:00:00+00:00"  # after LATER: reads the revision it just published
STALE = "2026-09-20T00:00:00+00:00"  # days before NOW: past every level's window
AGENCY = "agency_default"
OTHER_AGENCY = "agency_other"
ALLOWANCE_KEY = "security.ai.medium_tool_allowance"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "src" / "schema"

CHECK_IN = "2026-10-01"
CHECK_OUT = "2026-10-03"


def _schema(name: str) -> str:
    return (SCHEMA_DIR / name).read_text()


def _block(sql: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
    assert match is not None, f"the {table} table is not in the schema"
    return match.group(1)


def _values(text: str, pattern: str) -> tuple[str, ...]:
    match = re.search(pattern, text, re.S)
    assert match is not None, f"predicate not found: {pattern}"
    return tuple(value.strip().strip("'") for value in match.group(1).split(","))


def build_kernel(*, at: str = NOW, agency_id: str = AGENCY) -> RK.ReservationKernel:
    """One property, one unit — the inventory a check asks about."""
    kernel = RK.ReservationKernel.open(
        ":memory:",
        clock=FrozenClock.at(at),
        ids=IdFactory(deterministic=True, seed="w4a2"),
        agency_id=agency_id,
    )
    kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
    kernel.upsert_unit(
        unit_id="unit_1", property_id="prop_1", name="Villa unit_1", base_capacity=2, max_guests=6
    )
    return kernel


# -- 1. the vocabulary -------------------------------------------------------- #


class VocabularyTestCase(unittest.TestCase):
    """The three flow states, and where the boundary with Appendix A sits."""

    def test_the_flow_has_exactly_three_states(self):
        self.assertEqual(AV.CHECK_STATES, ("CHECK_REQUESTED", "CHECKING", "ANSWERED"))

    def test_the_open_states_are_the_ones_a_caller_waits_in(self):
        self.assertEqual(AV.CHECK_OPEN_STATES, ("CHECK_REQUESTED", "CHECKING"))
        self.assertEqual(AV.CHECK_OPEN_STATES, AV.REQUEST_STATES)

    def test_answered_is_not_an_availability_state(self):
        """``ANSWERED`` says the *request* is over; it never describes inventory.

        Appendix A is the availability vocabulary, and an availability state that
        means "we finished asking" would be a second, contradictory answer.
        """
        self.assertNotIn(AV.ANSWERED_STATE, AV.AVAILABILITY_STATES)
        self.assertNotIn(AV.ANSWERED_STATE, AV.SOURCE_RESULTS)

    def test_the_platform_driven_states_are_the_appendix_a_ones(self):
        for state in AV.CHECK_OPEN_STATES:
            self.assertIn(state, AV.AVAILABILITY_STATES)

    def test_every_state_has_a_transition_row(self):
        self.assertEqual(tuple(AV.CHECK_TRANSITIONS), AV.CHECK_STATES)

    def test_answer_is_terminal(self):
        self.assertEqual(AV.CHECK_TRANSITIONS[AV.ANSWERED_STATE], ())
        self.assertFalse(AV.check_transition_allowed(AV.ANSWERED_STATE, "CHECKING"))

    def test_checking_is_reachable_and_forward_only(self):
        self.assertTrue(AV.check_transition_allowed("CHECK_REQUESTED", "CHECKING"))
        self.assertFalse(AV.check_transition_allowed("CHECKING", "CHECK_REQUESTED"))

    def test_a_source_can_answer_without_an_explicit_checking_step(self):
        """A synchronous supplier API answers in the same call; forcing a CHECKING
        row would be theatre, not a record of anything that happened."""
        self.assertTrue(AV.check_transition_allowed("CHECK_REQUESTED", AV.ANSWERED_STATE))

    def test_an_unknown_state_is_never_allowed(self):
        self.assertFalse(AV.check_transition_allowed("INVENTED", "CHECKING"))
        self.assertFalse(AV.check_transition_allowed("CHECKING", "INVENTED"))

    def test_the_answer_states_are_the_ones_a_caller_may_act_on(self):
        self.assertEqual(
            AV.ANSWERED_STATES, ("AVAILABLE", "NOT_AVAILABLE", "AVAILABLE_BUT_RESTRICTED")
        )
        for state in AV.ANSWERED_STATES:
            self.assertIn(state, AV.SOURCE_RESULTS)


# -- 2. the schema ------------------------------------------------------------ #


class CheckSchemaTestCase(unittest.TestCase):
    """A state the CHECK constraint rejects is a state that cannot be written."""

    def setUp(self):
        self.sql = _schema("001_initial.sql")
        self.pg = _schema("001_initial.pg.sql")

    def test_the_request_carries_the_agency_scope(self):
        columns = _block(self.sql, "availability_checks")
        self.assertIn("agency_id", columns)
        self.assertIn("fk_availability_checks_agency", columns)

    def test_the_event_log_is_scoped_through_its_parent(self):
        """Wave 1-E: a child table reaches its tenant through the parent FK, so a
        second copy of the scope can never disagree with the first."""
        columns = _block(self.sql, "availability_check_events")
        self.assertNotIn("agency_id", columns)
        self.assertIn("fk_ace_check", columns)

    def test_the_event_states_match_the_vocabulary(self):
        columns = _block(self.sql, "availability_check_events")
        self.assertEqual(
            _values(columns, r"state\s+TEXT\s+NOT NULL CHECK \(state IN \(([^)]*)\)\)"),
            AV.CHECK_STATES,
        )

    def test_the_postgres_twin_agrees(self):
        self.assertEqual(
            _values(
                _block(self.pg, "availability_check_events"),
                r"state\s+TEXT\s+NOT NULL CHECK \(state IN \(([^)]*)\)\)",
            ),
            AV.CHECK_STATES,
        )

    def test_the_event_sequence_is_unique_per_check(self):
        self.assertIn("uq_ace_seq", _block(self.sql, "availability_check_events"))

    def test_an_answer_must_carry_the_evidence_it_answers_with(self):
        columns = _block(self.sql, "availability_check_events")
        self.assertIn("ck_ace_answer_has_evidence", columns)
        self.assertIn("ck_ace_open_has_no_evidence", columns)

    def test_both_tables_are_append_only(self):
        """The state is derived from the log, so a mutable row would let the log
        and the state disagree — and the log is the one that must win."""
        for table in ("availability_checks", "availability_check_events"):
            for verb in ("UPDATE", "DELETE"):
                name = f"trg_{table}_no_{verb.lower()}"
                self.assertIn(name, self.sql, f"{name} is missing")

    def test_the_append_only_triggers_are_unconditional(self):
        """The PG twin generator ignores ``WHEN``, so a conditional trigger would
        diverge between the two engines instead of protecting both."""
        for match in re.finditer(
            r"CREATE TRIGGER (trg_availability_check\w*_no_\w+)\b(.*?)END;", self.sql, re.S
        ):
            self.assertNotIn("WHEN", match.group(2), f"{match.group(1)} is conditional")

    def test_the_window_must_move_forward(self):
        self.assertIn("ck_availability_checks_window", _block(self.sql, "availability_checks"))

    def test_the_event_log_indexes_its_parent(self):
        self.assertIn("ix_ace_check", self.sql)


# -- 3. the request flow ------------------------------------------------------ #


class RequestFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.kernel = build_kernel()
        self.addCleanup(self.kernel.close)
        self.service = AV.AvailabilityService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )

    # -- helpers ----------------------------------------------------------- #

    def request(self, *, key="idem_check_0001", check_in=CHECK_IN, check_out=CHECK_OUT,
                unit_id="unit_1", actor="user:anas", actor_role="owner"):
        return self.kernel.request_availability_check(
            unit_id=unit_id,
            check_in=check_in,
            check_out=check_out,
            actor=actor,
            actor_role=actor_role,
            idempotency_key=key,
        )

    def evidence(self, *, result="AVAILABLE", captured_at=None, level="A",
                 source_type="property_api", unit_id="unit_1", restrictions=None):
        return self.kernel.record_evidence(
            unit_id=unit_id,
            source_type=source_type,
            evidence_level=level,
            result=result,
            captured_at=captured_at,
            restrictions=restrictions,
        )

    def events(self, check_id):
        return self.kernel.conn.execute(
            "SELECT * FROM availability_check_events WHERE check_id = ? ORDER BY seq", (check_id,)
        ).fetchall()

    def audit_actions(self):
        return [
            row["action"]
            for row in self.kernel.conn.execute("SELECT action FROM audit_events ORDER BY seq")
        ]

    # -- requesting -------------------------------------------------------- #

    def test_a_request_enters_check_requested(self):
        outcome = self.request()
        self.assertEqual(outcome.state, "CHECK_REQUESTED")
        self.assertEqual(outcome.unit_id, "unit_1")
        self.assertEqual(outcome.check_in, CHECK_IN)
        self.assertIsNone(outcome.evidence_id)
        self.assertFalse(outcome.replayed)

    def test_the_request_is_recorded_as_a_first_event(self):
        outcome = self.request()
        events = self.events(outcome.check_id)
        self.assertEqual([row["state"] for row in events], ["CHECK_REQUESTED"])
        self.assertEqual([row["seq"] for row in events], [1])
        self.assertEqual(events[0]["actor"], "user:anas")

    def test_the_state_is_derived_from_the_log_not_stored(self):
        """A stored copy would drift; the request row must carry no state column."""
        columns = {row[1] for row in self.kernel.conn.execute("PRAGMA table_info(availability_checks)")}
        self.assertNotIn("state", columns)
        self.assertNotIn("status", columns)

    def test_a_replayed_request_returns_the_same_check(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first.check_id, second.check_id)
        self.assertTrue(second.replayed)
        self.assertEqual(len(self.events(first.check_id)), 1)

    def test_reusing_a_key_with_a_different_payload_is_refused(self):
        self.request()
        with self.assertRaises(DomainError) as exc:
            self.request(check_out="2026-10-05")
        self.assertEqual(exc.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_a_short_idempotency_key_is_refused(self):
        with self.assertRaises(DomainError) as exc:
            self.request(key="short")
        self.assertEqual(exc.exception.code, "INVALID_FIELD_LENGTH")

    def test_an_unknown_unit_is_refused(self):
        with self.assertRaises(DomainError) as exc:
            self.request(unit_id="unit_missing")
        self.assertEqual(exc.exception.code, "UNIT_NOT_FOUND")

    def test_a_window_that_does_not_move_forward_is_refused(self):
        for check_in, check_out in ((CHECK_OUT, CHECK_IN), (CHECK_IN, CHECK_IN)):
            with self.assertRaises(DomainError) as exc:
                self.request(check_in=check_in, check_out=check_out)
            self.assertEqual(exc.exception.code, "INVALID_STAY_RANGE")

    def test_a_blank_actor_is_refused(self):
        with self.assertRaises(DomainError):
            self.request(actor="")

    def test_the_request_is_audited(self):
        outcome = self.request()
        self.assertIn("availability.check_requested", self.audit_actions())
        row = self.kernel.conn.execute(
            "SELECT entity_id FROM audit_events WHERE action = ?",
            ("availability.check_requested",),
        ).fetchone()
        self.assertEqual(row["entity_id"], outcome.check_id)

    # -- working the request ----------------------------------------------- #

    def test_begin_moves_the_check_into_checking(self):
        outcome = self.request()
        moved = self.kernel.begin_check(
            outcome.check_id, actor="user:anas", actor_role="owner"
        )
        self.assertEqual(moved.state, "CHECKING")
        self.assertEqual([row["state"] for row in self.events(outcome.check_id)],
                         ["CHECK_REQUESTED", "CHECKING"])

    def test_begin_twice_is_refused(self):
        outcome = self.request()
        self.kernel.begin_check(outcome.check_id, actor="user:anas", actor_role="owner")
        with self.assertRaises(DomainError) as exc:
            self.kernel.begin_check(outcome.check_id, actor="user:anas", actor_role="owner")
        self.assertEqual(exc.exception.code, "CHECK_INVALID_TRANSITION")

    def test_begin_on_an_answered_check_is_refused(self):
        outcome = self.request()
        evidence_id = self.evidence()
        self.kernel.answer_check(
            outcome.check_id, evidence_id=evidence_id, actor="user:anas", actor_role="owner"
        )
        with self.assertRaises(DomainError) as exc:
            self.kernel.begin_check(outcome.check_id, actor="user:anas", actor_role="owner")
        self.assertEqual(exc.exception.code, "CHECK_INVALID_TRANSITION")

    def test_an_unknown_check_is_refused(self):
        with self.assertRaises(DomainError) as exc:
            self.kernel.begin_check("chk_missing", actor="user:anas", actor_role="owner")
        self.assertEqual(exc.exception.code, "CHECK_NOT_FOUND")

    # -- answering --------------------------------------------------------- #

    def test_answering_closes_the_request(self):
        outcome = self.request()
        evidence_id = self.evidence()
        answered = self.kernel.answer_check(
            outcome.check_id, evidence_id=evidence_id, actor="user:anas", actor_role="owner"
        )
        self.assertEqual(answered.state, "ANSWERED")
        self.assertEqual(answered.evidence_id, evidence_id)
        rows = self.events(outcome.check_id)
        self.assertEqual(rows[-1]["state"], "ANSWERED")
        self.assertEqual(rows[-1]["evidence_id"], evidence_id)

    def test_answering_straight_from_check_requested_is_allowed(self):
        outcome = self.request()
        answered = self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        self.assertEqual(answered.state, "ANSWERED")

    def test_the_reported_state_after_an_answer_is_the_answers(self):
        """The request is over; what a caller needs now is the availability state."""
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        state = self.service.check_state(outcome.check_id)
        self.assertEqual(state.state, "AVAILABLE")

    def test_a_negative_answer_reports_not_available(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id,
            evidence_id=self.evidence(result="NOT_AVAILABLE"),
            actor="user:anas",
            actor_role="owner",
        )
        self.assertEqual(self.service.check_state(outcome.check_id).state, "NOT_AVAILABLE")

    def test_a_restricted_answer_keeps_its_restriction(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id,
            evidence_id=self.evidence(
                result="AVAILABLE_BUT_RESTRICTED", restrictions={"min_stay_nights": 5}
            ),
            actor="user:anas",
            actor_role="owner",
        )
        state = self.service.check_state(outcome.check_id)
        self.assertEqual(state.state, "AVAILABLE_BUT_RESTRICTED")
        self.assertEqual(state.restrictions["min_stay_nights"], 5)

    def test_answering_without_evidence_is_refused(self):
        outcome = self.request()
        with self.assertRaises(DomainError) as exc:
            self.kernel.answer_check(
                outcome.check_id, evidence_id=None, actor="user:anas", actor_role="owner"
            )
        self.assertEqual(exc.exception.code, "INVALID_FIELD")

    def test_answering_with_unknown_evidence_is_refused(self):
        outcome = self.request()
        with self.assertRaises(DomainError) as exc:
            self.kernel.answer_check(
                outcome.check_id, evidence_id="evid_missing", actor="user:anas", actor_role="owner"
            )
        self.assertEqual(exc.exception.code, "EVIDENCE_NOT_FOUND")

    def test_answering_with_another_units_evidence_is_refused(self):
        """Evidence about a different villa is not an answer about this one."""
        self.kernel.upsert_unit(
            unit_id="unit_2", property_id="prop_1", name="Villa unit_2", base_capacity=2, max_guests=4
        )
        outcome = self.request()
        with self.assertRaises(DomainError) as exc:
            self.kernel.answer_check(
                outcome.check_id,
                evidence_id=self.evidence(unit_id="unit_2"),
                actor="user:anas",
                actor_role="owner",
            )
        self.assertEqual(exc.exception.code, "EVIDENCE_UNIT_MISMATCH")

    def test_answering_twice_is_refused(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        with self.assertRaises(DomainError) as exc:
            self.kernel.answer_check(
                outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
            )
        self.assertEqual(exc.exception.code, "CHECK_ALREADY_ANSWERED")

    def test_stale_evidence_is_accepted_at_write_time_and_reported_stale_at_read_time(self):
        """ADR-0019 checks freshness before the result, not before the record.

        Refusing the write would lose the fact that an answer arrived; reporting
        it as fresh would be a lie. Recording it and reporting
        RECONFIRMATION_REQUIRED keeps both facts.
        """
        outcome = self.request()
        answered = self.kernel.answer_check(
            outcome.check_id,
            evidence_id=self.evidence(captured_at=STALE),
            actor="user:anas",
            actor_role="owner",
        )
        self.assertEqual(answered.state, "ANSWERED")
        self.assertEqual(self.service.check_state(outcome.check_id).state, "RECONFIRMATION_REQUIRED")

    def test_an_expired_answer_reports_reconfirmation_required(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id,
            evidence_id=self.kernel.record_evidence(
                unit_id="unit_1",
                source_type="property_api",
                evidence_level="A",
                result="AVAILABLE",
                captured_at="2026-09-19T00:00:00+00:00",
                valid_until="2026-09-25T00:00:00+00:00",
            ),
            actor="user:anas",
            actor_role="owner",
        )
        self.assertEqual(self.service.check_state(outcome.check_id).state, "RECONFIRMATION_REQUIRED")

    def test_answering_is_audited(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        self.assertIn("availability.check_answered", self.audit_actions())

    # -- the log ----------------------------------------------------------- #

    def test_the_sequence_advances_by_one(self):
        outcome = self.request()
        self.kernel.begin_check(outcome.check_id, actor="user:anas", actor_role="owner")
        self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        self.assertEqual([row["seq"] for row in self.events(outcome.check_id)], [1, 2, 3])

    def test_the_log_cannot_be_edited(self):
        outcome = self.request()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "UPDATE availability_check_events SET state = 'CHECKING' WHERE check_id = ?",
                (outcome.check_id,),
            )

    def test_the_log_cannot_be_deleted(self):
        outcome = self.request()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "DELETE FROM availability_check_events WHERE check_id = ?", (outcome.check_id,)
            )

    def test_the_request_cannot_be_edited(self):
        outcome = self.request()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "UPDATE availability_checks SET check_in = '2026-11-01' WHERE check_id = ?",
                (outcome.check_id,),
            )

    def test_another_agency_cannot_see_the_check(self):
        outcome = self.request()
        other = AV.AvailabilityService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=OTHER_AGENCY
        )
        with self.assertRaises(DomainError) as exc:
            other.check_state(outcome.check_id)
        self.assertEqual(exc.exception.code, "CHECK_NOT_FOUND")

    def test_an_unknown_check_has_no_state(self):
        with self.assertRaises(DomainError) as exc:
            self.service.check_state("chk_missing")
        self.assertEqual(exc.exception.code, "CHECK_NOT_FOUND")


# -- 4. the subject-level state ----------------------------------------------- #


class SubjectStateTestCase(unittest.TestCase):
    """``state_for`` answers "is this villa free?" — request states included."""

    def setUp(self):
        self.kernel = build_kernel()
        self.addCleanup(self.kernel.close)
        self.service = AV.AvailabilityService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )

    def request(self, *, check_in=CHECK_IN, check_out=CHECK_OUT, key="idem_check_0001"):
        return self.kernel.request_availability_check(
            unit_id="unit_1",
            check_in=check_in,
            check_out=check_out,
            actor="user:anas",
            actor_role="owner",
            idempotency_key=key,
        )

    def evidence(self, *, result="AVAILABLE", captured_at=None):
        return self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="property_api",
            evidence_level="A",
            result=result,
            captured_at=captured_at,
        )

    def test_nothing_known_is_unknown(self):
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "UNKNOWN")

    def test_an_open_request_is_the_state(self):
        self.request()
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "CHECK_REQUESTED")

    def test_a_started_request_is_the_state(self):
        outcome = self.request()
        self.kernel.begin_check(outcome.check_id, actor="user:anas", actor_role="owner")
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "CHECKING")

    def test_a_different_window_is_a_different_question(self):
        """A check for October says nothing about November."""
        self.request()
        state = self.service.state_for("unit_1", check_in="2026-11-01", check_out="2026-11-03")
        self.assertEqual(state.state, "UNKNOWN")

    def test_an_answer_outranks_the_request_that_produced_it(self):
        outcome = self.request()
        self.kernel.answer_check(
            outcome.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "AVAILABLE")

    def test_a_fresh_answer_is_not_masked_by_a_later_request(self):
        """A re-check does not un-answer the last answer: an answer is an answer."""
        first = self.request(key="idem_check_0001")
        self.kernel.answer_check(
            first.check_id, evidence_id=self.evidence(), actor="user:anas", actor_role="owner"
        )
        self.request(key="idem_check_0002")
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "AVAILABLE")

    def test_an_in_flight_request_reports_itself_when_the_answer_went_stale(self):
        """Stale evidence plus a live re-check is "we are asking again", which is
        more useful than a bare RECONFIRMATION_REQUIRED — and neither authorises."""
        first = self.request(key="idem_check_0001")
        self.kernel.answer_check(
            first.check_id,
            evidence_id=self.evidence(captured_at=STALE),
            actor="user:anas",
            actor_role="owner",
        )
        self.request(key="idem_check_0002")
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "CHECK_REQUESTED")

    def test_stale_evidence_without_a_recheck_asks_for_reconfirmation(self):
        first = self.request()
        self.kernel.answer_check(
            first.check_id,
            evidence_id=self.evidence(captured_at=STALE),
            actor="user:anas",
            actor_role="owner",
        )
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "RECONFIRMATION_REQUIRED")

    def test_the_newest_answer_is_the_one_reported(self):
        first = self.request(key="idem_check_0001")
        self.kernel.answer_check(
            first.check_id,
            evidence_id=self.evidence(result="NOT_AVAILABLE"),
            actor="user:anas",
            actor_role="owner",
        )
        self.kernel.record_evidence(
            unit_id="unit_1",
            source_type="property_api",
            evidence_level="A",
            result="AVAILABLE",
            captured_at="2026-09-26T01:00:00+00:00",
        )
        state = self.service.state_for("unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
        self.assertEqual(state.state, "AVAILABLE")


# -- 5. §56 authority --------------------------------------------------------- #


class MachineAuthorityTestCase(unittest.TestCase):
    """§56: availability requests are MEDIUM, so the policy decides — not the code."""

    def setUp(self):
        self.kernel = build_kernel()
        self.addCleanup(self.kernel.close)
        self.service = self._service()
        self.principal = self.service.register_principal(
            "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas", at=NOW
        )

    def _service(self, *, at=NOW):
        return AiService(
            self.kernel.conn,
            policy=PolicyResolver(self.kernel.conn, clock=FrozenClock.at(at), agency_id=AGENCY),
            clock=FrozenClock.at(at),
            agency_id=AGENCY,
        )

    def request(self, *, actor="ai:villa-concierge", actor_role="ai_service", principal_id=None):
        return self.kernel.request_availability_check(
            unit_id="unit_1",
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            actor=actor,
            actor_role=actor_role,
            idempotency_key="idem_check_0001",
            principal_id=principal_id,
        )

    def refusal_audits(self, conn=None):
        return [
            row["action"]
            for row in (conn or self.kernel.conn).execute(
                "SELECT action FROM audit_events WHERE action LIKE 'AI_TOOL%' ORDER BY seq"
            )
        ]

    def publish_allowance(self, tools, *, effective_from=LATER, conn=None):
        PolicyResolver(
            conn or self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        ).publish(
            ALLOWANCE_KEY,
            tools,
            effective_from=effective_from,
            effective_to="9999-12-31",
            source_doc_ref="§56 medium-risk policy-controlled tools",
            approved_by="owner_anas",
        )

    def test_a_human_may_request_a_check(self):
        outcome = self.request(actor="user:anas", actor_role="owner")
        self.assertEqual(outcome.state, "CHECK_REQUESTED")

    def test_a_machine_without_a_principal_is_refused(self):
        """Naming an AI is not identifying one: authority attaches to a registered
        principal, not to a string an actor chose for itself."""
        with self.assertRaises(DomainError) as exc:
            self.request()
        self.assertEqual(exc.exception.code, "MACHINE_PRINCIPAL_REQUIRED")

    def test_an_unknown_principal_is_refused(self):
        with self.assertRaises(UnknownAiPrincipal):
            self.request(principal_id="aip_missing")

    def test_a_machine_is_refused_while_the_allowance_is_unpublished(self):
        """This is the §56 default: the tool exists, the policy does not, so the
        answer is no. Nothing in the flow may treat "unpublished" as "allowed"."""
        with self.assertRaises(AiMediumToolNotAllowed) as exc:
            self.request(principal_id=self.principal.principal_id)
        self.assertEqual(exc.exception.code, "AI_MEDIUM_TOOL_NOT_ALLOWED")
        self.assertEqual(exc.exception.details["tool"], RK.AVAILABILITY_REQUEST_TOOL)

    def test_a_refusal_leaves_a_row_behind(self):
        with self.assertRaises(AiMediumToolNotAllowed):
            self.request(principal_id=self.principal.principal_id)
        self.assertIn("AI_TOOL_REFUSED", self.refusal_audits())
        self.assertEqual(
            self.kernel.conn.execute("SELECT COUNT(*) AS n FROM availability_checks").fetchone()["n"],
            0,
        )

    def test_the_owner_opens_the_flow_by_publishing_the_policy(self):
        """No code change: publish the allowance and the same call is admitted."""
        later = build_kernel(at=AFTER_LATER)
        self.addCleanup(later.close)
        self.publish_allowance([RK.AVAILABILITY_REQUEST_TOOL], conn=later.conn)
        later_service = AiService(
            later.conn,
            policy=PolicyResolver(later.conn, clock=FrozenClock.at(AFTER_LATER), agency_id=AGENCY),
            clock=FrozenClock.at(AFTER_LATER),
            agency_id=AGENCY,
        )
        principal = later_service.register_principal(
            "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas",
            at=AFTER_LATER,
        )
        outcome = later.request_availability_check(
            unit_id="unit_1",
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            actor="ai:villa-concierge",
            actor_role="ai_service",
            idempotency_key="idem_check_0001",
            principal_id=principal.principal_id,
        )
        self.assertEqual(outcome.state, "CHECK_REQUESTED")
        self.assertIn("AI_TOOL_ALLOWED", self.refusal_audits(later.conn))

    def test_an_allowance_that_is_not_yet_effective_does_not_open_the_flow(self):
        """Policy is versioned with a window; a future revision governs the future."""
        self.publish_allowance([RK.AVAILABILITY_REQUEST_TOOL])
        with self.assertRaises(AiMediumToolNotAllowed):
            self.request(principal_id=self.principal.principal_id)

    def test_an_allowance_for_another_tool_does_not_open_this_one(self):
        later = build_kernel(at=AFTER_LATER)
        self.addCleanup(later.close)
        self.publish_allowance(["hold.request_tentative"], conn=later.conn)
        later_service = AiService(
            later.conn,
            policy=PolicyResolver(later.conn, clock=FrozenClock.at(AFTER_LATER), agency_id=AGENCY),
            clock=FrozenClock.at(AFTER_LATER),
            agency_id=AGENCY,
        )
        principal = later_service.register_principal(
            "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas",
            at=AFTER_LATER,
        )
        with self.assertRaises(AiMediumToolNotAllowed):
            later.request_availability_check(
                unit_id="unit_1",
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
                actor="ai:villa-concierge",
                actor_role="ai_service",
                idempotency_key="idem_check_0001",
                principal_id=principal.principal_id,
            )

    def test_the_deny_registry_outranks_the_allowance(self):
        """§56's deny registry is checked first, so listing a denied tool in the
        allowance does not resurrect it."""
        later = build_kernel(at=AFTER_LATER)
        self.addCleanup(later.close)
        self.publish_allowance([RK.AVAILABILITY_REQUEST_TOOL], conn=later.conn)
        PolicyResolver(later.conn, clock=FrozenClock.at(AFTER_LATER), agency_id=AGENCY).publish(
            "security.ai.denied_actions",
            ["availability.request"],
            effective_from=LATER,
            effective_to="9999-12-31",
            source_doc_ref="§18 explicit deny",
            approved_by="owner_anas",
        )
        later_service = AiService(
            later.conn,
            policy=PolicyResolver(later.conn, clock=FrozenClock.at(AFTER_LATER), agency_id=AGENCY),
            clock=FrozenClock.at(AFTER_LATER),
            agency_id=AGENCY,
        )
        principal = later_service.register_principal(
            "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas",
            at=AFTER_LATER,
        )
        with self.assertRaises(DomainError) as exc:
            later.request_availability_check(
                unit_id="unit_1",
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
                actor="ai:villa-concierge",
                actor_role="ai_service",
                idempotency_key="idem_check_0001",
                principal_id=principal.principal_id,
            )
        self.assertEqual(exc.exception.code, "AI_DENIED_ACTION")

    def test_a_suspended_principal_is_refused(self):
        later = build_kernel(at=AFTER_LATER)
        self.addCleanup(later.close)
        self.publish_allowance([RK.AVAILABILITY_REQUEST_TOOL], conn=later.conn)
        later_service = AiService(
            later.conn,
            policy=PolicyResolver(later.conn, clock=FrozenClock.at(AFTER_LATER), agency_id=AGENCY),
            clock=FrozenClock.at(AFTER_LATER),
            agency_id=AGENCY,
        )
        principal = later_service.register_principal(
            "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas",
            at=AFTER_LATER,
        )
        later_service.suspend(
            principal.principal_id, actor="owner_anas", reason="drill", at=AFTER_LATER
        )
        with self.assertRaises(DomainError) as exc:
            later.request_availability_check(
                unit_id="unit_1",
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
                actor="ai:villa-concierge",
                actor_role="ai_service",
                idempotency_key="idem_check_0001",
                principal_id=principal.principal_id,
            )
        self.assertEqual(exc.exception.code, "AI_PRINCIPAL_NOT_ACTIVE")


class ReachabilityTestCase(unittest.TestCase):
    """The claim ADR-0021 makes, checked in one place.

    WAVE 4-A-2 exists because two Appendix A states were in the vocabulary and nowhere in
    the system. A wave that adds a flow can add a different unreachable state just as
    easily, so this walks all seven and fails if any of them cannot be produced.
    """

    def setUp(self):
        self.kernel = build_kernel()
        self.addCleanup(self.kernel.close)
        for index in (2, 3, 4):
            self.kernel.upsert_unit(
                unit_id=f"unit_{index}",
                property_id="prop_1",
                name=f"Villa unit_{index}",
                base_capacity=2,
                max_guests=6,
            )
        self.service = AV.AvailabilityService(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )

    def request(self, unit_id, *, key):
        return self.kernel.request_availability_check(
            unit_id=unit_id,
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            actor="user:anas",
            actor_role="owner",
            idempotency_key=key,
        )

    def answer(self, outcome, *, result, unit_id, **evidence):
        return self.kernel.answer_check(
            outcome.check_id,
            evidence_id=self.kernel.record_evidence(
                unit_id=unit_id,
                source_type="property_api",
                evidence_level="A",
                result=result,
                **evidence,
            ),
            actor="user:anas",
            actor_role="owner",
        )

    def state(self, unit_id):
        return self.service.state_for(unit_id, check_in=CHECK_IN, check_out=CHECK_OUT).state

    def test_every_appendix_a_state_is_reachable(self):
        reachable = set()

        reachable.add(self.state("unit_1"))  # UNKNOWN: nothing known, nothing asked

        asked = self.request("unit_1", key="idem_reach_0001")
        reachable.add(self.state("unit_1"))  # CHECK_REQUESTED: asked, not started

        self.kernel.begin_check(asked.check_id, actor="agent_maya", actor_role="agent")
        reachable.add(self.state("unit_1"))  # CHECKING: with the source

        self.answer(asked, result="AVAILABLE", unit_id="unit_1")
        reachable.add(self.state("unit_1"))  # AVAILABLE: answered yes

        no = self.request("unit_2", key="idem_reach_0002")
        self.answer(no, result="NOT_AVAILABLE", unit_id="unit_2")
        reachable.add(self.state("unit_2"))  # NOT_AVAILABLE: answered no

        restricted = self.request("unit_3", key="idem_reach_0003")
        self.answer(
            restricted,
            result="AVAILABLE_BUT_RESTRICTED",
            unit_id="unit_3",
            restrictions={"min_stay_nights": 5},
        )
        reachable.add(self.state("unit_3"))  # AVAILABLE_BUT_RESTRICTED: yes, with a condition

        aged = self.request("unit_4", key="idem_reach_0004")
        self.answer(
            aged,
            result="AVAILABLE",
            unit_id="unit_4",
            captured_at="2026-09-19T00:00:00+00:00",
            valid_until="2026-09-25T00:00:00+00:00",
        )
        reachable.add(self.state("unit_4"))  # RECONFIRMATION_REQUIRED: the answer aged

        self.assertEqual(reachable, set(AV.AVAILABILITY_STATES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

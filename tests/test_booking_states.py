"""Tests for ``src/booking_states.py`` — the canonical booking state machine.

The central claim of the module is provenance: the 32 states are **read out of**
the ``bookings.status`` CHECK enum in ``src/schema/001_initial.sql``, not typed
in from memory. So the first thing tested here is exactly that — three
independent derivations of the same list (the literal DDL text parsed by regex,
the schema loader's parsed enum, and the module's own constant) must agree, and
any drift must fail. Everything else (graph shape, terminal states, guards,
journal rows, error codes, no floats) is tested against that derived set.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import booking_states as B
import primitives as P
from primitives import DomainError
from schema_loader import connect_sqlite

DDL_PATH = Path(__file__).resolve().parents[1] / "src" / "schema" / "001_initial.sql"

#: The 32 states **verbatim and in declaration order** from the
#: ``status TEXT NOT NULL CHECK (status IN (...))`` clause of ``CREATE TABLE
#: bookings`` (``001_initial.sql`` lines 258-266). Written out here on purpose:
#: if someone edits the DDL or the module, this fails.
DDL_STATES: tuple[str, ...] = (
    "INQUIRY",
    "QUALIFIED",
    "AVAILABILITY_PENDING",
    "AVAILABILITY_CONFIRMED",
    "AVAILABILITY_UNAVAILABLE",
    "QUOTE_DRAFTED",
    "QUOTE_SENT",
    "QUOTE_EXPIRED",
    "HOLD_REQUESTED",
    "HOLD_ACTIVE",
    "HOLD_EXPIRED",
    "PAYMENT_PENDING",
    "PAYMENT_PARTIAL",
    "PAYMENT_UNMATCHED",
    "PAYMENT_FAILED",
    "DEPOSIT_PAID",
    "PAID_IN_FULL",
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    "COMPLETED",
    "CANCELLATION_REQUESTED",
    "CANCELLATION_REVIEW",
    "CANCELLED",
    "REFUND_PENDING",
    "REFUNDED",
    "NO_SHOW",
    "RELOCATED",
    "DISPUTED",
    "CLOSED",
)

#: ``uq_bookings_active_slot`` ... WHERE status IN (...) — the occupying states.
DDL_OCCUPYING: tuple[str, ...] = (
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    # ADR-0002 (Opsi A) — kept in DDL order so a drift shows up as a diff.
    "CANCELLATION_REQUESTED",
    "CANCELLATION_REVIEW",
    "DISPUTED",
)

#: Columns of ``booking_state_events``, in DDL order — the journal row shape.
DDL_JOURNAL_COLUMNS: tuple[str, ...] = (
    "state_event_id",
    "booking_id",
    "seq",
    "from_state",
    "to_state",
    "command",
    "actor",
    "actor_role",
    "command_id",
    "evidence_ref",
    "approval_ref",
    "reason",
    "occurred_at",
)

SCREAMING_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")

REQUIRED_REQUEST_FIELDS = (
    "booking_id",
    "from_state",
    "to_state",
    "command",
    "actor",
    "actor_role",
    "command_id",
    "occurred_at",
)

ALL_GUARD_FIELDS = {
    "reason": "reason recorded for the transition",
    "evidence_ref": "evt_0001",
    "approval_ref": "apr_0001",
}


def _table_body(sql: str, table: str) -> str:
    """Raw DDL body of ``CREATE TABLE <table> (...)`` straight from the file."""
    match = re.search(
        r"CREATE TABLE %s \((.*?)\n\);" % re.escape(table),
        sql,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"CREATE TABLE {table} not found in {DDL_PATH}")
    return match.group(1)


def _status_check_literals() -> tuple[str, ...]:
    """Extract the bookings.status enum by regex — an independent DDL reader."""
    body = _table_body(DDL_PATH.read_text(encoding="utf-8"), "bookings")
    match = re.search(r"status\s+TEXT\s+NOT NULL CHECK \(status IN \((.*?)\)\)", body, re.DOTALL)
    if match is None:
        raise AssertionError("bookings.status CHECK (status IN (...)) not found")
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _occupying_literals() -> tuple[str, ...]:
    sql = DDL_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE UNIQUE INDEX uq_bookings_active_slot .*?WHERE status IN \((.*?)\);",
        sql,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("uq_bookings_active_slot predicate not found")
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def request_fields(**overrides):
    """A complete, valid ``TransitionRequest`` payload (all fields filled)."""
    base = {
        "booking_id": "bk_000001",
        "from_state": "INQUIRY",
        "to_state": "QUALIFIED",
        "command": "qualify_booking",
        "actor": "agent:maria",
        "actor_role": "sales_agent",
        "command_id": "cmd_00000001",
        "occurred_at": "2026-09-21T08:00:00+00:00",
    }
    base.update(overrides)
    return base


def request(**overrides) -> B.TransitionRequest:
    """A built request; extra kwargs are merged over the valid template."""
    return B.TransitionRequest.from_mapping(request_fields(**overrides))


def guarded_action(from_state: str, to_state: str, **overrides) -> B.TransitionOutcome:
    """Apply a transition with every guard field present, so only the graph decides."""
    fields = request_fields(from_state=from_state, to_state=to_state)
    fields.update(ALL_GUARD_FIELDS)
    fields.update(overrides)
    return B.apply_transition(B.TransitionRequest.from_mapping(fields))


def assert_screaming_snake(test: unittest.TestCase, code: object) -> None:
    test.assertIsInstance(code, str)
    test.assertRegex(code, SCREAMING_SNAKE)


def iter_scalars(value):
    """Yield every non-container value inside nested dict/list/tuple structures."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_scalars(key)
            yield from iter_scalars(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from iter_scalars(item)
    else:
        yield value


# --------------------------------------------------------------------------- #
# 1. Provenance: the state set IS the schema enum
# --------------------------------------------------------------------------- #
class TestStatesAreDerivedFromSchema(unittest.TestCase):
    """A state list that drifts from the DDL is a data-corruption bug waiting."""

    def test_declaration_order_matches_the_ddl_lines(self) -> None:
        self.assertEqual(B.STATES_IN_DECLARATION_ORDER, DDL_STATES)

    def test_state_set_equals_the_raw_ddl_literals(self) -> None:
        self.assertEqual(_status_check_literals(), DDL_STATES)
        self.assertEqual(set(B.BOOKING_STATES), set(_status_check_literals()))

    def test_state_set_equals_the_parsed_schema_enum(self) -> None:
        for dialect in ("sqlite", "pg"):
            with self.subTest(dialect=dialect):
                self.assertEqual(B.states_from_schema(dialect), tuple(sorted(DDL_STATES)))

    def test_state_count_is_thirty_two(self) -> None:
        self.assertEqual(B.STATE_COUNT, 32)
        self.assertEqual(len(B.BOOKING_STATES), 32)
        self.assertEqual(len(DDL_STATES), 32)

    def test_states_are_unique_and_screaming_snake(self) -> None:
        self.assertEqual(len(set(DDL_STATES)), len(DDL_STATES))
        for state in DDL_STATES:
            with self.subTest(state=state):
                self.assertRegex(state, SCREAMING_SNAKE)

    def test_occupying_states_match_the_partial_index_predicate(self) -> None:
        self.assertEqual(B.occupying_states_from_schema(), tuple(sorted(DDL_OCCUPYING)))
        self.assertEqual(set(B.OCCUPYING_STATES), set(_occupying_literals()))
        self.assertTrue(set(B.OCCUPYING_STATES).issubset(B.BOOKING_STATES))
        self.assertEqual(len(B.OCCUPYING_STATES), 8)

    def test_load_time_self_check_passes_for_both_dialects(self) -> None:
        for dialect in ("sqlite", "pg"):
            with self.subTest(dialect=dialect):
                self.assertIsNone(B.verify_against_schema(dialect))
        self.assertIsNone(B.check_state_enum(B.BOOKING_STATES))
        self.assertIsNone(B.check_state_enum(DDL_STATES))

    def test_check_state_enum_accepts_lowercase_spellings(self) -> None:
        self.assertIsNone(B.check_state_enum(s.lower() for s in DDL_STATES))

    def test_check_state_enum_detects_a_missing_state(self) -> None:
        with self.assertRaises(DomainError) as caught:
            B.check_state_enum(set(DDL_STATES) - {"CLOSED"})
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_STATE_ENUM_MISMATCH)
        self.assertEqual(caught.exception.details.get("missing"), ("CLOSED",))

    def test_check_state_enum_detects_an_invented_state(self) -> None:
        with self.assertRaises(DomainError) as caught:
            B.check_state_enum(set(DDL_STATES) | {"BOOKED"})
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_STATE_ENUM_MISMATCH)
        self.assertEqual(caught.exception.details.get("unexpected"), ("BOOKED",))

    def test_check_state_enum_detects_a_duplicate(self) -> None:
        with self.assertRaises(DomainError) as caught:
            B.check_state_enum(list(DDL_STATES) + ["CLOSED"])
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_STATE_ENUM_MISMATCH)
        self.assertEqual(caught.exception.details.get("duplicates"), ("CLOSED",))

    def test_missing_enum_in_the_schema_is_reported(self) -> None:
        empty_model = SimpleNamespace(enums={}, indexes={})
        with mock.patch.object(B, "parse_schema_model", return_value=empty_model):
            with self.assertRaises(DomainError) as caught:
                B.states_from_schema()
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_ENUM_UNAVAILABLE)

    def test_unreadable_schema_is_reported(self) -> None:
        with mock.patch.object(B, "parse_schema_model", side_effect=OSError("no schema")):
            with self.assertRaises(DomainError) as caught:
                B.states_from_schema()
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_ENUM_UNAVAILABLE)

    def test_missing_occupying_index_is_reported(self) -> None:
        model = SimpleNamespace(
            enums={"bookings.status": tuple(s.lower() for s in DDL_STATES)},
            indexes={},
        )
        with mock.patch.object(B, "parse_schema_model", return_value=model):
            with self.assertRaises(DomainError) as caught:
                B.occupying_states_from_schema()
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_ENUM_UNAVAILABLE)

    def test_index_predicate_drift_is_detected(self) -> None:
        """A predicate that names a non-enum status and forgets four real ones.

        ``unknown`` = predicate literals absent from ``bookings.status``;
        ``missing`` = occupying states this module declares that the predicate
        omits. Both are drift, both are reported as tuples.
        """
        model = SimpleNamespace(
            enums={"bookings.status": tuple(s.lower() for s in DDL_STATES)},
            indexes={
                "uq_bookings_active_slot": SimpleNamespace(
                    predicate="status in ('confirmed','booked')"
                )
            },
        )
        with mock.patch.object(B, "parse_schema_model", return_value=model):
            with self.assertRaises(DomainError) as caught:
                B.occupying_states_from_schema()
        self.assertEqual(caught.exception.code, B.CODE_SCHEMA_STATE_ENUM_MISMATCH)
        self.assertEqual(caught.exception.details.get("unknown"), ("BOOKED",))
        self.assertEqual(
            caught.exception.details.get("missing"),
            (
                "BALANCE_DUE",
                "BALANCE_OVERDUE",
                "CANCELLATION_REQUESTED",
                "CANCELLATION_REVIEW",
                "DISPUTED",
                "IN_STAY",
                "PRE_ARRIVAL",
            ),
        )


# --------------------------------------------------------------------------- #
# 2. Graph shape and terminal states
# --------------------------------------------------------------------------- #
class TestTransitionGraph(unittest.TestCase):
    """Every state must be wired, and nothing may loop back on itself."""

    def test_transitions_cover_exactly_the_state_set(self) -> None:
        self.assertEqual(set(B.TRANSITIONS), set(DDL_STATES))
        for state, targets in B.TRANSITIONS.items():
            with self.subTest(state=state):
                self.assertIsInstance(targets, frozenset)
                self.assertTrue(targets.issubset(B.BOOKING_STATES))

    def test_no_self_transitions_exist_in_the_graph(self) -> None:
        for source, target in B.all_transitions():
            with self.subTest(edge=(source, target)):
                self.assertNotEqual(source, target)

    def test_all_transitions_is_sorted_and_deduplicated(self) -> None:
        edges = B.all_transitions()
        self.assertIsInstance(edges, tuple)
        self.assertEqual(edges, tuple(sorted(set(edges))))
        self.assertEqual(len(edges), sum(len(t) for t in B.TRANSITIONS.values()))
        self.assertGreater(len(edges), 0)

    def test_closed_is_the_only_terminal_state(self) -> None:
        self.assertEqual(set(B.TERMINAL_STATES), {"CLOSED"})
        self.assertEqual(set(B.sink_states()), set(B.TERMINAL_STATES))
        self.assertEqual(B.TRANSITIONS["CLOSED"], frozenset())
        self.assertTrue(B.is_terminal("CLOSED"))
        self.assertTrue(B.is_terminal(" closed "))
        self.assertFalse(B.is_terminal("IN_STAY"))

    def test_non_terminal_states_have_a_way_out(self) -> None:
        for state in sorted(B.BOOKING_STATES - B.TERMINAL_STATES):
            with self.subTest(state=state):
                self.assertGreater(len(B.allowed_next_states(state)), 0)
                self.assertTrue(B.reaches_terminal(state))

    def test_every_state_is_reachable_from_inquiry(self) -> None:
        reached = set(B.reachable_from("INQUIRY"))
        self.assertEqual(reached, set(B.BOOKING_STATES) - {"INQUIRY"})
        self.assertNotIn("INQUIRY", reached)

    def test_reachable_from_is_sorted_and_terminal_aware(self) -> None:
        reached = B.reachable_from("PRE_ARRIVAL")
        self.assertIsInstance(reached, tuple)
        self.assertEqual(reached, tuple(sorted(reached)))
        self.assertIn("CLOSED", reached)
        self.assertTrue(B.reaches_terminal("INQUIRY"))
        self.assertFalse(B.reaches_terminal("CLOSED") is False)
        self.assertTrue(B.reaches_terminal("CLOSED"))

    def test_can_transition_agrees_with_the_graph(self) -> None:
        for source in B.BOOKING_STATES:
            for target in B.BOOKING_STATES:
                expected = target in B.TRANSITIONS[source] and source not in B.TERMINAL_STATES
                with self.subTest(edge=(source, target)):
                    self.assertEqual(B.can_transition(source, target), expected)

    def test_allowed_next_states_normalises_and_validates(self) -> None:
        self.assertEqual(
            B.allowed_next_states(" inquiry "),
            B.allowed_next_states("INQUIRY"),
        )
        self.assertEqual(
            set(B.allowed_next_states("INQUIRY")),
            set(B.TRANSITIONS["INQUIRY"]),
        )
        for source, target in B.all_transitions():
            with self.subTest(edge=(source, target)):
                self.assertIn(target, B.allowed_next_states(source))

    def test_unknown_state_is_refused_everywhere(self) -> None:
        for call in (
            lambda: B.allowed_next_states("BOOKED"),
            lambda: B.reachable_from("BOOKED"),
            lambda: B.is_terminal("BOOKED"),
            lambda: B.is_occupying_state("BOOKED"),
            lambda: B.guards_for("BOOKED", "QUALIFIED"),
            lambda: B.guards_for("INQUIRY", "BOOKED"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(DomainError) as caught:
                    call()
                self.assertEqual(caught.exception.code, B.CODE_UNKNOWN_STATE)

    def test_is_known_state_is_total(self) -> None:
        self.assertTrue(B.is_known_state("closed"))
        self.assertFalse(B.is_known_state("BOOKED"))
        self.assertFalse(B.is_known_state(None))
        self.assertFalse(B.is_known_state(5))

    def test_lifecycle_path_is_wired_end_to_end(self) -> None:
        """The documented happy path: enquiry -> stay -> closed books."""
        path = (
            "INQUIRY",
            "QUALIFIED",
            "AVAILABILITY_CONFIRMED",
            "QUOTE_SENT",
            "HOLD_ACTIVE",
            "DEPOSIT_PAID",
            "CONFIRMED",
            "PRE_ARRIVAL",
            "IN_STAY",
            "COMPLETED",
            "CLOSED",
        )
        for source, target in zip(path, path[1:]):
            with self.subTest(edge=(source, target)):
                self.assertTrue(B.can_transition(source, target))
        self.assertFalse(B.can_transition("CLOSED", "INQUIRY"))

    def test_lifecycle_fast_paths_are_wired(self) -> None:
        """The short paths skip a bookkeeping state, never a guarded *fact*."""
        for source, target in (
            ("QUALIFIED", "AVAILABILITY_CONFIRMED"),
            ("AVAILABILITY_CONFIRMED", "QUOTE_SENT"),
            ("QUOTE_SENT", "HOLD_ACTIVE"),
            ("HOLD_ACTIVE", "DEPOSIT_PAID"),
            ("IN_STAY", "CANCELLATION_REQUESTED"),
        ):
            with self.subTest(edge=(source, target)):
                self.assertTrue(B.can_transition(source, target))

    def test_no_show_cannot_be_declared_during_a_stay(self) -> None:
        """NO_SHOW frees the slot, so it must never be reachable from IN_STAY.

        A guest who is physically inside the villa cannot be a no-show: recording
        one would both misstate occupancy and hand the dates back to the
        availability engine for re-sale (double booking). No-show is therefore
        decided strictly before arrival.
        """
        self.assertFalse(B.is_occupying_state("NO_SHOW"))
        self.assertFalse(B.can_transition("IN_STAY", "NO_SHOW"))
        fields = request_fields(from_state="IN_STAY", to_state="NO_SHOW")
        fields.update(ALL_GUARD_FIELDS)
        with self.assertRaises(DomainError) as caught:
            B.apply_transition(B.TransitionRequest.from_mapping(fields))
        self.assertEqual(caught.exception.code, B.CODE_INVALID_TRANSITION)
        for pre_arrival in ("CONFIRMED", "BALANCE_DUE", "BALANCE_OVERDUE", "PRE_ARRIVAL"):
            with self.subTest(source=pre_arrival):
                self.assertTrue(B.can_transition(pre_arrival, "NO_SHOW"))

    def test_cancellation_review_and_dispute_hold_the_slot(self) -> None:
        """ADR-0002 Opsi A (diputuskan pemilik sistem 2026-09-21).

        A cancellation under review and a dispute raised while the guest is still
        inside the villa are occupying: the unit may not be re-sold until the
        cancellation is *decided* (-> ``CANCELLED``) or the dispute is closed.
        Releasing the dates earlier hands them back to the availability engine
        while the guest may still arrive — that is a double booking.

        Option A widens the predicate only; it does not remove the operational
        moves (that was option B), so both transitions must stay legal.
        """
        for state in ("DISPUTED", "CANCELLATION_REQUESTED", "CANCELLATION_REVIEW"):
            with self.subTest(state=state):
                self.assertTrue(B.is_occupying_state(state))
        self.assertEqual(
            set(B.OCCUPYING_STATES),
            {
                "CONFIRMED",
                "BALANCE_DUE",
                "BALANCE_OVERDUE",
                "PRE_ARRIVAL",
                "IN_STAY",
                "CANCELLATION_REQUESTED",
                "CANCELLATION_REVIEW",
                "DISPUTED",
            },
        )
        self.assertTrue(B.can_transition("IN_STAY", "DISPUTED"))
        self.assertTrue(B.can_transition("IN_STAY", "CANCELLATION_REQUESTED"))
        self.assertTrue(B.can_transition("CONFIRMED", "CANCELLATION_REQUESTED"))
        # The decision that DOES release the slot must stay reachable, otherwise
        # fail-closed becomes fail-stuck.
        self.assertTrue(B.can_transition("CANCELLATION_REQUESTED", "CANCELLED"))
        self.assertTrue(B.can_transition("CANCELLATION_REVIEW", "CANCELLED"))
        for releasing in ("CANCELLED", "NO_SHOW", "RELOCATED", "COMPLETED", "CLOSED"):
            with self.subTest(releasing=releasing):
                self.assertFalse(B.is_occupying_state(releasing))


# --------------------------------------------------------------------------- #
# 3. Guard hooks
# --------------------------------------------------------------------------- #
class TestGuards(unittest.TestCase):
    """Guards are the reason a state machine beats a free-text status column."""

    EXPECTED_GUARDS = {
        ("INQUIRY", "QUALIFIED"): (),
        ("QUALIFIED", "AVAILABILITY_PENDING"): (),
        ("AVAILABILITY_PENDING", "AVAILABILITY_CONFIRMED"): ("evidence",),
        ("HOLD_ACTIVE", "DEPOSIT_PAID"): ("evidence",),
        ("BALANCE_DUE", "PAID_IN_FULL"): ("evidence",),
        ("IN_STAY", "COMPLETED"): (),
        ("IN_STAY", "CANCELLATION_REQUESTED"): ("reason",),
        ("PRE_ARRIVAL", "NO_SHOW"): ("evidence", "reason"),
        ("IN_STAY", "RELOCATED"): ("approval", "reason"),
        ("CANCELLATION_REVIEW", "CANCELLED"): ("approval", "reason"),
        ("CANCELLATION_REVIEW", "REFUND_PENDING"): ("approval", "reason"),
        ("REFUND_PENDING", "REFUNDED"): ("approval", "evidence"),
        ("COMPLETED", "DISPUTED"): ("reason",),
        ("CANCELLED", "CLOSED"): (),
    }

    def test_expected_guard_table(self) -> None:
        for edge, expected in self.EXPECTED_GUARDS.items():
            with self.subTest(edge=edge):
                self.assertEqual(B.guards_for(*edge), expected)

    def test_guards_are_sorted_tuples(self) -> None:
        for edge, names in B.TRANSITION_GUARDS.items():
            with self.subTest(edge=edge):
                self.assertIsInstance(names, tuple)
                self.assertEqual(names, tuple(sorted(names)))
        self.assertEqual(B.TRANSITION_GUARDS[("*", "REFUNDED")], ("approval", "evidence"))
        self.assertEqual(
            B.TRANSITION_GUARDS[("CANCELLATION_REVIEW", "CANCELLED")],
            ("approval", "reason"),
        )

    def test_every_guard_name_is_registered(self) -> None:
        self.assertEqual(set(B.GUARDS), {"reason", "evidence", "approval"})
        for edge, names in B.TRANSITION_GUARDS.items():
            with self.subTest(edge=edge):
                self.assertTrue(set(names).issubset(set(B.GUARDS)))

    def test_wildcard_guards_apply_to_concrete_sources(self) -> None:
        for (source, target), names in B.GUARD_TABLE.items():
            if source != "*":
                continue
            sources = [
                state
                for state in B.BOOKING_STATES
                if target in B.TRANSITIONS[state] and state not in B.TERMINAL_STATES
            ]
            with self.subTest(target=target):
                self.assertTrue(sources, f"wildcard guard target {target} is unreachable")
                for concrete in sources:
                    self.assertTrue(set(names).issubset(set(B.guards_for(concrete, target))))

    def test_guard_keys_name_known_states(self) -> None:
        for source, target in B.GUARD_TABLE:
            with self.subTest(edge=(source, target)):
                self.assertTrue(source == "*" or source in B.BOOKING_STATES)
                self.assertIn(target, B.BOOKING_STATES)

    def test_unguarded_edge_has_no_guards(self) -> None:
        self.assertEqual(B.guards_for("CANCELLED", "CLOSED"), ())

    def test_illegal_edge_reports_invalid_transition(self) -> None:
        with self.assertRaises(DomainError) as caught:
            B.guards_for("INQUIRY", "IN_STAY")
        self.assertEqual(caught.exception.code, B.CODE_INVALID_TRANSITION)

    def test_guard_normalises_state_case(self) -> None:
        self.assertEqual(
            B.guards_for("cancellation_review", "cancelled"),
            ("approval", "reason"),
        )

    def test_default_guard_hooks_reject_missing_fields(self) -> None:
        context = B.GuardContext(request=request(), seq=1, facts={})
        for hook, code in (
            (B.guard_require_reason, B.CODE_MISSING_REASON),
            (B.guard_require_evidence, B.CODE_MISSING_EVIDENCE),
            (B.guard_require_approval, B.CODE_MISSING_APPROVAL),
        ):
            with self.subTest(hook=hook.__name__):
                with self.assertRaises(DomainError) as caught:
                    hook(context)
                self.assertEqual(caught.exception.code, code)
                assert_screaming_snake(self, caught.exception.code)

    def test_default_guard_hooks_accept_present_fields(self) -> None:
        context = B.GuardContext(request=request(**ALL_GUARD_FIELDS), seq=1, facts={})
        for hook in (B.guard_require_reason, B.guard_require_evidence, B.guard_require_approval):
            with self.subTest(hook=hook.__name__):
                self.assertIsNone(hook(context))

    def test_guard_context_reads_facts(self) -> None:
        context = B.GuardContext(request=request(), seq=7, facts={"refund_minor": 0})
        self.assertEqual(context.fact("refund_minor"), 0)
        self.assertEqual(context.fact("missing", "fallback"), "fallback")
        self.assertEqual(context.seq, 7)


# --------------------------------------------------------------------------- #
# 4. TransitionRequest field validation
# --------------------------------------------------------------------------- #
class TestTransitionRequest(unittest.TestCase):
    """Field shape is checked at construction; graph legality later."""

    def test_valid_request_round_trips(self) -> None:
        fields = request_fields(**ALL_GUARD_FIELDS)
        built = B.TransitionRequest.from_mapping(fields)
        self.assertEqual(built.as_dict(), fields)
        self.assertEqual(B.TransitionRequest.from_mapping(built.as_dict()), built)

    def test_states_are_normalised(self) -> None:
        built = request(from_state="  inquiry ", to_state="qualified")
        self.assertEqual(built.from_state, "INQUIRY")
        self.assertEqual(built.to_state, "QUALIFIED")

    def test_unknown_extra_keys_are_ignored(self) -> None:
        built = B.TransitionRequest.from_mapping(request_fields(sneaky="value"))
        self.assertFalse(hasattr(built, "sneaky"))

    def test_frozen(self) -> None:
        built = request()
        with self.assertRaises(Exception):
            built.from_state = "QUALIFIED"  # type: ignore[misc]

    def test_missing_required_field(self) -> None:
        for name in REQUIRED_REQUEST_FIELDS:
            fields = request_fields()
            del fields[name]
            with self.subTest(field=name):
                with self.assertRaises(DomainError) as caught:
                    B.TransitionRequest.from_mapping(fields)
                self.assertEqual(caught.exception.code, B.CODE_MISSING_FIELD)
                self.assertEqual(caught.exception.details.get("field"), name)
                assert_screaming_snake(self, caught.exception.code)

    def test_none_required_field(self) -> None:
        for name in REQUIRED_REQUEST_FIELDS:
            with self.subTest(field=name):
                with self.assertRaises(DomainError) as caught:
                    B.TransitionRequest.from_mapping(request_fields(**{name: None}))
                self.assertEqual(caught.exception.code, B.CODE_MISSING_FIELD)

    def test_blank_text_fields(self) -> None:
        for name in ("command", "actor", "actor_role", "from_state", "to_state"):
            for blank in ("", "   ", "\t"):
                with self.subTest(field=name, blank=repr(blank)):
                    with self.assertRaises(DomainError) as caught:
                        request(**{name: blank})
                    self.assertEqual(caught.exception.code, B.CODE_MISSING_FIELD)

    def test_non_string_state_is_invalid(self) -> None:
        for value in (5, 3.5, ["INQUIRY"], {"state": "INQUIRY"}):
            with self.subTest(value=value):
                with self.assertRaises(DomainError) as caught:
                    request(from_state=value)
                self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)

    def test_booking_id_must_be_a_ref(self) -> None:
        for value in ("bad id", "bad/id\"", "-"):
            with self.subTest(value=value):
                with self.assertRaises(DomainError) as caught:
                    request(booking_id=value)
                assert_screaming_snake(self, caught.exception.code)

    def test_command_id_length_bounds(self) -> None:
        with self.assertRaises(DomainError) as caught:
            request(command_id="cmd_1")
        self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)
        self.assertEqual(caught.exception.details.get("field"), "command_id")
        with self.assertRaises(DomainError):
            request(command_id="cmd_" + "9" * 80)

    def test_command_id_boundary_is_inclusive(self) -> None:
        self.assertEqual(request(command_id="cmd_1234").command_id, "cmd_1234")
        self.assertEqual(len(request(command_id="c" * 64).command_id), 64)

    def test_optional_refs_reject_blank_and_malformed(self) -> None:
        with self.assertRaises(DomainError) as caught:
            request(evidence_ref="")
        self.assertEqual(caught.exception.code, B.CODE_MISSING_FIELD)
        with self.assertRaises(DomainError) as caught:
            request(approval_ref="apr 1")
        assert_screaming_snake(self, caught.exception.code)

    def test_reason_is_free_text_but_not_blank(self) -> None:
        self.assertEqual(request(reason="  guest asked to move  ").reason, "guest asked to move")
        with self.assertRaises(DomainError) as caught:
            request(reason="   ")
        self.assertEqual(caught.exception.code, B.CODE_MISSING_FIELD)
        with self.assertRaises(DomainError):
            request(reason="x" * 2001)

    def test_occurred_at_accepts_aware_datetime_and_strings(self) -> None:
        moment = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(request(occurred_at=moment).occurred_at, "2026-09-21T08:00:00+00:00")
        for text in ("2026-09-21T08:00:00Z", "2026-09-21T15:00:00+07:00"):
            with self.subTest(text=text):
                built = request(occurred_at=text)
                self.assertEqual(P.to_iso(P.parse_iso(built.occurred_at)), built.occurred_at)

    def test_occurred_at_rejects_naive_and_garbage(self) -> None:
        naive = datetime(2026, 9, 21, 8, 0)
        with self.assertRaises(DomainError) as caught:
            request(occurred_at=naive)
        assert_screaming_snake(self, caught.exception.code)
        for value in ("not-a-time", "", 12345, None):
            with self.subTest(value=value):
                with self.assertRaises(DomainError) as caught:
                    request(occurred_at=value)
                assert_screaming_snake(self, caught.exception.code)

    def test_from_mapping_requires_a_mapping(self) -> None:
        for value in ([], "INQUIRY", 5):
            with self.subTest(value=value):
                with self.assertRaises(DomainError) as caught:
                    B.TransitionRequest.from_mapping(value)
                self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)


# --------------------------------------------------------------------------- #
# 5. apply_transition — exhaustive over all 32 x 32 pairs
# --------------------------------------------------------------------------- #
class TestApplyTransitionExhaustive(unittest.TestCase):
    """32 x 32 = 1024 ordered pairs; each must land in exactly one bucket."""

    def test_every_pair_is_classified_exactly_once(self) -> None:
        legal = self_transition = terminal = illegal = 0
        for source in sorted(B.BOOKING_STATES):
            for target in sorted(B.BOOKING_STATES):
                fields = request_fields(from_state=source, to_state=target)
                fields.update(ALL_GUARD_FIELDS)
                built = B.TransitionRequest.from_mapping(fields)
                if source in B.TERMINAL_STATES:
                    with self.subTest(edge=(source, target)):
                        with self.assertRaises(DomainError) as caught:
                            B.apply_transition(built)
                    self.assertEqual(caught.exception.code, B.CODE_TERMINAL_STATE)
                    terminal += 1
                elif source == target:
                    with self.assertRaises(DomainError) as caught:
                        B.apply_transition(built)
                    self.assertEqual(caught.exception.code, B.CODE_SELF_TRANSITION)
                    self_transition += 1
                elif target in B.TRANSITIONS[source]:
                    outcome = B.apply_transition(built)
                    self.assertEqual(outcome.to_state, target)
                    legal += 1
                else:
                    with self.assertRaises(DomainError) as caught:
                        B.apply_transition(built)
                    self.assertEqual(caught.exception.code, B.CODE_INVALID_TRANSITION)
                    illegal += 1
        self.assertEqual(legal, len(B.all_transitions()))
        self.assertEqual(legal, 116)
        self.assertEqual(self_transition, 31)
        self.assertEqual(terminal, 32)
        self.assertEqual(illegal, 845)
        self.assertEqual(legal + self_transition + terminal + illegal, 32 * 32)

    def test_outcome_echoes_the_request_canonically(self) -> None:
        fields = request_fields(from_state="INQUIRY", to_state="QUALIFIED")
        outcome = B.apply_transition(B.TransitionRequest.from_mapping(fields), seq=4)
        self.assertEqual(outcome.as_dict(), {**fields, "seq": 4, "guards": []})
        self.assertEqual(outcome.guards, ())
        self.assertEqual(outcome.seq, 4)

    def test_guards_run_and_are_reported(self) -> None:
        outcome = guarded_action("CANCELLATION_REVIEW", "CANCELLED")
        self.assertEqual(outcome.guards, ("approval", "reason"))
        self.assertEqual(outcome.reason, ALL_GUARD_FIELDS["reason"])
        self.assertEqual(outcome.approval_ref, ALL_GUARD_FIELDS["approval_ref"])

    def test_missing_guard_fields_fail_with_their_own_code(self) -> None:
        cases = (
            ("IN_STAY", "CANCELLATION_REQUESTED", {}, B.CODE_MISSING_REASON),
            ("PRE_ARRIVAL", "NO_SHOW", {"reason": ALL_GUARD_FIELDS["reason"]}, B.CODE_MISSING_EVIDENCE),
            (
                "IN_STAY",
                "RELOCATED",
                {"reason": ALL_GUARD_FIELDS["reason"]},
                B.CODE_MISSING_APPROVAL,
            ),
            (
                "CANCELLATION_REVIEW",
                "CANCELLED",
                {"reason": ALL_GUARD_FIELDS["reason"]},
                B.CODE_MISSING_APPROVAL,
            ),
            ("REFUND_PENDING", "REFUNDED", {}, B.CODE_MISSING_EVIDENCE),
            ("BALANCE_DUE", "PAID_IN_FULL", {}, B.CODE_MISSING_EVIDENCE),
            ("COMPLETED", "DISPUTED", {}, B.CODE_MISSING_REASON),
        )
        for source, target, provided, code in cases:
            fields = request_fields(from_state=source, to_state=target)
            fields.update(provided)
            with self.subTest(edge=(source, target)):
                with self.assertRaises(DomainError) as caught:
                    B.apply_transition(B.TransitionRequest.from_mapping(fields))
                self.assertEqual(caught.exception.code, code)
                assert_screaming_snake(self, caught.exception.code)

    def test_guard_fields_satisfy_every_guarded_edge(self) -> None:
        for (source, _wildcard), names in B.TRANSITION_GUARDS.items():
            if source != "*":
                continue
        guarded = 0
        for source, target in B.all_transitions():
            if not B.guards_for(source, target):
                continue
            with self.subTest(edge=(source, target)):
                outcome = guarded_action(source, target)
                self.assertEqual(outcome.guards, B.guards_for(source, target))
                guarded += 1
        self.assertGreater(guarded, 20)

    def test_default_guards_can_be_disabled(self) -> None:
        fields = request_fields(from_state="IN_STAY", to_state="CANCELLATION_REQUESTED")
        outcome = B.apply_transition(
            B.TransitionRequest.from_mapping(fields),
            use_default_guards=False,
        )
        self.assertEqual(outcome.guards, ())
        with self.assertRaises(DomainError) as caught:
            B.apply_transition(B.TransitionRequest.from_mapping(fields))
        self.assertEqual(caught.exception.code, B.CODE_MISSING_REASON)

    def test_extra_guard_can_allow_and_is_reported(self) -> None:
        calls = []

        def guard_refund_window(context):
            calls.append((context.request.to_state, context.seq, context.fact("days_left")))
            return None

        outcome = B.apply_transition(
            request(from_state="CANCELLATION_REVIEW", to_state="CANCELLED", **ALL_GUARD_FIELDS),
            seq=5,
            extra_guards=[guard_refund_window],
            facts={"days_left": 12},
        )
        self.assertEqual(calls, [("CANCELLED", 5, 12)])
        self.assertIn("guard_refund_window", outcome.guards)
        self.assertEqual(outcome.guards, tuple(sorted(outcome.guards)))
        self.assertEqual(outcome.guards, ("approval", "guard_refund_window", "reason"))

    def test_extra_guard_can_reject(self) -> None:
        def guard_no_refund(context):
            raise DomainError(
                "refund window closed",
                code="REFUND_WINDOW_CLOSED",
                days_left=context.fact("days_left"),
            )

        with self.assertRaises(DomainError) as caught:
            B.apply_transition(
                request(from_state="REFUND_PENDING", to_state="REFUNDED", **ALL_GUARD_FIELDS),
                extra_guards=[guard_no_refund],
                facts={"days_left": -1},
            )
        self.assertEqual(caught.exception.code, "REFUND_WINDOW_CLOSED")
        self.assertEqual(caught.exception.details.get("days_left"), -1)

    def test_extra_guard_by_name_is_resolved(self) -> None:
        """A guard asked for by name runs and is reported, exactly like a callable."""
        satisfied = B.apply_transition(
            request(from_state="INQUIRY", to_state="QUALIFIED", evidence_ref="evt_0001"),
            extra_guards=["evidence"],
        )
        self.assertEqual(satisfied.guards, ("evidence",))
        with self.assertRaises(DomainError) as caught:
            B.apply_transition(
                request(from_state="INQUIRY", to_state="QUALIFIED"),
                extra_guards=[B.GUARD_EVIDENCE],
            )
        self.assertEqual(caught.exception.code, B.CODE_MISSING_EVIDENCE)
        with self.assertRaises(DomainError) as caught:
            B.apply_transition(
                request(), extra_guards=["refund_window"]
            )
        self.assertEqual(caught.exception.code, B.CODE_UNKNOWN_GUARD)

    def test_extra_guard_entries_must_be_names_or_callables(self) -> None:
        with self.assertRaises(DomainError) as caught:
            B.apply_transition(request(), extra_guards=[5])
        self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)

    def test_duplicate_guard_names_run_once(self) -> None:
        calls = []

        def guard_counted(context):
            calls.append(context.seq)

        outcome = B.apply_transition(
            request(from_state="IN_STAY", to_state="CANCELLATION_REQUESTED", reason="guest asked"),
            extra_guards=[guard_counted, guard_counted],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome.guards.count("guard_counted"), 1)

    def test_terminal_state_never_moves(self) -> None:
        for target in sorted(B.BOOKING_STATES):
            fields = request_fields(from_state="CLOSED", to_state=target)
            fields.update(ALL_GUARD_FIELDS)
            with self.subTest(target=target):
                with self.assertRaises(DomainError) as caught:
                    B.apply_transition(B.TransitionRequest.from_mapping(fields))
                self.assertEqual(caught.exception.code, B.CODE_TERMINAL_STATE)

    def test_unknown_states_are_refused(self) -> None:
        for source, target, field in (
            ("BOOKED", "QUALIFIED", "from_state"),
            ("INQUIRY", "BOOKED", "to_state"),
            ("inquiry_2", "QUALIFIED", "from_state"),
        ):
            fields = request_fields(from_state=source, to_state=target)
            fields.update(ALL_GUARD_FIELDS)
            with self.subTest(edge=(source, target)):
                with self.assertRaises(DomainError) as caught:
                    B.apply_transition(B.TransitionRequest.from_mapping(fields))
                self.assertEqual(caught.exception.code, B.CODE_UNKNOWN_STATE)
                self.assertEqual(caught.exception.details.get("field"), field)

    def test_seq_must_be_a_positive_int(self) -> None:
        for bad in (0, -1, 1.0, True, "1", None, 2.5):
            with self.subTest(seq=bad):
                with self.assertRaises(DomainError) as caught:
                    B.apply_transition(guarded_helper_request(), seq=bad)
                self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)
                assert_screaming_snake(self, caught.exception.code)

    def test_apply_accepts_a_mapping_or_rejects_junk(self) -> None:
        outcome = B.apply_transition(request_fields(from_state="CANCELLED", to_state="CLOSED"))
        self.assertEqual(outcome.from_state, "CANCELLED")
        for junk in (None, 5, "INQUIRY", object()):
            with self.subTest(junk=junk):
                with self.assertRaises(DomainError) as caught:
                    B.apply_transition(junk)
                self.assertEqual(caught.exception.code, B.CODE_INVALID_FIELD)

    def test_apply_is_deterministic(self) -> None:
        first = guarded_action("PRE_ARRIVAL", "NO_SHOW")
        second = guarded_action("PRE_ARRIVAL", "NO_SHOW")
        self.assertEqual(first, second)
        self.assertEqual(first.as_dict(), second.as_dict())


def guarded_helper_request() -> B.TransitionRequest:
    """A plain request used by the seq tests (seq is validated before anything else)."""
    return B.TransitionRequest.from_mapping(request_fields())


# --------------------------------------------------------------------------- #
# 6. Journal rows
# --------------------------------------------------------------------------- #
class TestJournalRow(unittest.TestCase):
    """The outcome is meant to be appended verbatim to booking_state_events."""

    def test_event_row_matches_the_journal_columns(self) -> None:
        row = guarded_action("PRE_ARRIVAL", "NO_SHOW").event_row("se_00000001")
        self.assertEqual(tuple(row), DDL_JOURNAL_COLUMNS)
        self.assertEqual(set(row), set(DDL_JOURNAL_COLUMNS))

    def test_event_row_values_echo_the_outcome(self) -> None:
        outcome = guarded_action("BALANCE_DUE", "PAID_IN_FULL", reason="transfer cleared")
        row = outcome.event_row("se_00000002")
        self.assertEqual(row["state_event_id"], "se_00000002")
        self.assertEqual(row["booking_id"], outcome.booking_id)
        self.assertEqual(row["seq"], outcome.seq)
        self.assertEqual(row["from_state"], "BALANCE_DUE")
        self.assertEqual(row["to_state"], "PAID_IN_FULL")
        self.assertEqual(row["occurred_at"], "2026-09-21T08:00:00+00:00")
        self.assertEqual(row["reason"], "transfer cleared")
        self.assertNotIn("guards", row)

    def test_event_row_requires_a_ref(self) -> None:
        outcome = guarded_action("CANCELLED", "CLOSED")
        for bad in (None, "", "bad id"):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as caught:
                    outcome.event_row(bad)
                assert_screaming_snake(self, caught.exception.code)

    def test_event_row_is_json_serialisable_and_float_free(self) -> None:
        row = guarded_action("IN_STAY", "RELOCATED").event_row("se_00000003")
        self.assertIn("RELOCATED", json.dumps(row, sort_keys=True))
        for value in iter_scalars(row):
            self.assertNotIsInstance(value, float)


# --------------------------------------------------------------------------- #
# 7. Real database: the CHECK enum and the journal
# --------------------------------------------------------------------------- #
class TestAgainstSqlite(unittest.TestCase):
    """Prove the derived set against a live database built from the same DDL."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO properties (property_id, name, property_type, region, province,"
            " timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES ('prop_1','Canggu Villa','villa','Bali','Bali','Asia/Makassar',"
            " 'SELLABLE','ACTIVATED','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        self.conn.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES ('unit_1','prop_1','Villa A','entire_villa',2,2,2,4,'ACTIVE',"
            " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        self.conn.execute(
            "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
            " effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
            " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
            " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('pol_1','prop_1','property',1,'2026-01-01','2026-12-31','IDR','30','10','10',"
            " '20',48,720,2,'{}','" + "a" * 64 + "','doc_1','ops:lead',"
            " '2026-01-01T00:00:00+00:00')"
        )

    def _insert_booking(self, index: int, state: str) -> None:
        day = f"{index:02d}"
        self.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
            " nights, status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, total_minor, payment_status, idempotency_key, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                # Same width as ``request_fields()["booking_id"]`` so the journal
                # FK below matches the row this helper just wrote.
                f"bk_{index:06d}",
                "prop_1",
                "unit_1",
                f"2026-03-{day}",
                f"2026-04-{day}",
                30,
                state,
                "guest_1",
                2,
                0,
                0,
                "IDR",
                "pol_1",
                "a" * 64,
                1_000_000,
                "UNPAID",
                f"idem_{index:04d}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    def test_database_accepts_every_derived_state(self) -> None:
        for index, state in enumerate(DDL_STATES, start=1):
            with self.subTest(state=state):
                self._insert_booking(index, state)
        stored = self.conn.execute(
            "SELECT status FROM bookings ORDER BY booking_id"
        ).fetchall()
        self.assertEqual([row["status"] for row in stored], list(DDL_STATES))

    def test_database_rejects_a_state_that_is_not_in_the_enum(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_booking(1, "BOOKED")

    def _insert_slot(self, booking_id: str, day_in: str, day_out: str, state: str) -> None:
        """Insert one booking on an explicit window, so two rows can collide."""
        self.conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out,"
            " nights, status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, total_minor, payment_status, idempotency_key, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                booking_id,
                "prop_1",
                "unit_1",
                day_in,
                day_out,
                30,
                state,
                "guest_1",
                2,
                0,
                0,
                "IDR",
                "pol_1",
                "a" * 64,
                1_000_000,
                "UNPAID",
                f"idem_{booking_id}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    def test_review_and_dispute_bindings_are_refused_by_the_database(self) -> None:
        """ADR-0002 Opsi A, proven against the DDL rather than the constants.

        ``uq_bookings_active_slot`` is what makes the no-double-booking invariant
        structural. If the predicate dropped the cancellation/dispute statuses, the
        kernel's Python check could still be bypassed by any writer that touches the
        table directly — so the enforcement is asserted here, one window per state.
        """
        windows = {
            "CANCELLATION_REQUESTED": ("2026-05-01", "2026-06-01"),
            "CANCELLATION_REVIEW": ("2026-05-02", "2026-06-02"),
            "DISPUTED": ("2026-05-03", "2026-06-03"),
        }
        for index, (state, (day_in, day_out)) in enumerate(windows.items()):
            with self.subTest(state=state):
                # One row per state: the primary key and the idempotency key are
                # derived from the id, so each subtest needs its own.
                self._insert_slot(f"bk_review_{index}", day_in, day_out, state)
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert_slot(f"bk_resale_{index}", day_in, day_out, "CONFIRMED")

    def test_a_decided_cancellation_does_release_the_slot(self) -> None:
        """The other half of the decision: fail-closed, not permanently stuck."""
        self._insert_slot("bk_gone", "2026-07-01", "2026-08-01", "CANCELLED")
        self._insert_slot("bk_resale", "2026-07-01", "2026-08-01", "CONFIRMED")
        stored = self.conn.execute(
            "SELECT booking_id FROM bookings WHERE status = 'CONFIRMED'"
        ).fetchall()
        self.assertEqual([row["booking_id"] for row in stored], ["bk_resale"])
        self.assertTrue(B.is_occupying_state("CONFIRMED"))

    def test_journal_row_can_be_inserted_and_is_append_only(self) -> None:
        self._insert_booking(1, "INQUIRY")
        outcome = guarded_action("INQUIRY", "QUALIFIED")
        row = outcome.event_row("se_00000001")
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO booking_state_events ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        stored = self.conn.execute(
            "SELECT * FROM booking_state_events WHERE state_event_id = 'se_00000001'"
        ).fetchone()
        self.assertEqual(stored["to_state"], "QUALIFIED")
        self.assertEqual(stored["seq"], 1)
        self.assertEqual(dict(stored), row)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"INSERT INTO booking_state_events ({columns}) VALUES ({placeholders})",
                ("se_00000002",) + tuple(row.values())[1:],
            )


# --------------------------------------------------------------------------- #
# 8. House rules: no floats, SCREAMING_SNAKE codes, public surface
# --------------------------------------------------------------------------- #
class TestHouseRules(unittest.TestCase):
    """Floating point money/seq and half-defined error codes are both forbidden."""

    EXPECTED_CODES = {
        "CODE_UNKNOWN_STATE": "UNKNOWN_STATE",
        "CODE_SELF_TRANSITION": "SELF_TRANSITION",
        "CODE_INVALID_TRANSITION": "INVALID_TRANSITION",
        "CODE_TERMINAL_STATE": "TERMINAL_STATE",
        "CODE_MISSING_FIELD": "MISSING_FIELD",
        "CODE_INVALID_FIELD": "INVALID_FIELD",
        "CODE_MISSING_REASON": "MISSING_REASON",
        "CODE_MISSING_EVIDENCE": "MISSING_EVIDENCE",
        "CODE_MISSING_APPROVAL": "MISSING_APPROVAL",
        "CODE_UNKNOWN_GUARD": "UNKNOWN_GUARD",
        "CODE_SCHEMA_STATE_ENUM_MISMATCH": "SCHEMA_STATE_ENUM_MISMATCH",
        "CODE_SCHEMA_ENUM_UNAVAILABLE": "SCHEMA_ENUM_UNAVAILABLE",
        "CODE_INVALID_STATE_GRAPH": "INVALID_STATE_GRAPH",
    }

    def test_error_codes_are_screaming_snake_and_exactly_as_documented(self) -> None:
        for name, value in self.EXPECTED_CODES.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(B, name), value)
                assert_screaming_snake(self, value)
        self.assertEqual(len(set(self.EXPECTED_CODES.values())), 13)

    def test_module_errors_use_only_those_codes(self) -> None:
        observed = set()

        def collect(call) -> None:
            try:
                call()
            except DomainError as error:
                observed.add(error.code)
                assert_screaming_snake(self, error.code)

        collect(lambda: B.allowed_next_states("BOOKED"))
        collect(lambda: B.guards_for("INQUIRY", "IN_STAY"))
        collect(lambda: B.guards_for("BOOKED", "QUALIFIED"))
        collect(lambda: B.check_state_enum(set(DDL_STATES) | {"BOOKED"}))
        collect(lambda: B.apply_transition(request(from_state="INQUIRY", to_state="IN_STAY")))
        collect(lambda: B.apply_transition(request(from_state="INQUIRY", to_state="INQUIRY")))
        collect(lambda: B.apply_transition(request(from_state="CLOSED", to_state="CLOSED")))
        collect(lambda: B.apply_transition(request(from_state="BOOKED", to_state="QUALIFIED")))
        collect(lambda: B.apply_transition(request(from_state="QUALIFIED", to_state="CANCELLED")))
        collect(lambda: B.apply_transition(request(), extra_guards=["nope"]))
        collect(lambda: guarded_action("REFUND_PENDING", "REFUNDED", evidence_ref=None))
        collect(lambda: guarded_action("COMPLETED", "DISPUTED", reason=None))
        collect(lambda: guarded_action("IN_STAY", "RELOCATED", approval_ref=None))
        collect(lambda: guarded_action("PRE_ARRIVAL", "NO_SHOW", evidence_ref=None))
        self.assertEqual(
            observed,
            {
                B.CODE_UNKNOWN_STATE,
                B.CODE_SELF_TRANSITION,
                B.CODE_INVALID_TRANSITION,
                B.CODE_TERMINAL_STATE,
                B.CODE_MISSING_REASON,
                B.CODE_MISSING_EVIDENCE,
                B.CODE_MISSING_APPROVAL,
                B.CODE_UNKNOWN_GUARD,
                B.CODE_SCHEMA_STATE_ENUM_MISMATCH,
            },
        )

    def test_no_floating_point_anywhere_in_the_module(self) -> None:
        surfaces = [
            B.STATES_IN_DECLARATION_ORDER,
            B.states_from_schema(),
            B.occupying_states_from_schema(),
            B.all_transitions(),
            tuple(B.TRANSITION_GUARDS.items()),
            tuple(sorted(B.GUARDS)),
            B.reachable_from("INQUIRY"),
            B.allowed_next_states("PRE_ARRIVAL"),
            guarded_action("PRE_ARRIVAL", "NO_SHOW").as_dict(),
        ]
        for surface in surfaces:
            for value in iter_scalars(surface):
                self.assertNotIsInstance(value, float)
        outcome = guarded_action("PRE_ARRIVAL", "NO_SHOW")
        self.assertIsInstance(outcome.seq, int)
        self.assertNotIsInstance(outcome.seq, bool)

    def test_public_collections_are_immutable_or_sorted(self) -> None:
        self.assertIsInstance(B.BOOKING_STATES, frozenset)
        self.assertIsInstance(B.TRANSITIONS, dict)
        for target in B.TRANSITIONS.values():
            self.assertIsInstance(target, frozenset)
        self.assertEqual(B.states_from_schema(), tuple(sorted(B.states_from_schema())))
        self.assertEqual(B.all_transitions(), tuple(sorted(B.all_transitions())))

    def test_all_exports_resolve(self) -> None:
        self.assertTrue(B.__all__)
        for name in B.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(B, name), f"{name} is in __all__ but undefined")
        for name in (
            "BOOKING_STATES",
            "STATES_IN_DECLARATION_ORDER",
            "STATE_COUNT",
            "TERMINAL_STATES",
            "OCCUPYING_STATES",
            "TRANSITIONS",
            "TRANSITION_GUARDS",
            "GUARDS",
            "GuardContext",
            "TransitionRequest",
            "TransitionOutcome",
            "apply_transition",
            "guards_for",
            "states_from_schema",
            "verify_against_schema",
            *self.EXPECTED_CODES,
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(B, name), f"{name} is missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

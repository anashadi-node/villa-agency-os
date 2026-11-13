"""Canonical booking state machine for Villa Agency OS.

The 32 booking states are **not invented here** — they are the ``CHECK`` enum on
``bookings.status`` in ``src/schema/001_initial.sql`` (lines 258-266), and
``booking_state_events`` is the append-only transition journal this module
produces rows for. :func:`states_from_schema` re-reads the DDL through
:mod:`schema_loader`, and :func:`verify_against_schema` fails closed with
``SCHEMA_STATE_ENUM_MISMATCH`` when the literal table below drifts from it. The
check also runs at import time, so a schema edit that is not mirrored here
breaks the module loudly instead of silently accepting an unknown state.

Three layers live in this module:

1. **Vocabulary** — :data:`BOOKING_STATES`, :data:`TERMINAL_STATES`,
   :data:`OCCUPYING_STATES` (the partial index ``uq_bookings_active_slot``
   predicate: which statuses actually block a re-sale).
2. **Graph** — :data:`TRANSITIONS` (``from_state -> frozenset(to_state)``),
   validated at import for unknown targets, self-loops, declared-vs-actual
   terminal states and unreachable states.
3. **Guards** — :data:`TRANSITION_GUARDS` maps a transition (``"*"`` = any
   origin) to the guards that must pass before the journal row may be written
   (``reason``, ``evidence``, ``approval``). Callers may add their own hooks via
   ``apply_transition(..., extra_guards=...)``.

Determinism rules: no floating point anywhere, every returned collection is a
sorted ``tuple`` (or ``frozenset``) so repeated calls are byte-identical, and
the only clock-free input is ``occurred_at``, supplied by the caller.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from primitives import DomainError, parse_iso, require_ref, require_text, to_iso
from schema_loader import SchemaError, parse_schema_model

__all__ = [
    # error codes
    "CODE_UNKNOWN_STATE",
    "CODE_SELF_TRANSITION",
    "CODE_INVALID_TRANSITION",
    "CODE_TERMINAL_STATE",
    "CODE_MISSING_FIELD",
    "CODE_INVALID_FIELD",
    "CODE_MISSING_REASON",
    "CODE_MISSING_EVIDENCE",
    "CODE_MISSING_APPROVAL",
    "CODE_UNKNOWN_GUARD",
    "CODE_SCHEMA_STATE_ENUM_MISMATCH",
    "CODE_SCHEMA_ENUM_UNAVAILABLE",
    "CODE_INVALID_STATE_GRAPH",
    # vocabulary
    "BOOKING_STATES",
    "TERMINAL_STATES",
    "OCCUPYING_STATES",
    "STATES_IN_DECLARATION_ORDER",
    "STATE_COUNT",
    "TRANSITIONS",
    "TRANSITION_GUARDS",
    "GUARDS",
    "GUARD_REASON",
    "GUARD_EVIDENCE",
    "GUARD_APPROVAL",
    # schema derivation
    "states_from_schema",
    "occupying_states_from_schema",
    "check_state_enum",
    "verify_against_schema",
    # graph queries
    "is_known_state",
    "is_terminal",
    "is_occupying_state",
    "sink_states",
    "allowed_next_states",
    "can_transition",
    "guards_for",
    "reachable_from",
    "reaches_terminal",
    "all_transitions",
    # transition API
    "TransitionRequest",
    "GuardContext",
    "TransitionOutcome",
    "apply_transition",
]


# --------------------------------------------------------------------------- #
# Error codes (the machine-readable contract; never parse the message)
# --------------------------------------------------------------------------- #
CODE_UNKNOWN_STATE = "UNKNOWN_STATE"
CODE_SELF_TRANSITION = "SELF_TRANSITION"
CODE_INVALID_TRANSITION = "INVALID_TRANSITION"
CODE_TERMINAL_STATE = "TERMINAL_STATE"
CODE_MISSING_FIELD = "MISSING_FIELD"
CODE_INVALID_FIELD = "INVALID_FIELD"
CODE_MISSING_REASON = "MISSING_REASON"
CODE_MISSING_EVIDENCE = "MISSING_EVIDENCE"
CODE_MISSING_APPROVAL = "MISSING_APPROVAL"
CODE_UNKNOWN_GUARD = "UNKNOWN_GUARD"
CODE_SCHEMA_STATE_ENUM_MISMATCH = "SCHEMA_STATE_ENUM_MISMATCH"
CODE_SCHEMA_ENUM_UNAVAILABLE = "SCHEMA_ENUM_UNAVAILABLE"
CODE_INVALID_STATE_GRAPH = "INVALID_STATE_GRAPH"


# --------------------------------------------------------------------------- #
# Vocabulary — transcribed in DDL order from bookings.status (001_initial.sql)
# --------------------------------------------------------------------------- #
STATES_IN_DECLARATION_ORDER: tuple[str, ...] = (
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

BOOKING_STATES: frozenset[str] = frozenset(STATES_IN_DECLARATION_ORDER)
STATE_COUNT = len(STATES_IN_DECLARATION_ORDER)

#: No outgoing transitions — the sole sink of the graph. Verified at import
#: against the computed sinks so the two can never disagree.
TERMINAL_STATES: frozenset[str] = frozenset({"CLOSED"})

#: Occupying statuses, from the partial unique index ``uq_bookings_active_slot``
#: in the DDL: a cancelled/closed booking must not block a re-sale, these do.
OCCUPYING_STATES: frozenset[str] = frozenset(
    {
        "CONFIRMED",
        "BALANCE_DUE",
        "BALANCE_OVERDUE",
        "PRE_ARRIVAL",
        "IN_STAY",
        # ADR-0002 (Opsi A): a cancellation under review, and a dispute raised
        # while the guest is still inside the villa, keep the dates locked until
        # the cancellation is DECIDED (-> CANCELLED) or the dispute is closed.
        # The slot is never released on the strength of a promise not to re-sell.
        "CANCELLATION_REQUESTED",
        "CANCELLATION_REVIEW",
        "DISPUTED",
    }
)


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #
#: ``from_state -> sorted tuple of legal next states``. Every state in
#: :data:`BOOKING_STATES` has an entry (a terminal state maps to ``()``).
_TRANSITION_TABLE: Mapping[str, tuple[str, ...]] = {
    "INQUIRY": ("CANCELLED", "QUALIFIED"),
    # Fast paths that skip an intermediate bookkeeping state. A manager who
    # already holds the owner's calendar may confirm availability without a
    # separate "pending" step, may quote straight from a confirmed calendar,
    # may activate the hold when the deposit proof arrives, and may mark the
    # deposit paid while the hold is still running (Indonesian bank transfers
    # routinely clear inside the hold window). None of these skip a *fact*:
    # the guard on the target state still demands its evidence row.
    "QUALIFIED": ("AVAILABILITY_CONFIRMED", "AVAILABILITY_PENDING", "CANCELLED"),
    "AVAILABILITY_PENDING": ("AVAILABILITY_CONFIRMED", "AVAILABILITY_UNAVAILABLE", "CANCELLED"),
    "AVAILABILITY_UNAVAILABLE": ("AVAILABILITY_PENDING", "CANCELLED"),
    "AVAILABILITY_CONFIRMED": ("CANCELLED", "QUOTE_DRAFTED", "QUOTE_SENT"),
    "QUOTE_DRAFTED": ("CANCELLED", "QUOTE_SENT"),
    "QUOTE_SENT": ("CANCELLED", "HOLD_ACTIVE", "HOLD_REQUESTED", "QUOTE_EXPIRED"),
    "QUOTE_EXPIRED": ("CANCELLED", "QUOTE_DRAFTED"),
    "HOLD_REQUESTED": ("CANCELLED", "HOLD_ACTIVE", "HOLD_EXPIRED"),
    "HOLD_ACTIVE": ("CANCELLED", "DEPOSIT_PAID", "HOLD_EXPIRED", "PAYMENT_PENDING"),
    "HOLD_EXPIRED": ("CANCELLED", "HOLD_REQUESTED", "QUOTE_DRAFTED"),
    "PAYMENT_PENDING": (
        "CANCELLED",
        "DEPOSIT_PAID",
        "PAID_IN_FULL",
        "PAYMENT_FAILED",
        "PAYMENT_PARTIAL",
        "PAYMENT_UNMATCHED",
    ),
    "PAYMENT_PARTIAL": (
        "CANCELLED",
        "DEPOSIT_PAID",
        "PAID_IN_FULL",
        "PAYMENT_FAILED",
        "PAYMENT_UNMATCHED",
    ),
    "PAYMENT_UNMATCHED": (
        "CANCELLED",
        "DEPOSIT_PAID",
        "PAID_IN_FULL",
        "PAYMENT_FAILED",
        "PAYMENT_PARTIAL",
    ),
    "PAYMENT_FAILED": ("CANCELLED", "PAYMENT_PENDING"),
    "DEPOSIT_PAID": ("CANCELLED", "CONFIRMED", "PAID_IN_FULL", "REFUND_PENDING"),
    "PAID_IN_FULL": ("CANCELLED", "CONFIRMED", "REFUND_PENDING"),
    "CONFIRMED": (
        "BALANCE_DUE",
        # ADR-0069 (keputusan owner 2026-11-13, opsi a): a confirmed booking can be
        # cancelled directly, not only through CANCELLATION_REQUESTED. The reasons are
        # real and happen before arrival: overbooking discovered after confirmation,
        # owner withdrawing the villa, force majeure, and a guest whose funds never
        # clear. Every one of these used to be written by the kernel through an edge
        # the canonical graph did not have, so the graph — the map every audit and
        # every agent trusts — claimed the situation was impossible.
        "CANCELLED",
        "CANCELLATION_REQUESTED",
        "DISPUTED",
        "NO_SHOW",
        "PAID_IN_FULL",
        "PRE_ARRIVAL",
        "RELOCATED",
    ),
    "BALANCE_DUE": (
        "BALANCE_OVERDUE",
        "CANCELLED",  # ADR-0069: same direct path as CONFIRMED (balance never paid).
        "CANCELLATION_REQUESTED",
        "DISPUTED",
        "NO_SHOW",
        "PAID_IN_FULL",
        "PRE_ARRIVAL",
        "RELOCATED",
    ),
    "BALANCE_OVERDUE": (
        "CANCELLATION_REQUESTED",
        "CANCELLED",
        "DISPUTED",
        "NO_SHOW",
        "PAID_IN_FULL",
        "PRE_ARRIVAL",
        "RELOCATED",
    ),
    # ADR-0069: a confirmed arrival can still be cancelled directly (owner withdrawal,
    # villa made unavailable by damage, overbooking found late). The courtesy path via
    # CANCELLATION_REQUESTED stays the default when the guest asks and money is at stake.
    "PRE_ARRIVAL": ("CANCELLED", "CANCELLATION_REQUESTED", "DISPUTED", "IN_STAY", "NO_SHOW", "RELOCATED"),
    # IN_STAY -> NO_SHOW is deliberately NOT an edge: NO_SHOW is not an
    # occupying status, so allowing it while the guest is physically in the
    # villa would release the slot for re-sale (double-booking) and misstate
    # occupancy. A no-show is decided before arrival (CONFIRMED/BALANCE_DUE/
    # BALANCE_OVERDUE/PRE_ARRIVAL -> NO_SHOW), never during the stay.
    # ADR-0069: a stay can be cut short and cancelled directly (relocation is preferred
    # when another unit is available; cancellation is what happens when it is not).
    "IN_STAY": ("CANCELLED", "CANCELLATION_REQUESTED", "COMPLETED", "DISPUTED", "RELOCATED"),
    "COMPLETED": ("CLOSED", "DISPUTED", "REFUND_PENDING"),
    "CANCELLATION_REQUESTED": ("CANCELLATION_REVIEW", "CANCELLED"),
    "CANCELLATION_REVIEW": ("CANCELLED", "CONFIRMED", "NO_SHOW", "REFUND_PENDING"),
    "CANCELLED": ("CLOSED", "DISPUTED", "REFUND_PENDING"),
    "REFUND_PENDING": ("DISPUTED", "PAYMENT_FAILED", "REFUNDED"),
    "REFUNDED": ("CLOSED", "DISPUTED"),
    "NO_SHOW": ("CLOSED", "DISPUTED", "REFUND_PENDING", "RELOCATED"),
    "RELOCATED": ("CLOSED", "COMPLETED", "DISPUTED", "REFUND_PENDING"),
    "DISPUTED": ("CANCELLED", "CLOSED", "COMPLETED", "REFUND_PENDING", "REFUNDED"),
    "CLOSED": (),
}

TRANSITIONS: Mapping[str, frozenset[str]] = {
    state: frozenset(targets) for state, targets in _TRANSITION_TABLE.items()
}


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
GUARD_REASON = "reason"
GUARD_EVIDENCE = "evidence"
GUARD_APPROVAL = "approval"

#: ``(from_state, to_state) -> guard names``; ``from_state == "*"`` means "any
#: origin". Concrete and wildcard entries are unioned (see :func:`guards_for`).
GUARD_TABLE: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("*", "CANCELLATION_REQUESTED"): (GUARD_REASON,),
    ("*", "CANCELLED"): (GUARD_REASON,),
    ("CANCELLATION_REVIEW", "CANCELLED"): (GUARD_REASON, GUARD_APPROVAL),
    ("*", "DISPUTED"): (GUARD_REASON,),
    ("*", "NO_SHOW"): (GUARD_REASON, GUARD_EVIDENCE),
    ("*", "RELOCATED"): (GUARD_REASON, GUARD_APPROVAL),
    ("*", "REFUND_PENDING"): (GUARD_REASON, GUARD_APPROVAL),
    ("*", "REFUNDED"): (GUARD_APPROVAL, GUARD_EVIDENCE),
    ("*", "CONFIRMED"): (GUARD_EVIDENCE,),
    ("*", "DEPOSIT_PAID"): (GUARD_EVIDENCE,),
    ("*", "PAID_IN_FULL"): (GUARD_EVIDENCE,),
    ("*", "PAYMENT_PARTIAL"): (GUARD_EVIDENCE,),
    ("*", "PAYMENT_UNMATCHED"): (GUARD_EVIDENCE,),
    ("*", "PAYMENT_FAILED"): (GUARD_EVIDENCE,),
    ("AVAILABILITY_PENDING", "AVAILABILITY_CONFIRMED"): (GUARD_EVIDENCE,),
}

#: Normalised public view: guard names sorted, so iteration is deterministic.
TRANSITION_GUARDS: Mapping[tuple[str, str], tuple[str, ...]] = {
    key: tuple(sorted(names)) for key, names in GUARD_TABLE.items()
}


@dataclass(frozen=True)
class GuardContext:
    """Everything a guard hook may look at. Guards raise to reject."""

    request: "TransitionRequest"
    seq: int
    facts: Mapping[str, Any] = field(default_factory=dict)

    def fact(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)


#: A guard hook: return ``None`` to allow, raise :class:`DomainError` to reject.
Guard = Callable[[GuardContext], None]


def _reject(code: str, message: str, context: GuardContext, **details: Any) -> "DomainError":
    return DomainError(
        message,
        code=code,
        from_state=context.request.from_state,
        to_state=context.request.to_state,
        command=context.request.command,
        **details,
    )


def guard_require_reason(context: GuardContext) -> None:
    """Transitions that rewrite a commitment must state why."""
    if context.request.reason is None:
        raise _reject(
            CODE_MISSING_REASON,
            f"a reason is required to move to {context.request.to_state}",
            context,
            field="reason",
        )


def guard_require_evidence(context: GuardContext) -> None:
    """Availability/payment movements must cite their evidence row."""
    if context.request.evidence_ref is None:
        raise _reject(
            CODE_MISSING_EVIDENCE,
            f"evidence_ref is required to move to {context.request.to_state}",
            context,
            field="evidence_ref",
        )


def guard_require_approval(context: GuardContext) -> None:
    """Money- and guest-impacting movements need a recorded approval."""
    if context.request.approval_ref is None:
        raise _reject(
            CODE_MISSING_APPROVAL,
            f"approval_ref is required to move to {context.request.to_state}",
            context,
            field="approval_ref",
        )


#: Name -> hook. ``apply_transition`` resolves names through this registry.
GUARDS: Mapping[str, Guard] = {
    GUARD_APPROVAL: guard_require_approval,
    GUARD_EVIDENCE: guard_require_evidence,
    GUARD_REASON: guard_require_reason,
}

#: Evaluation order of the built-in guards, most fundamental first: a movement
#: with no evidence row cannot be "approved" into legality, and a movement that
#: rewrites a commitment states why before anyone signs it off. Reporting order
#: (:attr:`TransitionOutcome.guards`) stays alphabetical so journal rows are
#: stable no matter what order the hooks ran in.
_GUARD_EVALUATION_ORDER: tuple[str, ...] = (GUARD_EVIDENCE, GUARD_REASON, GUARD_APPROVAL)


def _evaluation_order(names: Iterable[str]) -> list[str]:
    """Built-in guards in precedence order, then any custom names alphabetically."""
    builtin = [name for name in _GUARD_EVALUATION_ORDER if name in names]
    custom = sorted(name for name in names if name not in _GUARD_EVALUATION_ORDER)
    return builtin + custom


# --------------------------------------------------------------------------- #
# Schema derivation
# --------------------------------------------------------------------------- #
_STATUS_ENUM_KEY = "bookings.status"
_OCCUPYING_INDEX = "uq_bookings_active_slot"
_SQL_LITERAL = re.compile(r"'([^']*)'")
_GRAPH_START_STATE = "INQUIRY"
_REQUIRED_REQUEST_FIELDS: tuple[str, ...] = (
    "booking_id",
    "from_state",
    "to_state",
    "command",
    "actor",
    "actor_role",
    "command_id",
    "occurred_at",
)
_OPTIONAL_REQUEST_FIELDS: tuple[str, ...] = ("evidence_ref", "approval_ref", "reason")


def _schema_model(dialect: str = "sqlite"):
    try:
        return parse_schema_model(dialect)
    except (SchemaError, OSError, ValueError) as exc:  # fail closed, never guess
        raise DomainError(
            f"cannot read the {dialect} schema to derive the booking states: {exc}",
            code=CODE_SCHEMA_ENUM_UNAVAILABLE,
            dialect=dialect,
        ) from exc


def states_from_schema(dialect: str = "sqlite") -> tuple[str, ...]:
    """The canonical booking states, extracted from the DDL's ``CHECK`` enum.

    ``schema_loader`` folds SQL text to lowercase, so the values are upper-cased
    again here before being compared — the DDL literals themselves are
    SCREAMING_SNAKE (``src/schema/001_initial.sql``, ``bookings.status``).
    """
    model = _schema_model(dialect)
    values = model.enums.get(_STATUS_ENUM_KEY)
    if not values:
        raise DomainError(
            "the schema declares no status enum for bookings.status",
            code=CODE_SCHEMA_ENUM_UNAVAILABLE,
            dialect=dialect,
            key=_STATUS_ENUM_KEY,
        )
    return tuple(sorted(value.strip().upper() for value in values))


def occupying_states_from_schema(dialect: str = "sqlite") -> tuple[str, ...]:
    """States named by the ``uq_bookings_active_slot`` partial index predicate."""
    model = _schema_model(dialect)
    index = model.indexes.get(_OCCUPYING_INDEX)
    if index is None or not index.predicate:
        raise DomainError(
            f"the schema does not define the {_OCCUPYING_INDEX} partial index",
            code=CODE_SCHEMA_ENUM_UNAVAILABLE,
            dialect=dialect,
            index=_OCCUPYING_INDEX,
        )
    literals = {m.group(1).strip().upper() for m in _SQL_LITERAL.finditer(index.predicate)}
    if not literals:
        raise DomainError(
            f"could not read any state literal out of {_OCCUPYING_INDEX}",
            code=CODE_SCHEMA_ENUM_UNAVAILABLE,
            dialect=dialect,
            predicate=index.predicate,
        )
    derived = literals & BOOKING_STATES
    # Two independent drifts, reported together: the predicate names a status the
    # enum does not define (``unknown``), and the predicate omits a status this
    # module treats as occupying (``missing``). Both must fail closed — a
    # predicate that is too narrow re-sells a blocked unit, one that is too wide
    # blocks a free one.
    unknown = tuple(sorted(literals - BOOKING_STATES))
    missing = tuple(sorted(OCCUPYING_STATES - derived))
    if unknown or missing:
        raise DomainError(
            f"{_OCCUPYING_INDEX} names states that are not in the bookings.status enum",
            code=CODE_SCHEMA_STATE_ENUM_MISMATCH,
            dialect=dialect,
            unknown=unknown,
            missing=missing,
        )
    return tuple(sorted(literals))


def check_state_enum(declared: Iterable[str], dialect: str = "sqlite") -> None:
    """Fail closed unless ``declared`` is exactly the schema's enum.

    Raises:
        DomainError: code ``SCHEMA_STATE_ENUM_MISMATCH`` on any drift (a missing
            state, an invented one, a duplicated state, or a whitespace/case
            variant of a real one).
    """
    actual = frozenset(states_from_schema(dialect))
    normalised = tuple(str(item).strip().upper() for item in declared)
    given = frozenset(normalised)
    duplicates = tuple(sorted(name for name, count in Counter(normalised).items() if count > 1))
    if given != actual or duplicates:
        # Details are tuples, like every other public collection in this module:
        # a caller may hash/log them and never has to guard against mutation.
        raise DomainError(
            "declared booking states do not match the schema enum",
            code=CODE_SCHEMA_STATE_ENUM_MISMATCH,
            dialect=dialect,
            missing=tuple(sorted(actual - given)),
            unexpected=tuple(sorted(given - actual)),
            duplicates=duplicates,
        )


def verify_against_schema(dialect: str = "sqlite") -> None:
    """Assert the whole module vocabulary still matches the DDL. Returns None."""
    check_state_enum(BOOKING_STATES, dialect)
    check_state_enum(STATES_IN_DECLARATION_ORDER, dialect)
    mismatched = frozenset(occupying_states_from_schema(dialect)) ^ OCCUPYING_STATES
    if mismatched:
        raise DomainError(
            "OCCUPYING_STATES does not match the schema's active-slot index",
            code=CODE_SCHEMA_STATE_ENUM_MISMATCH,
            dialect=dialect,
            mismatched=sorted(mismatched),
        )


# --------------------------------------------------------------------------- #
# Graph queries (all collections are sorted tuples: idempotent ordering)
# --------------------------------------------------------------------------- #
def is_known_state(state: Any) -> bool:
    """True when ``state`` is one of the 32 canonical states (no coercion)."""
    return isinstance(state, str) and state.strip().upper() in BOOKING_STATES


def _require_state(state: Any, field_name: str = "state") -> str:
    normalised = state.strip().upper() if isinstance(state, str) else state
    if not isinstance(normalised, str) or not normalised:
        raise DomainError(f"{field_name} is required", code=CODE_MISSING_FIELD, field=field_name)
    if normalised not in BOOKING_STATES:
        raise DomainError(
            f"{field_name} is not a bookings.status value: {state!r}",
            code=CODE_UNKNOWN_STATE,
            field=field_name,
            state=state,
        )
    return normalised


def is_terminal(state: Any) -> bool:
    """True for a state with no outgoing transition (currently ``CLOSED``)."""
    return _require_state(state) in TERMINAL_STATES


def is_occupying_state(state: Any) -> bool:
    """True when the status blocks a re-sale (schema partial-index semantics)."""
    return _require_state(state) in OCCUPYING_STATES


def sink_states() -> frozenset[str]:
    """Computed terminal states — must equal :data:`TERMINAL_STATES`."""
    return frozenset(state for state in BOOKING_STATES if not TRANSITIONS.get(state))


def allowed_next_states(state: Any) -> tuple[str, ...]:
    """Sorted legal successors of ``state`` (empty for a terminal state)."""
    return tuple(sorted(TRANSITIONS[_require_state(state)]))


def can_transition(from_state: Any, to_state: Any) -> bool:
    """True when the edge exists. Unknown/therefore-illegal states raise."""
    source = _require_state(from_state, "from_state")
    target = _require_state(to_state, "to_state")
    return target in TRANSITIONS[source]


def guards_for(from_state: Any, to_state: Any) -> tuple[str, ...]:
    """Sorted guard names that must pass for this edge.

    Raises ``UNKNOWN_STATE`` for an unknown state and ``INVALID_TRANSITION`` for
    a pair the graph does not contain (asking about an illegal edge is a bug,
    not a "no guards" answer).
    """
    source = _require_state(from_state, "from_state")
    target = _require_state(to_state, "to_state")
    if source == target or target not in TRANSITIONS[source]:
        raise DomainError(
            f"{source} -> {target} is not a legal booking transition",
            code=CODE_INVALID_TRANSITION,
            from_state=source,
            to_state=target,
        )
    names: set[str] = set(GUARD_TABLE.get(("*", target), ()))
    names |= set(GUARD_TABLE.get((source, target), ()))
    return tuple(sorted(names))


def reachable_from(state: Any) -> tuple[str, ...]:
    """Sorted states reachable from ``state``, excluding ``state`` itself."""
    start = _require_state(state)
    seen: set[str] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for nxt in TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return tuple(sorted(seen - {start}))


def reaches_terminal(state: Any) -> bool:
    """True when a path to a terminal state exists (every state: it does)."""
    return bool(set(reachable_from(state)) & TERMINAL_STATES) or is_terminal(state)


def all_transitions() -> tuple[tuple[str, str], ...]:
    """Every legal edge as a sorted tuple of ``(from_state, to_state)`` pairs."""
    return tuple(
        sorted(
            (source, target)
            for source in BOOKING_STATES
            for target in TRANSITIONS[source]
        )
    )


# --------------------------------------------------------------------------- #
# Import-time self-consistency of the graph (fail closed, never guess)
# --------------------------------------------------------------------------- #
def _assert_graph_consistency() -> None:
    if set(TRANSITIONS) != set(BOOKING_STATES):
        raise DomainError(
            "TRANSITIONS must have exactly one entry per booking state",
            code=CODE_INVALID_STATE_GRAPH,
            missing=sorted(set(BOOKING_STATES) - set(TRANSITIONS)),
            extra=sorted(set(TRANSITIONS) - set(BOOKING_STATES)),
        )
    for source, targets in TRANSITIONS.items():
        unknown = sorted(set(targets) - set(BOOKING_STATES))
        if unknown:
            raise DomainError(
                f"TRANSITIONS[{source!r}] targets unknown states",
                code=CODE_INVALID_STATE_GRAPH,
                from_state=source,
                unknown=unknown,
            )
        if source in targets:
            raise DomainError(
                f"TRANSITIONS[{source!r}] contains a self-loop",
                code=CODE_INVALID_STATE_GRAPH,
                state=source,
            )
    computed = sink_states()
    if computed != TERMINAL_STATES:
        raise DomainError(
            "TERMINAL_STATES disagrees with the computed sinks of TRANSITIONS",
            code=CODE_INVALID_STATE_GRAPH,
            declared=sorted(TERMINAL_STATES),
            computed=sorted(computed),
        )
    unreachable = sorted(set(BOOKING_STATES) - ({_GRAPH_START_STATE} | set(reachable_from(_GRAPH_START_STATE))))
    if unreachable:
        raise DomainError(
            "some booking states are unreachable from the entry state",
            code=CODE_INVALID_STATE_GRAPH,
            entry=_GRAPH_START_STATE,
            unreachable=unreachable,
        )
    stranded = sorted(state for state in BOOKING_STATES if not reaches_terminal(state))
    if stranded:
        raise DomainError(
            "some booking states cannot reach a terminal state",
            code=CODE_INVALID_STATE_GRAPH,
            stranded=stranded,
        )
    for key, names in GUARD_TABLE.items():
        source, target = key
        if target not in BOOKING_STATES:
            raise DomainError(
                "GUARD_TABLE targets an unknown state",
                code=CODE_INVALID_STATE_GRAPH,
                key=list(key),
                state=target,
            )
        if source != "*":
            if source not in BOOKING_STATES or target not in TRANSITIONS.get(source, frozenset()):
                raise DomainError(
                    "GUARD_TABLE references an illegal transition",
                    code=CODE_INVALID_STATE_GRAPH,
                    key=list(key),
                )
        elif not any(target in targets for targets in TRANSITIONS.values()):
            raise DomainError(
                "GUARD_TABLE wildcard references a target no state can reach",
                code=CODE_INVALID_STATE_GRAPH,
                key=list(key),
            )
        for name in names:
            if name not in GUARDS:
                raise DomainError(
                    "GUARD_TABLE names a guard that is not registered",
                    code=CODE_UNKNOWN_GUARD,
                    key=list(key),
                    guard=name,
                    registered=sorted(GUARDS),
                )


_assert_graph_consistency()
verify_against_schema()


# --------------------------------------------------------------------------- #
# Transition API
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TransitionRequest:
    """A command asking to move one booking from ``from_state`` to ``to_state``.

    Field shapes are enforced here (missing/blank/malformed -> ``MISSING_FIELD``
    / ``INVALID_FIELD``); whether the edge exists at all is decided by
    :func:`apply_transition`.
    """

    booking_id: str
    from_state: str
    to_state: str
    command: str
    actor: str
    actor_role: str
    command_id: str
    occurred_at: str
    evidence_ref: Optional[str] = None
    approval_ref: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "booking_id", require_ref(self.booking_id, "booking_id"))
        object.__setattr__(self, "from_state", _normalise_state(self.from_state, "from_state"))
        object.__setattr__(self, "to_state", _normalise_state(self.to_state, "to_state"))
        object.__setattr__(self, "command", require_text(self.command, "command", max_len=64))
        object.__setattr__(self, "actor", require_text(self.actor, "actor", max_len=128))
        object.__setattr__(self, "actor_role", require_text(self.actor_role, "actor_role", max_len=64))
        command_id = require_ref(self.command_id, "command_id", max_len=64)
        if len(command_id) < 8:  # matches CHECK (length(command_id) >= 8)
            raise DomainError(
                "command_id must be at least 8 characters",
                code=CODE_INVALID_FIELD,
                field="command_id",
                length=len(command_id),
            )
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "occurred_at", _normalise_timestamp(self.occurred_at))
        object.__setattr__(self, "evidence_ref", _optional_ref(self.evidence_ref, "evidence_ref"))
        object.__setattr__(self, "approval_ref", _optional_ref(self.approval_ref, "approval_ref"))
        reason = self.reason
        if reason is not None:
            reason = require_text(reason, "reason", max_len=2000)
        object.__setattr__(self, "reason", reason)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "TransitionRequest":
        """Build from a dict of journal fields; absent keys fail as ``MISSING_FIELD``."""
        if not isinstance(mapping, Mapping):
            raise DomainError(
                "a transition request must be a mapping",
                code=CODE_INVALID_FIELD,
                field="request",
                got=type(mapping).__name__,
            )
        values: dict[str, Any] = {}
        for name in _REQUIRED_REQUEST_FIELDS:
            if name not in mapping or mapping[name] is None:
                raise DomainError(f"{name} is required", code=CODE_MISSING_FIELD, field=name)
            values[name] = mapping[name]
        for name in _OPTIONAL_REQUEST_FIELDS:
            values[name] = mapping.get(name)
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        """Canonical echo of the *set* fields.

        Optional fields that were not supplied are omitted rather than echoed as
        ``None``: ``from_mapping(self.as_dict())`` must rebuild the same request,
        and a ``None``-filled payload would claim a guard field was considered.
        """
        payload: dict[str, Any] = {
            "booking_id": self.booking_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "command": self.command,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "command_id": self.command_id,
            "occurred_at": self.occurred_at,
        }
        for name, value in (
            ("evidence_ref", self.evidence_ref),
            ("approval_ref", self.approval_ref),
            ("reason", self.reason),
        ):
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class TransitionOutcome:
    """An accepted transition: the journal row, ready to append."""

    booking_id: str
    seq: int
    from_state: str
    to_state: str
    command: str
    actor: str
    actor_role: str
    command_id: str
    occurred_at: str
    evidence_ref: Optional[str] = None
    approval_ref: Optional[str] = None
    reason: Optional[str] = None
    guards: tuple[str, ...] = ()

    def event_row(self, state_event_id: str) -> dict[str, Any]:
        """Rows for ``booking_state_events``; ``state_event_id`` is supplied by the caller."""
        return {
            "state_event_id": require_ref(state_event_id, "state_event_id"),
            "booking_id": self.booking_id,
            "seq": self.seq,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "command": self.command,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "command_id": self.command_id,
            "evidence_ref": self.evidence_ref,
            "approval_ref": self.approval_ref,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }

    def as_dict(self) -> dict[str, Any]:
        """Canonical echo of the outcome; unset guard fields are omitted."""
        payload: dict[str, Any] = {
            "booking_id": self.booking_id,
            "seq": self.seq,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "command": self.command,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "command_id": self.command_id,
            "occurred_at": self.occurred_at,
        }
        for name, value in (
            ("evidence_ref", self.evidence_ref),
            ("approval_ref", self.approval_ref),
            ("reason", self.reason),
        ):
            if value is not None:
                payload[name] = value
        payload["guards"] = list(self.guards)
        return payload


def _normalise_state(state: Any, field_name: str) -> str:
    if state is None:
        raise DomainError(f"{field_name} is required", code=CODE_MISSING_FIELD, field=field_name)
    if not isinstance(state, str):
        raise DomainError(
            f"{field_name} must be a string",
            code=CODE_INVALID_FIELD,
            field=field_name,
            got=type(state).__name__,
        )
    text = state.strip().upper()
    if not text:
        raise DomainError(f"{field_name} is required", code=CODE_MISSING_FIELD, field=field_name)
    return text


def _normalise_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, str):
        return to_iso(parse_iso(value))
    raise DomainError(
        "occurred_at must be an ISO-8601 string or an aware datetime",
        code=CODE_INVALID_FIELD,
        field="occurred_at",
        got=type(value).__name__,
    )


def _optional_ref(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return require_ref(value, field_name)


def _check_seq(seq: Any) -> int:
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise DomainError(
            "seq must be an int (bool and float are refused)",
            code=CODE_INVALID_FIELD,
            field="seq",
            got=type(seq).__name__,
        )
    if seq < 1:
        raise DomainError("seq must be >= 1", code=CODE_INVALID_FIELD, field="seq", value=seq)
    return seq


def _resolve_guards(names: Sequence[str]) -> list[tuple[str, Guard]]:
    resolved: list[tuple[str, Guard]] = []
    for name in names:
        hook = GUARDS.get(name)
        if hook is None:
            raise DomainError(
                f"unknown guard {name!r}",
                code=CODE_UNKNOWN_GUARD,
                guard=name,
                registered=sorted(GUARDS),
            )
        resolved.append((name, hook))
    return resolved


def _resolve_extra_guards(extra: Iterable[Any]) -> list[tuple[str, Guard]]:
    if isinstance(extra, (str, bytes)) or not isinstance(extra, Iterable):
        raise DomainError(
            "extra_guards must be an iterable of registered names or callables",
            code=CODE_INVALID_FIELD,
            field="extra_guards",
            got=type(extra).__name__,
        )
    resolved: list[tuple[str, Guard]] = []
    for item in extra:
        if isinstance(item, str):
            hook = GUARDS.get(item)
            if hook is None:
                raise DomainError(
                    f"unknown guard {item!r}",
                    code=CODE_UNKNOWN_GUARD,
                    guard=item,
                    registered=sorted(GUARDS),
                )
            resolved.append((item, hook))
            continue
        if callable(item):
            resolved.append((getattr(item, "__name__", "custom_guard"), item))
            continue
        raise DomainError(
            "extra_guards entries must be registered names or callables",
            code=CODE_INVALID_FIELD,
            field="extra_guards",
            got=type(item).__name__,
        )
    return resolved


def apply_transition(
    request: Any,
    *,
    seq: int = 1,
    use_default_guards: bool = True,
    extra_guards: Iterable[Any] = (),
    facts: Optional[Mapping[str, Any]] = None,
) -> TransitionOutcome:
    """Validate a transition, run its guards, and return the journal row.

    Validation order (each failure carries its own code):

    1. field shape — ``MISSING_FIELD`` / ``INVALID_FIELD`` (in the request)
    2. unknown state — ``UNKNOWN_STATE``
    3. origin is terminal — ``TERMINAL_STATE``
    4. no-op — ``SELF_TRANSITION``
    5. edge not in the graph — ``INVALID_TRANSITION``
    6. guards — ``MISSING_REASON`` / ``MISSING_EVIDENCE`` / ``MISSING_APPROVAL``
       (plus ``UNKNOWN_GUARD`` for a hook name that is not registered)
    """
    if isinstance(request, Mapping):
        request = TransitionRequest.from_mapping(request)
    if not isinstance(request, TransitionRequest):
        raise DomainError(
            "apply_transition expects a TransitionRequest or a mapping",
            code=CODE_INVALID_FIELD,
            field="request",
            got=type(request).__name__,
        )
    _check_seq(seq)

    source = request.from_state
    target = request.to_state
    if source not in BOOKING_STATES:
        raise DomainError(
            f"from_state is not a bookings.status value: {source!r}",
            code=CODE_UNKNOWN_STATE,
            field="from_state",
            state=source,
        )
    if target not in BOOKING_STATES:
        raise DomainError(
            f"to_state is not a bookings.status value: {target!r}",
            code=CODE_UNKNOWN_STATE,
            field="to_state",
            state=target,
        )
    if source in TERMINAL_STATES:
        raise DomainError(
            f"{source} is terminal; a closed booking never moves again",
            code=CODE_TERMINAL_STATE,
            from_state=source,
            to_state=target,
        )
    if source == target:
        raise DomainError(
            f"{source} -> {target} is a no-op; states must change",
            code=CODE_SELF_TRANSITION,
            from_state=source,
            to_state=target,
        )
    if target not in TRANSITIONS[source]:
        raise DomainError(
            f"{source} -> {target} is not a legal booking transition",
            code=CODE_INVALID_TRANSITION,
            from_state=source,
            to_state=target,
            allowed=sorted(TRANSITIONS[source]),
        )

    hooks: list[tuple[str, Guard]] = []
    if use_default_guards:
        hooks.extend(_resolve_guards(list(guards_for(source, target))))
    hooks.extend(_resolve_extra_guards(extra_guards))

    ordered: dict[str, Guard] = {}
    for name, hook in hooks:
        ordered.setdefault(name, hook)

    context = GuardContext(request=request, seq=seq, facts=dict(facts or {}))
    for name in _evaluation_order(ordered):
        ordered[name](context)

    return TransitionOutcome(
        booking_id=request.booking_id,
        seq=seq,
        from_state=source,
        to_state=target,
        command=request.command,
        actor=request.actor,
        actor_role=request.actor_role,
        command_id=request.command_id,
        occurred_at=request.occurred_at,
        evidence_ref=request.evidence_ref,
        approval_ref=request.approval_ref,
        reason=request.reason,
        guards=tuple(sorted(ordered)),
    )

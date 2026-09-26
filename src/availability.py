"""The canonical availability state (§7, §11, Appendix A).

§11 is explicit: "Min-stay/occupancy restrictions menghasilkan explicit restricted
state, bukan NOT_AVAILABLE". Appendix A fixes the vocabulary. This module is the single
home for that vocabulary and for deriving the state from evidence.

Two rules shape the derivation:

* The state is **derived, never stored** — a stored copy would drift from the evidence
  it claims to describe.
* **Freshness is checked before the result.** An out-of-date "yes" is not a current
  "yes": stale evidence becomes RECONFIRMATION_REQUIRED rather than an invitation to
  quote, which is why a favourable result never shortcuts the freshness check.

CHECK_REQUESTED and CHECKING are states the platform asks for, not results a supplier
reports, so they are excluded from ``SOURCE_RESULTS``: the vocabulary is one set, but
the table only accepts the half a source can legitimately claim.

Those two states are also the two halves of the §7 *request*: they belong to a check
the platform opened, and a check is over once a source has answered it. That flow lives
here too, for the same reason the vocabulary does — a second module deriving states
would be a second answer to the same question.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Optional, Sequence

from gates import (
    CODE_AVAILABILITY_UNKNOWN,
    CODE_EVIDENCE_EXPIRED,
    CODE_EVIDENCE_STALE,
    CODE_EVIDENCE_TIMESTAMP_INVALID,
    CODE_OK,
    CODE_UNIT_OCCUPIED,
    LEVEL_FRESHNESS_SECONDS,
)
from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    SystemClock,
    parse_iso,
    require_ref,
    restriction_payload,
)

__all__ = [
    "ANSWERED_STATE",
    "ANSWERED_STATES",
    "AVAILABILITY_STATES",
    "CHECK_OPEN_STATES",
    "CHECK_REASON_CODES",
    "CHECK_STATES",
    "CHECK_TRANSITIONS",
    "REQUEST_STATES",
    "RESTRICTED_RESULT",
    "RESTRICTION_KEYS",
    "SOURCE_RESULTS",
    "AvailabilityService",
    "AvailabilityState",
    "check_transition_allowed",
    "derive_check_state",
    "derive_state",
]

# Appendix A, verbatim and in order.
AVAILABILITY_STATES: tuple[str, ...] = (
    "UNKNOWN",
    "CHECK_REQUESTED",
    "CHECKING",
    "AVAILABLE",
    "NOT_AVAILABLE",
    "AVAILABLE_BUT_RESTRICTED",
    "RECONFIRMATION_REQUIRED",
)

# States the platform drives; a source cannot report them as a result.
REQUEST_STATES: tuple[str, ...] = ("CHECK_REQUESTED", "CHECKING")

# What a source may legitimately claim, and therefore what the table accepts.
SOURCE_RESULTS: tuple[str, ...] = (
    "AVAILABLE",
    "NOT_AVAILABLE",
    "AVAILABLE_BUT_RESTRICTED",
    "UNKNOWN",
)

RESTRICTED_RESULT = "AVAILABLE_BUT_RESTRICTED"

# The two restrictions the spec names. A restricted state must say which one applies.
RESTRICTION_KEYS: tuple[str, ...] = ("min_stay_nights", "max_occupancy")

# --------------------------------------------------------------------------- #
# The §7 check-availability request
# --------------------------------------------------------------------------- #
# A request has its own lifecycle: the platform asks, the source answers, and the
# request is finished. CHECK_REQUESTED and CHECKING are shared with the availability
# vocabulary above (they are the subject's state while nothing has answered yet);
# ANSWERED is not shared, because it describes the *request*, and after it the answer
# speaks for itself. Keeping the two vocabularies in one module and separate names is
# the point: they overlap without being the same set.
ANSWERED_STATE = "ANSWERED"

#: Every state a check row can be in, mirroring the schema CHECK verbatim.
CHECK_STATES: tuple[str, ...] = (*REQUEST_STATES, ANSWERED_STATE)

#: The states in which a check is still waiting for an answer.
CHECK_OPEN_STATES: tuple[str, ...] = REQUEST_STATES

#: Legal moves. A source may answer a request that was never marked CHECKING — an
#: integration that returns the answer in the same call has nothing to mark in
#: between, and inventing a CHECKING step for it would record a transition that never
#: happened. ANSWERED is terminal: re-answering is a new request, not an edit.
CHECK_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "CHECK_REQUESTED": ("CHECKING", ANSWERED_STATE),
    "CHECKING": (ANSWERED_STATE,),
    ANSWERED_STATE: (),
}

#: Reason codes for the two request states. They are deliberately not gate codes: a
#: gate never refuses *because* a check is in flight, it refuses because there is no
#: evidence, which is a different sentence.
CODE_CHECK_PENDING = "AVAILABILITY_CHECK_PENDING"
CODE_CHECK_IN_PROGRESS = "AVAILABILITY_CHECK_IN_PROGRESS"
CHECK_REASON_CODES: Mapping[str, str] = {
    "CHECK_REQUESTED": CODE_CHECK_PENDING,
    "CHECKING": CODE_CHECK_IN_PROGRESS,
}

#: States a caller may act on. UNKNOWN and RECONFIRMATION_REQUIRED are both "no answer
#: yet", and that is exactly when an in-flight request is the more useful report.
ANSWERED_STATES: tuple[str, ...] = ("AVAILABLE", "NOT_AVAILABLE", RESTRICTED_RESULT)


def check_transition_allowed(from_state: str, to_state: str) -> bool:
    """Whether a check may move ``from_state`` to ``to_state``."""
    return to_state in CHECK_TRANSITIONS.get(str(from_state).strip().upper(), ())


@dataclass(frozen=True)
class AvailabilityState:
    """The canonical state plus the provenance that produced it."""

    state: str
    reason_code: str
    evidence_id: Optional[str] = None
    evidence_level: Optional[str] = None
    valid_until: Optional[str] = None
    restrictions: Mapping[str, Any] = field(default_factory=dict)


def _moment(value: Any) -> Optional[datetime]:
    """A timezone-aware moment, or ``None`` when the input cannot be read as one."""
    if isinstance(value, datetime):
        moment = value
    elif value is None or str(value).strip() == "":
        return None
    else:
        try:
            moment = parse_iso(str(value))
        except (TypeError, ValueError):
            return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def derive_state(evidence: Mapping[str, Any], *, at: Any) -> AvailabilityState:
    """Derive the canonical availability state of one evidence row.

    Never raises on bad data: unreadable input produces UNKNOWN or
    RECONFIRMATION_REQUIRED, because the caller's next move is to ask the supplier
    again, not to trust the row.
    """
    row = dict(evidence)
    moment = _moment(at)
    if moment is None:
        raise DomainError("at must be a timestamp", code="INVALID_TIMESTAMP")

    level = str(row.get("evidence_level") or "").strip().upper()
    result = str(row.get("result") or "").strip().upper()
    evidence_id = row.get("evidence_id")
    valid_until_raw = row.get("valid_until")
    restrictions = restriction_payload(row.get("restrictions"))

    def outcome(state: str, reason: str, **over: Any) -> AvailabilityState:
        return AvailabilityState(
            state=state,
            reason_code=reason,
            evidence_id=evidence_id,
            evidence_level=level or None,
            valid_until=valid_until_raw if isinstance(valid_until_raw, str) else None,
            restrictions=restrictions,
            **over,
        )

    if result not in SOURCE_RESULTS or level not in LEVEL_FRESHNESS_SECONDS:
        return outcome("UNKNOWN", CODE_AVAILABILITY_UNKNOWN)

    captured = _moment(row.get("captured_at"))
    if captured is None:
        return outcome("UNKNOWN", CODE_EVIDENCE_TIMESTAMP_INVALID)

    valid_until = _moment(valid_until_raw)
    if valid_until is not None and valid_until <= moment:
        return outcome("RECONFIRMATION_REQUIRED", CODE_EVIDENCE_EXPIRED)

    window = LEVEL_FRESHNESS_SECONDS[level]
    if window <= 0 or (moment - captured).total_seconds() > window:
        return outcome("RECONFIRMATION_REQUIRED", CODE_EVIDENCE_STALE)

    if result == "UNKNOWN":
        # A source saying "I do not know" is not a source saying "yes".
        return outcome("UNKNOWN", CODE_AVAILABILITY_UNKNOWN)
    if result == RESTRICTED_RESULT:
        return outcome(RESTRICTED_RESULT, "AVAILABILITY_RESTRICTED")
    if result == "NOT_AVAILABLE":
        return outcome("NOT_AVAILABLE", CODE_UNIT_OCCUPIED)
    return outcome("AVAILABLE", CODE_OK)


def derive_check_state(
    events: Sequence[Mapping[str, Any]],
    evidence: Optional[Mapping[str, Any]] = None,
    *,
    at: Any,
) -> AvailabilityState:
    """Derive the state of a check from its event log, oldest first.

    The newest event decides, because the log is the history and the last line of a
    history is what is true now. Once a check is ANSWERED the request has no further
    opinion: the state becomes whatever the cited evidence says *now*, which is how a
    stale answer turns into RECONFIRMATION_REQUIRED without anything being rewritten.
    """
    moment = _moment(at)
    if moment is None:
        raise DomainError("at must be a timestamp", code="INVALID_TIMESTAMP")
    rows = sorted((dict(row) for row in events), key=lambda row: int(row.get("seq") or 0))
    if not rows:
        # A request writes its own first event, so no events means no request.
        return AvailabilityState(state="UNKNOWN", reason_code=CODE_AVAILABILITY_UNKNOWN)
    newest = rows[-1]
    state = str(newest.get("state") or "").strip().upper()
    if state == ANSWERED_STATE:
        if evidence is None:
            # An answer that cites nothing is not an answer.
            return AvailabilityState(state="UNKNOWN", reason_code=CODE_AVAILABILITY_UNKNOWN)
        return derive_state(evidence, at=moment)
    if state in CHECK_OPEN_STATES:
        return AvailabilityState(
            state=state,
            reason_code=CHECK_REASON_CODES[state],
            evidence_id=newest.get("evidence_id"),
        )
    return AvailabilityState(state="UNKNOWN", reason_code=CODE_AVAILABILITY_UNKNOWN)


class AvailabilityService:
    """Derives canonical availability states from stored evidence.

    Read-only on purpose: ``ReservationKernel.record_evidence`` stays the single
    writer, so there is exactly one path that can create availability evidence.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        agency_id: str = DEFAULT_AGENCY_ID,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or SystemClock()
        self.agency_id = require_ref(agency_id, "agency_id")

    def state_of(self, evidence_id: str, *, at: Any = None) -> AvailabilityState:
        reference = require_ref(evidence_id, "evidence_id")
        row = self.conn.execute(
            "SELECT * FROM availability_evidence WHERE evidence_id = ? AND agency_id = ?",
            (reference, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "unknown availability evidence",
                code="EVIDENCE_NOT_FOUND",
                evidence_id=reference,
            )
        return derive_state(dict(row), at=at or self.clock.now())

    # ----------------------------------------------------------------- #
    # The §7 check-availability request
    # ----------------------------------------------------------------- #
    def check(self, check_id: str) -> Mapping[str, Any]:
        """One request row, scoped to this agency."""
        reference = require_ref(check_id, "check_id")
        row = self.conn.execute(
            "SELECT * FROM availability_checks WHERE check_id = ? AND agency_id = ?",
            (reference, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "unknown availability check",
                code="CHECK_NOT_FOUND",
                check_id=reference,
            )
        return dict(row)

    def check_events(self, check_id: str) -> list[Mapping[str, Any]]:
        """The request's event log, oldest first."""
        reference = require_ref(check_id, "check_id")
        rows = self.conn.execute(
            "SELECT * FROM availability_check_events WHERE check_id = ? ORDER BY seq",
            (reference,),
        ).fetchall()
        return [dict(row) for row in rows]

    def check_state(self, check_id: str, *, at: Any = None) -> AvailabilityState:
        """The request's state: still asking, or whatever its answer says now."""
        self.check(check_id)  # an unknown request is refused, not reported as UNKNOWN
        events = self.check_events(check_id)
        evidence_id = None
        for event in reversed(events):
            if event["state"] == ANSWERED_STATE:
                evidence_id = event.get("evidence_id")
                break
        evidence = None
        if evidence_id:
            row = self.conn.execute(
                "SELECT * FROM availability_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            evidence = dict(row) if row is not None else None
        return derive_check_state(events, evidence, at=at or self.clock.now())

    def open_check(
        self, unit_id: str, *, check_in: str, check_out: str
    ) -> Optional[Mapping[str, Any]]:
        """The newest request for exactly this window that is still waiting.

        The window is part of the question: a request for other dates says nothing
        about these, so it is not reported here.
        """
        rows = self.conn.execute(
            "SELECT * FROM availability_checks"
            " WHERE agency_id = ? AND unit_id = ? AND check_in = ? AND check_out = ?"
            " ORDER BY requested_at DESC, check_id DESC",
            (
                self.agency_id,
                require_ref(unit_id, "unit_id"),
                require_ref(check_in, "check_in"),
                require_ref(check_out, "check_out"),
            ),
        ).fetchall()
        for row in rows:
            events = self.check_events(row["check_id"])
            if events and events[-1]["state"] in CHECK_OPEN_STATES:
                return dict(row)
        return None

    def state_for(
        self, unit_id: str, *, check_in: str, check_out: str, at: Any = None
    ) -> AvailabilityState:
        """The canonical state of a unit for a window: answer first, request second.

        Three rules, in order:

        1. A usable answer *is* the state. A fresh AVAILABLE stays AVAILABLE even while
           somebody asks again, because hiding a valid answer behind an in-flight
           request would be the worse report.
        2. Otherwise an in-flight request for this exact window is the state: "we are
           asking" says more than "we have nothing".
        3. Otherwise UNKNOWN.

        Evidence is unit-level in this schema (a row carries occupancy ranges, not a
        window), so rule 1 reads the unit's newest capture; the window narrows only the
        requests. That limitation belongs to the schema, and it is stated here rather
        than papered over.
        """
        moment = at or self.clock.now()
        answer = self._latest_answer(unit_id, at=moment)
        if answer is not None and answer.state in ANSWERED_STATES:
            return answer
        request = self.open_check(unit_id, check_in=check_in, check_out=check_out)
        if request is not None:
            return self.check_state(request["check_id"], at=moment)
        if answer is not None:
            return answer
        return AvailabilityState(state="UNKNOWN", reason_code=CODE_AVAILABILITY_UNKNOWN)

    def _latest_answer(self, unit_id: str, *, at: Any) -> Optional[AvailabilityState]:
        """The derived state of the unit's most recent capture, or ``None`` if none."""
        row = self.conn.execute(
            "SELECT * FROM availability_evidence WHERE agency_id = ? AND unit_id = ?"
            " ORDER BY captured_at DESC, evidence_id DESC LIMIT 1",
            (self.agency_id, require_ref(unit_id, "unit_id")),
        ).fetchone()
        return derive_state(dict(row), at=at) if row is not None else None

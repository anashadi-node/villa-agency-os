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
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Optional

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
    "AVAILABILITY_STATES",
    "REQUEST_STATES",
    "RESTRICTED_RESULT",
    "RESTRICTION_KEYS",
    "SOURCE_RESULTS",
    "AvailabilityService",
    "AvailabilityState",
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

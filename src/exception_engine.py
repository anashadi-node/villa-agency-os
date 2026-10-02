"""The exception engine: a failure becomes a recorded obligation, exactly once.

What problem this solves
------------------------
Everything else in this repository refuses loudly *before* it does harm. This module is for
the other case: something already went wrong — a job threw, a provider refused, a detector
found a hole — and the question is no longer "may this happen" but **who must know, how fast,
and can we prove afterwards that we told them**.

Three disciplines, and the failure each one prevents:

* **A severity is looked up, never guessed.** The caller states a *category*; the table below
  maps it to a playbook and a severity (SEV1..SEV4, `incidents.py`). An unknown category is
  refused, because "we opened it as a warning because nothing else fit" is how a data breach
  gets a 24-hour SLA.
* **A repeating failure is one incident with a count.** The fingerprint of
  (agency, source, category, code, aggregate) is stored with a window: the second arrival
  increments `occurrences` instead of waking the GM again. A loop that breaks 400 times a
  minute produces one incident and a number a post-mortem can use.
* **The report commits with the fact.** Opening an incident also writes an outbox event
  (ADR-0007), so the notification is a record that survives a crash — not a callback that may
  or may not have fired. The engine writes; it never sends (a sender lives outside, ADR-0037).

What this module deliberately does **not** do
---------------------------------------------
It does not decide that something *is* a failure: the caller brings a classified failure. It
does not send, page, or retry (the outbox publisher does). And it does not invent a playbook
for a category the documents do not cover — the Security & Privacy Warden's own playbook
(ADR-0039) is its own slice; until it exists, a security finding routes to an existing
playbook and says so, rather than acquiring a severity nobody wrote down.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional

from incidents import IncidentService
from outbox import OutboxService
from primitives import (
    Clock,
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "CAPTURE_EVENT_TYPE",
    "DEFAULT_DEDUPE_WINDOW_MINUTES",
    "FAILURE_ROUTES",
    "FailureRoute",
    "Capture",
    "ExceptionEngine",
    "InvalidFailure",
    "UnknownFailureCategory",
]

#: The outbox event this engine writes. §14: a typed letter, not a sentence.
CAPTURE_EVENT_TYPE = "incident.failure_recorded"

#: How long the same failure is treated as a repeat of the open incident. The window **rolls**:
#: it is measured from the *last* arrival, so a loop that keeps failing stays one incident with a
#: growing count (which is what a post-mortem wants), while a failure that returns after a quiet
#: spell opens a new obligation with its own clock. Thirty minutes swallows a retry loop and is
#: short enough that "it came back after lunch" is treated as news.
DEFAULT_DEDUPE_WINDOW_MINUTES = 30


@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Join the caller's transaction, or open one. Same discipline as the outbox: an incident
    and the fingerprint that points at it must commit together, or the count is a lie."""
    if getattr(conn, "in_transaction", False):
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

MIN_MESSAGE_CHARS = 8


class InvalidFailure(DomainError):
    """The failure could not be recorded as given."""

    code = "INVALID_FAILURE"


class UnknownFailureCategory(DomainError):
    """A category with no route: severity would have to be guessed, so it is refused."""

    code = "UNKNOWN_FAILURE_CATEGORY"


@dataclass(frozen=True)
class FailureRoute:
    """Where one kind of failure goes: which playbook, which severity, whose clock starts."""

    category: str
    playbook: str
    severity: str
    personal_data_involved: bool
    description: str


#: The classified failures this platform knows how to route. Every entry names an *existing*
#: playbook (`incidents.PLAYBOOKS`) and the severity the response document gives it — no entry
#: is invented here, and adding one is a policy act, not a code edit.
FAILURE_ROUTES: Mapping[str, FailureRoute] = MappingProxyType(
    {
        "DATA_EXPOSURE": FailureRoute(
            category="DATA_EXPOSURE",
            playbook="DATA_BREACH",
            severity="SEV1",
            personal_data_involved=True,
            description="Personal data was exposed or may have been. The 72-hour regulator clock starts.",
        ),
        "BOOKING_CONFLICT": FailureRoute(
            category="BOOKING_CONFLICT",
            playbook="DOUBLE_BOOKING",
            severity="SEV1",
            personal_data_involved=False,
            description="Two guests hold the same unit: a guest is standing at a door that is not theirs.",
        ),
        "SAFETY_EMERGENCY": FailureRoute(
            category="SAFETY_EMERGENCY",
            playbook="SAFETY_EMERGENCY",
            severity="SEV1",
            personal_data_involved=False,
            description="A person's safety is involved.",
        ),
        "PAYMENT_UNRECONCILED": FailureRoute(
            category="PAYMENT_UNRECONCILED",
            playbook="PAYMENT_MISMATCH",
            severity="SEV2",
            personal_data_involved=False,
            description="Money arrived that does not match a booking, or a booking that has no money.",
        ),
        "VENUE_UNAVAILABLE": FailureRoute(
            category="VENUE_UNAVAILABLE",
            playbook="VENUE_OUTAGE",
            severity="SEV2",
            personal_data_involved=False,
            description="A unit cannot be occupied as promised.",
        ),
        "PROVIDER_OUTAGE": FailureRoute(
            category="PROVIDER_OUTAGE",
            playbook="PROVIDER_FAILURE",
            severity="SEV3",
            personal_data_involved=False,
            description="A provider (payment, messaging, villa) is down or refusing.",
        ),
        "JOB_FAILURE": FailureRoute(
            category="JOB_FAILURE",
            playbook="PROVIDER_FAILURE",
            severity="SEV4",
            personal_data_involved=False,
            description="A scheduled job failed: hygiene until it is shown to have guest impact.",
        ),
    }
)

ROUTE_CATEGORIES: tuple[str, ...] = tuple(sorted(FAILURE_ROUTES))


@dataclass(frozen=True)
class Capture:
    """What one captured failure produced."""

    incident_id: str
    severity: str
    playbook: str
    category: str
    is_new_incident: bool
    occurrences: int
    dedupe_key: str
    outbox_event_id: str
    personal_data_involved: bool


class ExceptionEngine:
    """Turns a classified failure into an incident plus a report that cannot be lost."""

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = "agency_default",
        incidents: Optional[IncidentService] = None,
        outbox: Optional[OutboxService] = None,
        dedupe_window_minutes: int = DEFAULT_DEDUPE_WINDOW_MINUTES,
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)
        self.incidents = incidents if incidents is not None else IncidentService(
            conn, clock=self._clock, ids=self._ids, agency_id=self.agency_id
        )
        self.outbox = outbox if outbox is not None else OutboxService(
            conn, clock=self._clock, ids=self._ids, agency_id=self.agency_id
        )
        try:
            window = int(dedupe_window_minutes)
        except (TypeError, ValueError) as exc:
            raise InvalidFailure(
                "dedupe_window_minutes must be an integer", got=repr(dedupe_window_minutes)
            ) from exc
        if window < 1:
            raise InvalidFailure("dedupe_window_minutes must be at least 1", got=window)
        self.dedupe_window_minutes = window

    # -- the one entry point ---------------------------------------------- #
    def capture(
        self,
        *,
        category: str,
        code: str,
        message: str,
        source: str,
        aggregate_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        personal_data_involved: bool = False,
        at: Optional[datetime] = None,
    ) -> Capture:
        """Record one failure: open an incident, or count it against the open one.

        The caller brings the *classification* (category) and the *evidence* (code, message,
        source). Severity and playbook come from :data:`FAILURE_ROUTES`; personal data can be
        raised by the caller but **never lowered** — a route that says personal data is
        involved cannot be talked out of it by a caller in a hurry.
        """
        route = FAILURE_ROUTES.get(str(category).strip().upper())
        if route is None:
            raise UnknownFailureCategory(
                "this failure has no route: severity would have to be guessed",
                category=repr(category),
                known=list(ROUTE_CATEGORIES),
            )
        code = require_text(code, "code", max_len=64)
        source = require_text(source, "source", max_len=64)
        try:
            message = require_text(message, "message", max_len=500)
        except DomainError as exc:
            raise InvalidFailure("a failure needs a message a human can read", field="message") from exc
        if len(message) < MIN_MESSAGE_CHARS:
            raise InvalidFailure(
                f"message must be at least {MIN_MESSAGE_CHARS} characters",
                field="message",
                length=len(message),
            )
        focus = require_ref(aggregate_id, "aggregate_id", max_len=128) if aggregate_id else "-"
        moment = at if at is not None else self._clock.now()
        if moment.tzinfo is None:
            raise InvalidFailure("at must carry a timezone", at=str(moment))

        involves_people = bool(route.personal_data_involved or personal_data_involved)
        dedupe_key = sha256_hex(
            canonical_json(
                {
                    "agency_id": self.agency_id,
                    "category": route.category,
                    "code": code,
                    "source": source,
                    "aggregate_id": focus,
                }
            )
        )
        existing = self._fingerprint(dedupe_key)
        if existing is not None and self._within_window(existing, moment):
            return self._count_repeat(existing, route, moment, message, involves_people)

        incident = self.incidents.open_incident(
            playbook=route.playbook,
            title=f"[{route.category}] {source}: {code}",
            severity=route.severity,
            detected_by="system:exception_engine",
            source=source,
            affected_refs=(focus,) if focus != "-" else (),
            personal_data_involved=involves_people,
            summary=message,
            at=moment,
        )
        event = self.outbox.emit(
            event_type=CAPTURE_EVENT_TYPE,
            aggregate_type="incident",
            aggregate_id=incident.incident_id,
            payload={
                "category": route.category,
                "code": code,
                "source": source,
                "severity": route.severity,
                "playbook": route.playbook,
                "personal_data_involved": involves_people,
                "message": message,
                "dedupe_key": dedupe_key,
                "occurrences": 1,
                "details": dict(details or {}),
            },
            actor="system:exception_engine",
            idempotency_key=f"incident-{incident.incident_id}",
            correlation_id=incident.incident_id,
            at=moment,
        )
        with _write_transaction(self._conn):
            if existing is None:
                self._conn.execute(
                    "INSERT INTO exception_fingerprints (fingerprint_id, agency_id, dedupe_key,"
                    " category, source, code, severity, occurrences, incident_id, first_seen_at,"
                    " last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        self._ids.new("fp"),
                        self.agency_id,
                        dedupe_key,
                        route.category,
                        source,
                        code,
                        route.severity,
                        incident.incident_id,
                        to_iso(moment),
                        to_iso(moment),
                    ),
                )
            else:
                self._conn.execute(
                    "UPDATE exception_fingerprints SET occurrences = 1, incident_id = ?,"
                    " severity = ?, first_seen_at = ?, last_seen_at = ? WHERE fingerprint_id = ?",
                    (
                        incident.incident_id,
                        route.severity,
                        to_iso(moment),
                        to_iso(moment),
                        existing["fingerprint_id"],
                    ),
                )
        return Capture(
            incident_id=incident.incident_id,
            severity=route.severity,
            playbook=route.playbook,
            category=route.category,
            is_new_incident=True,
            occurrences=1,
            dedupe_key=dedupe_key,
            outbox_event_id=event.outbox_event_id,
            personal_data_involved=involves_people,
        )

    # -- reads ------------------------------------------------------------- #
    def occurrences(self, dedupe_key: str) -> int:
        """How many times this failure has been seen inside the current window."""
        row = self._fingerprint(require_ref(dedupe_key, "dedupe_key", max_len=64))
        return 0 if row is None else int(row["occurrences"])

    def fingerprint_for(
        self, *, category: str, code: str, source: str, aggregate_id: Optional[str] = None
    ) -> str:
        """The key this engine would use for a failure — public so a caller can look it up."""
        route = FAILURE_ROUTES.get(str(category).strip().upper())
        if route is None:
            raise UnknownFailureCategory(
                "this failure has no route: severity would have to be guessed",
                category=repr(category),
                known=list(ROUTE_CATEGORIES),
            )
        return sha256_hex(
            canonical_json(
                {
                    "agency_id": self.agency_id,
                    "category": route.category,
                    "code": require_text(code, "code", max_len=64),
                    "source": require_text(source, "source", max_len=64),
                    "aggregate_id": (
                        require_ref(aggregate_id, "aggregate_id", max_len=128) if aggregate_id else "-"
                    ),
                }
            )
        )

    # -- internals --------------------------------------------------------- #
    def _fingerprint(self, dedupe_key: str) -> Optional[Mapping[str, Any]]:
        row = self._conn.execute(
            "SELECT fingerprint_id, dedupe_key, occurrences, incident_id, last_seen_at, severity"
            " FROM exception_fingerprints WHERE agency_id = ? AND dedupe_key = ?",
            (self.agency_id, dedupe_key),
        ).fetchone()
        return None if row is None else dict(row)

    def _within_window(self, existing: Mapping[str, Any], moment: datetime) -> bool:
        """True while the same failure counts as a repeat.

        The boundary is *inclusive*, matching the precedent in ``incidents.py`` ("overdue is
        strict: at exactly the deadline it has not passed yet"): at exactly last_seen + window
        the failure is still the same obligation; a minute later it is a new one.
        """
        return parse_iso(existing["last_seen_at"]) + timedelta(
            minutes=self.dedupe_window_minutes
        ) >= moment

    def _count_repeat(
        self,
        existing: Mapping[str, Any],
        route: FailureRoute,
        moment: datetime,
        message: str,
        involves_people: bool,
    ) -> Capture:
        """The same failure inside the window: count it against the open incident."""
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE exception_fingerprints SET occurrences = occurrences + 1, last_seen_at = ?"
                " WHERE fingerprint_id = ?",
                (to_iso(moment), existing["fingerprint_id"]),
            )
        occurrences = int(existing["occurrences"]) + 1
        incident_id = str(existing["incident_id"])
        event = self.outbox.emit(
            event_type=CAPTURE_EVENT_TYPE,
            aggregate_type="incident",
            aggregate_id=incident_id,
            payload={
                "category": route.category,
                "severity": route.severity,
                "message": message,
                "occurrences": occurrences,
                "repeat": True,
            },
            actor="system:exception_engine",
            idempotency_key=f"incident-{incident_id}-repeat-{occurrences}",
            correlation_id=incident_id,
            at=moment,
        )
        return Capture(
            incident_id=incident_id,
            severity=str(existing["severity"]),
            playbook=route.playbook,
            category=route.category,
            is_new_incident=False,
            occurrences=occurrences,
            dedupe_key=str(existing["dedupe_key"]),
            outbox_event_id=event.outbox_event_id,
            personal_data_involved=involves_people,
        )

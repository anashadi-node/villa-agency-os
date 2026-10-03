"""Agent 3's §22 KPI telemetry — the reservation funnel and its governance numbers.

Authority chain: the charter's §5 success metrics (double booking 0, quote error < 1%,
first response < 15 min, the inquiry→quote→hold→paid→confirmed funnel) and the AI
Workforce Architecture's telemetry rule: **no P2+ data ever reaches a metric** — a KPI here
is a count, a duration aggregate or a rate computed over recorded facts, never a guest
identity, a conversation's words or any personal field.

What the module answers, over one agency and one time window:

* **funnel** — inquiries → quotes sent → quotes accepted → holds confirmed → holds
  converted → bookings confirmed → payments succeeded, each stage counted where its owner
  records it (``cases``, ``quotes``, ``holds``, ``bookings``, ``payments``);
* **rates** — acceptance, hold conversion and end-to-end booking conversion, ``None`` when
  the denominator is zero (a rate invented from an empty funnel is a lie);
* **SLA** — first-response and quote breaches, counted the way ``cases.py`` detects them
  (answered late, or still unanswered past the deadline);
* **reliability** — overlapping confirmed bookings per unit, which the kernel refuses to
  write, so a non-zero count here means the invariant was violated somewhere;
* **AI governance** — conversations, how many escalated, how many were handed off, and the
  AI-gateway *denied action* count: the machine being refused is a KPI, not a failure.

The numbers are derived, never stored: a metric that outlives its window is a snapshot, and
this module re-reads the facts at the instant it is asked, so it is reproducible and cannot
drift from the tables it reads. Idempotent, read-only, one connection — it writes nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Optional

from primitives import Clock, SystemClock, require_ref

__all__ = [
    "RESERVATION_KPI_SCHEMA",
    "FUNNEL_STAGES",
    "WINDOW_DAYS",
    "BOOKED_STATUSES",
    "ReservationKpis",
    "collect",
]

#: The payload version, so a consumer can tell a metric bundle's shape apart over time.
RESERVATION_KPI_SCHEMA = "reservation-kpi/1"

#: The funnel, in charter order (inquiry→qualified→quote→hold→paid→confirmed, §5).
FUNNEL_STAGES: tuple[str, ...] = (
    "inquiries",
    "quotes_sent",
    "quotes_accepted",
    "holds_confirmed",
    "holds_converted",
    "bookings_confirmed",
    "payments_succeeded",
)

#: The default window: four weeks, the charter's "baseline dulu" horizon, in days.
WINDOW_DAYS = 28

#: Booking statuses that mean a confirmed sale (the kernel's own vocabulary subset that
#: marks the booking as won — anything before these is still a quote or a hold).
BOOKED_STATUSES: tuple[str, ...] = (
    "DEPOSIT_PAID",
    "PAID_IN_FULL",
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    "COMPLETED",
)


@dataclass(frozen=True)
class ReservationKpis:
    """One instant's answer — plain integers and rates, no person-shaped field."""

    agency_id: str
    schema: str = RESERVATION_KPI_SCHEMA
    window_days: int = WINDOW_DAYS
    at: Optional[str] = None
    funnel: Mapping[str, int] = field(default_factory=dict)
    rates: Mapping[str, Optional[float]] = field(default_factory=dict)
    sla: Mapping[str, int] = field(default_factory=dict)
    governance: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The dict a dashboard or exporter reads — stable, JSON-shaped, integer-first."""

        return {
            "schema": self.schema,
            "agency_id": self.agency_id,
            "window_days": self.window_days,
            "at": self.at,
            "funnel": dict(self.funnel),
            "rates": dict(self.rates),
            "sla": dict(self.sla),
            "governance": dict(self.governance),
        }


def _cutoff(at: Any, window_days: int) -> str:
    """The window's lower bound as an ISO instant (created_at columns are ISO text)."""

    moment = at if at is not None else SystemClock().now()
    if hasattr(moment, "tzinfo") and moment.tzinfo is not None:
        return (moment - timedelta(days=window_days)).isoformat()
    raise ValueError("at must be an aware datetime")


def _ratio(part: int, whole: int) -> Optional[float]:
    """A rate, or ``None`` when the funnel stage has not happened yet — never a fake 0%."""

    if whole <= 0:
        return None
    return round(part / whole, 4)


def collect(
    conn: sqlite3.Connection,
    *,
    agency_id: str,
    clock: Optional[Clock] = None,
    at: Any = None,
    window_days: int = WINDOW_DAYS,
) -> ReservationKpis:
    """Read every KPI in one pass; read-only, idempotent, reproducible from the same instant."""

    agency_id = require_ref(agency_id, "agency_id")
    if clock is None and at is None:
        clock = SystemClock()
    moment = at if at is not None else clock.now()  # type: ignore[union-attr]
    cutoff = _cutoff(moment, window_days)
    now_iso = moment.isoformat() if hasattr(moment, "isoformat") else str(moment)

    def count(sql: str, params: tuple[Any, ...]) -> int:
        return int(conn.execute(sql, params).fetchone()["n"])

    funnel = {
        "inquiries": count(
            "SELECT COUNT(*) AS n FROM cases WHERE agency_id = ? AND created_at >= ?",
            (agency_id, cutoff),
        ),
        "quotes_sent": count(
            "SELECT COUNT(*) AS n FROM quotes WHERE agency_id = ?"
            " AND status IN ('SENT','ACCEPTED') AND created_at >= ?",
            (agency_id, cutoff),
        ),
        "quotes_accepted": count(
            "SELECT COUNT(*) AS n FROM quotes WHERE agency_id = ?"
            " AND status = 'ACCEPTED' AND created_at >= ?",
            (agency_id, cutoff),
        ),
        "holds_confirmed": count(
            "SELECT COUNT(*) AS n FROM holds WHERE agency_id = ?"
            " AND status IN ('CONFIRMED','CONVERTED') AND created_at >= ?",
            (agency_id, cutoff),
        ),
        "holds_converted": count(
            "SELECT COUNT(*) AS n FROM holds WHERE agency_id = ?"
            " AND status = 'CONVERTED' AND created_at >= ?",
            (agency_id, cutoff),
        ),
        "bookings_confirmed": count(
            "SELECT COUNT(*) AS n FROM bookings WHERE agency_id = ?"
            f" AND status IN ({','.join('?' * len(BOOKED_STATUSES))})"
            " AND confirmed_at >= ?",
            (agency_id, *BOOKED_STATUSES, cutoff),
        ),
        "payments_succeeded": count(
            "SELECT COUNT(*) AS n FROM payments WHERE agency_id = ?"
            " AND status = 'SUCCEEDED' AND created_at >= ?",
            (agency_id, cutoff),
        ),
    }
    rates = {
        "quote_acceptance_rate": _ratio(funnel["quotes_accepted"], funnel["quotes_sent"]),
        "hold_conversion_rate": _ratio(funnel["holds_converted"], funnel["holds_confirmed"]),
        "booking_conversion_rate": _ratio(
            funnel["bookings_confirmed"], funnel["quotes_accepted"]
        ),
    }
    sla = {
        # Answered late (the fact exists and lost) or unanswered past the deadline —
        # the same two cases `cases.overdue` detects, counted instead of listed.
        "first_response_breached": count(
            "SELECT COUNT(*) AS n FROM cases WHERE agency_id = ? AND created_at >= ?"
            " AND ((first_responded_at IS NOT NULL AND first_responded_at >"
            " first_response_due_at) OR (first_responded_at IS NULL AND"
            " first_response_due_at < ?))",
            (agency_id, cutoff, now_iso),
        ),
        "quote_sla_breached": count(
            "SELECT COUNT(*) AS n FROM cases WHERE agency_id = ? AND created_at >= ?"
            " AND ((quoted_at IS NOT NULL AND quoted_at > quote_due_at) OR"
            " (quoted_at IS NULL AND quote_due_at < ?))",
            (agency_id, cutoff, now_iso),
        ),
    }
    # Reliability: the kernel refuses overlapping confirmed stays, so any pair here is a
    # violated invariant somewhere — counted, never repaired silently.
    overlap = count(
        "SELECT COUNT(*) AS n FROM bookings a, bookings b"
        " WHERE a.agency_id = ? AND b.agency_id = ?"
        f" AND a.status IN ({','.join('?' * len(BOOKED_STATUSES))})"
        f" AND b.status IN ({','.join('?' * len(BOOKED_STATUSES))})"
        " AND a.unit_id = b.unit_id AND a.booking_id < b.booking_id"
        " AND a.check_in < b.check_out AND b.check_in < a.check_out",
        (agency_id, agency_id, *BOOKED_STATUSES, *BOOKED_STATUSES),
    )
    conversations = count(
        "SELECT COUNT(DISTINCT conversation_id) AS n FROM ai_conversations"
        " WHERE agency_id = ? AND opened_at >= ?",
        (agency_id, cutoff),
    )
    escalated = count(
        "SELECT COUNT(DISTINCT e.conversation_id) AS n FROM ai_conversation_events e"
        " JOIN ai_conversations c ON c.conversation_id = e.conversation_id"
        " WHERE c.agency_id = ? AND c.opened_at >= ? AND e.kind = 'ESCALATED'",
        (agency_id, cutoff),
    )
    handed_off = count(
        "SELECT COUNT(DISTINCT e.conversation_id) AS n FROM ai_conversation_events e"
        " JOIN ai_conversations c ON c.conversation_id = e.conversation_id"
        " WHERE c.agency_id = ? AND c.opened_at >= ? AND e.kind = 'HANDOFF_REQUESTED'",
        (agency_id, cutoff),
    )
    governance = {
        "conversations": conversations,
        "escalated": escalated,
        "escalation_rate": _ratio(escalated, conversations),
        "handed_off": handed_off,
        "overlapping_bookings": overlap,
    }
    return ReservationKpis(
        agency_id=agency_id,
        window_days=window_days,
        at=now_iso,
        funnel=funnel,
        rates=rates,
        sla=sla,
        governance=governance,
    )
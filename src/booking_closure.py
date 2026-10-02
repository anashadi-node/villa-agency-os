"""§30's booking closure gate — the blocker list, computed from the modules that own the facts.

§30 of the specification:

    ``BOOKING_CLOSED`` hanya bila: guest checked out · open guest issues = 0 · financial
    reconciliation complete · supplier reconciliation complete · security deposit resolved ·
    incidents closed · required documents complete.
    Jika tidak, gunakan ``POST_STAY_PENDING`` dan tampilkan blocker list.

This module is that **list**. It reads; it never writes, and it never assumes. Each check reads the
fact where its owner records it:

============================  ==================================================
check                         owner of the fact
============================  ==================================================
``guest_checked_out``         ``bookings.status`` (the kernel writes it)
``open_guest_issues``         ``cases`` (an open case is an open guest matter)
                              and ``ops_tasks`` of kind ``GUEST_FOLLOWUP``
``financial_reconciliation``  ``invoices`` + ``payments`` (``SUCCEEDED``), with
                              an ``OPEN`` ``reconciliation_cases`` row blocking
``supplier_reconciliation``   **no owner** — recorded gap
``security_deposit_resolved`` **no owner** — recorded gap
``incidents_closed``          ``incidents`` (attribution by ``affected_refs``)
``required_documents``        **no declared list** — recorded gap
============================  ==================================================

Where no owner exists the check fails **closed** and says so by name: "the platform cannot see
this" is an unfinished booking, never a closed one. That is the whole point of a closure gate — a
booking closed on an unverifiable fact cannot be reopened honestly.

A verdict is data (``ClosureVerdict``), not an exception: the caller decides what to do with the
blocker list, and the kernel's ``close_booking`` refuses with it (§30 asks the platform to *show*
the list, which a bare refusal could not).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from primitives import DomainError, require_ref

__all__ = [
    "CHECK_CHECKED_OUT",
    "CHECK_GUEST_ISSUES",
    "CHECK_FINANCIAL",
    "CHECK_SUPPLIER",
    "CHECK_DEPOSIT",
    "CHECK_INCIDENTS",
    "CHECK_DOCUMENTS",
    "CHECKS",
    "CLOSED_BOOKING_STATES",
    "OPEN_TASK_STATUSES",
    "ClosureCheck",
    "ClosureVerdict",
    "BookingNotFound",
    "evaluate",
]

#: The check names, in §30's own order, so a blocker list reads like the spec it came from.
CHECK_CHECKED_OUT = "guest_checked_out"
CHECK_GUEST_ISSUES = "open_guest_issues"
CHECK_FINANCIAL = "financial_reconciliation"
CHECK_SUPPLIER = "supplier_reconciliation"
CHECK_DEPOSIT = "security_deposit_resolved"
CHECK_INCIDENTS = "incidents_closed"
CHECK_DOCUMENTS = "required_documents"

CHECKS: tuple[str, ...] = (
    CHECK_CHECKED_OUT,
    CHECK_GUEST_ISSUES,
    CHECK_FINANCIAL,
    CHECK_SUPPLIER,
    CHECK_DEPOSIT,
    CHECK_INCIDENTS,
    CHECK_DOCUMENTS,
)

#: The status a booking must be in before §30 is even asked: the guest has left. ``CLOSED`` is
#: included so evaluating an already-closed booking is not a failure (closing twice is a no-op).
CLOSED_BOOKING_STATES: tuple[str, ...] = ("COMPLETED", "CLOSED")

#: ``ops_tasks`` statuses that still count as live work (the queue's ``TASK_STATUSES`` minus the
#: two terminal ones).
OPEN_TASK_STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "BLOCKED")

#: Incidents that are still open; ``RESOLVED`` and ``POSTMORTEM_DONE`` are the closed ends of
#: ``incidents.status``.
OPEN_INCIDENT_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING")

#: The gap, said once so the blocker list can carry it: ``supplier_route`` owns contracts, rates and
#: verification of the villa relationship — not the money that settles between agency and supplier.
SUPPLIER_GAP = (
    "no module records the supplier settlement: supplier_route owns contracts and rates, not the "
    "money that settles between agency and supplier"
)

#: The deposit's resolution has no record either: the booking stores the amount held, and nothing
#: stores whether it was returned, forfeited or applied.
DEPOSIT_GAP = (
    "no module records the security deposit's resolution: the booking stores the amount held, not "
    "whether it was returned, forfeited or applied"
)

#: Which document kinds a closed booking needs is tenant/policy data and no policy key declares it,
#: so the check cannot pass on its own — the recorded kinds are reported instead.
DOCUMENTS_GAP = "no policy declares which document kinds a closed booking requires"


class ClosureError(DomainError):
    """Base class for refusals this module makes."""

    code = "BOOKING_CLOSURE_ERROR"


class BookingNotFound(ClosureError):
    """The booking the gate was asked about is not recorded. Refused, never guessed."""

    code = "BOOKING_NOT_FOUND"


@dataclass(frozen=True)
class ClosureCheck:
    """One §30 condition: what was asked, what was seen, and where it was seen."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ClosureVerdict:
    """§30's answer for one booking: ready to close, or exactly what is blocking it."""

    booking_id: str
    status: str
    ready: bool
    checks: tuple[ClosureCheck, ...]
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready: a log line, a UI and a refusal's details need the same shape."""

        return {
            "booking_id": self.booking_id,
            "status": self.status,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
            "blockers": list(self.blockers),
        }


def _check(name: str, ok: bool, detail: str) -> ClosureCheck:
    return ClosureCheck(name=name, ok=bool(ok), detail=detail)


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _checked_out(conn: sqlite3.Connection, booking: Any) -> ClosureCheck:
    """§30's first line: the guest has left. The booking row is the only record of that."""

    status = str(booking["status"])
    if status in CLOSED_BOOKING_STATES:
        return _check(CHECK_CHECKED_OUT, True, f"the booking is {status}")
    return _check(
        CHECK_CHECKED_OUT,
        False,
        f"the booking is {status}: §30 asks after the stay, not during it",
    )


def _guest_issues(
    conn: sqlite3.Connection, *, booking_id: str, agency_id: str
) -> ClosureCheck:
    """§30's "open guest issues = 0", read where the estate keeps guest matters.

    Two owners: an **open case** for the booking (the case is the matter), and a live
    ``GUEST_FOLLOWUP`` task (the work a person still owes the guest — the kind RES-W06 and RES-W09
    file). Other task kinds are the agency's own operations and do not block closure: a cleaning
    job is not a guest issue.
    """

    open_cases = _count(
        conn,
        "SELECT COUNT(*) FROM cases"
        " WHERE booking_id = ? AND agency_id = ? AND status NOT LIKE 'CLOSED_%'",
        (booking_id, agency_id),
    )
    open_tasks = _count(
        conn,
        "SELECT COUNT(*) FROM ops_tasks"
        " WHERE booking_id = ? AND agency_id = ? AND kind = 'GUEST_FOLLOWUP'"
        " AND status IN ('OPEN', 'IN_PROGRESS', 'BLOCKED')",
        (booking_id, agency_id),
    )
    return _check(
        CHECK_GUEST_ISSUES,
        open_cases == 0 and open_tasks == 0,
        f"open cases: {open_cases}; live guest follow-ups: {open_tasks}",
    )


def _financial(
    conn: sqlite3.Connection, *, booking_id: str, agency_id: str, total_minor: int
) -> ClosureCheck:
    """§30's financial reconciliation: every invoice for the booking is settled, and no
    reconciliation case is still open.

    "Settled" is the estate's own word for it — a payment whose ``status`` is ``SUCCEEDED`` (the
    schema ties ``settled_at`` to that status), summed against the invoice. An ``OPEN``
    reconciliation case means the money is *disputed*, and a disputed payment is not reconciled.
    """

    invoice = conn.execute(
        "SELECT invoice_id, total_minor FROM invoices"
        " WHERE booking_id = ? AND agency_id = ? AND status = 'ISSUED'"
        " ORDER BY issued_at DESC LIMIT 1",
        (booking_id, agency_id),
    ).fetchone()
    if invoice is None:
        if total_minor == 0:
            return _check(
                CHECK_FINANCIAL, True, "no invoice is needed: the booking totals zero"
            )
        return _check(
            CHECK_FINANCIAL, False, "no issued invoice covers this booking"
        )
    invoice_id = str(invoice["invoice_id"])
    required = int(invoice["total_minor"])
    settled = _count(
        conn,
        "SELECT COALESCE(SUM(amount_minor), 0) FROM payments"
        " WHERE invoice_id = ? AND agency_id = ? AND status = 'SUCCEEDED'",
        (invoice_id, agency_id),
    )
    open_recons = _count(
        conn,
        "SELECT COUNT(*) FROM reconciliation_cases"
        " WHERE invoice_id = ? AND agency_id = ? AND status = 'OPEN'",
        (invoice_id, agency_id),
    )
    ok = settled >= required and open_recons == 0
    return _check(
        CHECK_FINANCIAL,
        ok,
        f"invoice {invoice_id}: settled {settled} of {required} minor;"
        f" open reconciliation cases: {open_recons}",
    )


def _supplier(conn: sqlite3.Connection, *, booking_id: str, agency_id: str) -> ClosureCheck:
    """§30's supplier reconciliation — **no owner yet**, so the check fails closed and says why.

    ``supplier_route`` verifies contracts, rates and the villa relationship; nothing records the
    settlement between agency and supplier. A booking may not be closed on a fact no module holds.
    """

    return _check(CHECK_SUPPLIER, False, SUPPLIER_GAP)


def _deposit(conn: sqlite3.Connection, booking: Any) -> ClosureCheck:
    """§30's security deposit: nothing to resolve when none is held, otherwise a recorded gap."""

    held = int(booking["security_deposit_minor"])
    if held == 0:
        return _check(CHECK_DEPOSIT, True, "no security deposit is held")
    return _check(CHECK_DEPOSIT, False, f"{held} minor held. {DEPOSIT_GAP}")


def _incidents(
    conn: sqlite3.Connection, *, booking_id: str, agency_id: str
) -> ClosureCheck:
    """§30's incidents: an open incident that *names this booking* blocks closure.

    Incidents carry no ``booking_id`` column — they carry ``affected_refs``, a JSON list of the
    entities involved — so attribution is by reference, and a reference the platform cannot parse
    cannot be attributed to this booking.
    """

    rows = conn.execute(
        "SELECT incident_id, affected_refs FROM incidents"
        " WHERE agency_id = ? AND status IN ('OPEN', 'MITIGATING')",
        (agency_id,),
    ).fetchall()
    hits: list[str] = []
    for row in rows:
        raw = row["affected_refs"]
        try:
            refs = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(refs, (list, tuple)):
            continue
        if booking_id in {str(ref) for ref in refs}:
            hits.append(str(row["incident_id"]))
    return _check(
        CHECK_INCIDENTS,
        not hits,
        "no open incident names this booking"
        if not hits
        else f"open incidents naming this booking: {', '.join(sorted(hits))}",
    )


def _documents(
    conn: sqlite3.Connection, *, booking_id: str, agency_id: str
) -> ClosureCheck:
    """§30's required documents: the *required* list is tenant data and no policy declares it."""

    kinds = sorted(
        {
            str(row["kind"])
            for row in conn.execute(
                "SELECT DISTINCT kind FROM documents"
                " WHERE booking_id = ? AND agency_id = ? AND status = 'GENERATED'",
                (booking_id, agency_id),
            ).fetchall()
        }
    )
    recorded = ", ".join(kinds) if kinds else "none"
    return _check(
        CHECK_DOCUMENTS,
        False,
        f"{DOCUMENTS_GAP}; documents recorded for this booking: {recorded}",
    )


def evaluate(
    conn: sqlite3.Connection, *, booking_id: str, agency_id: str
) -> ClosureVerdict:
    """The §30 gate for one booking: every check, and the blocker list when one fails.

    ``booking_id`` is validated as a reference and must exist: the gate answers about a recorded
    booking or refuses. The verdict is returned even when the booking is only partially through its
    life — the caller reads ``ready`` — because §30 wants the *list*, not a bare "no".
    """

    booking_id = require_ref(booking_id, "booking_id")
    booking = conn.execute(
        "SELECT * FROM bookings WHERE booking_id = ? AND agency_id = ?",
        (booking_id, agency_id),
    ).fetchone()
    if booking is None:
        raise BookingNotFound(
            f"unknown booking: {booking_id}", booking_id=booking_id, agency_id=agency_id
        )
    checks = (
        _checked_out(conn, booking),
        _guest_issues(conn, booking_id=booking_id, agency_id=agency_id),
        _financial(
            conn,
            booking_id=booking_id,
            agency_id=agency_id,
            total_minor=int(booking["total_minor"]),
        ),
        _supplier(conn, booking_id=booking_id, agency_id=agency_id),
        _deposit(conn, booking),
        _incidents(conn, booking_id=booking_id, agency_id=agency_id),
        _documents(conn, booking_id=booking_id, agency_id=agency_id),
    )
    blockers = tuple(check.name for check in checks if not check.ok)
    return ClosureVerdict(
        booking_id=booking_id,
        status=str(booking["status"]),
        ready=not blockers,
        checks=checks,
        blockers=blockers,
    )

"""§30's security-deposit resolution — the fact that answers "what happened to the hold".

Authority chain: ``src/schema/001_initial.sql`` + ``013_deposit_resolutions.sql`` own the
storage contract, this module is the **single writer** of ``deposit_resolutions``, and
``booking_closure._deposit`` is the §30 reader that judges the recorded facts. The booking
row keeps the *amount held*; this table keeps what became of it: **returned**, **forfeited**
or **applied** — decided by a person, carried by evidence, and append-only.

The rules, stated once:

* **Human authority.** A deposit's outcome is money decided in the tenant's name, so a
  machine actor is refused at the writer — the same predicate :mod:`supplier_route`
  applies to its own human steps, shared rather than copied.
* **One currency, one booking.** Every resolution is recorded in the booking's own
  currency against the booking's own id, in the tenant's scope. A foreign booking id is
  *not found*, not forbidden.
* **No over-resolution.** The sum of every resolution recorded for a booking may never
  exceed the amount the booking holds. A contradiction between evidence and ledger is a
  refusal at the writer, and the §30 gate judges the same arithmetic again on read —
  the writer's refusal is a courtesy; the gate's verdict is the contract.
* **Idempotent.** The same ``idempotency_key`` is the same decision: a retry returns the
  recorded row unchanged instead of a second fact. Different decisions get different keys
  — a part-return and the remaining forfeiture are two rows, and the gate reads the sum.
* **Append-only.** UPDATE and DELETE are refused by trigger. A resolution that can be
  edited is a resolution nobody has to prove.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional, Sequence

from audit import append_event
from primitives import Clock, DomainError, IdFactory, SystemClock, require_ref, require_text
from supplier_route import is_machine_actor

__all__ = [
    "DEPOSIT_RESOLUTION_TABLE",
    "ACTIONS",
    "DepositResolutionError",
    "DepositBookingNotFound",
    "DepositActionUnknown",
    "DepositHumanRequired",
    "DepositOverResolved",
    "DepositResolutionService",
]

DEPOSIT_RESOLUTION_TABLE = "deposit_resolutions"

#: The closed vocabulary §30's check reads. An unknown action is refused, not stored:
#: a free-text outcome is not a fact anyone can reconcile.
ACTIONS: tuple[str, ...] = ("RETURNED", "FORFEITED", "APPLIED")


class DepositResolutionError(DomainError):
    """Base class for deliberate refusals this module makes."""

    code = "DEPOSIT_RESOLUTION_ERROR"


class DepositBookingNotFound(DepositResolutionError):
    """The booking does not exist in this tenant — a foreign id is not found, not forbidden."""

    code = "DEPOSIT_BOOKING_NOT_FOUND"


class DepositActionUnknown(DepositResolutionError):
    """The action is not in the closed §30 vocabulary."""

    code = "DEPOSIT_ACTION_UNKNOWN"


class DepositHumanRequired(DepositResolutionError):
    """A deposit's outcome is a person's decision; a machine actor is refused."""

    code = "DEPOSIT_HUMAN_REQUIRED"


class DepositOverResolved(DepositResolutionError):
    """The resolutions recorded would exceed the amount the booking actually holds."""

    code = "DEPOSIT_OVER_RESOLVED"


class DepositResolutionService:
    """The single writer of ``deposit_resolutions`` for one agency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        agency_id: str = "agency_default",
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")

    # -- reads ------------------------------------------------------------- #

    def for_booking(self, booking_id: str) -> tuple[Mapping[str, Any], ...]:
        """Every resolution recorded for one booking in this tenant, oldest first."""

        booking_id = require_ref(booking_id, "booking_id")
        rows = self.conn.execute(
            "SELECT * FROM deposit_resolutions"
            " WHERE booking_id = ? AND agency_id = ? ORDER BY created_at, deposit_resolution_id",
            (booking_id, self.agency_id),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def totals(self, booking_id: str) -> dict[str, int]:
        """The sum recorded per action — exactly what the §30 gate reconciles."""

        rows = self.conn.execute(
            "SELECT action, SUM(amount_minor) AS total FROM deposit_resolutions"
            " WHERE booking_id = ? AND agency_id = ? GROUP BY action ORDER BY action",
            (booking_id, self.agency_id),
        ).fetchall()
        return {str(row["action"]): int(row["total"]) for row in rows}

    def _booking(self, booking_id: str) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT booking_id, agency_id, currency, security_deposit_minor"
            " FROM bookings WHERE booking_id = ? AND agency_id = ?",
            (booking_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DepositBookingNotFound(
                f"unknown booking: {booking_id}",
                booking_id=booking_id,
                agency_id=self.agency_id,
            )
        return row

    # -- the one write ----------------------------------------------------- #

    def record(
        self,
        *,
        booking_id: str,
        action: str,
        amount_minor: int,
        currency: str,
        decided_by: str,
        actor_role: str,
        evidence_ref: str,
        idempotency_key: str,
        note: Optional[str] = None,
        at: Any = None,
    ) -> Mapping[str, Any]:
        """Record one part of the deposit's outcome — the person's decision, with evidence."""

        booking_id = require_ref(booking_id, "booking_id")
        action = require_text(action, "action", max_len=16).upper()
        if action not in ACTIONS:
            raise DepositActionUnknown(
                f"unknown deposit action: {action}",
                action=action,
                allowed=list(ACTIONS),
            )
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise DepositResolutionError(
                "deposit resolution amount must be a positive integer in minor units",
                code="DEPOSIT_AMOUNT_INVALID",
                amount_minor=amount_minor,
            )
        currency = require_text(currency, "currency", max_len=3).upper()
        # An evidence ref is an opaque token (the same contract the audit chain enforces):
        # it names where the proof lives, it is not prose.
        evidence_ref = require_ref(evidence_ref, "evidence_ref")
        idempotency_key = require_text(idempotency_key, "idempotency_key", max_len=128)
        if len(idempotency_key) < 6:
            raise DepositResolutionError(
                "idempotency_key must be a value a retry can repeat",
                code="DEPOSIT_IDEMPOTENCY_REQUIRED",
            )

        # Money decided in the tenant's name is a person's act: a machine principal is
        # refused here, before anything is written (§18/§21/§56 — shared predicate).
        if is_machine_actor(decided_by, actor_role):
            raise DepositHumanRequired(
                "a security deposit's resolution is decided by a person",
                decided_by=decided_by,
                actor_role=actor_role,
            )

        existing = self.conn.execute(
            "SELECT * FROM deposit_resolutions WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            # Same key, same decision — return the recorded fact unchanged.
            return dict(existing)

        booking = self._booking(booking_id)
        if currency != str(booking["currency"]).upper():
            raise DepositResolutionError(
                "the deposit's resolution is recorded in the booking's own currency",
                code="DEPOSIT_CURRENCY_MISMATCH",
                booking_currency=str(booking["currency"]),
                currency=currency,
            )
        held = int(booking["security_deposit_minor"])
        already = sum(self.totals(booking_id).values())
        if already + amount_minor > held:
            raise DepositOverResolved(
                f"resolutions would exceed the {held} minor the booking holds",
                held_minor=held,
                already_minor=already,
                amount_minor=amount_minor,
            )

        moment = self.clock.now() if at is None else at
        created_at = moment if isinstance(moment, str) else moment.isoformat()
        resolution_id = self.ids.new("depres")
        with self.conn:
            self.conn.execute(
                "INSERT INTO deposit_resolutions (deposit_resolution_id, agency_id, booking_id,"
                " action, amount_minor, currency, decided_by, actor_role, evidence_ref, note,"
                " idempotency_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resolution_id,
                    self.agency_id,
                    booking_id,
                    action,
                    amount_minor,
                    currency,
                    decided_by,
                    require_text(actor_role, "actor_role", max_len=64),
                    evidence_ref,
                    note,
                    idempotency_key,
                    created_at,
                ),
            )
            append_event(
                self.conn,
                clock=self.clock,
                ids=self.ids,
                action="deposit.resolved",
                entity_type="deposit_resolution",
                entity_id=resolution_id,
                actor=decided_by,
                actor_role=actor_role,
                after={
                    "booking_id": booking_id,
                    "action": action,
                    "amount_minor": amount_minor,
                    "currency": currency,
                },
                evidence_ref=evidence_ref,
            )
        return self._row(resolution_id)

    def _row(self, resolution_id: str) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM deposit_resolutions WHERE deposit_resolution_id = ?",
            (resolution_id,),
        ).fetchone()
        return dict(row)


def satisfied(held_minor: int, recorded: Sequence[Mapping[str, Any]]) -> bool:
    """The arithmetic the §30 gate re-checks: the parts recorded sum to the amount held."""

    return sum(int(row["amount_minor"]) for row in recorded) == held_minor
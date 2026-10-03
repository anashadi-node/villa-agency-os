"""§30's supplier settlement — what the agency owes, and has paid, the supplier.

Authority chain: ``src/schema/001_initial.sql`` + ``014_supplier_settlements.sql`` own the
storage contract, this module is the **single writer** of ``supplier_settlements``, and
``booking_closure._supplier`` is the §30 reader that judges the recorded facts. The agency
holds the guest's money; the supplier (villa owner) is owed their share under the route's
contract. Before this table the gate could only fail closed by name ("supplier_route owns
contracts and rates, not the money"); the row is that owner.

The rules, stated once:

* **Two halves, one table.** ``DUE`` declares what the supplier is owed for this booking —
  the contract's consequence, decided by a person with the contract/SOP named as evidence.
  ``PAID`` records the parts that settled it. The gate passes only when the parts paid sum
  to exactly what was declared due; anything else fails closed by name.
* **Human authority** and **no over-payment**, exactly like :mod:`deposit_resolutions` —
  the same shared predicate refuses a machine actor, and the writer refuses arithmetic
  that contradicts the ledger.
* **Provenance rides along.** Every row carries the booking's own ``route_id``
  (``villa_supplier_relationship_id``), so a settlement is always answerable to the route
  whose contract produced it.
* **Idempotent, append-only, evidence-carried** — same contract as the deposit: the same
  ``idempotency_key`` is the same decision, UPDATE/DELETE are refused by trigger, and the
  act lands in the audit chain with its evidence ref.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional

from audit import append_event
from primitives import Clock, DomainError, IdFactory, SystemClock, require_ref, require_text
from supplier_route import is_machine_actor

__all__ = [
    "SUPPLIER_SETTLEMENT_TABLE",
    "ACTIONS",
    "SupplierSettlementError",
    "SupplierBookingNotFound",
    "SupplierActionUnknown",
    "SupplierHumanRequired",
    "SupplierOverPaid",
    "SupplierSettlementService",
]

SUPPLIER_SETTLEMENT_TABLE = "supplier_settlements"

#: The closed vocabulary §30's check reads: what is owed, and what moved.
ACTIONS: tuple[str, ...] = ("DUE", "PAID")


class SupplierSettlementError(DomainError):
    """Base class for deliberate refusals this module makes."""

    code = "SUPPLIER_SETTLEMENT_ERROR"


class SupplierBookingNotFound(SupplierSettlementError):
    """The booking does not exist in this tenant — a foreign id is not found, not forbidden."""

    code = "SUPPLIER_BOOKING_NOT_FOUND"


class SupplierActionUnknown(SupplierSettlementError):
    """The action is not in the closed §30 vocabulary."""

    code = "SUPPLIER_ACTION_UNKNOWN"


class SupplierHumanRequired(SupplierSettlementError):
    """A settlement is a person's decision; a machine actor is refused."""

    code = "SUPPLIER_HUMAN_REQUIRED"


class SupplierOverPaid(SupplierSettlementError):
    """The parts paid would exceed what was declared due."""

    code = "SUPPLIER_OVER_PAID"


class SupplierSettlementService:
    """The single writer of ``supplier_settlements`` for one agency."""

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
        """Every settlement row recorded for one booking in this tenant, oldest first."""

        booking_id = require_ref(booking_id, "booking_id")
        rows = self.conn.execute(
            "SELECT * FROM supplier_settlements"
            " WHERE booking_id = ? AND agency_id = ? ORDER BY created_at, supplier_settlement_id",
            (booking_id, self.agency_id),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def totals(self, booking_id: str) -> dict[str, int]:
        """The sum recorded per action — exactly what the §30 gate reconciles."""

        rows = self.conn.execute(
            "SELECT action, SUM(amount_minor) AS total FROM supplier_settlements"
            " WHERE booking_id = ? AND agency_id = ? GROUP BY action ORDER BY action",
            (booking_id, self.agency_id),
        ).fetchall()
        return {str(row["action"]): int(row["total"]) for row in rows}

    def _booking(self, booking_id: str) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT booking_id, agency_id, currency, villa_supplier_relationship_id"
            " FROM bookings WHERE booking_id = ? AND agency_id = ?",
            (booking_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise SupplierBookingNotFound(
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
        """Record one half of the settlement — the person's decision, with evidence."""

        booking_id = require_ref(booking_id, "booking_id")
        action = require_text(action, "action", max_len=16).upper()
        if action not in ACTIONS:
            raise SupplierActionUnknown(
                f"unknown settlement action: {action}",
                action=action,
                allowed=list(ACTIONS),
            )
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise SupplierSettlementError(
                "settlement amount must be a positive integer in minor units",
                code="SUPPLIER_AMOUNT_INVALID",
                amount_minor=amount_minor,
            )
        currency = require_text(currency, "currency", max_len=3).upper()
        # An evidence ref is an opaque token (the same contract the audit chain enforces):
        # it names where the proof lives — the contract, the SOP, or the transfer receipt.
        evidence_ref = require_ref(evidence_ref, "evidence_ref")
        idempotency_key = require_text(idempotency_key, "idempotency_key", max_len=128)
        if len(idempotency_key) < 6:
            raise SupplierSettlementError(
                "idempotency_key must be a value a retry can repeat",
                code="SUPPLIER_IDEMPOTENCY_REQUIRED",
            )
        if is_machine_actor(decided_by, actor_role):
            raise SupplierHumanRequired(
                "a supplier settlement is decided by a person",
                decided_by=decided_by,
                actor_role=actor_role,
            )

        existing = self.conn.execute(
            "SELECT * FROM supplier_settlements WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            # Same key, same decision — return the recorded fact unchanged.
            return dict(existing)

        booking = self._booking(booking_id)
        if currency != str(booking["currency"]).upper():
            raise SupplierSettlementError(
                "the settlement is recorded in the booking's own currency",
                code="SUPPLIER_CURRENCY_MISMATCH",
                booking_currency=str(booking["currency"]),
                currency=currency,
            )
        if action == "PAID":
            due = self.totals(booking_id).get("DUE", 0)
            paid = self.totals(booking_id).get("PAID", 0)
            if paid + amount_minor > due:
                raise SupplierOverPaid(
                    f"payments would exceed the {due} minor declared due to the supplier",
                    due_minor=due,
                    paid_minor=paid,
                    amount_minor=amount_minor,
                )

        moment = self.clock.now() if at is None else at
        created_at = moment if isinstance(moment, str) else moment.isoformat()
        settlement_id = self.ids.new("supset")
        with self.conn:
            self.conn.execute(
                "INSERT INTO supplier_settlements (supplier_settlement_id, agency_id, booking_id,"
                " route_id, action, amount_minor, currency, decided_by, actor_role, evidence_ref,"
                " note, idempotency_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    settlement_id,
                    self.agency_id,
                    booking_id,
                    str(booking["villa_supplier_relationship_id"]),
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
                action="supplier.settled",
                entity_type="supplier_settlement",
                entity_id=settlement_id,
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
        return self._row(settlement_id)

    def _row(self, settlement_id: str) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM supplier_settlements WHERE supplier_settlement_id = ?",
            (settlement_id,),
        ).fetchone()
        return dict(row)
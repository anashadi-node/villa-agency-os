"""Drive a hold to ``CONFIRMED`` the only way the kernel allows (audit 2026-11-13, K2).

``confirm_from_hold`` used to jump from a hold straight to ``CONFIRMED``. It is
retired; ADR-0033's order is the only road now: reserve the booking, issue its
invoice, take a *verified* payment through ``PaymentService`` (signed webhook →
``SUCCEEDED``), let Finance mark it paid, then close it with the villa's block
proof. Tests that need a confirmed booking for something else (cancellation, the
stay, workers) go through this helper, so every one of them exercises the real gate
instead of a shortcut around it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

import invoicing as INVOICING
import payments as PAYMENTS

PROVIDER = "dummy"
SECRET = "booking-support-webhook-secret-0001"


def _sign(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def pay_booking(kernel: Any, booking_id: str, *, amount_minor: Optional[int] = None, key: str = "") -> str:
    """Issue one invoice for ``amount_minor`` (default: the booking total) and settle it
    with a verified webhook. A smaller amount is a deposit invoice: hosted intents are
    always raised for an invoice's full outstanding amount.

    Returns the ``payment_id`` of the ``SUCCEEDED`` payment.
    """
    row = kernel.conn.execute(
        "SELECT total_minor, currency, policy_fingerprint FROM bookings WHERE booking_id = ?",
        (booking_id,),
    ).fetchone()
    total = int(row["total_minor"])
    amount = total if amount_minor is None else amount_minor
    suffix = key or booking_id
    invoices = INVOICING.InvoiceService(
        kernel.conn, clock=kernel.clock, ids=kernel.ids, agency_id=kernel.agency_id
    )
    invoice = invoices.issue(
        booking_id=booking_id,
        currency=row["currency"],
        policy_fingerprint=row["policy_fingerprint"],
        lines=[{"kind": "NIGHTLY", "description": "Stay", "quantity": 1, "unit_amount_minor": amount}],
        idempotency_key=f"inv_{suffix}",
        actor="user:finance-1",
        actor_role="finance",
    )
    payments = PAYMENTS.PaymentService(
        kernel.conn, clock=kernel.clock, ids=kernel.ids, provider=PROVIDER, agency_id=kernel.agency_id
    )
    intent = payments.create_intent(
        invoice_id=invoice.invoice_id,
        amount_minor=amount,
        currency=row["currency"],
        idempotency_key=f"intent_{suffix}",
        created_by="user:finance-1",
    )
    body = json.dumps(
        {
            "provider_intent_id": str(intent.provider_intent_id),
            "invoice_id": invoice.invoice_id,
            "amount_minor": amount,
            "currency": row["currency"],
            "status": "succeeded",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    payments.handle_webhook(
        provider=PROVIDER,
        event_id=f"evt_{suffix}"[:60],
        event_type="payment.succeeded",
        payload_raw=body,
        signature=_sign(body),
        secret=SECRET,
    )
    return str(intent.payment_id)


def block_proof(kernel: Any, unit_id: str, *, level: str = "B") -> str:
    """The villa's written confirmation that the unit is blocked for the agency."""
    return kernel.record_evidence(
        unit_id=unit_id,
        source_type="manager_written",
        evidence_level=level,
        result="available",
        can_quote=True,
        can_hold=True,
        can_confirm=True,
        source_ref="villa_block_letter",
    )


def confirm_through_payment(
    kernel: Any,
    *,
    hold_id: str,
    quote_id: str,
    idempotency_key: str,
    command_id: str,
    adults: int,
    children: int = 0,
    infants: int = 0,
    actor: Optional[str] = None,
    actor_role: Optional[str] = None,
    **_ignored: Any,
) -> Any:
    """reserve_from_hold → invoice → verified payment → mark_paid → block proof → confirm."""
    reserved = kernel.reserve_from_hold(
        hold_id=hold_id,
        quote_id=quote_id,
        idempotency_key=idempotency_key,
        command_id=command_id,
        adults=adults,
        children=children,
        infants=infants,
        actor=actor,
        actor_role=actor_role,
    )
    if reserved.status == "CONFIRMED":
        return reserved
    payment_id = pay_booking(kernel, reserved.booking_id, key=idempotency_key)
    kernel.mark_paid(
        booking_id=reserved.booking_id, payment_id=payment_id, command_id=f"{command_id}_paid",
        actor="user:finance-1", actor_role="finance",
    )
    unit_id = kernel.get_booking(reserved.booking_id)["unit_id"]
    return kernel.confirm_booking(
        booking_id=reserved.booking_id,
        block_evidence_id=block_proof(kernel, unit_id),
        command_id=f"{command_id}_conf",
        actor=actor,
        actor_role=actor_role,
    )

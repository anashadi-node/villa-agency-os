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
from datetime import timedelta
from typing import Any, Optional

import identity as ID
import invoicing as INVOICING
import payments as PAYMENTS
import totp as T
from agency_roles import AgencyRoles

PROVIDER = "dummy"
SECRET = "booking-support-webhook-secret-0001"

#: The human who signs in for privileged kernel calls in tests. A real session is opened
#: against the real ``SessionService`` — no test-only bypass, no invented principal.
SIGNER_IDENTITY = "rm-1"
SIGNER_ROLE = "reservation_manager"
_SIGNER_SECRET: Optional[str] = None
_SPENT_STEPS: dict[str, int] = {}


def signer_token(kernel: Any, *, identity: str = SIGNER_IDENTITY) -> str:
    """A bound, MFA-satisfied, freshly stepped-up signer session for ``kernel``'s agency.

    G2-A: ``confirm_booking``, ``record_supplier_block_proof`` and
    ``revoke_supplier_block_proof`` need a signed-in signer regardless of the supplier
    assertion flag, so every test that drives them asks for one here rather than passing
    an actor string the kernel would have to believe on faith.
    """
    global _SIGNER_SECRET
    cached = getattr(kernel, "_g2_signer_tokens", None)
    if cached is None:
        cached = {}
        kernel._g2_signer_tokens = cached
    if identity in cached:
        token, minted = cached[identity]
        if (kernel.clock.now() - minted).total_seconds() < 900:
            return token
    conn = kernel.conn
    if _SIGNER_SECRET is None:
        _SIGNER_SECRET = T.generate_secret()
    roles = AgencyRoles(conn, kernel.clock, agency_id=kernel.agency_id)
    try:
        roles.seed_blueprint(actor="owner-1", actor_role="owner")
    except Exception:  # already seeded by the fixture that owns this database
        pass
    try:
        roles.assign_holder(SIGNER_ROLE, holder_ref=identity, actor="owner-1", actor_role="owner")
    except Exception:  # already appointed
        pass
    svc = ID.SessionService(
        conn,
        clock=kernel.clock,
        ids=kernel.ids,
        agency_id=kernel.agency_id,
        secrets=ID.MappingSecretResolver({f"{identity}-ref": _SIGNER_SECRET}),
    )
    at_moment = kernel.clock.now() - timedelta(seconds=120)
    at = ID.to_iso(at_moment)
    try:
        svc.enroll_totp(identity, secret_ref=f"{identity}-ref", at=at)
        svc.confirm_totp(identity, T.totp(_SIGNER_SECRET, at_moment), at=at)
    except Exception:  # a factor already enrolled by this database's own fixture
        pass
    step = int(kernel.clock.now().timestamp() // T.DEFAULT_STEP_SECONDS)
    if _SPENT_STEPS.get(identity, -1) >= step:
        kernel.clock.advance(seconds=T.DEFAULT_STEP_SECONDS)
        step += 1
    _SPENT_STEPS[identity] = step
    code = T.totp(_SIGNER_SECRET, kernel.clock.now())
    opened = svc.open_session(identity, SIGNER_ROLE, mfa_code=code)
    svc.satisfy_step_up(opened.session.session_id, code)
    cached[identity] = (opened.token, kernel.clock.now())
    return opened.token


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


def block_proof(
    kernel: Any, unit_id: str = "unit_1", *, level: str = "B", **evidence_options: Any
) -> str:
    """The villa's written confirmation that the unit is blocked for the agency.

    This returns an ``availability_evidence`` id — a *unit-level* answer. Since
    ADR-0073 that is no longer enough to confirm a booking on its own; see
    :func:`supplier_block_proof`, which binds such an answer to one booking.
    """
    return kernel.record_evidence(
        unit_id=unit_id,
        source_type="manager_written",
        evidence_level=level,
        result="available",
        can_quote=True,
        can_hold=True,
        can_confirm=True,
        source_ref="villa_block_letter",
        **evidence_options,
    )


def ensure_supplier(kernel: Any, *, unit_id: str, property_id: str) -> tuple[str, str]:
    """An ACTIVE supplier and an ACTIVE relationship covering ``unit_id``.

    Deterministic ids per unit, so a test that seeds twice does not trip the
    primary key. Returns ``(supplier_id, relationship_id)``.
    """
    supplier_id = f"sup_{unit_id}"
    relationship_id = f"vsr_{unit_id}"
    stamp = "2026-01-01T00:00:00+00:00"
    kernel.conn.execute(
        "INSERT OR IGNORE INTO suppliers (supplier_id, agency_id, name, status,"
        " created_at, updated_at) VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
        (supplier_id, kernel.agency_id, f"Villa manager for {unit_id}", stamp, stamp),
    )
    kernel.conn.execute(
        "INSERT OR IGNORE INTO villa_supplier_relationships"
        " (villa_supplier_relationship_id, agency_id, supplier_id, property_id, unit_id,"
        " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (relationship_id, kernel.agency_id, supplier_id, property_id, unit_id, stamp, stamp),
    )
    return supplier_id, relationship_id


def supplier_block_proof(
    kernel: Any,
    booking_id: str,
    *,
    level: str = "B",
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    evidence_id: Optional[str] = None,
    supplier_id: Optional[str] = None,
    relationship_id: Optional[str] = None,
    expires_at: Optional[str] = None,
    verification_method: str = "SUPPLIER_EMAIL",
    verified_by: Optional[str] = None,
    verifier_role: Optional[str] = None,
    actor: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    session_token: Optional[str] = None,
    **evidence_options: Any,
) -> str:
    """Record the booking-scoped proof ADR-0073 requires (one booking, one unit, one stay).

    ``evidence_id`` lets a caller reuse an existing availability answer; by default a
    fresh level-A/B ``AVAILABLE`` answer is recorded and bound to this booking.
    """
    booking = kernel.conn.execute(
        "SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)
    ).fetchone()
    if booking is None:  # a typo in a test should say so, not seed a supplier
        raise AssertionError(f"no booking {booking_id!r} to prove")
    default_supplier, default_relationship = ensure_supplier(
        kernel, unit_id=str(booking["unit_id"]), property_id=str(booking["property_id"])
    )
    evidence = evidence_id or kernel.record_evidence(
        unit_id=str(booking["unit_id"]),
        source_type="manager_written",
        evidence_level=level,
        result="available",
        can_quote=True,
        can_hold=True,
        can_confirm=True,
        source_ref="villa_block_letter",
        **evidence_options,
    )
    return kernel.record_supplier_block_proof(
        booking_id=booking_id,
        evidence_id=evidence,
        supplier_id=supplier_id or default_supplier,
        relationship_id=relationship_id or default_relationship,
        check_in=check_in or str(booking["check_in"]),
        check_out=check_out or str(booking["check_out"]),
        verification_method=verification_method,
        verified_by=verified_by or SIGNER_IDENTITY,
        actor=actor or SIGNER_IDENTITY,
        verifier_role=verifier_role or SIGNER_ROLE,
        idempotency_key=idempotency_key or f"sbp_{booking_id}_{evidence}",
        expires_at=expires_at,
        session_token=session_token or signer_token(kernel),
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
    return kernel.confirm_booking(
        booking_id=reserved.booking_id,
        block_proof_id=supplier_block_proof(kernel, reserved.booking_id),
        command_id=f"{command_id}_conf",
        # The closing actor is the signed-in signer (G2-A, AUTH-09): the kernel refuses a
        # claimed ``actor`` that the session does not prove, so the reserve-step claim is
        # deliberately not forwarded here.
        session_token=signer_token(kernel),
    )

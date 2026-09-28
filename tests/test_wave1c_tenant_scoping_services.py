"""Wave 1-C RED tests: tenant scope for finance/documents/cases services.

Master Spec §18/§39: backend/domain services enforce agency scope. A service from
agency B must see records from agency A as missing, not as accessible data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cases as C
import documents as D
import invoicing as INV
import payments as PAY
import primitives as P

NOW = "2026-09-21T08:00:00+00:00"
FINGERPRINT = "c" * 64


def service_clock() -> P.FrozenClock:
    return P.FrozenClock.at(NOW)


def ensure_agency(conn, agency_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO agencies (agency_id, name, status, created_at, updated_at)
        VALUES (?, ?, 'ACTIVE', ?, ?)
        """,
        (agency_id, agency_id, NOW, NOW),
    )


def seed_booking(conn, *, agency_id: str, booking_id: str) -> None:
    ensure_agency(conn, agency_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO properties (
            property_id, agency_id, name, property_type, region, province, timezone,
            status, contract_pack_status, created_at, updated_at
        ) VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar',
            'SELLABLE', 'ACTIVATED', ?, ?)
        """,
        (f"prop_{agency_id}", agency_id, f"Villa {agency_id}", NOW, NOW),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO units (
            unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,
            base_capacity, max_guests, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?)
        """,
        (f"unit_{agency_id}", agency_id, f"prop_{agency_id}", f"Unit {agency_id}", NOW, NOW),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO policy_versions (
            policy_version_id, agency_id, property_id, unit_scope, version,
            effective_from, effective_to, currency, deposit_pct, service_fee_pct,
            tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,
            max_hold_extensions, payload, fingerprint, source_doc_ref,
            approved_by, created_at
        ) VALUES (?, ?, ?, 'property', 1, '2026-01-01', '2027-01-01', 'IDR',
            '30', '5', '10', '15', 48, 720, 2, '{}', ?, 'policy.pdf',
            'user:owner', ?)
        """,
        (f"policy_{agency_id}", agency_id, f"prop_{agency_id}", FINGERPRINT, NOW),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO bookings (
            booking_id, agency_id, villa_supplier_relationship_id, property_id,
            unit_id, check_in, check_out, nights, status, guest_ref, adults,
            children, infants, currency, policy_version_id, policy_fingerprint,
            quote_ref, hold_ref, total_minor, amount_paid_minor,
            security_deposit_minor, payment_status, idempotency_key, version_lock,
            created_at, updated_at, confirmed_at, cancelled_at
        ) VALUES (?, ?, 'route_default', ?, ?, '2026-10-01', '2026-10-03', 2,
            'CONFIRMED', ?, 2, 0, 0, 'IDR', ?, ?, 'quote_1', 'hold_1',
            1500000, 0, 0, 'UNPAID', ?, 1, ?, ?, ?, NULL)
        """,
        (
            booking_id,
            agency_id,
            f"prop_{agency_id}",
            f"unit_{agency_id}",
            f"guest_{agency_id}",
            f"policy_{agency_id}",
            FINGERPRINT,
            f"idem_{booking_id}",
            NOW,
            NOW,
            NOW,
        ),
    )


def invoice_line() -> dict[str, object]:
    return {
        "kind": "NIGHTLY",
        "description": "Villa night",
        "quantity": 2,
        "unit_amount_minor": 750_000,
    }


def test_case_service_scopes_open_get_and_overdue_by_agency(conn):
    a = C.CaseService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="ca"), agency_id="agency_a")
    b = C.CaseService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="cb"), agency_id="agency_b")

    case, _ = a.open_case(
        channel="website",
        guest_ref="guest_a",
        content_class="P1",
        body="Need a villa",
        idempotency_key="idem_case_a",
    )

    assert conn.execute("SELECT agency_id FROM cases WHERE case_id = ?", (case.case_id,)).fetchone()[0] == "agency_a"
    with pytest.raises(P.DomainError) as caught:
        b.get(case.case_id)
    assert caught.value.code == "CASE_NOT_FOUND"
    assert b.overdue(now="2026-09-22T08:00:00+00:00") == []


def test_invoice_service_scopes_issue_get_and_payment_intent(conn):
    seed_booking(conn, agency_id="agency_a", booking_id="book_a")
    inv_a = INV.InvoiceService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="ia"), agency_id="agency_a")
    inv_b = INV.InvoiceService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="ib"), agency_id="agency_b")

    invoice = inv_a.issue(
        booking_id="book_a",
        currency="IDR",
        policy_fingerprint=FINGERPRINT,
        lines=[invoice_line()],
        idempotency_key="idem_invoice_a",
    )
    assert invoice.agency_id == "agency_a"

    with pytest.raises(P.DomainError) as caught_get:
        inv_b.get(invoice.invoice_id)
    assert caught_get.value.code == "INVOICE_NOT_FOUND"
    with pytest.raises(P.DomainError) as caught_issue:
        inv_b.issue(
            booking_id="book_a",
            currency="IDR",
            policy_fingerprint=FINGERPRINT,
            lines=[invoice_line()],
            idempotency_key="idem_invoice_b",
        )
    assert caught_issue.value.code == "BOOKING_NOT_FOUND"

    pay_b = PAY.PaymentService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="pb"), provider="dummy", agency_id="agency_b")
    with pytest.raises(P.DomainError) as caught_pay:
        pay_b.create_intent(
            invoice_id=invoice.invoice_id,
            amount_minor=invoice.total_minor,
            currency="IDR",
            idempotency_key="idem_payment_b",
            created_by="user_b",
        )
    assert caught_pay.value.code == "INVOICE_NOT_FOUND"


def test_payment_service_scopes_created_payment_reads(conn):
    seed_booking(conn, agency_id="agency_a", booking_id="book_a")
    inv_a = INV.InvoiceService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="ipa"), agency_id="agency_a")
    invoice = inv_a.issue(
        booking_id="book_a",
        currency="IDR",
        policy_fingerprint=FINGERPRINT,
        lines=[invoice_line()],
        idempotency_key="idem_invoice_pa",
    )
    pay_a = PAY.PaymentService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="pa"), provider="dummy", agency_id="agency_a")
    pay_b = PAY.PaymentService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="pb"), provider="dummy", agency_id="agency_b")

    intent = pay_a.create_intent(
        invoice_id=invoice.invoice_id,
        amount_minor=invoice.total_minor,
        currency="IDR",
        idempotency_key="idem_payment_a",
        created_by="user_a",
    )
    assert conn.execute("SELECT agency_id FROM payments WHERE payment_id = ?", (intent.payment_id,)).fetchone()[0] == "agency_a"

    with pytest.raises(P.DomainError) as caught:
        pay_b.get_payment(intent.payment_id)
    assert caught.value.code == "PAYMENT_NOT_FOUND"


def test_document_service_scopes_get_and_subject_references(conn, tmp_path: Path):
    seed_booking(conn, agency_id="agency_a", booking_id="book_a")
    inv_a = INV.InvoiceService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="ida"), agency_id="agency_a")
    invoice = inv_a.issue(
        booking_id="book_a",
        currency="IDR",
        policy_fingerprint=FINGERPRINT,
        lines=[invoice_line()],
        idempotency_key="idem_invoice_doc",
    )
    doc_a = D.DocumentService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="da"), root=tmp_path, agency_id="agency_a")
    doc_b = D.DocumentService(conn, clock=service_clock(), ids=P.IdFactory(deterministic=True, seed="db"), root=tmp_path, agency_id="agency_b")

    doc = doc_a.generate(
        kind="INVOICE",
        document_key="invoice-doc-a",
        subject={"invoice_id": invoice.invoice_id, "recipient_ref": "guest_a"},
        generated_by="user_a",
        actor_role="finance",
    )
    assert doc.agency_id == "agency_a"

    with pytest.raises(P.DomainError) as caught_get:
        doc_b.get(doc.document_id)
    assert caught_get.value.code == "DOCUMENT_NOT_FOUND"
    with pytest.raises(P.DomainError) as caught_generate:
        doc_b.generate(
            kind="INVOICE",
            document_key="invoice-doc-b",
            subject={"invoice_id": invoice.invoice_id, "recipient_ref": "guest_b"},
            generated_by="user_b",
            actor_role="finance",
        )
    assert caught_generate.value.code == "DOCUMENT_SOURCE_MISSING"

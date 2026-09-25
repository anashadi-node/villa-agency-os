"""Wave 1-F-B tests: agency-scoped reporting counters and business-facing keys.

Two defects of the same class — a number that belongs to one agency must never mix
with, or be blocked by, another agency's:

1. ``observability.collect_metrics()`` counted every agency's rows together, so in a
   two-agency deployment the dashboard and alert numbers were meaningless (they
   were sums, not facts about a tenant).
2. ``invoices.invoice_number`` and ``documents.(document_key, version)`` were
   globally unique, so the second agency to onboard was refused *its own* first
   invoice number and *its own* first document version.

Counters are asserted against a world seeded for two agencies, covering the three
SQL shapes the module uses: a direct column, a join through a child table, and a
GROUP BY label.
"""

from __future__ import annotations

import pytest

import approvals as A
import documents as DOC
import invoicing as INV
import observability as OBS
from primitives import FrozenClock, IdFactory
from schema_loader import connect_sqlite

NOW = "2026-09-21T08:00:00+00:00"
LATER = "2026-09-21T09:00:00+00:00"
DIGEST = "0" * 64

#: One row of each shape per agency. Every counter below is seeded for both
#: agencies so a global read shows 2 and a scoped read shows 1.
SEEDED_COUNTERS = (
    "cases_total",
    "cases_open",
    "invoices_unpaid",
    "payments_unmatched",
    "ops_tasks_open",
    "incidents_open",
    "sla_breaches_open",
    "approvals_pending",
    "reconciliation_open",
    "documents_generated",
    "drafts_awaiting_approval",
    "intake_fields_proposed",
)


def seed_agency(conn, agency_id: str) -> None:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, ?, 'ACTIVE', ?, ?)",
        (agency_id, agency_id, NOW, NOW),
    )


def seed_case(conn, agency_id: str, suffix: str) -> str:
    case_id = f"case_{suffix}"
    conn.execute(
        "INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority,"
        " sla_policy, first_response_due_at, quote_due_at, idempotency_key, created_at,"
        " updated_at) VALUES (?, ?, 'website', ?, 'NEW', 'NORMAL', 'web_v1', ?, ?, ?, ?, ?)",
        (
            case_id,
            agency_id,
            f"guest_{suffix}",
            "2026-09-22T08:00:00+00:00",
            "2026-09-23T08:00:00+00:00",
            f"idem_case_{suffix}",
            NOW,
            NOW,
        ),
    )
    return case_id


def seed_business_rows(conn, agency_id: str, suffix: str) -> str:
    """One row of each shape a counter reads, all inside ``agency_id``."""
    case_id = seed_case(conn, agency_id, suffix)
    invoice_id = f"inv_{suffix}"
    conn.execute(
        "INSERT INTO invoices (invoice_id, agency_id, invoice_number, booking_id,"
        " policy_fingerprint, currency, subtotal_minor, tax_minor, service_minor,"
        " total_minor, amount_paid_minor, status, payment_status, issued_at, due_at,"
        " created_at) VALUES (?, ?, ?, ?, ?, 'IDR', 1000, 100, 100,"
        " 1200, 0, 'ISSUED', 'UNPAID', ?, ?, ?)",
        (
            invoice_id,
            agency_id,
            f"INV-202609-{suffix.upper()}",
            f"book_{suffix}1",
            DIGEST,
            NOW,
            "2026-09-28T08:00:00+00:00",
            NOW,
        ),
    )
    payment_id = f"pay_{suffix}"
    conn.execute(
        "INSERT INTO payments (payment_id, agency_id, invoice_id, provider,"
        " provider_intent_id, method, currency, amount_minor, status, created_at,"
        " settled_at) VALUES (?, ?, ?, 'dummy', ?, 'HOSTED_PAGE', 'IDR', 1200,"
        " 'SUCCEEDED', ?, ?)",
        (payment_id, agency_id, invoice_id, f"intent_{suffix}_01", NOW, NOW),
    )
    # An event that never landed on the payment: exactly what the
    # ``payments_unmatched`` counter is meant to surface.
    conn.execute(
        "INSERT INTO payment_events (event_id, provider, provider_event_id, payment_id,"
        " event_type, signature_verified, applied, rejection_reason, payload_sha256,"
        " received_at, created_at) VALUES (?, 'dummy', ?, ?, 'payment.succeeded', 1, 0,"
        " 'invoice sudah lunas saat event datang', ?, ?, ?)",
        (f"evt_{suffix}", f"provider_evt_{suffix}", payment_id, DIGEST, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, case_id, due_at, priority,"
        " assignee, status, checklist, idempotency_key, created_at, updated_at)"
        " VALUES (?, ?, 'GUEST_FOLLOWUP', ?, ?, 'NORMAL', 'staff_014', 'OPEN', '[]', ?, ?, ?)",
        (f"task_{suffix}", agency_id, case_id, "2026-09-21T07:00:00+00:00",
         f"idem_task_{suffix}", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO incidents (incident_id, agency_id, severity, title, detected_at,"
        " detected_by, source, playbook, status, created_at)"
        " VALUES (?, ?, 'SEV3', ?, ?, 'ops_1', 'ops_report', 'VENUE_OUTAGE', 'OPEN', ?)",
        (f"inc_{suffix}", agency_id, f"Insiden uji {suffix}", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO sla_breaches (breach_id, agency_id, subject_type, subject_id, kind,"
        " due_at, detected_at, breach_minutes, status, created_at)"
        " VALUES (?, ?, 'ops_task', ?, 'OPS_TASK', ?, ?, 30, 'OPEN', ?)",
        (f"breach_{suffix}", agency_id, f"task_{suffix}", NOW, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO reconciliation_cases (recon_id, agency_id, kind, detected_at, due_at,"
        " status, details, created_at) VALUES (?, ?, 'UNMATCHED_PAYMENT', ?, ?, 'OPEN',"
        " '{}', ?)",
        (f"recon_{suffix}", agency_id, NOW, "2026-09-22T08:00:00+00:00", NOW),
    )
    conn.execute(
        "INSERT INTO documents (document_id, agency_id, document_key, kind, version,"
        " format, case_id, content_class, redaction_profile, storage_ref, body_sha256,"
        " status, generated_by, generated_at, created_at)"
        " VALUES (?, ?, ?, 'NOTICE', 1, 'TEXT', ?, 'P2', 'default', ?, ?, 'GENERATED',"
        " 'agent_01', ?, ?)",
        (f"doc_{suffix}", agency_id, f"doc.key_{suffix}", case_id,
         f"storage/{suffix}.txt", DIGEST, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO message_drafts (draft_id, case_id, channel, template, locale,"
        " recipient_ref, body_text, body_sha256, grounding, gate_decision, status,"
        " created_by, created_at) VALUES (?, ?, 'website', 'inquiry_reply', 'id-ID',"
        " ?, 'Terima kasih atas pertanyaannya', ?, 'GROUNDED', 'ALLOW',"
        " 'PENDING_APPROVAL', 'agent_01', ?)",
        (f"draft_{suffix}", case_id, f"guest_{suffix}", DIGEST, NOW),
    )
    message_id = f"msg_{suffix}"
    conn.execute(
        "INSERT INTO case_messages (case_message_id, case_id, direction, channel,"
        " sender_ref, received_at, content_class, body_sha256, created_at)"
        " VALUES (?, ?, 'INBOUND', 'website', ?, ?, 'P0', ?, ?)",
        (message_id, case_id, f"guest_{suffix}", NOW, DIGEST, NOW),
    )
    conn.execute(
        "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id, extractor,"
        " method, produced_at, status, payload, fingerprint, created_at)"
        " VALUES (?, ?, ?, 'rule_v1', 'rule', ?, 'PROPOSED', '{}', ?, ?)",
        (f"prop_{suffix}", case_id, message_id, NOW, DIGEST, NOW),
    )
    conn.execute(
        "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id, field_name,"
        " field_value, value_type, confidence, source_message_id, status, value_sha256,"
        " created_at) VALUES (?, ?, ?, 'check_in', '2026-10-12', 'date', 90, ?,"
        " 'PROPOSED', ?, ?)",
        (f"field_{suffix}", f"prop_{suffix}", case_id, message_id, DIGEST, NOW),
    )
    A.ApprovalService(conn, FrozenClock.at(NOW), IdFactory(seed=f"appr{suffix}"),
                      agency_id=agency_id).request(
        subject_type="case",
        subject_id=case_id,
        action="discount.approve",
        requested_by="agent_01",
        requested_role="agent",
        reason="tamuan meminta potongan untuk menginap lebih lama",
        idempotency_key=f"idem_appr_{suffix}",
    )
    return case_id


def seed_booking_chain(conn, agency_id: str, suffix: str) -> str:
    """property -> unit -> policy -> booking, all inside ``agency_id``."""
    property_id, unit_id, policy_id = f"prop_{suffix}", f"unit_{suffix}", f"policy_{suffix}"
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
        " province, timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
        " 'ACTIVATED', ?, ?)",
        (property_id, agency_id, f"Villa {suffix}", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?)",
        (unit_id, agency_id, property_id, f"Unit {suffix}", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO policy_versions (policy_version_id, agency_id, property_id, unit_scope,"
        " version, effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
        " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
        " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES (?, ?, ?, 'property', 1, '2026-01-01', '2027-01-01', 'IDR', '30', '10',"
        " '5', '10', 48, 60, 1, '{}', ?, 'doc.pdf', 'ops_1', ?)",
        (policy_id, agency_id, property_id, DIGEST, NOW),
    )
    booking_ids = []
    for index, check_in in enumerate(("2026-10-01", "2026-11-01"), start=1):
        booking_id = f"book_{suffix}{index}"
        conn.execute(
            "INSERT INTO bookings (booking_id, agency_id, property_id, unit_id, check_in,"
            " check_out, nights, status, guest_ref, adults, children, infants, currency,"
            " policy_version_id, policy_fingerprint, quote_ref, hold_ref, total_minor,"
            " amount_paid_minor, security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 2, 'CONFIRMED', ?, 2, 0, 0, 'IDR', ?, ?,"
            " 'quote_1', 'hold_1', 1500000, 300000, 0, 'UNPAID', ?, 1, ?, ?, ?)",
            (
                booking_id,
                agency_id,
                property_id,
                unit_id,
                check_in,
                "2026-10-03" if check_in == "2026-10-01" else "2026-11-03",
                f"guest_{suffix}",
                policy_id,
                DIGEST,
                f"idem_{booking_id}",
                NOW,
                NOW,
                NOW,
            ),
        )
        booking_ids.append(booking_id)
    return booking_ids[0]


def world(conn) -> None:
    """Two independent agencies, each with one row of every shape counter reads."""
    for agency_id, suffix in (("agency_a", "a"), ("agency_b", "b")):
        seed_agency(conn, agency_id)
        seed_booking_chain(conn, agency_id, suffix)
        seed_business_rows(conn, agency_id, suffix)


INVOICE_LINE = {
    "kind": "NIGHTLY",
    "description": "Villa night",
    "quantity": 2,
    "unit_amount_minor": 750_000,
}


# --------------------------------------------------------------------------- #
# 1. Reporting counters
# --------------------------------------------------------------------------- #
def test_reporting_counters_belong_to_one_agency(conn):
    world(conn)
    clock = FrozenClock.at(NOW)
    a = OBS.collect_metrics(conn, clock=clock, agency_id="agency_a")
    b = OBS.collect_metrics(conn, clock=clock, agency_id="agency_b")

    for key in SEEDED_COUNTERS:
        assert a.counters[key] == 1, f"{key}: agency_a sees {a.counters[key]}, not its own 1"
        assert b.counters[key] == 1, f"{key}: agency_b sees {b.counters[key]}, not its own 1"

    assert a.labels["cases_by_status"] == {"NEW": 1}
    assert b.labels["cases_by_status"] == {"NEW": 1}
    assert a.gauges["oldest_open_reconciliation_age_hours"] == 0.0


def test_reporting_without_an_agency_still_describes_the_whole_database(conn):
    """Single-agency deployments keep working: no agency means no filter."""
    world(conn)
    clock = FrozenClock.at(NOW)
    everything = OBS.collect_metrics(conn, clock=clock)

    for key in SEEDED_COUNTERS:
        assert everything.counters[key] == 2, f"{key}: {everything.counters[key]}"
    assert everything.labels["cases_by_status"] == {"NEW": 2}


def test_an_agency_with_no_rows_reports_zero_not_the_other_tenants_numbers(conn):
    world(conn)
    clock = FrozenClock.at(NOW)
    empty = OBS.collect_metrics(conn, clock=clock, agency_id="agency_kosong")

    for key in SEEDED_COUNTERS:
        assert empty.counters[key] == 0, f"{key}: a fresh agency saw {empty.counters[key]}"


def test_the_audit_chain_gauge_stays_global_on_purpose(conn):
    """One hash chain for the whole platform: scoping it would break verification."""
    world(conn)
    clock = FrozenClock.at(NOW)
    a = OBS.collect_metrics(conn, clock=clock, agency_id="agency_a")
    b = OBS.collect_metrics(conn, clock=clock, agency_id="agency_b")

    assert a.gauges["audit_chain_head_seq"] == b.gauges["audit_chain_head_seq"]


# --------------------------------------------------------------------------- #
# 2. invoice_number is a per-agency sequence
# --------------------------------------------------------------------------- #
def test_each_agency_gets_its_own_invoice_number_sequence(conn):
    world(conn)
    clock = FrozenClock.at(NOW)
    inv_a = INV.InvoiceService(conn, clock, IdFactory(seed="ia"), agency_id="agency_a")
    inv_b = INV.InvoiceService(conn, clock, IdFactory(seed="ib"), agency_id="agency_b")

    first_a = inv_a.issue(
        booking_id="book_a1", currency="IDR", policy_fingerprint=DIGEST,
        lines=[dict(INVOICE_LINE)], idempotency_key="idem_issue_a_0001",
    )
    first_b = inv_b.issue(
        booking_id="book_b1", currency="IDR", policy_fingerprint=DIGEST,
        lines=[dict(INVOICE_LINE)], idempotency_key="idem_issue_b_0001",
    )

    assert first_a.invoice_number == first_b.invoice_number, (
        "agency_b was pushed off its own first invoice number"
    )
    stored = conn.execute(
        "SELECT agency_id FROM invoices WHERE invoice_number = ? ORDER BY agency_id",
        (first_a.invoice_number,),
    ).fetchall()
    assert [row["agency_id"] for row in stored] == ["agency_a", "agency_b"]


def test_the_invoice_sequence_continues_inside_one_agency(conn):
    world(conn)
    clock = FrozenClock.at(NOW)
    inv_a = INV.InvoiceService(conn, clock, IdFactory(seed="ia2"), agency_id="agency_a")

    first = inv_a.issue(
        booking_id="book_a1", currency="IDR", policy_fingerprint=DIGEST,
        lines=[dict(INVOICE_LINE)], idempotency_key="idem_seq_a_0001",
    )
    second = inv_a.issue(
        booking_id="book_a2", currency="IDR", policy_fingerprint=DIGEST,
        lines=[dict(INVOICE_LINE)], idempotency_key="idem_seq_a_0002",
    )

    assert second.invoice_number != first.invoice_number
    assert second.invoice_number > first.invoice_number


def test_a_number_used_by_another_agency_is_not_taken_here(conn):
    world(conn)
    inv_b = INV.InvoiceService(conn, FrozenClock.at(NOW), IdFactory(seed="ib2"),
                              agency_id="agency_b")
    taken_by_a = conn.execute(
        "SELECT invoice_number FROM invoices WHERE agency_id = 'agency_a'"
    ).fetchone()["invoice_number"]

    assert inv_b._number_taken(taken_by_a) is False, (
        "agency_b was told a number it never used is already taken"
    )


# --------------------------------------------------------------------------- #
# 3. document versions are counted per agency
# --------------------------------------------------------------------------- #
def test_document_versions_are_counted_per_agency(conn, tmp_path):
    world(conn)
    clock = FrozenClock.at(NOW)
    key = "doc.shared_key_1"
    doc_a = DOC.DocumentService(
        conn, clock=clock, ids=IdFactory(seed="da"), root=tmp_path / "a", agency_id="agency_a"
    )
    doc_b = DOC.DocumentService(
        conn, clock=clock, ids=IdFactory(seed="db"), root=tmp_path / "b", agency_id="agency_b"
    )

    first_a = doc_a.generate(
        kind="CONFIRMATION", document_key=key, subject={"booking_id": "book_a1"},
        generated_by="agent_01", actor_role="agent",
    )
    first_b = doc_b.generate(
        kind="CONFIRMATION", document_key=key, subject={"booking_id": "book_b1"},
        generated_by="agent_01", actor_role="agent",
    )

    assert (first_a.version, first_b.version) == (1, 1), (
        "the second agency was pushed off its own first version"
    )

    second_a = doc_a.generate(
        kind="CONFIRMATION", document_key=key, subject={"booking_id": "book_a1"},
        generated_by="agent_01", actor_role="agent",
    )
    assert second_a.version == 2, "the version inside one agency no longer increments"
    versions = conn.execute(
        "SELECT agency_id, version FROM documents WHERE document_key = ?"
        " ORDER BY agency_id, version",
        (key,),
    ).fetchall()
    assert [(row["agency_id"], row["version"]) for row in versions] == [
        ("agency_a", 1), ("agency_a", 2), ("agency_b", 1),
    ]

def test_one_agency_cannot_supersede_another_agencys_document(conn, tmp_path):
    """Sharing a document_key must not let a tenant rewrite a sibling's status."""
    world(conn)
    clock = FrozenClock.at(NOW)
    key = "doc.shared_key_2"
    doc_a = DOC.DocumentService(
        conn, clock=clock, ids=IdFactory(seed="da2"), root=tmp_path / "a2", agency_id="agency_a"
    )
    doc_b = DOC.DocumentService(
        conn, clock=clock, ids=IdFactory(seed="db2"), root=tmp_path / "b2", agency_id="agency_b"
    )

    kept = doc_a.generate(
        kind="CONFIRMATION", document_key=key, subject={"booking_id": "book_a1"},
        generated_by="agent_01", actor_role="agent",
    )
    doc_b.generate(
        kind="CONFIRMATION", document_key=key, subject={"booking_id": "book_b1"},
        generated_by="agent_01", actor_role="agent",
    )

    status = conn.execute(
        "SELECT status FROM documents WHERE document_id = ?", (kept.document_id,)
    ).fetchone()["status"]
    assert status == "GENERATED", f"agency_b rewrote agency_a's document to {status}"

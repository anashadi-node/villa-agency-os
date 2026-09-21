"""Row builders shared by the operation-room tests.

Direct SQL, not service calls: a test for "the sweep finds an overdue invoice"
must be able to create an overdue invoice even when the invoice service would
refuse to create one in the past. The vocabulary and constraints come from the
schema, so anything built here is a state the database itself accepts.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

# The approval service canonicalises its payload before hashing and refuses to
# read a row whose digest does not match. A seeder that invents a digest would
# only test the tamper detector, so it uses the writer's own canonicalisation.
from approvals import _payload_digest

CREATED = "2026-09-18T08:00:00+00:00"   # rows exist before they fall due
PAST = "2026-09-20T08:00:00+00:00"      # already past every SLA at the test clock
DAY = "2026-09-22T08:00:00+00:00"       # still inside every SLA
NOW = "2026-09-21T08:00:00+00:00"       # the frozen clock most tests use


def _shift_days(moment: str, days: int) -> str:
    from datetime import datetime

    return (datetime.fromisoformat(moment) + timedelta(days=days)).isoformat()


def _earliest(first: str, second: str) -> str:
    return min(first, second)


def stay(offset: int, nights: int = 4) -> tuple[str, str]:
    """A distinct stay per seeded row: the schema forbids reused slots."""

    start = date(2026, 10, 1) + timedelta(days=offset)
    return start.isoformat(), (start + timedelta(days=nights)).isoformat()


class SeedMixin:
    conn: Any

    # -- plumbing ----------------------------------------------------------
    def _insert(self, table: str, **cols: Any) -> None:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))

    def inventory(self) -> None:
        """Property, unit, policy and availability evidence: the parents every
        booking-related row needs to exist at all."""

        self._insert(
            "properties", property_id="prop_0001", name="Villa Uji", property_type="villa",
            region="bali", province="Bali", timezone="Asia/Makassar", status="SELLABLE",
            contract_pack_status="ACTIVATED", contract_pack_evaluated_at=CREATED,
            created_at=CREATED, updated_at=CREATED,
        )
        self._insert(
            "units", unit_id="unit_0001", property_id="prop_0001", name="Unit Uji",
            unit_type="entire_villa", bedrooms=2, bathrooms=2, base_capacity=2, max_guests=4,
            status="ACTIVE", created_at=CREATED, updated_at=CREATED,
        )
        self._insert(
            "availability_evidence", evidence_id="ev_0001", unit_id="unit_0001",
            evidence_level="A", source_type="manager_written", source_ref="owner_anas",
            captured_at=CREATED, valid_until=DAY, result="AVAILABLE",
            can_quote=1, can_hold=1, can_confirm=1, created_at=CREATED,
        )
        self._insert(
            "policy_versions", policy_version_id="policy_v1", property_id="prop_0001",
            unit_scope="property", version=1, effective_from="2026-01-01",
            effective_to="9999-12-31", currency="IDR", deposit_pct="30",
            service_fee_pct="0", tax_pbjt_pct="10", max_discount_pct="10",
            quote_valid_hours=24, hold_ttl_minutes=120, max_hold_extensions=1,
            payload="{}", fingerprint="f" * 64, source_doc_ref="seed",
            approved_by="owner_anas", created_at=CREATED,
        )

    # -- rows --------------------------------------------------------------
    def case(self, case_id: str = "case_0001", status: str = "NEW",
             first_due: str = PAST, quote_due: str = DAY,
             first_answered: bool = False, quoted: bool = False) -> None:
        closed = status.startswith("CLOSED_")
        self._insert(
            "cases", case_id=case_id, channel="website", guest_ref="guest_0001",
            status=status, priority="NORMAL", sla_policy="web_v1",
            owner_actor="agent_ana",
            first_response_due_at=first_due, quote_due_at=quote_due,
            first_responded_at=CREATED if first_answered else None,
            quoted_at=CREATED if quoted else None,
            closed_at=PAST if closed else None,
            close_reason="tamu memilih tanggal lain" if closed else None,
            idempotency_key=f"idem_{case_id}", created_at=CREATED, updated_at=CREATED,
        )

    def hold(self, hold_id: str = "hold_0001", expires_at: str = DAY,
             status: str = "ACTIVE", offset: int = 0) -> None:
        check_in, check_out = stay(offset)
        self._insert(
            "holds", hold_id=hold_id, unit_id="unit_0001", property_id="prop_0001",
            check_in=check_in, check_out=check_out, nights=4, status=status,
            guest_ref="guest_0001", policy_version_id="policy_v1", evidence_id="ev_0001",
            idempotency_key=f"idem_{hold_id}", created_at=CREATED, expires_at=expires_at,
        )

    def booking(self, booking_id: str = "book_0001", status: str = "CONFIRMED",
                offset: int = 0) -> None:
        check_in, check_out = stay(offset)
        self._insert(
            "bookings", booking_id=booking_id, property_id="prop_0001",
            unit_id="unit_0001", check_in=check_in, check_out=check_out, nights=4,
            status=status, guest_ref="guest_0001", adults=2, children=0, infants=0,
            currency="IDR", policy_version_id="policy_v1", policy_fingerprint="f" * 64,
            total_minor=1_000_000, payment_status="UNPAID",
            idempotency_key=f"idem_{booking_id}", created_at=CREATED, updated_at=CREATED,
        )

    def invoice(self, invoice_id: str = "inv_0001", payment_status: str = "UNPAID",
                due_at: str = PAST, status: str = "ISSUED") -> None:
        voided = status == "VOID"
        # issued_at < due_at is a schema invariant, so a very overdue invoice needs
        # an issue date further back than the shared CREATED constant.
        issued_at = _earliest(CREATED, _shift_days(due_at, -30))
        self._insert(
            "invoices", invoice_id=invoice_id,
            invoice_number=f"INV-202609-{invoice_id[-4:]}", booking_id="book_0001",
            policy_fingerprint="f" * 64, currency="IDR", subtotal_minor=900_000,
            tax_minor=100_000, service_minor=0, total_minor=1_000_000,
            amount_paid_minor=0, status=status, payment_status=payment_status,
            issued_at=issued_at, due_at=due_at, created_at=issued_at,
            voided_at=PAST if voided else None,
            void_reason="salah tarif, diterbitkan ulang" if voided else None,
        )

    def ops_task(self, task_id: str = "task_0001", status: str = "OPEN",
                 due_at: str = PAST, assignee: str | None = None) -> None:
        done = status in ("DONE", "CANCELLED")
        self._insert(
            "ops_tasks", task_id=task_id, kind="CLEANING", due_at=due_at,
            priority="NORMAL", status=status, assignee=assignee,
            completed_at=PAST if done else None,
            blocked_reason="vendor belum konfirmasi" if status == "BLOCKED" else None,
            idempotency_key=f"idem_{task_id}", created_at=CREATED, updated_at=CREATED,
        )

    def reconciliation(self, recon_id: str = "recon_0001", due_at: str = PAST,
                       status: str = "OPEN") -> None:
        resolved = status != "OPEN"
        self._insert(
            "reconciliation_cases", recon_id=recon_id, kind="AMOUNT_MISMATCH",
            detected_at=CREATED, due_at=due_at, status=status, created_at=CREATED,
            resolved_at=PAST if resolved else None,
            resolution_note="dicocokkan manual dengan mutasi bank" if resolved else None,
        )

    def incident(self, incident_id: str = "inc_0001", severity: str = "SEV1",
                 status: str = "OPEN", notice_required: int = 0,
                 notice_due_at: str | None = None, notified_at: str | None = None) -> None:
        self._insert(
            "incidents", incident_id=incident_id, severity=severity,
            title="Test incident", detected_at=CREATED, detected_by="ops.alert",
            source="monitor", playbook="DOUBLE_BOOKING", status=status,
            regulatory_notification_required=notice_required,
            notified_at=notified_at,
            notice_due_at=notice_due_at if notice_due_at else ("2026-09-21T08:00:00+00:00" if notice_required else None),
            resolved_at=PAST if status in ("RESOLVED", "POSTMORTEM_DONE") else None,
            summary="selesai, terdokumentasi" if status in ("RESOLVED", "POSTMORTEM_DONE") else None,
            created_at=CREATED,
        )

    def approval(self, approval_id: str = "appr_0001", status: str = "PENDING",
                 expires_at: str = PAST) -> None:
        self._insert(
            "approval_requests", approval_id=approval_id, subject_type="discount",
            subject_id="case_0001", action="discount.override", risk_level="MEDIUM",
            requested_by="agent_ana", requested_role="reservation", requested_at=CREATED,
            reason="permintaan diskon masa menginap panjang",
            payload=json.dumps({}), payload_sha256=_payload_digest({}),
            required_role="owner", required_approvals=1,
            status=status, expires_at=expires_at,
            idempotency_key=f"idem_{approval_id}", created_at=CREATED,
        )

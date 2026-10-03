#!/usr/bin/env python3
"""Build a demo dataset so the dashboard can be shown to a buyer in minutes.

**Honest scope.** This script writes *synthetic rows* straight into the database
(it does not walk the services, because a demo needs many states at once — a
paid booking, an overdue invoice, an unmatched payment, an expired hold — and
manufacturing those through the API would take far longer and hide the sample
data inside real-looking flows). For that reason every identity it writes starts
with ``demo_`` and every property name contains "Contoh": `scripts/preflight.py`
refuses a production database that contains them.

The flow-level proof — inquiry → intake → match → quote → hold → payment →
confirmation → document → stay → settlement → closure — is ``scripts/go_live_smoke.py``,
which drives the public API on a database created from nothing and is itself covered by
``tests/test_go_live_smoke.py``. This seeder stays what it is: a fast demo fixture.

    python3 scripts/seed_demo.py --db /tmp/demo/villa.db
    python3 scripts/seed_demo.py --db /tmp/demo/villa.db --dry-run

Exit codes: 0 = seeded (or reported, with ``--dry-run``), 1 = refused because the
database already holds rows — a real dataset or a previous demo one, 2 = bad
arguments / unreadable database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from approvals import _payload_digest  # noqa: E402
from primitives import FrozenClock, IdFactory, to_iso  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEMO_PREFIX = "demo_"

#: The pending decision the demo shows. Hashed for real below: a placeholder digest makes the
#: approval unapprovable and makes ``scheduler.py`` refuse the whole tick.
DEMO_APPROVAL_PAYLOAD = {"discount_pct": 8}
NOW = "2026-09-21T08:00:00+00:00"
DAY = "2026-09-22T08:00:00+00:00"
YESTERDAY = "2026-09-20T08:00:00+00:00"
LAST_WEEK = "2026-09-14T08:00:00+00:00"
NEXT_WEEK = "2026-09-28T08:00:00+00:00"

COUNTED_TABLES = (
    "properties", "units", "policy_versions", "availability_evidence",
    "cases", "case_messages", "case_events", "intake_proposals", "intake_fields",
    "holds", "bookings", "invoices", "invoice_lines", "payments",
    "payment_events", "reconciliation_cases", "documents", "ops_tasks",
    "incidents", "incident_steps", "approval_requests", "audit_events",
)
GUARD_TABLES = ("properties", "units", "bookings", "cases")
IDENTITY_COLUMNS = {"properties": "property_id", "units": "unit_id",
                    "bookings": "booking_id", "cases": "case_id"}


class SeedRefused(Exception):
    """The database is not a demo database; the script must not touch it."""


def _identity_rows(conn, *, demo: bool) -> list[str]:
    """Guarded tables already holding rows, counted as a demo or a real dataset."""

    offenders: list[str] = []
    for table in GUARD_TABLES:
        column = IDENTITY_COLUMNS[table]
        operator = "LIKE" if demo else "NOT LIKE"
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} {operator} ?",
            (f"{DEMO_PREFIX}%",),
        ).fetchone()
        if int(row["n"]):
            offenders.append(f"{table}={int(row['n'])}")
    return offenders


def _has_real_rows(conn) -> list[str]:
    """Tables holding rows whose identity is not prefixed with ``demo_``."""
    return _identity_rows(conn, demo=False)


def _has_demo_rows(conn) -> list[str]:
    """Tables already holding a demo dataset: seeding again would collide on identity."""
    return _identity_rows(conn, demo=True)


class DemoSeeder:
    def __init__(self, conn, *, clock=None, ids=None) -> None:
        self.conn = conn
        self.clock = clock or FrozenClock.at(NOW)
        self.ids = ids or IdFactory(True, "demo")
        self.now = to_iso(self.clock.now())

    # -- plumbing ---------------------------------------------------------
    def insert(self, table: str, **cols) -> None:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))

    def id_for(self, prefix: str) -> str:
        return f"{DEMO_PREFIX}{prefix}_{self.ids.new('x').split('_')[-1]}"

    # -- inventory --------------------------------------------------------
    def inventory(self) -> None:
        properties = (
            ("prop_ubud", "Villa Contoh Ubud", "villa", "ubud", "Bali", "Asia/Makassar"),
            ("prop_canggu", "Villa Contoh Canggu", "villa", "canggu", "Bali", "Asia/Makassar"),
            ("prop_jogja", "Rumah Contoh Prawirotaman", "guesthouse", "yogyakarta",
             "DI Yogyakarta", "Asia/Jakarta"),
        )
        for key, name, kind, region, province, tz in properties:
            self.insert(
                "properties", property_id=f"{DEMO_PREFIX}{key}", name=name,
                property_type=kind, region=region, province=province, timezone=tz,
                status="SELLABLE", contract_pack_status="ACTIVATED",
                contract_pack_evaluated_at=LAST_WEEK, created_at=LAST_WEEK, updated_at=LAST_WEEK,
            )

        units = (
            ("unit_ubud_1", "prop_ubud", "Villa Utama", "entire_villa", 3, 3, 4, 8),
            ("unit_ubud_2", "prop_ubud", "Suite Garden", "suite", 1, 1, 2, 3),
            ("unit_canggu_1", "prop_canggu", "Villa Pantai", "entire_villa", 4, 4, 6, 10),
            ("unit_jogja_1", "prop_jogja", "Kamar Prawiro 1", "room", 1, 1, 2, 2),
        )
        versions_by_property: dict[str, int] = {}
        for key, prop, name, kind, beds, baths, base, maxg in units:
            version = versions_by_property.get(prop, 0) + 1
            versions_by_property[prop] = version
            self.insert(
                "units", unit_id=f"{DEMO_PREFIX}{key}", property_id=f"{DEMO_PREFIX}{prop}",
                name=name, unit_type=kind, bedrooms=beds, bathrooms=baths,
                base_capacity=base, max_guests=maxg, status="ACTIVE",
                created_at=LAST_WEEK, updated_at=LAST_WEEK,
            )
            self.insert(
                "availability_evidence", evidence_id=f"{DEMO_PREFIX}ev_{key}",
                unit_id=f"{DEMO_PREFIX}{key}", evidence_level="A", source_type="manager_written",
                source_ref="owner_demo", captured_at=LAST_WEEK, valid_until=NEXT_WEEK,
                result="AVAILABLE", can_quote=1, can_hold=1, can_confirm=1,
                notes="data contoh", created_at=LAST_WEEK,
            )
            self.insert(
                "policy_versions", policy_version_id=f"{DEMO_PREFIX}policy_{key}",
                property_id=f"{DEMO_PREFIX}{prop}", unit_scope="unit", version=version,
                effective_from="2026-01-01", effective_to="9999-12-31", currency="IDR",
                deposit_pct="30", service_fee_pct="5", tax_pbjt_pct="10",
                max_discount_pct="10", quote_valid_hours=24, hold_ttl_minutes=120,
                max_hold_extensions=1, payload=json.dumps({"demo": True}),
                fingerprint="d" * 64, source_doc_ref=f"{DEMO_PREFIX}policy",
                approved_by="owner_demo", created_at=LAST_WEEK,
            )

    # -- guests & cases ---------------------------------------------------
    def cases(self) -> None:
        thread = (
            ("case_rahma", "website", "guest_rahma", "TRIAGED", "HIGH",
             "Halo, kami keluarga 4 orang mau menginap di Ubud 12-15 Oktober 2026, "
             "budget sekitar Rp 2.500.000 per malam. Ada kolam dan sarapan?",
             "P2"),
            ("case_marco", "email", "guest_marco", "QUOTED", "NORMAL",
             "Hi, looking for a 2-bedroom place in Canggu for 3 nights from 2026-10-20. "
             "Family of 4, need parking.",
             "P2"),
            ("case_sari", "whatsapp_business", "guest_sari", "AWAITING_EVIDENCE", "NORMAL",
             "Mas, villa Prawiro tanggal 5-7 November masih ada? Untuk 2 orang.",
             "P2"),
        )
        for key, channel, guest, status, priority, body, klass in thread:
            case_id = f"{DEMO_PREFIX}{key}"
            self.insert(
                "cases", case_id=case_id, channel=channel, guest_ref=guest, status=status,
                priority=priority, sla_policy=f"{'web' if channel == 'website' else 'email'}_v1",
                owner_actor="agent_demo", first_response_due_at=DAY, quote_due_at=NEXT_WEEK,
                first_responded_at=LAST_WEEK, idempotency_key=f"{DEMO_PREFIX}idem_{key}",
                created_at=LAST_WEEK, updated_at=LAST_WEEK,
            )
            message_id = f"{DEMO_PREFIX}msg_{key}"
            self.insert(
                "case_messages", case_message_id=message_id, case_id=case_id,
                direction="INBOUND", channel=channel, sender_ref=guest,
                received_at=LAST_WEEK, content_class=klass, body_redacted=body,
                body_sha256="e" * 64, attachment_count=0, untrusted=1, created_at=LAST_WEEK,
            )
            self.insert(
                "case_events", case_event_id=f"{DEMO_PREFIX}ev_{key}_1", case_id=case_id,
                seq=1, kind="CREATED", actor="agent_demo", actor_role="reservation",
                command_id=f"{DEMO_PREFIX}cmd_{key}_1", occurred_at=LAST_WEEK,
            )
            self.insert(
                "case_events", case_event_id=f"{DEMO_PREFIX}ev_{key}_2", case_id=case_id,
                seq=2, kind="MESSAGE_IN", actor="connector", actor_role="system",
                command_id=f"{DEMO_PREFIX}cmd_{key}_2", occurred_at=LAST_WEEK,
                refs=json.dumps([message_id]),
            )
            proposal_id = f"{DEMO_PREFIX}prop_{key}"
            self.insert(
                "intake_proposals", proposal_id=proposal_id, case_id=case_id,
                source_message_id=message_id, extractor="rule-v1", method="rule",
                produced_at=LAST_WEEK, status="PROPOSED",
                payload=json.dumps({"fields": 3}), fingerprint="f" * 64, created_at=LAST_WEEK,
            )
            self.insert(
                "intake_fields", intake_field_id=f"{DEMO_PREFIX}field_{key}_1",
                proposal_id=proposal_id, case_id=case_id, field_name="adults",
                field_value="4", value_type="integer", confidence=82,
                source_message_id=message_id, source_span_start=9, source_span_end=20,
                status="PROPOSED", value_sha256="a" * 64, created_at=LAST_WEEK,
            )

    # -- pipeline states ---------------------------------------------------
    def pipeline(self) -> None:
        # A hold that expires in six hours: the dashboard shows it under "expiring".
        self.insert(
            "holds", hold_id=f"{DEMO_PREFIX}hold_1", unit_id=f"{DEMO_PREFIX}unit_ubud_1",
            property_id=f"{DEMO_PREFIX}prop_ubud", check_in="2026-10-12",
            check_out="2026-10-15", nights=3, status="CONFIRMED", guest_ref="guest_rahma",
            policy_version_id=f"{DEMO_PREFIX}policy_unit_ubud_1",
            evidence_id=f"{DEMO_PREFIX}ev_unit_ubud_1",
            idempotency_key=f"{DEMO_PREFIX}idem_hold_1", created_at=LAST_WEEK,
            expires_at="2026-09-21T14:00:00+00:00",
            expires_timezone="Asia/Makassar",
        )

        # A confirmed booking with a paid invoice and a receipt.
        self.insert(
            "bookings", booking_id=f"{DEMO_PREFIX}book_1", property_id=f"{DEMO_PREFIX}prop_canggu",
            unit_id=f"{DEMO_PREFIX}unit_canggu_1", check_in="2026-10-20",
            check_out="2026-10-23", nights=3, status="CONFIRMED", guest_ref="guest_marco",
            adults=4, children=0, infants=0, currency="IDR",
            policy_version_id=f"{DEMO_PREFIX}policy_unit_canggu_1",
            policy_fingerprint="d" * 64, total_minor=9_000_000,
            amount_paid_minor=9_000_000, payment_status="CLEARED_FULL",
            idempotency_key=f"{DEMO_PREFIX}idem_book_1", created_at=LAST_WEEK,
            updated_at=YESTERDAY, confirmed_at=YESTERDAY,
        )
        self.insert(
            "invoices", invoice_id=f"{DEMO_PREFIX}inv_1",
            invoice_number="INV-202609-0001", booking_id=f"{DEMO_PREFIX}book_1",
            policy_fingerprint="d" * 64, currency="IDR", subtotal_minor=7_800_000,
            tax_minor=900_000, service_minor=300_000, total_minor=9_000_000,
            amount_paid_minor=9_000_000, status="ISSUED", payment_status="PAID",
            issued_at=LAST_WEEK, due_at=YESTERDAY, created_at=LAST_WEEK,
        )
        self.insert(
            "invoice_lines", line_id=f"{DEMO_PREFIX}line_1", invoice_id=f"{DEMO_PREFIX}inv_1",
            kind="NIGHTLY", description="3 malam Villa Pantai", quantity=3,
            unit_amount_minor=2_600_000, amount_minor=7_800_000,
            rate_card_id=None, nights=3, created_at=LAST_WEEK,
        )
        self.insert(
            "payments", payment_id=f"{DEMO_PREFIX}pay_1", invoice_id=f"{DEMO_PREFIX}inv_1",
            provider="demo_provider", provider_intent_id="pi_demo_paid_0001",
            method="HOSTED_PAGE", currency="IDR", amount_minor=9_000_000,
            status="SUCCEEDED", created_at=LAST_WEEK, settled_at=YESTERDAY,
            provider_status="settled",
        )
        self.insert(
            "payment_events", event_id=f"{DEMO_PREFIX}pevent_1", provider="demo_provider",
            provider_event_id="evt_demo_paid_0001", event_type="payment.succeeded",
            signature_verified=1, applied=1, payment_id=f"{DEMO_PREFIX}pay_1",
            amount_minor=9_000_000, currency="IDR", payload_sha256="b" * 64,
            received_at=YESTERDAY, created_at=YESTERDAY,
        )
        self.insert(
            "documents", document_id=f"{DEMO_PREFIX}doc_1",
            document_key=f"BOOKING:{DEMO_PREFIX}book_1:CONFIRMATION", kind="CONFIRMATION",
            version=1, format="HTML", booking_id=f"{DEMO_PREFIX}book_1",
            content_class="P1", redaction_profile="guest-safe",
            storage_ref=f"documents/confirmation.{DEMO_PREFIX}book_1.html",
            body_sha256="c" * 64, status="GENERATED", generated_by="agent_demo",
            generated_at=YESTERDAY, created_at=YESTERDAY,
        )

        # An overdue unpaid invoice: the heartbeat will open a payment-chase task.
        self.insert(
            "invoices", invoice_id=f"{DEMO_PREFIX}inv_2",
            invoice_number="INV-202609-0002", booking_id=f"{DEMO_PREFIX}book_1",
            policy_fingerprint="d" * 64, currency="IDR", subtotal_minor=2_600_000,
            tax_minor=260_000, service_minor=130_000, total_minor=2_990_000,
            amount_paid_minor=0, status="ISSUED", payment_status="UNPAID",
            issued_at=LAST_WEEK, due_at=YESTERDAY, created_at=LAST_WEEK,
        )

        # Money that arrived without a matching invoice: finance has to look.
        self.insert(
            "payment_events", event_id=f"{DEMO_PREFIX}pevent_2", provider="demo_provider",
            provider_event_id="evt_demo_unmatched_0001", event_type="payment.succeeded",
            signature_verified=1, applied=0, rejection_reason="tidak ada invoice yang cocok",
            amount_minor=1_500_000, currency="IDR", payload_sha256="b" * 64,
            received_at=YESTERDAY, created_at=YESTERDAY,
        )
        self.insert(
            "reconciliation_cases", recon_id=f"{DEMO_PREFIX}recon_1", kind="UNMATCHED_PAYMENT",
            payment_event_id=f"{DEMO_PREFIX}pevent_2", detected_at=YESTERDAY,
            due_at="2026-09-22T08:00:00+00:00", status="OPEN", details="nominal 1.500.000 tanpa invoice",
            created_at=YESTERDAY,
        )

        # Operations: one overdue task, one for tomorrow.
        self.insert(
            "ops_tasks", task_id=f"{DEMO_PREFIX}task_1", kind="CLEANING",
            booking_id=f"{DEMO_PREFIX}book_1", unit_id=f"{DEMO_PREFIX}unit_canggu_1",
            due_at=YESTERDAY, priority="HIGH", status="OPEN", assignee="ops_demo",
            checklist=json.dumps(["ganti linen", "cek AC"]),
            idempotency_key=f"{DEMO_PREFIX}idem_task_1", created_at=LAST_WEEK,
            updated_at=LAST_WEEK,
        )
        self.insert(
            "ops_tasks", task_id=f"{DEMO_PREFIX}task_2", kind="CHECKIN_PREP",
            booking_id=f"{DEMO_PREFIX}book_1", unit_id=f"{DEMO_PREFIX}unit_canggu_1",
            due_at="2026-10-19T08:00:00+00:00", priority="NORMAL", status="OPEN",
            checklist=json.dumps(["kirim panduan tamu", "siapkan kunci"]),
            idempotency_key=f"{DEMO_PREFIX}idem_task_2", created_at=LAST_WEEK,
            updated_at=LAST_WEEK,
        )

        # An incident with a live regulatory deadline (shows the 72h SLA working).
        self.insert(
            "incidents", incident_id=f"{DEMO_PREFIX}inc_1", severity="SEV2",
            title="Kunci pintu utama macet", detected_at=YESTERDAY,
            detected_by="ops_demo", source="guest_message", playbook="VENUE_OUTAGE",
            status="MITIGATING", personal_data_involved=0,
            regulatory_notification_required=0, created_at=YESTERDAY,
        )
        self.insert(
            "incident_steps", step_id=f"{DEMO_PREFIX}step_1", incident_id=f"{DEMO_PREFIX}inc_1",
            seq=1, action="hubungi tukang kunci", actor="ops_demo", occurred_at=YESTERDAY,
            outcome="tukang datang 2 jam",
        )

        # A pending human decision: nothing outbound happens without this. The digest must be the
        # real one — `ApprovalService` recomputes it on every read and refuses the request when it
        # does not match, so a placeholder here makes the demo approval unapprovable *and* makes
        # `scheduler.py` refuse the whole tick with APPROVAL_TAMPERED. Found 2026-09-28 by the
        # fresh-database audit: the documented demo step could not run at all.
        self.insert(
            "approval_requests", approval_id=f"{DEMO_PREFIX}appr_1", subject_type="discount",
            subject_id=f"{DEMO_PREFIX}case_rahma", action="discount.override",
            risk_level="MEDIUM", requested_by="agent_demo", requested_role="reservation",
            requested_at=YESTERDAY, reason="tamu menginap 3 malam, minta diskon 8%",
            evidence_refs=json.dumps([f"{DEMO_PREFIX}msg_case_rahma"]),
            payload=json.dumps(DEMO_APPROVAL_PAYLOAD),
            payload_sha256=_payload_digest(DEMO_APPROVAL_PAYLOAD),
            money_minor=600_000, currency="IDR", required_role="owner",
            required_approvals=2, status="PENDING", expires_at=NEXT_WEEK,
            idempotency_key=f"{DEMO_PREFIX}idem_appr_1", created_at=YESTERDAY,
        )


def seed(db: Path, *, dry_run: bool = False) -> dict[str, int]:
    conn = connect_sqlite(str(db))
    try:
        offenders = _has_real_rows(conn)
        if offenders:
            raise SeedRefused(
                "database already holds non-demo rows ("
                + ", ".join(offenders)
                + "); seeding demo data into it would mix example guests with real ones"
            )
        demo = _has_demo_rows(conn)
        if demo:
            raise SeedRefused(
                "database already holds a demo dataset ("
                + ", ".join(demo)
                + "); seeding again would collide on identity — use a fresh path, or "
                "delete this demo database first"
            )
        conn.execute("BEGIN IMMEDIATE")
        seeder = DemoSeeder(conn)
        before = {table: _count(conn, table) for table in COUNTED_TABLES}
        seeder.inventory()
        seeder.cases()
        seeder.pipeline()
        created = {
            table: _count(conn, table) - before[table] for table in COUNTED_TABLES
        }
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        conn.execute("SELECT 1")
        return {key: value for key, value in created.items() if value}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="SQLite database to seed")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db)
    if db.exists() and not db.is_file():
        print(f"not a database file: {db}", file=sys.stderr)
        return 2
    # ``data/`` is not in git, so on a fresh checkout the documented command would fail
    # with "unable to open database file" before it wrote anything.
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        created = seed(db, dry_run=args.dry_run)
    except SeedRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"seed failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    label = "would create" if args.dry_run else "created"
    print(f"demo dataset {label} in {db}")
    for table, count in sorted(created.items()):
        print(f"  {table:<24} {count}")
    print()
    print("Semua identitas diawali 'demo_' dan nama properti memuat 'Contoh':")
    print("scripts/preflight.py menolak database produksi yang berisi baris ini.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

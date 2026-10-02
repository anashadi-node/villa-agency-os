"""Tests for the internal dashboard (``src/dashboard.py`` + ``templates/``).

The point of this file is to pin the seven invariants in the module docstring —
read-only data access, real authentication, deny-by-default pages, escaped
output, no raw PII without entitlement, no P3/P4 content, and idempotency before
writes — with assertions that would fail if the invariant were weakened.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import timedelta

import pytest

import dashboard as D
import roles as ROLES_MOD
from approvals import ROLE_RANK, ApprovalService
from audit import AuditLog
from primitives import IdFactory, to_iso
from schema_loader import connect_sqlite

REPO_ROOT = D.REPO_ROOT
TEMPLATES = REPO_ROOT / "templates"

TOKENS = {f"tok-{role}": (f"agent-{role}-1", role) for role in D.ROLES}

ROUTES = {
    "command_center": "/",
    "cases": "/cases",
    "inventory": "/inventory",
    "finance": "/finance",
    "operations": "/operations",
    "approvals": "/approvals",
    "properties": "/properties",
    "audit": "/audit",
}

#: Mirror of PAGE_ROLES as an independent expectation: if the module loosens a
#: page, this table does not move with it.
ENTITLED = {
    "command_center": set(D.ROLES),
    "cases": {"reservation", "operations", "owner", "auditor", "gm"},
    "inventory": {"reservation", "operations", "owner"},
    "finance": {"finance", "owner", "gm"},
    "operations": {"reservation", "operations", "owner"},
    "approvals": set(D.ROLES),
    "properties": set(D.ROLES),
    "audit": {"owner", "auditor"},
}
PAGE_ROLE_MATRIX = [(page, role) for page in ROUTES for role in D.ROLES]

RAW_EMAIL = "budi@example.com"
RAW_PHONE = "081234567890"
MASKED_EMAIL = "b***@example.com"
XSS = "<script>alert('xss')</script>"
XSS_ESCAPED = "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
IMG_XSS = "<img src=x onerror=alert(1)>"

SEED_CASE = "case-0001"
SEED_CASE_2 = "case-0002"
SEED_CASE_3 = "case-0003"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class Response:
    def __init__(self, status: int, text: str, headers: dict) -> None:
        self.status = status
        self.text = text
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Response {self.status} {len(self.text)}b>"


class Client:
    """A tiny HTTP client: the tests exercise a real socket, not `dispatch`."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        form: dict | None = None,
        content_type: str | None = None,
    ) -> Response:
        data = None
        headers: dict[str, str] = {}
        if token is not None:
            headers["X-Dashboard-Token"] = token
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = content_type or "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as raw:
                return Response(raw.status, raw.read().decode("utf-8"), dict(raw.headers))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read().decode("utf-8"), dict(exc.headers))

    def get(self, path: str, *, token: str | None = None) -> Response:
        return self.request("GET", path, token=token)

    def post(self, path: str, *, token: str | None, form: dict, content_type: str | None = None):
        return self.request("POST", path, token=token, form=form, content_type=content_type)


def table_counts(conn) -> dict:
    """Row count of every table — the read-only proof's baseline."""
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in names
    }


def kpi_values(html_text: str) -> dict:
    """Extract every Command Center counter from a rendered page."""
    return {
        key: value
        for key, value in re.findall(r'data-kpi="([a-z_]+)" data-value="(\d*)"', html_text)
    }


def kpi(html_text: str, key: str) -> str:
    found = kpi_values(html_text)
    assert key in found, f"KPI {key} tidak ada di halaman"
    return found[key]


def make_app(conn, clock, **options):
    settings = dict(
        clock=clock,
        actor="owner-sakti-1",
        actor_role="owner",
        tokens=TOKENS,
    )
    settings.update(options)
    return D.Dashboard(conn, **settings)


@pytest.fixture()
def app(conn, clock):
    return make_app(conn, clock)


@pytest.fixture()
def seeded(conn, clock):
    seed(conn, clock)
    return conn


@pytest.fixture()
def seeded_app(seeded, clock):
    return make_app(seeded, clock)


class DbReader:
    """A second connection to the same file: the test thread's own reader."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def scalar(self, sql, params=()):
        return self._conn.execute(sql, params).fetchone()[0]

    def close(self) -> None:
        self._conn.close()


class Live:
    """A dashboard served over HTTP by another thread, plus a reader connection."""

    def __init__(self, client: Client, db: DbReader) -> None:
        self.client = client
        self.db = db

    @property
    def base_url(self) -> str:
        return self.client.base_url

    def get(self, path, *, token=None):
        return self.client.get(path, token=token)

    def request(self, method, path, *, token=None, form=None, content_type=None):
        return self.client.request(method, path, token=token, form=form, content_type=content_type)

    def post(self, path, *, token, form, content_type=None):
        return self.client.post(path, token=token, form=form, content_type=content_type)

    def counts(self) -> dict:
        return table_counts(self.db)


@contextmanager
def live_dashboard(db_path, clock, *, seed_data: bool):
    setup = connect_sqlite(db_path)
    if seed_data:
        seed(setup, clock)
    setup.close()
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        conn = connect_sqlite(db_path)
        app = make_app(conn, clock)
        box["app"] = app
        box["server"] = D.serve(app.handler_class(), "127.0.0.1", 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(15), "server tidak pernah siap"
    server = box["server"]
    reader = DbReader(connect_sqlite(db_path))
    try:
        yield Live(Client(f"http://127.0.0.1:{server.server_address[1]}"), reader)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        reader.close()
        assert not thread.is_alive(), "server thread tidak berhenti"


@pytest.fixture(autouse=True)
def quiet_request_log(monkeypatch):
    """Keep the per-request stderr line out of the report (the PII test overrides)."""
    monkeypatch.setattr(D, "emit_dashboard_log", lambda line: None)


@pytest.fixture()
def http(tmp_path, clock):
    """A real HTTP server over an *empty* database, torn down after the test."""
    with live_dashboard(tmp_path / "empty.db", clock, seed_data=False) as live:
        yield live


@pytest.fixture()
def seeded_http(tmp_path, clock):
    """A real HTTP server over the seeded database, torn down after the test."""
    with live_dashboard(tmp_path / "seeded.db", clock, seed_data=True) as live:
        yield live


# --------------------------------------------------------------------------- #
# Seed data: one of everything the dashboard displays
# --------------------------------------------------------------------------- #
def seed(conn, clock) -> None:
    now = clock.now()
    iso = lambda **delta: to_iso(now + timedelta(**delta))  # noqa: E731 - test helper
    fp = lambda char: char * 64  # noqa: E731

    conn.execute(
        "INSERT INTO properties (property_id, name, property_type, region, province, timezone,"
        " status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-1", "Villa Anantara", "villa", "Ubud", "Bali", "Asia/Makassar",
         "SELLABLE", "VERIFIED", iso(hours=-20), iso(days=-30), iso(hours=-1)),
    )
    conn.execute(
        "INSERT INTO properties (property_id, name, property_type, region, province, timezone,"
        " status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("prop-2", f"Villa {XSS}", "guesthouse", "Sleman", "DI Yogyakarta", "Asia/Jakarta",
         "SELLABLE_WITH_REVIEW", "DRAFT", None, iso(days=-10), iso(hours=-2)),
    )
    units = [
        ("unit-1", "prop-1", f"Unit Satu {IMG_XSS}", "entire_villa", 3, 2, 2, 6, "ACTIVE"),
        ("unit-2", "prop-1", "Unit Dua", "suite", 2, 1, 2, 3, "ACTIVE"),
        ("unit-3", "prop-2", "Unit Tiga", "room", 1, 1, 1, 2, "MAINTENANCE"),
    ]
    for unit in units:
        conn.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            unit + (iso(days=-30), iso(hours=-3)),
        )
    conn.execute(
        "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
        " effective_from, effective_to, currency, deposit_pct, service_fee_pct, tax_pbjt_pct,"
        " max_discount_pct, quote_valid_hours, hold_ttl_minutes, max_hold_extensions, payload,"
        " fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("policy-1", "prop-1", "property", 1, iso(days=-60), iso(days=300), "IDR",
         "30", "5", "10", "10", 48, 120, 1, "{}", fp("a"), "doc-policy-1", "owner-sakti-1",
         iso(days=-60)),
    )
    conn.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level, source_type,"
        " source_ref, captured_at, valid_until, result, can_quote, can_hold, can_confirm,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("evidence-1", "unit-1", "A", "property_api", "src-1", iso(hours=-2), iso(days=2),
         "AVAILABLE", 1, 1, 1, iso(hours=-2)),
    )
    bookings = [
        ("booking-1", "prop-1", "unit-1", "2026-09-21", "2026-09-24", 3, "CONFIRMED",
         "guest-ref-1", 2, 0, 0, "IDR", "policy-1", fp("b"), 4_500_000, 1_000_000, 0,
         "DEPOSIT_CLEARED", "idem-booking-1", iso(days=-3), iso(hours=-1)),
        ("booking-2", "prop-1", "unit-2", "2026-10-01", "2026-10-05", 4, "CANCELLED",
         "guest-ref-2", 2, 1, 0, "IDR", "policy-1", fp("b"), 1_000_000, 0, 0,
         "UNPAID", "idem-booking-2", iso(days=-5), iso(days=-1)),
    ]
    for booking in bookings:
        conn.execute(
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, total_minor, amount_paid_minor, security_deposit_minor,"
            " payment_status, idempotency_key, version_lock, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            booking[:19] + (1,) + booking[19:],
        )
    holds = [
        ("hold-1", "unit-2", "prop-1", "2026-09-22", "2026-09-25", 3, "CONFIRMED", "guest-ref-2",
         "idem-hold-1", iso(hours=-1), iso(hours=12)),
        ("hold-2", "unit-3", "prop-2", "2026-09-23", "2026-09-27", 4, "CONFIRMED", "guest-ref-3",
         "idem-hold-2", iso(hours=-1), iso(days=1, hours=2)),
    ]
    for hold in holds:
        conn.execute(
            "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
            " status, guest_ref, policy_version_id, evidence_id, idempotency_key, created_at,"
            " expires_at, expires_timezone) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            hold[:8] + ("policy-1", "evidence-1") + hold[8:] + ("Asia/Makassar",),
        )

    cases = [
        (SEED_CASE, "email", "guest-ref-1", "NEW", "HIGH", "sla-v1",
         iso(hours=1), iso(hours=5), "idem-case-1", iso(hours=-2), iso(hours=-1), None, None, None),
        (SEED_CASE_2, "website", f"guest-ref-{XSS}", "CLOSED_WON", "NORMAL", "sla-v1",
         iso(hours=-25), iso(hours=-20), "idem-case-2", iso(days=-3),
         iso(hours=-1), "booking-1", iso(hours=-1), "tamu memesan langsung"),
        (SEED_CASE_3, "phone", "guest-ref-3", "ESCALATED", "URGENT", "sla-v1",
         iso(hours=-1), iso(hours=2), "idem-case-3", iso(hours=-3), iso(hours=-2), None, None, None),
    ]
    for case in cases:
        conn.execute(
            "INSERT INTO cases (case_id, channel, guest_ref, status, priority, sla_policy,"
            " first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at,"
            " booking_id, closed_at, close_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            case,
        )
    messages = [
        ("msg-1", SEED_CASE, "INBOUND", "email", "guest-ref-1", iso(hours=-1), "P2",
         f"{XSS} Nama tamu: Budi Santoso, email {RAW_EMAIL}, telepon {RAW_PHONE}",
         fp("c"), 1),
        ("msg-2", SEED_CASE, "INBOUND", "email", "guest-ref-1", iso(hours=-2), "P3", None, fp("d"), 1),
    ]
    for message in messages:
        conn.execute(
            "INSERT INTO case_messages (case_message_id, case_id, direction, channel, sender_ref,"
            " received_at, content_class, body_redacted, body_sha256, attachment_count, untrusted,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            message[:9] + (message[9], message[5]),
        )
    events = [
        ("evt-1", SEED_CASE, 1, "CREATED", None, "NEW", iso(hours=-2), None),
        ("evt-2", SEED_CASE, 2, "STATUS_CHANGED", "NEW", "TRIAGED", iso(hours=-1), IMG_XSS),
    ]
    for event in events:
        conn.execute(
            "INSERT INTO case_events (case_event_id, case_id, seq, kind, from_status, to_status,"
            " actor, actor_role, command_id, reason, refs, occurred_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            event[:6] + ("agent-reservation-1", "agent", f"cmd-{event[0]}", event[6], "[]",
                         event[7] or event[5]),
        )
    conn.execute(
        "INSERT INTO sla_breaches (breach_id, subject_type, subject_id, case_id, kind, due_at,"
        " detected_at, breach_minutes, owner_actor, status, resolution_note, resolved_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("breach-1", "case", SEED_CASE, SEED_CASE, "FIRST_RESPONSE", iso(hours=-3), iso(hours=-2),
         60, "agent-reservation-1", "OPEN", None, None, iso(hours=-2)),
    )
    conn.execute(
        "INSERT INTO sla_breaches (breach_id, subject_type, subject_id, case_id, kind, due_at,"
        " detected_at, breach_minutes, owner_actor, status, resolution_note, resolved_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("breach-2", "case", SEED_CASE, SEED_CASE, "QUOTE", iso(hours=-5), iso(hours=-4), 45,
         "agent-reservation-1", "RESOLVED",
         f"dihubungi via {RAW_EMAIL} / {RAW_PHONE} lalu dikirim quote", iso(hours=-1),
         iso(hours=-4)),
    )
    conn.execute(
        "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
        " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
        " status, payment_status, issued_at, due_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("invoice-1", "INV-2026-0001", "booking-1", fp("b"), "IDR", 4_000_000, 400_000, 100_000,
         4_500_000, 0, "ISSUED", "UNPAID", iso(days=-1), iso(days=2), iso(days=-1)),
    )
    conn.execute(
        "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
        " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
        " status, payment_status, issued_at, due_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("invoice-2", "INV-2026-0002", "booking-2", fp("b"), "IDR", 700_000, 200_000, 100_000,
         1_000_000, 1_000_000, "ISSUED", "PAID", iso(days=-2), iso(hours=-3), iso(days=-2)),
    )
    payments = [
        ("payment-1", "invoice-1", "midtrans", "intent-aaa111", "HOSTED_PAGE", "IDR", 1_000_000,
         "SUCCEEDED", iso(hours=-3), iso(hours=-2), "settlement"),
        ("payment-2", "invoice-2", "xendit", "intent-bbb222", "BANK_TRANSFER", "IDR", 1_000_000,
         "SUCCEEDED", iso(hours=-4), iso(hours=-1), "settlement"),
    ]
    for payment in payments:
        conn.execute(
            "INSERT INTO payments (payment_id, invoice_id, provider, provider_intent_id, method,"
            " currency, amount_minor, status, created_at, settled_at, provider_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payment,
        )
    conn.execute(
        "INSERT INTO payment_events (event_id, payment_id, provider, provider_event_id,"
        " event_type, signature_verified, applied, payload_sha256, received_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pevent-1", "payment-1", "midtrans", "provider-evt-1", "settlement", 1, 1, fp("e"),
         iso(hours=-2), iso(hours=-2)),
    )
    conn.execute(
        "INSERT INTO payment_events (event_id, payment_id, provider, provider_event_id,"
        " event_type, signature_verified, applied, rejection_reason, payload_sha256, received_at,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("pevent-2", "payment-2", "xendit", "provider-evt-2", "settlement.webhook", 0, 0,
         "signature tidak valid", fp("f"), iso(hours=-1), iso(hours=-1)),
    )
    conn.execute(
        "INSERT INTO reconciliation_cases (recon_id, kind, payment_id, invoice_id, detected_at,"
        " due_at, status, owner_actor, resolution_note, resolved_at, details, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("recon-1", "UNMATCHED_PAYMENT", "payment-2", "invoice-2", iso(hours=-1), iso(days=1),
         "OPEN", "agent-finance-1", None, None,
         json.dumps({"note": f"hubungi tamu {RAW_EMAIL}"}), iso(hours=-1)),
    )
    conn.execute(
        "INSERT INTO reconciliation_cases (recon_id, kind, payment_id, invoice_id, detected_at,"
        " due_at, status, owner_actor, resolution_note, resolved_at, details, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("recon-2", "AMOUNT_MISMATCH", "payment-1", "invoice-1", iso(hours=-5), iso(hours=-2),
         "RESOLVED", "agent-finance-1", "dicocokkan manual", iso(hours=-2), "{}", iso(hours=-5)),
    )
    tasks = [
        ("task-1", "CLEANING", "unit-1", "booking-1", None, iso(hours=-2), "HIGH", "OPEN",
         "agent-ops-1", None),
        ("task-2", "CHECKIN_PREP", "unit-2", "booking-1", None, iso(hours=3), "NORMAL", "OPEN",
         "agent-ops-1", None),
        ("task-3", "MAINTENANCE", "unit-3", None, None, iso(hours=-1), "URGENT", "BLOCKED",
         "agent-ops-1", f"tunggu vendor {RAW_EMAIL} / {RAW_PHONE}"),
        ("task-4", "CLEANING", "unit-1", None, None, iso(hours=-1), "LOW", "DONE", "agent-ops-1",
         None),
    ]
    for task in tasks:
        conn.execute(
            "INSERT INTO ops_tasks (task_id, kind, unit_id, booking_id, case_id, due_at, priority,"
            " status, assignee, blocked_reason, idempotency_key, created_at, updated_at,"
            " completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            task + (f"idem-{task[0]}", iso(hours=-6), iso(hours=-1),
                    iso(hours=-1) if task[7] == "DONE" else None),
        )
    conn.execute(
        "INSERT INTO incidents (incident_id, severity, title, detected_at, detected_by, source,"
        " playbook, affected_refs, personal_data_involved, regulatory_notification_required,"
        " notice_due_at, notified_at, status, summary, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("incident-1", "SEV2", f"Kebocoran data intake {XSS}", iso(hours=-5), "agent-ops-1",
         "monitoring", "PB-DATA-1", "[]", 1, 1, iso(hours=-4), None, "OPEN",
         f"Tamu melapor; kontak {RAW_EMAIL} / {RAW_PHONE}", iso(hours=-5)),
    )
    conn.execute(
        "INSERT INTO incidents (incident_id, severity, title, detected_at, detected_by, source,"
        " playbook, affected_refs, personal_data_involved, regulatory_notification_required,"
        " status, summary, resolved_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("incident-2", "SEV4", "Lampu taman mati", iso(hours=-30), "agent-ops-1", "ops",
         "PB-OPS-1", "[]", 0, 0, "RESOLVED", "sudah diganti dua unit", iso(hours=-20),
         iso(hours=-30)),
    )

    audit = AuditLog(conn, clock=clock, ids=IdFactory(seed="seed-audit"))
    audit.append(
        action="case.created", entity_type="case", entity_id=SEED_CASE,
        actor="agent-reservation-1", actor_role="agent", after={"status": "NEW"},
    )
    audit.append(
        action="case.status_changed", entity_type="case", entity_id=SEED_CASE,
        actor="agent-reservation-1", actor_role="agent", reason=f"triase {XSS}",
        before={"status": "NEW"}, after={"status": "TRIAGED"},
    )
    audit.append(
        action="booking.confirmed", entity_type="booking", entity_id="booking-1",
        actor="agent-reservation-1", actor_role="agent", after={"status": "CONFIRMED"},
    )

    approvals = ApprovalService(conn, clock=clock, ids=IdFactory(seed="seed-approval"))
    approvals.request(
        subject_type="case", subject_id=SEED_CASE, action="diskon 10 persen",
        requested_by="agent-reservation-1", requested_role="agent",
        reason="tamu repeat ingin diskon 10 persen", required_role="accountant",
        risk_level="LOW", idempotency_key="idem-approval-1",
    )
    decided = approvals.request(
        subject_type="case", subject_id=SEED_CASE_3, action="relokasi tamu",
        requested_by="agent-ops-1", requested_role="manager",
        reason="unit rusak, tamu dipindahkan", required_role="accountant",
        risk_level="MEDIUM", idempotency_key="idem-approval-2",
    )
    approvals.decide(
        decided.approval_id, approver="owner-sakti-1", approver_role="owner",
        decision="APPROVE", note="disetujui owner",
    )


# --------------------------------------------------------------------------- #
# 1. KPI counters — empty database
# --------------------------------------------------------------------------- #
def render(app, page: str, role: str) -> str:
    principal = app.tokens[f"tok-{role}"]
    return app.dispatch(
        D._Request(method="GET", path=ROUTES[page], query={}, form={}, principal=principal)
    ).body


def test_kpi_empty_db_every_counter_is_zero(app):
    page = render(app, "command_center", "owner")
    values = kpi_values(page)
    assert set(D.KPI_KEYS) <= set(values)
    assert all(value == "0" for value in values.values()), values
    assert 'data-chain="OK"' in page


def test_kpi_empty_db_marks_the_database_as_empty(app):
    page = render(app, "command_center", "owner")
    assert 'data-empty="1"' in page
    assert "Database kosong" in page


def test_kpi_empty_db_hides_finance_counters_from_reservation(app):
    page = render(app, "command_center", "reservation")
    for key in D.FINANCE_KPI_KEYS:
        assert f'data-kpi="{key}" data-value="" data-hidden="1"' in page
    assert "tidak tersedia untuk peran ini" in page


def test_kpi_empty_db_shows_finance_counters_to_owner(app):
    page = render(app, "command_center", "owner")
    for key in D.FINANCE_KPI_KEYS:
        assert f'data-kpi="{key}" data-value="0"' in page


def test_empty_db_pages_render_an_explicit_empty_marker(app):
    for page, role in (("cases", "reservation"), ("inventory", "operations"),
                       ("finance", "finance"), ("operations", "operations"),
                       ("approvals", "owner"), ("properties", "auditor"),
                       ("audit", "auditor")):
        body = render(app, page, role)
        assert 'data-empty="1"' in body, page
        assert "Tidak ada data:" in body, page


def test_empty_db_audit_page_reports_healthy_chain_without_anchor(app):
    body = render(app, "audit", "auditor")
    assert 'data-chain="OK"' in body
    assert "belum ada anker" in body
    assert "tidak (tidak ada anker)" in body


# --------------------------------------------------------------------------- #
# 2. KPI counters — populated database
# --------------------------------------------------------------------------- #
def test_kpi_seeded_cases_by_status(seeded_app):
    page = render(seeded_app, "command_center", "owner")
    assert kpi(page, "cases_by_status") == "3"
    assert "NEW" in page and "CLOSED_WON" in page and "ESCALATED" in page


def test_kpi_seeded_open_sla_breaches_counts_only_open(seeded_app):
    assert kpi(render(seeded_app, "command_center", "owner"), "open_sla_breaches") == "1"


def test_kpi_seeded_pending_approvals_excludes_decided(seeded_app):
    assert kpi(render(seeded_app, "command_center", "owner"), "pending_approvals") == "1"


def test_kpi_seeded_holds_expiring_today_uses_the_injected_clock(seeded_app, seeded):
    assert kpi(render(seeded_app, "command_center", "owner"), "holds_expiring_today") == "1"
    # a hold that expired yesterday must not be counted as "today"
    seeded.execute(
        "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights, status,"
        " guest_ref, policy_version_id, evidence_id, idempotency_key, created_at, expires_at,"
        " expires_timezone) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("hold-3", "unit-1", "prop-1", "2026-09-21", "2026-09-24", 3, "CONFIRMED",
         "guest-ref-9", "policy-1", "evidence-1", "idem-hold-3",
         "2026-09-20T07:00:00+00:00", "2026-09-20T12:00:00+00:00", "Asia/Makassar"),
    )
    assert kpi(render(seeded_app, "command_center", "owner"), "holds_expiring_today") == "1"


def test_kpi_seeded_unpaid_invoices_excludes_paid(seeded_app):
    assert kpi(render(seeded_app, "command_center", "finance"), "unpaid_invoices") == "1"


def test_kpi_seeded_unmatched_payments_ignores_applied_events(seeded_app):
    assert kpi(render(seeded_app, "command_center", "finance"), "unmatched_payments") == "1"


def test_kpi_seeded_late_ops_tasks_counts_overdue_active_only(seeded_app):
    # task-1 (OPEN, -2h) and task-3 (BLOCKED, -1h); task-4 is DONE, task-2 is future
    assert kpi(render(seeded_app, "command_center", "owner"), "late_ops_tasks") == "2"


def test_kpi_seeded_open_incidents_counts_only_open(seeded_app):
    assert kpi(render(seeded_app, "command_center", "owner"), "open_incidents") == "1"


def test_kpi_seeded_audit_chain_is_healthy_and_counted(seeded_app):
    page = render(seeded_app, "command_center", "owner")
    assert kpi(page, "audit_chain") == "3"
    assert 'data-chain="OK"' in page


def test_kpi_seeded_operations_page_reports_late_tasks(seeded_app):
    body = render(seeded_app, "operations", "operations")
    assert 'data-late-tasks="2"' in body


def test_finance_page_shows_invoice_payment_and_reconciliation(seeded_app):
    body = render(seeded_app, "finance", "finance")
    assert "INV-2026-0001" in body
    assert "payment-2" in body
    assert "UNMATCHED_PAYMENT" in body
    assert "IDR 4,500,000" in body


def test_inventory_calendar_marks_booked_hold_and_unknown(seeded_app):
    body = render(seeded_app, "inventory", "owner")
    assert 'data-unit="unit-1" data-day="2026-09-21" data-state="BOOKED"' in body
    assert 'data-unit="unit-1" data-day="2026-09-25" data-state="UNKNOWN"' in body
    assert 'data-unit="unit-2" data-day="2026-09-22" data-state="HOLD"' in body
    assert 'data-unit="unit-2" data-day="2026-09-21" data-state="UNKNOWN"' in body


def test_inventory_calendar_ignores_cancelled_bookings(seeded_app):
    body = render(seeded_app, "inventory", "owner")
    # booking-2 on unit-2 is CANCELLED, so its October dates never appear BOOKED
    assert 'data-day="2026-10-01"' not in body
    assert 'data-unit="unit-2" data-day="2026-09-26" data-state="UNKNOWN"' in body


def test_case_detail_timeline_and_redacted_messages(seeded_app):
    body = render(seeded_app, "command_center", "owner")  # sanity: app renders
    assert body
    principal = seeded_app.tokens["tok-owner"]
    detail = seeded_app.dispatch(
        D._Request(method="GET", path=f"/cases/{SEED_CASE}", query={}, form={}, principal=principal)
    ).body
    assert 'data-case-id="%s"' % SEED_CASE in detail
    assert "CREATED" in detail and "STATUS_CHANGED" in detail
    assert "P3" in detail
    assert "isi tidak disimpan" in detail


def test_case_detail_404_for_unknown_case(seeded_app):
    with pytest.raises(D.NotFound):
        seeded_app.view_case_detail("case-tidak-ada", seeded_app.tokens["tok-owner"])


def test_case_detail_rejects_non_reference_id(seeded_app):
    with pytest.raises(D.NotFound):
        seeded_app.view_case_detail("../../etc/passwd", seeded_app.tokens["tok-owner"])


# --------------------------------------------------------------------------- #
# 3. Authentication and authorisation over real HTTP
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page,role", PAGE_ROLE_MATRIX)
def test_every_route_status_matches_the_role_matrix(seeded_http, page, role):
    response = seeded_http.get(ROUTES[page], token=f"tok-{role}")
    if role in ENTITLED[page]:
        assert response.status == 200, (page, role, response.text[:400])
    else:
        assert response.status == 403, (page, role, response.text[:400])
        assert "FORBIDDEN" in response.text


@pytest.mark.parametrize("role", D.ROLES)
def test_case_detail_route_is_open_to_entitled_roles(seeded_http, role):
    response = seeded_http.get(f"/cases/{SEED_CASE}", token=f"tok-{role}")
    assert response.status == (200 if role in ENTITLED["cases"] else 403)


@pytest.mark.parametrize("path", list(ROUTES.values()) + [f"/cases/{SEED_CASE}", "/cases/xyz"])
def test_401_without_any_identity(http, path):
    response = http.get(path)
    assert response.status == 401
    assert "UNAUTHENTICATED" in response.text
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_401_with_unknown_token(http):
    response = http.get("/", token="tok-tebakan")
    assert response.status == 401


def test_401_on_post_without_identity(http):
    response = http.post("/approvals", token=None, form={"idempotency_key": "idem-x-0001"})
    assert response.status == 401


def test_bearer_authorization_header_is_accepted(http):
    client = Client(http.base_url)
    assert client.request("GET", "/").status == 401  # no header at all
    import urllib.request as _u

    request = _u.Request(
        http.base_url + "/", headers={"Authorization": "Bearer tok-owner"}
    )
    with _u.urlopen(request, timeout=15) as raw:
        assert raw.status == 200
        assert b"Command Center" in raw.read()
    wrong = _u.Request(
        http.base_url + "/", headers={"Authorization": "Bearer tok-bukan-owner"}
    )
    try:
        _u.urlopen(wrong, timeout=15)
        raise AssertionError("token palsu harus ditolak")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_403_body_does_not_leak_the_page_content(seeded_http):
    response = seeded_http.get("/finance", token="tok-reservation")
    assert response.status == 403
    assert "INV-2026-0001" not in response.text
    assert "payment-1" not in response.text


def test_navigation_does_not_advertise_forbidden_pages(seeded_http):
    body = seeded_http.get("/", token="tok-reservation").text
    assert 'href="/finance"' not in body
    assert 'href="/audit"' not in body
    assert 'data-nav-denied="finance"' in body


def test_unknown_route_is_404_for_an_authenticated_role(http):
    response = http.get("/tidak-ada", token="tok-owner")
    assert response.status == 404


def test_case_detail_id_is_taken_from_the_last_segment(seeded_http):
    response = seeded_http.get(f"/cases/{SEED_CASE}", token="tok-owner")
    assert response.status == 200
    assert f'data-case-id="{SEED_CASE}"' in response.text
    # the id is the *last* segment, so anything before it is ignored
    ignored = seeded_http.get(f"/cases/abaikan/{SEED_CASE}", token="tok-owner")
    assert ignored.status == 200
    assert f'data-case-id="{SEED_CASE}"' in ignored.text
    # an unknown last segment is a 404, not a silent fallback
    assert seeded_http.get("/cases/abaikan/tidak-ada", token="tok-owner").status == 404


def test_unsupported_http_verb_is_405(http):
    response = http.request("HEAD", "/", token="tok-owner")
    assert response.status == 405


# --------------------------------------------------------------------------- #
# 4. POST: the approval queue is the only writer
# --------------------------------------------------------------------------- #
REQUEST_FORM = {
    "subject_type": "case",
    "subject_id": SEED_CASE,
    "action": "setujui diskon 10 persen",
    "reason": "tamu repeat meminta diskon sepuluh persen",
    "risk_level": "LOW",
    "required_role": "accountant",
    "required_approvals": "1",
    "idempotency_key": "idem-dash-0001",
}


def test_post_without_idempotency_key_is_refused_and_writes_nothing(seeded_http):
    before = seeded_http.counts()
    form = dict(REQUEST_FORM)
    form.pop("idempotency_key")
    response = seeded_http.post("/approvals", token="tok-finance", form=form)
    assert response.status == 400
    assert "MISSING_IDEMPOTENCY_KEY" in response.text
    assert seeded_http.counts() == before


def test_post_with_short_idempotency_key_is_refused(seeded_http):
    before = seeded_http.counts()
    form = dict(REQUEST_FORM, idempotency_key="pendek")
    response = seeded_http.post("/approvals", token="tok-finance", form=form)
    assert response.status == 400
    assert "MISSING_IDEMPOTENCY_KEY" in response.text
    assert seeded_http.counts() == before


def test_post_creates_an_approval_through_the_service(seeded_http):
    before = seeded_http.counts()
    response = seeded_http.post("/approvals", token="tok-finance", form=REQUEST_FORM)
    assert response.status == 200
    assert 'data-banner="approval-requested"' in response.text
    after = seeded_http.counts()
    assert after["approval_requests"] == before["approval_requests"] + 1
    assert after["approval_decisions"] == before["approval_decisions"]
    row = seeded_http.db.execute(
        "SELECT requested_by, requested_role, action FROM approval_requests"
        " WHERE idempotency_key = ?",
        (REQUEST_FORM["idempotency_key"],),
    ).fetchone()
    assert row["requested_by"] == "agent-finance-1"
    # The canonical id of ADR-0035 (rank 3, the same tier ``accountant`` held).
    assert row["requested_role"] == "finance_manager"
    assert row["action"] == REQUEST_FORM["action"]


def test_post_ignores_identity_supplied_in_the_body(seeded_http):
    form = dict(REQUEST_FORM, idempotency_key="idem-dash-0002")
    form.update({"requested_by": "system:kernel", "requested_role": "owner",
                 "actor": "owner-sakti-1"})
    response = seeded_http.post("/approvals", token="tok-reservation", form=form)
    assert response.status == 200
    row = seeded_http.db.execute(
        "SELECT requested_by, requested_role FROM approval_requests WHERE idempotency_key = ?",
        ("idem-dash-0002",),
    ).fetchone()
    assert row["requested_by"] == "agent-reservation-1"
    assert row["requested_role"] == "reservation_agent"
    assert "system:kernel" not in response.text


def test_post_replay_with_the_same_key_creates_one_row(seeded_http):
    before = seeded_http.counts()
    first = seeded_http.post("/approvals", token="tok-finance", form=REQUEST_FORM)
    second = seeded_http.post("/approvals", token="tok-finance", form=REQUEST_FORM)
    assert first.status == second.status == 200
    assert _banner_attr(first.text, "data-approval-id") == _banner_attr(
        second.text, "data-approval-id"
    )
    assert seeded_http.counts()["approval_requests"] == before["approval_requests"] + 1


def test_post_replay_with_a_different_request_is_a_conflict(seeded_http):
    seeded_http.post("/approvals", token="tok-finance", form=REQUEST_FORM)
    before = seeded_http.counts()
    changed = dict(REQUEST_FORM, reason="alasan yang sama sekali berbeda")
    response = seeded_http.post("/approvals", token="tok-finance", form=changed)
    assert response.status == 400
    assert "IDEMPOTENCY_CONFLICT" in response.text
    assert seeded_http.counts() == before


def _banner_attr(html_text: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]*)"', html_text)
    assert match, f'{name} tidak ada di banner'
    return match.group(1)


def test_post_decide_records_a_decision_through_the_service(seeded_http):
    created = seeded_http.post("/approvals", token="tok-reservation", form=REQUEST_FORM)
    approval_id = _banner_attr(created.text, "data-approval-id")
    before = seeded_http.counts()
    response = seeded_http.post(
        f"/approvals/{approval_id}/decide",
        token="tok-finance",
        form={"decision": "APPROVE", "note": "disetujui finance",
              "idempotency_key": "idem-dash-decide-1"},
    )
    assert response.status == 200
    assert 'data-banner="approval-decided"' in response.text
    assert 'data-status="APPROVED"' in response.text
    after = seeded_http.counts()
    assert after["approval_decisions"] == before["approval_decisions"] + 1
    row = seeded_http.db.execute(
        "SELECT approver, approver_role, decision FROM approval_decisions"
        " WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    assert row["approver"] == "agent-finance-1"
    assert row["approver_role"] == "finance_manager"
    assert row["decision"] == "APPROVE"
    status = seeded_http.db.execute(
        "SELECT status FROM approval_requests WHERE approval_id = ?", (approval_id,)
    ).fetchone()[0]
    assert status == "APPROVED"


def test_post_decide_without_idempotency_key_writes_nothing(seeded_http):
    row = seeded_http.db.execute(
        "SELECT approval_id FROM approval_requests WHERE status = 'PENDING' LIMIT 1"
    ).fetchone()
    approval_id = str(row[0])
    before = seeded_http.counts()
    response = seeded_http.post(
        f"/approvals/{approval_id}/decide",
        token="tok-finance",
        form={"decision": "APPROVE", "note": "tanpa key"},
    )
    assert response.status == 400
    assert "MISSING_IDEMPOTENCY_KEY" in response.text
    assert seeded_http.counts() == before


def test_post_decide_with_bad_decision_writes_nothing(seeded_http):
    row = seeded_http.db.execute(
        "SELECT approval_id FROM approval_requests WHERE status = 'PENDING' LIMIT 1"
    ).fetchone()
    before = seeded_http.counts()
    response = seeded_http.post(
        f"/approvals/{row[0]}/decide",
        token="tok-finance",
        form={"decision": "SETUJU", "idempotency_key": "idem-dash-bad-1"},
    )
    assert response.status == 400
    assert seeded_http.counts() == before


def test_auditor_may_not_write_anything(seeded_http):
    before = seeded_http.counts()
    response = seeded_http.post(
        "/approvals", token="tok-auditor", form=dict(REQUEST_FORM, idempotency_key="idem-aud-0001")
    )
    assert response.status == 403
    assert seeded_http.counts() == before


def test_post_to_a_read_only_route_is_405(http):
    response = http.post("/cases", token="tok-owner", form={"idempotency_key": "idem-x-0001"})
    assert response.status == 405
    assert "METHOD_NOT_ALLOWED" in response.text


def test_post_with_wrong_content_type_is_refused(seeded_http):
    before = seeded_http.counts()
    response = seeded_http.post(
        "/approvals",
        token="tok-finance",
        form=REQUEST_FORM,
        content_type="application/json",
    )
    assert response.status == 400
    assert seeded_http.counts() == before


def test_approval_page_hides_the_write_form_from_the_auditor(seeded_http):
    body = seeded_http.get("/approvals", token="tok-auditor").text
    assert 'data-write-form="1"' not in body
    assert "hanya boleh membaca" in body
    owner_body = seeded_http.get("/approvals", token="tok-owner").text
    assert 'data-write-form="1"' in owner_body


# --------------------------------------------------------------------------- #
# 5. Escaping (XSS) and redaction (PII)
# --------------------------------------------------------------------------- #
XSS_PAGES = {
    ("cases", "reservation"): XSS,
    ("properties", "auditor"): XSS,
    ("operations", "operations"): XSS,
    ("audit", "auditor"): XSS,
}


@pytest.mark.parametrize("page,role", sorted(XSS_PAGES))
def test_xss_payloads_are_escaped_on_every_page_that_shows_them(seeded_app, page, role):
    body = render(seeded_app, page, role)
    # No carve-out: the payload must be escaped on every page that shows it.
    assert "<script>" not in body
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_xss_guest_name_in_a_message_is_escaped_on_the_case_page(seeded_app):
    principal = seeded_app.tokens["tok-reservation"]
    body = seeded_app.dispatch(
        D._Request(method="GET", path=f"/cases/{SEED_CASE}", query={}, form={}, principal=principal)
    ).body
    assert XSS_ESCAPED in body
    assert XSS not in body
    assert "Budi Santoso" in body  # redact_pii masks identifiers, not the whole text


def test_xss_property_and_unit_names_are_escaped(seeded_app):
    body = render(seeded_app, "properties", "owner")
    assert XSS not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


def test_xss_payload_never_reaches_any_page_unencoded(seeded_app):
    for page, role in PAGE_ROLE_MATRIX:
        if role not in ENTITLED[page]:
            continue
        body = render(seeded_app, page, role)
        assert "<script>" not in body, (page, role)
        assert "<img src=x" not in body, (page, role)


def test_no_raw_pii_on_pages_whose_role_is_not_entitled(seeded_http):
    paths = list(ROUTES.values()) + [f"/cases/{SEED_CASE}"]
    checked = 0
    for role in D.ROLES:
        if role in D.PII_ROLES:
            continue  # entitled roles are asserted separately, below
        for path in paths:
            response = seeded_http.get(path, token=f"tok-{role}")
            assert response.status in (200, 403), (role, path)
            assert RAW_EMAIL not in response.text, (role, path)
            assert RAW_PHONE not in response.text, (role, path)
            checked += 1
    assert checked >= 15  # the crawl really covered the matrix
    assert D.PII_ROLES == ("operations", "finance")


def test_entitled_role_sees_the_contact_it_is_allowed_to_see(seeded_http):
    body = seeded_http.get(f"/cases/{SEED_CASE}", token="tok-operations").text
    assert RAW_EMAIL in body
    assert RAW_PHONE in body
    assert MASKED_EMAIL not in body


def test_unentitled_role_sees_the_masked_form_instead(seeded_http):
    body = seeded_http.get(f"/cases/{SEED_CASE}", token="tok-owner").text
    assert MASKED_EMAIL in body
    assert "[TELEPON_DIHAPUS]" in body
    assert RAW_EMAIL not in body
    assert RAW_PHONE not in body


def test_pii_is_absent_from_error_pages(seeded_http):
    response = seeded_http.request(
        "POST", "/approvals", token="tok-finance",
        form={"subject_type": "case", "subject_id": SEED_CASE, "action": "x"},
    )
    assert response.status == 400
    assert RAW_EMAIL not in response.text
    assert RAW_PHONE not in response.text


def test_query_string_pii_is_never_logged(seeded_http, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(D, "emit_dashboard_log", lines.append)
    response = seeded_http.get(f"/?guest={RAW_EMAIL}&phone={RAW_PHONE}", token="tok-owner")
    assert response.status == 200
    assert lines, "jalur log tidak terpakai"
    logged = "".join(lines)
    assert RAW_EMAIL not in logged
    assert RAW_PHONE not in logged
    assert any("dashboard GET / ->" in line for line in lines)


def test_xss_in_an_approval_reason_is_escaped(seeded_app, seeded):
    now = seeded_app.clock.now()
    seeded.execute(
        "INSERT INTO approval_requests (approval_id, subject_type, subject_id, action, risk_level,"
        " requested_by, requested_role, requested_at, reason, evidence_refs, payload,"
        " payload_sha256, required_role, required_approvals, status, expires_at, idempotency_key,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("approval-xss", "case", SEED_CASE, "aksi mencurigakan", "LOW", "agent-ops-1",
         "manager", to_iso(now), f"{XSS} alasan yang sangat panjang", "[]", "{}", "0" * 64,
         "owner", 1, "PENDING", to_iso(now + timedelta(hours=1)), "idem-approval-xss",
         to_iso(now)),
    )
    body = render(seeded_app, "approvals", "owner")
    assert "approval-xss" in body  # the row really rendered
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_session_tokens_never_appear_in_a_rendered_page(seeded_app):
    for page, role in PAGE_ROLE_MATRIX:
        if role not in ENTITLED[page]:
            continue
        body = render(seeded_app, page, role)
        assert "tok-" not in body, (page, role)


def test_error_page_does_not_echo_identifiers_from_the_request(http):
    response = http.get("/tidak-ada?email=rahasia@example.com", token="tok-owner")
    assert response.status == 404
    assert "rahasia@example.com" not in response.text


# --------------------------------------------------------------------------- #
# 6. Read-only proof
# --------------------------------------------------------------------------- #
def test_every_get_leaves_every_table_unchanged(seeded_http):
    before = seeded_http.counts()
    paths = list(ROUTES.values()) + [f"/cases/{SEED_CASE}", f"/cases/{SEED_CASE}/timeline"]
    for role in D.ROLES:
        for path in paths:
            assert seeded_http.get(path, token=f"tok-{role}").status in (200, 403, 404)
    assert seeded_http.counts() == before


def test_every_get_leaves_an_empty_database_untouched(http):
    before = http.counts()
    paths = list(ROUTES.values()) + ["/cases/tidak-ada"]
    for role in D.ROLES:
        for path in paths:
            assert http.get(path, token=f"tok-{role}").status in (200, 403, 404)
    assert http.counts() == before


def test_get_never_appends_to_the_audit_chain(seeded_http):
    before = seeded_http.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    for role in D.ROLES:
        for path in ROUTES.values():
            seeded_http.get(path, token=f"tok-{role}")
    after = seeded_http.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert after == before


def test_refused_post_leaves_every_table_unchanged(seeded_http):
    before = seeded_http.counts()
    seeded_http.post("/approvals", token="tok-finance", form={"subject_type": "case"})
    seeded_http.post("/approvals/approval-tidak-ada/decide", token="tok-finance",
                     form={"decision": "APPROVE"})
    assert seeded_http.counts() == before


def test_dashboard_never_issues_a_write_statement(seeded_app, seeded):
    statements: list[str] = []
    inner = seeded._conn if hasattr(seeded, "_conn") else seeded

    class Recorder:
        def execute(self, sql, params=()):
            statements.append(str(sql))
            return inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(inner, name)

    app = make_app(Recorder(), seeded_app.clock)
    for role in D.ROLES:
        for page in ROUTES.values():
            try:
                app.dispatch(
                    D._Request(
                        method="GET", path=page, query={}, form={},
                        principal=app.tokens[f"tok-{role}"],
                    )
                )
            except D.DashboardError:
                pass
    forbidden = [
        sql for sql in statements
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\b", sql, re.I)
    ]
    assert forbidden == []


# --------------------------------------------------------------------------- #
# 7. Audit chain health
# --------------------------------------------------------------------------- #
def _tamper(conn, *, seq: int, prev_hash: str, event_id: str) -> None:
    conn.execute(
        "INSERT INTO audit_events (audit_event_id, seq, occurred_at, actor, actor_role, action,"
        " entity_type, entity_id, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, seq, "2026-09-21T07:00:00+00:00", "agent-ops-1", "agent", "tampered",
         "case", SEED_CASE, prev_hash, "1" * 64),
    )


def test_broken_chain_link_is_displayed_not_hidden(seeded_app, seeded):
    _tamper(seeded, seq=4, prev_hash="0" * 64, event_id="audit-broken")
    body = render(seeded_app, "audit", "owner")
    assert 'data-chain="BROKEN"' in body
    assert "AUDIT_CHAIN_BROKEN" in body
    assert 'data-chain="BROKEN"' in render(seeded_app, "command_center", "owner")


def test_sequence_gap_is_reported_as_such(seeded_app, seeded):
    _tamper(seeded, seq=99, prev_hash="0" * 64, event_id="audit-gap")
    body = render(seeded_app, "audit", "owner")
    assert "AUDIT_SEQUENCE_GAP" in body


def test_chain_health_is_per_role_visible_but_never_500(seeded_app, seeded):
    _tamper(seeded, seq=4, prev_hash="0" * 64, event_id="audit-broken-2")
    body = render(seeded_app, "command_center", "reservation")
    assert 'data-chain="BROKEN"' in body


def test_audit_page_lists_the_append_only_guards(seeded_app, seeded):
    body = render(seeded_app, "audit", "owner")
    assert "trg_audit_events_no_update" in body
    assert "trg_audit_events_no_delete" in body


def test_published_anchor_is_shown_and_verified(seeded, clock, tmp_path):
    import anchors as anchors_module

    audit = AuditLog(seeded, clock=clock, ids=IdFactory(seed="anchor-seed"))
    published = anchors_module.publish_anchor(
        conn=seeded, audit=audit, clock=clock, directory=tmp_path,
        actor="owner-sakti-1", actor_role="owner",
    )
    app = make_app(seeded, clock, anchor_dir=tmp_path)
    body = render(app, "audit", "owner")
    assert published.file_name in body
    assert "diverifikasi terhadap anker" in body.lower() or "Diverifikasi terhadap anker" in body
    assert "tidak (tidak ada anker)" not in body
    assert 'data-chain="OK"' in body


def test_anchor_directory_that_does_not_exist_is_tolerated(seeded, clock, tmp_path):
    app = make_app(seeded, clock, anchor_dir=tmp_path / "belum-ada")
    body = render(app, "audit", "owner")
    assert "belum ada anker" in body
    assert 'data-chain="OK"' in body


# --------------------------------------------------------------------------- #
# 8. Static dump (--dump)
# --------------------------------------------------------------------------- #
def test_dump_writes_every_page_the_role_may_open(seeded, clock, tmp_path):
    written = D.dump_all(
        tmp_path / "dump", conn=seeded, clock=clock, actor="owner-sakti-1", actor_role="owner"
    )
    names = {path.name for path in written}
    assert {"index.html", "cases.html", "inventory.html", "finance.html", "operations.html",
            "approvals.html", "properties.html", "audit.html"} <= names
    assert f"case-{SEED_CASE}.html" in names
    assert all(path.exists() for path in written)


def test_dump_contains_the_entitled_pages_only(seeded, clock, tmp_path):
    written = D.dump_all(
        tmp_path / "dump", conn=seeded, clock=clock,
        actor="agent-reservation-1", actor_role="reservation",
    )
    names = {path.name for path in written}
    assert "index.html" in names and "cases.html" in names
    assert "finance.html" not in names
    assert "audit.html" not in names


def test_dump_html_matches_the_live_kpi_counters(seeded, clock, tmp_path, seeded_http):
    written = D.dump_all(
        tmp_path / "dump", conn=seeded, clock=clock, actor="owner-sakti-1", actor_role="owner"
    )
    dumped = (tmp_path / "dump" / "index.html").read_text(encoding="utf-8")
    live = seeded_http.get("/", token="tok-owner").text
    assert kpi_values(dumped) == kpi_values(live)
    assert kpi(dumped, "pending_approvals") == kpi(live, "pending_approvals") == "1"
    assert {path.name for path in written}


def test_dump_escapes_and_redacts_like_the_live_pages(seeded, clock, tmp_path):
    D.dump_all(
        tmp_path / "dump", conn=seeded, clock=clock, actor="owner-sakti-1", actor_role="owner"
    )
    cases = (tmp_path / "dump" / f"case-{SEED_CASE}.html").read_text(encoding="utf-8")
    assert XSS not in cases
    assert XSS_ESCAPED in cases
    assert RAW_EMAIL not in cases
    assert MASKED_EMAIL in cases


def test_main_dump_cli_writes_files_and_exits_zero(clock, tmp_path, capsys):
    db = tmp_path / "villa.db"
    with open(db, "wb"):
        pass
    from schema_loader import connect_sqlite as _connect

    file_conn = _connect(db)
    seed(file_conn, clock)
    file_conn.close()
    out = tmp_path / "static"
    code = D.main(["--db", str(db), "--dump", str(out), "--role", "owner", "--port", "0"])
    assert code == 0
    assert (out / "index.html").exists()
    assert "halaman ditulis" in capsys.readouterr().out


def test_main_refuses_a_missing_database(tmp_path, capsys):
    code = D.main(["--db", str(tmp_path / "tidak-ada.db"), "--dump", str(tmp_path / "d")])
    assert code == 2
    assert "menolak start" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 9. Bind policy, handler factory, template engine
# --------------------------------------------------------------------------- #
def test_resolve_bind_refuses_wildcard_hosts():
    for host in ("0.0.0.0", "::", "*", ""):
        with pytest.raises(D.RemoteBindRefused):
            D.resolve_bind(host)
    assert D.resolve_bind("0.0.0.0", allow_remote=True) == "0.0.0.0"
    assert D.resolve_bind("127.0.0.1") == "127.0.0.1"


def test_serve_binds_loopback_on_an_ephemeral_port(app):
    server = D.serve(app.handler_class(), "127.0.0.1", 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] > 0
    finally:
        server.server_close()


def test_main_refuses_to_bind_a_wildcard_host(clock, tmp_path, capsys):
    db = tmp_path / "villa.db"
    with open(db, "wb"):
        pass
    from schema_loader import connect_sqlite as _connect

    _connect(db).close()
    code = D.main(["--db", str(db), "--host", "0.0.0.0", "--role", "owner"])
    assert code == 2
    assert "REMOTE_BIND_REFUSED" in capsys.readouterr().err


def test_create_app_returns_a_usable_handler_class(tmp_path, clock):
    """``create_app`` hands back a class ``http.server`` accepts directly."""
    with live_handler(tmp_path / "handler.db", clock) as (handler, client):
        assert issubclass(handler, D.DashboardRequestHandler)
        assert issubclass(handler, D.BaseHTTPRequestHandler)
        assert client.get("/", token="tok-owner").status == 200
        assert client.get("/").status == 401


@contextmanager
def live_handler(db_path, clock):
    """Build ``create_app`` *inside* the serving thread, then serve it.

    ``sqlite3`` binds a connection to its creating thread, so the handler the
    server will use has to be assembled by that same thread.
    """
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        conn = connect_sqlite(db_path)
        box["handler"] = D.create_app(
            conn, clock=clock, actor="owner-sakti-1", actor_role="owner", tokens=TOKENS
        )
        box["server"] = D.serve(box["handler"], "127.0.0.1", 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(15), "server tidak pernah siap"
    server = box["server"]
    try:
        yield box["handler"], Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "server thread tidak berhenti"


def test_create_app_generates_unguessable_tokens_when_none_are_given(conn, clock):
    first = D.Dashboard(conn, clock=clock, actor="owner-sakti-1", actor_role="owner")
    second = D.Dashboard(conn, clock=clock, actor="owner-sakti-1", actor_role="owner")
    assert set(first.tokens) != set(second.tokens)
    assert all(len(token) >= 20 for token in first.tokens)
    assert sorted(p.role for p in first.tokens.values()) == sorted(D.ROLES)


def test_actor_and_actor_role_replace_the_demo_identity(conn, clock):
    app = D.Dashboard(conn, clock=clock, actor="owner-sakti-1", actor_role="owner")
    identities = {p.role: p.identity for p in app.tokens.values()}
    assert identities["owner"] == "owner-sakti-1"
    assert identities["finance"] == D.DEFAULT_IDENTITIES["finance"]


def test_render_page_escapes_every_dynamic_value():
    page = D.render_page(
        "properties",
        {
            "title": "<script>t</script>",
            "page": "properties",
            "actor": XSS,
            "actor_role": "owner",
            "now": "2026-09-21T08:00:00+00:00",
            "schema_version": "1.0.0",
            "nav": D.Markup(""),
            "read_only_banner": D.Markup(""),
            "property_table": D.table(["Nama"], [[XSS]], empty="properti"),
            "unit_table": D.empty_note("unit"),
            "hold_table": D.empty_note("hold"),
        },
        templates_dir=TEMPLATES,
    )
    assert XSS not in page
    assert XSS_ESCAPED in page
    assert "&lt;script&gt;t&lt;/script&gt;" in page


def test_render_page_rejects_an_unknown_placeholder():
    with pytest.raises(D.TemplateError):
        D.render_page("properties", {"title": "x"}, templates_dir=TEMPLATES)


def test_render_page_raises_for_a_missing_template(tmp_path):
    with pytest.raises(D.TemplateError):
        D.render_page("tidak-ada", {}, templates_dir=tmp_path)


def test_render_page_rejects_a_path_like_name():
    with pytest.raises(D.TemplateError):
        D.render_page("../etc/passwd", {}, templates_dir=TEMPLATES)


def test_markup_bypasses_escaping_only_for_our_own_builders():
    assert D.escape_value("<b>") == "&lt;b&gt;"
    assert D.escape_value(D.Markup("<b>")) == "<b>"
    assert str(D.badge("<x>")) == '<span class="badge badge-info">&lt;x&gt;</span>'


def test_table_escapes_cells_and_marks_emptiness():
    assert "Tidak ada data" in str(D.table(["a"], [], empty="apa saja"))
    rendered = str(D.table(["a", "b"], [["<i>", 1]], empty="x"))
    assert "&lt;i&gt;" in rendered and "<i>" not in rendered


def test_redact_pii_masks_email_and_drops_phone_and_identity():
    text = f"hubungi {RAW_EMAIL} / {RAW_PHONE} / 3171012345678901"
    out = D.redact_pii(text)
    assert MASKED_EMAIL in out
    assert RAW_EMAIL not in out
    assert RAW_PHONE not in out
    assert "3171012345678901" not in out


def test_format_money_uses_integer_arithmetic_only():
    # IDR has no minor units (primitives.MINOR_UNITS): 4_500_000 is Rp 4.500.000
    assert D.format_money(4_500_000, "IDR") == "IDR 4,500,000"
    assert D.format_money(1_234, "USD") == "USD 12.34"
    assert D.format_money(0) == "0.00"
    assert D.format_money(-150, "USD") == "USD -1.50"
    assert D.format_money(-150, "IDR") == "IDR -150"
    assert D.format_money("bukan angka") == "—"
    assert D.format_money(None) == "—"


# --------------------------------------------------------------------------- #
# 10. Gates integration and policy wiring
# --------------------------------------------------------------------------- #
class AlwaysDenyGate:
    """A classification gate that refuses every P2 value to the page sink."""

    DESTINATION_EXPORT = "export"

    def __init__(self) -> None:
        self.calls = 0

    class ClassifiedField:
        def __init__(self, name: str, classification: str = "", value: object = None) -> None:
            self.name, self.classification, self.value = name, classification, value

    def gate_outbound(self, fields, *, destination, actor_role=None, justification=None):
        self.calls += len(list(fields))
        return D_gate_decision()

    def apply_redaction(self, fields):  # pragma: no cover - parity with the real module
        return tuple(fields)


class D_gate_decision:
    allowed = False
    denied = True
    code = "CODE_P2_AUTH_REQUIRED"

    def as_dict(self):
        return {"allowed": False, "code": self.code}


def test_gate_can_only_tighten_the_pii_policy(seeded, clock):
    gate = AlwaysDenyGate()
    app = make_app(seeded, clock, gates=gate)
    body = render(app, "operations", "operations")
    assert gate.calls > 0, "gate tidak pernah dipanggil"
    assert D.gates_module.gate_outbound is not None
    assert RAW_EMAIL not in body
    assert RAW_PHONE not in body


def test_gate_object_without_the_interface_is_refused(seeded, clock):
    with pytest.raises(D.InvalidInput):
        make_app(seeded, clock, gates=object())


def test_real_gates_module_is_accepted_and_keeps_entitlement(seeded, clock):
    app = make_app(seeded, clock, gates=D.gates_module)
    body = render(app, "operations", "operations")
    assert RAW_EMAIL in body  # operations maps to "manager", which may export P2


def test_page_roles_can_be_overridden_explicitly(conn, clock):
    app = make_app(conn, clock, page_roles={"finance": ("auditor",)})
    assert app.page_allowed("finance", "auditor")
    assert not app.page_allowed("finance", "owner")
    principal = app.tokens["tok-auditor"]
    response = app.dispatch(
        D._Request(method="GET", path="/finance", query={}, form={}, principal=principal)
    )
    assert response.status == 200


def test_principal_refuses_an_unknown_role():
    with pytest.raises(D.InvalidInput):
        D.Principal("agent-x-1", "superuser")


def test_auditor_has_no_approval_role():
    assert D.Principal("auditor-external-1", "auditor").approval_role is None
    assert D.Principal("agent-finance-1", "finance").approval_role == "finance_manager"
    assert not D.Principal("auditor-external-1", "auditor").may_write


def test_every_approval_role_the_dashboard_names_exists_in_the_kernel():
    """The rename trap: a mapping value the kernel does not know silences a whole role.

    ``operations`` maps to the approval role its holders act as. When that name is missing
    from ``approvals.ROLE_RANK`` the dashboard still authenticates the person, the kernel
    refuses every request they make, and nothing in the refusal says the name was retired.
    """
    assert set(D.APPROVAL_ROLE_BY_ROLE.values()) <= set(ROLE_RANK)
    assert "reservation_manager" in D.APPROVAL_ROLE_BY_ROLE.values()


def test_the_dashboard_map_names_the_canonical_role_and_ranks_it_as_before():
    """ADR-0035 §5 reconciliation: new writes use the canonical id — the rank is untouched.

    The reconciliation renamed what the dashboard writes (``accountant`` → ``finance_manager``)
    and added ``gm``. A rename that quietly moved a tier would be an authority change wearing a
    vocabulary change's clothes, so the rank each dashboard role acts at is pinned here: the
    reservation desk is tier 1, operations and the finance desk are tiers 2 and 3, and the GM
    is the new tier between the finance desk and the owner.
    """
    roles = {role: ROLES_MOD.role_rank(mapped) for role, mapped in D.APPROVAL_ROLE_BY_ROLE.items()}
    assert roles == {
        "reservation": 1,
        "operations": 2,
        "finance": 3,
        "gm": 4,
        "owner": 5,
    }
    assert ROLES_MOD.role_rank("gm") == 4
    # The legacy names still rank where they always did, or written rows would change tier.
    assert (
        ROLES_MOD.role_rank("agent"),
        ROLES_MOD.role_rank("manager"),
        ROLES_MOD.role_rank("accountant"),
    ) == (1, 2, 3)


def test_every_documented_dashboard_server_command_passes_a_token():
    """A documented server command with no token is a command that answers 401 to its reader.

    Found by the fresh-database audit (2026-09-28): README, DEPLOYMENT and SALES_PACKAGE each
    showed ``dashboard.py --host … --role owner`` with no token, while the dashboard mints a
    random token per role per start and prints it to stdout only. A reader following the docs
    got 401 on every page, and a systemd restart silently revoked every URL pointing at it.
    The token path itself is pinned above; this pins the docs, because that is where the
    defect was.
    """
    docs = ("README.md", "docs/DEPLOYMENT.md", "docs/SALES_PACKAGE.md")
    offenders = []
    for name in docs:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        # A command may be continued with a backslash (the systemd unit is), so join first:
        # a per-line scan would flag a unit whose token sits on the next line.
        for line in text.replace("\\\n", " ").splitlines():
            if "dashboard.py" not in line or "--host" not in line:
                continue  # a --dump run writes files and opens no port, so it needs no token
            if "--token" not in line:
                offenders.append(f"{name}: {line.strip()[:100]}")
    assert offenders == [], "a documented dashboard server command without --token"


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
import identity as identity_module
import roles as ROLES_MOD
from approvals import ROLE_RANK, ApprovalService
from audit import AuditLog
from primitives import FrozenClock, IdFactory, to_iso
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
    "marketing": "/marketing",
    "channels": "/channels",
    "shadow_eval": "/shadow_eval",
    "outbox": "/outbox",
    "audit": "/audit",
    # The workforce screens live under one head with a short slug each: /workforce/today is
    # what a supervisor types, "workforce_supervisor" is what the code calls it.
    "workforce_supervisor": "/workforce/today",
    "workforce_finance": "/workforce/payroll",
    "workforce_owner": "/workforce/cost",
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
    "marketing": {"reservation", "operations", "owner", "auditor"},
    "channels": {"reservation", "operations", "owner"},
    "shadow_eval": {"operations", "owner", "auditor"},
    "outbox": {"operations", "owner", "auditor"},
    "audit": {"owner", "auditor", "gm"},
    "workforce_supervisor": {"operations"},
    "workforce_finance": {"finance"},
    "workforce_owner": {"owner"},
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

    #: Raw session tokens minted for this database, by identity (set by the fixture).
    session_tokens: dict = {}

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
def live_dashboard(db_path, clock, *, seed_data: bool, sessions: tuple = (), bootstrap=None):
    setup = connect_sqlite(db_path)
    if seed_data:
        seed(setup, clock)
    opened = {
        identity: identity_module.SessionService(setup, clock=clock).open_session(identity, role)
        for identity, role in sessions
    }
    extra_tokens: dict = {}
    if bootstrap is not None:
        # A test that needs more than ``open_session`` can offer (role holders, TOTP, a
        # step-up) builds those rows here and returns the tokens it minted.
        extra_tokens = dict(bootstrap(setup) or {})
    setup.commit()
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
        live = Live(Client(f"http://127.0.0.1:{server.server_address[1]}"), reader)
        live.session_tokens = {identity: result.token for identity, result in opened.items()}
        live.session_tokens.update(extra_tokens)
        yield live
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
                       ("marketing", "owner"), ("channels", "operations"), ("shadow_eval", "owner"),
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
    # Rupiah, grouped the Indonesian way (audit 2026-11-13, T8): the finance page prints the
    # amount through `money_display`, which formats IDR as `4.500.000`. The seed publishes no
    # exchange rate, so the reference converter has nothing to stand behind and correctly
    # prints the rupiah figure alone.
    assert "IDR 4.500.000" in body
    assert "≈ USD" not in body


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
            "actor_role_label": XSS,
            "now": "2026-09-21T08:00:00+00:00",
            "schema_version": "1.0.0",
            "nav": D.Markup(""),
            "read_only_banner": D.Markup(""),
            "banner": D.Markup(""),
            "property_table": D.table(["Nama"], [[XSS]], empty="properti"),
            "unit_table": D.empty_note("unit"),
            "hold_table": D.empty_note("hold"),
            "supplier_contacts": D.Markup(""),
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


class _FakeDashboardServer:
    """Enough of a server for main() to print its banner and exit on KeyboardInterrupt."""

    server_address = ("127.0.0.1", 0)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


def test_token_file_is_read_and_its_value_is_never_printed(tmp_path, monkeypatch, conn, capsys):
    """A token must be able to arrive through a 0600 file, and must not leak to stdout.

    ``--token`` puts the value in ``/proc/<pid>/cmdline``, which every local user can read;
    a file on disk is the deployment path that avoids it.
    """
    planted = "file-token-4a91c7"
    path = tmp_path / "dashboard.token"
    path.write_text(planted + "\n", encoding="utf-8")
    seen: dict = {}
    real_create_app = D.create_app

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_create_app(*args, **kwargs)

    monkeypatch.setattr(D, "create_app", spy)
    monkeypatch.setattr(D, "_open_db", lambda _path: conn)
    monkeypatch.setattr(
        D, "serve", lambda handler, host, port, allow_remote=False: _FakeDashboardServer()
    )
    code = D.main(["--db", "abaikan.db", "--role", "owner", "--token-file", str(path)])
    captured = capsys.readouterr()
    assert code == 0
    assert tuple(seen["tokens"]) == (planted,)
    assert planted not in captured.out
    assert planted not in captured.err


def test_token_and_token_file_together_refuse_to_start(tmp_path, capsys):
    path = tmp_path / "dashboard.token"
    path.write_text("apa-saja", encoding="utf-8")
    code = D.main(["--token", "satu", "--token-file", str(path)])
    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_missing_token_file_refuses_to_start_instead_of_minting_one(tmp_path, capsys):
    """A typo in the path must not silently fall back to an auto-minted token."""
    code = D.main(["--token-file", str(tmp_path / "tidak-ada.token")])
    assert code == 2
    assert "cannot read --token-file" in capsys.readouterr().err


def test_empty_token_file_refuses_to_start(tmp_path, capsys):
    path = tmp_path / "dashboard.token"
    path.write_text("   \n", encoding="utf-8")
    code = D.main(["--token-file", str(path)])
    assert code == 2
    assert "--token-file is empty" in capsys.readouterr().err


def test_finance_summary_states_each_currency_separately(seeded_app, conn, clock):
    """Pin the defect: one SUM across currencies is a figure nobody can read back.

    Both seeded invoices are IDR, so the summary must carry one IDR row equal to their sum. A USD
    invoice must add its own row and leave the IDR row untouched; if the two currencies are ever
    merged into one figure again, the assertions on each row fail.
    """
    now = clock.now()
    page = render(seeded_app, "finance", "finance")
    idr = re.search(r"Outstanding invoices IDR</dt>\s*<dd[^>]*>([^<]+)</dd>", page)
    assert idr, "the finance summary does not name the currency it summed"
    # Displayed through money_display, so the raw minor unit never reaches the page.
    assert idr.group(1).strip() == "IDR 5.500.000"
    assert "Outstanding invoices USD" not in page

    conn.execute(
        "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
        " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
        " status, payment_status, issued_at, due_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("invoice-usd-1", "INV-2026-0003", "booking-1", "b" * 64, "USD", 2_400_000, 0, 100_000,
         2_500_000, 0, "ISSUED", "UNPAID", to_iso(now - timedelta(days=1)),
         to_iso(now + timedelta(days=2)), to_iso(now - timedelta(days=1))),
    )

    page = render(seeded_app, "finance", "finance")
    idr = re.search(r"Outstanding invoices IDR</dt>\s*<dd[^>]*>([^<]+)</dd>", page)
    usd = re.search(r"Outstanding invoices USD</dt>\s*<dd[^>]*>([^<]+)</dd>", page)
    assert idr and usd, "both currencies must be listed"
    assert idr.group(1).strip() == "IDR 5.500.000"
    assert usd.group(1).strip() == "USD 25.000,00"

# --------------------------------------------------------------------------- #
# 8. Channels — what arrived, and what is waiting to leave
# --------------------------------------------------------------------------- #
# A table cell is either plain escaped text or a timestamp rendered as
# <time datetime="…">07 Nov 2026, 07:00 WIB</time>. The readable half is an
# element, not text, so a pattern that only allows [^<]* would silently stop
# matching the moment we made the page friendlier.
_TEXT_CELL = r"(?:<time[^>]*>[^<]*</time>|[^<]*)"


def _inbound_count(page: str, channel: str) -> int:
    """The inbound count for one channel, read back from the rendered table."""
    match = re.search(
        rf"<tr><td>{re.escape(channel)}</td><td>(\d+)</td><td>{_TEXT_CELL}</td></tr>", page
    )
    assert match, f"baris kanal {channel} tidak ditemukan"
    return int(match.group(1))


def _seed_inbound(conn, *, message_id, case_id, channel, received_at):
    """A message row written the way intake writes it — the test's own act."""
    conn.execute(
        "INSERT INTO case_messages (case_message_id, case_id, direction, channel, sender_ref,"
        " received_at, content_class, body_sha256, created_at)"
        " VALUES (?, ?, 'INBOUND', ?, 'guest_test', ?, 'P1', ?, ?)",
        (message_id, case_id, channel, received_at, "0" * 63 + "7", received_at),
    )


def _seed_draft(conn, *, draft_id, case_id, channel, status, created_at, sent=False):
    """One draft, approved by a person — the queue this page exists to show."""
    conn.execute(
        "INSERT INTO message_drafts (draft_id, case_id, channel, template, locale,"
        " recipient_ref, body_text, body_sha256, grounding, gate_decision, status,"
        " created_by, created_at, approved_by, approved_at, sent_by, sent_at, external_ref)"
        " VALUES (?, ?, ?, 'quote_v1', 'id-ID', 'guest_test', 'Halo', ?, 'GROUNDED', 'ALLOW',"
        " ?, 'agent-reservation-1', ?, 'owner-sakti-1', ?, ?, ?, ?)",
        (
            draft_id,
            case_id,
            channel,
            "0" * 63 + "8",
            status,
            created_at,
            created_at,
            "agent-reservation-1" if sent else None,
            created_at if sent else None,
            "mail-1" if sent else None,
        ),
    )


def test_channels_page_counts_inbound_per_channel(seeded_app):
    iso = to_iso(seeded_app.clock.now())
    before = _inbound_count(render(seeded_app, "channels", "operations"), "WhatsApp Business")
    _seed_inbound(seeded_app.conn, message_id="msg_dash_1", case_id=SEED_CASE,
                  channel="whatsapp_business", received_at=iso)
    page = render(seeded_app, "channels", "operations")
    assert _inbound_count(page, "WhatsApp Business") == before + 1


def test_channels_page_counts_a_channel_that_has_never_seen_traffic(seeded_app):
    """A channel with no traffic keeps its row at zero: absent is not the same as zero."""
    page = render(seeded_app, "channels", "operations")
    assert _inbound_count(page, "Phone") == 0
    assert "<tr><td>Phone</td><td>0</td><td>—</td></tr>" in page


def test_channels_page_never_counts_another_agencys_traffic(seeded_app):
    iso = to_iso(seeded_app.clock.now())
    conn = seeded_app.conn
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_other', 'Other Agency', 'ACTIVE', ?, ?)",
        (iso, iso),
    )
    conn.execute(
        "INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority,"
        " sla_policy, first_response_due_at, quote_due_at, idempotency_key, created_at,"
        " updated_at) SELECT 'case_other_1', 'agency_other', channel, guest_ref, status,"
        " priority, sla_policy, first_response_due_at, quote_due_at, 'idem-other-0001',"
        " created_at, updated_at FROM cases WHERE case_id = ?",
        (SEED_CASE,),
    )
    before = _inbound_count(render(seeded_app, "channels", "operations"), "WhatsApp Business")
    _seed_inbound(conn, message_id="msg_other_1", case_id="case_other_1",
                  channel="whatsapp_business", received_at=iso)
    page = render(seeded_app, "channels", "operations")
    assert _inbound_count(page, "WhatsApp Business") == before
    assert "case_other_1" not in page


def test_channels_page_shows_an_approved_draft_nobody_has_sent(seeded_app):
    approved_at = to_iso(seeded_app.clock.now() - timedelta(minutes=90))
    _seed_draft(seeded_app.conn, draft_id="draft_queue_1", case_id=SEED_CASE,
                channel="whatsapp_business", status="APPROVED", created_at=approved_at)
    page = render(seeded_app, "channels", "operations")
    assert "draft_queue_1" in page
    assert "approved and not sent yet: <strong>1</strong>" in page
    queued = re.search(
        rf"<tr><td>draft_queue_1</td><td>[\w-]+</td><td>WhatsApp Business</td>"
        rf"<td>quote_v1</td><td>GROUNDED</td><td>({_TEXT_CELL})</td><td>90</td></tr>",
        page,
    )
    assert queued, "baris antrean tidak memuat umur 90 menit"
    assert queued.group(1).endswith("WIB</time>"), (
        "waktu approval harus terbaca sebagai WIB, bukan ISO mentah"
    )
    assert "<tr><td>GROUNDED</td><td>1</td></tr>" in page


def test_channels_page_keeps_a_sent_draft_out_of_the_queue(seeded_app):
    """A sent draft is history, not work: it shows under receipts, never in the queue."""
    iso = to_iso(seeded_app.clock.now())
    _seed_draft(seeded_app.conn, draft_id="draft_sent_1", case_id=SEED_CASE,
                channel="email", status="SENT", created_at=iso, sent=True)
    page = render(seeded_app, "channels", "operations")
    assert "draft_sent_1" not in page
    assert "approved and not sent yet: <strong>0</strong>" in page
    sent_section = page.split("Sent, with a delivery reference")[1]
    assert "<tr><td>Email</td><td>1</td>" in sent_section


def test_channels_page_does_not_name_a_guest(seeded_app):
    """The page counts channels and drafts; it never renders guest text or contact."""
    page = render(seeded_app, "channels", "operations")
    assert RAW_EMAIL not in page
    assert RAW_PHONE not in page


# --------------------------------------------------------------------------- #
# 9. AI quality — the shadow measurement, next to the bar it is measured against
# --------------------------------------------------------------------------- #
def _publish_shadow_bars(conn, now_iso):
    """The owner's bars, effective before the window this test measures.

    The frozen clock is 2026-09-21 while the seeded baseline revision only takes
    effect on 2026-10-02, so a page under test would read "no bar" for a reason that
    has nothing to do with the page. ``policy_revisions`` is append-only, so this adds
    a version instead of editing one — exactly what an owner would have done.
    """
    for key, revision_id, value in (
        ("governance.shadow.min_agreement_rate", "polrev_test_agreement", "95"),
        ("governance.shadow.min_cases", "polrev_test_cases", "100"),
    ):
        conn.execute(
            "INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key,"
            " version, value, effective_from, effective_to, source_doc_ref, approved_by,"
            " fingerprint, created_at) VALUES (?, 'agency_default', ?, 2, ?, '2026-01-01',"
            " '9999-12-31', 'test decision', 'owner_anas', ?, ?)",
            (revision_id, key, value, "0" * 63 + "6", now_iso),
        )


def _seed_shadow(seeded_app, *, statuses=(("ACCEPTED", 14), ("ACCEPTED", 7), ("REJECTED", 3))):
    """One shadow proposal with reviewed fields, as the extractor path would write it."""
    conn = seeded_app.conn
    iso = to_iso(seeded_app.clock.now())
    _publish_shadow_bars(conn, iso)
    message_id = conn.execute(
        "SELECT case_message_id FROM case_messages WHERE case_id = ?"
        " ORDER BY received_at LIMIT 1",
        (SEED_CASE,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id, extractor,"
        " method, produced_at, status, payload, fingerprint, created_at)"
        " VALUES ('prop_dash_1', ?, ?, 'agent3/RES-W01', 'llm_shadow', ?, 'ACCEPTED', '{}', ?, ?)",
        (SEED_CASE, message_id, iso, "0" * 63 + "5", iso),
    )
    for index, (status, span) in enumerate(statuses, start=1):
        conn.execute(
            "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id, field_name,"
            " field_value, value_type, confidence, source_message_id, source_span_start,"
            " source_span_end, status, value_sha256, created_at, reviewed_by, reviewed_at,"
            " rejection_reason)"
            " VALUES (?, 'prop_dash_1', ?, ?, '2026-11-10', 'date', 90, ?, ?, 14, ?, ?, ?, ?, ?, ?)",
            (
                f"field_dash_{index}",
                SEED_CASE,
                f"field_{index}",
                message_id,
                span,
                status,
                "0" * 63 + "3",
                iso,
                None if status == "PROPOSED" else "staff_reviewer",
                None if status == "PROPOSED" else iso,
                "tidak yakin" if status == "REJECTED" else None,
            ),
        )
    return message_id


def test_marketing_page_shows_public_readiness_without_guest_pii(seeded_app):
    page = render(seeded_app, "marketing", "owner")
    assert "Marketing surface" in page
    assert "Sellable villas" in page
    assert "Villa Anantara" in page
    assert "Website inquiries" in page
    assert SEED_CASE_2 in page
    assert RAW_EMAIL not in page
    assert RAW_PHONE not in page
    assert "Hidden Backoffice" not in page


def test_shadow_eval_page_shows_the_rate_next_to_its_bar(seeded_app):
    _seed_shadow(seeded_app)
    page = render(seeded_app, "shadow_eval", "owner")
    assert "agent3/RES-W01" in page
    assert "66.67%" in page  # 2 of 3 reviewed fields agreed with the extractor
    assert "not enough evidence (1/100 cases)" in page
    assert "<tr><td>Minimum agreement rate</td><td>95%</td><td>95%</td></tr>" in page
    assert 'data-shadow="measured"' in page
    assert "Ungrounded drafts that reached a guest (must stay 0)" in page


def test_shadow_eval_page_reports_the_zero_invariant_honestly(seeded_app):
    """The invariant is counted, not assumed: a clean estate reads 0, not 'unknown'."""
    page = render(seeded_app, "shadow_eval", "owner")
    assert "<dd>0</dd>" in page


def _other_agency_app(seeded_app):
    """A second agency with no published shadow bars: the refusal path, for real."""
    conn = seeded_app.conn
    iso = to_iso(seeded_app.clock.now())
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_other', 'Other Agency', 'ACTIVE', ?, ?)",
        (iso, iso),
    )
    return make_app(conn, seeded_app.clock, agency_id="agency_other")


def test_shadow_eval_page_refuses_to_print_a_rate_without_a_bar(seeded_app):
    """A measurement without its bar is not a result — the page shows the refusal.

    ``agency_other`` has published no shadow bars, the module refuses, and the refusal
    is what the page renders. The platform baseline stays visible, labelled as a
    baseline: it is context, never a stand-in for the tenant's own bar.
    """
    other = _other_agency_app(seeded_app)
    body = other.dispatch(
        D._Request(method="GET", path="/shadow_eval", query={}, form={},
                   principal=other.tokens["tok-owner"])
    ).body
    assert 'data-shadow="unavailable"' in body
    assert "not published" in body
    assert "no rate at all" in body
    assert "<td>95%</td>" in body
    assert "<td>100</td>" in body


def test_shadow_eval_page_is_open_to_the_auditor_and_closed_to_reservation(seeded_app):
    assert seeded_app.page_allowed("shadow_eval", "auditor")
    assert not seeded_app.page_allowed("shadow_eval", "reservation")
    assert seeded_app.page_allowed("channels", "reservation")


def test_nav_groups_cover_every_nav_page():
    """NAV_GROUPS is a presentation-only regrouping of NAV (ADR-0035 org chart).

    It must cover exactly the same page set, with no duplicate and no drift,
    or the grouped nav would silently hide a real page / show a dead one.
    """
    nav_pages = [page for page, _route in D.NAV]
    grouped_pages = [page for _label, pages in D.NAV_GROUPS for page in pages]
    assert sorted(grouped_pages) == sorted(nav_pages)
    assert len(grouped_pages) == len(set(grouped_pages)), "page listed in two groups"


def test_nav_links_still_emit_data_nav_markers_per_role():
    """Grouping is visual only: every role still gets the same allow/deny set."""
    for role in D.ROLES:
        html = str(D.nav_links(role))
        for page, _route in D.NAV:
            if role in D.PAGE_ROLES.get(page, ()):
                assert f'data-nav="{page}"' in html
            else:
                assert f'data-nav-denied="{page}"' in html


def test_role_display_label_covers_every_dashboard_role():
    for role in D.ROLES:
        assert role in D.ROLE_DISPLAY_LABEL
        assert D.ROLE_DISPLAY_LABEL[role]


def test_no_page_shows_a_raw_iso_instant_in_visible_text(seeded_app):
    """The owner reads WIB; the ISO instant belongs in an attribute, never in prose.

    ``_cell`` and ``_stamp`` render every complete instant as
    ``<time datetime="…">14 Nov 2026, 08:16 WIB</time>``. This walks every page the
    owner can open and asserts two things at once: no ISO instant survives in the
    text the eye reads, and at least one ``<time>`` element is actually there — a
    page that simply dropped the timestamps would otherwise pass the first half.
    """
    instant = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    pages = [page for page, _route in D.NAV if "owner" in D.PAGE_ROLES.get(page, ())]
    assert len(pages) >= 10, "the sweep must cover the real page set"
    for page in pages:
        body = render(seeded_app, page, "owner")
        visible = re.sub(r"<[^>]+>", " ", body)
        leaked = instant.findall(visible)
        assert not leaked, f"{page} masih menampilkan ISO mentah: {leaked[:3]}"
        assert "<time datetime=" in body, f"{page} tidak merender satu pun <time>"


# --------------------------------------------------------------------------- #
# Reservation workspace — the HOLD slice (Stage B)
#
# One vertical slice: the operator sees inventory, picks a unit and dates, names the
# evidence it relies on, and the *kernel* decides. These tests walk acceptance A-N of
# the Stage B contract; the kernel's own suite stays the proof that the range rules
# inside it still hold (M), and the dashboard suite above is the regression (N).
# --------------------------------------------------------------------------- #

HOLD_TOKEN_PATTERN = re.compile(r'name="form_token" value="([0-9a-f]{64})"')
HOLD_ACTION = 'action="/holds"'

#: A slot nobody occupies: unit-1 is booked 21-24 Sep, held nothing else in the seed.
FREE_WINDOW = {"check_in": "2026-09-26", "check_out": "2026-09-29"}


def _hold_form(token: str, **overrides: str) -> dict:
    """The form a browser would submit after loading /inventory."""
    form = {
        "form_token": token,
        "unit_id": "unit-1",
        "guest_ref": "guest-web-1",
        "policy_version_id": "policy-1",
        "evidence_id": "evidence-1",
        "idempotency_key": "idem-web-hold-1",
        **FREE_WINDOW,
    }
    form.update(overrides)
    return form


def _form_token(live, *, token: str = "tok-reservation", path: str = "/inventory") -> str:
    """Read the token out of the rendered form — never minted on the test's behalf."""
    page = live.get(path, token=token)
    assert page.status == 200, f"halaman inventory tidak terbuka: {page.status}"
    match = HOLD_TOKEN_PATTERN.search(page.text)
    assert match, "form hold tidak dirender dengan token"
    return match.group(1)


def _hold_rows(live) -> list:
    return list(live.db.execute(
        "SELECT hold_id, unit_id, status, check_in, check_out, idempotency_key, evidence_id"
        " FROM holds ORDER BY hold_id"
    ))


def test_hold_form_is_rendered_for_a_writer_and_names_no_unsupported_action(seeded_http):
    """K: the operator gets a real form; the unit list is the actionable one."""
    page = seeded_http.get("/inventory", token="tok-reservation")

    assert page.status == 200
    assert HOLD_ACTION in page.text and 'data-form="hold-request"' in page.text
    assert 'name="check_in" type="date"' in page.text
    assert 'name="idempotency_key"' in page.text
    assert 'name="form_token"' in page.text, "form tulis wajib membawa token"
    # unit-3 is MAINTENANCE: offering it would be offering a slot nobody can sell.
    assert 'value="unit-3"' not in page.text
    assert 'value="unit-1"' in page.text and 'value="unit-2"' in page.text
    # The form asks for a hold only; confirmation and money stay on the human path.
    assert "booking confirmation" in page.text
    assert 'action="/holds"' in page.text and 'action="/approvals"' not in page.text


def test_authorized_operator_requests_a_hold(seeded_http):
    """A + L: right role, real hold, and an audit row that names the actor."""
    before = seeded_http.counts()
    token = _form_token(seeded_http)

    response = seeded_http.post("/holds", token="tok-reservation", form=_hold_form(token))

    assert response.status == 200, f"hold ditolak tak terduga: {response.status}"
    assert 'data-banner="hold-requested"' in response.text
    assert 'data-status="CONFIRMED"' in response.text
    assert 'data-replayed="0"' in response.text
    assert "No slot was taken" not in response.text
    # Same rule the page sweep enforces on GET: the eye reads a label, not an instant.
    visible = re.sub(r"<[^>]+>", " ", response.text)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", visible)
    rows = _hold_rows(seeded_http)
    assert len(rows) == before["holds"] + 1
    created = [row for row in rows if row["idempotency_key"] == "idem-web-hold-1"]
    assert len(created) == 1
    assert created[0]["unit_id"] == "unit-1"
    assert created[0]["status"] == "CONFIRMED", "status hold ditulis oleh kernel, bukan route"
    assert created[0]["evidence_id"] == "evidence-1", "hold harus menunjuk bukti yang dipilih"
    audit = [
        (row["actor"], row["action"])
        for row in seeded_http.db.execute(
            "SELECT actor, action FROM audit_events WHERE entity_id = ?"
            " AND action = 'hold.requested'",
            (created[0]["hold_id"],),
        )
    ]
    assert audit == [("agent-reservation-1", "hold.requested")], "hold wajib punya bukti audit"
    # Nothing else moved: no booking, no money, no evidence invented.
    after = seeded_http.counts()
    assert set(after) == set(before)
    for table in ("bookings", "invoices", "payments", "availability_evidence"):
        assert after[table] == before[table], f"{table} berubah tanpa izin"


def test_hold_is_denied_for_a_read_only_role(seeded_http):
    """B: the auditor cannot even open the page, let alone post to it."""
    before = _hold_rows(seeded_http)
    page = seeded_http.get("/inventory", token="tok-auditor")
    post = seeded_http.post("/holds", token="tok-auditor", form=_hold_form("0" * 64))

    assert page.status == 403
    assert post.status == 403
    assert _hold_rows(seeded_http) == before


def test_hold_refuses_a_unit_from_another_tenant(seeded_http):
    """C: a unit that belongs to another agency is not addressable at all."""
    seeded_http.db.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency-other', 'Agency Lain', 'ACTIVE', '2026-09-21T07:00:00Z',"
        " '2026-09-21T07:00:00Z')"
    )
    seeded_http.db.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
        " province, timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop-other', 'agency-other', 'Villa Lain', 'guesthouse', 'Badung',"
        " 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', '2026-09-21T07:00:00Z',"
        " '2026-09-21T07:00:00Z')"
    )
    seeded_http.db.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit-other', 'agency-other', 'prop-other', 'Unit Lain', 'room', 1, 1,"
        " 1, 2, 'ACTIVE', '2026-09-21T07:00:00Z', '2026-09-21T07:00:00Z')"
    )
    before = _hold_rows(seeded_http)
    token = _form_token(seeded_http)

    response = seeded_http.post(
        "/holds", token="tok-reservation", form=_hold_form(token, unit_id="unit-other")
    )

    assert response.status == 400
    assert 'data-code="UNIT_NOT_FOUND"' in response.text
    assert _hold_rows(seeded_http) == before
    # And the page for this tenant never advertises the other agency's unit.
    assert "unit-other" not in seeded_http.get("/inventory", token="tok-reservation").text


def test_expired_session_cannot_reach_the_hold_route(seeded, clock):
    """D: the cookie is a carrier for a session, and a stale session authenticates nobody."""
    sessions = identity_module.SessionService(seeded, clock=clock, agency_id="agency_default")
    opened = sessions.open_session("agent-reservation-1", "reservation_agent")
    headers = {"Cookie": f"{D.SESSION_COOKIE}={opened.token}"}

    fresh = make_app(seeded, clock)
    principal = fresh.authenticate(headers)
    assert (principal.identity, principal.role) == ("agent-reservation-1", "reservation")

    later = FrozenClock.at("2026-09-21T09:30:00Z")  # idle timeout is 30 minutes
    expired = make_app(seeded, later)
    with pytest.raises(D.Unauthenticated):
        expired.authenticate(headers)


def test_hold_requires_a_form_token_from_this_process_and_this_principal(seeded_http):
    """E: missing, forged and other-principal tokens are all refused before any write."""
    before = _hold_rows(seeded_http)
    token = _form_token(seeded_http)

    missing = seeded_http.post(
        "/holds", token="tok-reservation", form=_hold_form(token, form_token="")
    )
    forged = seeded_http.post(
        "/holds", token="tok-reservation", form=_hold_form("f" * 64)
    )
    # A token minted for another principal's page is not this principal's token.
    owner_token = _form_token(seeded_http, token="tok-owner")
    crossed = seeded_http.post(
        "/holds", token="tok-reservation", form=_hold_form(owner_token)
    )

    assert missing.status == 403 and 'data-code="FORM_TOKEN_INVALID"' in missing.text
    assert forged.status == 403 and 'data-code="FORM_TOKEN_INVALID"' in forged.text
    assert crossed.status == 403 and 'data-code="FORM_TOKEN_INVALID"' in crossed.text
    assert _hold_rows(seeded_http) == before


def test_hold_rejects_invalid_dates(seeded_http):
    """F: the kernel owns date validation; the route does not re-implement it."""
    before = _hold_rows(seeded_http)
    token = _form_token(seeded_http)

    backwards = seeded_http.post(
        "/holds",
        token="tok-reservation",
        form=_hold_form(token, check_in="2026-09-29", check_out="2026-09-26"),
    )
    nonsense = seeded_http.post(
        "/holds",
        token="tok-reservation",
        form=_hold_form(token, check_in="2026-02-30", check_out="2026-03-02"),
    )

    assert backwards.status == 400, backwards.status
    assert nonsense.status == 400, nonsense.status
    assert 'data-banner="hold-refused"' in backwards.text
    assert "Hold refused" in nonsense.text
    assert _hold_rows(seeded_http) == before


def test_hold_rejects_stale_evidence(seeded_http):
    """G: evidence past its window cannot be leaned on, even when it says AVAILABLE."""
    seeded_http.db.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level,"
        " source_type, source_ref, captured_at, valid_until, result, can_quote, can_hold,"
        " can_confirm, created_at) VALUES ('evidence-stale', 'unit-1', 'A', 'property_api',"
        " 'src-stale', '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z', 'AVAILABLE', 1, 1, 1,"
        " '2026-09-01T00:00:00Z')"
    )
    before = _hold_rows(seeded_http)
    token = _form_token(seeded_http)

    response = seeded_http.post(
        "/holds", token="tok-reservation", form=_hold_form(token, evidence_id="evidence-stale")
    )

    assert response.status == 400, response.status
    assert 'data-code="EVIDENCE_STALE"' in response.text
    assert _hold_rows(seeded_http) == before


def test_hold_rejects_an_overlapping_booking(seeded_http):
    """H: 21-24 Sep on unit-1 is sold; the range rule refuses the request."""
    before = _hold_rows(seeded_http)
    token = _form_token(seeded_http)

    response = seeded_http.post(
        "/holds",
        token="tok-reservation",
        form=_hold_form(token, check_in="2026-09-22", check_out="2026-09-24"),
    )

    # 400, not 409: the kernel classifies the refusal and this route only renders it.
    assert response.status == 400, response.status
    assert 'data-code="SLOT_ALREADY_BOOKED"' in response.text
    assert _hold_rows(seeded_http) == before


def test_hold_retry_is_idempotent(seeded_http):
    """I: the same submission twice is one hold, and the page says so."""
    token = _form_token(seeded_http)
    form = _hold_form(token)

    first = seeded_http.post("/holds", token="tok-reservation", form=form)
    second = seeded_http.post("/holds", token="tok-reservation", form=form)

    assert first.status == 200 and 'data-replayed="0"' in first.text
    assert second.status == 200 and 'data-replayed="1"' in second.text
    rows = [row for row in _hold_rows(seeded_http) if row["idempotency_key"] == form["idempotency_key"]]
    assert len(rows) == 1, "pengulangan tidak boleh membuat hold kedua"


def test_refused_hold_writes_nothing_anywhere(seeded_http):
    """J: a refused request leaves no partial state — not in holds, not in audit."""
    # Fresh evidence for unit-2, so the refusal under test is the *range* one and not
    # the evidence check that runs before it.
    seeded_http.db.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level,"
        " source_type, source_ref, captured_at, valid_until, result, can_quote, can_hold,"
        " can_confirm, created_at) VALUES ('evidence-2', 'unit-2', 'A', 'property_api',"
        " 'src-2', '2026-09-21T06:00:00Z', '2026-09-23T08:00:00Z', 'AVAILABLE', 1, 1, 1,"
        " '2026-09-21T06:00:00Z')"
    )
    token = _form_token(seeded_http)
    before = seeded_http.counts()

    refused = seeded_http.post(
        "/holds",
        token="tok-reservation",
        form=_hold_form(
            token, unit_id="unit-2", evidence_id="evidence-2",
            check_in="2026-09-23", check_out="2026-09-26",
        ),
    )

    assert refused.status == 400, refused.status  # unit-2 is held 22-25 Sep in the seed
    codes = re.findall(r'data-code="([A-Z_]+)"', refused.text)
    assert codes == ["SLOT_ALREADY_HELD"], codes
    after = seeded_http.counts()
    assert after == before, "penolakan tidak boleh meninggalkan jejak tulis"


# --------------------------------------------------------------------------- #
# Booking conversion workspace — HOLD → PAYMENT_PENDING (Stage C1)
#
# The second vertical slice: an operator takes one eligible hold and one quote the
# guest already accepted, and the *kernel* decides the rest. These tests walk
# acceptance A-N of the Stage C1 contract; nothing here writes a booking, a payment
# or a policy of its own.
# --------------------------------------------------------------------------- #

BOOKING_ACTION = 'action="/bookings"'
BOOKING_FORM = 'data-form="booking-from-hold"'
BOOKING_TOKEN_PATTERN = re.compile(r'name="form_token" value="([0-9a-f]{64})"')


def _seed_rate_card(db) -> None:
    """The rate card a quote must point at (``quotes.rate_card_id`` is a FK)."""
    db.execute(
        "INSERT INTO rate_cards (rate_card_id, property_id, unit_scope, name, version,"
        " effective_from, effective_to, currency, weekly_discount_pct, monthly_discount_pct,"
        " source_doc_ref, approved_by, created_at)"
        " VALUES ('rc-1', 'prop-1', 'unit-2', 'Rate Unit Dua', 1, '2026-09-01', '2026-12-31',"
        " 'IDR', '0', '0', 'doc-rc-1', 'owner-sakti-1', '2026-09-01T00:00:00Z')"
    )


def _seed_quote(
    db,
    *,
    quote_id: str = "quote-1",
    unit_id: str = "unit-2",
    check_in: str = "2026-09-22",
    check_out: str = "2026-09-25",
    total_minor: int = 3_680_000,
    status: str = "SENT",
    accepted: bool = True,
) -> None:
    """A quote priced under policy-1, optionally with the version the guest accepted.

    The numbers are a fixture, not a rule: the deposit and the balance come out of
    ``policy-1``'s 30% inside the kernel, and the tests assert only invariants
    (deposit + balance == total), never a hand-computed rupiah figure.
    """
    if db.execute("SELECT 1 FROM rate_cards WHERE rate_card_id = 'rc-1'").fetchone() is None:
        _seed_rate_card(db)
    db.execute(
        "INSERT INTO quotes (quote_id, property_id, unit_id, check_in, check_out, nights,"
        " adults, children, infants, currency, policy_version_id, policy_fingerprint,"
        " rate_card_id, accommodation_minor, extra_guest_minor, cleaning_minor, extras_minor,"
        " discount_minor, subtotal_minor, service_fee_minor, tax_minor, total_minor,"
        " security_deposit_minor, lines, fingerprint, status, warnings, created_at, expires_at)"
        " VALUES (?, 'prop-1', ?, ?, ?, 3, 2, 0, 0, 'IDR', 'policy-1', ?, 'rc-1', ?, 0,"
        " 200000, 0, 0, ?, 160000, 320000, ?, 1000000, '[]', ?, ?, '[]',"
        " '2026-09-21T07:00:00Z', '2026-09-23T08:00:00Z')",
        (
            quote_id,
            unit_id,
            check_in,
            check_out,
            "a" * 64,
            total_minor - 680_000,
            total_minor - 680_000,
            total_minor,
            "c" * 64,
            status,
        ),
    )
    if accepted:
        db.execute(
            "INSERT INTO quotation_versions (quotation_version_id, property_id, unit_id,"
            " check_in, check_out, version, quote_id, status, supersedes_id, created_by,"
            " created_at, sent_at)"
            " VALUES (?, 'prop-1', ?, ?, ?, 1, ?, 'ACCEPTED', NULL, 'agent-reservation-1',"
            " '2026-09-21T07:30:00Z', '2026-09-21T07:31:00Z')",
            (f"qv-{quote_id}", unit_id, check_in, check_out, quote_id),
        )


def _booking_token(live, *, token: str = "tok-reservation") -> str:
    """Read the token out of the *booking* form — never minted on the test's behalf."""
    page = live.get("/inventory", token=token)
    assert page.status == 200, f"halaman inventory tidak terbuka: {page.status}"
    start = page.text.find(BOOKING_FORM)
    assert start != -1, "form booking tidak dirender"
    match = BOOKING_TOKEN_PATTERN.search(page.text, start)
    assert match, "form booking tidak membawa token"
    return match.group(1)


def _booking_form(token: str, **overrides: str) -> dict:
    """The shape a browser submits from the booking form."""
    form = {
        "form_token": token,
        "hold_id": "hold-1",
        "quote_id": "quote-1",
        "adults": "2",
        "children": "0",
        "infants": "0",
        "idempotency_key": "idem-web-book-1",
    }
    form.update(overrides)
    return form


def _booking_rows(live) -> list:
    return list(live.db.execute(
        "SELECT booking_id, unit_id, check_in, check_out, status, payment_status,"
        " hold_ref, quote_ref, total_minor, amount_paid_minor, confirmed_at"
        " FROM bookings ORDER BY booking_id"
    ))


def _attr(text: str, name: str) -> str:
    """Read one data-attribute off the rendered page, refusing to guess."""
    match = re.search(rf'{name}="([^"]+)"', text)
    assert match, f"atribut {name} tidak ada di halaman"
    return match.group(1)


def test_booking_form_is_offered_only_when_a_live_quote_exists(seeded_http):
    """M: an empty estate says so; a priced one offers the conversion, and nothing more."""
    empty = seeded_http.get("/inventory", token="tok-reservation")
    assert empty.status == 200
    assert 'data-note="booking-unavailable"' in empty.text
    assert BOOKING_ACTION not in empty.text, "tanpa quote tidak boleh ada form booking"

    _seed_quote(seeded_http.db)
    page = seeded_http.get("/inventory", token="tok-reservation")

    assert BOOKING_ACTION in page.text and BOOKING_FORM in page.text
    assert 'name="quote_id"' in page.text and 'value="quote-1"' in page.text
    assert 'name="hold_id"' in page.text and 'value="hold-1"' in page.text
    assert "PAYMENT_PENDING" in page.text
    # The form stops at the booking: payment and confirmation are not its sentences.
    assert 'name="mark_paid"' not in page.text and 'name="confirm"' not in page.text


def test_authorized_operator_converts_a_hold_into_a_payment_pending_booking(seeded_http):
    """A + L: right role, real booking, hold still holding, audit row naming the actor."""
    _seed_quote(seeded_http.db)
    before = seeded_http.counts()
    token = _booking_token(seeded_http)

    response = seeded_http.post("/bookings", token="tok-reservation", form=_booking_form(token))

    assert response.status == 200, f"booking ditolak tak terduga: {response.status}"
    assert 'data-banner="booking-created"' in response.text
    assert 'data-status="PAYMENT_PENDING"' in response.text
    assert 'data-replayed="0"' in response.text
    total = int(_attr(response.text, "data-total-minor"))
    deposit = int(_attr(response.text, "data-deposit-due-minor"))
    balance = int(_attr(response.text, "data-balance-due-minor"))
    assert total == 3_680_000, "total harus berasal dari quote"
    assert deposit + balance == total, "deposit + sisa harus menutup total"

    rows = [
        row for row in _booking_rows(seeded_http)
        if row["booking_id"] not in ("booking-1", "booking-2")
    ]
    assert len(rows) == 1, "tepat satu booking baru"
    created = rows[0]
    assert created["status"] == "PAYMENT_PENDING"
    assert created["payment_status"] == "UNPAID", "route tidak boleh mengklaim uang masuk"
    assert created["amount_paid_minor"] == 0
    assert created["hold_ref"] == "hold-1" and created["quote_ref"] == "quote-1"
    assert created["confirmed_at"] is None, "konfirmasi final bukan bagian dari slice ini"
    assert created["total_minor"] == total

    hold = seeded_http.db.execute(
        "SELECT status FROM holds WHERE hold_id = 'hold-1'"
    ).fetchone()
    assert hold["status"] == "CONFIRMED", "hold tetap mengunci slot sampai konfirmasi"
    audit = [
        (row["actor"], row["action"])
        for row in seeded_http.db.execute(
            "SELECT actor, action FROM audit_events WHERE entity_id = ?", (created["booking_id"],)
        )
    ]
    assert ("agent-reservation-1", "booking.reserved") in audit
    after = seeded_http.counts()
    assert after["bookings"] == before["bookings"] + 1
    for table in ("invoices", "payments"):
        assert after[table] == before[table], f"{table} berubah tanpa izin"


def test_booking_is_denied_for_a_read_only_role(seeded_http):
    """B: the auditor has no booking form and no booking route."""
    _seed_quote(seeded_http.db)
    before = _booking_rows(seeded_http)

    page = seeded_http.get("/inventory", token="tok-auditor")
    post = seeded_http.post("/bookings", token="tok-auditor", form=_booking_form("0" * 64))

    assert page.status == 403
    assert post.status == 403
    assert _booking_rows(seeded_http) == before


def test_booking_refuses_a_hold_from_another_tenant(seeded_http):
    """C: another agency's hold is not addressable, and the page never shows it."""
    _seed_quote(seeded_http.db)
    seeded_http.db.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency-other', 'Agency Lain', 'ACTIVE', '2026-09-21T07:00:00Z',"
        " '2026-09-21T07:00:00Z')"
    )
    seeded_http.db.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
        " province, timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop-other', 'agency-other', 'Villa Lain', 'guesthouse', 'Badung',"
        " 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', '2026-09-21T07:00:00Z',"
        " '2026-09-21T07:00:00Z')"
    )
    seeded_http.db.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit-other', 'agency-other', 'prop-other', 'Unit Lain', 'room', 1, 1,"
        " 1, 2, 'ACTIVE', '2026-09-21T07:00:00Z', '2026-09-21T07:00:00Z')"
    )
    seeded_http.db.execute(
        "INSERT INTO holds (hold_id, agency_id, unit_id, property_id, check_in, check_out,"
        " nights, status, guest_ref, policy_version_id, evidence_id, idempotency_key,"
        " created_at, expires_at, expires_timezone)"
        " VALUES ('hold-other', 'agency-other', 'unit-other', 'prop-other', '2026-09-22',"
        " '2026-09-25', 3, 'CONFIRMED', 'guest-other', 'policy-1', 'evidence-1',"
        " 'idem-hold-other', '2026-09-21T07:00:00Z', '2026-09-21T20:00:00Z', 'Asia/Makassar')"
    )
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)

    response = seeded_http.post(
        "/bookings", token="tok-reservation", form=_booking_form(token, hold_id="hold-other")
    )

    assert response.status == 400, response.status
    assert 'data-code="HOLD_NOT_FOUND"' in response.text
    assert 'data-banner="booking-created"' not in response.text
    assert _booking_rows(seeded_http) == before
    assert "hold-other" not in seeded_http.get("/inventory", token="tok-reservation").text


def test_booking_route_requires_a_session(seeded_http):
    """D: no cookie, no booking — the route is behind the same wall as the pages."""
    _seed_quote(seeded_http.db)
    before = _booking_rows(seeded_http)

    response = seeded_http.post("/bookings", token=None, form=_booking_form("0" * 64))

    assert response.status == 401, response.status
    assert _booking_rows(seeded_http) == before


def test_booking_requires_a_form_token_from_this_process_and_this_principal(seeded_http):
    """E: missing, forged and other-principal tokens are refused before any write."""
    _seed_quote(seeded_http.db)
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)
    owner_token = _booking_token(seeded_http, token="tok-owner")

    missing = seeded_http.post(
        "/bookings", token="tok-reservation", form=_booking_form(token, form_token="")
    )
    forged = seeded_http.post("/bookings", token="tok-reservation", form=_booking_form("f" * 64))
    crossed = seeded_http.post(
        "/bookings", token="tok-reservation", form=_booking_form(owner_token)
    )

    for response in (missing, forged, crossed):
        assert response.status == 403, response.status
        assert 'data-code="FORM_TOKEN_INVALID"' in response.text
    assert _booking_rows(seeded_http) == before


def test_booking_refuses_a_released_or_expired_hold(seeded_http):
    """F + G: only a live, confirmed hold may become a booking."""
    _seed_quote(seeded_http.db)
    for hold in (
        ("hold-released", "RELEASE_REQUESTED", "2026-09-21T06:00:00Z",
         "2026-09-21T20:00:00Z", "2026-09-27", "2026-09-29"),
        ("hold-expired", "CONFIRMED", "2026-09-19T00:00:00Z",
         "2026-09-20T00:00:00Z", "2026-10-10", "2026-10-12"),
    ):
        seeded_http.db.execute(
            "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
            " status, guest_ref, policy_version_id, evidence_id, idempotency_key, created_at,"
            " expires_at, expires_timezone)"
            " VALUES (?, 'unit-2', 'prop-1', ?, ?, 2, ?, 'guest-x',"
            " 'policy-1', 'evidence-1', ?, ?, ?, 'Asia/Makassar')",
            (hold[0], hold[4], hold[5], hold[1], f"idem-{hold[0]}", hold[2], hold[3]),
        )
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)

    released = seeded_http.post(
        "/bookings", token="tok-reservation",
        form=_booking_form(token, hold_id="hold-released", idempotency_key="idem-web-book-rel"),
    )
    expired = seeded_http.post(
        "/bookings", token="tok-reservation",
        form=_booking_form(token, hold_id="hold-expired", idempotency_key="idem-web-book-exp"),
    )

    assert released.status == 400 and 'data-code="HOLD_NOT_ACTIVE"' in released.text
    assert expired.status == 400 and 'data-code="HOLD_ALREADY_EXPIRED"' in expired.text
    assert _booking_rows(seeded_http) == before


def test_booking_fails_closed_without_an_accepted_quote_for_this_stay(seeded_http):
    """H: a price nobody accepted, a quote for another unit, and a missing quote."""
    _seed_quote(seeded_http.db)
    _seed_quote(seeded_http.db, quote_id="quote-2", accepted=False)
    _seed_quote(seeded_http.db, quote_id="quote-3", unit_id="unit-1",
                check_in="2026-09-26", check_out="2026-09-29")
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)

    unaccepted = seeded_http.post(
        "/bookings", token="tok-reservation",
        form=_booking_form(token, quote_id="quote-2", idempotency_key="idem-web-book-q2"),
    )
    other_unit = seeded_http.post(
        "/bookings", token="tok-reservation",
        form=_booking_form(token, quote_id="quote-3", idempotency_key="idem-web-book-q3"),
    )
    missing = seeded_http.post(
        "/bookings", token="tok-reservation",
        form=_booking_form(token, quote_id="", idempotency_key="idem-web-book-q0"),
    )

    assert unaccepted.status == 400 and 'data-code="CONFIRM_NOT_ACCEPTED"' in unaccepted.text
    assert other_unit.status == 400 and 'data-code="QUOTE_UNIT_MISMATCH"' in other_unit.text
    assert missing.status == 400 and 'data-code="INVALID_INPUT"' in missing.text
    assert _booking_rows(seeded_http) == before


def test_booking_retry_is_idempotent(seeded_http):
    """I: the same submission twice is one booking, and the page says so."""
    _seed_quote(seeded_http.db)
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)
    form = _booking_form(token)

    first = seeded_http.post("/bookings", token="tok-reservation", form=form)
    second = seeded_http.post("/bookings", token="tok-reservation", form=form)

    assert first.status == 200 and 'data-replayed="0"' in first.text
    assert second.status == 200 and 'data-replayed="1"' in second.text
    ids = {
        _attr(response.text, "data-booking-id")
        for response in (first, second)
    }
    assert len(ids) == 1, "pengulangan tidak boleh menjadi booking kedua"
    assert len(_booking_rows(seeded_http)) == len(before) + 1


def test_one_hold_cannot_become_two_bookings(seeded_http):
    """J: a second operator with a *different* key still lands on the same booking."""
    _seed_quote(seeded_http.db)
    before = _booking_rows(seeded_http)
    token = _booking_token(seeded_http)

    first = seeded_http.post("/bookings", token="tok-reservation", form=_booking_form(token))
    second = seeded_http.post(
        "/bookings", token="tok-owner",
        form=_booking_form(_booking_token(seeded_http, token="tok-owner"),
                           idempotency_key="idem-web-book-2"),
    )

    first_id = _attr(first.text, "data-booking-id")
    second_id = _attr(second.text, "data-booking-id")
    assert first.status == 200 and second.status == 200
    assert first_id == second_id, "satu hold hanya boleh menopang satu booking"
    assert second.text.count('data-replayed="1"') == 1
    assert len(_booking_rows(seeded_http)) == len(before) + 1


def test_booking_refusal_creates_no_partial_state(seeded_http):
    """K: an over-capacity party leaves no booking, no invoice, no audit row."""
    _seed_quote(seeded_http.db)
    before = seeded_http.counts()
    token = _booking_token(seeded_http)

    response = seeded_http.post(
        "/bookings", token="tok-reservation", form=_booking_form(token, adults="4")
    )

    assert response.status == 400, response.status
    assert 'data-code="CAPACITY_EXCEEDED"' in response.text
    assert 'data-banner="booking-created"' not in response.text
    assert seeded_http.counts() == before, "penolakan tidak boleh meninggalkan jejak tulis"

# --------------------------------------------------------------------------- #
# Stage D: finance -> reservation, the money half of the booking chain
#
# The tests below walk acceptance A-G of the Stage D contract over real HTTP and
# the real domain services. Every record they create is synthetic and lives in the
# test's own SQLite file; nothing here writes a payment, a provider event or a
# policy of its own, and no test asserts a hand-computed rupiah figure it could
# have invented — the numbers come from the quote the fixture seeds.
# --------------------------------------------------------------------------- #
INVOICE_FORM = 'data-form="issue-invoice"'
MARK_PAID_FORM = 'data-form="mark-paid"'
STAGE_D_LINES = [
    {"code": "night:1", "description": "Nightly rate", "quantity": 3, "amount_minor": 3_000_000},
    {"code": "service_fee", "description": "Service fee", "amount_minor": 150_000},
    {"code": "tax_pbjt", "description": "PBJT", "amount_minor": 300_000},
]
STAGE_D_SUBTOTAL = 3_000_000
STAGE_D_SERVICE = 150_000
STAGE_D_TAX = 300_000
STAGE_D_TOTAL = STAGE_D_SUBTOTAL + STAGE_D_SERVICE + STAGE_D_TAX


def _seed_stage_d_booking(
    db,
    *,
    booking_id: str = "booking-d1",
    quote_id: str = "quote-d1",
    status: str = "PAYMENT_PENDING",
    quote_status: str = "ACCEPTED",
    unit_id: str = "unit-2",
    check_in: str = "2026-12-10",
    check_out: str = "2026-12-13",
    amount_paid_minor: int = 0,
    payment_status: str = "UNPAID",
) -> None:
    """A booking that waits for money, priced by a quote whose lines reconcile."""
    if db.execute("SELECT 1 FROM rate_cards WHERE rate_card_id = 'rc-1'").fetchone() is None:
        _seed_rate_card(db)
    db.execute(
        "INSERT INTO quotes (quote_id, property_id, unit_id, check_in, check_out, nights,"
        " adults, children, infants, currency, policy_version_id, policy_fingerprint,"
        " rate_card_id, accommodation_minor, extra_guest_minor, cleaning_minor, extras_minor,"
        " discount_minor, subtotal_minor, service_fee_minor, tax_minor, total_minor,"
        " security_deposit_minor, lines, fingerprint, status, warnings, created_at, expires_at)"
        " VALUES (?, 'prop-1', ?, ?, ?, 3, 2, 0, 0, 'IDR', 'policy-1', ?, 'rc-1', ?, 0, 0, 0,"
        " 0, ?, ?, ?, ?, 500000, ?, ?, ?, '[]', '2026-11-01T00:00:00Z',"
        " '2027-01-01T00:00:00Z')",
        (
            quote_id,
            unit_id,
            check_in,
            check_out,
            "a" * 64,
            STAGE_D_SUBTOTAL,
            STAGE_D_SUBTOTAL,
            STAGE_D_SERVICE,
            STAGE_D_TAX,
            STAGE_D_TOTAL,
            json.dumps(STAGE_D_LINES),
            "c" * 64,
            quote_status,
        ),
    )
    db.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, total_minor, amount_paid_minor, security_deposit_minor,"
        " payment_status, idempotency_key, version_lock, created_at, updated_at)"
        " VALUES (?, 'prop-1', ?, ?, ?, 3, ?, 'guest-stage-d', 2, 0, 0, 'IDR', 'policy-1',"
        " ?, ?, ?, ?, 500000, ?, ?, 1, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
        (
            booking_id,
            unit_id,
            check_in,
            check_out,
            status,
            "b" * 64,
            quote_id,
            STAGE_D_TOTAL,
            amount_paid_minor,
            payment_status,
            f"idem-{booking_id}",
        ),
    )


def _seed_stage_d_invoice(
    db,
    *,
    invoice_id: str = "invoice-d1",
    booking_id: str = "booking-d1",
    number: str = "INV-2026-9001",
) -> None:
    db.execute(
        "INSERT INTO invoices (invoice_id, invoice_number, booking_id, policy_fingerprint,"
        " currency, subtotal_minor, tax_minor, service_minor, total_minor, amount_paid_minor,"
        " status, payment_status, issued_at, due_at, created_at)"
        " VALUES (?, ?, ?, ?, 'IDR', ?, ?, ?, ?, 0, 'ISSUED', 'UNPAID',"
        " '2026-11-01T00:00:00Z', '2026-11-08T00:00:00Z', '2026-11-01T00:00:00Z')",
        (
            invoice_id,
            number,
            booking_id,
            "b" * 64,
            STAGE_D_SUBTOTAL,
            STAGE_D_TAX,
            STAGE_D_SERVICE,
            STAGE_D_TOTAL,
        ),
    )


def _seed_stage_d_payment(
    db,
    *,
    payment_id: str = "payment-d1",
    invoice_id: str = "invoice-d1",
    status: str = "SUCCEEDED",
    amount_minor: int = STAGE_D_TOTAL,
) -> None:
    """A payment row, written the way the payment service would leave it.

    ``SUCCEEDED`` is the only status this test file lets the happy path see; a
    provider event that produced it is out of scope here (and in production it is
    ``payments.PaymentService``, not the dashboard, that writes it).
    """
    db.execute(
        "INSERT INTO payments (payment_id, invoice_id, provider, provider_intent_id, method,"
        " currency, amount_minor, status, created_at, settled_at, provider_status)"
        " VALUES (?, ?, 'midtrans', ?, 'BANK_TRANSFER', 'IDR', ?, ?,"
        " '2026-11-01T00:00:00Z', ?, 'settlement')",
        (
            payment_id,
            invoice_id,
            f"intent-{payment_id}",
            amount_minor,
            status,
            "2026-11-01T00:00:00Z" if status == "SUCCEEDED" else None,
        ),
    )


def _form_token_from(live, marker: str, *, token: str = "tok-finance") -> str:
    """Read a token out of a finance form — never minted here.

    When the only pending booking already carries an invoice, the invoice form is
    not offered; the mark-paid form carries the same token, so either form will do.
    """
    page = live.get("/finance", token=token)
    assert page.status == 200, f"halaman finance tidak terbuka: {page.status}"
    for candidate in (marker, MARK_PAID_FORM, INVOICE_FORM):
        start = page.text.find(candidate)
        if start != -1:
            match = BOOKING_TOKEN_PATTERN.search(page.text, start)
            assert match, f"form {candidate} tidak membawa token"
            return match.group(1)
    raise AssertionError(f"tidak ada form finance yang dirender (diminta {marker})")


def _invoice_rows(live) -> list:
    return list(live.db.execute(
        "SELECT invoice_id, booking_id, subtotal_minor, tax_minor, service_minor, total_minor"
        " FROM invoices ORDER BY invoice_id"
    ))


def _booking_row(live, booking_id: str = "booking-d1"):
    return live.db.execute(
        "SELECT status, amount_paid_minor, payment_status, confirmed_at FROM bookings"
        " WHERE booking_id = ?",
        (booking_id,),
    ).fetchone()


def test_finance_page_lists_the_booking_waiting_for_money(seeded_http):
    """A + G: Finance sees the pending booking, its invoice count and its payment state."""
    _seed_stage_d_booking(seeded_http.db)

    page = seeded_http.get("/finance", token="tok-finance")

    assert page.status == 200
    assert 'data-table="finance-pending"' in page.text
    assert 'data-booking-id="booking-d1"' in page.text
    assert 'data-invoice-count="0"' in page.text
    assert 'data-verified-payment=""' in page.text
    assert INVOICE_FORM in page.text, "invoice issuance must be offered"
    assert MARK_PAID_FORM not in page.text, "no verified payment, no mark-paid button"
    assert 'data-note="mark-paid-unavailable"' in page.text


def test_confirmation_is_not_a_finance_action(seeded_http):
    """F: after ADR-0073 the finance page points at Reservation, and finance cannot confirm."""
    _seed_stage_d_booking(seeded_http.db)

    page = seeded_http.get("/finance", token="tok-finance")

    assert page.status == 200
    assert 'data-note="confirmation-elsewhere"' in page.text
    assert 'action="/bookings/confirm"' not in page.text
    refusal = seeded_http.post(
        "/bookings/confirm", token="tok-finance", form={"form_token": "x" * 64}
    )
    assert refusal.status == 403, "finance holds no page that may confirm a booking"


def test_invoice_is_priced_from_the_accepted_quote(seeded_http):
    """1 + 2: authorized issuance succeeds and the money matches the quote exactly."""
    _seed_stage_d_booking(seeded_http.db)
    before = seeded_http.counts()
    token = _form_token_from(seeded_http, INVOICE_FORM)

    response = seeded_http.post(
        "/invoices",
        token="tok-finance",
        form={
            "form_token": token,
            "booking_id": "booking-d1",
            "idempotency_key": "idem-inv-1",
        },
    )

    assert response.status == 200, response.text[-800:]
    assert 'data-banner="invoice-issued"' in response.text
    assert f'data-total-minor="{STAGE_D_TOTAL}"' in response.text
    rows = _invoice_rows(seeded_http)
    created = [row for row in rows if row[1] == "booking-d1"]
    assert len(created) == 1, "satu booking, satu invoice"
    _, _, subtotal, tax, service, total = created[0]
    assert (subtotal, tax, service, total) == (
        STAGE_D_SUBTOTAL,
        STAGE_D_TAX,
        STAGE_D_SERVICE,
        STAGE_D_TOTAL,
    )
    lines = list(seeded_http.db.execute(
        "SELECT kind, amount_minor FROM invoice_lines WHERE invoice_id = ?", (created[0][0],)
    ))
    assert sum(row[1] for row in lines) == STAGE_D_TOTAL
    assert len(lines) == len(STAGE_D_LINES)
    after = seeded_http.counts()
    assert after["bookings"] == before["bookings"], "invoicing must not touch the booking"


def test_invoice_issuance_replays_instead_of_double_billing(seeded_http):
    """3: the same idempotency key cannot buy two invoices."""
    _seed_stage_d_booking(seeded_http.db)
    token = _form_token_from(seeded_http, INVOICE_FORM)
    form = {
        "form_token": token,
        "booking_id": "booking-d1",
        "idempotency_key": "idem-inv-replay",
    }

    first = seeded_http.post("/invoices", token="tok-finance", form=form)
    second = seeded_http.post("/invoices", token="tok-finance", form=form)

    assert first.status == 200 and second.status == 200, (first.status, second.status, second.text[-900:])
    assert 'data-replayed="0"' in first.text
    assert 'data-replayed="1"' in second.text
    assert len([row for row in _invoice_rows(seeded_http) if row[1] == "booking-d1"]) == 1


def test_invoice_refused_for_a_quote_the_guest_never_accepted(seeded_http):
    """1: an unaccepted quote cannot become a receivable."""
    _seed_stage_d_booking(seeded_http.db, quote_status="SENT")
    before = seeded_http.counts()
    token = _form_token_from(seeded_http, INVOICE_FORM)

    response = seeded_http.post(
        "/invoices",
        token="tok-finance",
        form={"form_token": token, "booking_id": "booking-d1", "idempotency_key": "idem-inv-2"},
    )

    assert response.status == 400
    assert 'data-code="QUOTE_NOT_ACCEPTED"' in response.text
    assert seeded_http.counts() == before, "refusal must leave no invoice behind"


def test_invoice_refused_for_a_booking_in_another_tenant(seeded_http):
    """4: tenant isolation holds at the write, not only at the read."""
    seeded_http.db.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_other', 'Other Agency', 'ACTIVE',"
        " '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    _seed_stage_d_booking(seeded_http.db)
    _seed_stage_d_booking(seeded_http.db, booking_id="booking-other", quote_id="quote-other")
    seeded_http.db.execute(
        "UPDATE bookings SET agency_id = 'agency_other' WHERE booking_id = 'booking-other'"
    )
    before = seeded_http.counts()
    token = _form_token_from(seeded_http, INVOICE_FORM)

    response = seeded_http.post(
        "/invoices",
        token="tok-finance",
        form={
            "form_token": token,
            "booking_id": "booking-other",
            "idempotency_key": "idem-inv-3",
        },
    )

    assert response.status == 400, response.status
    assert 'data-code="BOOKING_NOT_FOUND"' in response.text
    assert seeded_http.counts() == before


def _mark_paid_form(
    token: str,
    *,
    booking_id: str = "booking-d1",
    payment_id: str = "payment-d1",
    key: str = "idem-paid-1",
) -> dict:
    return {
        "form_token": token,
        "booking_id": booking_id,
        "payment_id": payment_id,
        "idempotency_key": key,
    }


def test_mark_paid_refused_when_the_payment_is_not_verified(seeded_http):
    """5 + 6: a pending payment is not money, and the booking does not move."""
    _seed_stage_d_booking(seeded_http.db)
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db, status="PENDING")
    # A second booking with no invoice keeps the issuance form on the page, so the
    # forged request below carries a token a browser could actually have obtained.
    _seed_stage_d_booking(
        seeded_http.db, booking_id="booking-extra", quote_id="quote-extra",
        check_in="2026-12-28", check_out="2026-12-31",
    )
    before = _booking_row(seeded_http)
    token = _form_token_from(seeded_http, INVOICE_FORM)

    response = seeded_http.post(
        "/payments/mark-paid", token="tok-finance", form=_mark_paid_form(token)
    )

    assert response.status == 400, response.status
    assert 'data-code="PAYMENT_NOT_VERIFIED"' in response.text
    assert _booking_row(seeded_http) == before, "penolakan tidak boleh menyentuh booking"


def test_mark_paid_refused_when_the_payment_belongs_to_another_booking(seeded_http):
    """6 + 13: a verified payment for a different booking authorizes nothing here."""
    _seed_stage_d_booking(seeded_http.db)
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_booking(
        seeded_http.db,
        booking_id="booking-d2",
        quote_id="quote-d2",
        check_in="2026-12-20",
        check_out="2026-12-23",
        amount_paid_minor=STAGE_D_TOTAL,
        payment_status="CLEARED_FULL",
    )
    _seed_stage_d_invoice(
        seeded_http.db, invoice_id="invoice-d2", booking_id="booking-d2", number="INV-2026-9002"
    )
    _seed_stage_d_payment(seeded_http.db, payment_id="payment-d2", invoice_id="invoice-d2")
    before = _booking_row(seeded_http)
    token = _form_token_from(seeded_http, MARK_PAID_FORM)

    response = seeded_http.post(
        "/payments/mark-paid",
        token="tok-finance",
        form=_mark_paid_form(token, payment_id="payment-d2"),
    )

    assert response.status == 400, response.status
    assert 'data-code="PAYMENT_NOT_FOR_BOOKING"' in response.text
    assert _booking_row(seeded_http) == before


def test_mark_paid_settles_the_booking_on_a_verified_payment(seeded_http):
    """7 + 11: verified money moves the booking, and it still is not CONFIRMED."""
    _seed_stage_d_booking(
        seeded_http.db, amount_paid_minor=STAGE_D_TOTAL, payment_status="CLEARED_FULL"
    )
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db)
    token = _form_token_from(seeded_http, MARK_PAID_FORM)

    response = seeded_http.post(
        "/payments/mark-paid", token="tok-finance", form=_mark_paid_form(token)
    )

    assert response.status == 200, response.text[-800:]
    assert 'data-banner="booking-paid"' in response.text
    assert 'data-status="PAID_IN_FULL"' in response.text
    assert f'data-amount-paid-minor="{STAGE_D_TOTAL}"' in response.text
    status, paid, payment_status, confirmed_at = _booking_row(seeded_http)
    assert status == "PAID_IN_FULL" and paid == STAGE_D_TOTAL
    assert payment_status == "CLEARED_FULL"
    assert confirmed_at is None, "paid is not confirmed: the block proof is a separate step"
    state_events = [
        tuple(row) for row in seeded_http.db.execute(
            "SELECT from_state, to_state, command, evidence_ref FROM booking_state_events"
            " WHERE booking_id = 'booking-d1'"
        )
    ]
    assert ("PAYMENT_PENDING", "PAID_IN_FULL", "mark_paid", "payment-d1") in state_events


def test_duplicate_mark_paid_does_not_move_the_booking_twice(seeded_http):
    """9 + 14: the second click is a replay, not a second transition."""
    _seed_stage_d_booking(
        seeded_http.db, amount_paid_minor=STAGE_D_TOTAL, payment_status="CLEARED_FULL"
    )
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db)
    token = _form_token_from(seeded_http, MARK_PAID_FORM)

    first = seeded_http.post(
        "/payments/mark-paid", token="tok-finance", form=_mark_paid_form(token)
    )
    after_first = list(seeded_http.db.execute(
        "SELECT version_lock, amount_paid_minor, status FROM bookings WHERE booking_id = 'booking-d1'"
    ))
    second = seeded_http.post(
        "/payments/mark-paid",
        token="tok-finance",
        form=_mark_paid_form(token, key="idem-paid-2"),
    )

    assert first.status == 200 and second.status == 200
    assert 'data-replayed="1"' in second.text
    after_second = list(seeded_http.db.execute(
        "SELECT version_lock, amount_paid_minor, status FROM bookings WHERE booking_id = 'booking-d1'"
    ))
    assert after_second == after_first, "replay must not bump version_lock or the paid mirror"
    events = list(seeded_http.db.execute(
        "SELECT command FROM booking_state_events WHERE booking_id = 'booking-d1'"
        " AND command = 'mark_paid'"
    ))
    assert len(events) == 1, "one transition, one event"


def test_payment_below_the_policy_deposit_is_refused(seeded_http):
    """8: money below the policy deposit cannot even buy DEPOSIT_PAID."""
    below = 300_000  # policy-1 asks for 30% of the total
    _seed_stage_d_booking(
        seeded_http.db, amount_paid_minor=below, payment_status="DEPOSIT_CLEARED"
    )
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db, amount_minor=below)
    token = _form_token_from(seeded_http, MARK_PAID_FORM)

    response = seeded_http.post(
        "/payments/mark-paid", token="tok-finance", form=_mark_paid_form(token)
    )

    assert response.status == 400, response.status
    assert 'data-code="PAYMENT_BELOW_DEPOSIT"' in response.text
    status, _, _, confirmed_at = _booking_row(seeded_http)
    assert status == "PAYMENT_PENDING" and confirmed_at is None


def test_mark_paid_refused_without_a_payment_id(seeded_http):
    """15: an incomplete request writes nothing at all."""
    _seed_stage_d_booking(seeded_http.db)
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_booking(
        seeded_http.db, booking_id="booking-extra", quote_id="quote-extra",
        check_in="2026-12-28", check_out="2026-12-31",
    )
    before = seeded_http.counts()
    token = _form_token_from(seeded_http, INVOICE_FORM)

    response = seeded_http.post(
        "/payments/mark-paid",
        token="tok-finance",
        form={"form_token": token, "booking_id": "booking-d1", "idempotency_key": "idem-paid-3"},
    )

    assert response.status == 400
    assert seeded_http.counts() == before


def test_auditor_cannot_issue_an_invoice_or_settle_a_booking(seeded_http):
    """10: the read-only role reads; it cannot write money."""
    _seed_stage_d_booking(
        seeded_http.db, amount_paid_minor=STAGE_D_TOTAL, payment_status="CLEARED_FULL"
    )
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db)
    before = seeded_http.counts()
    token = _form_token_from(seeded_http, INVOICE_FORM)

    invoice_attempt = seeded_http.post(
        "/invoices",
        token="tok-auditor",
        form={"form_token": token, "booking_id": "booking-d1", "idempotency_key": "idem-inv-aud"},
    )
    paid_attempt = seeded_http.post(
        "/payments/mark-paid", token="tok-auditor", form=_mark_paid_form(token)
    )

    assert invoice_attempt.status == 403 and paid_attempt.status == 403
    assert seeded_http.counts() == before
    assert _booking_row(seeded_http)[0] == "PAYMENT_PENDING"


def test_reservation_saw_the_booking_become_paid(seeded_http):
    """E: the Reservation page reads the paid state the finance step produced."""
    _seed_stage_d_booking(
        seeded_http.db, amount_paid_minor=STAGE_D_TOTAL, payment_status="CLEARED_FULL"
    )
    _seed_stage_d_invoice(seeded_http.db)
    _seed_stage_d_payment(seeded_http.db)
    token = _form_token_from(seeded_http, MARK_PAID_FORM)
    seeded_http.post("/payments/mark-paid", token="tok-finance", form=_mark_paid_form(token))

    page = seeded_http.get("/inventory", token="tok-reservation")

    assert page.status == 200
    assert 'data-table="awaiting-payment"' in page.text
    assert 'data-booking-id="booking-d1"' in page.text
    assert "PAID_IN_FULL" in page.text


# --------------------------------------------------------------------------- #
# Stage E: the supplier block and the confirmation it authorises (ADR-0073)
# --------------------------------------------------------------------------- #
CONFIRM_FORM = 'data-form="confirm-booking"'


def _seed_stage_e_paid_booking(
    db,
    *,
    booking_id: str = "booking-e1",
    hold_id: str = "hold-e1",
    check_in: str = "2026-12-10",
    check_out: str = "2026-12-13",
) -> None:
    """A PAID_IN_FULL booking with the live hold that still keeps its slot."""
    _seed_stage_d_booking(
        db,
        booking_id=booking_id,
        quote_id=f"quote-{booking_id}",
        status="PAID_IN_FULL",
        check_in=check_in,
        check_out=check_out,
        amount_paid_minor=STAGE_D_TOTAL,
        payment_status="CLEARED_FULL",
    )
    db.execute(
        "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
        " status, guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,"
        " created_at, expires_at, expires_timezone) VALUES (?, 'unit-2', 'prop-1', ?, ?, 3,"
        " 'CONFIRMED', 'guest-stage-e', ?, 'policy-1', 'evidence-1', ?,"
        " '2026-11-01T00:00:00Z', '2027-06-01T00:00:00Z', 'Asia/Makassar')",
        (hold_id, check_in, check_out, f"quote-{booking_id}", f"idem-{hold_id}"),
    )
    db.execute(
        "UPDATE bookings SET hold_ref = ?, created_at = '2026-09-01T00:00:00Z',"
        " updated_at = '2026-09-01T00:00:00Z' WHERE booking_id = ?",
        (hold_id, booking_id),
    )


def _seed_stage_e_proof(
    db,
    *,
    booking_id: str = "booking-e1",
    proof_id: str = "proof-e1",
    evidence_id: str = "evid-block-1",
    check_in: str = "2026-12-10",
    check_out: str = "2026-12-13",
    captured_at: str = "2026-09-02T00:00:00Z",
    expires_at: str = "2027-06-01T00:00:00Z",
    revoked: bool = False,
) -> None:
    """The supplier block ADR-0073 requires, seeded as rows — the kernel writes these."""
    stamp = captured_at
    db.execute(
        "INSERT OR IGNORE INTO suppliers (supplier_id, name, status, created_at, updated_at)"
        " VALUES ('sup-1', 'Villa manager', 'ACTIVE', ?, ?)",
        (stamp, stamp),
    )
    db.execute(
        "INSERT OR IGNORE INTO villa_supplier_relationships"
        " (villa_supplier_relationship_id, supplier_id, property_id, unit_id, status,"
        " created_at, updated_at) VALUES ('vsr-1', 'sup-1', 'prop-1', 'unit-2', 'ACTIVE', ?, ?)",
        (stamp, stamp),
    )
    db.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level, source_type,"
        " source_ref, captured_at, valid_until, result, occupancy_ranges, can_quote, can_hold,"
        " can_confirm, created_at) VALUES (?, 'unit-2', 'B', 'manager_written',"
        " 'villa_block_letter', ?, ?, 'AVAILABLE', '[]', 1, 1, 1, ?)",
        (evidence_id, stamp, expires_at, stamp),
    )
    db.execute(
        "INSERT INTO supplier_block_proofs (proof_id, booking_id, unit_id, check_in, check_out,"
        " supplier_id, relationship_id, evidence_id, verification_method, verified_by,"
        " verified_at, expires_at, created_at, revoked_at, revoked_by, revoke_reason)"
        " VALUES (?, ?, 'unit-2', ?, ?, 'sup-1', 'vsr-1', ?, 'SUPPLIER_EMAIL',"
        " 'user:ops-1', ?, ?, ?, ?, ?, ?)",
        (
            proof_id,
            booking_id,
            check_in,
            check_out,
            evidence_id,
            stamp,
            expires_at,
            stamp,
            stamp if revoked else None,
            "user:ops-1" if revoked else None,
            "supplier withdrew the block" if revoked else None,
        ),
    )


def _manager_sessions(conn, clock, names: tuple) -> dict:
    """Real Reservation Manager sessions: role holder, enrolled TOTP, fresh step-up.

    G2-A: closing a booking or signing a block proof is a privileged human act, so the
    kernel resolves the session itself and refuses anything weaker. Each name gets its own
    factor (a TOTP step is single-use) and spends one step-up, exactly as a person would.
    """
    import totp as T
    from agency_roles import AgencyRoles
    from primitives import parse_iso

    secrets = {f"{name}-ref": T.generate_secret() for name in names}

    def stamp(seconds: int) -> str:
        return to_iso(clock.now() + timedelta(seconds=seconds))

    def code(name: str, seconds: int) -> str:
        return T.totp(secrets[f"{name}-ref"], parse_iso(stamp(seconds)))

    roles = AgencyRoles(conn, clock, agency_id="agency_default")
    roles.seed_blueprint(actor="owner-1", actor_role="owner")
    svc = identity_module.SessionService(
        conn, clock=clock, ids=IdFactory(deterministic=True, seed="g2m"),
        agency_id="agency_default", secrets=identity_module.MappingSecretResolver(secrets),
    )
    tokens: dict = {}
    for name in names:
        roles.assign_holder(
            "reservation_manager", holder_ref=name, actor="owner-1", actor_role="owner"
        )
        svc.enroll_totp(name, secret_ref=f"{name}-ref", at=stamp(-180))
        svc.confirm_totp(name, code(name, -180), at=stamp(-180))
        opened = svc.open_session(name, "reservation_manager", at=stamp(-20))
        svc.satisfy_step_up(opened.session.session_id, code(name, 0), at=stamp(0))
        tokens[name] = opened.token
    return tokens


@pytest.fixture()
def confirm_http(tmp_path, clock):
    """Seeded dashboard where one real Reservation Manager may close a paid booking."""
    with live_dashboard(
        tmp_path / "confirm.db",
        clock,
        seed_data=True,
        bootstrap=lambda conn: _manager_sessions(conn, clock, ("rm-confirm",)),
    ) as live:
        yield live


def _confirm_token(live, *, token: str = "tok-reservation") -> str:
    page = live.get("/inventory", token=token)
    assert page.status == 200, page.status
    start = page.text.find(CONFIRM_FORM)
    assert start != -1, "the confirmation form was not rendered"
    match = BOOKING_TOKEN_PATTERN.search(page.text, start)
    assert match, "the confirmation form carries no token"
    return match.group(1)


def _confirmation_row(booking_id: str, live) -> str:
    """The rendered row for one booking, so the assertions read real markup."""
    page = live.get("/inventory", token="tok-reservation")
    table = page.text.find('data-table="confirmation"')
    assert table != -1, "the confirmation panel is not on the page"
    start = page.text.find(f'data-booking-id="{booking_id}"', table)
    assert start != -1, "booking is not on the confirmation panel"
    end = page.text.find("</tr>", start)
    return page.text[start : end if end != -1 else start + 2000]


def test_confirmation_is_offered_only_with_a_live_supplier_proof(seeded_http):
    """The button appears with a real proof, and never without one."""
    _seed_stage_e_paid_booking(seeded_http.db)
    _seed_stage_e_paid_booking(
        seeded_http.db, booking_id="booking-e2", hold_id="hold-e2",
        check_in="2026-12-20", check_out="2026-12-23",
    )
    _seed_stage_e_proof(seeded_http.db, proof_id="proof-e1")

    proven = _confirmation_row("booking-e1", seeded_http)
    unproven = _confirmation_row("booking-e2", seeded_http)

    assert 'data-proof-state="VERIFIED"' in proven
    assert 'action="/bookings/confirm"' in proven
    assert "proof-e1" in proven and "user:ops-1" in proven
    assert 'data-proof-state="EVIDENCE_MISSING"' in unproven
    assert 'data-note="confirmation-blocked"' in unproven
    assert 'action="/bookings/confirm"' not in unproven


def test_confirmation_action_closes_the_booking_with_an_audit_reference(confirm_http):
    _seed_stage_e_paid_booking(confirm_http.db)
    _seed_stage_e_proof(confirm_http.db)
    token = _confirm_token(confirm_http, token=confirm_http.session_tokens["rm-confirm"])

    response = confirm_http.post(
        "/bookings/confirm",
        token=confirm_http.session_tokens["rm-confirm"],
        form={
            "form_token": token,
            "booking_id": "booking-e1",
            "block_proof_id": "proof-e1",
            "idempotency_key": "idem-confirm-e1",
        },
    )

    assert response.status == 200, response.text[-900:]
    assert 'data-banner="booking-confirmed"' in response.text
    assert 'data-status="CONFIRMED"' in response.text
    assert 'data-replayed="0"' in response.text
    assert 'data-audit-id=""' not in response.text
    booking = confirm_http.db.execute(
        "SELECT status, confirmed_at FROM bookings WHERE booking_id = 'booking-e1'"
    ).fetchone()
    assert booking["status"] == "CONFIRMED" and booking["confirmed_at"]
    hold = confirm_http.db.execute(
        "SELECT status FROM holds WHERE hold_id = 'hold-e1'"
    ).fetchone()
    assert hold["status"] == "CONVERTED"
    event = confirm_http.db.execute(
        "SELECT to_state, evidence_ref FROM booking_state_events"
        " WHERE booking_id = 'booking-e1' AND to_state = 'CONFIRMED'"
    ).fetchone()
    assert event["evidence_ref"] == "proof-e1"


def test_confirmation_refuses_an_expired_proof(confirm_http):
    _seed_stage_e_paid_booking(confirm_http.db)
    _seed_stage_e_proof(confirm_http.db, proof_id="proof-live")
    _seed_stage_e_paid_booking(
        confirm_http.db, booking_id="booking-e2", hold_id="hold-e2",
        check_in="2026-12-20", check_out="2026-12-23",
    )
    _seed_stage_e_proof(
        confirm_http.db,
        booking_id="booking-e2",
        proof_id="proof-stale",
        evidence_id="evid-block-2",
        check_in="2026-12-20",
        check_out="2026-12-23",
        captured_at="2026-09-02T00:00:00Z",
        expires_at="2026-09-10T00:00:00Z",
    )
    token = _confirm_token(confirm_http, token=confirm_http.session_tokens["rm-confirm"])

    response = confirm_http.post(
        "/bookings/confirm",
        token=confirm_http.session_tokens["rm-confirm"],
        form={
            "form_token": token,
            "booking_id": "booking-e2",
            "block_proof_id": "proof-stale",
            "idempotency_key": "idem-confirm-stale",
        },
    )

    assert response.status == 400, response.status
    assert 'data-code="BLOCK_PROOF_EXPIRED"' in response.text
    assert confirm_http.db.execute(
        "SELECT status FROM bookings WHERE booking_id = 'booking-e2'"
    ).fetchone()["status"] == "PAID_IN_FULL"


def test_confirmation_refuses_a_revoked_proof(confirm_http):
    _seed_stage_e_paid_booking(confirm_http.db)
    _seed_stage_e_proof(confirm_http.db, revoked=True)
    _seed_stage_e_paid_booking(
        confirm_http.db, booking_id="booking-e2", hold_id="hold-e2",
        check_in="2026-12-20", check_out="2026-12-23",
    )
    _seed_stage_e_proof(confirm_http.db, booking_id="booking-e2", proof_id="proof-live",
                        evidence_id="evid-block-2", check_in="2026-12-20",
                        check_out="2026-12-23")
    token = _confirm_token(confirm_http, token=confirm_http.session_tokens["rm-confirm"])

    response = confirm_http.post(
        "/bookings/confirm",
        token=confirm_http.session_tokens["rm-confirm"],
        form={
            "form_token": token,
            "booking_id": "booking-e1",
            "block_proof_id": "proof-e1",
            "idempotency_key": "idem-confirm-revoked",
        },
    )

    assert response.status == 400, response.status
    assert 'data-code="BLOCK_PROOF_REVOKED"' in response.text
    assert 'data-proof-state="REVOKED"' in _confirmation_row("booking-e1", confirm_http)


def test_confirmation_is_refused_for_finance_and_the_auditor(seeded_http):
    """Finance settles money; only Reservation or Operations closes the booking."""
    _seed_stage_e_paid_booking(seeded_http.db)
    _seed_stage_e_proof(seeded_http.db)
    before = seeded_http.counts()
    token = _confirm_token(seeded_http)

    finance = seeded_http.post(
        "/bookings/confirm",
        token="tok-finance",
        form={
            "form_token": token,
            "booking_id": "booking-e1",
            "block_proof_id": "proof-e1",
            "idempotency_key": "idem-confirm-finance",
        },
    )
    auditor = seeded_http.post(
        "/bookings/confirm",
        token="tok-auditor",
        form={
            "form_token": token,
            "booking_id": "booking-e1",
            "block_proof_id": "proof-e1",
            "idempotency_key": "idem-confirm-auditor",
        },
    )

    assert finance.status == 403 and auditor.status == 403
    assert seeded_http.counts() == before
    assert seeded_http.db.execute(
        "SELECT status FROM bookings WHERE booking_id = 'booking-e1'"
    ).fetchone()["status"] == "PAID_IN_FULL"


def test_a_second_confirmation_replays_instead_of_double_journalling(confirm_http):
    _seed_stage_e_paid_booking(confirm_http.db)
    _seed_stage_e_proof(confirm_http.db)
    token = _confirm_token(confirm_http, token=confirm_http.session_tokens["rm-confirm"])
    form = {
        "form_token": token,
        "booking_id": "booking-e1",
        "block_proof_id": "proof-e1",
        "idempotency_key": "idem-confirm-1",
    }

    first = confirm_http.post("/bookings/confirm", token=confirm_http.session_tokens["rm-confirm"], form=form)
    second = confirm_http.post(
        "/bookings/confirm",
        token=confirm_http.session_tokens["rm-confirm"],
        form={**form, "idempotency_key": "idem-confirm-2"},
    )

    assert first.status == 200 and second.status == 200
    assert 'data-replayed="1"' in second.text
    events = confirm_http.db.execute(
        "SELECT COUNT(*) FROM booking_state_events"
        " WHERE booking_id = 'booking-e1' AND to_state = 'CONFIRMED'"
    ).fetchone()[0]
    audits = confirm_http.db.execute(
        "SELECT COUNT(*) FROM audit_events"
        " WHERE entity_id = 'booking-e1' AND action = 'booking.confirmed'"
    ).fetchone()[0]
    assert (events, audits) == (1, 1)


# --------------------------------------------------------------------------- #
# Stage E2: recording the supplier block — a human signs, the kernel decides
# --------------------------------------------------------------------------- #
BLOCK_PROOF_FORM = 'data-form="record-block-proof"'
E2_EVIDENCE = "evid-e2-1"
E2_CAPTURED = "2026-09-21T09:00:00Z"
E2_VALID_UNTIL = "2027-06-01T00:00:00Z"


def _seed_block_supplier(db) -> None:
    """The supplier and the active relationship covering the booking's unit."""
    stamp = "2026-09-02T00:00:00Z"
    db.execute(
        "INSERT OR IGNORE INTO suppliers (supplier_id, name, status, created_at, updated_at)"
        " VALUES ('sup-1', 'Villa manager', 'ACTIVE', ?, ?)",
        (stamp, stamp),
    )
    db.execute(
        "INSERT OR IGNORE INTO villa_supplier_relationships"
        " (villa_supplier_relationship_id, supplier_id, property_id, unit_id, status,"
        " created_at, updated_at) VALUES ('vsr-1', 'sup-1', 'prop-1', 'unit-2', 'ACTIVE', ?, ?)",
        (stamp, stamp),
    )


def _seed_block_evidence(
    db,
    *,
    evidence_id: str = E2_EVIDENCE,
    unit_id: str = "unit-2",
    level: str = "B",
    result: str = "AVAILABLE",
    can_confirm: int = 1,
    restrictions=None,
    captured_at: str = E2_CAPTURED,
    valid_until: str = E2_VALID_UNTIL,
) -> None:
    """A supplier answer as the ingest path would store it — a row, not a claim."""
    db.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level, source_type,"
        " source_ref, captured_at, valid_until, result, restrictions, occupancy_ranges,"
        " can_quote, can_hold, can_confirm, created_at)"
        " VALUES (?, ?, ?, 'manager_written', 'block_letter_email', ?, ?, ?, ?, '[]', 1, 1, ?, ?)",
        (
            evidence_id,
            unit_id,
            level,
            captured_at,
            valid_until,
            result,
            restrictions,
            can_confirm,
            captured_at,
        ),
    )


def _banner_of(response) -> str:
    """The refusal banner (not the page footer), so a failure names its own cause."""
    found = re.search(r'<p class="err"[^>]*>.*?</p>', response.text, re.S)
    return f"{response.status}: {found.group(0) if found else response.text[:300]}"


def _block_proof_token(live, booking_id: str, *, token: str) -> str:
    page = live.get("/inventory", token=token)
    assert page.status == 200, (page.status, page.text[:400])
    start = page.text.find(f'action="/bookings/{booking_id}/block-proof"')
    assert start != -1, "the block-proof form was not rendered"
    match = BOOKING_TOKEN_PATTERN.search(page.text, start)
    assert match, "the block-proof form carries no token"
    return match.group(1)


def _post_block_proof(live, *, token=None, booking_id: str = "booking-e1", **fields):
    payload = {
        "evidence_id": E2_EVIDENCE,
        "relationship_id": "vsr-1",
        "verification_method": "SUPPLIER_EMAIL",
        "idempotency_key": "idem-form-0001",
    }
    payload.update(fields)
    return live.post(f"/bookings/{booking_id}/block-proof", token=token, form=payload)


def _proof_rows(live) -> list:
    return list(
        live.db.execute(
            "SELECT proof_id, booking_id, check_in, check_out, evidence_id, verified_by,"
            " verification_method, revoked_at FROM supplier_block_proofs"
        ).fetchall()
    )


@pytest.fixture()
def block_proof_http(tmp_path, clock):
    """Seeded dashboard, a paid booking, two real Reservation Managers and one machine.

    verifier-1/2 hold the manager role with TOTP and a fresh step-up (G2-A: a signature
    is a privileged human act). ``ai:planner-1`` keeps a plain agent session on purpose:
    it is the machine-principal negative.
    """
    with live_dashboard(
        tmp_path / "block-proof.db",
        clock,
        seed_data=True,
        sessions=(("ai:planner-1", "reservation_agent"),),
        bootstrap=lambda conn: _manager_sessions(conn, clock, ("verifier-1", "verifier-2")),
    ) as live:
        _seed_stage_e_paid_booking(live.db)
        yield live


def test_a_signed_in_human_records_the_block_and_unlocks_confirmation(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(live, token=token, form_token=form_token)

    assert response.status == 200, _banner_of(response)
    assert 'data-banner="block-proof-recorded"' in response.text
    assert 'data-replayed="0"' in response.text
    assert 'data-verified-by="human:verifier-1"' in response.text
    rows = _proof_rows(live)
    assert len(rows) == 1
    proof = rows[0]
    assert proof["booking_id"] == "booking-e1"
    assert proof["verified_by"] == "human:verifier-1"
    assert proof["verification_method"] == "SUPPLIER_EMAIL"
    assert proof["evidence_id"] == E2_EVIDENCE
    assert proof["revoked_at"] is None
    # The booking is still paid, not confirmed: recording is not confirming.
    status = live.db.execute(
        "SELECT status FROM bookings WHERE booking_id = 'booking-e1'"
    ).fetchone()["status"]
    assert status == "PAID_IN_FULL"
    # ... and confirmation, still a separate action, now has its proof.
    confirm_token = _confirm_token(live, token=token)
    confirmed = live.post(
        "/bookings/confirm",
        token=token,
        form={
            "form_token": confirm_token,
            "booking_id": "booking-e1",
            "block_proof_id": proof["proof_id"],
            "idempotency_key": "idem-e2-confirm-1",
        },
    )
    assert confirmed.status == 200, confirmed.text[-1200:]
    assert 'data-banner="booking-confirmed"' in confirmed.text


def test_block_proof_requires_authentication(block_proof_http):
    response = _post_block_proof(block_proof_http, token=None)
    assert response.status == 401
    assert _proof_rows(block_proof_http) == []


def test_block_proof_refuses_a_role_that_cannot_see_inventory(block_proof_http):
    response = _post_block_proof(block_proof_http, token="tok-finance")
    assert response.status == 403
    assert _proof_rows(block_proof_http) == []


def test_block_proof_refuses_a_shared_role_token_as_the_verifier(block_proof_http):
    live = block_proof_http
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    response = _post_block_proof(live, token="tok-reservation")

    assert response.status == 403
    assert "HUMAN_SESSION_REQUIRED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_a_machine_principal_as_the_verifier(block_proof_http):
    live = block_proof_http
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    response = _post_block_proof(live, token=live.session_tokens["ai:planner-1"])

    assert response.status == 403
    assert "MACHINE_PRINCIPAL_REFUSED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_a_forged_verifier_name(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, verified_by="human:owner-anas"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_VERIFIER_FORGED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_requires_a_valid_form_token(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    response = _post_block_proof(live, token=token, form_token="not-a-token")

    assert response.status == 403
    assert _proof_rows(live) == []


def test_block_proof_refuses_evidence_from_another_unit(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(live.db, evidence_id="evid-other-unit", unit_id="unit-1")
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-other-unit"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_WRONG_UNIT" in response.text
    assert _proof_rows(live) == []


def test_block_proof_ignores_dates_supplied_by_the_form(block_proof_http):
    """The stay is the server's fact: a form cannot move the proof to other nights."""
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live,
        token=token,
        form_token=form_token,
        check_in="2027-01-01",
        check_out="2027-01-02",
        unit_id="unit-1",
        verified_by="human:verifier-1",
    )

    assert response.status == 200, response.text[-1200:]
    proof = _proof_rows(live)[0]
    assert proof["check_in"] == "2026-12-10"
    assert proof["check_out"] == "2026-12-13"


def test_block_proof_refuses_a_restricted_answer(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(
        live.db,
        evidence_id="evid-e2-restricted",
        result="AVAILABLE_BUT_RESTRICTED",
        restrictions='{"min_stay_nights": 5}',
    )
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-e2-restricted"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_DATES_RESTRICTED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_evidence_that_is_not_authoritative(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(live.db, evidence_id="evid-e2-level-c", level="C", can_confirm=0)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-e2-level-c"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_NOT_AUTHORITATIVE" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_evidence_that_expired(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(
        live.db,
        evidence_id="evid-e2-stale",
        captured_at="2026-09-18T00:00:00Z",
        valid_until="2026-09-20T00:00:00Z",
    )
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-e2-stale"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_EXPIRED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_requires_the_supplier_answer(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(live, token=token, form_token=form_token, evidence_id="")

    assert response.status == 400
    assert _proof_rows(live) == []


def test_block_proof_replay_reports_the_existing_proof_without_writing_twice(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    first = _post_block_proof(live, token=token, form_token=form_token)
    second = _post_block_proof(live, token=token, form_token=form_token)

    assert first.status == 200 and second.status == 200, (first.status, second.status, second.text[-900:])
    assert 'data-replayed="0"' in first.text
    assert 'data-replayed="1"' in second.text
    assert len(_proof_rows(live)) == 1


def test_block_proof_refuses_to_replace_a_live_proof_with_other_evidence(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(live.db, evidence_id="evid-e2-2", captured_at="2026-09-21T10:00:00Z")
    form_token = _block_proof_token(live, "booking-e1", token=token)

    assert _post_block_proof(live, token=token, form_token=form_token).status == 200
    second = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-e2-2"
    )

    assert second.status >= 400
    assert "BLOCK_PROOF_ALREADY_RECORDED" in second.text
    assert len(_proof_rows(live)) == 1


def test_a_revoked_proof_does_not_authorize_confirmation(block_proof_http):
    live = block_proof_http
    _seed_stage_e_proof(live.db, revoked=True)

    page = live.get("/inventory", token="tok-reservation")

    assert page.status == 200
    assert 'data-proof-state="REVOKED"' in page.text
    assert 'action="/bookings/confirm"' not in page.text
    assert 'data-note="confirmation-blocked"' in page.text


def test_a_refused_block_proof_leaves_no_partial_write(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_block_evidence(live.db, evidence_id="evid-other-unit", unit_id="unit-1")
    form_token = _block_proof_token(live, "booking-e1", token=token)
    before = live.counts()

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-other-unit"
    )

    assert response.status >= 400
    assert live.counts() == before


def test_the_recording_form_appears_only_for_a_caller_who_can_be_the_verifier(block_proof_http):
    """A shared role token can read the workspace but cannot act inside it."""
    live = block_proof_http
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    human_page = live.get("/inventory", token=live.session_tokens["verifier-1"])
    static_page = live.get("/inventory", token="tok-reservation")

    assert 'data-proof-state="AWAITING_VERIFICATION"' in human_page.text
    assert BLOCK_PROOF_FORM in human_page.text
    assert 'data-proof-state="AWAITING_VERIFICATION"' in static_page.text
    assert BLOCK_PROOF_FORM not in static_page.text
    assert 'data-note="confirmation-blocked"' in static_page.text


def test_block_proof_cannot_touch_a_booking_of_another_agency(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, booking_id="booking-of-agency-b", form_token=form_token
    )

    assert response.status >= 400
    assert "BOOKING_NOT_FOUND" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_evidence_owned_by_another_agency(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    stamp = "2026-09-02T00:00:00Z"
    live.db.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_b', 'Agency B', 'ACTIVE', ?, ?)",
        (stamp, stamp),
    )
    live.db.execute(
        "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id, evidence_level,"
        " source_type, source_ref, captured_at, valid_until, result, occupancy_ranges,"
        " can_quote, can_hold, can_confirm, created_at)"
        " VALUES ('evid-agency-b', 'agency_b', 'unit-2', 'B', 'manager_written',"
        " 'other_agency_letter', ?, ?, 'AVAILABLE', '[]', 1, 1, 1, ?)",
        (E2_CAPTURED, E2_VALID_UNTIL, E2_CAPTURED),
    )
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-agency-b"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF" in response.text
    assert _proof_rows(live) == []


def test_a_second_person_cannot_replay_the_first_persons_verification(block_proof_http):
    """Resubmitting someone else's proof is not your verification."""
    live = block_proof_http
    first = live.session_tokens["verifier-1"]
    second = live.session_tokens["verifier-2"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    second_token = _block_proof_token(live, "booking-e1", token=second)
    assert _post_block_proof(
        live, token=first, form_token=_block_proof_token(live, "booking-e1", token=first)
    ).status == 200
    # The form is gone from the page now; the token is per person and still valid.
    response = _post_block_proof(live, token=second, form_token=second_token)

    assert response.status >= 400
    assert "BLOCK_PROOF_ALREADY_RECORDED" in response.text
    rows = _proof_rows(live)
    assert len(rows) == 1 and rows[0]["verified_by"] == "human:verifier-1"


def test_a_form_token_minted_for_one_person_is_refused_for_another(block_proof_http):
    live = block_proof_http
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    stolen = _block_proof_token(live, "booking-e1", token=live.session_tokens["verifier-1"])

    response = _post_block_proof(
        live, token=live.session_tokens["verifier-2"], form_token=stolen
    )

    assert response.status == 403
    assert "FORM_TOKEN_INVALID" in response.text
    assert _proof_rows(live) == []


def test_a_machine_session_is_not_offered_the_form(block_proof_http):
    live = block_proof_http
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    page = live.get("/inventory", token=live.session_tokens["ai:planner-1"])

    assert page.status == 200
    assert 'data-proof-state="AWAITING_VERIFICATION"' in page.text
    assert BLOCK_PROOF_FORM not in page.text


def test_block_proof_refuses_an_inactive_supplier_relationship(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)
    live.db.execute(
        "UPDATE villa_supplier_relationships SET status = 'SUSPENDED'"
        " WHERE villa_supplier_relationship_id = 'vsr-1'"
    )

    response = _post_block_proof(live, token=token, form_token=form_token)

    assert response.status >= 400
    assert "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_a_booking_that_is_not_paid(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    _seed_stage_d_booking(
        live.db,
        booking_id="booking-pending",
        quote_id="quote-pending",
        status="PAYMENT_PENDING",
        check_in="2026-12-15",
        check_out="2026-12-18",
        amount_paid_minor=0,
        payment_status="UNPAID",
    )
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, booking_id="booking-pending", form_token=form_token
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_BOOKING_NOT_PAYABLE" in response.text
    assert _proof_rows(live) == []


def test_block_proof_refuses_an_over_long_note(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    form_token = _block_proof_token(live, "booking-e1", token=token)

    response = _post_block_proof(
        live, token=token, form_token=form_token, notes="x" * 201
    )

    assert response.status == 400
    assert _proof_rows(live) == []


def test_an_expired_live_proof_is_not_reported_as_a_fresh_replay(block_proof_http):
    live = block_proof_http
    token = live.session_tokens["verifier-1"]
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)
    # The token has to be taken while the page still offers the form; the expired
    # proof lands afterwards, the way time would have moved in production.
    form_token = _block_proof_token(live, "booking-e1", token=token)
    _seed_stage_e_proof(
        live.db,
        evidence_id="evid-e2-expired",
        captured_at="2026-09-18T00:00:00Z",
        expires_at="2026-09-20T00:00:00Z",
    )

    response = _post_block_proof(
        live, token=token, form_token=form_token, evidence_id="evid-e2-expired"
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_ALREADY_RECORDED" in response.text
    assert 'data-banner="block-proof-recorded"' not in response.text


# --------------------------------------------------------------------------- #
# E3: revoking a supplier block from the workspace
# --------------------------------------------------------------------------- #

REVOKE_FORM = 'data-form="revoke-block-proof"'


def _revoke_form_token(live, booking_id: str, *, token: str) -> str:
    page = live.get("/inventory", token=token)
    assert page.status == 200, page.status
    start = page.text.find(f'action="/bookings/{booking_id}/block-proof/revoke"')
    assert start != -1, "the revoke form was not rendered"
    token_at = page.text.find('name="form_token"', start)
    assert token_at != -1
    value_at = page.text.find('value="', token_at) + len('value="')
    return page.text[value_at : page.text.find('"', value_at)]


def _revoke(
    live,
    *,
    token: str | None = None,
    booking_id: str = "booking-e1",
    **fields,
):
    payload = {"proof_id": "proof-e1", "reason": "villa withdrew the block"}
    payload.update(fields)
    return live.post(
        f"/bookings/{booking_id}/block-proof/revoke", token=token, form=payload
    )


def _audit_count(live, action: str) -> int:
    return int(
        live.db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = ?", (action,)
        ).fetchone()[0]
    )


def _verified_booking(live) -> str:
    """The fixture's paid booking, given a live proof; returns a verifier's token."""
    _seed_stage_e_proof(live.db)
    return live.session_tokens["verifier-1"]


def test_an_operator_revokes_a_block_and_the_trail_records_it(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)
    form_token = _revoke_form_token(live, "booking-e1", token=token)

    response = _revoke(
        live,
        token=token,
        form_token=form_token,
        proof_id="proof-e1",
        reason="supplier pulled the block",
        idempotency_key="idem-revoke-01",
    )

    assert response.status == 200, response.text[-900:]
    assert 'data-banner="block-proof-revoked"' in response.text
    row = live.db.execute(
        "SELECT revoked_at, revoked_by, revoke_reason FROM supplier_block_proofs"
        " WHERE proof_id = 'proof-e1'"
    ).fetchone()
    assert row["revoked_at"] is not None
    assert row["revoked_by"] == "human:verifier-1"
    assert row["revoke_reason"] == "supplier pulled the block"
    assert _audit_count(live, "supplier_block.revoked") == 1
    assert 'action="/bookings/confirm"' not in response.text


def test_revoking_twice_writes_once_and_says_so(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)
    form_token = _revoke_form_token(live, "booking-e1", token=token)
    first = _revoke(live, token=token, form_token=form_token, reason="pulled")

    again = _revoke(live, token=token, form_token=form_token, reason="pulled")

    assert first.status == 200 and again.status == 200
    assert 'data-replayed="1"' in again.text
    assert _audit_count(live, "supplier_block.revoked") == 1


def test_revocation_needs_a_reason_and_writes_nothing_without_one(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)
    form_token = _revoke_form_token(live, "booking-e1", token=token)

    response = _revoke(live, token=token, form_token=form_token, reason="  ")

    assert response.status >= 400
    assert "BLOCK_PROOF_REVOCATION_NEEDS_REASON" in response.text
    row = live.db.execute(
        "SELECT revoked_at FROM supplier_block_proofs WHERE proof_id = 'proof-e1'"
    ).fetchone()
    assert row["revoked_at"] is None
    assert _audit_count(live, "supplier_block.revoked") == 0


def test_revocation_cannot_reach_another_bookings_proof(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)
    _seed_stage_d_booking(
        live.db,
        booking_id="booking-other",
        quote_id="quote-other",
        check_in="2026-12-20",
        check_out="2026-12-22",
    )
    # The token is per person, not per form: take it where a form exists, then
    # point the request at a booking whose proof belongs to someone else.
    form_token = _revoke_form_token(live, "booking-e1", token=token)

    response = _revoke(
        live, token=token, booking_id="booking-other", form_token=form_token,
        proof_id="proof-e1",
    )

    assert response.status >= 400
    assert "BLOCK_PROOF_NOT_FOR_BOOKING" in response.text
    row = live.db.execute(
        "SELECT revoked_at FROM supplier_block_proofs WHERE proof_id = 'proof-e1'"
    ).fetchone()
    assert row["revoked_at"] is None


def test_a_shared_role_token_cannot_revoke(block_proof_http):
    live = block_proof_http
    _verified_booking(live)
    page = live.get("/inventory", token="tok-reservation")

    assert REVOKE_FORM not in page.text
    response = _revoke(live, token="tok-reservation", form_token="bogus-token")
    assert response.status == 403
    assert "HUMAN_SESSION_REQUIRED" in response.text


def test_a_machine_session_cannot_revoke(block_proof_http):
    live = block_proof_http
    _verified_booking(live)

    response = _revoke(
        live, token=live.session_tokens["ai:planner-1"], form_token="bogus-token"
    )

    assert response.status == 403
    assert "MACHINE_PRINCIPAL_REFUSED" in response.text


def test_a_revoked_proof_leaves_room_for_a_replacement(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)
    form_token = _revoke_form_token(live, "booking-e1", token=token)
    _revoke(live, token=token, form_token=form_token, reason="wrong letter")
    _seed_block_supplier(live.db)
    _seed_block_evidence(live.db)

    response = _post_block_proof(
        live, token=token, form_token=_block_proof_token(live, "booking-e1", token=token)
    )

    assert response.status == 200, response.text[-900:]
    assert 'data-banner="block-proof-recorded"' in response.text
    live_rows = live.db.execute(
        "SELECT COUNT(*) FROM supplier_block_proofs WHERE revoked_at IS NULL"
    ).fetchone()[0]
    assert live_rows == 1
    assert _audit_count(live, "supplier_block.revoked") == 1
    assert _audit_count(live, "supplier_block.recorded") == 1


def test_revocation_needs_the_form_token(block_proof_http):
    live = block_proof_http
    token = _verified_booking(live)

    response = _revoke(live, token=token, form_token="bogus-token")

    assert response.status == 403
    assert "FORM_TOKEN_INVALID" in response.text
    row = live.db.execute(
        "SELECT revoked_at FROM supplier_block_proofs WHERE proof_id = 'proof-e1'"
    ).fetchone()
    assert row["revoked_at"] is None


# --------------------------------------------------------------------------- #
# Stage E5: supplier contact registry and the booking-specific assertion screen
# --------------------------------------------------------------------------- #
def _e5_people(conn, clock):
    """Role holders, TOTP factors and three signed-in humans — the minimum E5 writes need.

    A registry action must come from a *session* (the kernel refuses a static role token),
    and an owner/gm session needs MFA at login, so the fixture mints real factors. The
    reservation manager then steps up once, which is what a signature spends.
    """
    import totp as T
    from agency_roles import AgencyRoles
    from primitives import parse_iso

    people = (("owner-1", "owner"), ("gm-1", "gm"), ("rm-1", "reservation_manager"))
    secrets = {f"{name}-ref": T.generate_secret() for name, _ in people}

    def stamp(seconds: int) -> str:
        return to_iso(clock.now() + timedelta(seconds=seconds))

    def code(name: str, seconds: int) -> str:
        return T.totp(secrets[f"{name}-ref"], parse_iso(stamp(seconds)))

    roles = AgencyRoles(conn, clock, agency_id="agency_default")
    roles.seed_blueprint(actor="owner-1", actor_role="owner")
    for name, role in people:
        roles.assign_holder(role, holder_ref=name, actor="owner-1", actor_role="owner")

    svc = identity_module.SessionService(
        conn, clock=clock, ids=IdFactory(deterministic=True, seed="e5"),
        agency_id="agency_default", secrets=identity_module.MappingSecretResolver(secrets),
    )
    for name, _ in people:
        svc.enroll_totp(name, secret_ref=f"{name}-ref", at=stamp(-180))
        svc.confirm_totp(name, code(name, -180), at=stamp(-180))
    owner = svc.open_session("owner-1", "owner", mfa_code=code("owner-1", -60), at=stamp(-60))
    gm = svc.open_session("gm-1", "gm", mfa_code=code("gm-1", -30), at=stamp(-30))
    rm = svc.open_session("rm-1", "reservation_manager", at=stamp(-20))
    svc.satisfy_step_up(rm.session.session_id, code("rm-1", 0), at=stamp(0))
    return {"owner": owner.token, "gm": gm.token, "rm": rm.token}


@pytest.fixture()
def supplier_http(tmp_path, clock):
    """Seeded dashboard where the three E5 humans hold real roles and real sessions."""
    with live_dashboard(
        tmp_path / "supplier-assertion.db",
        clock,
        seed_data=True,
        bootstrap=lambda conn: _e5_people(conn, clock),
    ) as live:
        _seed_stage_e_paid_booking(live.db)
        _seed_block_supplier(live.db)
        yield live


def _form_token_after(live, anchor: str, *, token: str, path: str = "/properties") -> str:
    page = live.get(path, token=token)
    assert page.status == 200, page.text[:400]
    start = page.text.find(anchor)
    assert start != -1, f"form not rendered: {anchor}"
    match = BOOKING_TOKEN_PATTERN.search(page.text, start)
    assert match, "the form carries no token"
    return match.group(1)


def _register_contact(live, *, value: str = "+62 812 3456 7890", key: str = "idem-http-reg-1"):
    token = live.session_tokens["owner"]
    form_token = _form_token_after(live, 'action="/supplier-contacts"', token=token)
    return live.post("/supplier-contacts", token=token, form={
        "form_token": form_token, "supplier_id": "sup-1", "contact_value": value,
        "registration_basis": "SIGNED_AGREEMENT", "idempotency_key": key,
    })


def _only_contact_id(live) -> str:
    row = live.db.execute("SELECT contact_id FROM supplier_contacts").fetchone()
    assert row is not None, "the contact was not written"
    return str(row[0])


def _verify_contact(live, contact_id: str, *, key: str = "idem-http-ver-1"):
    token = live.session_tokens["gm"]
    form_token = _form_token_after(
        live, f'action="/supplier-contacts/{contact_id}/verify"', token=token
    )
    return live.post(f"/supplier-contacts/{contact_id}/verify", token=token, form={
        "form_token": form_token, "idempotency_key": key,
    })


def test_the_screen_registers_a_contact_and_a_second_human_checks_it(supplier_http):
    live = supplier_http
    registered = _register_contact(live)
    assert registered.status == 200, registered.text[-900:]
    assert 'data-banner="supplier-contact-registered"' in registered.text
    assert 'data-state="UNVERIFIED"' in registered.text
    assert "PENDING" in registered.text

    contact_id = _only_contact_id(live)
    checked = _verify_contact(live, contact_id)
    assert checked.status == 200, checked.text[-900:]
    assert 'data-banner="supplier-contact-verified"' in checked.text
    assert 'data-state="VERIFIED"' in checked.text

    rows = live.db.execute(
        "SELECT c.created_by, e.actor, e.kind FROM supplier_contacts c"
        " JOIN supplier_contact_events e ON e.contact_id = c.contact_id"
    ).fetchall()
    assert [(r["created_by"], r["actor"], r["kind"]) for r in rows] == [
        ("owner-1", "gm-1", "VERIFIED")
    ]


def test_the_screen_never_prints_the_registered_number_in_full(supplier_http):
    live = supplier_http
    response = _register_contact(live, value="+62 812 3456 7890")
    assert "+62 812 3456 7890" not in response.text
    assert "7890" in response.text  # last four, to recognise the row


def test_the_maker_cannot_check_their_own_registration_through_the_screen(supplier_http):
    live = supplier_http
    _register_contact(live)
    contact_id = _only_contact_id(live)
    token = live.session_tokens["owner"]
    # The verify form is not offered to the maker, so the write is attempted directly.
    response = live.post(f"/supplier-contacts/{contact_id}/verify", token=token, form={
        "form_token": _form_token_after(live, 'action="/supplier-contacts"', token=token),
        "idempotency_key": "idem-http-self-check",
    })
    assert "CONTACT_CHECKER_IS_MAKER" in response.text
    events = live.db.execute("SELECT COUNT(*) FROM supplier_contact_events").fetchone()[0]
    assert events == 0


def test_the_assertion_form_is_offered_only_once_a_contact_is_verified(supplier_http):
    live = supplier_http
    rm = live.session_tokens["rm"]
    before = live.get("/inventory", token=rm)
    assert before.status == 200
    assert 'action="/bookings/booking-e1/supplier-assertion"' not in before.text
    assert 'data-note="supplier-assertion-contact-missing"' in before.text

    _register_contact(live)
    _verify_contact(live, _only_contact_id(live))

    after = live.get("/inventory", token=rm)
    assert 'data-form="record-supplier-assertion"' in after.text
    start = after.text.find('action="/bookings/booking-e1/supplier-assertion"')
    assert start != -1
    form = after.text[start:start + 4000]
    assert 'name="check_in" value="2026-12-10"' in form
    assert 'name="check_out" value="2026-12-13"' in form
    assert "+62 812 3456 7890" not in after.text


def test_a_stepped_up_reservation_manager_records_the_assertion_and_the_screen_shows_it(
    supplier_http, clock
):
    live = supplier_http
    _register_contact(live)
    contact_id = _only_contact_id(live)
    _verify_contact(live, contact_id)

    rm = live.session_tokens["rm"]
    form_token = _form_token_after(
        live, 'action="/bookings/booking-e1/supplier-assertion"', token=rm, path="/inventory"
    )
    response = live.post("/bookings/booking-e1/supplier-assertion", token=rm, form={
        "form_token": form_token, "supplier_id": "sup-1", "relationship_id": "vsr-1",
        "contact_id": contact_id, "check_in": "2026-12-10", "check_out": "2026-12-13",
        "supplier_confirmed_at": to_iso(clock.now()), "idempotency_key": "idem-http-assert-1",
    })
    assert response.status == 200, response.text[-900:]
    assert 'data-banner="supplier-assertion-recorded"' in response.text
    assert 'data-assertion-state="FRESH"' in response.text

    row = live.db.execute(
        "SELECT booking_id, callback_by, callback_role FROM evidence_supplier_attributions"
    ).fetchone()
    assert (row["booking_id"], row["callback_by"], row["callback_role"]) == (
        "booking-e1", "rm-1", "reservation_manager"
    )
    # The booking is still paid: an assertion is evidence, never a confirmation.
    status = live.db.execute(
        "SELECT status FROM bookings WHERE booking_id = 'booking-e1'"
    ).fetchone()["status"]
    assert status == "PAID_IN_FULL"


def test_a_static_role_token_cannot_read_or_write_the_registry(supplier_http):
    live = supplier_http
    page = live.get("/properties", token="tok-owner")
    assert page.status == 200
    assert 'data-note="supplier-contacts-unavailable"' in page.text
    assert "SESSION_REQUIRED" in page.text
    assert 'data-form="register-supplier-contact"' not in page.text

    response = live.post("/supplier-contacts", token="tok-owner", form={
        "supplier_id": "sup-1", "contact_value": "+62 812 3456 7890",
        "registration_basis": "SIGNED_AGREEMENT", "idempotency_key": "idem-http-static",
    })
    assert response.status in (400, 403, 409), response.status
    assert live.db.execute("SELECT COUNT(*) FROM supplier_contacts").fetchone()[0] == 0

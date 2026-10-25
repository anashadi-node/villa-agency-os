"""Internal dashboard — read-only operational views, approval-only write path.

The dashboard is the human window onto the kernel: it *shows* what the database
says and offers exactly one way to change anything — the existing approval queue.
It never becomes a second writer.

Invariants (each has a test in ``tests/test_dashboard.py``):

1. **Read-only for data.** Every GET handler only ever SELECTs. The single write
   path is ``POST /approvals`` (and ``POST /approvals/<id>/decide``), which calls
   :class:`approvals.ApprovalService` — the module that owns those tables.
   Nothing here issues an INSERT/UPDATE/DELETE. A full crawl of every GET route
   leaves the row count of every table unchanged.
2. **Real, if simple, authentication.** Identity *and* role come from a bearer
   token — the ``X-Dashboard-Token`` header, or an ``Authorization`` header whose
   scheme is ``Bearer`` — mapped to a principal. No token ⇒ 401. A token whose
   role may not open a page ⇒ 403. The identity is never taken from the request
   body or the query string.
3. **Deny by default.** :data:`PAGE_ROLES` lists the roles allowed per page; a
   page absent from that mapping is refused for everyone. ``/finance`` is
   finance/owner only.
4. **Nothing dynamic is unescaped.** :func:`render_page` escapes every value it
   substitutes; only :class:`Markup` — HTML this module built from already
   escaped values — bypasses escaping. Guest names, message bodies, notes and
   payloads are attacker-controlled and flow through it.
5. **No raw PII on a page whose role is not entitled.** Free text is passed
   through :func:`redact_pii` (``b***@example.com``, phone and identity numbers
   dropped) unless the role is entitled; when a classification gate is injected
   the gate has the final say and can only *remove* entitlement.
6. **P3/P4 columns are never read.** The queries below touch identity-free
   columns only, and a body that was never stored (P3/P4) is rendered as
   "not stored" rather than invented.
7. **Idempotency before every write.** A POST without a usable
   ``idempotency_key`` is refused with 400 and writes nothing at all.

Stdlib only: ``http.server``, no framework, no template engine.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

import anchors as anchors_module
import gates as gates_module
import shadow_eval as shadow_eval_module
from approvals import (
    MIN_IDEMPOTENCY_KEY_LENGTH,
    ROLE_RANK,
    SUBJECT_TYPES,
    ApprovalService,
)
from audit import AuditEvent, ChainAnchor, verify_chain
from decimal import Decimal, InvalidOperation

from hold_states import LIVE_HOLD_STATES, OCCUPYING_HOLD_STATES
from primitives import (
    DEFAULT_AGENCY_ID,
    resolve_agency_id,
    MINOR_UNITS,
    DomainError,
    IdFactory,
    Money,
    SystemClock,
    require_ref,
    require_text,
    to_iso,
)
from schema_loader import applied_version

__all__ = [
    "ALL_ROLES",
    "APPROVAL_ROLE_BY_ROLE",
    "DEFAULT_HOST",
    "KPI_KEYS",
    "PAGE_ROLES",
    "ROLES",
    "Dashboard",
    "DashboardError",
    "DashboardRequestHandler",
    "Forbidden",
    "InvalidInput",
    "Markup",
    "MethodNotAllowed",
    "MissingIdempotencyKey",
    "NotFound",
    "Principal",
    "RemoteBindRefused",
    "TemplateError",
    "Unauthenticated",
    "build_arg_parser",
    "create_app",
    "dump_all",
    "escape_value",
    "format_money",
    "main",
    "mask_email",
    "redact_pii",
    "render_page",
    "resolve_bind",
    "serve",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATES_DIR = REPO_ROOT / "templates"

#: loopback only: a dashboard bound to the world is an unauthenticated leak.
DEFAULT_HOST = "127.0.0.1"
WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "*", "")

#: The six dashboard roles. Deliberately not the approval roles (see below).
#: ``gm`` is here because ADR-0036 put the GM on every money-out path: a role that can
#: approve a payment but cannot open the page that shows it is not wired, just named.
ROLES: tuple[str, ...] = ("reservation", "finance", "operations", "owner", "auditor", "gm")
ALL_ROLES = ROLES

#: Dashboard role → the role the approval kernel knows (``approvals.ROLE_RANK``, whose one
#: home is ``roles``). ``auditor`` is absent on purpose: an auditor reads, never decides.
#: The values are the canonical ids of ADR-0035; the legacy names (``agent``, ``manager``,
#: ``accountant``) still resolve through ``roles.ROLE_ALIASES`` so rows written before the
#: reconciliation keep deciding, but nothing new is written under them.
APPROVAL_ROLE_BY_ROLE: Mapping[str, str] = MappingProxyType(
    {
        "reservation": "reservation_agent",
        "operations": "reservation_manager",
        "finance": "finance_manager",
        "owner": "owner",
        "gm": "gm",
    }
)

#: Roles allowed to open a page. Anything not listed is refused for that role.
PAGE_ROLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "command_center": ALL_ROLES,
        "cases": ("reservation", "operations", "owner", "auditor", "gm"),
        "case_detail": ("reservation", "operations", "owner", "auditor", "gm"),
        "inventory": ("reservation", "operations", "owner"),
        "finance": ("finance", "owner", "gm"),
        "operations": ("reservation", "operations", "owner"),
        "approvals": ("reservation", "operations", "finance", "owner", "auditor", "gm"),
        "properties": ALL_ROLES,
        "marketing": ("reservation", "operations", "owner", "auditor"),
        "channels": ("reservation", "operations", "owner"),
        "shadow_eval": ("operations", "owner", "auditor"),
        "outbox": ("operations", "owner", "auditor"),
        "audit": ("owner", "auditor", "gm"),
        "error": ALL_ROLES,
    }
)

#: Roles allowed to *act* (approval writes). An auditor is read-only by design.
WRITE_ROLES: tuple[str, ...] = ("reservation", "operations", "finance", "owner", "gm")

#: Roles for which free text keeps its identifiers. A rendered page is treated as
#: an outbound copy of the record, so this follows the stricter export rule of
#: ``docs/DATA_CLASSIFICATION_AND_ACCESS.md`` §2, not the in-store rule.
PII_ROLES: tuple[str, ...] = ("operations", "finance")

#: Dashboard role → the role string the classification gate evaluates against.
#: ``gm`` maps to itself and is listed in the gate's P2/P3 export roles (owner decision
#: 2026-10-02: the GM reads the audit page and may export the data behind it). ``auditor``
#: stays a reader: it is in no export list, so an auditor can look and not take.
GATE_ROLE_BY_ROLE: Mapping[str, str] = MappingProxyType(
    {
        "reservation": "agent",
        "operations": "manager",
        "finance": "finance",
        "owner": "owner",
        "auditor": "auditor",
        "gm": "gm",
    }
)

#: The gate interface this module relies on when a gate is injected.
GATE_INTERFACE: tuple[str, ...] = ("gate_outbound", "ClassifiedField")
GATE_DESTINATION = getattr(gates_module, "DESTINATION_EXPORT", "export")

#: Counters shown on the Command Center.
KPI_KEYS: tuple[str, ...] = (
    "cases_by_status",
    "open_sla_breaches",
    "pending_approvals",
    "holds_expiring_today",
    "unpaid_invoices",
    "unmatched_payments",
    "late_ops_tasks",
    "open_incidents",
    "audit_chain",
)
FINANCE_KPI_KEYS: tuple[str, ...] = ("unpaid_invoices", "unmatched_payments")
FINANCE_KPI_ROLES: tuple[str, ...] = ("finance", "owner", "gm")

#: Booking statuses that consume a unit's calendar day.
BOOKED_BOOKING_STATUSES: tuple[str, ...] = (
    "DEPOSIT_PAID",
    "PAID_IN_FULL",
    "CONFIRMED",
    "BALANCE_DUE",
    "BALANCE_OVERDUE",
    "PRE_ARRIVAL",
    "IN_STAY",
    "COMPLETED",
    "CANCELLATION_REQUESTED",
    "CANCELLATION_REVIEW",
    "REFUND_PENDING",
    "REFUNDED",
    "NO_SHOW",
    "RELOCATED",
)

ACTIVE_TASK_STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "BLOCKED")
OPEN_INCIDENT_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING")
OPEN_SLA_STATUS = "OPEN"
PENDING_APPROVAL_STATUS = "PENDING"

#: The calendar window ``/inventory`` renders.
CALENDAR_DAYS = 7

#: How far back ``/channels`` counts what arrived, and how far back
#: ``/shadow_eval`` measures an extractor (ADR-0038's rolling window).
CHANNEL_WINDOW_DAYS = 30
SHADOW_WINDOW_DAYS = 90

#: How far back ``/outbox`` lists recent events, and which undelivered states
#: the queue note counts as "still waiting".
OUTBOX_WINDOW_DAYS = 7
OUTBOX_LIVE_STATES = ("PENDING", "FAILED")
MARKETING_WINDOW_DAYS = 14

#: Hard cap on an operational queue: a queue is read at a glance, and an
#: unbounded table is how one bad row turns a page into a wall.
QUEUE_LIMIT = 50

#: The channels the schema allows, in the order the page lists them. A channel
#: with no traffic keeps its row with a zero, so "nothing arrived" is shown
#: rather than implied by a missing line.
CHANNEL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "website": "Website",
        "email": "Email",
        "whatsapp_business": "WhatsApp Business",
        "dashboard": "Dashboard",
        "phone": "Phone",
    }
)

#: Draft statuses that mean "a human approved it, nobody has sent it yet".
UNSENT_DRAFT_STATUSES: tuple[str, ...] = ("APPROVED",)

#: Hard cap on a POST body: a dashboard form is small, and an unbounded read is
#: how a trivial request becomes a memory exhaustion.
MAX_FORM_BYTES = 64 * 1024

# Same rule as the marketing site: one stalled socket must not own the
# process. ``http.server`` is deliberately single-threaded (see ``serve``), so
# without a read timeout a half-sent request blocks the operator's dashboard
# for as long as the client stays connected.
CONNECTION_TIMEOUT_SECONDS = 20

#: Depth of the listen queue. ``socketserver`` defaults to 5, and on Linux a full
#: accept queue drops new SYNs — the kernel then retries them one second later.
#: Measured on this repo's marketing site: 20 simultaneous visitors cost 1248 ms
#: at the default depth and 83 ms at 64, with identical per-request latency.
#: This buffers *connections*, not requests; serving stays single-threaded.
REQUEST_QUEUE_SIZE = 64

DEFAULT_IDENTITIES: Mapping[str, str] = MappingProxyType(
    {
        "reservation": "agent-reservation-1",
        "finance": "agent-finance-1",
        "operations": "agent-ops-1",
        "owner": "owner-sakti-1",
        "auditor": "auditor-external-1",
        "gm": "gm-ceo-1",
    }
)

#: Dashboard role -> the human-readable org title from the ADR-0035 blueprint
#: (``agency_roles.py``). Presentation only: it never changes ``principal.role``,
#: ``PAGE_ROLES`` gating, or ``APPROVAL_ROLE_BY_ROLE`` — it only labels the header
#: so a page reads like "who is looking at this", matching the real org chart.
ROLE_DISPLAY_LABEL: Mapping[str, str] = MappingProxyType(
    {
        "reservation": "Reservation Agent (AI-assisted desk)",
        "operations": "Operations / Reservation Manager",
        "finance": "Finance & Accounting Manager",
        "owner": "Owner",
        "gm": "General Manager (GM/CEO)",
        "auditor": "Auditor (read-only)",
    }
)

#: Every dashboard page, in navigation order.
NAV: tuple[tuple[str, str], ...] = (
    ("command_center", "/"),
    ("cases", "/cases"),
    ("inventory", "/inventory"),
    ("finance", "/finance"),
    ("operations", "/operations"),
    ("approvals", "/approvals"),
    ("properties", "/properties"),
    ("marketing", "/marketing"),
    ("channels", "/channels"),
    ("shadow_eval", "/shadow_eval"),
    ("outbox", "/outbox"),
    ("audit", "/audit"),
)
PAGE_TITLES: Mapping[str, str] = MappingProxyType(
    {
        "command_center": "Command Center",
        "cases": "Kasus",
        "case_detail": "Detail Kasus",
        "inventory": "Kalender Unit",
        "finance": "Keuangan",
        "operations": "Operasi",
        "approvals": "Antrean Approval",
        "properties": "Properti & Unit",
        "marketing": "Marketing Surface",
        "channels": "Channels",
        "shadow_eval": "AI Quality (Shadow)",
        "outbox": "Outbox & Integrations",
        "audit": "Rantai Audit",
        "error": "Galat",
    }
)
_DUMP_NAMES: Mapping[str, str] = MappingProxyType(
    {"command_center": "index", "case_detail": "case"}
)

#: Visual grouping for the nav — mirrors the agency's real org chart (ADR-0035
#: blueprint in ``agency_roles.py``): Reservations/Guest, Operations, Finance,
#: Approval & Compliance, Property & Partnership, Commercial & Marketing,
#: Technology & Channels. Presentation only — it reorders how ``nav_links()``
#: *displays* the same 12 ``NAV`` pages into department headers; it changes
#: nothing about ``PAGE_ROLES`` gating, routes, or ``NAV`` itself (which other
#: code, e.g. the request dispatcher, still iterates independently).
#: ``tests/test_dashboard.py::test_nav_groups_cover_every_nav_page`` guards
#: that this set never drifts from ``NAV``.
NAV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("", ("command_center",)),
    ("Reservations & Guests", ("cases", "inventory")),
    ("Operations", ("operations",)),
    ("Finance", ("finance",)),
    ("Approvals & Compliance", ("approvals", "audit")),
    ("Property & Partnerships", ("properties",)),
    ("Commercial & Marketing", ("marketing",)),
    ("Technology & Channels", ("channels", "outbox", "shadow_eval")),
)

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?62|0)8[1-9][0-9]{6,11}")
_ID_NUMBER_RE = re.compile(r"\b\d{16}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{8,}\b")


def mask_email(value: str) -> str:
    """``budi@example.com`` → ``b***@example.com`` (domain kept, local masked)."""
    local, _, domain = value.partition("@")
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def redact_pii(text: Any) -> str:
    """Drop identifiers from free text: masked email, no phone, no ID number."""
    if text is None:
        return ""
    out = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), str(text))
    out = _PHONE_RE.sub("[TELEPON_DIHAPUS]", out)
    out = _ID_NUMBER_RE.sub("[NOMOR_IDENTITAS_DIHAPUS]", out)
    out = _LONG_DIGITS_RE.sub("[ANGKA_DIHAPUS]", out)
    return out


def emit_dashboard_log(line: str) -> None:
    """The single sink for dashboard log lines.

    Everything the dashboard logs goes through here, already redacted, so a
    deployment can route it (or silence it in tests) without touching the
    handler — and so no code path can accidentally log a raw request line.
    """
    sys.stderr.write(line + "\n")


def format_money(minor: Any, currency: str = "") -> str:
    """Render integer minor units without ever introducing a float.

    The exponent comes from :data:`primitives.MINOR_UNITS` — IDR has none, so
    ``4_500_000`` IDR is ``IDR 4,500,000``, not ``IDR 45,000.00``.
    """
    try:
        amount = int(minor)
    except (TypeError, ValueError):
        return "—"
    exponent = MINOR_UNITS.get(str(currency).upper(), 2) if currency else 2
    scale = 10 ** exponent
    units, remainder = divmod(abs(amount), scale)
    sign = "-" if amount < 0 else ""
    body = f"{sign}{units:,}"
    if exponent:
        body += f".{remainder:0{exponent}d}"
    return f"{currency} {body}".strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rate_percent(value: Any) -> str:
    """A rate that is already a fraction, rendered as a percent without a float.

    ``shadow_eval`` hands back a :class:`Decimal`; going through a float to print
    it would be the one place a rate could come back as ``94.99999%``.
    """
    if value is None:
        return "—"
    try:
        percent = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        return "—"
    return f"{percent.normalize():f}%"


def _shadow_verdict(measurement: Any, min_cases: int) -> str:
    """Why an extractor does or does not meet the bar — the reason, not a lone flag.

    The order mirrors ``shadow_eval``'s own rule: volume first, then a rate that
    exists, then the unsupported-claim veto — ADR-0038 ranks that veto above the
    rate on purpose, because one claim with no source refuses the bar even at
    perfect agreement.
    """
    if measurement.cases < min_cases:
        return f"not enough evidence ({measurement.cases}/{min_cases} cases)"
    if measurement.agreement_rate is None:
        return "no decided field yet"
    if measurement.unsupported:
        return f"refused: {measurement.unsupported} unsupported claim(s)"
    return "meets the bar" if measurement.meets_bar else "below the bar"


def _is_pg_conn(conn: Any) -> bool:
    """Whether this connection speaks the PostgreSQL wire protocol."""
    module = type(conn).__module__.split(".", 1)[0]
    return module in ("psycopg", "psycopg2")


def _driver_rows(conn: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    """Run a read query through whichever driver the connection carries.

    SQLite takes ``?`` and ``conn.execute`` directly. PostgreSQL (psycopg 2/3)
    takes ``%s`` and needs a cursor, and its default rows are bare tuples — but
    the views read columns **by name** (``row["case_id"]``, 179 sites). The
    cursor's ``description`` gives the names, so rows are wrapped once here,
    in the only place driver differences are allowed to exist.
    """
    if not _is_pg_conn(conn):
        return list(conn.execute(sql, tuple(params)))
    translated = _positional_to_psql(sql)
    with conn.cursor() as cur:
        cur.execute(translated, tuple(params))
        names = [str(d[0]) for d in cur.description or ()]
        return [_PgRow(names, pg_row) for pg_row in cur.fetchall()]


class _PgRow(Mapping[str, Any]):
    """A mapping view over one cursor row: ``row["col"]`` and ``row[i]`` both work."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        object.__setattr__(self, "_names", tuple(names))
        object.__setattr__(self, "_values", tuple(values))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[int(key)]

    def __iter__(self):
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._values)


def _positional_to_psql(sql: str) -> str:
    """Rewrite ``?`` placeholders to psycopg ``%s``, never inside quotes.

    A naive replace would corrupt literal question marks inside string
    literals; walk the SQL character by character, tracking quote state.
    """
    out = []
    in_single = in_double = False
    for char in sql:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


def _age_minutes(value: Any, now: Any) -> Optional[int]:
    """How long something has been waiting, in whole minutes.
    A timestamp stored without a zone cannot be measured against an aware clock;
    the caller shows ``—`` instead of guessing, because a wrong age on a queue page
    is how a stuck item stays invisible.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return max(0, int((now - moment).total_seconds() // 60))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DashboardError(DomainError):
    """Base class for dashboard refusals (they always carry a stable code)."""


class Unauthenticated(DashboardError):
    """No usable bearer token."""

    def __init__(self, message: str = "identitas tidak dikenali", **details: Any) -> None:
        super().__init__(message, code="UNAUTHENTICATED", **details)


class Forbidden(DashboardError):
    """A known principal asking for something its role may not have."""

    def __init__(
        self, message: str = "peran ini tidak boleh membuka halaman itu", **details: Any
    ) -> None:
        super().__init__(message, code="FORBIDDEN", **details)


class NotFound(DashboardError):
    """Unknown route or unknown record."""

    def __init__(self, message: str = "tidak ditemukan", **details: Any) -> None:
        super().__init__(message, code="NOT_FOUND", **details)


class MethodNotAllowed(DashboardError):
    def __init__(self, message: str = "metode tidak diizinkan", **details: Any) -> None:
        super().__init__(message, code="METHOD_NOT_ALLOWED", **details)


class InvalidInput(DashboardError):
    def __init__(
        self, message: str = "input tidak valid", *, code: str = "INVALID_INPUT", **details: Any
    ) -> None:
        super().__init__(message, code=code, **details)


class MissingIdempotencyKey(InvalidInput):
    """A write without an idempotency key is a retry waiting to duplicate."""

    def __init__(
        self, message: str = "idempotency_key wajib untuk aksi tulis", **details: Any
    ) -> None:
        super().__init__(message, code="MISSING_IDEMPOTENCY_KEY", **details)


class TemplateError(DashboardError):
    """A template asked for a key nobody supplied: fail loud, not blank."""

    def __init__(self, message: str = "template gagal dirender", **details: Any) -> None:
        super().__init__(message, code="TEMPLATE_ERROR", **details)


class RemoteBindRefused(DashboardError):
    """Binding a non-loopback address is an explicit, deliberate act."""

    def __init__(self, message: str = "menolak bind ke alamat non-loopback", **details: Any) -> None:
        super().__init__(message, code="REMOTE_BIND_REFUSED", **details)


#: HTTP status per error class. A domain refusal from the approval kernel is a
#: 400 with its own code — never a 500, and never a silent success.
_STATUS_BY_ERROR: tuple[tuple[type, int], ...] = (
    (Unauthenticated, 401),
    (Forbidden, 403),
    (NotFound, 404),
    (MethodNotAllowed, 405),
    (MissingIdempotencyKey, 400),
    (InvalidInput, 400),
    (TemplateError, 500),
    (RemoteBindRefused, 500),
    (DashboardError, 400),
    (DomainError, 400),
)


def status_for(exc: BaseException) -> int:
    for klass, status in _STATUS_BY_ERROR:
        if isinstance(exc, klass):
            return status
    return 500


# --------------------------------------------------------------------------- #
# Escaping + the (tiny) template engine
# --------------------------------------------------------------------------- #
class Markup(str):
    """HTML this module composed from already-escaped pieces.

    Only the helpers below return it; a value coming from the database is never
    a :class:`Markup`, so it always gets escaped.
    """

    __slots__ = ()


def escape_value(value: Any) -> str:
    """Escape anything on its way into the page; :class:`Markup` passes through."""
    if isinstance(value, Markup):
        return str(value)
    return html.escape("" if value is None else str(value), quote=True)


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def _render_fragment(text: str, context: Mapping[str, Any], *, source: str) -> str:
    """Substitute ``{{ key }}``; every value is escaped unless it is Markup."""

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in context:
            raise TemplateError(
                "template meminta nilai yang tidak disediakan", template=source, key=key
            )
        return escape_value(context[key])

    return _PLACEHOLDER_RE.sub(replace, text)


def _template_path(name: str, templates_dir: Optional[Any] = None) -> Path:
    base = Path(templates_dir) if templates_dir is not None else DEFAULT_TEMPLATES_DIR
    file_name = name if name.endswith(".html") else f"{name}.html"
    if "/" in file_name or file_name.startswith("."):
        raise TemplateError("nama template harus satu berkas di templates/", template=name)
    return base / file_name


def render_page(
    name: str, context: Mapping[str, Any], *, templates_dir: Optional[Any] = None
) -> str:
    """Render one page: the named fragment wrapped in ``base.html``.

    ``name`` is a page key (``"finance"``), not a path: a page key maps to
    ``templates/<key>.html``, which holds the body fragment. Every dynamic value
    is escaped here — a template cannot opt out, and can only insert pre-escaped
    :class:`Markup`.
    """
    fragment_path = _template_path(name, templates_dir)
    if not fragment_path.exists():
        raise TemplateError("template tidak ditemukan", template=fragment_path.name)
    fragment = _render_fragment(
        fragment_path.read_text(encoding="utf-8"), context, source=fragment_path.name
    )
    base_path = _template_path("base", templates_dir)
    if not base_path.exists():
        raise TemplateError("template induk tidak ditemukan", template=base_path.name)
    merged = dict(context)
    merged["body"] = Markup(fragment)
    return _render_fragment(base_path.read_text(encoding="utf-8"), merged, source=base_path.name)


# --------------------------------------------------------------------------- #
# Small HTML builders (all of them escape their inputs)
# --------------------------------------------------------------------------- #
def _cell(value: Any) -> str:
    return escape_value(value)


def badge(text: Any, kind: str = "info") -> Markup:
    return Markup(
        f'<span class="badge badge-{escape_value(kind)}">{_cell(text)}</span>'
    )


def empty_note(what: str) -> Markup:
    """The explicit "this list is empty" marker every table falls back to."""
    return Markup(f'<p class="empty" data-empty="1">Tidak ada data: {_cell(what)}.</p>')


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    empty: str,
    attrs: str = "",
) -> Markup:
    """A table whose every cell is escaped; empty input renders ``empty_note``."""
    materialised = [list(row) for row in rows]
    if not materialised:
        return empty_note(empty)
    head = "".join(f"<th>{_cell(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(cell)}</td>" for cell in row) + "</tr>"
        for row in materialised
    )
    return Markup(
        f'<table {escape_value(attrs)}><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def definition_list(items: Iterable[tuple[str, Any]]) -> Markup:
    parts = [
        f'<div class="field"><dt>{_cell(label)}</dt><dd>{_cell(value)}</dd></div>'
        for label, value in items
    ]
    return Markup('<dl class="fields">' + "".join(parts) + "</dl>")


def kpi_row(
    key: str,
    label: str,
    value: Optional[int],
    *,
    available: bool = True,
    note: str = "",
    attrs: str = "",
) -> Markup:
    """One Command Center counter, parseable as ``data-kpi`` / ``data-value``.

    ``attrs`` is trusted attribute text built by this module (already escaped by
    the caller); every other value here is escaped.
    """
    show = available and value is not None
    raw = str(_as_int(value)) if show else ""
    rendered = raw if show else "—"
    hidden = "" if show else ' data-hidden="1"'
    tail = f'<td class="muted">{_cell(note)}</td>' if note else "<td></td>"
    return Markup(
        f'<tr data-kpi="{escape_value(key)}" data-value="{escape_value(raw)}"{hidden}'
        f'{attrs}><td>{_cell(label)}</td>'
        f'<td class="num">{_cell(rendered)}</td>{tail}</tr>'
    )


def nav_links(role: str) -> Markup:
    """Only the pages this role may open; a hidden page is not advertised.

    Pages are grouped into department sections (``NAV_GROUPS``) so the nav
    reads like the agency's org chart instead of a flat page list — purely
    presentational, same gating, same ``data-nav``/``data-nav-denied`` markers.
    """
    routes = dict(NAV)
    groups = []
    for label, pages in NAV_GROUPS:
        links = []
        for page in pages:
            route = routes[page]
            if role in PAGE_ROLES.get(page, ()):
                links.append(
                    f'<a href="{escape_value(route)}" data-nav="{escape_value(page)}">'
                    f"{_cell(PAGE_TITLES[page])}</a>"
                )
                continue
            links.append(
                f'<span class="badge" data-nav-denied="{escape_value(page)}" '
                f'title="peran ini tidak berhak">—</span>'
            )
        header = (
            f'<span class="navgroup-label">{_cell(label)}</span>' if label else ""
        )
        groups.append(f'<div class="navgroup">{header}{"".join(links)}</div>')
    return Markup('<nav class="nav">' + "".join(groups) + "</nav>")


# --------------------------------------------------------------------------- #
# Request plumbing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Principal:
    """One authenticated caller: identity, role, **and the agency it serves**.

    The agency is part of the principal because it is part of who the caller is,
    not something a request may assert. A token is only valid inside the agency it
    was issued for (``require_page`` enforces that); ``agency_default`` keeps a
    single-agency deployment working unchanged.
    """

    identity: str
    role: str
    agency_id: str = DEFAULT_AGENCY_ID

    def __post_init__(self) -> None:
        require_ref(self.identity, "identity")
        if self.role not in ROLES:
            raise InvalidInput("peran tidak dikenal", role=self.role, known=list(ROLES))
        require_ref(self.agency_id, "agency_id")

    @property
    def approval_role(self) -> Optional[str]:
        return APPROVAL_ROLE_BY_ROLE.get(self.role)

    @property
    def may_write(self) -> bool:
        return self.role in WRITE_ROLES

    @property
    def sees_pii(self) -> bool:
        return self.role in PII_ROLES


def _validate_role(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in ROLES:
        raise InvalidInput("peran tidak dikenal", role=repr(value), known=list(ROLES))
    return value.strip()


def _bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _token_registry(
    actor: str,
    actor_role: str,
    tokens: Optional[Mapping[str, Any]],
    agency_id: str = DEFAULT_AGENCY_ID,
) -> "dict[str, Principal]":
    """Build ``token → principal`` for **one** agency's dashboard.

    With no explicit ``tokens`` every role gets a freshly generated token, so a
    default deployment has no guessable credential; ``actor``/``actor_role``
    replace the demo identity of that role, so the operator is who they say.

    A spec that names an agency keeps it (and ``require_page`` then refuses it
    unless it is this dashboard's agency — minting a token for a *different*
    agency is a configuration mistake, and it must not quietly grant access). A
    spec that names none inherits this dashboard's agency; inheriting
    ``agency_default`` instead would lock every page of a scoped deployment.
    """
    if tokens is not None:
        registry: dict[str, Principal] = {}
        for token, spec in tokens.items():
            secret = require_text(token, "token", max_len=256)
            if isinstance(spec, str):
                role = _validate_role(spec)
                identity = DEFAULT_IDENTITIES[role]
            else:
                agency = agency_id
                try:
                    parts = tuple(spec)
                    if len(parts) == 2:
                        identity, role = parts
                    elif len(parts) == 3:
                        identity, role, agency = parts
                    else:
                        raise ValueError(len(parts))
                except (TypeError, ValueError) as exc:
                    raise InvalidInput(
                        "spec token harus (identitas, peran) atau nama peran",
                        token=secret[:8],
                    ) from exc
                role = _validate_role(role)
            try:
                registry[secret] = Principal(
                    identity=require_ref(identity, "identity"),
                    role=role,
                    agency_id=require_ref(agency, "agency_id"),
                )
            except DomainError as exc:
                # A malformed token specification is a deployment mistake: report it
                # as the dashboard's own input error, not as a domain refusal.
                raise InvalidInput(
                    "spec token tidak sah",
                    token=secret[:8],
                    detail=str(exc),
                ) from exc
        if not registry:
            raise InvalidInput("tokens tidak boleh kosong")
        return registry

    registry = {}
    for role in ROLES:
        identity = actor if role == actor_role else DEFAULT_IDENTITIES[role]
        registry[secrets.token_urlsafe(24)] = Principal(
            identity=require_ref(identity, "identity"), role=role, agency_id=agency_id
        )
    return registry


class _Request:
    """What a handler needs from a request, after authentication."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        form: Mapping[str, list[str]],
        principal: Optional[Principal],
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.form = form
        self.principal = principal

    def field(self, name: str, default: str = "") -> str:
        values = self.form.get(name)
        if not values:
            return default
        return values[0]

    def option(self, name: str, default: str = "") -> str:
        return self.field(name, default).strip()


class _Response:
    def __init__(self, status: int, body: str, *, page: str = "", detail: str = "") -> None:
        self.status = status
        self.body = body
        self.page = page
        self.detail = detail


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #
class Dashboard:
    """Read-only views + the approval queue. One writer per table, still."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any,
        actor: str,
        actor_role: str,
        gates: Any = None,
        tokens: Optional[Mapping[str, Any]] = None,
        ids: Optional[Any] = None,
        templates_dir: Optional[Any] = None,
        anchor_dir: Optional[Any] = None,
        page_roles: Optional[Mapping[str, Sequence[str]]] = None,
        approvals: Optional[ApprovalService] = None,
        enforce_append_only: bool = True,
        agency_id: str = "agency_default",
    ) -> None:
        if clock is None or not hasattr(clock, "now"):
            raise InvalidInput("clock wajib disuntik (Clock/FrozenClock)")
        self.conn = conn
        self.clock = clock
        self.actor = require_ref(actor, "actor")
        self.actor_role = _validate_role(actor_role)
        self.agency_id = require_ref(agency_id, "agency_id")
        self._ensure_agency_scope()
        self.gates = gates
        if gates is not None:
            missing = [name for name in GATE_INTERFACE if not hasattr(gates, name)]
            if missing:
                raise InvalidInput(
                    "objek gates tidak punya antarmuka klasifikasi yang dibutuhkan",
                    missing=missing,
                    expected=list(GATE_INTERFACE),
                )
        self.templates_dir = Path(templates_dir) if templates_dir is not None else None
        self.anchor_dir = Path(anchor_dir) if anchor_dir is not None else None
        self.page_roles = {name: tuple(roles) for name, roles in (page_roles or PAGE_ROLES).items()}
        self.tokens = _token_registry(self.actor, self.actor_role, tokens, self.agency_id)
        self.token_by_role = _invert_tokens(self.tokens)
        # The approval queue is the only writer this app ever talks to; building
        # it fails closed when the audit guards are missing.
        self.approvals = (
            approvals
            if approvals is not None
            else ApprovalService(
                conn, clock=clock, ids=ids if ids is not None else IdFactory(),
                enforce_append_only=enforce_append_only,
            )
        )
        self._handler_class: Optional[type] = None

    def _ensure_agency_scope(self) -> None:
        """Fail fast when the configured agency does not exist (read-only check).

        The dashboard is a read-only surface: creating the agency row is the
        job of the service that owns writes (``cases.CaseService`` and friends),
        never of this module.
        """
        found = _driver_rows(
            self.conn,
            "SELECT 1 FROM agencies WHERE agency_id = ? LIMIT 1",
            (self.agency_id,),
        )
        if not found:
            raise InvalidInput(
                "agency_id belum terdaftar di tabel agencies",
                agency_id=self.agency_id,
                hint="buka lewat service write (mis. CaseService) untuk mendaftarkan agency baru",
            )

    # -- plumbing ---------------------------------------------------------- #
    def handler_class(self) -> type:
        """A ready-to-serve handler class for ``http.server.HTTPServer``."""
        if self._handler_class is None:
            self._handler_class = type(
                "VillaDashboardHandler", (DashboardRequestHandler,), {"dashboard": self}
            )
        return self._handler_class

    def token_for_role(self, role: str) -> Optional[str]:
        return self.token_by_role.get(_validate_role(role))

    def authenticate(self, headers: Any) -> Principal:
        """Identity **and** role come from the token; nothing else is trusted."""
        supplied = headers.get("X-Dashboard-Token") or _bearer(headers.get("Authorization"))
        if not supplied:
            raise Unauthenticated("token dashboard tidak dikirim")
        candidate = str(supplied).strip()
        for token, principal in self.tokens.items():
            if secrets.compare_digest(token, candidate):
                return principal
        raise Unauthenticated("token dashboard tidak dikenal")

    def page_allowed(self, page: str, role: str) -> bool:
        return role in self.page_roles.get(page, ())

    def require_page(self, page: str, principal: Optional[Principal]) -> Principal:
        if principal is None:
            raise Unauthenticated("token dashboard tidak dikirim")
        if not self.page_allowed(page, principal.role):
            raise Forbidden(
                "peran ini tidak boleh membuka halaman itu",
                role=principal.role,
                page=page,
                allowed=list(self.page_roles.get(page, ())),
            )
        if principal.agency_id != self.agency_id:
            raise Forbidden(
                "token ini milik agency lain",
                principal_agency=principal.agency_id,
                scope=self.agency_id,
                page=page,
            )
        return principal

    # -- rows and counters ------------------------------------------------- #
    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        return _driver_rows(self.conn, sql, tuple(params))

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        rows = self._rows(sql, params)
        return _as_int(rows[0][0]) if rows else 0

    def schema_version(self) -> str:
        version = applied_version(self.conn)
        return version or "(belum diterapkan)"

    def database_is_empty(self) -> bool:
        for name in ("cases", "bookings", "invoices", "ops_tasks", "incidents"):
            if self._count(f"SELECT COUNT(*) FROM {name} WHERE agency_id = ?", (self.agency_id,)):
                return False
        return True

    def kpi_values(self) -> dict[str, Any]:
        """Every Command Center counter, straight from the database."""
        today = self.clock.today().isoformat()
        now = to_iso(self.clock.now())
        by_status = {
            str(row["status"]): _as_int(row["n"])
            for row in self._rows(
                "SELECT status, COUNT(*) AS n FROM cases WHERE agency_id = ? GROUP BY status",
                (self.agency_id,),
            )
        }
        return {
            "cases_by_status": by_status,
            "cases_total": sum(by_status.values()),
            "open_sla_breaches": self._count(
                "SELECT COUNT(*) FROM sla_breaches WHERE agency_id = ? AND status = ?",
                (self.agency_id, OPEN_SLA_STATUS),
            ),
            "pending_approvals": self._count(
                "SELECT COUNT(*) FROM approval_requests WHERE agency_id = ? AND status = ?",
                (self.agency_id, PENDING_APPROVAL_STATUS),
            ),
            "holds_expiring_today": self._count(
                "SELECT COUNT(*) FROM holds WHERE agency_id = ? AND status IN ({})"
                " AND substr(expires_at, 1, 10) = ?".format(
                    ", ".join("?" for _ in LIVE_HOLD_STATES)
                ),
                (self.agency_id, *LIVE_HOLD_STATES, today),
            ),
            "unpaid_invoices": self._count(
                "SELECT COUNT(*) FROM invoices WHERE agency_id = ? AND status = 'ISSUED' "
                "AND payment_status IN ('UNPAID','PARTIAL')",
                (self.agency_id,)
            ),
            "unmatched_payments": self._count(
                "SELECT COUNT(*) FROM payments p WHERE p.agency_id = ? AND p.status = 'SUCCEEDED' "
                "AND NOT EXISTS (SELECT 1 FROM payment_events e "
                "WHERE e.payment_id = p.payment_id AND e.applied = 1)",
                (self.agency_id,)
            ),
            "late_ops_tasks": self._count(
                "SELECT COUNT(*) FROM ops_tasks WHERE agency_id = ? AND status IN ('OPEN','IN_PROGRESS','BLOCKED') "
                "AND substr(due_at, 1, 19) < substr(?, 1, 19)",
                (self.agency_id, now),
            ),
            "open_incidents": self._count(
                "SELECT COUNT(*) FROM incidents WHERE agency_id = ? AND status IN ('OPEN','MITIGATING')",
                (self.agency_id,)
            ),
            "audit_chain": self.chain_health(),
            "today": today,
            "now": now,
        }

    # -- audit chain ------------------------------------------------------- #
    def _anchor_for_verification(self) -> Optional[ChainAnchor]:
        if self.anchor_dir is None:
            return None
        try:
            latest = anchors_module.latest_published(self.anchor_dir)
        except DomainError:
            return None
        if latest is None:
            return None
        return ChainAnchor(count=latest.count, head_hash=latest.head_hash)

    def chain_health(self) -> dict[str, Any]:
        """Verify the hash chain; a broken chain is a *displayed* fact, never a 500."""
        rows = self._rows("SELECT * FROM audit_events ORDER BY seq")
        anchor = self._anchor_for_verification()
        try:
            events = [AuditEvent.from_row(row) for row in rows]
            report = verify_chain(events, anchor=anchor)
        except DomainError as exc:
            return {
                "status": "BROKEN",
                "code": getattr(exc, "code", "AUDIT_CHAIN_ERROR"),
                "count": len(rows),
                "head_hash": "",
                "events": len(rows),
                "verified_against_anchor": anchor is not None,
            }
        except Exception as exc:  # noqa: BLE001 - a health view must never 500
            return {
                "status": "BROKEN",
                "code": type(exc).__name__,
                "count": len(rows),
                "head_hash": "",
                "events": len(rows),
                "verified_against_anchor": anchor is not None,
            }
        return {
            "status": "OK",
            "code": "CHAIN_OK",
            "count": report.count,
            "head_hash": report.head_hash,
            "events": len(rows),
            "verified_against_anchor": report.verified_against_anchor,
        }

    def last_anchor(self) -> dict[str, Any]:
        """The most recently published anchor, file first, chain record second."""
        if self.anchor_dir is not None:
            try:
                latest = anchors_module.latest_published(self.anchor_dir)
            except DomainError:
                latest = None
            if latest is not None:
                return {
                    "source": f"berkas {latest.file_name}",
                    "count": latest.count,
                    "head_hash": latest.head_hash,
                    "published_at": latest.published_at,
                    "found": True,
                }
        rows = self._rows(
            "SELECT * FROM audit_events WHERE action = ? ORDER BY seq DESC LIMIT 1",
            (anchors_module.ANCHOR_AUDIT_ACTION,),
        )
        if not rows:
            return {"source": "", "count": None, "head_hash": "", "published_at": "", "found": False}
        try:
            event = AuditEvent.from_row(rows[0])
            after = event.after or {}
        except Exception:  # noqa: BLE001 - a malformed row must not hide the page
            return {
                "source": "audit_events (baris rusak)",
                "count": None,
                "head_hash": "",
                "published_at": "",
                "found": False,
            }
        return {
            "source": f"audit_events #{event.seq}",
            "count": after.get("count"),
            "head_hash": str(after.get("head_hash") or event.hash),
            "published_at": event.occurred_at,
            "found": True,
        }

    def append_only_guards(self) -> tuple[str, ...]:
        names = {
            str(row[0])
            for row in self._rows("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        return tuple(sorted(name for name in names if "audit_events" in name))

    # -- outbound value filter --------------------------------------------- #
    def outbound(
        self,
        value: Any,
        *,
        principal: Principal,
        classification: str = "P2",
        field_name: str = "text",
    ) -> str:
        """One dynamic value on its way into a page, after the PII policy.

        The built-in policy decides; a configured classification gate can only
        make it stricter (a denied gate decision removes entitlement).

        Free text is declared **P2**, not P1, even when it looks like an internal
        note: these fields routinely embed guest contact details ("dihubungi via
        …"), and declaring them P1 would let a real identifier slip past a
        classification gate as "misclassified content".
        """
        text = "" if value is None else str(value)
        if not text:
            return text
        entitled = principal.sees_pii
        if self.gates is not None and entitled:
            decision = self.gates.gate_outbound(
                [
                    self.gates.ClassifiedField(
                        name=field_name, classification=classification, value=text
                    )
                ],
                destination=GATE_DESTINATION,
                actor_role=GATE_ROLE_BY_ROLE.get(principal.role, principal.role),
            )
            if decision.denied:
                entitled = False
        return text if entitled else redact_pii(text)

    # -- shared context ---------------------------------------------------- #
    def base_context(self, principal: Principal, page: str) -> dict[str, Any]:
        title = f"{PAGE_TITLES.get(page, page)} — Villa Agency OS"
        return {
            "title": title,
            "page": page,
            "actor": principal.identity,
            "actor_role": principal.role,
            "actor_role_label": ROLE_DISPLAY_LABEL.get(principal.role, principal.role),
            "now": to_iso(self.clock.now()),
            "schema_version": self.schema_version(),
            "nav": nav_links(principal.role),
            "read_only_banner": Markup(
                '<span class="tag" data-mode="read-only">mode baca-saja</span>'
            ),
        }

    def render(self, page: str, principal: Principal, context: Mapping[str, Any]) -> str:
        merged = self.base_context(principal, page)
        merged.update(context)
        return render_page(page, merged, templates_dir=self.templates_dir)

    # ------------------------------------------------------------------ #
    # Views (GET). None of them writes.
    # ------------------------------------------------------------------ #
    def view_command_center(self, principal: Principal) -> dict[str, Any]:
        kpis = self.kpi_values()
        chain = kpis["audit_chain"]
        finance_ok = principal.role in FINANCE_KPI_ROLES
        finance_note = "" if finance_ok else "tidak tersedia untuk peran ini"
        rows = [
            kpi_row(
                "cases_by_status",
                "Kasus (semua status)",
                kpis["cases_total"],
                note="rincian di bawah",
            ),
            kpi_row("open_sla_breaches", "SLA breach terbuka", kpis["open_sla_breaches"]),
            kpi_row("pending_approvals", "Approval pending", kpis["pending_approvals"]),
            kpi_row(
                "holds_expiring_today",
                "Hold kedaluwarsa hari ini",
                kpis["holds_expiring_today"],
                note=kpis["today"],
            ),
            kpi_row(
                "unpaid_invoices",
                "Invoice belum lunas",
                kpis["unpaid_invoices"],
                available=finance_ok,
                note=finance_note,
            ),
            kpi_row(
                "unmatched_payments",
                "Payment belum tercocokkan",
                kpis["unmatched_payments"],
                available=finance_ok,
                note=finance_note,
            ),
            kpi_row("late_ops_tasks", "Ops task terlambat", kpis["late_ops_tasks"]),
            kpi_row("open_incidents", "Insiden terbuka", kpis["open_incidents"]),
            kpi_row(
                "audit_chain",
                "Peristiwa rantai audit",
                chain["count"],
                note=chain["status"],
                attrs=f' data-chain="{escape_value(chain["status"])}"',
            ),
        ]
        status_rows = [
            (status, count)
            for status, count in sorted(kpis["cases_by_status"].items(), key=lambda kv: kv[0])
        ]
        sla_rows = [
            (
                row["breach_id"],
                row["subject_type"],
                row["subject_id"],
                row["kind"],
                row["due_at"],
                row["breach_minutes"],
            )
            for row in self._rows(
                "SELECT * FROM sla_breaches WHERE agency_id = ? AND status = ?"
                " ORDER BY due_at LIMIT 20",
                (self.agency_id, OPEN_SLA_STATUS),
            )
        ]
        events = self._recent_audit(5)
        return {
            "kpi_rows": Markup("".join(str(row) for row in rows)),
            "empty_banner": (
                Markup(
                    '<p class="empty banner" data-empty="1">Database kosong: belum ada '
                    "data operasional untuk ditampilkan.</p>"
                )
                if self.database_is_empty()
                else Markup("")
            ),
            "cases_status_table": table(
                ["Status", "Jumlah"],
                status_rows,
                empty="kasus per status",
            ),
            "sla_table": table(
                ["Breach", "Subjek", "ID", "Jenis", "Jatuh tempo", "Terlambat (menit)"],
                sla_rows,
                empty="SLA breach terbuka",
            ),
            "audit_table": self._audit_table(events),
            "kpi_note": Markup(
                '<p class="muted" data-note="kpi">Angka dihitung dari database pada '
                f"<code>{escape_value(kpis['now'])}</code>.</p>"
            ),
        }

    def _recent_audit(self, limit: int) -> list[Any]:
        rows = self._rows("SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,))
        events = []
        for row in rows:
            try:
                events.append(AuditEvent.from_row(row))
            except Exception:  # noqa: BLE001 - a broken row is shown as such
                events.append(None)
        return events

    def _audit_table(self, events: Sequence[Any], principal: Optional[Principal] = None) -> Markup:
        rows = []
        for event in events:
            if event is None:
                rows.append(["(baris rusak)", "", "", "", "", "", ""])
                continue
            reason = redact_pii(event.reason or "")
            rows.append(
                [
                    event.seq,
                    event.occurred_at,
                    event.actor,
                    event.actor_role,
                    event.action,
                    f'{event.entity_type}:{event.entity_id}',
                    reason,
                    str(event.hash)[:12],
                ]
            )
        return table(
            ["Seq", "Waktu", "Aktor", "Peran", "Aksi", "Entitas", "Alasan", "Hash"],
            rows,
            empty="peristiwa audit",
        )

    def view_cases(self, principal: Principal) -> dict[str, Any]:
        rows = self._rows(
            "SELECT c.*, (SELECT COUNT(*) FROM case_messages m WHERE m.case_id = c.case_id) AS msgs "
            "FROM cases c WHERE c.agency_id = ? ORDER BY c.created_at DESC LIMIT 200",
            (self.agency_id,),
        )
        table_rows = []
        for row in rows:
            due = str(row["first_response_due_at"])
            responded = row["first_responded_at"]
            state = "OK" if responded else ("LEWAT" if due < to_iso(self.clock.now()) else "MENUNGGU")
            table_rows.append(
                [
                    Markup(f'<a href="/cases/{escape_value(row["case_id"])}">{escape_value(row["case_id"])}</a>'),
                    row["status"],
                    row["priority"],
                    row["channel"],
                    row["guest_ref"],
                    row["owner_actor"] or "—",
                    due,
                    responded or "—",
                    f"{state} / {row['msgs']} pesan",
                ]
            )
        status_rows = [
            (str(row["status"]), _as_int(row["n"]))
            for row in self._rows(
                "SELECT status, COUNT(*) AS n FROM cases WHERE agency_id = ? GROUP BY status",
                (self.agency_id,),
            )
        ]
        return {
            "cases_table": table(
                [
                    "Kasus",
                    "Status",
                    "Prioritas",
                    "Kanal",
                    "Guest ref",
                    "Pemilik",
                    "Batas respons",
                    "Respons pertama",
                    "SLA / pesan",
                ],
                table_rows,
                empty="kasus",
            ),
            "status_table": table(["Status", "Jumlah"], status_rows, empty="kasus per status"),
        }

    def view_case_detail(self, case_id: str, principal: Principal) -> dict[str, Any]:
        clean = self._case_id_or_404(case_id)
        rows = self._rows("SELECT * FROM cases WHERE case_id = ? AND agency_id = ?", (clean, self.agency_id))
        if not rows:
            raise NotFound("kasus tidak ditemukan", case_id=clean)
        case = rows[0]
        guest_rows = (
            self._rows(
                "SELECT preferred_currency FROM guests WHERE guest_id = ? AND agency_id = ?",
                (case["guest_id"], self.agency_id),
            )
            if case["guest_id"]
            else []
        )
        guest_currency = guest_rows[0]["preferred_currency"] if guest_rows else None
        events = [
            (
                row["seq"],
                row["kind"],
                f"{row['from_status'] or '—'} → {row['to_status'] or '—'}",
                row["actor"],
                row["actor_role"],
                row["occurred_at"],
                self.outbound(
                    row["reason"],
                    principal=principal,
                    classification="P2",
                    field_name="case_event_reason",
                ),
            )
            for row in self._rows(
                "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq", (clean,)
            )
        ]
        messages = []
        for row in self._rows(
            "SELECT * FROM case_messages WHERE case_id = ? ORDER BY received_at", (clean,)
        ):
            body = row["body_redacted"]
            if body is None:
                body = f"(isi tidak disimpan; kelas {row['content_class']})"
            else:
                body = self.outbound(
                    body,
                    principal=principal,
                    classification=str(row["content_class"]),
                    field_name="case_message_body",
                )
            messages.append(
                (
                    row["received_at"],
                    row["direction"],
                    row["channel"],
                    row["sender_ref"],
                    row["content_class"],
                    "tidak dipercaya" if _as_int(row["untrusted"], 1) else "tepercaya",
                    body,
                )
            )
        breaches = [
            (
                row["breach_id"],
                row["kind"],
                row["due_at"],
                row["detected_at"],
                row["breach_minutes"],
                row["status"],
                self.outbound(
                    row["resolution_note"],
                    principal=principal,
                    classification="P2",
                    field_name="sla_resolution_note",
                ),
            )
            for row in self._rows(
                "SELECT * FROM sla_breaches WHERE case_id = ? AND agency_id = ?"
                " ORDER BY due_at",
                (clean, self.agency_id),
            )
        ]
        header = definition_list(
            [
                ("Kasus", case["case_id"]),
                ("Status", case["status"]),
                ("Prioritas", case["priority"]),
                ("Kanal", case["channel"]),
                ("Guest ref", case["guest_ref"]),
                ("Guest's preferred currency", guest_currency or "—"),
                ("Pemilik", case["owner_actor"] or "—"),
                ("Booking", case["booking_id"] or "—"),
                ("Kebijakan SLA", case["sla_policy"]),
                ("Batas respons", case["first_response_due_at"]),
                ("Respons pertama", case["first_responded_at"] or "—"),
                ("Batas quote", case["quote_due_at"]),
                ("Quote", case["quoted_at"] or "—"),
                ("Dibuat", case["created_at"]),
                ("Diperbarui", case["updated_at"]),
                ("Ditutup", case["closed_at"] or "—"),
                (
                    "Alasan tutup",
                    self.outbound(
                        case["close_reason"],
                        principal=principal,
                        classification="P2",
                        field_name="close_reason",
                    )
                    or "—",
                ),
            ]
        )
        return {
            "case_id": clean,
            "case_header": header,
            "timeline_table": table(
                ["Seq", "Jenis", "Transisi", "Aktor", "Peran", "Waktu", "Alasan"],
                events,
                empty="peristiwa kasus",
            ),
            "message_table": table(
                ["Waktu", "Arah", "Kanal", "Pengirim", "Kelas", "Kepercayaan", "Isi"],
                messages,
                empty="pesan kasus",
            ),
            "breach_table": table(
                ["Breach", "Jenis", "Jatuh tempo", "Terdeteksi", "Menit", "Status", "Catatan"],
                breaches,
                empty="SLA breach untuk kasus ini",
            ),
        }

    def _case_id_or_404(self, raw: str) -> str:
        try:
            return require_ref(unquote(raw or ""), "case_id")
        except DomainError as exc:
            raise NotFound("kasus tidak ditemukan", case_id=str(raw)[:64]) from exc

    def view_inventory(self, principal: Principal) -> dict[str, Any]:
        today = self.clock.today()
        days = [today + timedelta(days=offset) for offset in range(CALENDAR_DAYS)]
        units = self._rows(
            "SELECT u.*, p.name AS property_name FROM units u "
            "JOIN properties p ON p.property_id = u.property_id "
            "WHERE u.agency_id = ? AND p.agency_id = ? "
            "ORDER BY p.name, u.name",
            (self.agency_id, self.agency_id),
        )
        bookings = self._rows(
            "SELECT unit_id, check_in, check_out, status FROM bookings"
            " WHERE agency_id = ? AND unit_id IS NOT NULL",
            (self.agency_id,),
        )
        holds = self._rows(
            "SELECT unit_id, check_in, check_out, status, expires_at FROM holds"
            " WHERE agency_id = ?",
            (self.agency_id,),
        )

        booked_index: dict[str, list[Any]] = {}
        for row in bookings:
            if str(row["status"]) in BOOKED_BOOKING_STATUSES:
                booked_index.setdefault(str(row["unit_id"]), []).append(row)
        hold_index: dict[str, list[Any]] = {}
        for row in holds:
            # A hold marks its cells for as long as it blocks the slot — including the
            # ones pending verification (§7). Showing those as free would contradict
            # what the kernel does with the next request for the same dates.
            if str(row["status"]) in OCCUPYING_HOLD_STATES:
                hold_index.setdefault(str(row["unit_id"]), []).append(row)

        rows = []
        for unit in units:
            unit_id = str(unit["unit_id"])
            cells = []
            for day in days:
                stamp = day.isoformat()
                state = "UNKNOWN"
                for booking in booked_index.get(unit_id, []):
                    if str(booking["check_in"])[:10] <= stamp < str(booking["check_out"])[:10]:
                        state = "BOOKED"
                        break
                else:
                    for hold in hold_index.get(unit_id, []):
                        if str(hold["check_in"])[:10] <= stamp < str(hold["check_out"])[:10]:
                            state = "HOLD"
                            break
                mark = Markup(
                    f'<td data-unit="{escape_value(unit_id)}" data-day="{escape_value(stamp)}" '
                    f'data-state="{escape_value(state)}">{_cell(state)}</td>'
                )
                cells.append(mark)
            rows.append(
                [
                    unit_id,
                    unit["property_name"],
                    unit["name"],
                    unit["status"],
                    Markup("".join(str(cell) for cell in cells)),
                ]
            )
        return {
            "window": f"{days[0].isoformat()} … {days[-1].isoformat()}",
            "day_headers": Markup(
                "".join(f'<th data-day="{escape_value(day.isoformat())}">{escape_value(day.isoformat())}</th>'
                        for day in days)
            ),
            "inventory_rows": Markup("".join(self._inventory_row(row) for row in rows))
            if rows
            else empty_note("unit untuk kalender"),
            "legend": Markup(
                '<p class="muted" data-note="legend">BOOKED = booking aktif pada tanggal itu; '
                "HOLD = hold yang masih memblokir slot (termasuk yang menunggu verifikasi); "
                "UNKNOWN = tidak ada bukti "
                "ketersediaan (bukan berarti tersedia).</p>"
            ),
        }

    @staticmethod
    def _inventory_row(cells: Sequence[Any]) -> str:
        return (
            "<tr>"
            + "".join(f"<td>{_cell(cell)}</td>" for cell in cells[:-1])
            + f"{escape_value(cells[-1])}</tr>"
        )

    def view_finance(self, principal: Principal) -> dict[str, Any]:
        now = to_iso(self.clock.now())
        invoices = [
            (
                row["invoice_number"] or "—",
                row["invoice_id"],
                row["booking_id"],
                row["status"],
                row["payment_status"],
                format_money(row["total_minor"], row["currency"]),
                format_money(row["amount_paid_minor"], row["currency"]),
                row["issued_at"],
                row["due_at"],
                "LEWAT JATUH TEMPO"
                if str(row["status"]) == "ISSUED"
                and str(row["due_at"]) < now
                and str(row["payment_status"]) != "PAID"
                else "-",
            )
            for row in self._rows(
                "SELECT * FROM invoices WHERE agency_id = ? ORDER BY issued_at DESC LIMIT 200",
                (self.agency_id,),
            )
        ]
        payments = [
            (
                row["payment_id"],
                row["invoice_id"],
                row["provider"],
                row["method"],
                format_money(row["amount_minor"], row["currency"]),
                row["status"],
                row["provider_intent_id"],
                row["settled_at"] or "—",
                row["provider_status"] or "—",
            )
            for row in self._rows(
                "SELECT * FROM payments WHERE agency_id = ? ORDER BY created_at DESC LIMIT 200",
                (self.agency_id,),
            )
        ]
        recon = [
            (
                row["recon_id"],
                row["kind"],
                row["status"],
                row["payment_id"] or "—",
                row["invoice_id"] or "—",
                row["detected_at"],
                row["due_at"],
                self.outbound(
                    row["resolution_note"],
                    principal=principal,
                    classification="P2",
                    field_name="recon_resolution_note",
                ),
            )
            for row in self._rows(
                "SELECT * FROM reconciliation_cases WHERE agency_id = ?"
                " ORDER BY detected_at DESC LIMIT 200",
                (self.agency_id,),
            )
        ]
        kpis = self.kpi_values()
        # Satu baris per mata uang. Menjumlahkan lintas mata uang menghasilkan satu angka yang
        # tidak berarti apa pun (rupiah + dolar), dan tidak ada cara membaca ulang angka itu.
        outstanding = [
            (f"Outstanding invoices {row['currency']} (minor)", int(row["total"] or 0))
            for row in self._rows(
                "SELECT currency, COALESCE(SUM(total_minor), 0) AS total FROM invoices"
                " WHERE agency_id = ? AND status = 'ISSUED' GROUP BY currency ORDER BY currency",
                (self.agency_id,),
            )
        ]
        return {
            "finance_summary": definition_list(
                [
                    ("Invoice belum lunas", kpis["unpaid_invoices"]),
                    ("Payment belum tercocokkan", kpis["unmatched_payments"]),
                    ("Rekonsiliasi terbuka", self._count(
                        "SELECT COUNT(*) FROM reconciliation_cases"
                        " WHERE agency_id = ? AND status = 'OPEN'",
                        (self.agency_id,))),
                ] + outstanding
            ),
            "invoice_table": table(
                [
                    "Nomor",
                    "Invoice",
                    "Booking",
                    "Status",
                    "Pembayaran",
                    "Total",
                    "Terbayar",
                    "Terbit",
                    "Jatuh tempo",
                    "Catatan",
                ],
                invoices,
                empty="invoice",
            ),
            "payment_table": table(
                [
                    "Payment",
                    "Invoice",
                    "Provider",
                    "Metode",
                    "Jumlah",
                    "Status",
                    "Intent",
                    "Settled",
                    "Status provider",
                ],
                payments,
                empty="payment",
            ),
            "recon_table": table(
                ["Rekonsiliasi", "Jenis", "Status", "Payment", "Invoice", "Terdeteksi", "Jatuh tempo", "Catatan"],
                recon,
                empty="kasus rekonsiliasi",
            ),
        }

    def view_operations(self, principal: Principal) -> dict[str, Any]:
        now = to_iso(self.clock.now())
        tasks = [
            (
                row["task_id"],
                row["kind"],
                row["status"],
                row["priority"],
                row["unit_id"] or "—",
                row["booking_id"] or "—",
                row["case_id"] or "—",
                row["assignee"] or "—",
                row["due_at"],
                "TERLAMBAT" if str(row["due_at"]) < now and str(row["status"]) in ACTIVE_TASK_STATUSES else "-",
                self.outbound(
                    row["blocked_reason"],
                    principal=principal,
                    classification="P2",
                    field_name="blocked_reason",
                ),
            )
            for row in self._rows(
                "SELECT * FROM ops_tasks WHERE agency_id = ? ORDER BY due_at LIMIT 200",
                (self.agency_id,),
            )
        ]
        incidents = [
            (
                row["incident_id"],
                row["severity"],
                row["title"],
                row["status"],
                row["source"],
                row["playbook"],
                "ya" if _as_int(row["personal_data_involved"]) else "tidak",
                "ya" if _as_int(row["regulatory_notification_required"]) else "tidak",
                row["detected_at"],
                row["notified_at"] or "—",
                self.outbound(
                    row["summary"],
                    principal=principal,
                    classification="P2",
                    field_name="incident_summary",
                ),
            )
            for row in self._rows(
                "SELECT * FROM incidents WHERE agency_id = ? ORDER BY detected_at DESC LIMIT 200",
                (self.agency_id,),
            )
        ]
        return {
            "late_tasks": self._count(
                "SELECT COUNT(*) FROM ops_tasks WHERE agency_id = ? AND status IN ('OPEN','IN_PROGRESS','BLOCKED') "
                "AND substr(due_at, 1, 19) < substr(?, 1, 19)",
                (self.agency_id, now),
            ),
            "ops_table": table(
                [
                    "Tugas",
                    "Jenis",
                    "Status",
                    "Prioritas",
                    "Unit",
                    "Booking",
                    "Kasus",
                    "Penanggung jawab",
                    "Jatuh tempo",
                    "Keterlambatan",
                    "Alasan terblokir",
                ],
                tasks,
                empty="tugas operasional",
            ),
            "incident_table": table(
                [
                    "Insiden",
                    "Severity",
                    "Judul",
                    "Status",
                    "Sumber",
                    "Playbook",
                    "Data pribadi",
                    "Wajib lapor",
                    "Terdeteksi",
                    "Diberitahukan",
                    "Ringkasan",
                ],
                incidents,
                empty="insiden",
            ),
        }

    def view_approvals(self, principal: Principal) -> dict[str, Any]:
        rows = self._rows(
            "SELECT * FROM approval_requests WHERE agency_id = ? ORDER BY requested_at DESC LIMIT 200",
            (self.agency_id,),
        )
        # Tabel anak tidak membawa agency_id: scope lewat parent, bukan lewat keyakinan.
        decisions = self._rows(
            "SELECT * FROM approval_decisions WHERE approval_id IN ("
            "SELECT approval_id FROM approval_requests WHERE agency_id = ?)"
            " ORDER BY decided_at DESC LIMIT 200",
            (self.agency_id,),
        )
        decision_counts: dict[str, int] = {}
        for row in decisions:
            decision_counts[str(row["approval_id"])] = decision_counts.get(str(row["approval_id"]), 0) + 1
        table_rows = []
        for row in rows:
            approval_id = str(row["approval_id"])
            table_rows.append(
                [
                    approval_id,
                    row["subject_type"],
                    row["subject_id"],
                    row["action"],
                    row["status"],
                    row["risk_level"],
                    f"{row['required_role']} ×{row['required_approvals']}",
                    f"{row['requested_by']} ({row['requested_role']})",
                    row["requested_at"],
                    row["expires_at"],
                    decision_counts.get(approval_id, 0),
                    self.outbound(
                        row["reason"],
                        principal=principal,
                        classification="P2",
                        field_name="approval_reason",
                    ),
                ]
            )
        decision_rows = [
            (
                row["approval_id"],
                row["approver"],
                row["approver_role"],
                row["decision"],
                row["decided_at"],
                self.outbound(
                    row["note"],
                    principal=principal,
                    classification="P2",
                    field_name="decision_note",
                ),
            )
            for row in decisions
        ]
        return {
            "pending_count": self._count(
                "SELECT COUNT(*) FROM approval_requests WHERE agency_id = ? AND status = ?",
                (self.agency_id, PENDING_APPROVAL_STATUS),
            ),
            "banner": Markup(""),
            "may_write": principal.may_write,
            "write_note": Markup(
                '<p class="muted" data-note="write">Aksi tulis di dashboard hanya ada di sini, '
                "dan semuanya lewat <code>ApprovalService</code>. Setiap permintaan wajib "
                "membawa <code>idempotency_key</code>.</p>"
                if principal.may_write
                else '<p class="muted" data-note="write">Peran ini hanya boleh membaca: '
                "aksi approval ditolak (403).</p>"
            ),
            "create_form": self._approval_form(principal),
            "approval_table": table(
                [
                    "Approval",
                    "Subjek",
                    "ID subjek",
                    "Aksi",
                    "Status",
                    "Risiko",
                    "Butuh",
                    "Diminta oleh",
                    "Diminta",
                    "Kedaluwarsa",
                    "Keputusan",
                    "Alasan",
                ],
                table_rows,
                empty="permintaan approval",
            ),
            "decision_table": table(
                ["Approval", "Approver", "Peran", "Keputusan", "Waktu", "Catatan"],
                decision_rows,
                empty="keputusan approval",
            ),
        }

    def _approval_form(self, principal: Principal) -> Markup:
        if not principal.may_write:
            return Markup("")
        subject_options = "".join(
            f'<option value="{escape_value(name)}">{escape_value(name)}</option>'
            for name in SUBJECT_TYPES
        )
        role_options = "".join(
            f'<option value="{escape_value(name)}">{escape_value(name)}</option>'
            for name in sorted(ROLE_RANK)
        )
        return Markup(
            '<form method="post" action="/approvals" data-write-form="1" '
            'class="card">'
            "<h3>Ajukan approval</h3>"
            f'<label>subject_type <select name="subject_type">{subject_options}</select></label>'
            '<label>subject_id <input name="subject_id" required></label>'
            '<label>action <input name="action" required></label>'
            f'<label>risk_level <select name="risk_level">'
            '<option value="LOW">LOW</option><option value="MEDIUM" selected>MEDIUM</option>'
            '<option value="HIGH">HIGH</option></select></label>'
            f'<label>required_role <select name="required_role">{role_options}</select></label>'
            '<label>required_approvals <input name="required_approvals" value="1" inputmode="numeric"></label>'
            '<label>reason <textarea name="reason" required></textarea></label>'
            '<label>idempotency_key <input name="idempotency_key" required '
            'minlength="8"></label>'
            '<button type="submit">Ajukan</button>'
            "</form>"
        )

    def view_properties(self, principal: Principal) -> dict[str, Any]:
        properties = [
            (
                row["property_id"],
                row["name"],
                row["property_type"],
                row["region"],
                row["province"],
                row["timezone"],
                row["status"],
                row["contract_pack_status"],
                row["contract_pack_evaluated_at"] or "—",
                row["created_at"],
            )
            for row in self._rows("SELECT * FROM properties WHERE agency_id = ? ORDER BY name LIMIT 200", (self.agency_id,))
        ]
        units = [
            (
                row["unit_id"],
                row["property_id"],
                row["name"],
                row["unit_type"],
                row["bedrooms"],
                row["bathrooms"],
                row["base_capacity"],
                row["max_guests"],
                row["status"],
            )
            for row in self._rows("SELECT * FROM units WHERE agency_id = ? ORDER BY name LIMIT 500", (self.agency_id,))
        ]
        holds = [
            (
                row["hold_id"],
                row["unit_id"],
                row["check_in"],
                row["check_out"],
                row["status"],
                row["expires_at"],
                row["guest_ref"],
            )
            for row in self._rows("SELECT * FROM holds WHERE agency_id = ? ORDER BY created_at DESC LIMIT 200", (self.agency_id,))
        ]
        return {
            "property_table": table(
                [
                    "Properti",
                    "ID",
                    "Nama",
                    "Tipe",
                    "Region",
                    "Provinsi",
                    "TZ",
                    "Status",
                    "Contract pack",
                    "Evaluasi pack",
                    "Dibuat",
                ],
                properties,
                empty="properti",
            ),
            "unit_table": table(
                ["Unit", "Properti", "Nama", "Tipe", "Kamar", "KM", "Kapasitas", "Maks", "Status"],
                units,
                empty="unit",
            ),
            "hold_table": table(
                ["Hold", "Unit", "Check-in", "Check-out", "Status", "Kedaluwarsa", "Guest ref"],
                holds,
                empty="hold",
            ),
        }

    def view_marketing(self, principal: Principal) -> dict[str, Any]:
        """Public-site readiness without exposing guest PII."""
        now_iso = to_iso(self.clock.now())
        cutoff = to_iso(self.clock.now() - timedelta(days=MARKETING_WINDOW_DAYS))
        status_rows = self._rows(
            "SELECT status, COUNT(*) AS n FROM properties WHERE agency_id = ?"
            " GROUP BY status ORDER BY status",
            (self.agency_id,),
        )
        status_counts = {str(row["status"]): _as_int(row["n"]) for row in status_rows}
        recent_cases = self._rows(
            "SELECT COALESCE(reference, case_id) AS reference, status, created_at, first_response_due_at, quote_due_at"
            " FROM cases WHERE agency_id = ? AND channel = 'website' AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT ?",
            (self.agency_id, cutoff, QUEUE_LIMIT),
        )
        coverage = self._rows(
            "SELECT p.property_id AS property_id, p.name AS name, p.status AS status,"
            " COUNT(DISTINCT CASE WHEN u.status = 'ACTIVE' THEN u.unit_id END) AS active_units,"
            " COUNT(DISTINCT CASE WHEN ae.valid_until > ? THEN u.unit_id END) AS units_with_current_evidence,"
            " MAX(ae.captured_at) AS latest_captured_at, MAX(ae.valid_until) AS latest_valid_until"
            " FROM properties p"
            " LEFT JOIN units u ON u.agency_id = p.agency_id AND u.property_id = p.property_id"
            " LEFT JOIN availability_evidence ae ON ae.agency_id = u.agency_id AND ae.unit_id = u.unit_id"
            " WHERE p.agency_id = ? AND p.status IN ('SELLABLE', 'SELLABLE_WITH_REVIEW')"
            " GROUP BY p.property_id, p.name, p.status ORDER BY p.status, p.name LIMIT ?",
            (now_iso, self.agency_id, QUEUE_LIMIT),
        )
        return {
            "window_days": MARKETING_WINDOW_DAYS,
            "summary": definition_list(
                [
                    ("Sellable villas", status_counts.get("SELLABLE", 0)),
                    ("With review", status_counts.get("SELLABLE_WITH_REVIEW", 0)),
                    ("Not sellable", status_counts.get("NOT_SELLABLE", 0)),
                    ("Website inquiries", len(recent_cases)),
                ]
            ),
            "coverage_table": table(
                ["Villa", "Status", "Active units", "Current evidence", "Latest captured", "Valid until"],
                [
                    (
                        row["name"],
                        row["status"],
                        row["active_units"],
                        row["units_with_current_evidence"],
                        row["latest_captured_at"] or "—",
                        row["latest_valid_until"] or "—",
                    )
                    for row in coverage
                ],
                empty="marketing property",
            ),
            "website_case_table": table(
                ["Reference", "Status", "Created", "First response due", "Quote due"],
                [
                    (
                        row["reference"],
                        row["status"],
                        row["created_at"],
                        row["first_response_due_at"],
                        row["quote_due_at"],
                    )
                    for row in recent_cases
                ],
                empty="website inquiry",
            ),
            "note": Markup(
                "This page reads public-site readiness only. It does not publish a villa, "
                "send an inquiry, or change availability evidence."
            ),
        }

    def view_audit(self, principal: Principal) -> dict[str, Any]:
        chain = self.chain_health()
        anchor = self.last_anchor()
        events = self._recent_audit(50)
        guards = self.append_only_guards()
        return {
            "chain_status": chain["status"],
            "chain_code": chain["code"],
            "chain_count": chain["count"],
            "chain_head": chain["head_hash"],
            "chain_anchor_checked": chain["verified_against_anchor"],
            "chain_summary": definition_list(
                [
                    ("Status rantai", chain["status"]),
                    ("Kode", chain["code"]),
                    ("Jumlah peristiwa", chain["count"]),
                    ("Head hash", chain["head_hash"] or "—"),
                    (
                        "Diverifikasi terhadap anker",
                        "ya" if chain["verified_against_anchor"] else "tidak (tidak ada anker)",
                    ),
                    ("Anchor terakhir", anchor["source"] or "belum ada anker"),
                    ("Jumlah saat anker", anchor["count"] if anchor["count"] is not None else "—"),
                    ("Head saat anker", anchor["head_hash"] or "—"),
                    ("Anker diterbitkan", anchor["published_at"] or "—"),
                    ("Guard append-only", ", ".join(guards) or "TIDAK ADA (bahaya)"),
                    ("Versi skema", self.schema_version()),
                ]
            ),
            "event_table": self._audit_table(events),
            "breach_table": table(
                ["Breach", "Subjek", "Jenis", "Status", "Jatuh tempo", "Terdeteksi"],
                [
                    (
                        row["breach_id"],
                        f"{row['subject_type']}:{row['subject_id']}",
                        row["kind"],
                        row["status"],
                        row["due_at"],
                        row["detected_at"],
                    )
                    for row in self._rows(
                        "SELECT * FROM sla_breaches WHERE agency_id = ?"
                        " ORDER BY due_at LIMIT 100",
                        (self.agency_id,),
                    )
                ],
                empty="SLA breach",
            ),
        }

    # -- the two surfaces the wave left without a page ------------------ #
    def _empty_banner(self) -> Markup:
        """The explicit "this database is empty" banner, or nothing when it is not."""

        if not self.database_is_empty():
            return Markup("")
        return Markup(
            '<p class="empty banner" data-empty="1">Empty database: there is no '
            "operational data to show yet.</p>"
        )

    def view_channels(self, principal: Principal) -> dict[str, Any]:
        """What arrived on each channel, and what is staged to leave it.

        The dashboard never sends: ``channels.ChannelAdapter`` does, and only for a
        draft a human approved. So the number only this page can give is the queue —
        an approved draft no worker has collected yet is invisible everywhere else,
        and a queue nobody can see is a queue that grows.

        Every query is scoped through ``cases``: neither ``case_messages`` nor
        ``message_drafts`` carries an ``agency_id`` of its own, so the join is what
        keeps one tenant's traffic off another tenant's page.
        """
        cutoff = to_iso(self.clock.now() - timedelta(days=CHANNEL_WINDOW_DAYS))
        inbound = {
            str(row["channel"]): row
            for row in self._rows(
                "SELECT m.channel AS channel, COUNT(*) AS n, MAX(m.received_at) AS last_at"
                " FROM case_messages m JOIN cases c ON c.case_id = m.case_id"
                " WHERE c.agency_id = ? AND m.direction = 'INBOUND' AND m.received_at >= ?"
                " GROUP BY m.channel",
                (self.agency_id, cutoff),
            )
        }
        drafts = self._rows(
            "SELECT d.channel AS channel, d.status AS status, COUNT(*) AS n,"
            " MIN(d.created_at) AS oldest_at"
            " FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE c.agency_id = ? GROUP BY d.channel, d.status ORDER BY d.channel, d.status",
            (self.agency_id,),
        )
        queue = self._rows(
            "SELECT d.draft_id AS draft_id, d.case_id AS case_id, d.channel AS channel,"
            " d.template AS template, d.grounding AS grounding, d.approved_at AS approved_at"
            " FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE c.agency_id = ? AND d.status = 'APPROVED' AND d.sent_at IS NULL"
            " ORDER BY d.approved_at LIMIT ?",
            (self.agency_id, QUEUE_LIMIT),
        )
        grounding = self._rows(
            "SELECT d.grounding AS grounding, COUNT(*) AS n"
            " FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE c.agency_id = ? GROUP BY d.grounding ORDER BY d.grounding",
            (self.agency_id,),
        )
        sent = self._rows(
            "SELECT d.channel AS channel, COUNT(*) AS n, MAX(d.sent_at) AS last_at"
            " FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE c.agency_id = ? AND d.status = 'SENT' AND d.external_ref IS NOT NULL"
            " GROUP BY d.channel",
            (self.agency_id,),
        )
        now = self.clock.now()
        waiting = sum(_as_int(row["n"]) for row in drafts if row["status"] == "PENDING_APPROVAL")
        ready = sum(
            _as_int(row["n"]) for row in drafts if row["status"] in UNSENT_DRAFT_STATUSES
        )
        return {
            "window_days": CHANNEL_WINDOW_DAYS,
            "empty_banner": self._empty_banner(),
            "inbound_table": table(
                ["Channel", f"Inbound ({CHANNEL_WINDOW_DAYS} days)", "Last inbound"],
                [
                    (
                        CHANNEL_LABELS.get(channel, channel),
                        _as_int(inbound[channel]["n"]) if channel in inbound else 0,
                        str(inbound[channel]["last_at"]) if channel in inbound else "—",
                    )
                    for channel in CHANNEL_LABELS
                ],
                empty="inbound message",
            ),
            "drafts_table": table(
                ["Channel", "Status", "Drafts", "Oldest"],
                [
                    (
                        CHANNEL_LABELS.get(str(row["channel"]), row["channel"]),
                        row["status"],
                        _as_int(row["n"]),
                        row["oldest_at"],
                    )
                    for row in drafts
                ],
                empty="message draft",
            ),
            "queue_table": table(
                ["Draft", "Case", "Channel", "Template", "Grounding", "Approved", "Waiting (min)"],
                [
                    (
                        row["draft_id"],
                        row["case_id"],
                        CHANNEL_LABELS.get(str(row["channel"]), row["channel"]),
                        row["template"],
                        row["grounding"],
                        row["approved_at"],
                        _age_minutes(row["approved_at"], now) if row["approved_at"] else "—",
                    )
                    for row in queue
                ],
                empty="approved draft waiting to be sent",
            ),
            "grounding_table": table(
                ["Grounding", "Drafts"],
                [(row["grounding"], _as_int(row["n"])) for row in grounding],
                empty="message draft",
            ),
            "sent_table": table(
                ["Channel", "Sent", "Last sent"],
                [
                    (
                        CHANNEL_LABELS.get(str(row["channel"]), row["channel"]),
                        _as_int(row["n"]),
                        row["last_at"],
                    )
                    for row in sent
                ],
                empty="sent message",
            ),
            "queue_note": Markup(
                f"Waiting for a human approval: <strong>{waiting}</strong> · "
                f"approved and not sent yet: <strong>{ready}</strong>. "
                "No channel worker exists yet, so nothing leaves on its own: sending "
                "goes through <code>channels.ChannelAdapter</code>, for an approved "
                "draft, by a person."
            ),
        }

    def view_shadow_eval(self, principal: Principal) -> dict[str, Any]:
        """The shadow-mode measurement (ADR-0038): extractor proposals vs human decisions.

        This page is a reader, never a second evaluator: it renders
        :func:`shadow_eval.collect` and nothing else. When the bars cannot be resolved
        for the agency the module refuses, and the refusal is what the page shows — a
        rate without its bar is not a result, and printing the platform baseline in
        its place would be inventing an answer.
        """
        code = ""
        measurements: list[Any] = []
        bars: Mapping[str, Any] = {}
        ungrounded: Any = "not measured"
        try:
            report = shadow_eval_module.collect(
                self.conn,
                agency_id=self.agency_id,
                window_days=SHADOW_WINDOW_DAYS,
                clock=self.clock,
            )
        except DomainError as exc:
            code = str(getattr(exc, "code", "SHADOW_EVAL_ERROR"))
        else:
            measurements = sorted(report.extractors.values(), key=lambda item: item.extractor)
            bars = dict(report.bars)
            ungrounded = report.ungrounded_reached_guest
        measured = not code
        min_cases = _as_int(bars.get("min_cases"))
        if not measured:
            status_note = Markup(
                '<p class="empty" data-shadow="unavailable" data-code="'
                + escape_value(code)
                + '">The bars are unreadable for this agency (<code>'
                + escape_value(code)
                + "</code>), so this page shows no rate at all: a measurement without "
                "its bar is not a result. Publish the shadow bars for this tenant, "
                "then reload.</p>"
            )
        elif ungrounded:
            status_note = Markup(
                '<p class="empty banner" data-empty="1" data-shadow="violation">'
                f"{_as_int(ungrounded)} draft(s) reached a guest without being GROUNDED. "
                "The schema is supposed to make that impossible; it is measured here "
                "rather than assumed.</p>"
            )
        else:
            status_note = Markup(
                f'<p class="ok" data-shadow="measured" data-extractors="{len(measurements)}">'
                f"Measured over the last <strong>{SHADOW_WINDOW_DAYS}</strong> days: "
                f"<strong>{len(measurements)}</strong> shadow extractor(s) in the window, "
                "and no draft reached a guest without grounding. The module graduates "
                "nothing and writes nothing.</p>"
            )
        return {
            "window_days": SHADOW_WINDOW_DAYS,
            "empty_banner": self._empty_banner(),
            "status_note": status_note,
            "bars_table": table(
                ["Bar", "Effective for this agency", "Platform baseline"],
                [
                    (
                        "Minimum agreement rate",
                        _rate_percent(bars.get("agreement_rate")) if measured else "not published",
                        f"{shadow_eval_module.BASELINE_AGREEMENT_PCT}%",
                    ),
                    (
                        "Minimum decided fields",
                        _as_int(bars.get("min_cases")) if measured else "not published",
                        shadow_eval_module.BASELINE_MIN_CASES,
                    ),
                ],
                empty="policy bar",
            ),
            "extractor_table": table(
                [
                    "Extractor",
                    "Cases",
                    "Decided",
                    "Agreed",
                    "Disagreed",
                    "Pending",
                    "Unsupported",
                    "Agreement",
                    "Verdict",
                ],
                [
                    (
                        item.extractor,
                        item.cases,
                        item.decided,
                        item.agreed,
                        item.disagreed,
                        item.pending,
                        item.unsupported,
                        _rate_percent(item.agreement_rate),
                        _shadow_verdict(item, min_cases),
                    )
                    for item in measurements
                ],
                empty="shadow extractor measurement",
            ),
            "invariant_list": definition_list(
                [
                    ("Window (days)", SHADOW_WINDOW_DAYS),
                    ("Ungrounded drafts that reached a guest (must stay 0)", ungrounded),
                    ("Bars readable for this agency", "yes" if measured else "no"),
                    ("Extractors in the window", len(measurements)),
                ]
            ),
        }

    def view_outbox(self, principal: Principal) -> dict[str, Any]:
        """The integration outbox and inbox, read-only.

        The outbox is the contract: a kernel service writes an event in the same
        transaction as its state change, a worker publishes it, and this page
        shows the queue nobody else renders — pending letters, dead letters with
        their last error, and inbox items nobody processed yet. An event stuck
        here with no worker is integration debt made visible, which is the only
        honest way to run integrations before a worker exists.
        """
        now = self.clock.now()
        cutoff = to_iso(now - timedelta(days=OUTBOX_WINDOW_DAYS))
        status_rows = self._rows(
            "SELECT status, COUNT(*) AS n, MAX(created_at) AS newest_at"
            " FROM outbox_events WHERE agency_id = ? GROUP BY status ORDER BY status",
            (self.agency_id,),
        )
        by_status = {str(row["status"]): row for row in status_rows}
        pending = self._rows(
            "SELECT outbox_event_id, event_type, aggregate_type, aggregate_id,"
            " attempts, max_attempts, next_attempt_at, created_at"
            " FROM outbox_events WHERE agency_id = ? AND status = 'PENDING'"
            " ORDER BY created_at LIMIT 20",
            (self.agency_id,),
        )
        dead = self._rows(
            "SELECT outbox_event_id, event_type, attempts, max_attempts,"
            " last_error, created_at"
            " FROM outbox_events WHERE agency_id = ? AND status = 'DEAD_LETTER'"
            " ORDER BY created_at LIMIT 20",
            (self.agency_id,),
        )
        inbox_rows = self._rows(
            "SELECT source, COUNT(*) AS n, MAX(received_at) AS last_at, "
            "SUM(CASE WHEN processed_at IS NULL THEN 1 ELSE 0 END) AS unprocessed"
            " FROM integration_inbox WHERE agency_id = ? GROUP BY source ORDER BY source",
            (self.agency_id,),
        )
        recent = self._rows(
            "SELECT outbox_event_id, event_type, status, published_at, created_at"
            " FROM outbox_events WHERE agency_id = ? AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT 20",
            (self.agency_id, cutoff),
        )
        waiting = sum(
            _as_int(by_status[state]["n"]) for state in OUTBOX_LIVE_STATES if state in by_status
        )
        unprocessed = sum(_as_int(row["unprocessed"]) for row in inbox_rows)
        return {
            "window_days": OUTBOX_WINDOW_DAYS,
            "empty_banner": self._empty_banner(),
            "status_table": table(
                ["Status", "Events", "Newest"],
                [
                    (row["status"], _as_int(row["n"]), row["newest_at"] or "—")
                    for row in status_rows
                ],
                empty="outbox event",
            ),
            "pending_table": table(
                ["Event", "Type", "Aggregate", "Attempts", "Next attempt", "Created"],
                [
                    (
                        row["outbox_event_id"],
                        row["event_type"],
                        f"{row['aggregate_type']}:{row['aggregate_id']}",
                        f"{_as_int(row['attempts'])}/{_as_int(row['max_attempts'])}",
                        row["next_attempt_at"],
                        row["created_at"],
                    )
                    for row in pending
                ],
                empty="pending outbox event",
            ),
            "dead_table": table(
                ["Event", "Type", "Attempts", "Last error", "Created"],
                [
                    (
                        row["outbox_event_id"],
                        row["event_type"],
                        f"{_as_int(row['attempts'])}/{_as_int(row['max_attempts'])}",
                        row["last_error"] or "—",
                        row["created_at"],
                    )
                    for row in dead
                ],
                empty="dead-lettered event",
            ),
            "inbox_table": table(
                ["Source", "Received", "Unprocessed", "Last received"],
                [
                    (
                        row["source"],
                        _as_int(row["n"]),
                        _as_int(row["unprocessed"]),
                        row["last_at"] or "—",
                    )
                    for row in inbox_rows
                ],
                empty="integration inbox source",
            ),
            "recent_table": table(
                ["Event", "Type", "Status", "Published", "Created"],
                [
                    (
                        row["outbox_event_id"],
                        row["event_type"],
                        row["status"],
                        row["published_at"] or "—",
                        row["created_at"],
                    )
                    for row in recent
                ],
                empty="outbox event in the window",
            ),
            "queue_note": Markup(
                f"Not yet delivered (PENDING + FAILED): <strong>{waiting}</strong> · "
                f"inbox items nobody processed: <strong>{unprocessed}</strong>. "
                "This page sends nothing: a worker publishes due events through "
                "<code>OutboxService.publish_due</code>, and a person re-queues a "
                "dead letter with a reason. No worker is wired yet, so letters "
                "wait here in the open."
            ),
        }

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def _resolve_get(self, segments: Sequence[str]) -> tuple[str, Optional[str]]:
        if not segments:
            return "command_center", None
        head = segments[0]
        if head == "cases":
            if len(segments) == 1:
                return "cases", None
            # The case id is the *last* segment, so /cases/<id>/anything still
            # names the case; an id that is not a reference token is a 404.
            return "case_detail", segments[-1]
        for page in ("inventory", "finance", "operations", "approvals", "properties", "marketing", "channels",
                     "shadow_eval", "outbox", "audit"):
            if head == page and len(segments) == 1:
                return page, None
        raise NotFound("rute tidak dikenal", path="/" + "/".join(segments))

    def dispatch(self, request: _Request) -> _Response:
        """Authenticate, authorise, then serve. Nothing here writes except approval."""
        principal = request.principal
        if principal is None:
            raise Unauthenticated("token dashboard tidak dikirim")
        segments = [segment for segment in urlsplit(request.path).path.split("/") if segment]
        if request.method == "GET":
            page, argument = self._resolve_get(segments)
            self.require_page(page, principal)
            if page == "case_detail":
                assert argument is not None
                context = self.view_case_detail(argument, principal)
            else:
                context = getattr(self, f"view_{page}")(principal)
            return _Response(200, self.render(page, principal, context), page=page)

        if request.method != "POST":
            raise MethodNotAllowed("metode tidak didukung", method=request.method)

        if segments == ["approvals"]:
            self.require_page("approvals", principal)
            return self.post_approval_request(request, principal)
        if len(segments) == 3 and segments[0] == "approvals" and segments[2] == "decide":
            self.require_page("approvals", principal)
            return self.post_approval_decide(request, principal, segments[1])
        raise MethodNotAllowed(
            "POST hanya tersedia untuk aksi approval",
            path="/" + "/".join(segments),
            allowed=["POST /approvals", "POST /approvals/<id>/decide"],
        )

    # ------------------------------------------------------------------ #
    # The only write path: the approval queue
    # ------------------------------------------------------------------ #
    def _require_idempotency(self, request: _Request) -> str:
        key = request.option("idempotency_key")
        if not key:
            raise MissingIdempotencyKey("idempotency_key wajib untuk aksi tulis", field="idempotency_key")
        if len(key) < MIN_IDEMPOTENCY_KEY_LENGTH:
            raise MissingIdempotencyKey(
                "idempotency_key terlalu pendek, aksi tidak ditulis",
                field="idempotency_key",
                minimum=MIN_IDEMPOTENCY_KEY_LENGTH,
            )
        return key

    def _require_writer(self, principal: Principal) -> str:
        if not principal.may_write:
            raise Forbidden("peran ini hanya boleh membaca", role=principal.role)
        role = principal.approval_role
        if role is None:  # pragma: no cover - WRITE_ROLES all map today
            raise Forbidden("tidak ada peran approval untuk peran dashboard ini", role=principal.role)
        return role

    def post_approval_request(self, request: _Request, principal: Principal) -> _Response:
        approval_role = self._require_writer(principal)
        key = self._require_idempotency(request)
        action = request.option("action")
        reason = request.field("reason").strip()
        if not action or not reason:
            raise InvalidInput("action dan reason wajib diisi", missing=["action", "reason"])
        money = None
        money_minor = request.option("money_minor")
        if money_minor:
            currency = request.option("currency").upper()
            if len(currency) != 3:
                raise InvalidInput("currency wajib 3 huruf bila money_minor diisi", currency=currency)
            try:
                money = Money(int(money_minor), currency)
            except (TypeError, ValueError) as exc:
                raise InvalidInput("money_minor harus integer", money_minor=money_minor) from exc
        payload = None
        raw_payload = request.option("payload_json")
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError) as exc:
                raise InvalidInput("payload_json bukan JSON yang valid") from exc
            if not isinstance(payload, dict):
                raise InvalidInput("payload_json harus objek JSON")
        required_approvals = _as_int(request.option("required_approvals") or 1, 1)
        approval = self.approvals.request(
            subject_type=request.option("subject_type"),
            subject_id=request.option("subject_id"),
            action=action,
            requested_by=principal.identity,
            requested_role=approval_role,
            reason=reason,
            payload=payload,
            required_role=request.option("required_role") or "owner",
            required_approvals=required_approvals,
            risk_level=request.option("risk_level") or "MEDIUM",
            money=money,
            idempotency_key=key,
        )
        context = self.view_approvals(principal)
        context["banner"] = Markup(
            f'<p class="ok" data-banner="approval-requested" data-approval-id="'
            f'{escape_value(approval.approval_id)}" data-status="{escape_value(approval.status)}">'
            f"Permintaan approval {escape_value(approval.approval_id)} tercatat "
            f"({escape_value(approval.status)}).</p>"
        )
        return _Response(200, self.render("approvals", principal, context), page="approvals")

    def post_approval_decide(
        self, request: _Request, principal: Principal, approval_id: str
    ) -> _Response:
        approval_role = self._require_writer(principal)
        key = self._require_idempotency(request)
        decision = request.option("decision").upper()
        if decision not in ("APPROVE", "REJECT"):
            raise InvalidInput("decision harus APPROVE atau REJECT", decision=decision, idempotency_key=key)
        clean = require_ref(unquote(approval_id), "approval_id")
        decision_row = self.approvals.decide(
            clean,
            approver=principal.identity,
            approver_role=approval_role,
            decision=decision,
            note=request.option("note") or None,
        )
        context = self.view_approvals(principal)
        context["banner"] = Markup(
            f'<p class="ok" data-banner="approval-decided" data-approval-id="'
            f'{escape_value(clean)}" data-decision="{escape_value(decision)}" '
            f'data-status="{escape_value(decision_row.status)}" '
            f'data-idempotency-key="{escape_value(key)}">'
            f"Keputusan {escape_value(decision)} untuk {escape_value(clean)} tercatat.</p>"
        )
        return _Response(200, self.render("approvals", principal, context), page="approvals")

    # ------------------------------------------------------------------ #
    # Error pages / dump
    # ------------------------------------------------------------------ #
    def error_response(self, exc: BaseException) -> _Response:
        status = status_for(exc)
        code = getattr(exc, "code", None) or ("INTERNAL_ERROR" if status == 500 else "ERROR")
        message = str(exc) if isinstance(exc, DomainError) else "galat internal"
        principal = None
        if isinstance(exc, DomainError):
            details = getattr(exc, "details", {}) or {}
            role = details.get("role")
            if isinstance(role, str) and role in ROLES:
                principal = Principal(
                    DEFAULT_IDENTITIES[role], role, agency_id=self.agency_id
                )
        context = self.base_context(
            principal or Principal(self.actor, self.actor_role, agency_id=self.agency_id),
            "error",
        )
        context.update(
            {
                "status": status,
                "error_code": code,
                "error_message": redact_pii(message),
                "error_details": json.dumps(
                    {k: v for k, v in (getattr(exc, "details", {}) or {}).items()},
                    default=str,
                    sort_keys=True,
                ),
            }
        )
        body = render_page("error", context, templates_dir=self.templates_dir)
        return _Response(status, body, page="error", detail=str(code))

    def dump(self, directory: Any) -> list[Path]:
        """Write every page this role may open as a static file, for a demo."""
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        principal = Principal(self.actor, self.actor_role, agency_id=self.agency_id)
        written: list[Path] = []
        for page, route in NAV:
            if not self.page_allowed(page, principal.role):
                continue
            response = self.dispatch(
                _Request(method="GET", path=route, query={}, form={}, principal=principal)
            )
            name = _DUMP_NAMES.get(page, page)
            path = base / f"{name}.html"
            path.write_text(response.body, encoding="utf-8")
            written.append(path)
        if self.page_allowed("case_detail", principal.role):
            for row in self._rows(
                "SELECT case_id FROM cases WHERE agency_id = ? ORDER BY created_at",
                (self.agency_id,),
            ):
                case_id = str(row["case_id"])
                response = self.dispatch(
                    _Request(
                        method="GET",
                        path=f"/cases/{case_id}",
                        query={},
                        form={},
                        principal=principal,
                    )
                )
                path = base / f"case-{case_id}.html"
                path.write_text(response.body, encoding="utf-8")
                written.append(path)
        return written


def _invert_tokens(registry: Mapping[str, Principal]) -> dict[str, str]:
    by_role: dict[str, str] = {}
    for token, principal in registry.items():
        by_role.setdefault(principal.role, token)
    return by_role


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class DashboardRequestHandler(BaseHTTPRequestHandler):
    """One request → one rendered page. Never writes except through the queue."""

    protocol_version = "HTTP/1.1"
    server_version = "VillaAgencyDashboard/1.0"
    sys_version = ""
    #: Bounded read: a half-sent request dies instead of holding the single
    #: serving thread (see ``CONNECTION_TIMEOUT_SECONDS``).
    timeout = CONNECTION_TIMEOUT_SECONDS
    dashboard: Dashboard

    # -- HTTP verbs -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._serve("POST")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self._refuse("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._refuse("PATCH")

    def _refuse(self, method: str) -> None:
        exc = MethodNotAllowed("metode tidak didukung", method=method)
        self._write(self.dashboard.error_response(exc))

    def _serve(self, method: str) -> None:
        try:
            request = self._build_request(method)
            response = self.dashboard.dispatch(request)
        except DomainError as exc:
            response = self.dashboard.error_response(exc)
        except Exception as exc:  # noqa: BLE001 - last line of defence, no traceback leak
            response = self.dashboard.error_response(
                InvalidInput("galat internal", code="INTERNAL_ERROR", kind=type(exc).__name__)
            )
        self._write(response)

    def _build_request(self, method: str) -> _Request:
        parsed = urlsplit(self.path)
        principal = self.dashboard.authenticate(self.headers)
        form: Mapping[str, list[str]] = {}
        if method == "POST":
            form = self._read_form()
        return _Request(
            method=method,
            path=parsed.path or "/",
            query=parse_qs(parsed.query, keep_blank_values=True),
            form=form,
            principal=principal,
        )

    def _read_form(self) -> Mapping[str, list[str]]:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/x-www-form-urlencoded":
            raise InvalidInput(
                "hanya application/x-www-form-urlencoded yang didukung",
                code="UNSUPPORTED_MEDIA_TYPE",
                content_type=content_type,
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise InvalidInput("Content-Length wajib", code="LENGTH_REQUIRED")
        length = _as_int(raw_length, -1)
        if length < 0:
            raise InvalidInput("Content-Length tidak valid", code="INVALID_LENGTH")
        if length > MAX_FORM_BYTES:
            raise InvalidInput(
                "body terlalu besar", code="FORM_TOO_LARGE", limit=MAX_FORM_BYTES, length=length
            )
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        return parse_qs(body, keep_blank_values=True)

    def _write(self, response: _Response) -> None:
        payload = response.body.encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Framing protection is not implied by ``default-src 'none'``: frames are
        # governed by ``frame-ancestors`` alone, so a page without it can still be
        # clickjacked inside an attacker's iframe.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        if response.status == 405:
            self.send_header("Allow", "GET, POST")
        if response.status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="villa-dashboard"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client gone
            pass

    # -- logging: never the query string, never unredacted ----------------- #
    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = urlsplit(self.path).path
        emit_dashboard_log(f"dashboard {self.command} {path} -> {code}")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib naming
        message = format % args if args else format
        sys.stderr.write(redact_pii(f"dashboard {message}\n"))


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def create_app(
    conn: Any,
    *,
    clock: Any,
    actor: str,
    actor_role: str,
    gates: Any = None,
    **options: Any,
) -> type:
    """Return a handler class ready for ``http.server.HTTPServer``.

    ``gates`` is the optional classification gate (``src/gates.py``); when an
    object is supplied it must expose ``gate_outbound`` and ``ClassifiedField``,
    and it can only make the PII policy stricter.
    """
    app = Dashboard(
        conn, clock=clock, actor=actor, actor_role=actor_role, gates=gates, **options
    )
    return app.handler_class()


def resolve_bind(host: Optional[str], *, allow_remote: bool = False) -> str:
    """Loopback unless the caller *explicitly* asked to expose the dashboard."""
    candidate = (host or "").strip()
    if not candidate:
        raise RemoteBindRefused("host wajib diisi eksplisit", host=host)
    if candidate in WILDCARD_HOSTS and not allow_remote:
        raise RemoteBindRefused(
            "dashboard menolak bind ke alamat non-loopback tanpa izin eksplisit",
            host=candidate,
            hint="pakai --allow-remote bila memang disengaja",
        )
    return candidate


class ListeningHTTPServer(HTTPServer):
    """``HTTPServer`` whose listen queue can absorb a burst of connections.

    Only the accept queue is widened. Requests are still served one at a time,
    which keeps the ``sqlite3`` connection on the thread that owns it.
    """

    request_queue_size = REQUEST_QUEUE_SIZE


def serve(
    handler_class: type,
    host: str = DEFAULT_HOST,
    port: int = 8080,
    *,
    allow_remote: bool = False,
) -> HTTPServer:
    """Bind loopback (unless allowed) and return a server; the caller serves it.

    Deliberately **single-threaded**: a ``sqlite3`` connection is bound to the
    thread that created it, and a thread-per-request server would sample it from
    threads that do not own it. One internal operator does not need concurrency,
    and serialising requests also means ``--db`` sees one writer-order.

    The listen queue is widened (``REQUEST_QUEUE_SIZE``) so that a burst of
    callers waits in the kernel instead of being dropped and retried.
    """
    bind_host = resolve_bind(host, allow_remote=allow_remote)
    return ListeningHTTPServer((bind_host, int(port)), handler_class)


def dump_all(
    directory: Any,
    *,
    conn: Any,
    clock: Any,
    actor: str,
    actor_role: str,
    gates: Any = None,
    **options: Any,
) -> list[Path]:
    """Render every page of a role into ``directory`` as static HTML."""
    app = Dashboard(
        conn, clock=clock, actor=actor, actor_role=actor_role, gates=gates, **options
    )
    return app.dump(directory)


DEFAULT_DB = "data/villa.db"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="Dashboard internal Villa Agency OS (baca-saja + antrean approval).",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="berkas SQLite (default: %(default)s)")
    parser.add_argument(
        "--pg-dsn",
        default=None,
        metavar="DSN",
        help=(
            "connect to PostgreSQL instead of SQLite (production dialect; credentials "
            "stay in this DSN, never committed). Applies the schema if missing."
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="alamat bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8080, help="port (default: %(default)s)")
    parser.add_argument(
        "--role", default="owner", choices=list(ROLES), help="peran operator (default: %(default)s)"
    )
    parser.add_argument("--actor", default=None, help="identitas operator (default: per peran)")
    parser.add_argument("--dump", default=None, metavar="DIR", help="tulis halaman statis lalu keluar")
    parser.add_argument("--anchor-dir", default=None, help="direktori anker rantai audit")
    parser.add_argument("--templates-dir", default=None, help="direktori template alternatif")
    parser.add_argument("--token", default=None, help="token tetap untuk peran operator")
    parser.add_argument(
        "--agency",
        default=None,
        help=(
            "agency yang dilayani dashboard ini (default: VILLA_AGENCY_ID, "
            "lalu agency_default); token yang diterbitkan terikat pada agency ini"
        ),
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="izinkan bind ke 0.0.0.0/:: (default: loopback saja)",
    )
    return parser


def _open_db(path: Any) -> Any:
    from schema_loader import connect_sqlite

    target = Path(path)
    if not target.exists():
        raise InvalidInput("berkas database tidak ditemukan", code="DB_NOT_FOUND", db=str(target))
    return connect_sqlite(target)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: ``python3 src/dashboard.py --db data/villa.db --port 8080 --role owner``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.pg_dsn and args.db != DEFAULT_DB:
        print(
            "dashboard: --pg-dsn and --db are mutually exclusive; choose one data source",
            file=sys.stderr,
        )
        return 2
    try:
        if args.pg_dsn:
            from schema_loader import connect_pg

            conn = connect_pg(args.pg_dsn, apply=True)
        else:
            conn = _open_db(args.db)
    except (DomainError, OSError) as exc:
        print(f"dashboard: menolak start: {redact_pii(exc)}", file=sys.stderr)
        return 2
    identity = args.actor or DEFAULT_IDENTITIES[args.role]
    agency = resolve_agency_id(args.agency)
    tokens = {args.token: (identity, args.role, agency)} if args.token else None
    options: dict[str, Any] = {
        "tokens": tokens,
        "agency_id": agency,
        "templates_dir": args.templates_dir,
        "anchor_dir": args.anchor_dir,
    }
    try:
        if args.dump:
            written = dump_all(
                args.dump,
                conn=conn,
                clock=SystemClock(),
                actor=identity,
                actor_role=args.role,
                **options,
            )
            print(f"dashboard: {len(written)} halaman ditulis ke {args.dump}")
            return 0
        handler = create_app(
            conn,
            clock=SystemClock(),
            actor=identity,
            actor_role=args.role,
            **options,
        )
        server = serve(handler, args.host, args.port, allow_remote=args.allow_remote)
        token = args.token or _token_for(handler, args.role)
    except DomainError as exc:
        print(f"dashboard: menolak start: [{exc.code}] {redact_pii(exc)}", file=sys.stderr)
        return 2
    host, port = server.server_address[0], server.server_address[1]
    print(f"dashboard: http://{host}:{port}/ (peran {args.role}, baca-saja kecuali approval)")
    if token:
        print(f"dashboard: token {args.role}: {token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("dashboard: berhenti")
    finally:
        server.server_close()
    return 0


def _token_for(handler_class: type, role: str) -> Optional[str]:
    app = getattr(handler_class, "dashboard", None)
    return None if app is None else app.token_for_role(role)


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())

"""Public marketing website: the guest-facing surface of Villa Agency OS.

Product boundary (``docs/PRODUCT_SURFACE_SEPARATION_2026-10-05.md``): this module
is the public storefront for prospective guests (home, villa collection,
destinations, villa detail, inquiry intake). It shares the kernel — the case
spine, the CRM guest master, and read-only inventory queries — with the internal
dashboard, but it never exposes internal operations: no audit chain, no
approvals, no finance, no staff pages, no channel back office.

Recorded decisions:
 * Language: en-US copy (ADR-0011 direction; the language ratchet pins ``src/``
   literals). An Indonesian marketing locale is a separate, future decision.
 * Guest PII: the email lives in the ``guests`` table (its designed home, written
   through :class:`crm.CrmService`); the case carries only the opaque
   ``guest_id``. The message body is stored under content class ``P2`` — the
   stored copy is redacted, the raw text survives only as a sha256 digest.
 * SLA: channel ``website`` maps to policy ``web_v1``; the promised response
   times shown on the site are read from ``cases.SLA_POLICIES`` at render time,
   never restated here.
 * Anti-abuse: honeypot field, per-IP sliding-window rate limit, same-origin
   check on POST, bounded form size and message length, opaque-token validation
   for the property hint. All fail closed; no third-party services, no external
   assets (single-file inline CSS/SVG only).
 * Serving: deliberately single-threaded (the sqlite connection is bound to the
   creating thread, same reason as the internal dashboard) and loopback-bound
   unless ``--allow-remote`` is given explicitly.

CLI::

    python3 src/marketing_site.py --db /tmp/demo/villa.db --port 8090
    python3 src/marketing_site.py --db /tmp/demo/villa.db --dump /tmp/demo/site
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from collections import deque
from types import MappingProxyType
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlsplit

from cases import CaseService, SLA_POLICIES
from crm import CrmService, DuplicateGuest, CrmError
from dashboard import Markup, escape_value, render_page, serve
from primitives import DomainError, IdFactory, SystemClock, require_ref, resolve_agency_id
from schema_loader import connect_sqlite

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATES_DIR = REPO_ROOT / "templates" / "marketing"

#: Fallback villa shown on the home page when the catalogue is empty.
FEATURED_VILLA_LIMIT = 3

#: Public form bounds. The message cap stays well under the kernel's own body
#: cap (``cases.MAX_BODY_CHARS``), so the spine never has to truncate.
MAX_NAME_CHARS = 100
MAX_MESSAGE_CHARS = 5_000
MAX_FORM_BYTES = 32 * 1024

#: The honeypot field: humans never see it, so a filled value means a bot.
HONEYPOT_FIELD = "company"
FORM_TOKEN_FIELD = "form_token"
_FORM_TOKEN_RE = re.compile(r"^[0-9a-f]{16,64}$")
_EMAIL_HINT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Content security policy for the public surface. Styles are inline (single
#: file, no build step); scripts are not used at all.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
)

#: A visitor IP may submit at most this many inquiries per window.
RATE_LIMIT_MAX_EVENTS = 5
RATE_LIMIT_WINDOW_SECONDS = 600

SELLABLE_STATUS = "SELLABLE"


class IntakeError(DomainError):
    """Base class for public intake failures (each code is a stable contract)."""


class FormInvalid(IntakeError):
    """A field failed validation; the visitor can fix and resubmit."""

    code = "FORM_INVALID"


class SpamRejected(IntakeError):
    """The honeypot was filled; treat the submission as automated."""

    code = "SPAM_REJECTED"


class RateLimited(IntakeError):
    """Too many submissions from one address inside the rate window."""

    code = "RATE_LIMITED"


def compose_inquiry_body(name: str, message: str) -> str:
    """The inbound message body recorded on the case spine.

    Kept public so tests (and auditors) can verify the stored digest against the
    exact raw text. The email is deliberately *not* part of the body: it lives
    in the ``guests`` table, and the stored P2 copy is redacted anyway.
    """
    return f"Inquiry from the website\nName: {name}\n\n{message}"


class SlidingWindowRateLimiter:
    """Per-key sliding-window limiter driven by the site clock.

    Uses the injected clock, so a frozen clock in tests makes the window fully
    deterministic (it never slides until the test moves it).
    """

    def __init__(self, clock: Any, *, max_events: int, window_seconds: int) -> None:
        self.clock = clock
        self.max_events = int(max_events)
        self.window_seconds = int(window_seconds)
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock.now().timestamp()
        window = self._events.setdefault(key, deque())
        while window and now - window[0] > self.window_seconds:
            window.popleft()
        if len(window) >= self.max_events:
            return False
        window.append(now)
        return True


class PublicReadModel:
    """Read-only, tenant-scoped catalogue queries for the public site.

    Only ``SELLABLE`` properties ever leave the database here —
    ``SELLABLE_WITH_REVIEW``, ``NOT_SELLABLE`` and ``SUSPENDED`` are internal
    states a guest must never see.
    """

    def __init__(self, conn: Any, agency_id: str) -> None:
        self.conn = conn
        self.agency_id = require_ref(agency_id, "agency_id")

    def sellable_villas(self, *, province: Optional[str] = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.property_id AS property_id, p.name AS name, p.region AS region,"
            " p.province AS province, p.property_type AS property_type,"
            " COUNT(u.unit_id) AS active_units,"
            " COALESCE(MIN(NULLIF(u.bedrooms, 0)), 0) AS bedrooms_min,"
            " COALESCE(MAX(u.bedrooms), 0) AS bedrooms_max,"
            " COALESCE(MAX(u.max_guests), 0) AS sleeps_max"
            " FROM properties p"
            " LEFT JOIN units u ON u.property_id = p.property_id"
            "  AND u.agency_id = p.agency_id AND u.status = 'ACTIVE'"
            " WHERE p.agency_id = ? AND p.status = ?"
        )
        params: list[Any] = [self.agency_id, SELLABLE_STATUS]
        if province is not None:
            sql += " AND p.province = ?"
            params.append(province)
        sql += (
            " GROUP BY p.property_id, p.name, p.region, p.province, p.property_type"
            " ORDER BY p.province, p.region, p.name"
        )
        return [dict(row) for row in self.conn.execute(sql, params)]

    def villa(self, property_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT property_id, name, region, province, property_type"
            " FROM properties WHERE agency_id = ? AND property_id = ? AND status = ?",
            (self.agency_id, require_ref(property_id, "property_id"), SELLABLE_STATUS),
        ).fetchone()
        return None if row is None else dict(row)

    def active_units(self, property_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT name, unit_type, bedrooms, bathrooms, base_capacity, max_guests"
                " FROM units WHERE agency_id = ? AND property_id = ? AND status = 'ACTIVE'"
                " ORDER BY bedrooms DESC, name",
                (self.agency_id, require_ref(property_id, "property_id")),
            )
        ]


class InquiryIntake:
    """Turn a validated public form into a kernel case — the only write path."""

    def __init__(self, conn: Any, *, agency_id: str, clock: Any, ids: Any) -> None:
        self.agency_id = require_ref(agency_id, "agency_id")
        self.clock = clock
        self.cases = CaseService(conn, clock, ids, agency_id=self.agency_id)
        self.crm = CrmService(
            conn, clock=clock, ids=ids, agency_id=self.agency_id, case_service=self.cases
        )

    def submit(
        self,
        *,
        form: Mapping[str, str],
        remote_addr: str,
        rate_limiter: Any,
        expected_origin_host: Optional[str],
        origin_header: Optional[str],
    ) -> dict[str, str]:
        """Validate the form, upsert the guest, open the case.

        Returns ``{"reference", "case_id", "guest_id"}``; raises :class:`IntakeError`
        subclasses for every rejection the visitor (or a bot) can cause.
        """
        if form.get(HONEYPOT_FIELD, "").strip():
            raise SpamRejected("this submission was rejected")
        if origin_header and expected_origin_host:
            if urlsplit(origin_header).netloc != expected_origin_host:
                raise FormInvalid("this form must be submitted from the site itself")
        if not rate_limiter.allow(remote_addr):
            raise RateLimited("too many inquiries from this address; please try again later")

        name = self._require_field(form, "name", MAX_NAME_CHARS)
        email = self._require_field(form, "email", 320)
        message = self._require_field(form, "message", MAX_MESSAGE_CHARS)
        token = self._require_field(form, FORM_TOKEN_FIELD, 64)
        if not _FORM_TOKEN_RE.match(token):
            raise FormInvalid("this form session has expired; please reload the page")
        if len(message) < 10:
            raise FormInvalid("please describe your trip in at least 10 characters")
        if not _EMAIL_HINT_RE.match(email):
            raise FormInvalid("please enter a valid email address")

        property_ref = form.get("property_ref", "").strip() or None
        if property_ref is not None:
            try:
                property_ref = require_ref(property_ref, "property_ref")
            except DomainError:
                raise FormInvalid("unknown villa reference") from None

        guest = self.crm.find_guest(email=email)
        if guest is None:
            try:
                guest_id = self.crm.register_guest(name, email=email, locale="en-US")
            except DuplicateGuest:  # pragma: no cover - single-threaded server
                existing = self.crm.find_guest(email=email)
                guest_id = str(existing["guest_id"])
        else:
            guest_id = str(guest["guest_id"])

        idempotency_key = token
        try:
            result = self.crm.open_inquiry(
                guest_id=guest_id,
                channel="website",
                body=compose_inquiry_body(name, message),
                idempotency_key=idempotency_key,
                content_class="P2",
                channel_ref=property_ref,
            )
        except DomainError as exc:
            if exc.code == "DUPLICATE_CASE":
                return self._existing_result(idempotency_key)
            raise

        return {
            "reference": str(result["reference"]),
            "case_id": str(result["case_id"]),
            "guest_id": guest_id,
        }

    def _existing_result(self, idempotency_key: str) -> dict[str, str]:
        row = self.cases.conn.execute(
            "SELECT case_id, reference FROM cases"
            " WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, idempotency_key),
        ).fetchone()
        if row is None:  # pragma: no cover - the duplicate row must exist
            raise CrmError("duplicate case reported but not found", code="STATE_INCONSISTENT")
        return {
            "reference": str(row["reference"] or row["case_id"]),
            "case_id": str(row["case_id"]),
            "guest_id": "",
        }

    @staticmethod
    def _require_field(form: Mapping[str, str], field: str, max_chars: int) -> str:
        value = form.get(field, "").strip()
        if not value:
            raise FormInvalid(f"the {field} field is required")
        if len(value) > max_chars:
            raise FormInvalid(f"the {field} field is too long (max {max_chars} characters)")
        return value


def _card_html(villa: Mapping[str, Any]) -> Markup:
    """One villa card; every dynamic value passes through ``escape_value``."""
    bedrooms = int(villa["bedrooms_max"])
    sleeps = int(villa["sleeps_max"])
    units = int(villa["active_units"])
    facts: list[str] = []
    if bedrooms:
        facts.append(f"{bedrooms} bedrooms" if bedrooms == int(villa["bedrooms_min"]) else f"up to {bedrooms} bedrooms")
    if sleeps:
        facts.append(f"sleeps {sleeps}")
    if units:
        facts.append(f"{units} unit{'s' if units != 1 else ''}")
    fact_line = " · ".join(facts) if facts else "Details on request"
    href = escape_value(f"/villas/{villa['property_id']}")
    return Markup(
        f'<article class="card">'
        f'<div class="card-region">{escape_value(villa["province"])} · {escape_value(villa["region"])}</div>'
        f'<h3><a href="{href}">{escape_value(villa["name"])}</a></h3>'
        f'<p class="card-facts">{escape_value(villa["property_type"].replace("_", " "))} — {escape_value(fact_line)}</p>'
        f'<a class="card-link" href="{href}">View villa &rarr;</a>'
        f'</article>'
    )


def _villa_row_html(unit: Mapping[str, Any]) -> Markup:
    unit_type = str(unit["unit_type"]).replace("_", " ")
    return Markup(
        f"<tr><td>{escape_value(unit['name'])}</td>"
        f"<td>{escape_value(unit_type)}</td>"
        f"<td>{escape_value(unit['bedrooms'])}</td>"
        f"<td>{escape_value(unit['bathrooms'])}</td>"
        f"<td>{escape_value(unit['max_guests'])}</td></tr>"
    )


def _units_table(units: list[Mapping[str, Any]]) -> Markup:
    if not units:
        return Markup("<p>Unit details are confirmed by our team on request.</p>")
    rows = "".join(str(_villa_row_html(unit)) for unit in units)
    return Markup(
        "<table><thead><tr><th>Unit</th><th>Type</th><th>Bedrooms</th>"
        "<th>Bathrooms</th><th>Sleeps</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _sla_text() -> str:
    policy = SLA_POLICIES["web_v1"]
    return (
        f"Our team confirms availability within {policy.first_response_minutes} minutes"
        f" and sends a full quote within {policy.quote_minutes // 60} hours"
        " during opening hours."
    )


class MarketingSite:
    """Wires the read model, the intake and the templates into one app."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any = None,
        ids: Any = None,
        agency_id: Optional[str] = None,
        templates_dir: Optional[Any] = None,
        rate_limiter: Any = None,
    ) -> None:
        self.conn = conn
        self.agency_id = agency_id or resolve_agency_id()
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.templates_dir = Path(templates_dir) if templates_dir else DEFAULT_TEMPLATES_DIR
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            self.clock,
            max_events=RATE_LIMIT_MAX_EVENTS,
            window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        )
        self.read_model = PublicReadModel(self.conn, self.agency_id)
        self.intake = InquiryIntake(
            self.conn, agency_id=self.agency_id, clock=self.clock, ids=self.ids
        )

    # Live-URL → static-filename map for --dump link rewriting (villas/{id} is
    # wildcard; matched separately in _page). Everything else is a fixed file.
    DUMP_FILE_MAP: Mapping[str, str] = MappingProxyType(
        {
            "/": "index.html",
            "/villas": "villas.html",
            "/destinations/bali": "destinations/bali.html",
            "/destinations/yogyakarta": "destinations/yogyakarta.html",
            "/inquiry": "inquiry.html",
        }
    )
    def _page(self, name: str, context: Mapping[str, Any], base: str = "") -> str:
        html = render_page(name, context, templates_dir=self.templates_dir)
        if base:
            # Static-dump mode: rewrite site-absolute hrefs so static files open
            # correctly from file://. Every live URL maps to its dump filename
            # (directory URLs point to that dir's index.html); the target is then
            # expressed as a relative path from this page's dump subdirectory.
            import posixpath

            def _rel(match: "re.Match[str]") -> str:
                target = match.group(1)
                path, _sep, query = target.partition("?")
                path = "/" + path
                if path != "/" and path.endswith("/"):
                    path = path.rstrip("/")
                file = self.DUMP_FILE_MAP.get(path)
                if file is None and path.startswith("/villas/"):
                    file = path.lstrip("/") + ".html"
                if file is None:
                    return match.group(0)
                if file.endswith("/"):
                    file += "index.html"
                rel = posixpath.relpath(file, base or ".")
                if query:
                    rel += "?" + query
                return 'href="' + rel + '"'

            html = re.sub(r'href="/([^"#]*)"', _rel, html)
        return html

    def home_html(self, base: str = "") -> str:
        villas = self.read_model.sellable_villas()
        featured = villas[:FEATURED_VILLA_LIMIT]
        cards = "".join(str(_card_html(v)) for v in featured) or (
            "<p class='empty'>Our collection is being curated right now.</p>"
        )
        return self._page(
            "home",
            {
                "page_title": "Villa Agency — Private Villas in Bali & Yogyakarta",
                "villa_cards": Markup(cards),
                "head_extra": Markup(""),
            },
            base=base,
        )

    def villas_html(self, base: str = "") -> str:
        villas = self.read_model.sellable_villas()
        cards = "".join(str(_card_html(v)) for v in villas) or (
            "<p class='empty'>No villas are published yet.</p>"
        )
        return self._page(
            "villas",
            {
                "page_title": "Villa Collection — Villa Agency",
                "villa_cards": Markup(cards),
                "villa_count": len(villas),
                "head_extra": Markup(""),
            },
            base=base,
        )

    def destination_html(self, province: str, base: str = "") -> Optional[str]:
        names = {"bali": "Bali", "yogyakarta": "Yogyakarta"}
        label = names.get(province)
        if label is None:
            return None
        villas = self.read_model.sellable_villas(province={"bali": "Bali", "yogyakarta": "DI Yogyakarta"}[province])
        cards = "".join(str(_card_html(v)) for v in villas) or (
            "<p class='empty'>Villas for this destination are being curated.</p>"
        )
        return self._page(
            "destination",
            {
                "page_title": f"{label} Villas — Villa Agency",
                "destination_name": escape_value(label),
                "villa_cards": Markup(cards),
                "head_extra": Markup(""),
            },
            base=base,
        )

    def villa_html(self, property_id: str, base: str = "") -> Optional[str]:
        try:
            villa = self.read_model.villa(property_id)
        except DomainError:
            return None
        if villa is None:
            return None
        units = self.read_model.active_units(property_id)
        return self._page(
            "villa_detail",
            {
                "page_title": f"{villa['name']} — Villa Agency",
                "villa_name": escape_value(villa["name"]),
                "villa_meta": escape_value(f"{villa['province']} · {villa['region']} · {villa['property_type'].replace('_', ' ')}"),
                "units_table": _units_table(units),
                "property_ref": escape_value(property_id),
                "head_extra": Markup(""),
            },
            base=base,
        )

    def inquiry_form_html(self, property_ref: str = "", base: str = "") -> str:
        token = secrets.token_hex(16)
        asking = ""
        if property_ref:
            try:
                villa = self.read_model.villa(property_ref)
            except DomainError:
                villa = None
            if villa is not None:
                asking = Markup(
                    f"<p class='note'>You are asking about: {escape_value(villa['name'])}</p>"
                )
        return self._page(
            "inquiry_form",
            {
                "page_title": "Plan My Stay — Villa Agency",
                "form_token": escape_value(token),
                "property_ref": escape_value(property_ref),
                "asking_note": Markup(asking),
                "sla_text": escape_value(_sla_text()),
                "head_extra": Markup(""),
            },
            base=base,
        )

    def inquiry_success_html(self, result: Mapping[str, str]) -> str:
        return self._page(
            "inquiry_success",
            {
                "page_title": "Inquiry received — Villa Agency",
                "reference": escape_value(result["reference"]),
                "sla_text": escape_value(_sla_text()),
                "head_extra": Markup('<meta name="robots" content="noindex">'),
            },
        )

    def error_html(self, status: int, message: str) -> str:
        return self._page(
            "error",
            {
                "page_title": f"{status} — Villa Agency",
                "error_code": escape_value(status),
                "error_message": escape_value(message),
                "head_extra": Markup('<meta name="robots" content="noindex">'),
            },
        )

    # -- static dump -------------------------------------------------------- #
    def dump(self, directory: Any) -> list[Path]:
        """Write every public GET page as static HTML (no secrets, no PII).

        ``base`` marks subdirectory pages so links are rewritten relative
        (``../``) and the dump opens correctly from ``file://``.
        """
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        written = [out / "inquiry.html"]
        (out / "inquiry.html").write_text(
            self.inquiry_form_html(base="."), encoding="utf-8"
        )
        (out / "index.html").write_text(self.home_html(base="."), encoding="utf-8")
        (out / "villas.html").write_text(self.villas_html(base="."), encoding="utf-8")
        written.append(out / "index.html")
        written.append(out / "villas.html")
        for slug, _province in (("bali", "Bali"), ("yogyakarta", "DI Yogyakarta")):
            page = self.destination_html(slug, base="destinations/")
            if page is not None:
                target = out / "destinations" / f"{slug}.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(page, encoding="utf-8")
                written.append(target)
        for villa in self.read_model.sellable_villas():
            target = out / "villas" / f"{villa['property_id']}.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self.villa_html(str(villa["property_id"]), base="villas/") or "",
                encoding="utf-8",
            )
            written.append(target)
        return written

    # -- serving ------------------------------------------------------------ #
    def handler_class(self) -> type:
        return build_handler(self)


class _MarketingRequestHandler(BaseHTTPRequestHandler):
    """Public request handler: no auth, no sessions, read model + one form."""

    server_version = "VillaMarketing/1.0"
    site: MarketingSite

    # -- helpers ------------------------------------------------------------ #
    def _send_html(self, html: str, status: int = 200, *, no_store: bool = False) -> None:
        payload = html.encode("utf-8")
        cache = "no-store" if no_store else "public, max-age=300"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if no_store:
            self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._send_html(self.site.error_html(status, message), status, no_store=True)

    # -- GET ---------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        site = self.site
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._send_html(site.home_html())
        elif path == "/villas":
            self._send_html(site.villas_html())
        elif path.startswith("/villas/"):
            slug = path[len("/villas/"):]
            page = None
            try:
                require_ref(slug, "villa")
                page = site.villa_html(slug)
            except DomainError:
                page = None
            if page is None:
                self._error(404, "That villa page does not exist.")
            else:
                self._send_html(page)
        elif path == "/destinations/bali":
            page = site.destination_html("bali")
            if page is None:
                self._error(404, "Page not found.")
            else:
                self._send_html(page)
        elif path == "/destinations/yogyakarta":
            page = site.destination_html("yogyakarta")
            if page is None:
                self._error(404, "Page not found.")
            else:
                self._send_html(page)
        elif path == "/inquiry":
            query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
            property_ref = dict(query).get("property", "").strip()
            if property_ref:
                try:
                    require_ref(property_ref, "property")
                except DomainError:
                    property_ref = ""
            self._send_html(site.inquiry_form_html(property_ref))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        else:
            self._error(404, "Page not found.")

    # -- POST --------------------------------------------------------------- #
    def _read_form(self) -> Optional[dict[str, str]]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError:
            return None
        if length <= 0 or length > MAX_FORM_BYTES:
            return None
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        pairs = parse_qsl(body, keep_blank_values=True, max_num_fields=32)
        form = dict(pairs)
        if len(form) != len(pairs):
            return None  # duplicate field names: malformed by construction
        return form

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if urlsplit(self.path).path.rstrip("/") != "/inquiry":
            self._error(405, "Only the inquiry form accepts submissions.")
            return
        form = self._read_form()
        if form is None:
            self._error(400, "The form could not be read; please reload and try again.")
            return
        try:
            result = self.site.intake.submit(
                form=form,
                remote_addr=self.client_address[0],
                rate_limiter=self.site.rate_limiter,
                expected_origin_host=self.headers.get("Host"),
                origin_header=self.headers.get("Origin"),
            )
        except RateLimited as exc:
            self._error(429, str(exc.message))
            return
        except (FormInvalid, SpamRejected) as exc:
            self._error(400, str(exc.message))
            return
        except DomainError:  # kernel rejection: never leak internals
            self._error(500, "Something went wrong on our side; please try again shortly.")
            return
        self._send_html(self.site.inquiry_success_html(result), no_store=True)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def build_handler(site: MarketingSite) -> type:
    """Return a handler class bound to one site (same pattern as the dashboard)."""
    return type(
        "BoundMarketingHandler",
        (_MarketingRequestHandler,),
        {"site": site},
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Public marketing website server")
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--agency", default=None, help="agency id (defaults to the environment)")
    parser.add_argument("--allow-remote", action="store_true", help="bind beyond loopback")
    parser.add_argument("--dump", default=None, help="write static pages and exit")
    args = parser.parse_args(argv)

    conn = connect_sqlite(args.db)
    site = MarketingSite(conn, agency_id=args.agency)
    if args.dump:
        written = site.dump(args.dump)
        print(f"wrote {len(written)} pages to {args.dump}")
        return 0
    httpd = serve(build_handler(site), args.host, args.port, allow_remote=args.allow_remote)
    host, port = httpd.server_address[:2]
    print(f"marketing site on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

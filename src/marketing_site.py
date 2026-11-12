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
 * Guest currency default: a new guest's ``preferred_currency`` (IDR or USD —
   display default only) is inferred from the apparent visitor country on the
   inquiry request (ADR-0060); it never prices anything shown on this site —
   no price is shown here at all, a human sends every quote.
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
   network assets (single-file response: inline CSS/SVG, and base64-embedded
   JPEG villa photos read from ``assets/photos/`` at startup — zero new routes,
   zero outbound requests from the rendered page; see ``docs/PHOTO_CREDITS_2026-10-06.md``).
 * Serving: deliberately single-threaded (the sqlite connection is bound to the
   creating thread, same reason as the internal dashboard) and loopback-bound
   unless ``--allow-remote`` is given explicitly.

CLI::

    python3 src/marketing_site.py --db /tmp/demo/villa.db --port 8090
    python3 src/marketing_site.py --db /tmp/demo/villa.db --dump /tmp/demo/site
"""

from __future__ import annotations

import argparse
import base64
import functools
import ipaddress
import json
import re
import secrets
import sqlite3
import sys
import traceback
from collections import deque
from types import MappingProxyType
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit

import fx as FX
import geo as Geo
import villa_showcase as Showcase

from cases import CaseService, SLA_POLICIES
from crm import CrmService, DuplicateGuest, CrmError
from dashboard import Markup, SafeHeaderMixin, escape_value, render_page, serve
from primitives import DomainError, IdFactory, SystemClock, require_ref, resolve_agency_id, sha256_hex, to_iso
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

# A single stalled socket must not own the process. ``http.server`` is
# single-threaded here, so without a read timeout a client that sends half a
# request and then goes quiet blocks *every* visitor indefinitely — one
# connection, total outage, no credentials needed. Twenty seconds is far more
# than any real request needs and far less than an attacker would like.
CONNECTION_TIMEOUT_SECONDS = 20

#: Villa photo placeholders — CC BY-SA / public-domain stock from Wikimedia
#: Commons (credits + source links: ``docs/PHOTO_CREDITS_2026-10-06.md``), NOT
#: real photos of the listed properties. Matched to a villa by a keyword found
#: in its name; unmatched villas fall back to the CSS "Photo coming soon" card
#: (see ``.card .photo.placeholder`` in ``templates/marketing/base.html``).
#: The keyword table and the per-villa area/style copy live in
#: :mod:`villa_showcase`, so the binding has one owner.
PHOTOS_DIR = REPO_ROOT / "assets" / "photos"
HERO_PHOTO_FILE = "hero_bali_pool.jpg"

#: Ambience strip on the villa detail page — illustrative resort shots, same
#: CC BY-SA / CC0 sourcing as the villa cards (see the credits doc).
_GALLERY_PHOTOS: tuple[str, ...] = (
    "gallery/amb1_reserve_pool.jpg",
    "gallery/amb2_private_pool_villa.jpg",
    "gallery/amb3_pool_sunset.jpg",
)


@functools.lru_cache(maxsize=None)
def _photo_data_uri(relative: str) -> Optional[str]:
    """Base64 data: URI for a file under ``assets/photos/``, cached after first read."""
    path = PHOTOS_DIR / relative
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _villa_photo_uri(villa_name: str, size: str = "card") -> Optional[str]:
    """Photo URI for a villa by name keyword; ``size`` picks the card or detail set."""
    stem = Showcase.photo_stem_for(villa_name)
    if stem is None:
        return None
    return _photo_data_uri(f"{size}/{stem}.jpg")


def _gallery_html() -> Markup:
    """Illustrative ambience strip for the villa detail page (skips missing files)."""
    tiles = []
    for rel in _GALLERY_PHOTOS:
        uri = _photo_data_uri(rel)
        if uri:
            tiles.append(f'<div class="photo-tile" style="background-image:url(\'{uri}\')" role="img" aria-label="Illustrative resort ambience"></div>')
    if not tiles:
        return Markup("")
    return Markup(
        '<h2>Ambience</h2>'
        '<p class="note">Illustrative resort imagery — not this villa. '
        'Ask our team for current photos of the property.</p>'
        f'<div class="photo-strip">{"".join(tiles)}</div>'
    )

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

#: The header the reverse proxy must write for the client address. Read only
#: when the site declares the proxy trusted (see ``--trusted-proxy``).
FORWARDED_FOR_HEADER = "X-Forwarded-For"

SELLABLE_STATUS = "SELLABLE"


def client_address_from_forwarded_for(
    value: Optional[str], *, peer: str
) -> str:
    """The client address a trusted proxy reported, or ``peer`` when it cannot be read.

    Only the **last** hop is considered. A proxy either replaces the header
    outright (``proxy_set_header X-Forwarded-For $remote_addr;``) or appends the
    address it actually saw (``$proxy_add_x_forwarded_for``); in both shapes the
    final element is written by our own proxy, so it is the one element a
    visitor cannot forge. Everything a visitor could have sent — earlier hops,
    junk, an empty header — falls back to the socket peer, never to a claim.

    The value is normalised through :mod:`ipaddress`, so one client that appears
    as ``::ffff:203.0.113.7`` and as ``203.0.113.7`` counts once.
    """
    parts = [part.strip() for part in (value or "").split(",")]
    candidates = [part for part in parts if part]
    if not candidates:
        return peer
    last = candidates[-1]
    if last.startswith("["):  # "[2001:db8::1]:443" — proxy wrote the port
        last = last.partition("]")[0].lstrip("[")
    else:
        host, sep, port = last.rpartition(":")
        if sep and port.isdigit() and host.count(":") == 0:
            last = host  # "203.0.113.7:51234"
    try:
        parsed = ipaddress.ip_address(last)
    except ValueError:
        return peer
    # One client, one key: an IPv4-mapped IPv6 form is the same visitor.
    mapped = getattr(parsed, "ipv4_mapped", None)
    return str(mapped if mapped is not None else parsed)


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
        if len(self._events) > self.MAX_TRACKED_CLIENTS:
            self._evict(now)
        return True

    #: Ceiling on the number of distinct clients held in memory. Without it a caller
    #: that rotates addresses (an IPv6 /64 is free) grows the map forever: each new
    #: key is a deque that never expires, because nothing sweeps the map itself.
    MAX_TRACKED_CLIENTS = 4096

    def tracked_clients(self) -> int:
        """How many client keys the limiter currently holds (observability + tests)."""
        return len(self._events)

    def _evict(self, now: float) -> None:
        """Drop expired windows first, then the least recently seen clients."""
        for key in [k for k, w in self._events.items() if not w or now - w[-1] > self.window_seconds]:
            del self._events[key]
        overflow = len(self._events) - self.MAX_TRACKED_CLIENTS
        if overflow <= 0:
            return
        oldest = sorted(
            self._events.items(),
            key=lambda item: item[1][-1] if item[1] else float("-inf"),
        )[:overflow]
        for key, _window in oldest:
            del self._events[key]


class PublicReadModel:
    """Read-only, tenant-scoped catalogue queries for the public site.

    Only ``SELLABLE`` properties ever leave the database here —
    ``SELLABLE_WITH_REVIEW``, ``NOT_SELLABLE`` and ``SUSPENDED`` are internal
    states a guest must never see.
    """

    def __init__(self, conn: Any, agency_id: str) -> None:
        self.conn = conn
        self.agency_id = require_ref(agency_id, "agency_id")
        #: Geometric columns arrive in migration 017. A database that has not been
        #: migrated yet must still serve its catalogue — without pins, not with a 500.
        self.has_geo = self._detect_geo()

    def _detect_geo(self) -> bool:
        try:
            self.conn.execute("SELECT latitude, longitude, geo_precision FROM properties LIMIT 0")
        except sqlite3.OperationalError:
            return False
        return True

    def _geo_select(self) -> str:
        if self.has_geo:
            return " p.latitude AS latitude, p.longitude AS longitude, p.geo_precision AS geo_precision,"
        return " NULL AS latitude, NULL AS longitude, 'UNKNOWN' AS geo_precision,"

    def sellable_villas(self, *, province: Optional[str] = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.property_id AS property_id, p.name AS name, p.region AS region,"
            " p.province AS province, p.property_type AS property_type," + self._geo_select() +
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
            + (", p.latitude, p.longitude, p.geo_precision" if self.has_geo else "")
            + " ORDER BY p.province, p.region, p.name"
        )
        return [dict(row) for row in self.conn.execute(sql, params)]

    def villa(self, property_id: str) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT p.property_id AS property_id, p.name AS name, p.region AS region,"
            " p.province AS province, p.property_type AS property_type,"
            + self._geo_select().rstrip(",")
            + " FROM properties p WHERE p.agency_id = ? AND p.property_id = ? AND p.status = ?"
        )
        row = self.conn.execute(
            sql, (self.agency_id, require_ref(property_id, "property_id"), SELLABLE_STATUS)
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

    def availability_hint(self, property_id: str, now_iso: str) -> str:
        """Guest-safe availability hint: useful, but never an instant-confirm promise."""
        row = self.conn.execute(
            "SELECT ae.result, ae.evidence_level, ae.can_quote, ae.can_hold, ae.can_confirm,"
            " ae.valid_until"
            " FROM availability_evidence ae"
            " JOIN units u ON u.agency_id = ae.agency_id AND u.unit_id = ae.unit_id"
            " WHERE ae.agency_id = ? AND u.property_id = ? AND u.status = 'ACTIVE'"
            " AND ae.valid_until > ?"
            " ORDER BY ae.captured_at DESC, ae.evidence_id DESC LIMIT 1",
            (self.agency_id, require_ref(property_id, "property_id"), now_iso),
        ).fetchone()
        if row is None:
            return (
                "Availability hint: live availability is not published here. "
                "Our team checks with the villa owner before sending a quote."
            )
        if row["result"] == "AVAILABLE" and int(row["can_quote"]):
            if int(row["can_confirm"]):
                return "Availability hint: recently verified; our team still reconfirms before payment."
            if int(row["can_hold"]):
                return "Availability hint: likely available for a short hold after team verification."
            return "Availability hint: likely quotable after team verification."
        if row["result"] == "AVAILABLE_BUT_RESTRICTED":
            return "Availability hint: dates may work, but special restrictions must be checked first."
        if row["result"] == "NOT_AVAILABLE":
            return "Availability hint: latest evidence says selected dates may be unavailable. Ask us for nearby dates."
        return "Availability hint: availability is being checked before any quote is promised."


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
        country_hint: Optional[str] = None,
    ) -> dict[str, str]:
        """Validate the form, upsert the guest, open the case.

        ``country_hint`` is the visitor's apparent country (e.g. a trusted edge header such
        as ``CF-IPCountry``) — cosmetic only. It sets the new guest's ``preferred_currency``
        (ADR-0060: IDR for Indonesia, USD otherwise) so staff see the right default currency
        in the dashboard; it never prices anything and is ignored for a guest who already
        exists.

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
        body = compose_inquiry_body(name, message)
        existing_result = self._existing_result(token, guest, body, property_ref)
        if existing_result is not None:
            return existing_result
        if guest is None:
            try:
                guest_id = self.crm.register_guest(
                    name, email=email, locale="en-US",
                    preferred_currency=FX.infer_guest_currency(country_hint),
                )
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
                body=body,
                idempotency_key=idempotency_key,
                content_class="P2",
                channel_ref=property_ref,
            )
        except DomainError as exc:
            if exc.code == "DUPLICATE_CASE":
                existing_result = self._existing_result(
                    idempotency_key, self.crm.find_guest(email=email), body, property_ref
                )
                if existing_result is not None:
                    return existing_result
            raise

        return {
            "reference": str(result["reference"]),
            "case_id": str(result["case_id"]),
            "guest_id": guest_id,
        }

    def _existing_result(self, idempotency_key: str, guest: Any, body: str,
                         property_ref: Optional[str]) -> Optional[dict[str, str]]:
        row = self.cases.conn.execute(
            "SELECT case_id, reference, guest_ref, channel, channel_ref FROM cases"
            " WHERE agency_id = ? AND idempotency_key = ?",
            (self.agency_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        event = self.cases.conn.execute(
            "SELECT refs FROM case_events WHERE case_id = ? AND seq = 1 AND kind = 'CREATED'",
            (row["case_id"],),
        ).fetchone()
        try:
            refs = json.loads(event["refs"]) if event else []
            message_id = refs[0] if (isinstance(refs, list) and len(refs) == 1
                                     and isinstance(refs[0], str)) else None
        except (ValueError, TypeError):
            message_id = None
        message = self.cases.conn.execute(
            "SELECT body_sha256 FROM case_messages WHERE case_id = ? AND case_message_id = ?"
            " AND direction = 'INBOUND' AND channel = 'website'",
            (row["case_id"], message_id),
        ).fetchone()
        if (guest is None or row["guest_ref"] != guest["guest_id"]
                or row["channel"] != "website" or row["channel_ref"] != property_ref
                or message is None or message["body_sha256"] != sha256_hex(body)):
            raise FormInvalid("this form has already been used; please reload the page")
        if not row["reference"]:
            raise CrmError("inquiry reference is not yet available", code="STATE_INCONSISTENT")
        return {
            "reference": str(row["reference"]),
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


def _places_line(villa: Mapping[str, Any], *extra: str) -> str:
    """Join the display parts of a villa's location, dropping empty ones entirely.

    A dropped part must take its separator with it: joining ``province · region`` when
    the region duplicates the province would leave a stray " · " on the card.
    """
    parts = [
        Showcase.display_place(str(villa["province"]), str(villa["region"])),
        Showcase.display_place(str(villa["region"]), str(villa["province"])),
        *extra,
    ]
    return " · ".join(part for part in parts if part)


def _villa_point(villa: Mapping[str, Any]) -> Optional[tuple[float, float, str]]:
    """The public point this row may publish, or ``None`` when it may publish nothing.

    Rows come from databases that may predate migration 017, so a missing column reads
    as "no coordinate" rather than raising: a page that renders without a map is
    correct, a page that guesses a pin is not.
    """
    try:
        return Geo.resolve_point(
            villa.get("latitude"),
            villa.get("longitude"),
            villa.get("geo_precision") or "UNKNOWN",
        )
    except Geo.GeoError:
        # A row that stores a broken pair (hand-edited database, half-finished import)
        # must not take the whole catalogue page down with it.
        return None


def _area_map_html(villas: Sequence[Mapping[str, Any]]) -> Markup:
    """A map of the published areas, or nothing at all.

    Only villas with a publishable point are drawn; when none has one the page shows
    words instead of an empty frame, because an empty map implies "we have no villas".
    """
    points = []
    for villa in villas:
        resolved = _villa_point(villa)
        if resolved is None:
            continue
        lat, lon, precision = resolved
        points.append(
            {
                "label": str(villa["name"]),
                "latitude": lat,
                "longitude": lon,
                "detail": "area only" if precision == "AREA_APPROX" else "surveyed location",
            }
        )
    if not points:
        return Markup("")
    return Markup(Geo.area_map_svg(points, caption=(
        "Approximate areas only — pins show the village, not the gate. "
        "The exact address is shared once a booking is confirmed."
    )))


def _locator_html(villa: Mapping[str, Any]) -> Markup:
    """The detail page's "Where you'll be" block, with distances to public landmarks."""
    resolved = _villa_point(villa)
    if resolved is None:
        return Markup(
            '<h2>Where you\'ll be</h2>'
            f"<p>{escape_value(_places_line(villa))}. We share the exact address and a "
            "pin once your booking is confirmed.</p>"
        )
    lat, lon, _precision = resolved
    try:
        nearby = Geo.nearest_landmarks(lat, lon, str(villa["province"]))
    except Geo.GeoError:
        nearby = []
    rows = "".join(
        f"<tr><td>{escape_value(row['label'])}</td><td>{row['km']:g} km</td></tr>"
        for row in nearby
    )
    table = (
        "<table><thead><tr><th>Landmark</th><th>Straight-line</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else ""
    )
    return Markup(
        "<h2>Where you'll be</h2>"
        f"<p>{escape_value(_places_line(villa))}</p>"
        + Geo.locator_map_svg(lat, lon, str(villa["province"]), label=str(villa["name"]))
        + table
    )


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
    photo_uri = _villa_photo_uri(str(villa["name"]))
    if photo_uri:
        photo_div = f'<div class="photo" style="background-image:url(\'{photo_uri}\')" role="img" aria-label="{escape_value(villa["name"])} — illustrative photo"></div>'
    else:
        photo_div = '<div class="photo placeholder"></div>'
    return Markup(
        f'<article class="card">'
        f'{photo_div}'
        f'<div class="body">'
        f'<div class="card-region">{escape_value(_places_line(villa))}</div>'
        f'<h3><a href="{href}">{escape_value(villa["name"])}</a></h3>'
        f'<p class="card-facts">{escape_value(villa["property_type"].replace("_", " "))} — {escape_value(fact_line)}</p>'
        f'<a class="card-link" href="{href}">View villa &rarr;</a>'
        f'</div>'
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


def _villa_story_html(showcase: Showcase.VillaShowcase) -> Markup:
    """Area/style prose for one villa, plus the scope that prose does not cover.

    The copy comes from :mod:`villa_showcase` as plain text and is escaped here —
    a villa name or region that carries markup must not reach the page as markup.
    """
    return Markup(
        "<h2>About this villa</h2>"
        f"<p>{escape_value(showcase.area_note)}</p>"
        f"<p>{escape_value(showcase.style_note)}</p>"
        f'<p class="note">{escape_value(showcase.disclosure)}</p>'
    )


def _highlights_html(showcase: Showcase.VillaShowcase) -> Markup:
    """Factual bullets (units, bedrooms, capacity) built from stored values only."""
    items = "".join(f"<li>{escape_value(line)}</li>" for line in showcase.highlights)
    return Markup(f'<ul class="highlights">{items}</ul>')


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
        trusted_proxy: bool = False,
    ) -> None:
        self.conn = conn
        self.agency_id = agency_id or resolve_agency_id()
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.templates_dir = Path(templates_dir) if templates_dir else DEFAULT_TEMPLATES_DIR
        #: When true, the reverse proxy's forwarded address is the rate-limit
        #: key and the logged client. Off by default: a header a visitor can
        #: send must never be trusted just because a proxy might be in front.
        self.trusted_proxy = bool(trusted_proxy)
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
            "/owners": "owners.html",
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
        hero_uri = _photo_data_uri(HERO_PHOTO_FILE)
        hero_style = (
            Markup(
                "<style>.hero-band{background-image:"
                "linear-gradient(165deg,rgba(13,44,34,.68) 0%,rgba(18,58,45,.62) 46%,rgba(14,46,63,.70) 100%),"
                f"url('{hero_uri}');background-size:cover;background-position:center}}</style>"
            )
            if hero_uri
            else Markup("")
        )
        return self._page(
            "home",
            {
                "page_title": "Villa Agency — Private Villas in Bali & Yogyakarta",
                "villa_cards": Markup(cards),
                "area_map": _area_map_html(villas),
                "head_extra": hero_style,
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
                "area_map": _area_map_html(villas),
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
                "area_map": _area_map_html(villas),
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
        availability_hint = self.read_model.availability_hint(property_id, to_iso(self.clock.now()))
        showcase = Showcase.showcase_for(villa, units)
        photo_uri = _villa_photo_uri(str(villa["name"]), size="detail")
        if photo_uri:
            villa_photo = Markup(
                f'<div class="photo detail" style="background-image:url(\'{photo_uri}\')" '
                f'role="img" aria-label="{escape_value(villa["name"])} — illustrative photo"></div>'
            )
        else:
            villa_photo = Markup('<div class="photo detail placeholder"></div>')
        return self._page(
            "villa_detail",
            {
                "page_title": f"{villa['name']} — Villa Agency",
                "villa_name": escape_value(villa["name"]),
                "villa_meta": escape_value(_places_line(villa, str(villa["property_type"]).replace("_", " "))),
                "villa_photo": villa_photo,
                "villa_story": _villa_story_html(showcase),
                "villa_highlights": _highlights_html(showcase),
                "villa_gallery": _gallery_html(),
                "units_table": _units_table(units),
                "villa_locator": _locator_html(villa),
                "property_ref": escape_value(property_id),
                "availability_hint": escape_value(availability_hint),
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

    def owners_html(self, base: str = "") -> str:
        return self._page(
            "owners",
            {
                "page_title": "For Villa Owners — Villa Agency",
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
        written = [out / "inquiry.html", out / "owners.html"]
        (out / "inquiry.html").write_text(
            self.inquiry_form_html(base="."), encoding="utf-8"
        )
        (out / "owners.html").write_text(self.owners_html(base="."), encoding="utf-8")
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


class _MarketingRequestHandler(SafeHeaderMixin, BaseHTTPRequestHandler):
    """Public request handler: no auth, no sessions, read model + one form."""

    server_version = "VillaMarketing/1.0"
    #: Do not advertise the interpreter patch level: it tells an attacker which
    #: stdlib CVEs still apply (the dashboard already blanks this).
    sys_version = ""
    #: Bounded read: ``StreamRequestHandler.setup`` applies this to the socket,
    #: so a half-sent request dies instead of holding the single serving thread.
    timeout = CONNECTION_TIMEOUT_SECONDS
    site: MarketingSite
    #: Set as soon as a response is committed, so the catch-all guard never
    #: tries to prepend a 500 to a body that is already in flight.
    _response_started = False

    # -- helpers ------------------------------------------------------------ #
    def _send_html(
        self,
        html: str,
        status: int = 200,
        *,
        no_store: bool = False,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        payload = html.encode("utf-8")
        cache = "no-store" if no_store else "public, max-age=300"
        self._response_started = True
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
        # Names and values go through SafeHeaderMixin like every other header: a
        # caller cannot smuggle a second header through this door either.
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _error(
        self, status: int, message: str, *, retry_after: Optional[int] = None
    ) -> None:
        extra = {"Retry-After": str(int(retry_after))} if retry_after is not None else None
        self._send_html(
            self.site.error_html(status, message), status, no_store=True, extra_headers=extra
        )

    def _remote_addr(self) -> str:
        """The address that identifies this visitor to the rate limiter.

        Behind a proxy the socket peer is the proxy for *every* guest, which
        would collapse the site onto a single bucket; then, and only then, the
        address our own proxy wrote is used instead.
        """
        peer = str(self.client_address[0])
        headers = getattr(self, "headers", None)
        if not self.site.trusted_proxy or headers is None:
            # ``headers`` is None when the request line never parsed, and this
            # method also runs from ``log_message`` — a junk request must still
            # produce a 400 and a log line, not an exception in the log path.
            return peer
        return client_address_from_forwarded_for(
            headers.get(FORWARDED_FOR_HEADER), peer=peer
        )

    # -- GET ---------------------------------------------------------------- #
    def _fail_safe(self, exc: BaseException) -> None:
        """Answer with a 500 page instead of dropping the connection.

        An uncaught exception inside ``http.server`` closes the socket with an
        empty reply: the visitor sees a network error, a health check still sees
        a running process, and the access log stays silent. The traceback goes
        to the journal (never to the visitor), and the guest gets a plain page.
        """
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        if self._response_started:
            return  # headers already on the wire: nothing safe left to send
        try:
            self._error(500, "Something went wrong on our side; please try again shortly.")
        except Exception:  # noqa: BLE001 - the socket may already be gone
            traceback.print_exc()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        try:
            self._dispatch_get()
        except (BrokenPipeError, ConnectionResetError):
            raise  # client vanished mid-response: not a rendering fault
        except Exception as exc:  # noqa: BLE001 - a public page never drops the socket
            self._fail_safe(exc)

    def _dispatch_get(self) -> None:
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
        elif path == "/owners":
            self._send_html(site.owners_html())
        elif path == "/inquiry":
            query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
            property_ref = dict(query).get("property", "").strip()
            if property_ref:
                try:
                    require_ref(property_ref, "property")
                except DomainError:
                    property_ref = ""
            self._send_html(site.inquiry_form_html(property_ref), no_store=True)
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
        try:
            self._dispatch_post()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 - a form post never drops the socket
            self._fail_safe(exc)

    def _dispatch_post(self) -> None:
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
                remote_addr=self._remote_addr(),
                rate_limiter=self.site.rate_limiter,
                expected_origin_host=self.headers.get("Host"),
                origin_header=self.headers.get("Origin"),
                country_hint=self.headers.get("CF-IPCountry"),
            )
        except RateLimited as exc:
            self._error(429, str(exc.message), retry_after=RATE_LIMIT_WINDOW_SECONDS)
            return
        except (FormInvalid, SpamRejected) as exc:
            self._error(400, str(exc.message))
            return
        except DomainError:  # kernel rejection: never leak internals
            self._error(500, "Something went wrong on our side; please try again shortly.")
            return
        self._send_html(self.site.inquiry_success_html(result), no_store=True)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        sys.stderr.write("%s - %s\n" % (self._remote_addr(), format % args))


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
    parser.add_argument(
        "--trusted-proxy",
        action="store_true",
        help="trust the reverse proxy's X-Forwarded-For (the proxy must overwrite it)",
    )
    parser.add_argument("--dump", default=None, help="write static pages and exit")
    args = parser.parse_args(argv)
    if args.trusted_proxy and args.allow_remote:
        parser.error(
            "--trusted-proxy cannot be combined with --allow-remote: a socket that"
            " accepts direct connections cannot tell a proxy apart from a forger"
        )

    conn = connect_sqlite(args.db)
    site = MarketingSite(conn, agency_id=args.agency, trusted_proxy=args.trusted_proxy)
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

"""Deterministic, identifier-free document issuance on top of ``documents``.

Kinds
-----
The schema's ``documents.kind`` vocabulary is used verbatim (:data:`DOCUMENT_KINDS`).
"Surat penawaran" (the offer letter) is issued as ``CONTRACT``: the offer is the
contractual document that precedes a stay and the vocabulary carries no OFFER
value.

Design rules enforced here
--------------------------
* **One version per issue.** ``(document_key, version)`` is unique, so a second
  :meth:`DocumentService.generate` for the same key writes version 2 and flips
  version 1 to ``SUPERSEDED`` — a document is never overwritten, and its file is
  never truncated.
* **Byte determinism.** The body is a pure function of (kind, key, subject,
  locale, instant, actor) plus the referenced rows. ``document_id`` and the
  version are deliberately *not* rendered: two issues with the same instant
  produce identical bytes and an identical ``body_sha256``.
* **Fail closed before any write.** Every validation — path safety, role,
  reference existence, invoice/booking reconciliation, identifier scan — runs
  before the first byte reaches disk, so a refusal leaves neither a row nor a
  file behind.
* **P1 only.** Each body is scrubbed with :func:`primitives.redact` and re-checked
  against :data:`gates.IDENTIFIER_PATTERNS`; a body that still carries a direct
  identifier is refused rather than stored. Documents are therefore declared
  ``P1`` (operational, non-personal) per ``docs/DATA_CLASSIFICATION_AND_ACCESS.md``
  §1 — which is exactly what lets them reach the ``EXPORT``/``EMAIL`` destinations.
* **Safe storage.** ``storage_ref`` is derived from the validated ``document_key``
  and every path is re-checked to sit inside the service root
  (:func:`resolve_storage_path`), so a hostile key cannot escape it.
* **HTML without script.** Simple semantic HTML with escaped text: no JavaScript,
  no ``iframe``, no inline handler attributes. A plain-text export is available
  through :func:`plain_text` and :meth:`DocumentService.text`.
"""

from __future__ import annotations

import html
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, Optional, Sequence

from gates import IDENTIFIER_PATTERNS
from invoicing import derive_totals
from primitives import (
    MINOR_UNITS,
    DomainError,
    EntityNotFound,
    IdFactory,
    Money,
    SystemClock,
    parse_iso,
    redact,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    # vocabulary
    "DOCUMENT_KINDS",
    "DOCUMENT_FORMATS",
    "DOCUMENT_STATUSES",
    "DOCUMENT_COLUMNS",
    "DOCUMENT_FORMAT",
    "DOCUMENT_CONTENT_CLASS",
    "REDACTION_PROFILE",
    "DOCUMENT_ROOT_NAME",
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "MIN_DOCUMENT_KEY_LENGTH",
    "SUBJECT_FIELDS",
    "DOCUMENT_ROLE_POLICY",
    "VOID_ROLES",
    "CONFIRMATION_BOOKING_STATUSES",
    "CONTRACT_BLOCKED_BOOKING_STATUSES",
    "OFFER_VALIDITY_HOURS",
    # entry points
    "DocumentService",
    "Document",
    "VerificationReport",
    "default_root",
    "resolve_storage_path",
    "storage_ref_for",
    "format_amount",
    "format_date",
    "plain_text",
    "assert_identifier_free",
    # error / issue codes
    "UNSAFE_DOCUMENT_KEY",
    "INVALID_DOCUMENT_REQUEST",
    "UNKNOWN_DOCUMENT_KIND",
    "UNKNOWN_DOCUMENT_STATUS",
    "UNSUPPORTED_LOCALE",
    "DOCUMENT_ROLE_FORBIDDEN",
    "DOCUMENT_NOT_FOUND",
    "DOCUMENT_NOT_ACTIVE",
    "DOCUMENT_SOURCE_MISSING",
    "DOCUMENT_SOURCE_INVALID",
    "DOCUMENT_SOURCE_MISMATCH",
    "INVOICE_SOURCE_MISMATCH",
    "DOCUMENT_IDENTIFIER_LEAK",
    "UNKNOWN_SUBJECT_FIELD",
    "DOCUMENT_STORAGE_INVALID",
    "ISSUE_OK",
    "ISSUE_FILE_MISSING",
    "ISSUE_HASH_MISMATCH",
    "ISSUE_UNSAFE_STORAGE_REF",
    "ISSUE_MISSING_STORAGE_REF",
    "ISSUE_FILE_UNREADABLE",
]

# --------------------------------------------------------------------------- #
# Vocabulary (mirrors the schema CHECK constraints)
# --------------------------------------------------------------------------- #
DOCUMENT_KINDS: tuple[str, ...] = (
    "CONFIRMATION",
    "RECEIPT",
    "INVOICE",
    "CONTRACT",
    "NOTICE",
    "REPORT",
)
DOCUMENT_FORMATS: tuple[str, ...] = ("HTML", "TEXT", "MARKDOWN")
DOCUMENT_STATUSES: tuple[str, ...] = ("GENERATED", "SUPERSEDED", "VOID")

DOCUMENT_COLUMNS: tuple[str, ...] = (
    "document_id",
    "agency_id",
    "document_key",
    "kind",
    "version",
    "format",
    "booking_id",
    "invoice_id",
    "case_id",
    "content_class",
    "redaction_profile",
    "storage_ref",
    "body_sha256",
    "status",
    "generated_by",
    "generated_at",
    "created_at",
)

#: Documents are always HTML; the schema also allows TEXT/MARKDOWN, which this
#: service reaches through the plain-text export instead of a second renderer.
DOCUMENT_FORMAT = "HTML"

#: Identifier-free operational output — see the module docstring.
DOCUMENT_CONTENT_CLASS = "P1"
REDACTION_PROFILE = "redact:v1"
DOCUMENT_ROOT_NAME = "documents"
STORAGE_EXTENSION = "html"

DEFAULT_LOCALE = "id-ID"
SUPPORTED_LOCALES: tuple[str, ...] = ("id-ID", "en-US")

MIN_DOCUMENT_KEY_LENGTH = 6

#: Hours an offer letter claims to stay valid (the pricing itself lives in the
#: quote/policy rows; this is the wording of the letter).
OFFER_VALIDITY_HOURS = 48

#: A booking in any of these states may be confirmed to the guest.
CONFIRMATION_BOOKING_STATUSES: tuple[str, ...] = (
    "CONFIRMED",
    "DEPOSIT_PAID",
    "PAID_IN_FULL",
    "BALANCE_DUE",
    "PRE_ARRIVAL",
    "IN_STAY",
)

#: States in which writing an offer letter would be a lie.
CONTRACT_BLOCKED_BOOKING_STATUSES: tuple[str, ...] = (
    "CANCELLED",
    "CLOSED",
    "NO_SHOW",
    "REFUNDED",
    "AVAILABILITY_UNAVAILABLE",
)

#: Who may issue what. Finance output is finance/manager/admin only (the data
#: classification matrix keeps financial access away from general agents).
DOCUMENT_ROLE_POLICY: dict[str, tuple[str, ...]] = {
    "CONFIRMATION": ("system", "agent", "manager", "admin"),
    "RECEIPT": ("system", "finance", "manager", "admin"),
    "INVOICE": ("system", "finance", "admin"),
    "CONTRACT": ("system", "manager", "admin"),
    "NOTICE": ("system", "manager", "admin"),
    "REPORT": ("system", "manager", "admin", "finance"),
}

#: Voiding is a management act: it retires an issued, hashed artefact.
VOID_ROLES: tuple[str, ...] = ("manager", "admin")

#: The only subject keys a caller may pass. Anything else is refused (so an
#: unvetted ``guest_email``/``guest_name`` field can never ride into the body).
SUBJECT_FIELDS: tuple[str, ...] = (
    "booking_id",
    "invoice_id",
    "case_id",
    "recipient_ref",
    "contact_email",
    "contact_phone",
    "title",
    "notes",
    "currency",
    "total_minor",
    "invoice_number",
)

# --------------------------------------------------------------------------- #
# Error codes
# --------------------------------------------------------------------------- #
UNSAFE_DOCUMENT_KEY = "UNSAFE_DOCUMENT_KEY"
INVALID_DOCUMENT_REQUEST = "INVALID_DOCUMENT_REQUEST"
UNKNOWN_DOCUMENT_KIND = "UNKNOWN_DOCUMENT_KIND"
UNKNOWN_DOCUMENT_STATUS = "UNKNOWN_DOCUMENT_STATUS"
UNSUPPORTED_LOCALE = "UNSUPPORTED_LOCALE"
DOCUMENT_ROLE_FORBIDDEN = "DOCUMENT_ROLE_FORBIDDEN"
DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
DOCUMENT_NOT_ACTIVE = "DOCUMENT_NOT_ACTIVE"
DOCUMENT_SOURCE_MISSING = "DOCUMENT_SOURCE_MISSING"
DOCUMENT_SOURCE_INVALID = "DOCUMENT_SOURCE_INVALID"
DOCUMENT_SOURCE_MISMATCH = "DOCUMENT_SOURCE_MISMATCH"
INVOICE_SOURCE_MISMATCH = "INVOICE_SOURCE_MISMATCH"
DOCUMENT_IDENTIFIER_LEAK = "DOCUMENT_IDENTIFIER_LEAK"
UNKNOWN_SUBJECT_FIELD = "UNKNOWN_SUBJECT_FIELD"
DOCUMENT_STORAGE_INVALID = "DOCUMENT_STORAGE_INVALID"

#: ``verify`` issue vocabulary.
ISSUE_OK = "OK"
ISSUE_FILE_MISSING = "FILE_MISSING"
ISSUE_HASH_MISMATCH = "HASH_MISMATCH"
ISSUE_UNSAFE_STORAGE_REF = "UNSAFE_STORAGE_REF"
ISSUE_MISSING_STORAGE_REF = "MISSING_STORAGE_REF"
ISSUE_FILE_UNREADABLE = "FILE_UNREADABLE"

# --------------------------------------------------------------------------- #
# Localisation (small, closed tables — no ``locale`` module, no floats)
# --------------------------------------------------------------------------- #
_MONTHS: dict[str, tuple[str, ...]] = {
    "id-ID": (
        "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
        "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
    ),
    "en-US": (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ),
}
_THOUSANDS: dict[str, str] = {"id-ID": ".", "en-US": ","}
_DECIMALS: dict[str, str] = {"id-ID": ",", "en-US": "."}

_KIND_TITLES: dict[str, dict[str, str]] = {
    "id-ID": {
        "CONFIRMATION": "Konfirmasi Pemesanan",
        "RECEIPT": "Resi Pembayaran",
        "INVOICE": "Invoice",
        "CONTRACT": "Surat Penawaran",
        "NOTICE": "Pemberitahuan",
        "REPORT": "Laporan",
    },
    "en-US": {
        "CONFIRMATION": "Booking Confirmation",
        "RECEIPT": "Payment Receipt",
        "INVOICE": "Invoice",
        "CONTRACT": "Offer Letter",
        "NOTICE": "Notice",
        "REPORT": "Report",
    },
}

_LABELS: dict[str, str] = {
    "booking_id": "Kode booking",
    "invoice_number": "Nomor invoice",
    "invoice_status": "Status invoice",
    "case_id": "Kode kasus",
    "case_status": "Status kasus",
    "channel": "Kanal",
    "unit_id": "Unit",
    "property_id": "Properti",
    "check_in": "Check-in",
    "check_out": "Check-out",
    "check_in_local": "Check-in (lokal)",
    "check_out_local": "Check-out (lokal)",
    "nights": "Malam",
    "adults": "Dewasa",
    "children": "Anak",
    "infants": "Bayi",
    "guests": "Jumlah tamu",
    "status": "Status booking",
    "currency": "Mata uang",
    "total": "Total",
    "subtotal": "Subtotal",
    "tax": "Pajak",
    "service": "Service",
    "issued_at": "Diterbitkan",
    "due_at": "Jatuh tempo",
    "paid": "Sudah dibayar",
    "outstanding": "Sisa tagihan",
    "payment_status": "Status pembayaran",
    "validity": "Berlaku",
    "recipient": "Penerima",
    "timestamp": "Stempel waktu",
    "timestamp_local": "Stempel waktu (lokal)",
    "locale": "Locale",
    "document_key": "Kunci dokumen",
    "generated_by": "Diterbitkan oleh",
    "classification": "Kelas data",
    "offer_total": "Penawaran total",
}

# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
#: Filename-safe opaque token: lowercase, dots allowed (``booking.book_1``),
#: no separators, no ``..`` (checked separately so the error code is precise).
_DOCUMENT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{5,127}$")
_TRAVERSAL_MARKERS = ("..", "/", "\\")


def _safe_document_key(value: Any) -> str:
    """Return ``value`` as a validated, filename-safe document key.

    Traversal (``../``, ``..``, a separator, a Windows path) and absolute keys
    are refused *before* the shape check so the caller always gets
    :data:`UNSAFE_DOCUMENT_KEY` for the dangerous cases.
    """
    text = require_text(value, "document_key", max_len=128)
    for marker in _TRAVERSAL_MARKERS:
        if marker in text:
            raise DomainError(
                "document_key must not contain a path separator or '..'",
                code=UNSAFE_DOCUMENT_KEY,
                field="document_key",
            )
    if os.path.isabs(text) or (len(text) > 1 and text[1] == ":"):
        raise DomainError(
            "document_key must be a relative, filename-safe token",
            code=UNSAFE_DOCUMENT_KEY,
            field="document_key",
        )
    if len(text) < MIN_DOCUMENT_KEY_LENGTH or not _DOCUMENT_KEY_RE.match(text):
        raise DomainError(
            "document_key must be a lowercase ref token of at least "
            f"{MIN_DOCUMENT_KEY_LENGTH} characters ([a-z0-9._-])",
            code=INVALID_DOCUMENT_REQUEST,
            field="document_key",
        )
    return text


def resolve_storage_path(root: Any, storage_ref: Any) -> Path:
    """Resolve ``storage_ref`` under ``root``, refusing anything that escapes it.

    This is the single place where a ``storage_ref`` (from a caller *or* from the
    database) becomes a filesystem path, so a tampered row cannot point the
    reader at ``/etc/passwd``.
    """
    ref = require_text(storage_ref, "storage_ref", max_len=255)
    if "\\" in ref or ":" in ref or ref.startswith("/"):
        raise DomainError(
            "storage_ref must be a relative POSIX path",
            code=DOCUMENT_STORAGE_INVALID,
            field="storage_ref",
        )
    parts = ref.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise DomainError(
            "storage_ref must not contain empty, '.' or '..' segments",
            code=DOCUMENT_STORAGE_INVALID,
            field="storage_ref",
        )
    base = Path(root)
    target = base.joinpath(*parts)
    root_real = os.path.realpath(base)
    target_real = os.path.realpath(target)
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise DomainError(
            "storage_ref escapes the document root",
            code=DOCUMENT_STORAGE_INVALID,
            field="storage_ref",
        )
    return target


def storage_ref_for(kind: str, document_key: str, version: int) -> str:
    """The relative storage path of one document version."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DomainError(
            "version must be a positive integer",
            code=INVALID_DOCUMENT_REQUEST,
            field="version",
        )
    return f"{kind.lower()}/{document_key}/v{version:04d}.{STORAGE_EXTENSION}"


def default_root() -> Path:
    """``<repo>/documents`` — the root used when none is passed."""
    return Path(__file__).resolve().parents[1] / DOCUMENT_ROOT_NAME


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _group_digits(digits: str, separator: str) -> str:
    if len(digits) <= 3:
        return digits
    chunks: list[str] = []
    rest = digits
    while len(rest) > 3:
        chunks.insert(0, rest[-3:])
        rest = rest[:-3]
    chunks.insert(0, rest)
    return separator.join(chunks)


def format_amount(money: Money, locale: str = DEFAULT_LOCALE) -> str:
    """``IDR 1.500.000`` / ``USD 1,234.50`` — grouped, never one long digit run.

    Grouping is not cosmetic: :func:`primitives.redact` treats any bare run of
    nine or more digits as an identifier, so a grouped amount is the only form of
    a large total that may legitimately appear in a document body.
    """
    _require_locale(locale)
    raw = money.to_major_str()
    sign = "-" if raw.startswith("-") else ""
    unsigned = raw[1:] if sign else raw
    whole, _, fraction = unsigned.partition(".")
    text = f"{money.currency} {sign}{_group_digits(whole, _THOUSANDS[locale])}"
    if fraction:
        text += _DECIMALS[locale] + fraction
    return text


def format_date(value: str, locale: str = DEFAULT_LOCALE) -> str:
    """``2026-10-01`` -> ``01 Okt 2026`` (``01 Oct 2026`` in ``en-US``)."""
    _require_locale(locale)
    text = require_text(value, "date", max_len=32)
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:  # pragma: no cover - defensive: DB CHECK guarantees shape
        raise DomainError(
            "date must be an ISO date", code=INVALID_DOCUMENT_REQUEST, field="date"
        ) from exc
    return f"{parsed.day:02d} {_MONTHS[locale][parsed.month - 1]} {parsed.year}"


def _require_locale(value: Any) -> str:
    text = require_text(value, "locale", max_len=16)
    if text not in SUPPORTED_LOCALES:
        raise DomainError(
            "locale is not supported", code=UNSUPPORTED_LOCALE, field="locale"
        )
    return text


def _require_currency(value: Any, *, field: str = "currency") -> str:
    text = require_text(value, field, max_len=3).upper()
    if len(text) != 3 or text not in MINOR_UNITS:
        raise DomainError(
            "currency must be a known ISO-4217 code", code=INVALID_DOCUMENT_REQUEST, field=field
        )
    return text


def _require_minor(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError(
            f"{field} must be a non-negative integer minor amount",
            code=INVALID_DOCUMENT_REQUEST,
            field=field,
        )
    return value


def _normalise_instant(value: Any, *, field: str) -> str:
    """Accept a datetime or an ISO string, store the canonical UTC form."""
    if value is None:
        raise DomainError(f"{field} is required", code="MISSING_FIELD", field=field)
    if hasattr(value, "tzinfo") and not isinstance(value, str):
        return to_iso(value)  # rejects naive datetimes (NAIVE_TIMESTAMP)
    return to_iso(parse_iso(require_text(value, field, max_len=40)))


# --------------------------------------------------------------------------- #
# Text sanitation
# --------------------------------------------------------------------------- #
#: URI schemes that must never survive into a body; deliberately narrow so
#: ordinary prose ("data: 10 kamar") is untouched.
_UNSAFE_SCHEME_RE = re.compile(
    r"(?i)(?:javascript|vbscript)\s*:|\bdata\s*:\s*(?:text/html|image/svg\+xml)"
)
_SCRIPT_TOKEN_RE = re.compile(r"(?i)</?script")


def _strip_unsafe_schemes(text: str) -> str:
    return _UNSAFE_SCHEME_RE.sub("", text)


def _clean_text(value: Any, field: str, *, max_len: int) -> str:
    """Scrub one caller-supplied string into HTML-safe, identifier-free text.

    Order matters: the dangerous scheme goes first (escaping would not remove it),
    then :func:`primitives.redact`, then ``html.escape`` so the value can be
    interpolated into markup as content and nothing else.
    """
    text = require_text(value, field, max_len=max_len)
    return html.escape(redact(_strip_unsafe_schemes(text)))


def _clean_contact(value: Any, field: str, placeholder: str) -> str:
    """Redact a declared contact field, refusing a value that is *not* one.

    A contact field whose value :func:`primitives.redact` leaves untouched is
    either already masked or not the identifier it claims to be; storing it
    verbatim would put unclassified text in a P1 artefact, so it is refused.
    """
    text = require_text(value, field, max_len=200)
    masked = redact(_strip_unsafe_schemes(text))
    if masked == text and text != placeholder:
        raise DomainError(
            f"{field} must be a redactable direct identifier or {placeholder}",
            code=INVALID_DOCUMENT_REQUEST,
            field=field,
        )
    return html.escape(masked)


def assert_identifier_free(text: str, *, field: str = "body") -> str:
    """Refuse ``text`` when it still carries a direct identifier.

    Two detectors on purpose: :func:`primitives.redact` (the repo's definition,
    which is what ``audit.contains_direct_identifiers`` compares against) and the
    wider :data:`gates.IDENTIFIER_PATTERNS`, which also catches
    space/dash-separated card-like runs that ``redact`` lets through.
    """
    if redact(text) != text:
        raise DomainError(
            "document body still carries a direct identifier after redaction",
            code=DOCUMENT_IDENTIFIER_LEAK,
            field=field,
        )
    for name, pattern in IDENTIFIER_PATTERNS:
        if pattern.search(text):
            raise DomainError(
                "document body carries an identifier-shaped token",
                code=DOCUMENT_IDENTIFIER_LEAK,
                field=field,
                pattern=name,
            )
    return text


# --------------------------------------------------------------------------- #
# Plain-text export
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    """Collect visible text from the generated markup, one block per line."""

    _BLOCK = frozenset(
        {
            "article", "body", "div", "dl", "dd", "dt", "footer", "h1", "h2", "h3",
            "header", "head", "li", "p", "section", "table", "tbody", "td", "th",
            "thead", "title", "tr", "ul",
        }
    )
    _SKIP = frozenset({"script", "style", "iframe"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        if tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        if tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def plain_text(markup: str) -> str:
    """Deterministic plain-text export of a rendered body.

    The extracted text is sanitised the same way the markup is: no ``<script``
    token and no dangerous URI scheme survives, even though the export is inert.
    """
    if not isinstance(markup, str):
        raise DomainError(
            "markup must be a string", code=INVALID_DOCUMENT_REQUEST, field="markup"
        )
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    lines: list[str] = []
    for raw in extractor.text.split("\n"):
        collapsed = re.sub(r"\s+", " ", raw).strip()
        if collapsed:
            lines.append(_SCRIPT_TOKEN_RE.sub("", _strip_unsafe_schemes(collapsed)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _contact_rows(fields: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    if fields.get("contact_email"):
        out.append(("Email", str(fields["contact_email"])))
    if fields.get("contact_phone"):
        out.append(("Telepon", str(fields["contact_phone"])))
    return tuple(out)


@dataclass(frozen=True)
class _Facts:
    """Everything a body needs, already scrubbed and ordered."""

    rows: tuple[tuple[str, str], ...]
    lines: tuple[tuple[str, str, str, str], ...] = ()
    notes: str = ""
    recipient: str = ""
    contacts: tuple[tuple[str, str], ...] = ()


def _definition_rows(labels: Sequence[tuple[str, str]]) -> list[str]:
    out = ["<dl>"]
    for label, value in labels:
        out.append(f"<dt>{html.escape(label)}</dt>")
        out.append(f"<dd>{value}</dd>")
    out.append("</dl>")
    return out


def _line_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["<table>", "<thead>", "<tr>"]
    out.extend(f"<th>{html.escape(cell)}</th>" for cell in header)
    out.extend(["</tr>", "</thead>", "<tbody>"])
    for row in rows:
        out.append("<tr>")
        out.extend(f"<td>{cell}</td>" for cell in row)
        out.append("</tr>")
    out.extend(["</tbody>", "</table>"])
    return out


def _render_body(
    *,
    kind: str,
    document_key: str,
    locale: str,
    generated_at: str,
    generated_by: str,
    title: str,
    facts: _Facts,
) -> str:
    """Pure renderer: identical inputs produce identical bytes, always.

    ``document_id`` and ``version`` are intentionally absent, so re-issuing the
    same content at the same instant is byte-identical.
    """
    parts: list[str] = [
        "<!DOCTYPE html>",
        f'<html lang="{html.escape(locale)}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{title}</title>",
        "</head>",
        "<body>",
        f'<article class="document" data-kind="{html.escape(kind)}">',
        "<header>",
        f"<h1>{title}</h1>",
    ]
    parts.extend(
        _definition_rows(
            (
                (_LABELS["document_key"], document_key),
                (_LABELS["timestamp"], generated_at),
                (_LABELS["timestamp_local"], format_date(generated_at, locale)),
                (_LABELS["locale"], locale),
                (_LABELS["generated_by"], generated_by),
                (_LABELS["classification"], DOCUMENT_CONTENT_CLASS),
            )
        )
    )
    parts.append("</header>")
    parts.append("<main>")
    if facts.recipient:
        parts.append(f'<p class="recipient">{facts.recipient}</p>')
    for label, value in facts.contacts:
        parts.append(f'<p class="contact">{html.escape(label)}: {value}</p>')
    parts.extend(_definition_rows(facts.rows))
    if facts.lines:
        parts.extend(
            _line_table(
                ("Deskripsi", "Qty", "Harga satuan", "Jumlah"),
                facts.lines,
            )
        )
    if facts.notes:
        parts.append('<section class="notes">')
        parts.append("<h2>Catatan</h2>")
        parts.append(f"<p>{facts.notes}</p>")
        parts.append("</section>")
    parts.append("</main>")
    parts.append(
        "<footer><p>Dokumen ini dihasilkan otomatis oleh Villa Agency OS. "
        "Tidak memuat identitas langsung.</p></footer>"
    )
    parts.append("</article>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Document:
    """One ``documents`` row."""

    document_id: str
    agency_id: str
    document_key: str
    kind: str
    version: int
    format: str
    booking_id: Optional[str]
    invoice_id: Optional[str]
    case_id: Optional[str]
    content_class: str
    redaction_profile: str
    storage_ref: Optional[str]
    body_sha256: str
    status: str
    generated_by: str
    generated_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Document":
        return cls(**{name: row[name] for name in DOCUMENT_COLUMNS})

    @property
    def is_active(self) -> bool:
        return self.status == "GENERATED"

    @property
    def subject_ref(self) -> Optional[str]:
        return self.booking_id or self.invoice_id or self.case_id

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in DOCUMENT_COLUMNS}


@dataclass(frozen=True)
class VerificationReport:
    """Result of re-hashing a document against the file on disk."""

    document_id: str
    document_key: str
    version: int
    status: str
    content_class: str
    storage_ref: Optional[str]
    path: Optional[str]
    expected_sha256: str
    actual_sha256: Optional[str]
    ok: bool
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_key": self.document_key,
            "version": self.version,
            "status": self.status,
            "content_class": self.content_class,
            "storage_ref": self.storage_ref,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "ok": self.ok,
            "issues": list(self.issues),
        }


# --------------------------------------------------------------------------- #
# Small shared helpers (the repo keeps one private copy per service module)
# --------------------------------------------------------------------------- #
@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Run the body in an IMMEDIATE transaction, or join the caller's.

    IMMEDIATE takes the write lock before rows are read, so two concurrent issues
    of the same key cannot both read "latest version is 1" and both claim version
    2. When a caller already opened a transaction we neither nest a ``BEGIN`` nor
    commit their work.
    """
    if getattr(conn, "in_transaction", False):
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover - defensive: nothing to clean up
        pass


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class DocumentService:
    """Issue, retire, list and verify deterministic documents."""

    def __init__(
        self,
        conn: Any,
        clock: Optional[Any] = None,
        ids: Optional[Any] = None,
        root: Optional[Any] = None,
        agency_id: str = "agency_default",
    ) -> None:
        self._conn = conn
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else IdFactory(deterministic=False)
        self._root = Path(root) if root is not None else default_root()
        self.agency_id = require_ref(agency_id, "agency_id")
        self._ensure_agency_scope()

    def _ensure_agency_scope(self) -> None:
        now = _normalise_instant(self._clock.now(), field="now")
        self._conn.execute(
            """
            INSERT INTO agencies (agency_id, name, status, created_at, updated_at)
            VALUES (?, ?, 'ACTIVE', ?, ?)
            ON CONFLICT(agency_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (self.agency_id, self.agency_id, now, now),
        )

    # -- accessors -------------------------------------------------------
    @property
    def conn(self) -> Any:
        return self._conn

    @property
    def clock(self) -> Any:
        return self._clock

    @property
    def ids(self) -> Any:
        return self._ids

    @property
    def root(self) -> Path:
        return self._root

    # -- issuance --------------------------------------------------------
    def generate(
        self,
        *,
        kind: Any,
        document_key: Any,
        subject: Any,
        locale: str = DEFAULT_LOCALE,
        at: Any = None,
        generated_by: Any,
        actor_role: Any,
    ) -> Document:
        """Issue one document version: a ``documents`` row plus a file on disk.

        Every validation runs before the first write. On success the previous
        ``GENERATED`` version of ``document_key`` becomes ``SUPERSEDED`` and the
        new row is ``GENERATED`` with ``version`` one higher.
        """
        document_kind = _require_kind(kind)
        key = _safe_document_key(document_key)
        request_locale = _require_locale(locale)
        issued_at = _normalise_instant(
            at if at is not None else self._clock.now(), field="at"
        )
        actor = require_ref(generated_by, "generated_by", max_len=64)
        # Validation call for its effect: an unknown role must fail here.
        _require_role(actor_role, document_kind)
        fields = _require_subject(subject)
        facts, references = self._collect(document_kind, fields, request_locale)
        facts = replace(
            facts,
            recipient=html.escape(str(fields.get("recipient_ref") or "")),
            contacts=_contact_rows(fields),
        )
        body = _render_body(
            kind=document_kind,
            document_key=key,
            locale=request_locale,
            generated_at=issued_at,
            generated_by=actor,
            title=_title(document_kind, request_locale, fields),
            facts=facts,
        )
        assert_identifier_free(body, field="body")
        digest = sha256_hex(body)
        document_id = require_ref(self._ids.new("doc"), "document_id", max_len=64)

        path: Optional[Path] = None
        try:
            with _write_transaction(self._conn):
                version = self._next_version(key)
                storage_ref = storage_ref_for(document_kind, key, version)
                path = resolve_storage_path(self._root, storage_ref)
                self._supersede(key)
                self._conn.execute(
                    "INSERT INTO documents (document_id, agency_id, document_key, kind, version,"
                    " format, booking_id, invoice_id, case_id, content_class,"
                    " redaction_profile, storage_ref, body_sha256, status, generated_by,"
                    " generated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, 'GENERATED', ?, ?, ?)",
                    (
                        document_id,
                        self.agency_id,
                        key,
                        document_kind,
                        version,
                        DOCUMENT_FORMAT,
                        references.get("booking_id"),
                        references.get("invoice_id"),
                        references.get("case_id"),
                        DOCUMENT_CONTENT_CLASS,
                        REDACTION_PROFILE,
                        storage_ref,
                        digest,
                        actor,
                        issued_at,
                        issued_at,
                    ),
                )
                self._write_body(path, body)
        except BaseException:
            if path is not None:
                _unlink_quietly(path)
            raise
        return self.get(document_id)

    def void(
        self,
        document_id: Any,
        *,
        reason: Any,
        at: Any = None,
        actor: Any,
        actor_role: Any,
    ) -> Document:
        """Retire a ``GENERATED`` version (``GENERATED`` -> ``VOID``).

        The file stays where it is: a voided artefact is still an auditable one.
        """
        document = self.get(document_id)
        _require_role(actor_role, document.kind, field="actor_role", roles=VOID_ROLES)
        require_ref(actor, "actor", max_len=64)
        text = require_text(reason, "reason", max_len=200)
        if len(text) < 3:
            raise DomainError(
                "reason must be at least 3 characters",
                code=INVALID_DOCUMENT_REQUEST,
                field="reason",
            )
        if not document.is_active:
            raise DomainError(
                f"document {document.document_id!r} is {document.status}, not GENERATED",
                code=DOCUMENT_NOT_ACTIVE,
                document_id=document.document_id,
                status=document.status,
            )
        if at is not None:
            _normalise_instant(at, field="at")
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE documents SET status = 'VOID' WHERE document_id = ?"
                " AND agency_id = ? AND status = 'GENERATED'",
                (document.document_id, self.agency_id),
            )
        return self.get(document.document_id)

    # -- reads -----------------------------------------------------------
    def get(self, document_id: Any) -> Document:
        ref = require_ref(document_id, "document_id", max_len=64)
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id = ? AND agency_id = ?",
            (ref, self.agency_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                f"document {ref!r} does not exist",
                code=DOCUMENT_NOT_FOUND,
                document_id=ref,
            )
        return Document.from_row(row)

    def list_documents(
        self,
        *,
        kind: Any = None,
        subject_ref: Any = None,
        status: Any = None,
    ) -> tuple[Document, ...]:
        """Documents matching the filters, ordered by key then version ascending.

        ``subject_ref`` matches whichever of ``booking_id``/``invoice_id``/
        ``case_id`` the caller means — the row has one subject column per kind.
        """
        clauses: list[str] = ["agency_id = ?"]
        params: list[Any] = [self.agency_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(_require_kind(kind))
        if status is not None:
            clauses.append("status = ?")
            params.append(_require_status(status))
        if subject_ref is not None:
            ref = require_ref(subject_ref, "subject_ref", max_len=64)
            clauses.append("(booking_id = ? OR invoice_id = ? OR case_id = ?)")
            params.extend([ref, ref, ref])
        sql = "SELECT * FROM documents"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY document_key, version, document_id"
        return tuple(Document.from_row(row) for row in self._conn.execute(sql, params))

    def latest(self, document_key: Any) -> Document:
        """The highest-version row for ``document_key`` (whatever its status)."""
        key = _safe_document_key(document_key)
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_key = ? AND agency_id = ? ORDER BY version DESC LIMIT 1",
            (key, self.agency_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                f"no document exists for key {key!r}",
                code=DOCUMENT_NOT_FOUND,
                document_key=key,
            )
        return Document.from_row(row)

    def text(self, document_id: Any) -> str:
        """Plain-text export of a document's body, read back from disk."""
        document = self.get(document_id)
        if not document.storage_ref:
            raise DomainError(
                f"document {document.document_id!r} has no storage_ref",
                code=DOCUMENT_STORAGE_INVALID,
                document_id=document.document_id,
            )
        path = resolve_storage_path(self._root, document.storage_ref)
        if not path.exists():
            raise DomainError(
                f"document file is missing: {document.storage_ref}",
                code=DOCUMENT_STORAGE_INVALID,
                document_id=document.document_id,
                storage_ref=document.storage_ref,
            )
        return plain_text(path.read_text(encoding="utf-8"))

    def path_for(self, document: Document) -> Path:
        """Absolute path of a document's file, validated against the root."""
        if not document.storage_ref:
            raise DomainError(
                f"document {document.document_id!r} has no storage_ref",
                code=DOCUMENT_STORAGE_INVALID,
                document_id=document.document_id,
            )
        return resolve_storage_path(self._root, document.storage_ref)

    def verify(self, document_id: Any, *, active_only: bool = True) -> VerificationReport:
        """Re-hash the stored body and compare it with the file on disk.

        A ``SUPERSEDED``/``VOID`` version is refused by default (it is not an
        active document); pass ``active_only=False`` to audit its file anyway.
        """
        document = self.get(document_id)
        if active_only and not document.is_active:
            raise DomainError(
                f"document {document.document_id!r} is {document.status}, not active",
                code=DOCUMENT_NOT_ACTIVE,
                document_id=document.document_id,
                status=document.status,
            )
        issues: list[str] = []
        path: Optional[str] = None
        actual: Optional[str] = None
        if not document.storage_ref:
            issues.append(ISSUE_MISSING_STORAGE_REF)
        else:
            try:
                target = resolve_storage_path(self._root, document.storage_ref)
            except DomainError:
                issues.append(ISSUE_UNSAFE_STORAGE_REF)
            else:
                path = str(target)
                if not target.exists():
                    issues.append(ISSUE_FILE_MISSING)
                else:
                    try:
                        text = target.read_bytes().decode("utf-8")
                    except (OSError, UnicodeDecodeError):
                        issues.append(ISSUE_FILE_UNREADABLE)
                    else:
                        actual = sha256_hex(text)
                        if actual != document.body_sha256:
                            issues.append(ISSUE_HASH_MISMATCH)
        return VerificationReport(
            document_id=document.document_id,
            document_key=document.document_key,
            version=document.version,
            status=document.status,
            content_class=document.content_class,
            storage_ref=document.storage_ref,
            path=path,
            expected_sha256=document.body_sha256,
            actual_sha256=actual,
            ok=not issues,
            issues=tuple(issues),
        )

    # -- internals -------------------------------------------------------
    def _write_body(self, path: Path, body: str) -> None:
        """Write the exact bytes of ``body`` (UTF-8, no newline translation)."""
        payload = body.encode("utf-8")
        temporary = path.with_name(path.name + ".tmp")
        try:
            # Directory creation belongs inside this guard: when the storage path is
            # a file (or read-only), mkdir raises the same class of OS error as the
            # write, and letting it escape uncoded breaks the promise that every
            # storage failure in this module is a coded DomainError.
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        except OSError as exc:
            _unlink_quietly(temporary)
            raise DomainError(
                f"document file could not be written ({exc.__class__.__name__})",
                code=DOCUMENT_STORAGE_INVALID,
                storage_ref=str(path.name),
            ) from exc

    def _next_version(self, document_key: str) -> int:
        """Next version *of this agency's* document: the key is a per-tenant name."""
        row = self._conn.execute(
            "SELECT MAX(version) AS top FROM documents WHERE document_key = ?"
            " AND agency_id = ?",
            (document_key, self.agency_id),
        ).fetchone()
        top = row["top"] if row is not None else None
        return 1 if top is None else int(top) + 1

    def _supersede(self, document_key: str) -> int:
        """Retire this agency's earlier versions only: a shared key must not let one
        tenant rewrite another tenant's document status."""
        cursor = self._conn.execute(
            "UPDATE documents SET status = 'SUPERSEDED' WHERE document_key = ?"
            " AND agency_id = ? AND status = 'GENERATED'",
            (document_key, self.agency_id),
        )
        changed = cursor.rowcount
        return int(changed) if changed and changed > 0 else 0

    def _collect(
        self, kind: str, fields: Mapping[str, Any], locale: str
    ) -> tuple[_Facts, dict[str, Optional[str]]]:
        """Load the referenced rows, reconcile them, and build the fact list."""
        if kind == "CONFIRMATION":
            return self._confirmation(fields, locale)
        if kind == "CONTRACT":
            return self._offer_letter(fields, locale)
        if kind in ("INVOICE", "RECEIPT"):
            return self._financial(kind, fields, locale)
        return self._notice(kind, fields)

    # -- per-kind collection --------------------------------------------
    def _confirmation(
        self, fields: Mapping[str, Any], locale: str
    ) -> tuple[_Facts, dict[str, Optional[str]]]:
        booking_id = self._require_booking_id(fields)
        claims = _claims(fields, allowed=("currency", "total_minor"))
        booking = self._load_booking(booking_id)
        status = str(booking["status"])
        if status not in CONFIRMATION_BOOKING_STATUSES:
            raise DomainError(
                f"booking {booking_id!r} is {status} and cannot be confirmed to the guest",
                code=DOCUMENT_SOURCE_INVALID,
                booking_id=booking_id,
                status=status,
            )
        currency = self._booking_currency(booking)
        total = int(booking["total_minor"])
        _match_claims(claims, currency=currency, total_minor=total, source="booking")
        rows = self._booking_rows(booking, locale, currency)
        return _Facts(rows=rows, notes=_notes(fields)), {"booking_id": booking_id}

    def _offer_letter(
        self, fields: Mapping[str, Any], locale: str
    ) -> tuple[_Facts, dict[str, Optional[str]]]:
        booking_id = self._require_booking_id(fields)
        claims = _claims(fields, allowed=("currency", "total_minor"))
        booking = self._load_booking(booking_id)
        status = str(booking["status"])
        if status in CONTRACT_BLOCKED_BOOKING_STATUSES:
            raise DomainError(
                f"booking {booking_id!r} is {status}; an offer letter would be moot",
                code=DOCUMENT_SOURCE_INVALID,
                booking_id=booking_id,
                status=status,
            )
        currency = self._booking_currency(booking)
        total = int(booking["total_minor"])
        _match_claims(claims, currency=currency, total_minor=total, source="booking")
        rows = self._booking_rows(booking, locale, currency)
        rows = rows + (
            (_LABELS["offer_total"], format_amount(Money(total, currency), locale)),
            (_LABELS["validity"], f"{OFFER_VALIDITY_HOURS} jam sejak diterbitkan"),
        )
        return _Facts(rows=rows, notes=_notes(fields)), {"booking_id": booking_id}

    def _financial(
        self, kind: str, fields: Mapping[str, Any], locale: str
    ) -> tuple[_Facts, dict[str, Optional[str]]]:
        invoice_id = fields.get("invoice_id")
        if invoice_id is None:
            raise DomainError(
                f"{kind} needs an invoice_id",
                code=INVALID_DOCUMENT_REQUEST,
                field="invoice_id",
            )
        invoice_id = require_ref(invoice_id, "invoice_id", max_len=64)
        claims = _claims(
            fields, allowed=("currency", "total_minor", "invoice_number")
        )
        invoice = self._conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ? AND agency_id = ?",
            (invoice_id, self.agency_id),
        ).fetchone()
        if invoice is None:
            raise EntityNotFound(
                f"invoice {invoice_id!r} does not exist",
                code=DOCUMENT_SOURCE_MISSING,
                invoice_id=invoice_id,
            )
        if str(invoice["status"]) != "ISSUED":
            raise DomainError(
                f"invoice {invoice_id!r} is {invoice['status']}; no active document",
                code=DOCUMENT_SOURCE_INVALID,
                invoice_id=invoice_id,
                status=str(invoice["status"]),
            )
        number = invoice["invoice_number"]
        if not number:
            raise DomainError(
                f"invoice {invoice_id!r} has no invoice_number",
                code=DOCUMENT_SOURCE_INVALID,
                invoice_id=invoice_id,
            )
        currency = str(invoice["currency"])
        if currency not in MINOR_UNITS:
            raise DomainError(
                f"invoice {invoice_id!r} carries an unknown currency",
                code=DOCUMENT_SOURCE_INVALID,
                invoice_id=invoice_id,
            )
        booking_id = str(invoice["booking_id"])
        booking_claim = fields.get("booking_id")
        if booking_claim is not None and str(booking_claim) != booking_id:
            raise DomainError(
                "subject booking_id does not match the invoice's booking",
                code=INVOICE_SOURCE_MISMATCH,
                invoice_id=invoice_id,
                booking_id=booking_id,
            )
        line_rows = self._load_invoice_lines(invoice_id)
        self._reconcile_invoice(invoice_id, invoice, line_rows)
        _match_claims(
            claims,
            currency=currency,
            total_minor=int(invoice["total_minor"]),
            invoice_number=str(number),
            source="invoice",
        )
        rows = [
            (_LABELS["invoice_number"], html.escape(str(number))),
            (_LABELS["booking_id"], html.escape(booking_id)),
            (_LABELS["invoice_status"], html.escape(str(invoice["status"]))),
            (_LABELS["currency"], html.escape(currency)),
            (_LABELS["subtotal"], format_amount(Money(int(invoice["subtotal_minor"]), currency), locale)),
            (_LABELS["tax"], format_amount(Money(int(invoice["tax_minor"]), currency), locale)),
            (_LABELS["service"], format_amount(Money(int(invoice["service_minor"]), currency), locale)),
            (_LABELS["total"], format_amount(Money(int(invoice["total_minor"]), currency), locale)),
            (_LABELS["paid"], format_amount(Money(int(invoice["amount_paid_minor"]), currency), locale)),
            (
                _LABELS["outstanding"],
                format_amount(
                    Money(int(invoice["total_minor"]) - int(invoice["amount_paid_minor"]), currency),
                    locale,
                ),
            ),
            (_LABELS["payment_status"], html.escape(str(invoice["payment_status"]))),
            (_LABELS["issued_at"], html.escape(str(invoice["issued_at"]))),
            (_LABELS["due_at"], html.escape(str(invoice["due_at"]))),
        ]
        lines = tuple(
            (
                html.escape(redact(str(line["description"]))),
                str(int(line["quantity"])),
                format_amount(Money(int(line["unit_amount_minor"]), currency), locale),
                format_amount(Money(int(line["amount_minor"]), currency), locale),
            )
            for line in line_rows
        )
        return (
            _Facts(rows=tuple(rows), lines=lines, notes=_notes(fields)),
            {"invoice_id": invoice_id, "booking_id": booking_id},
        )

    def _notice(
        self, kind: str, fields: Mapping[str, Any]
    ) -> tuple[_Facts, dict[str, Optional[str]]]:
        booking_id = fields.get("booking_id")
        case_id = fields.get("case_id")
        _claims(fields, allowed=())
        if fields.get("invoice_id") is not None:
            raise DomainError(
                f"{kind} does not reference an invoice",
                code=INVALID_DOCUMENT_REQUEST,
                field="invoice_id",
            )
        if booking_id is None and case_id is None:
            raise DomainError(
                f"{kind} needs a booking_id or a case_id",
                code=INVALID_DOCUMENT_REQUEST,
                field="booking_id",
            )
        rows: list[tuple[str, str]] = []
        references: dict[str, Optional[str]] = {"booking_id": None, "case_id": None}
        if booking_id is not None:
            ref = require_ref(booking_id, "booking_id", max_len=64)
            booking = self._load_booking(ref)
            rows.append((_LABELS["booking_id"], html.escape(ref)))
            rows.append((_LABELS["status"], html.escape(str(booking["status"]))))
            rows.append((_LABELS["check_in"], html.escape(str(booking["check_in"]))))
            rows.append((_LABELS["check_out"], html.escape(str(booking["check_out"]))))
            references["booking_id"] = ref
        if case_id is not None:
            ref = require_ref(case_id, "case_id", max_len=64)
            row = self._conn.execute(
                "SELECT case_id, status, channel FROM cases WHERE case_id = ? AND agency_id = ?",
                (ref, self.agency_id),
            ).fetchone()
            if row is None:
                raise EntityNotFound(
                    f"case {ref!r} does not exist",
                    code=DOCUMENT_SOURCE_MISSING,
                    case_id=ref,
                )
            rows.append((_LABELS["case_id"], html.escape(ref)))
            rows.append((_LABELS["case_status"], html.escape(str(row["status"]))))
            rows.append((_LABELS["channel"], html.escape(str(row["channel"]))))
            references["case_id"] = ref
        return _Facts(rows=tuple(rows), notes=_notes(fields)), references

    # -- referenced rows -------------------------------------------------
    def _require_booking_id(self, fields: Mapping[str, Any]) -> str:
        value = fields.get("booking_id")
        if value is None:
            raise DomainError(
                "subject needs a booking_id",
                code=INVALID_DOCUMENT_REQUEST,
                field="booking_id",
            )
        return require_ref(value, "booking_id", max_len=64)

    def _load_booking(self, booking_id: str) -> Any:
        row = self._conn.execute(
            "SELECT * FROM bookings WHERE booking_id = ? AND agency_id = ?",
            (booking_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                f"booking {booking_id!r} does not exist",
                code=DOCUMENT_SOURCE_MISSING,
                booking_id=booking_id,
            )
        return row

    def _booking_currency(self, booking: Any) -> str:
        currency = str(booking["currency"])
        if currency not in MINOR_UNITS:
            raise DomainError(
                "booking carries an unknown currency",
                code=DOCUMENT_SOURCE_INVALID,
                booking_id=str(booking["booking_id"]),
            )
        return currency

    def _booking_rows(
        self, booking: Any, locale: str, currency: str
    ) -> tuple[tuple[str, str], ...]:
        check_in = str(booking["check_in"])
        check_out = str(booking["check_out"])
        guests = int(booking["adults"]) + int(booking["children"]) + int(booking["infants"])
        rows = (
            (_LABELS["booking_id"], html.escape(str(booking["booking_id"]))),
            (_LABELS["unit_id"], html.escape(str(booking["unit_id"]))),
            (_LABELS["property_id"], html.escape(str(booking["property_id"]))),
            (_LABELS["status"], html.escape(str(booking["status"]))),
            (_LABELS["check_in"], html.escape(check_in)),
            (_LABELS["check_out"], html.escape(check_out)),
            (_LABELS["check_in_local"], format_date(check_in, locale)),
            (_LABELS["check_out_local"], format_date(check_out, locale)),
            (_LABELS["nights"], str(int(booking["nights"]))),
            (_LABELS["adults"], str(int(booking["adults"]))),
            (_LABELS["children"], str(int(booking["children"]))),
            (_LABELS["infants"], str(int(booking["infants"]))),
            (_LABELS["guests"], str(guests)),
            (_LABELS["currency"], html.escape(currency)),
            (
                _LABELS["total"],
                format_amount(Money(int(booking["total_minor"]), currency), locale),
            ),
        )
        return rows

    def _load_invoice_lines(self, invoice_id: str) -> Sequence[Any]:
        return self._conn.execute(
            "SELECT line_id, kind, description, quantity, unit_amount_minor,"
            " amount_minor FROM invoice_lines WHERE invoice_id = ? ORDER BY line_id",
            (invoice_id,),
        ).fetchall()

    def _reconcile_invoice(
        self, invoice_id: str, invoice: Any, lines: Sequence[Any]
    ) -> None:
        """The stored lines must still add up to the stored totals.

        Reuses :func:`invoicing.derive_totals` instead of a private copy of the
        arithmetic, so a document can never disagree with the invoice module about
        what a line total is.
        """
        if not lines:
            raise DomainError(
                f"invoice {invoice_id!r} has no lines",
                code=INVOICE_SOURCE_MISMATCH,
                invoice_id=invoice_id,
            )
        try:
            derived = derive_totals(
                [
                    SimpleNamespace(kind=str(line["kind"]), amount_minor=int(line["amount_minor"]))
                    for line in lines
                ]
            )
        except DomainError as exc:
            # The source module refuses these lines outright (a negative bucket is
            # a pricing bug). Either way no document may quote them.
            raise DomainError(
                "invoice lines cannot be reconciled to a payable total",
                code=INVOICE_SOURCE_MISMATCH,
                invoice_id=invoice_id,
                cause=exc.code,
            ) from exc
        stored = (
            int(invoice["subtotal_minor"]),
            int(invoice["tax_minor"]),
            int(invoice["service_minor"]),
            int(invoice["total_minor"]),
        )
        if derived != stored:
            raise DomainError(
                "invoice lines do not reconcile to the invoice totals; refusing to"
                " publish a document that disagrees with the source",
                code=INVOICE_SOURCE_MISMATCH,
                invoice_id=invoice_id,
                derived={
                    "subtotal_minor": derived[0],
                    "tax_minor": derived[1],
                    "service_minor": derived[2],
                    "total_minor": derived[3],
                },
                stored={
                    "subtotal_minor": stored[0],
                    "tax_minor": stored[1],
                    "service_minor": stored[2],
                    "total_minor": stored[3],
                },
            )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_kind(value: Any) -> str:
    text = require_text(value, "kind", max_len=32).upper()
    if text not in DOCUMENT_KINDS:
        raise DomainError(
            "kind is not a document kind", code=UNKNOWN_DOCUMENT_KIND, field="kind"
        )
    return text


def _require_status(value: Any) -> str:
    text = require_text(value, "status", max_len=16).upper()
    if text not in DOCUMENT_STATUSES:
        raise DomainError(
            "status is not a document status",
            code=UNKNOWN_DOCUMENT_STATUS,
            field="status",
        )
    return text


def _require_role(
    value: Any,
    kind: str,
    *,
    field: str = "actor_role",
    roles: Optional[Sequence[str]] = None,
) -> str:
    text = require_text(value, field, max_len=32).lower()
    allowed = tuple(roles) if roles is not None else DOCUMENT_ROLE_POLICY[kind]
    if text not in allowed:
        raise DomainError(
            f"role {text!r} may not issue a {kind} document",
            code=DOCUMENT_ROLE_FORBIDDEN,
            field=field,
            kind=kind,
        )
    return text


def _require_subject(subject: Any) -> Mapping[str, Any]:
    if not isinstance(subject, Mapping):
        raise DomainError(
            "subject must be a mapping of document fields",
            code=INVALID_DOCUMENT_REQUEST,
            field="subject",
        )
    unknown = sorted(str(name) for name in subject.keys() if str(name) not in SUBJECT_FIELDS)
    if unknown:
        raise DomainError(
            "subject carries field(s) this service will not publish",
            code=UNKNOWN_SUBJECT_FIELD,
            fields=redact(",".join(unknown)),
        )
    contacts = (
        _clean_contact(subject["contact_email"], "contact_email", "[EMAIL]")
        if subject.get("contact_email") is not None
        else None
    )
    phone = (
        _clean_contact(subject["contact_phone"], "contact_phone", "[PHONE]")
        if subject.get("contact_phone") is not None
        else None
    )
    cleaned: dict[str, Any] = {}
    for name in ("booking_id", "invoice_id", "case_id", "recipient_ref", "invoice_number"):
        if subject.get(name) is not None:
            cleaned[name] = require_ref(subject[name], name, max_len=64)
    if subject.get("title") is not None:
        cleaned["title"] = _clean_text(subject["title"], "title", max_len=120)
    if subject.get("notes") is not None:
        cleaned["notes"] = _clean_text(subject["notes"], "notes", max_len=2000)
    if subject.get("currency") is not None:
        cleaned["currency"] = _require_currency(subject["currency"])
    if subject.get("total_minor") is not None:
        cleaned["total_minor"] = _require_minor(subject["total_minor"], field="total_minor")
    if contacts is not None:
        cleaned["contact_email"] = contacts
    if phone is not None:
        cleaned["contact_phone"] = phone
    return cleaned


def _notes(fields: Mapping[str, Any]) -> str:
    return str(fields.get("notes") or "")


def _title(kind: str, locale: str, fields: Mapping[str, Any]) -> str:
    custom = fields.get("title")
    if custom:
        return str(custom)
    return html.escape(_KIND_TITLES[locale][kind])


def _claims(
    fields: Mapping[str, Any], *, allowed: Sequence[str]
) -> dict[str, Any]:
    """Read the caller's cross-check claims, refusing one that has no source."""
    claims: dict[str, Any] = {}
    for name in ("currency", "total_minor", "invoice_number"):
        if fields.get(name) is None:
            continue
        if name not in allowed:
            raise DomainError(
                f"a {name} claim has no source for this document kind",
                code=INVALID_DOCUMENT_REQUEST,
                field=name,
            )
        claims[name] = fields[name]
    return claims


def _match_claims(
    claims: Mapping[str, Any],
    *,
    currency: str,
    total_minor: int,
    source: str,
    invoice_number: Optional[str] = None,
) -> None:
    """The caller's claims must agree with the source row, or nothing is written."""
    code = INVOICE_SOURCE_MISMATCH if source == "invoice" else DOCUMENT_SOURCE_MISMATCH
    if "currency" in claims and claims["currency"] != currency:
        raise DomainError(
            f"subject currency disagrees with the {source} row",
            code=code,
            field="currency",
        )
    if "total_minor" in claims and int(claims["total_minor"]) != total_minor:
        raise DomainError(
            f"subject total_minor disagrees with the {source} row",
            code=code,
            field="total_minor",
        )
    if "invoice_number" in claims and str(claims["invoice_number"]) != str(invoice_number):
        raise DomainError(
            "subject invoice_number disagrees with the invoice row",
            code=INVOICE_SOURCE_MISMATCH,
            field="invoice_number",
        )

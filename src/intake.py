"""Structured intake: deterministic, rule-first extraction of guest requests.

The module turns a redacted inbound guest message into *provenanced field
proposals* (``intake_proposals`` + ``intake_fields``) and puts a human review
step between an extracted value and the rest of the system.

Three layers:

1. **Vocabulary** — :data:`FIELD_SPECS` (name → :class:`FieldSpec`) is the single
   source of truth for which fields exist, their storage type, whether they are
   required, and the validator that must accept a proposed value.
2. **Extraction** — :func:`parse_rule_based` is pure regex/heuristics. It never
   calls a model, never invents a date from an ambiguous relative phrase
   ("akhir bulan", "weekend nanti", "secepatnya" → ``value is None`` with
   ``confidence < 40``), and returns byte-identical results for identical input.
3. **Service** — :class:`IntakeService` writes the proposal, enforces provenance
   against ``case_messages``, and lets a human ACCEPT/REJECT a field. An
   extracted value is **immutable** once written: only its review columns may
   change (``IMMUTABLE_FIELD`` otherwise).

Determinism: no floating point, no implicit ``now()`` (the clock is injected),
no implicit ids (the :class:`~primitives.IdFactory` is injected). Every returned
collection is a ``tuple``/``list`` in a fixed, documented order.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Optional

from primitives import (
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    fingerprint,
    parse_iso,
    parse_iso_date,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
    to_iso_date,
)

__all__ = [
    # error codes
    "CODE_INTAKE_PROPOSAL_NOT_FOUND",
    "CODE_INTAKE_FIELD_NOT_FOUND",
    "CODE_UNKNOWN_FIELD",
    "CODE_INVALID_FIELD_VALUE",
    "CODE_MISSING_PROVENANCE",
    "CODE_ALREADY_REVIEWED",
    "CODE_INVALID_REVIEW_DECISION",
    "CODE_LOW_CONFIDENCE",
    "CODE_MISSING_REVIEW_REASON",
    "CODE_IMMUTABLE_FIELD",
    "CODE_CASE_NOT_FOUND",
    "IntakeError",
    # vocabulary
    "VALUE_TYPES",
    "PURPOSES",
    "METHODS",
    "DECISIONS",
    "STATUS_PROPOSED",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "ACCEPT_CONFIDENCE_MIN",
    "AMBIGUOUS_CONFIDENCE_MAX",
    "MUTABLE_COLUMNS",
    "IMMUTABLE_COLUMNS",
    "AREA_ALIASES",
    "MONTHS",
    "FieldSpec",
    "FIELD_SPECS",
    "REQUIRED_FIELDS",
    "ProposedField",
    "FieldRow",
    # extraction
    "parse_rule_based",
    "validate_facts",
    # service
    "IntakeService",
]


# --------------------------------------------------------------------------- #
# Errors — one subclass, explicit UPPERCASE codes (never parse the message)
# --------------------------------------------------------------------------- #
CODE_INTAKE_PROPOSAL_NOT_FOUND = "INTAKE_PROPOSAL_NOT_FOUND"
CODE_INTAKE_FIELD_NOT_FOUND = "INTAKE_FIELD_NOT_FOUND"
CODE_UNKNOWN_FIELD = "UNKNOWN_FIELD"
CODE_INVALID_FIELD_VALUE = "INVALID_FIELD_VALUE"
CODE_MISSING_PROVENANCE = "MISSING_PROVENANCE"
CODE_ALREADY_REVIEWED = "ALREADY_REVIEWED"
CODE_INVALID_REVIEW_DECISION = "INVALID_REVIEW_DECISION"
CODE_LOW_CONFIDENCE = "LOW_CONFIDENCE"
CODE_MISSING_REVIEW_REASON = "MISSING_REVIEW_REASON"
CODE_IMMUTABLE_FIELD = "IMMUTABLE_FIELD"
CODE_CASE_NOT_FOUND = "CASE_NOT_FOUND"


class IntakeError(DomainError):
    """A deliberate intake failure. ``code`` is the contract; never parse text."""

    code = "INTAKE_ERROR"


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
VALUE_TYPES: tuple[str, ...] = ("date", "integer", "text", "money", "boolean", "enum")
PURPOSES: tuple[str, ...] = ("leisure", "family", "workation", "event", "other")
METHODS: tuple[str, ...] = ("rule", "llm_shadow", "manual")
DECISIONS: tuple[str, ...] = ("ACCEPT", "REJECT")

STATUS_PROPOSED = "PROPOSED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
FIELD_STATUSES: tuple[str, ...] = (STATUS_PROPOSED, STATUS_ACCEPTED, STATUS_REJECTED)

#: A value may only be ACCEPTed when it is real and the extractor was confident
#: enough. Below this the human must fix the value by hand, not rubber-stamp it.
ACCEPT_CONFIDENCE_MIN = 40

#: Anything at or below this is "we saw a wish, not a fact" (value stays None).
AMBIGUOUS_CONFIDENCE_MAX = 39

#: Columns a review may change. Everything else on a stored field is frozen.
MUTABLE_COLUMNS: tuple[str, ...] = (
    "status",
    "reviewed_by",
    "reviewed_at",
    "rejection_reason",
)

#: Columns that belong to the extraction itself and can never be rewritten.
IMMUTABLE_COLUMNS: tuple[str, ...] = (
    "intake_field_id",
    "proposal_id",
    "case_id",
    "field_name",
    "field_value",
    "value_type",
    "confidence",
    "source_message_id",
    "source_span_start",
    "source_span_end",
    "value_sha256",
    "created_at",
)

#: Known area names. The value is what gets stored; the key is what a guest
#: might type. Deliberately a closed list: an area we do not recognise stays
#: UNKNOWN instead of becoming a free-text guess.
AREA_ALIASES: Mapping[str, str] = {
    "canggu": "Canggu",
    "ubud": "Ubud",
    "seminyak": "Seminyak",
    "seminjak": "Seminyak",
    "sanur": "Sanur",
    "uluwatu": "Uluwatu",
    "kuta": "Kuta",
    "kuta utara": "Kuta Utara",
    "nusa dua": "Nusa Dua",
    "nusadua": "Nusa Dua",
    "jimbaran": "Jimbaran",
    "denpasar": "Denpasar",
    "kintamani": "Kintamani",
    "lovina": "Lovina",
    "amed": "Amed",
    "sidemen": "Sidemen",
    "munduk": "Munduk",
    "bedugul": "Bedugul",
    "yogya": "Yogyakarta",
    "jogja": "Yogyakarta",
    "jogjakarta": "Yogyakarta",
    "yogyakarta": "Yogyakarta",
    "prawirotaman": "Prawirotaman",
    "malioboro": "Malioboro",
    "bandung": "Bandung",
    "bogor": "Bogor",
    "jakarta": "Jakarta",
    "surabaya": "Surabaya",
    "lombok": "Lombok",
}

#: Indonesian month names and the abbreviations guests actually type.
MONTHS: Mapping[str, int] = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5, "may": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "ags": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12, "dec": 12,
}


# --------------------------------------------------------------------------- #
# Field specifications
# --------------------------------------------------------------------------- #
def _is_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    return isinstance(value, date)


def _valid_date(value: Any) -> bool:
    """A ``date``, or an ISO-8601 date string. ``datetime`` and junk fail."""
    if value is None:
        return True
    if _is_date(value):
        return True
    if isinstance(value, str):
        try:
            parse_iso_date(value)
        except DomainError:
            return False
        return True
    return False


def _valid_integer_min(minimum: int) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return value >= minimum

    return check


def _valid_money(value: Any) -> bool:
    """Money is integer minor units and must be strictly positive."""
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value > 0


def _valid_currency(value: Any) -> bool:
    """A three-letter uppercase ISO-4217-shaped code."""
    if value is None:
        return True
    return isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()


def _valid_enum(*allowed: str) -> Callable[[Any], bool]:
    permitted = frozenset(allowed)

    def check(value: Any) -> bool:
        if value is None:
            return True
        return isinstance(value, str) and value in permitted

    return check


def _valid_text(value: Any) -> bool:
    """Non-empty text. An empty string is not a fact, it is missing data."""
    if value is None:
        return True
    return isinstance(value, str) and bool(value.strip())


def _valid_boolean(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, bool)


@dataclass(frozen=True)
class FieldSpec:
    """The contract for one intake field.

    ``validator`` receives the *proposed* Python value and must return ``True``
    for ``None`` (UNKNOWN is a legal proposal) as well as for every acceptable
    concrete value. A value that fails the validator is never written.
    """

    name: str
    value_type: str
    required: bool
    validator: Callable[[Any], bool]
    description: str = ""


#: Declaration order is the canonical order used by ``unknown_required`` and by
#: the rows a proposal writes, so two runs over the same input are identical.
FIELD_SPECS: Mapping[str, FieldSpec] = {
    "check_in": FieldSpec(
        "check_in", "date", True, _valid_date, "Tanggal check-in (ISO-8601 date)."
    ),
    "check_out": FieldSpec(
        "check_out", "date", True, _valid_date, "Tanggal check-out (ISO-8601 date)."
    ),
    "adults": FieldSpec(
        "adults", "integer", True, _valid_integer_min(1), "Jumlah tamu dewasa (>= 1)."
    ),
    "children": FieldSpec(
        "children", "integer", False, _valid_integer_min(0), "Jumlah anak-anak."
    ),
    "infants": FieldSpec(
        "infants", "integer", False, _valid_integer_min(0), "Jumlah bayi."
    ),
    "area_preference": FieldSpec(
        "area_preference", "text", False, _valid_text, "Preferensi lokasi/daerah."
    ),
    "budget_minor": FieldSpec(
        "budget_minor", "money", False, _valid_money, "Budget dalam minor units."
    ),
    "currency": FieldSpec(
        "currency", "enum", False, _valid_currency, "Kode mata uang 3 huruf."
    ),
    "purpose": FieldSpec(
        "purpose", "enum", False, _valid_enum(*PURPOSES), "Tujuan menginap."
    ),
    "special_requests": FieldSpec(
        "special_requests", "text", False, _valid_text, "Permintaan khusus."
    ),
    "arrival_time": FieldSpec(
        "arrival_time", "text", False, _valid_text, "Perkiraan jam kedatangan."
    ),
    "flexible_dates": FieldSpec(
        "flexible_dates", "boolean", False, _valid_boolean, "Tanggal fleksibel."
    ),
}

REQUIRED_FIELDS: tuple[str, ...] = tuple(
    name for name, spec in FIELD_SPECS.items() if spec.required
)


# --------------------------------------------------------------------------- #
# Value (de)serialisation — field_value is TEXT, accepted_facts returns Python
# --------------------------------------------------------------------------- #
def _serialize_value(value: Any, value_type: str) -> Optional[str]:
    if value is None:
        return None
    if value_type == "date":
        return to_iso_date(value if _is_date(value) else parse_iso_date(str(value)))
    if value_type in ("integer", "money"):
        return str(int(value))
    if value_type == "boolean":
        return "true" if value else "false"
    return str(value)


def _deserialize_value(text: Optional[str], value_type: str) -> Any:
    if text is None:
        return None
    if value_type == "date":
        return parse_iso_date(text)
    if value_type in ("integer", "money"):
        return int(text)
    if value_type == "boolean":
        return text == "true"
    return text


def _coerce_value(spec: FieldSpec, value: Any) -> Any:
    """Normalise an already-validated value to its canonical Python type."""
    if value is None:
        return None
    kind = spec.value_type
    if kind == "date":
        return value if _is_date(value) else parse_iso_date(str(value))
    if kind in ("integer", "money"):
        if isinstance(value, bool) or not isinstance(value, int):
            return int(str(value))
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "ya")
    return str(value)


def _value_hash(serialized: Optional[str], value_type: str) -> str:
    return sha256_hex(canonical_json({"t": value_type, "v": serialized}))


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProposedField:
    """One extracted value with its provenance span and a 0..100 confidence."""

    name: str
    value: Any
    confidence: int
    span_start: Optional[int] = None
    span_end: Optional[int] = None

    def as_dict(self, value_type: Optional[str] = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_type": value_type,
            "value": self.value,
            "confidence": self.confidence,
            "span_start": self.span_start,
            "span_end": self.span_end,
        }


_FIELD_COLUMNS: tuple[str, ...] = (
    "intake_field_id",
    "proposal_id",
    "case_id",
    "field_name",
    "field_value",
    "value_type",
    "confidence",
    "source_message_id",
    "source_span_start",
    "source_span_end",
    "status",
    "value_sha256",
    "created_at",
    "reviewed_by",
    "reviewed_at",
    "rejection_reason",
)


@dataclass(frozen=True)
class FieldRow:
    """A stored intake field. ``field_value`` mirrors the TEXT column exactly."""

    intake_field_id: str
    proposal_id: str
    case_id: str
    field_name: str
    field_value: Optional[str]
    value_type: str
    confidence: int
    source_message_id: str
    source_span_start: Optional[int]
    source_span_end: Optional[int]
    status: str
    value_sha256: str
    created_at: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    rejection_reason: Optional[str] = None

    @property
    def python_value(self) -> Any:
        """The stored value converted back to its Python type (money stays minor)."""
        return _deserialize_value(self.field_value, self.value_type)


def _row_to_field(row: sqlite3.Row) -> FieldRow:
    return FieldRow(**{column: row[column] for column in _FIELD_COLUMNS})


# --------------------------------------------------------------------------- #
# Rule-based extraction
# --------------------------------------------------------------------------- #
_ROMAN_SEP = r"(?:-|–|—|s/d|s\.d\.|sampai|hingga|to|sampai dengan)"

_RE_RANGE_DAY_MONTH = re.compile(
    r"(?<![\d/])(?P<d1>\d{1,2})\s*" + _ROMAN_SEP + r"\s*(?P<d2>\d{1,2})\s+"
    r"(?P<mon>[A-Za-z]+)\.?\s*(?P<y>\d{4})?(?![\d/])",
    re.IGNORECASE,
)
_RE_RANGE_ISO_MONTH = re.compile(
    r"(?<![\d/])(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2})\s*" + _ROMAN_SEP + r"\s*"
    r"(?P<d2>\d{1,2})\s+(?P<mon>[A-Za-z]+)\.?\s*(?P<y2>\d{4})?(?![\d/])",
    re.IGNORECASE,
)
_RE_RANGE_ISO_ISO = re.compile(
    r"(?<![\d/])(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2})\s*" + _ROMAN_SEP + r"\s*"
    r"(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?![\d/])",
    re.IGNORECASE,
)
_RE_ISO_DATE = re.compile(r"(?<![\d/])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d/])")
_RE_SLASH_DATE = re.compile(
    r"(?<![\d/])(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})(?![\d/])"
)
_RE_DAY_MONTH = re.compile(
    r"(?<![\d/])(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]+)\.?\s*(?P<y>\d{4})?(?![\d/])",
    re.IGNORECASE,
)

_RE_NIGHTS = re.compile(r"(?P<n>\d{1,2})\s*(?:malam|nights?)\b", re.IGNORECASE)

_RE_ADULTS = re.compile(
    r"(?P<n>\d{1,2})\s*(?:orang\s+)?(?:dewasa|adult|adults|pax)\b", re.IGNORECASE
)
_RE_CHILDREN = re.compile(
    r"(?P<n>\d{1,2})\s*(?:orang\s+)?(?:anak(?:\s*-?\s*anak)?|child|children|kids?)\b",
    re.IGNORECASE,
)
_RE_INFANTS = re.compile(
    r"(?P<n>\d{1,2})\s*(?:orang\s+)?(?:bayi|infant|infants|baby)\b", re.IGNORECASE
)
_RE_GROUP = re.compile(r"(?P<n>\d{1,2})\s*(?:orang|pax|tamu)\b", re.IGNORECASE)

_RE_RUPIAH = re.compile(r"\bRp\.?\s*(?P<amount>\d[\d.]*(?:,\d{1,2})?)", re.IGNORECASE)
_RE_MULTIPLIER = re.compile(
    r"(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?P<unit>jt|juta|jtr|jtan|mio|million)\b",
    re.IGNORECASE,
)

_RE_ARRIVAL = re.compile(
    r"(?:datang|tiba|arrival|check[\s-]?in|kedatangan)[^\d\n]{0,16}"
    r"(?P<h>\d{1,2})[:.](?P<m>\d{2})",
    re.IGNORECASE,
)
_RE_CLOCK = re.compile(r"(?:jam|pukul)\s*(?P<h>\d{1,2})[:.](?P<m>\d{2})", re.IGNORECASE)

_AREA_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(
        re.escape(alias) for alias in sorted(AREA_ALIASES, key=len, reverse=True)
    ) + r")(?![A-Za-z])",
    re.IGNORECASE,
)

_PURPOSE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("workation", re.compile(r"\b(workation|kerja|bisnis|business|wfh|remote work)\b", re.IGNORECASE)),
    ("event", re.compile(
        r"\b(event|acara|pernikahan|wedding|ulang tahun|seminar|retreat korporat)\b",
        re.IGNORECASE,
    )),
    ("family", re.compile(r"\b(keluarga|family|bersama anak|anak-anak)\b", re.IGNORECASE)),
    ("leisure", re.compile(
        r"\b(liburan|holiday|leisure|wisata|jalan[- ]jalan|honeymoon|bulan madu|jalan-jalan)\b",
        re.IGNORECASE,
    )),
)

_FEATURE_PATTERNS: tuple[str, ...] = (
    "kolam renang pribadi",
    "private pool",
    "extra bed",
    "airport transfer",
    "antar jemput bandara",
    "sarapan",
    "breakfast",
    "baby cot",
    "high chair",
    "pemandangan laut",
    "ocean view",
    "bbq",
)

_FLEXIBLE = re.compile(
    r"\b(?:tanggal(?:nya)?\s*(?:bisa|dapat|masih)?\s*(?:di)?(?:fleksibel|flexible)"
    r"|fleksibel|flexible dates?)\b",
    re.IGNORECASE,
)

_AMBIGUOUS_DATE = re.compile(
    r"\b(?:akhir bulan|akhir pekan|weekend|minggu depan|bulan depan|pekan depan"
    r"|secepatnya|segera|secepat mungkin|nanti|belum tahu kapan|belum pasti kapan)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_PAX = re.compile(
    r"\b(?:belum\s+(?:tahu|pasti)|tidak\s+tahu|blm)\s+(?:jumlah\s+)?"
    r"(?:orang|pax|tamu|berapanya?)\b",
    re.IGNORECASE,
)

#: Confidence bands used by the rule extractor (documented, not tuned by feel).
_C_EXACT_ISO = 95
_C_RANGE = 92
_C_MONTH_WITH_YEAR = 90
_C_MONTH_NO_YEAR = 75
_C_NIGHTS = 85
_C_ADULTS = 90
_C_CHILDREN = 90
_C_INFANTS = 88
_C_GROUP = 70
_C_RUPIAH = 90
_C_MULTIPLIER = 80
_C_CURRENCY = 80
_C_AREA = 85
_C_PURPOSE = 75
_C_ARRIVAL = 80
_C_FEATURE = 60
_C_FLEXIBLE = 70
_C_AMBIGUOUS = 20  # < AMBIGUOUS_CONFIDENCE_MAX: a wish, never a fact

_CHECKOUT_CUES = re.compile(r"(check[\s-]?out|pulang|keluar|checkout)\b", re.IGNORECASE)
_CHECKIN_CUES = re.compile(r"(check[\s-]?in|datang|masuk|arrival|arrive)\b", re.IGNORECASE)


def _resolve_now(now: Any) -> date:
    """Accept an aware ``datetime`` or a clock exposing ``now()``; return its date.

    Order matters: ``datetime`` itself exposes a ``now`` classmethod, so a duck-typed
    ``hasattr(now, "now")`` check *before* the ``isinstance`` check calls
    ``datetime.now()`` and silently swaps the caller's aware instant for a naive
    local one. The parameter is therefore classified as a datetime first, and any
    value coming from a clock is re-validated for awareness afterwards.
    """
    if isinstance(now, datetime):
        moment = now
    elif hasattr(now, "now") and callable(getattr(now, "now")):
        moment = now.now()
    elif isinstance(now, date):
        return now
    else:
        raise IntakeError(
            f"now tidak dikenali: {type(now).__name__}", code="INVALID_TIMESTAMP"
        )
    if not isinstance(moment, datetime):
        raise IntakeError(
            f"now harus datetime, bukan {type(moment).__name__}", code="INVALID_TIMESTAMP"
        )
    if moment.tzinfo is None:
        raise IntakeError("now harus timezone-aware", code="NAIVE_TIMESTAMP")
    return moment.date()


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _infer_year(month: int, day: int, today: date) -> Optional[date]:
    """A day+month with no year is the next occurrence on/after ``today``."""
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        return None
    if candidate < today:
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def _month_number(token: str) -> Optional[int]:
    key = token.strip().strip(".").lower()
    return MONTHS.get(key)


@dataclass
class _SpanHit:
    start: int
    end: int
    check_in: Optional[date]
    check_out: Optional[date]


def _collect_date_hits(text: str, today: date) -> list[_SpanHit]:
    """Find concrete dates, most specific pattern first; no overlaps."""
    claimed: list[tuple[int, int]] = []
    hits: list[_SpanHit] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    def take(start: int, end: int) -> None:
        claimed.append((start, end))

    # 1. day-day month year  ->  12-14 Oktober 2026
    for match in _RE_RANGE_DAY_MONTH.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        month = _month_number(match.group("mon"))
        if month is None:
            continue
        year_group = match.group("y")
        if year_group:
            start = _safe_date(int(year_group), month, int(match.group("d1")))
            end = _safe_date(int(year_group), month, int(match.group("d2")))
        else:
            start = _infer_year(month, int(match.group("d1")), today)
            end = start
            if start is not None and int(match.group("d2")) < int(match.group("d1")):
                end = _infer_year(month + 1, int(match.group("d2")), today)
        if start is None or end is None or end <= start:
            continue
        take(match.start(), match.end())
        hits.append(_SpanHit(match.start(), match.end(), start, end))

    # 2. ISO range (-> ISO or day-month)
    for pattern in (_RE_RANGE_ISO_ISO, _RE_RANGE_ISO_MONTH):
        for match in pattern.finditer(text):
            if overlaps(match.start(), match.end()):
                continue
            start = _safe_date(
                int(match.group("y1")), int(match.group("m1")), int(match.group("d1"))
            )
            if "mon" in match.groupdict() and match.groupdict().get("mon"):
                month = _month_number(match.group("mon"))
            else:
                month = int(match.group("m2"))
            if month is None:
                continue
            year_group = match.groupdict().get("y2") or match.groupdict().get("y1")
            end = _safe_date(int(year_group), month, int(match.group("d2")))
            if start is None or end is None or end <= start:
                continue
            take(match.start(), match.end())
            hits.append(_SpanHit(match.start(), match.end(), start, end))

    # 3. ISO single  2026-10-12
    for match in _RE_ISO_DATE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        value = _safe_date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if value is None:
            continue
        take(match.start(), match.end())
        hits.append(_SpanHit(match.start(), match.end(), value, None))

    # 4. slash date  12/10/2026
    for match in _RE_SLASH_DATE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        value = _safe_date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if value is None:
            continue
        take(match.start(), match.end())
        hits.append(_SpanHit(match.start(), match.end(), value, None))

    # 5. day month [year]  12 Okt [2026]
    for match in _RE_DAY_MONTH.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        month = _month_number(match.group("mon"))
        if month is None:
            continue
        year_group = match.group("y")
        if year_group:
            value = _safe_date(int(year_group), month, int(match.group("d")))
        else:
            value = _infer_year(month, int(match.group("d")), today)
        if value is None:
            continue
        take(match.start(), match.end())
        hits.append(_SpanHit(match.start(), match.end(), value, None))

    hits.sort(key=lambda hit: (hit.start, hit.end))
    return hits


def _parse_amount_rupiah(token: str) -> Optional[Decimal]:
    """``2.500.000`` → 2500000, ``1,5`` → 1.5 (Indonesian: '.' groups, ',' decimals)."""
    cleaned = token.strip().replace(".", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_rule_based(text: str, *, now: Any) -> list[ProposedField]:
    """Extract field proposals from a guest message. Deterministic, no AI.

    Returns one :class:`ProposedField` per recognised field, ordered like
    :data:`FIELD_SPECS`. Ambiguous relative expressions produce ``value=None``
    with ``confidence <= AMBIGUOUS_CONFIDENCE_MAX`` — the extractor must never
    invent a date from "akhir bulan" or "weekend nanti".
    """
    if not isinstance(text, str):
        raise IntakeError("text harus string", code=CODE_INVALID_FIELD_VALUE)
    today = _resolve_now(now)
    found: dict[str, ProposedField] = {}

    def put(name: str, value: Any, confidence: int, start: Optional[int], end: Optional[int]) -> None:
        if name not in FIELD_SPECS:
            return
        if start is not None and end is not None and end < start:
            start, end = end, start
        previous = found.get(name)
        if previous is None or confidence > previous.confidence:
            found[name] = ProposedField(name, value, confidence, start, end)

    # -- dates ----------------------------------------------------------- #
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    check_in_span: Optional[tuple[int, int]] = None
    check_out_span: Optional[tuple[int, int]] = None

    for hit in _collect_date_hits(text, today):
        if hit.check_in is not None and hit.check_out is not None:
            if check_in is None:
                check_in, check_in_span = hit.check_in, (hit.start, hit.end)
                check_out, check_out_span = hit.check_out, (hit.start, hit.end)
            continue
        single = hit.check_in
        cue = text[max(0, hit.start - 24):hit.start]
        if _CHECKOUT_CUES.search(cue) and check_out is None:
            check_out, check_out_span = single, (hit.start, hit.end)
        elif _CHECKIN_CUES.search(cue) and check_in is None:
            check_in, check_in_span = single, (hit.start, hit.end)
        elif check_in is None:
            check_in, check_in_span = single, (hit.start, hit.end)
        elif check_out is None:
            check_out, check_out_span = single, (hit.start, hit.end)

    # -- duration (may derive check_out) --------------------------------- #
    nights_match = _RE_NIGHTS.search(text)
    if nights_match is not None:
        nights = int(nights_match.group("n"))
        span = (nights_match.start(), nights_match.end())
        if check_in is not None and check_out is None and nights > 0:
            check_out, check_out_span = check_in + timedelta(days=nights), span
        elif check_in is not None and check_out is not None and nights > 0:
            expected = check_in + timedelta(days=nights)
            if expected != check_out:
                # The two dates disagree; keep the explicit range, record the
                # duration nowhere rather than silently overwrite a real date.
                pass
        elif check_in is None and check_out is None and nights > 0:
            # Duration without any anchor date is not a date: leave UNKNOWN.
            pass

    if check_in is not None and check_in_span is not None:
        confidence = _C_EXACT_ISO if text[check_in_span[0]:check_in_span[1]].count("-") == 2 and len(
            text[check_in_span[0]:check_in_span[1]]
        ) == 10 else _C_RANGE
        put("check_in", check_in, confidence, *check_in_span)
    if check_out is not None and check_out_span is not None:
        confidence = _C_EXACT_ISO if text[check_out_span[0]:check_out_span[1]].count("-") == 2 and len(
            text[check_out_span[0]:check_out_span[1]]
        ) == 10 else _C_RANGE
        put("check_out", check_out, confidence, *check_out_span)

    # -- party size ------------------------------------------------------ #
    for regex, name, confidence in (
        (_RE_ADULTS, "adults", _C_ADULTS),
        (_RE_CHILDREN, "children", _C_CHILDREN),
        (_RE_INFANTS, "infants", _C_INFANTS),
    ):
        match = regex.search(text)
        if match is not None:
            put(name, int(match.group("n")), confidence, match.start(), match.end())

    if "adults" not in found:
        group = _RE_GROUP.search(text)
        if group is not None:
            put("adults", int(group.group("n")), _C_GROUP, group.start(), group.end())

    ambiguous_pax = _AMBIGUOUS_PAX.search(text)
    if ambiguous_pax is not None and "adults" not in found:
        put(
            "adults",
            None,
            _C_AMBIGUOUS,
            ambiguous_pax.start(),
            ambiguous_pax.end(),
        )

    # -- budget ---------------------------------------------------------- #
    budget: Optional[int] = None
    budget_span: Optional[tuple[int, int]] = None
    rupiah = _RE_RUPIAH.search(text)
    if rupiah is not None:
        amount = _parse_amount_rupiah(rupiah.group("amount"))
        if amount is not None and amount > 0:
            budget = int(amount)
            budget_span = (rupiah.start(), rupiah.end())
    if budget is None:
        multiplier = _RE_MULTIPLIER.search(text)
        if multiplier is not None:
            amount = _parse_amount_rupiah(multiplier.group("amount"))
            if amount is not None and amount > 0:
                budget = int(amount * Decimal(1_000_000))
                budget_span = (multiplier.start(), multiplier.end())
    if budget is not None and budget_span is not None:
        put("budget_minor", budget, _C_RUPIAH if rupiah is not None else _C_MULTIPLIER, *budget_span)
        put("currency", "IDR", _C_CURRENCY, budget_span[0], budget_span[1])

    # -- area ------------------------------------------------------------ #
    area = _AREA_PATTERN.search(text)
    if area is not None:
        canonical = AREA_ALIASES[area.group(0).lower()]
        put("area_preference", canonical, _C_AREA, area.start(), area.end())

    # -- purpose --------------------------------------------------------- #
    for label, pattern in _PURPOSE_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            put("purpose", label, _C_PURPOSE, match.start(), match.end())
            break

    # -- arrival time ---------------------------------------------------- #
    arrival = _RE_ARRIVAL.search(text) or _RE_CLOCK.search(text)
    if arrival is not None:
        hour, minute = int(arrival.group("h")), int(arrival.group("m"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            put(
                "arrival_time",
                f"{hour:02d}:{minute:02d}",
                _C_ARRIVAL,
                arrival.start(),
                arrival.end(),
            )

    # -- special requests ------------------------------------------------ #
    lowered = text.lower()
    for phrase in _FEATURE_PATTERNS:
        index = lowered.find(phrase)
        if index >= 0:
            put("special_requests", phrase, _C_FEATURE, index, index + len(phrase))
            break

    # -- flexible dates -------------------------------------------------- #
    flexible = _FLEXIBLE.search(text)
    if flexible is not None:
        put("flexible_dates", True, _C_FLEXIBLE, flexible.start(), flexible.end())

    # -- ambiguity guard: a wish is not a date --------------------------- #
    ambiguous = _AMBIGUOUS_DATE.search(text)
    if ambiguous is not None and "check_in" not in found:
        put("check_in", None, _C_AMBIGUOUS, ambiguous.start(), ambiguous.end())

    return [found[name] for name in FIELD_SPECS if name in found]


# --------------------------------------------------------------------------- #
# Cross-field validation (used by matching/quotes)
# --------------------------------------------------------------------------- #
def validate_facts(facts: Mapping[str, Any]) -> tuple[str, ...]:
    """Cross-field consistency rules. Returns violation codes, in check order.

    An empty tuple means the fact set is self-consistent. Only *present* keys
    are checked — a missing field is ``unknown_required``'s problem, not this
    function's.
    """
    if not isinstance(facts, Mapping):
        raise IntakeError("facts harus Mapping", code=CODE_INVALID_FIELD_VALUE)
    problems: list[str] = []

    check_in = facts.get("check_in")
    check_out = facts.get("check_out")
    if check_in is not None and check_out is not None:
        if not (_is_date(check_in) and _is_date(check_out)) or check_out <= check_in:
            problems.append("CHECK_OUT_NOT_AFTER_CHECK_IN")

    adults = facts.get("adults")
    if adults is not None and (
        isinstance(adults, bool) or not isinstance(adults, int) or adults < 1
    ):
        problems.append("ADULTS_BELOW_MINIMUM")

    infants = facts.get("infants")
    if (
        adults is not None
        and infants is not None
        and not isinstance(infants, bool)
        and isinstance(infants, int)
        and isinstance(adults, int)
        and infants > adults
    ):
        problems.append("INFANTS_EXCEED_ADULTS")

    budget = facts.get("budget_minor")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
    ):
        problems.append("BUDGET_NOT_POSITIVE")

    currency = facts.get("currency")
    if currency is not None and not _valid_currency(currency):
        problems.append("INVALID_CURRENCY")

    return tuple(problems)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class IntakeService:
    """Writes structured intake proposals and records the human review.

    ``propose`` persists the extractor's output verbatim; ``review`` is the only
    way a value becomes usable, and it can only touch the review columns.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()

    # -- plumbing --------------------------------------------------------- #
    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _resolve_time(self, when: Any) -> str:
        if when is None:
            return self._now()
        if isinstance(when, datetime):
            return to_iso(when)
        return to_iso(parse_iso(str(when)))

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """All-or-nothing write. No half-written proposal survives an error."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- lookups ---------------------------------------------------------- #
    def _require_case(self, case_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise IntakeError(f"case tidak ditemukan: {case_id}", code=CODE_CASE_NOT_FOUND, case_id=case_id)

    def _require_provenance(self, case_id: str, source_message_id: str) -> None:
        row = self.conn.execute(
            "SELECT case_id FROM case_messages WHERE case_message_id = ?",
            (source_message_id,),
        ).fetchone()
        if row is None or str(row["case_id"]) != case_id:
            raise IntakeError(
                "pesan sumber tidak ada di case ini",
                code=CODE_MISSING_PROVENANCE,
                case_id=case_id,
                source_message_id=source_message_id,
            )

    def _get_field(self, intake_field_id: str) -> FieldRow:
        row = self.conn.execute(
            "SELECT * FROM intake_fields WHERE intake_field_id = ?",
            (intake_field_id,),
        ).fetchone()
        if row is None:
            raise IntakeError(
                f"intake field tidak ditemukan: {intake_field_id}",
                code=CODE_INTAKE_FIELD_NOT_FOUND,
                intake_field_id=intake_field_id,
            )
        return _row_to_field(row)

    def get_proposal(self, proposal_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM intake_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise IntakeError(
                f"proposal tidak ditemukan: {proposal_id}",
                code=CODE_INTAKE_PROPOSAL_NOT_FOUND,
                proposal_id=proposal_id,
            )
        return row

    def effective_status(self, intake_field_id: str) -> str:
        """The status that decides whether a field may still be reviewed."""
        return self._get_field(intake_field_id).status

    # -- validation ------------------------------------------------------- #
    def _prepare_fields(
        self, fields: Sequence[ProposedField]
    ) -> list[tuple[str, ProposedField]]:
        """Validate every field, dedupe by name, then order by FIELD_SPECS.

        Dedup is deterministic: for one proposal a field name may appear once,
        and the highest-confidence proposal wins (ties keep the earliest input).
        This mirrors ``uq_intake_field_once`` in the schema.
        """
        if fields is None:
            raise IntakeError("fields wajib diisi", code=CODE_INVALID_FIELD_VALUE)
        chosen: dict[str, ProposedField] = {}
        for item in fields:
            if not isinstance(item, ProposedField):
                raise IntakeError(
                    f"field harus ProposedField, dapat {type(item).__name__}",
                    code=CODE_INVALID_FIELD_VALUE,
                )
            spec = FIELD_SPECS.get(item.name)
            if spec is None:
                raise IntakeError(
                    f"field tidak dikenal: {item.name!r}",
                    code=CODE_UNKNOWN_FIELD,
                    field=item.name,
                )
            confidence = item.confidence
            if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
                raise IntakeError(
                    f"confidence harus int 0..100: {item.confidence!r}",
                    code=CODE_INVALID_FIELD_VALUE,
                    field=item.name,
                )
            start, end = item.span_start, item.span_end
            if start is not None or end is not None:
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or isinstance(start, bool)
                    or isinstance(end, bool)
                    or end < start
                ):
                    raise IntakeError(
                        "span tidak valid (span_end harus >= span_start)",
                        code=CODE_INVALID_FIELD_VALUE,
                        field=item.name,
                        span_start=start,
                        span_end=end,
                    )
            if not spec.validator(item.value):
                raise IntakeError(
                    f"nilai tidak valid untuk {item.name}: {item.value!r}",
                    code=CODE_INVALID_FIELD_VALUE,
                    field=item.name,
                    value=repr(item.value),
                )
            try:
                coerced = _coerce_value(spec, item.value)
            except (DomainError, ValueError, TypeError) as exc:
                raise IntakeError(
                    f"nilai tidak bisa dikonversi untuk {item.name}: {exc}",
                    code=CODE_INVALID_FIELD_VALUE,
                    field=item.name,
                ) from exc
            candidate = ProposedField(item.name, coerced, confidence, start, end)
            previous = chosen.get(item.name)
            if previous is None or candidate.confidence > previous.confidence:
                chosen[item.name] = candidate
        if not chosen:
            # A proposal with no field carries no provenance at all: fail closed
            # instead of writing an empty, unreviewable intake_proposals row.
            raise IntakeError(
                "proposal tanpa field tidak punya provenance",
                code=CODE_MISSING_PROVENANCE,
            )
        return [(name, chosen[name]) for name in FIELD_SPECS if name in chosen]

    # -- proposal --------------------------------------------------------- #
    def propose(
        self,
        case_id: str,
        *,
        source_message_id: str,
        fields: Sequence[ProposedField],
        extractor: str,
        method: str = "rule",
        produced_at: Any = None,
    ) -> tuple[str, list[FieldRow]]:
        """Persist one proposal plus one row per field. Returns ``(id, rows)``."""
        case_id = require_ref(case_id, "case_id")
        self._require_case(case_id)
        source_message_id = require_ref(source_message_id, "source_message_id")
        self._require_provenance(case_id, source_message_id)
        extractor = require_text(extractor, "extractor", max_len=64)
        if len(extractor) < 3:
            raise IntakeError(
                "extractor minimal 3 karakter", code=CODE_INVALID_FIELD_VALUE, field="extractor"
            )
        method = str(method).strip().lower()
        if method not in METHODS:
            raise IntakeError(
                f"method tidak dikenal: {method!r}",
                code=CODE_INVALID_FIELD_VALUE,
                field="method",
                allowed=list(METHODS),
            )
        prepared = self._prepare_fields(fields)
        when = self._resolve_time(produced_at)

        payload_obj = {
            "case_id": case_id,
            "source_message_id": source_message_id,
            "extractor": extractor,
            "method": method,
            "fields": [
                item.as_dict(FIELD_SPECS[name].value_type) for name, item in prepared
            ],
        }
        payload = canonical_json(payload_obj)
        proposal_id = self.ids.new("prop")

        rows: list[FieldRow] = []
        with self._transaction():
            self.conn.execute(
                "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id,"
                " extractor, method, produced_at, status, payload, fingerprint, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?)",
                (
                    proposal_id,
                    case_id,
                    source_message_id,
                    extractor,
                    method,
                    when,
                    payload,
                    fingerprint(payload_obj),
                    when,
                ),
            )
            for name, item in prepared:
                spec = FIELD_SPECS[name]
                field_id = self.ids.new("ifld")
                serialized = _serialize_value(item.value, spec.value_type)
                self.conn.execute(
                    "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id,"
                    " field_name, field_value, value_type, confidence, source_message_id,"
                    " source_span_start, source_span_end, status, value_sha256, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?, ?)",
                    (
                        field_id,
                        proposal_id,
                        case_id,
                        name,
                        serialized,
                        spec.value_type,
                        item.confidence,
                        source_message_id,
                        item.span_start,
                        item.span_end,
                        _value_hash(serialized, spec.value_type),
                        when,
                    ),
                )
                rows.append(self._get_field(field_id))
        return proposal_id, rows

    # -- mutation of the review columns only ------------------------------ #
    def update_field(
        self, intake_field_id: str, *, changes: Mapping[str, Any]
    ) -> FieldRow:
        """Change a stored field's *review* state.

        Any column outside :data:`MUTABLE_COLUMNS` raises ``IMMUTABLE_FIELD``:
        the extracted value, its type, its confidence and its provenance can
        never be rewritten through the service.
        """
        intake_field_id = require_ref(intake_field_id, "intake_field_id")
        self._get_field(intake_field_id)
        if not isinstance(changes, Mapping):
            raise IntakeError("changes harus Mapping", code=CODE_INVALID_FIELD_VALUE)
        illegal = tuple(sorted(key for key in changes if key not in MUTABLE_COLUMNS))
        if illegal:
            raise IntakeError(
                "field hasil ekstraksi bersifat immutable",
                code=CODE_IMMUTABLE_FIELD,
                intake_field_id=intake_field_id,
                columns=list(illegal),
                allowed=list(MUTABLE_COLUMNS),
            )
        if not changes:
            return self._get_field(intake_field_id)

        to_apply = dict(changes)
        status = to_apply.get("status")
        if status is not None and status not in FIELD_STATUSES:
            raise IntakeError(
                f"status tidak dikenal: {status!r}",
                code=CODE_INVALID_FIELD_VALUE,
                field="status",
            )
        if status is not None and status != STATUS_PROPOSED:
            for required in ("reviewed_by", "reviewed_at"):
                if not to_apply.get(required):
                    raise IntakeError(
                        f"{required} wajib diisi untuk status {status}",
                        code=CODE_INVALID_FIELD_VALUE,
                        field=required,
                    )
        if status == STATUS_REJECTED:
            reason = to_apply.get("rejection_reason")
            if not isinstance(reason, str) or len(reason.strip()) < 3:
                raise IntakeError(
                    "REJECT wajib disertai alasan (>= 3 karakter)",
                    code=CODE_MISSING_REVIEW_REASON,
                    intake_field_id=intake_field_id,
                )

        columns = ", ".join(f"{key} = ?" for key in to_apply)
        values = [to_apply[key] for key in to_apply]
        try:
            with self._transaction():
                self.conn.execute(
                    f"UPDATE intake_fields SET {columns} WHERE intake_field_id = ?",
                    (*values, intake_field_id),
                )
        except sqlite3.IntegrityError:
            # The DDL CHECK is the last line of defence; the rollback above
            # guarantees no half-written row is left behind.
            raise
        return self._get_field(intake_field_id)

    # -- review ----------------------------------------------------------- #
    def review(
        self,
        *,
        intake_field_id: str,
        decision: str,
        reviewer: str,
        reviewer_role: str,
        at: Any = None,
        reason: Optional[str] = None,
    ) -> FieldRow:
        """ACCEPT or REJECT a proposed field. One review per field, ever."""
        intake_field_id = require_ref(intake_field_id, "intake_field_id")
        row = self._get_field(intake_field_id)
        if row.status != STATUS_PROPOSED:
            raise IntakeError(
                f"field sudah direview ({row.status})",
                code=CODE_ALREADY_REVIEWED,
                intake_field_id=intake_field_id,
                status=row.status,
            )
        normalized = str(decision or "").strip().upper()
        if normalized not in DECISIONS:
            raise IntakeError(
                f"keputusan tidak dikenal: {decision!r}",
                code=CODE_INVALID_REVIEW_DECISION,
                decision=decision,
                allowed=list(DECISIONS),
            )
        reviewer = require_text(reviewer, "reviewer", max_len=64)
        # The role is recorded with the decision (not stored on the row, which
        # has no role column) so a caller cannot review "as someone else".
        require_text(reviewer_role, "reviewer_role", max_len=32)
        when = self._resolve_time(at)

        if normalized == "REJECT":
            if reason is None or not str(reason).strip():
                raise IntakeError(
                    "REJECT wajib disertai alasan",
                    code=CODE_MISSING_REVIEW_REASON,
                    intake_field_id=intake_field_id,
                )
            clean_reason = require_text(reason, "reason", max_len=500)
            if len(clean_reason) < 3:
                raise IntakeError(
                    "alasan penolakan minimal 3 karakter",
                    code=CODE_MISSING_REVIEW_REASON,
                    intake_field_id=intake_field_id,
                )
            changes: dict[str, Any] = {
                "status": STATUS_REJECTED,
                "reviewed_by": reviewer,
                "reviewed_at": when,
                "rejection_reason": clean_reason,
            }
        else:
            if row.python_value is None or row.confidence < ACCEPT_CONFIDENCE_MIN:
                raise IntakeError(
                    "nilai belum layak diterima (kosong atau confidence rendah)",
                    code=CODE_LOW_CONFIDENCE,
                    intake_field_id=intake_field_id,
                    confidence=row.confidence,
                    has_value=row.python_value is not None,
                )
            changes = {
                "status": STATUS_ACCEPTED,
                "reviewed_by": reviewer,
                "reviewed_at": when,
                "rejection_reason": None,
            }
        return self.update_field(intake_field_id, changes=changes)

    # -- reads ------------------------------------------------------------ #
    def pending(self, case_id: str) -> list[FieldRow]:
        """Still-unreviewed proposals for a case, oldest first."""
        case_id = require_ref(case_id, "case_id")
        rows = self.conn.execute(
            "SELECT * FROM intake_fields WHERE case_id = ? AND status = 'PROPOSED'"
            " ORDER BY created_at, intake_field_id",
            (case_id,),
        ).fetchall()
        return [_row_to_field(row) for row in rows]

    def accepted_facts(self, case_id: str) -> dict[str, object]:
        """ACCEPTED values converted to Python types (money stays minor units).

        Only ACCEPTED rows qualify — a PROPOSED or REJECTED field is invisible
        here. When two proposals accepted the same field, the most recently
        reviewed one wins (ascending scan, later rows overwrite).
        """
        case_id = require_ref(case_id, "case_id")
        rows = self.conn.execute(
            "SELECT * FROM intake_fields WHERE case_id = ? AND status = 'ACCEPTED'"
            " ORDER BY reviewed_at, intake_field_id",
            (case_id,),
        ).fetchall()
        facts: dict[str, object] = {}
        for row in rows:
            field_row = _row_to_field(row)
            facts[field_row.field_name] = field_row.python_value
        return facts

    def unknown_required(self, case_id: str) -> tuple[str, ...]:
        """Required fields with no ACCEPTED fact, in FIELD_SPECS order."""
        accepted = self.accepted_facts(case_id)
        return tuple(name for name in REQUIRED_FIELDS if name not in accepted)

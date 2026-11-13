"""Shared kernel primitives for Villa Agency OS.

Deterministic, dependency-free helpers used by every domain module:

* :class:`Money` — integer minor units + ISO-4217 currency (never floats, never
  binary rounding drift).
* UTC time helpers + an injectable :class:`Clock` (no hidden ``now()`` calls in
  tests).
* Identifier factory (deterministic mode → reproducible tests).
* Canonical JSON + SHA-256 helpers used by the audit hash chain and policy
  fingerprints.
* :class:`DomainError` hierarchy carrying a machine-readable ``code``.
* :func:`redact` / :func:`pseudonymize` so P2 data never reaches logs or a model.

Nothing in this module talks to a database, network, or the clock implicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "UTC",
    "MINOR_UNITS",
    "Money",
    "CurrencyMismatch",
    "InvalidMoney",
    "DomainError",
    "EntityNotFound",
    "IDEMPOTENT_REPLAY",
    "Clock",
    "SystemClock",
    "FrozenClock",
    "IdFactory",
    "SYSTEM_ACTOR",
    "utc_now",
    "to_iso",
    "parse_iso",
    "to_iso_date",
    "parse_iso_date",
    "nights_between",
    "local_date",
    "canonical_json",
    "sha256_hex",
    "fingerprint",
    "decimal_of",
    "decimal_to_str",
    "redact",
    "mask_sensitive_numbers",
    "pseudonymize",
    "require_ref",
    "require_text",
    "MONEY_MAX_MINOR",
    "DEFAULT_AGENCY_ID",
    "AGENCY_ENV_VAR",
    "resolve_agency_id",
]

UTC = timezone.utc

#: ISO-4217 currencies and their number of minor units (exponent).
#: Only currencies listed here are accepted — a typo'd currency must fail, never
#: silently round to whole units.
MINOR_UNITS: Mapping[str, int] = {
    "IDR": 0,
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AUD": 2,
    "NZD": 2,
    "CAD": 2,
    "CHF": 2,
    "SGD": 2,
    "MYR": 2,
    "THB": 2,
    "PHP": 2,
    "INR": 2,
    "CNY": 2,
    "HKD": 2,
    "TWD": 2,
    "AED": 2,
    "SAR": 2,
    "ZAR": 2,
    "BRL": 2,
    "MXN": 2,
    "TRY": 2,
    "PLN": 2,
    "SEK": 2,
    "NOK": 2,
    "DKK": 2,
    "CZK": 2,
}

#: Hard ceiling for a single monetary amount (10^15 minor units). Anything above
#: is a data error, not a booking.
MONEY_MAX_MINOR = 10**15

#: Synthetic actor used for kernel-originated audit rows.
SYSTEM_ACTOR = "system:kernel"

_SHAPE_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}_[A-Za-z0-9.\-]{2,80}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Grammar of an opaque reference token (``booking_id``, ``unit_id``, ...).
#: References travel into SQL parameters, ledger rows, log lines, prompts and
#: file names, so path/quote/shell metacharacters and leading punctuation are a
#: caller bug rather than an identifier. Unquoted, single line, alphanumeric
#: first character, then a conservative safe set.
_REF_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]*")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DomainError(Exception):
    """Base class for every deliberate, business-level failure.

    ``code`` is the machine-readable contract used by callers and tests — never
    parse the human message.
    """

    code = "DOMAIN_ERROR"

    def __init__(self, message: str, code: Optional[str] = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details: dict[str, Any] = {k: v for k, v in details.items() if v is not None}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:  # pragma: no cover - formatting only
        if not self.details:
            return f"[{self.code}] {self.message}"
        shown = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
        return f"[{self.code}] {self.message} ({shown})"


class EntityNotFound(DomainError):
    code = "ENTITY_NOT_FOUND"


class CurrencyMismatch(DomainError):
    code = "CURRENCY_MISMATCH"


class InvalidMoney(DomainError):
    code = "INVALID_MONEY"


class IdempotentReplay(DomainError):
    """Raised when the same idempotency key is reused with a different payload."""

    code = "IDEMPOTENT_REPLAY"


IDEMPOTENT_REPLAY = IdempotentReplay.code


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
def _currency_info(currency: str) -> tuple[str, int]:
    """Return ``(normalised_code, exponent)`` for a supported ISO-4217 currency.

    The exponent is the number of decimal places (IDR/JPY: 0, USD/EUR: 2) — it is
    deliberately *not* the amount: mixing the two is the classic money bug.
    """
    code = (currency or "").strip().upper()
    if code not in MINOR_UNITS:
        raise InvalidMoney(
            f"unsupported or unknown ISO-4217 currency: {currency!r}",
            currency=currency,
            supported=sorted(MINOR_UNITS)[:8],
        )
    return code, MINOR_UNITS[code]


def decimal_of(value: Any) -> Decimal:
    """Coerce ``str``/``int``/``Decimal`` to :class:`Decimal`, refusing floats.

    Floats are rejected on purpose: ``0.1 + 0.2 != 0.3`` must never reach a quote.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidMoney("non-finite decimal", value=str(value))
        return value
    if isinstance(value, bool):
        raise InvalidMoney("bool is not a number", value=repr(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidMoney("empty numeric string")
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise InvalidMoney(f"not a valid decimal: {value!r}", value=value) from exc
        if not dec.is_finite():
            raise InvalidMoney("non-finite decimal", value=value)
        return dec
    raise InvalidMoney(
        f"unsupported numeric type {type(value).__name__} (float is forbidden)",
        value=repr(value),
    )


def decimal_to_str(value: Decimal, places: Optional[int] = None) -> str:
    """Canonical string form.

    ``places=None`` → shortest exact form (trailing zeros stripped).
    ``places=N``    → exactly N decimal places (never stripped: a price of
    ``10.50`` must not print as ``10.5``).
    """
    dec = decimal_of(value)
    if places is None:
        text = format(dec.normalize(), "f")
        return "0" if text in ("-0", "") else text
    quantised = dec.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(quantised, "f")
    return "0" if text in ("-0", "") else text


@dataclass(frozen=True, order=False)
class Money:
    """A monetary amount in integer minor units of a known ISO-4217 currency."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        code, minor = _currency_info(self.currency)
        object.__setattr__(self, "currency", code)
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise InvalidMoney(
                "amount_minor must be an int (minor units); use Money.from_major for decimals",
                amount_minor=repr(self.amount_minor),
            )
        if abs(self.amount_minor) > MONEY_MAX_MINOR:
            raise InvalidMoney("amount outside supported range", amount_minor=self.amount_minor)
        object.__setattr__(self, "_exponent", minor)

    # -- construction ------------------------------------------------------ #
    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    @classmethod
    def from_minor(cls, amount_minor: int, currency: str) -> "Money":
        return cls(amount_minor, currency)

    @classmethod
    def from_major(cls, amount: Any, currency: str, rounding: str = ROUND_HALF_UP) -> "Money":
        """Build from a major-unit value (``"1500000.00"``, ``Decimal("9.5")``)."""
        code, exponent = _currency_info(currency)
        dec = decimal_of(amount)
        scaled = dec.scaleb(exponent).quantize(Decimal(1), rounding=rounding)
        return cls(int(scaled), code)

    # -- conversion -------------------------------------------------------- #
    @property
    def exponent(self) -> int:
        """Decimal places for this currency (0 for IDR, 2 for USD)."""
        return int(getattr(self, "_exponent", MINOR_UNITS[self.currency]))

    @property
    def minor_units(self) -> int:
        """The amount expressed in minor units — the canonical integer form."""
        return self.amount_minor

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.exponent)

    def to_major_str(self, places: Optional[int] = None) -> str:
        return decimal_to_str(self.to_decimal(), self.exponent if places is None else places)

    def to_dict(self) -> dict[str, Any]:
        return {"amount_minor": self.amount_minor, "currency": self.currency}

    # -- arithmetic -------------------------------------------------------- #
    def _check(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise InvalidMoney(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(
                "cannot combine different currencies",
                left=self.currency,
                right=other.currency,
            )

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount_minor, self.currency)

    def __mul__(self, factor: Any) -> "Money":
        return self.scale(factor)

    __rmul__ = __mul__

    def scale(self, factor: Any, rounding: str = ROUND_HALF_UP) -> "Money":
        """Multiply by a rate/factor (int, str or Decimal) with explicit rounding."""
        dec = decimal_of(factor)
        scaled = (Decimal(self.amount_minor) * dec).quantize(Decimal(1), rounding=rounding)
        return Money(int(scaled), self.currency)

    def pct(self, percent: Any, rounding: str = ROUND_HALF_UP) -> "Money":
        """Return ``percent`` % of this amount (``"11"`` → 11 %)."""
        return self.scale(decimal_of(percent) / Decimal(100), rounding=rounding)

    def split(self, percent: Any) -> tuple["Money", "Money"]:
        """Split into (part, remainder) so that part + remainder == self exactly."""
        part = self.pct(percent)
        return part, self - part

    # -- predicates -------------------------------------------------------- #
    @property
    def is_zero(self) -> bool:
        return self.amount_minor == 0

    @property
    def is_negative(self) -> bool:
        return self.amount_minor < 0

    @property
    def is_positive(self) -> bool:
        return self.amount_minor > 0

    def __bool__(self) -> bool:
        return self.amount_minor != 0

    def __str__(self) -> str:
        return f"{self.currency} {self.to_major_str()}"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"Money({self.amount_minor}, {self.currency!r})"


class _MoneyEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # pragma: no cover - exercised via canonical_json
        if isinstance(o, Money):
            return o.to_dict()
        if isinstance(o, Decimal):
            return decimal_to_str(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
class Clock(Protocol):
    """Anything exposing an aware-UTC ``now()``."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """Real clock. Only production code paths should use this."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def today(self) -> date:
        return self.now().date()


@dataclass
class FrozenClock:
    """Test clock. Time only moves when a test moves it."""

    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self.current = self.current.astimezone(UTC)

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        """Calendar date in UTC — matches :class:`SystemClock.today`."""
        return self.current.date()

    @classmethod
    def at(cls, iso_text: str) -> "FrozenClock":
        return cls(parse_iso(iso_text))

    def advance(self, **delta: float) -> datetime:
        self.current = self.current + timedelta(**delta)
        return self.current

    def set(self, when: datetime | str) -> datetime:
        self.current = parse_iso(when) if isinstance(when, str) else when.astimezone(UTC)
        return self.current


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def to_iso(when: datetime) -> str:
    """Canonical timestamp: UTC, seconds precision, ``+00:00`` offset."""
    if not isinstance(when, datetime):
        raise DomainError(
            f"expected a datetime, got {type(when).__name__}",
            code="INVALID_TIMESTAMP",
            got=type(when).__name__,
        )
    if when.tzinfo is None:
        raise DomainError(
            "naive datetimes are not allowed; attach a timezone",
            code="NAIVE_TIMESTAMP",
        )
    return when.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive input is rejected (fail closed)."""
    if not isinstance(text, str) or not text.strip():
        raise DomainError("empty timestamp", code="INVALID_TIMESTAMP")
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise DomainError(f"not an ISO-8601 timestamp: {text!r}", code="INVALID_TIMESTAMP") from exc
    if parsed.tzinfo is None:
        raise DomainError(
            f"naive timestamp not accepted: {text!r}",
            code="NAIVE_TIMESTAMP",
        )
    return parsed.astimezone(UTC)


def to_iso_date(value: date) -> str:
    if isinstance(value, datetime):
        raise DomainError("expected a date, got datetime", code="INVALID_DATE")
    if not isinstance(value, date):
        raise DomainError(
            f"expected a date, got {type(value).__name__}",
            code="INVALID_DATE",
            got=type(value).__name__,
        )
    return value.isoformat()


def parse_iso_date(text: str) -> date:
    if not isinstance(text, str) or not text.strip():
        raise DomainError("empty date", code="INVALID_DATE")
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise DomainError(f"not an ISO-8601 date: {text!r}", code="INVALID_DATE") from exc


def nights_between(check_in: date, check_out: date) -> int:
    """Number of nights; raises for a non-positive stay (check-out > check-in)."""
    if isinstance(check_in, datetime) or isinstance(check_out, datetime):
        raise DomainError("use date objects for stay boundaries", code="INVALID_STAY_RANGE")
    if not isinstance(check_in, date) or not isinstance(check_out, date):
        raise DomainError("stay boundaries must be dates", code="INVALID_STAY_RANGE")
    nights = (check_out - check_in).days
    if nights <= 0:
        raise DomainError(
            "check_out must be strictly after check_in",
            code="INVALID_STAY_RANGE",
            check_in=to_iso_date(check_in),
            check_out=to_iso_date(check_out),
        )
    return nights


def local_date(when: datetime, tz_name: str) -> date:
    """Calendar date of ``when`` in the property's IANA timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(f"unknown timezone: {tz_name!r}", code="INVALID_TIMEZONE", timezone=tz_name) from exc
    return when.astimezone(tz).date()


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #
#: Positions of a minted token that always carry a hex letter. A sixteen-character
#: hex token can be read as a direct identifier by four neighbouring detectors
#: (``gates.IDENTIFIER_PATTERNS``, ``redact()``, ``dashboard.redact_pii``,
#: ``payments.contains_card_data``) about once in 200 ids — which refuses a real
#: document, a real payment record or a log line (ADR-0063). Two forced letters
#: eight characters apart make a run longer than seven digits impossible, so no
#: detector can match inside a token; the token stays sixteen hex characters
#: (entropy 2^61 instead of 2^64).
_TOKEN_LETTER_POSITIONS = (4, 12)
_TOKEN_LETTERS = "abcdef"


def _identifier_safe_hex() -> str:
    """Sixteen hex characters that no identifier detector reads as an identifier."""
    token = list(secrets.token_hex(8))
    for position in _TOKEN_LETTER_POSITIONS:
        token[position] = secrets.choice(_TOKEN_LETTERS)
    return "".join(token)


class IdFactory:
    """Generates opaque record ids.

    ``deterministic=True`` yields reproducible ids (``hold_t000001``) so tests and
    audit chains are stable; production uses ``secrets.token_hex``.
    """

    def __init__(self, deterministic: bool = False, seed: str = "t") -> None:
        self.deterministic = deterministic
        self.seed = seed
        self._counter = 0

    def new(self, prefix: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", prefix):
            raise ValueError(f"invalid id prefix: {prefix!r}")
        self._counter += 1
        if self.deterministic:
            token = f"{self.seed}{self._counter:06d}"
        else:
            token = _identifier_safe_hex()
        return f"{prefix}_{token}"


def restriction_payload(value: Any) -> dict[str, Any]:
    """The restriction payload of an availability row, as a plain mapping.

    Accepts stored JSON text or an already-parsed mapping, and returns {} for
    anything it cannot read: an unparseable restriction is not one a caller may
    rely on, and every reader of it denies in that case anyway.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if value is None or str(value).strip() == "":
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def require_ref(value: Any, field_name: str = "ref", max_len: int = 128) -> str:
    """Validate an identifier/reference: non-empty, printable, bounded."""
    if not isinstance(value, str):
        raise DomainError(
            f"{field_name} must be a string",
            code="INVALID_FIELD",
            field=field_name,
            got=type(value).__name__,
        )
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        raise DomainError(f"{field_name} is required", code="MISSING_FIELD", field=field_name)
    if len(text) > max_len:
        raise DomainError(
            f"{field_name} exceeds {max_len} characters",
            code="INVALID_FIELD",
            field=field_name,
            length=len(text),
        )
    if _CONTROL_CHARS.search(text):
        raise DomainError(
            f"{field_name} contains control characters",
            code="INVALID_FIELD",
            field=field_name,
        )
    if any(ch.isspace() for ch in text):
        # Identifiers are opaque tokens: an internal space means the caller passed
        # free text where a reference belongs ("hold ABC" is not an id).
        raise DomainError(
            f"{field_name} must not contain whitespace",
            code="INVALID_FIELD",
            field=field_name,
        )
    if not _REF_TOKEN.fullmatch(text):
        raise DomainError(
            f"{field_name} is not an opaque reference token",
            code="INVALID_FIELD",
            field=field_name,
        )
    return text


#: Environment variable an operator uses to select the agency a process serves.
AGENCY_ENV_VAR = "VILLA_AGENCY_ID"

#: The agency id every pre-Wave-1 row already carries. Single-tenant deployments keep
#: working untouched; a real multi-tenant deployment sets the variable below.
DEFAULT_AGENCY_ID = "agency_default"


def resolve_agency_id(explicit: Optional[str] = None, *, env: Optional[Mapping[str, str]] = None) -> str:
    """Which agency this process serves: an explicit value, else ``VILLA_AGENCY_ID``.

    Runtime agency selection is deliberately *not* an authentication mechanism — it
    decides whose rows a process may touch, while the identity/RBAC layer decides who
    the caller is. Keeping the two apart means a mis-set variable narrows access
    (the process sees an empty tenant) instead of widening it.

    A malformed value is refused rather than swallowed: silently falling back to
    ``agency_default`` would point a misconfigured process at another tenant's rows,
    which is exactly the failure this function exists to prevent.
    """
    if explicit is not None:
        return require_ref(explicit, "agency_id")
    source = os.environ if env is None else env
    configured = source.get(AGENCY_ENV_VAR)
    if configured is None or not str(configured).strip():
        return DEFAULT_AGENCY_ID
    return require_ref(str(configured), "agency_id")


def require_text(value: Any, field_name: str = "text", max_len: int = 2000, allow_empty: bool = False) -> str:
    """Validate free text (policy clauses, reasons). Normalises, never truncates."""
    if value is None:
        if allow_empty:
            return ""
        raise DomainError(f"{field_name} is required", code="MISSING_FIELD", field=field_name)
    if not isinstance(value, str):
        raise DomainError(f"{field_name} must be a string", code="INVALID_FIELD", field=field_name)
    text = unicodedata.normalize("NFC", value).strip()
    if not text and not allow_empty:
        raise DomainError(f"{field_name} must not be empty", code="MISSING_FIELD", field=field_name)
    if len(text) > max_len:
        raise DomainError(
            f"{field_name} exceeds {max_len} characters",
            code="INVALID_FIELD",
            field=field_name,
            length=len(text),
        )
    if _CONTROL_CHARS.search(text):
        raise DomainError(f"{field_name} contains control characters", code="INVALID_FIELD", field=field_name)
    return text


# --------------------------------------------------------------------------- #
# Canonical serialisation & hashing
# --------------------------------------------------------------------------- #
def _default(o: Any) -> Any:
    if isinstance(o, Money):
        return o.to_dict()
    if isinstance(o, Decimal):
        return decimal_to_str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8 kept."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def sha256_hex(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if not isinstance(part, str):
            raise TypeError("sha256_hex only accepts str parts")
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")  # unit separator: prevents concatenation ambiguity
    return digest.hexdigest()


def fingerprint(obj: Any, *, domain: str = "villa-agency-os") -> str:
    """Stable content hash (policy snapshots, quote reproducibility)."""
    return sha256_hex(domain, canonical_json(obj))


# --------------------------------------------------------------------------- #
# Privacy helpers (P2/P3 data must never reach logs or a model)
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?62|0)8[1-9][0-9]{6,11}")
_NIK_RE = re.compile(r"\b\d{16}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")
#: A run this long, containing at least one hex letter, is a digest — see ``redact``.
_HEX_RUN_RE = re.compile(r"[0-9a-f]+")
HEX_DIGEST_MIN = 32


def redact(text: Any) -> str:
    """Mask direct identifiers (email, Indonesian mobile, NIK/card-like digits).

    Use on anything destined for a log line, an error message, or an LLM prompt.

    A hex digest survives: it is irreversible, it carries no direct identifier, and
    masking it would destroy the integrity values that have to be compared byte for
    byte. A sha256 that happened to contain ``08`` followed by digits was stored in
    the audit chain as ``[PHONE]``, so a healthy anchor file was reported as tampered
    and publication refused — about once in every 250 anchors. Only runs *with* a hex
    letter qualify, so a long number (card, NIK) is still masked.
    """
    if text is None:
        return ""
    out = str(text)
    stashed: list[str] = []

    def _stash(match: "re.Match[str]") -> str:
        token = match.group(0)
        if len(token) >= HEX_DIGEST_MIN and _has_hex_letter(token):
            stashed.append(token)
            return f"\x00D{len(stashed) - 1}\x00"
        return token

    protected = _HEX_RUN_RE.sub(_stash, out)
    cleaned = _mask_identifiers(protected)
    for index, token in enumerate(stashed):
        cleaned = cleaned.replace(f"\x00D{index}\x00", token)
    return cleaned


#: A card number as people type it: 13-19 digits, optionally grouped by spaces or
#: dashes ("4111 1111 1111 1111"). Only a Luhn-valid run with two or more distinct
#: digits is masked, so amounts and dates survive (audit 2026-11-13, finding K1).
_CARD_RUN_RE = re.compile(r"(?<![\w])\d(?:[ -]?\d){12,18}(?![\w])")
#: A security code is only recognisable by the word in front of it.
_CVV_RE = re.compile(
    r"\b(cvv2?|cvc2?|cvn|csc|cid|kode\s+keamanan|security\s+code)\b([^\d\n]{0,12})\d{3,4}\b",
    re.IGNORECASE,
)
#: A passport number is likewise only recognisable by its keyword; the captured
#: value must contain a digit so "passport please" is left alone.
_PASSPORT_RE = re.compile(
    r"\b(passport|paspor|pasport|passeport)\b"
    r"([^A-Za-z0-9\n]{0,12}(?:(?:no|nr|number|nomor|num)\.?[^A-Za-z0-9\n]{0,4})?)"
    r"(?=[A-Za-z]*\d)[A-Za-z0-9]{6,9}\b",
    re.IGNORECASE,
)
#: An international or local phone number written with separators
#: ("+62 812-3456-7890", "(0361) 123 456"). The compact forms are covered by
#: ``_PHONE_RE`` and the digit-run patterns; this one needs 9-15 digits and a
#: leading ``+``, ``0`` or ``(`` so that dates and amounts are never read as phones.
_PHONE_SEPARATED_RE = re.compile(r"(?<![\w+])(?:\+|\(?0)[\d ().\-]{7,22}\d(?![\w])")


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask_card(match: "re.Match[str]") -> str:
    digits = re.sub(r"[ -]", "", match.group(0))
    if len(set(digits)) >= 2 and _luhn_valid(digits):
        return "[CARD]"
    return match.group(0)


def _mask_separated_phone(match: "re.Match[str]") -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 9 <= len(digits) <= 15:
        return "[PHONE]"
    return match.group(0)


def mask_sensitive_numbers(text: str) -> str:
    """Mask P3/P4 values that free text carries: card numbers, security codes,
    passport numbers and separated phone numbers.

    Shared by :func:`redact` and ``dashboard.redact_pii`` so that a guest who types
    a card number into a website inquiry never has it stored, logged or shown —
    the stored copy of a P2 message is the redacted one (``cases._body_fields``).
    """
    out = _CARD_RUN_RE.sub(_mask_card, text)
    out = _CVV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[CVV]", out)
    out = _PASSPORT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[PASSPORT]", out)
    out = _PHONE_SEPARATED_RE.sub(_mask_separated_phone, out)
    return out


def _mask_identifiers(text: str) -> str:
    out = _EMAIL_RE.sub("[EMAIL]", text)
    out = mask_sensitive_numbers(out)
    out = _PHONE_RE.sub("[PHONE]", out)
    out = _NIK_RE.sub("[ID_NUMBER]", out)
    out = _LONG_DIGITS_RE.sub("[DIGITS]", out)
    return out


def _has_hex_letter(token: str) -> bool:
    return any(char in "abcdef" for char in token)


def pseudonymize(value: Any, salt: str = "villa-agency-os") -> str:
    """Stable pseudonym (``guest_9f2c1a...``) for P2 references sent to a model."""
    if value is None:
        raise ValueError("cannot pseudonymize None")
    return "guest_" + sha256_hex(salt, str(value))[:16]

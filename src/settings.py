"""Agency settings: the values a **tenant** owns, and whether a tenant is ready to go live.

Why this module exists (ADR-0037, decision D17)
----------------------------------------------
The platform's own controls are owner policy values: the deny registry, approval thresholds,
the MFA role set. Everything a *tenant* brings — its payment provider, its WhatsApp number,
where its PBJT rate comes from, who is on call — differs per agency and must never become a
platform-wide default. This is the store for those values, and :meth:`AgencySettings.readiness`
answers the question D17 asks before real money: **may this agency go live yet?**

Three rules, each one a way this goes wrong otherwise:

* **The catalogue is closed.** An unknown key is refused, not stored. A settings table that
  accepts anything becomes a place where a typo silently does nothing — the tenant believes it
  configured something that no code reads.
* **A value is validated at the door.** A WhatsApp number that is not a number fails on write,
  not when a guest message bounces. Validation lives with the key, in one table, next to the
  description of what the key is for.
* **Every write is in the audit chain.** The row holds the value in force; the *history* is
  `audit_events` (actor, role, before, after, reason, timestamp). "Who changed the number, and
  what was it before" is answered by the chain, not by a second copy of the data.

What this module deliberately does not do: it does not decide *who* may change a value beyond a
role gate (a tenant's own owner, GM, or system admin), and it does not implement the tiered
FAM → GM approval for tax values (D5) — that ladder belongs to the tax flow, and half-building
it here would hide which approvals actually happened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from audit import append_event
from primitives import (
    Clock,
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    require_ref,
    to_iso,
)

__all__ = [
    "AgencySettings",
    "ReadinessItem",
    "ReadinessReport",
    "SETTING_DEFS",
    "READINESS_FILTERS",
    "SETTING_ROLES",
    "SettingDef",
    "SettingsError",
    "InvalidSettingKey",
    "InvalidSettingValue",
    "SettingRoleRefused",
    "required_keys",
]

#: Who may write an agency setting. A tenant's own authority — not a reservation desk, not the
#: AI, and not any role that only reads. Narrower than "any known role" on purpose.
SETTING_ROLES: tuple[str, ...] = ("owner", "gm", "system_admin")

_PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")

#: What "ready to sell" means per table, as literals this module owns. Properties have no
#: ACTIVE status: the schema's statuses are SELLABLE / SELLABLE_WITH_REVIEW / NOT_SELLABLE /
#: SUSPENDED, so readiness counts **SELLABLE** (a review-needed property is not ready to sell).
#: Units do have ACTIVE. A predicate the caller could pass would be a way to make the check
#: answer whatever the caller wanted.
READINESS_FILTERS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "properties": ("status", "SELLABLE"),
        "units": ("status", "ACTIVE"),
    }
)


class SettingsError(DomainError):
    code = "SETTINGS_ERROR"


class InvalidSettingKey(SettingsError):
    """The key is not in the catalogue: it would store a value nothing reads."""

    code = "INVALID_SETTING_KEY"


class InvalidSettingValue(SettingsError):
    code = "INVALID_SETTING_VALUE"


class SettingRoleRefused(SettingsError):
    code = "SETTING_ROLE_REFUSED"


def _text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise InvalidSettingValue("a setting needs a value")
    return text


def _phone(value: Any) -> str:
    text = _text(value).replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(text):
        raise InvalidSettingValue(
            "a WhatsApp number must be digits with an optional leading +, 8 to 15 long",
            got=repr(value),
        )
    return text


def _positive_int(value: Any) -> str:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidSettingValue("this setting must be a whole number", got=repr(value)) from exc
    if number < 1:
        raise InvalidSettingValue("this setting must be at least 1", got=number)
    return str(number)


def _text_list(value: Any) -> str:
    """A non-empty list of non-empty strings, stored as canonical JSON."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise InvalidSettingValue("this setting must be a list", got=repr(value)) from exc
    if not isinstance(value, (list, tuple)) or not value:
        raise InvalidSettingValue("this setting must be a non-empty list", got=repr(value))
    cleaned = [str(item).strip() for item in value]
    if any(not item for item in cleaned):
        raise InvalidSettingValue("every entry must be a non-empty name", got=repr(value))
    return canonical_json(cleaned)


@dataclass(frozen=True)
class SettingDef:
    """One key a tenant owns: what it means, how it is validated, and whether go-live needs it."""

    key: str
    description: str
    #: ``required_before_go_live`` is D17's list: without these, an agency may not take real
    #: guests and real money. The rest are refinements a tenant may add later.
    required_before_go_live: bool
    validate: Callable[[Any], str]
    example: str


#: The closed catalogue. Adding a key is a code change with a test, and that is deliberate: a
#: tenant filling a field no code reads is worse than the field not existing.
SETTING_DEFS: Mapping[str, SettingDef] = MappingProxyType(
    {
        "payment.provider": SettingDef(
            key="payment.provider",
            description="Payment provider this agency uses (D1). The tenant chooses it.",
            required_before_go_live=True,
            validate=_text,
            example="midtrans",
        ),
        "payment.rails": SettingDef(
            key="payment.rails",
            description="Payment rails the tenant enabled for this agency (D1/ADR-0012).",
            required_before_go_live=True,
            validate=_text_list,
            example='["CARD", "BANK_TRANSFER"]',
        ),
        "payment.hosted_page": SettingDef(
            key="payment.hosted_page",
            description="Whether payment uses the provider hosted page (D12: no card data is stored).",
            required_before_go_live=False,
            validate=_text,
            example="true",
        ),
        "messaging.whatsapp_number": SettingDef(
            key="messaging.whatsapp_number",
            description="This agency WhatsApp Business number (D6). Templates come from the platform.",
            required_before_go_live=True,
            validate=_phone,
            example="+628123456789",
        ),
        "messaging.feedback_channel": SettingDef(
            key="messaging.feedback_channel",
            description="Official channel named to guests for feedback (email, form, phone).",
            required_before_go_live=False,
            validate=_text,
            example="email",
        ),
        "tax.pbjt_source": SettingDef(
            key="tax.pbjt_source",
            description=(
                "Where this agency PBJT rate comes from, per regency/city (D5). The rate itself is "
                "validated by the Finance & Accounting Manager and approved by the GM/CEO."
            ),
            required_before_go_live=True,
            validate=_text,
            example="peraturan daerah kabupaten Badung",
        ),
        "operations.oncall_primary": SettingDef(
            key="operations.oncall_primary",
            description="Who must respond when an alarm fires during working hours (D13).",
            required_before_go_live=True,
            validate=_text,
            example="Mr. TS",
        ),
        "operations.oncall_escalation": SettingDef(
            key="operations.oncall_escalation",
            description="Who the alarm escalates to when the primary does not respond (D13).",
            required_before_go_live=False,
            validate=_text,
            example="Mr. CEO",
        ),
        "legal.review_weekday": SettingDef(
            key="legal.review_weekday",
            description="Weekday of this agency monthly legal review, done by GM/CEO and Owner (D8).",
            required_before_go_live=False,
            validate=_text,
            example="Jumat",
        ),
        "retention.data_years": SettingDef(
            key="retention.data_years",
            description=(
                "How long data is kept before it may be deleted (D9). The platform default is 7 years; "
                "the number belongs to the tenant, and deletion runs with GM/CEO and Owner approval."
            ),
            required_before_go_live=False,
            validate=_positive_int,
            example="7",
        ),
    }
)

REQUIRED_KEYS: tuple[str, ...] = tuple(
    sorted(key for key, definition in SETTING_DEFS.items() if definition.required_before_go_live)
)


def required_keys() -> tuple[str, ...]:
    """The keys an agency must fill before it may take real guests and real money (D17)."""
    return REQUIRED_KEYS


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    description: str
    required: bool
    value: Optional[str]
    filled_by: Optional[str]
    filled_at: Optional[str]

    @property
    def is_filled(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class ReadinessReport:
    """What one agency still owes before go-live, with the exact keys named."""

    agency_id: str
    items: tuple[ReadinessItem, ...]
    missing_required: tuple[str, ...]
    sellable_properties: int
    active_units: int

    @property
    def ready(self) -> bool:
        return not self.missing_required and self.sellable_properties > 0 and self.active_units > 0

    @property
    def blockers(self) -> tuple[str, ...]:
        """Every reason this agency may not go live, in plain words."""
        reasons = [f"required setting not filled: {key}" for key in self.missing_required]
        if self.sellable_properties < 1:
            reasons.append("no property in SELLABLE state")
        if self.active_units < 1:
            reasons.append("no unit in ACTIVE state")
        return tuple(reasons)


def _is_sqlite(conn: Any) -> bool:
    return type(conn).__module__.split(".", 1)[0] == "sqlite3"


@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Join the caller's transaction, or open one."""
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


class AgencySettings:
    """The single writer of ``agency_settings`` for one agency."""

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = "agency_default",
    ) -> None:
        if not _is_sqlite(conn):
            raise SettingsError("this store writes in the caller's transaction; it needs sqlite")
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)

    # -- writing ---------------------------------------------------------- #
    def set(
        self,
        key: str,
        value: Any,
        *,
        actor: str,
        actor_role: str,
        reason: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> ReadinessItem:
        """Store one tenant value, with the actor and the change in the audit chain."""
        definition = SETTING_DEFS.get(str(key).strip())
        if definition is None:
            raise InvalidSettingKey(
                "this key is not in the catalogue: a value here would be read by nothing",
                key=repr(key),
                known=sorted(SETTING_DEFS),
            )
        actor = require_ref(actor, "actor", max_len=128)
        role = require_ref(actor_role, "actor_role", max_len=64)
        if role not in SETTING_ROLES:
            raise SettingRoleRefused(
                "this role may not change an agency setting",
                role=role,
                allowed=list(SETTING_ROLES),
            )
        try:
            stored = definition.validate(value)
        except InvalidSettingValue as exc:
            raise InvalidSettingValue(
                f"{definition.key}: {exc}", key=definition.key, got=repr(value)
            ) from exc
        moment = self._now(at)
        previous = self._row(definition.key)
        before = None if previous is None else {"value": previous["value"], "revision": previous["revision"]}

        with _write_transaction(self._conn):
            if previous is None:
                self._conn.execute(
                    "INSERT INTO agency_settings (setting_id, agency_id, setting_key, value,"
                    " revision, updated_by, updated_role, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        self._ids.new("setting"),
                        self.agency_id,
                        definition.key,
                        stored,
                        actor,
                        role,
                        to_iso(moment),
                    ),
                )
            else:
                self._conn.execute(
                    "UPDATE agency_settings SET value = ?, revision = revision + 1, updated_by = ?,"
                    " updated_role = ?, updated_at = ? WHERE agency_id = ? AND setting_key = ?",
                    (
                        stored,
                        actor,
                        role,
                        to_iso(moment),
                        self.agency_id,
                        definition.key,
                    ),
                )
            append_event(
                self._conn,
                clock=self._clock,
                ids=self._ids,
                action="setting.set",
                entity_type="agency_setting",
                entity_id=definition.key,
                actor=actor,
                actor_role=role,
                before=before,
                after={"value": stored},
                reason=reason or f"set {definition.key} for agency {self.agency_id}",
            )
        return self.status(definition.key)

    def _now(self, at: Optional[datetime]) -> datetime:
        if at is None:
            return self._clock.now()
        if at.tzinfo is None:
            raise SettingsError("at must carry a timezone", code="INVALID_TIMESTAMP")
        return at

    # -- reading ----------------------------------------------------------- #
    def _row(self, key: str) -> Optional[Mapping[str, Any]]:
        row = self._conn.execute(
            "SELECT setting_id, setting_key, value, revision, updated_by, updated_role, updated_at"
            " FROM agency_settings WHERE agency_id = ? AND setting_key = ?",
            (self.agency_id, key),
        ).fetchone()
        return None if row is None else dict(row)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """The stored value, or ``default``. An unknown key is still refused."""
        if str(key).strip() not in SETTING_DEFS:
            raise InvalidSettingKey(
                "this key is not in the catalogue", key=repr(key), known=sorted(SETTING_DEFS)
            )
        row = self._row(str(key).strip())
        return default if row is None else str(row["value"])

    def status(self, key: str) -> ReadinessItem:
        definition = SETTING_DEFS.get(str(key).strip())
        if definition is None:
            raise InvalidSettingKey("this key is not in the catalogue", key=repr(key))
        row = self._row(definition.key)
        return ReadinessItem(
            key=definition.key,
            description=definition.description,
            required=definition.required_before_go_live,
            value=None if row is None else str(row["value"]),
            filled_by=None if row is None else str(row["updated_by"]),
            filled_at=None if row is None else str(row["updated_at"]),
        )

    def readiness(self) -> ReadinessReport:
        """Everything this agency still owes before it may take real guests and real money."""
        items = tuple(self.status(key) for key in sorted(SETTING_DEFS))
        missing = tuple(item.key for item in items if item.required and not item.is_filled)
        return ReadinessReport(
            agency_id=self.agency_id,
            items=items,
            missing_required=missing,
            sellable_properties=self._count_ready("properties"),
            active_units=self._count_ready("units"),
        )

    def _count_ready(self, table: str) -> int:
        """How many rows of ``table`` are ready to sell for this agency.

        The table, column and value are literals of :data:`READINESS_FILTERS`, never caller
        input, so the f-string carries no injection. An unknown table is refused rather than
        guessed.
        """
        predicate = READINESS_FILTERS.get(table)
        if predicate is None:  # pragma: no cover - defensive
            raise SettingsError("unknown readiness table", table=table)
        column, value = predicate
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE agency_id = ? AND {column} = ?",
            (self.agency_id, value),
        ).fetchone()
        return int(row["n"])


def readiness_for(conn: sqlite3.Connection, agency_id: str, *, clock: Optional[Clock] = None) -> ReadinessReport:
    """Readiness for one agency, for callers that only read (preflight, the report)."""
    return AgencySettings(conn, clock=clock, agency_id=agency_id).readiness()

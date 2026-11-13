"""CRM: guest master, inquiry intake and confirmed trip requirements (v1.1 §5, §6, §50 WAVE 3).

Scope of this slice: a **guest** with an identity, an **inquiry** with a human-readable
`INQ-YYYY-NNNNN` reference, and the **confirmed requirements** the inquiry will later be
matched and priced against. Conversations, tasks, matching and option sets are the next
slice of WAVE 3 and are deliberately absent here.

Reuse, not forking: an inquiry *is* the existing case spine. `CaseService.open_case`
stays the only writer that creates a case, and this service composes it and then attaches
the guest link and the reference. Writing case rows from a second place would give the
platform two writers for one lifecycle and two chances to drift.

The AI-extracted → verified step is enforced, not assumed. A requirement whose source is
`ACCEPTED_INTAKE` must cite an intake field that is genuinely `ACCEPTED` **and belongs to
the same case**; otherwise extraction would be dressing itself up as confirmed truth.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from primitives import (
    DomainError,
    IdFactory,
    SystemClock,
    parse_iso,
    require_ref,
    require_text,
    to_iso,
)
from audit import append_event
from fx import GUEST_CURRENCIES
from references import REFERENCE_PREFIXES, ReferenceAllocator, parse_reference

VALUE_TYPES = ("date", "integer", "text", "money", "boolean", "enum")
SOURCES = ("HUMAN_ENTRY", "ACCEPTED_INTAKE")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_RE = re.compile(r"^-?\d+(\.\d{1,4})?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Who registers a guest when the caller names nobody (the website intake).
GUEST_REGISTRAR_ACTOR = "system:crm"
GUEST_REGISTRAR_ROLE = "system"


class CrmError(DomainError):
    """A CRM rule refused the request."""


class GuestNotFound(CrmError):
    """No such guest inside this agency."""


class DuplicateGuest(CrmError):
    """A guest with this contact already exists — merging is a human decision."""


class InquiryNotFound(CrmError):
    """No such inquiry inside this agency."""


class RequirementRejected(CrmError):
    """The requirement is not one this platform is allowed to record."""


def _normalise_email(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = require_text(value, "email", max_len=320).strip().lower()
    if not _EMAIL_RE.match(text):
        raise RequirementRejected(f"email is not usable: {value!r}", code="VALIDATION_ERROR")
    return text


def _normalise_phone(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = require_text(value, "phone", max_len=40)
    # Keep a leading '+' but drop spacing/dashes so the same number dedupes.
    cleaned = re.sub(r"[^0-9+]", "", raw)
    if len(cleaned.lstrip("+")) < 7:
        raise RequirementRejected(f"phone is not usable: {value!r}", code="VALIDATION_ERROR")
    return cleaned


def _require_value_type(value: Any) -> str:
    if not isinstance(value, str) or value not in VALUE_TYPES:
        raise RequirementRejected(
            f"value_type must be one of {VALUE_TYPES}, got {value!r}",
            code="VALIDATION_ERROR", value_type=value,
        )
    return value


def _require_source(value: Any) -> str:
    if not isinstance(value, str) or value not in SOURCES:
        raise RequirementRejected(
            f"source must be one of {SOURCES}, got {value!r}", code="VALIDATION_ERROR",
        )
    return value


def _coerce(field_name: str, value: Any, value_type: str) -> str:
    """Return the stored form, or refuse.

    Types are checked here because a requirement typed `date` that holds `"next week"`
    will be matched and priced as if it were a date.

    An empty value is refused rather than stored: a confirmed requirement that holds
    nothing would sit in the ACTIVE set looking like a fact. Dropping a requirement is
    `withdraw_requirement`, which leaves the history intact.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RequirementRejected(
            f"{field_name} has no value; withdraw the requirement instead",
            code="VALIDATION_ERROR", field_name=field_name,
        )
    if not isinstance(value, str):
        raise RequirementRejected(
            f"{field_name} must be supplied as text, got {type(value).__name__}",
            code="VALIDATION_ERROR",
        )
    text = value.strip()
    if value_type == "date":
        if not _DATE_RE.match(text):
            raise RequirementRejected(f"{field_name} must be an ISO date (YYYY-MM-DD): {text!r}",
                                      code="VALIDATION_ERROR")
        try:
            # A bare calendar date is not a timestamp: `parse_iso` rightly rejects it
            # as naive, and `date.fromisoformat` is the check that matches the type.
            date.fromisoformat(text)
        except ValueError as exc:
            raise RequirementRejected(f"{field_name} is not a real calendar date: {text!r}",
                                      code="VALIDATION_ERROR") from exc
    elif value_type == "integer":
        if not re.match(r"^-?\d+$", text):
            raise RequirementRejected(f"{field_name} must be an integer: {text!r}",
                                      code="VALIDATION_ERROR")
    elif value_type == "money":
        if not _MONEY_RE.match(text):
            raise RequirementRejected(f"{field_name} must be a decimal amount: {text!r}",
                                      code="VALIDATION_ERROR")
        try:
            Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - regex already rejects these
            raise RequirementRejected(f"{field_name} is not a decimal: {text!r}",
                                      code="VALIDATION_ERROR") from exc
    elif value_type == "boolean":
        if text.lower() not in ("true", "false"):
            raise RequirementRejected(f"{field_name} must be true or false: {text!r}",
                                      code="VALIDATION_ERROR")
        return text.lower()
    elif value_type == "enum" and len(text) > 64:
        raise RequirementRejected(f"{field_name} enum value is too long", code="VALIDATION_ERROR")
    if len(text) > 500:
        raise RequirementRejected(f"{field_name} value is too long", code="VALIDATION_ERROR")
    return text


def _typed_value(field_name: str, stored: str, value_type: str) -> Any:
    """The consumer-facing value of a stored requirement.

    Requirements are stored as text so one column can hold every type while the
    database still checks the shape. Consumers need the type back: a matcher that
    compares ``adults`` as the string ``"4"`` or treats a budget as
    ``"1500000.5"`` minor units ranks a stay on a comparison nobody intended.
    """
    if value_type == "integer":
        return int(stored)
    if value_type == "money":
        amount = Decimal(stored)
        if amount != amount.to_integral_value():
            raise RequirementRejected(
                f"{field_name} must be a whole number of minor units: {stored!r}",
                code="MONEY_NOT_INTEGRAL",
                field_name=field_name,
            )
        return int(amount)
    if value_type == "boolean":
        return stored.lower() == "true"
    return stored


class CrmService:
    """Guest master + inquiry + requirements, all scoped to one agency."""

    def __init__(self, conn: Any, *, clock: Optional[Any] = None, ids: Optional[Any] = None,
                 agency_id: str = "agency_default", case_service: Any = None) -> None:
        self.conn = conn
        # SystemClock, not FrozenClock: a service default must be a working clock
        # (the repository injects FrozenClock in tests and never relies on this).
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self._case_service = case_service
        self.references = ReferenceAllocator(conn, agency_id=self.agency_id)

    # -- guest master ------------------------------------------------------- #
    def register_guest(self, full_name: Any, *, email: Any = None, phone: Any = None,
                       locale: str = "en-US", preferred_currency: str = "USD",
                       actor: str = GUEST_REGISTRAR_ACTOR,
                       actor_role: str = GUEST_REGISTRAR_ROLE) -> str:
        """Create a guest and its ``guest.registered`` audit row in one transaction.

        The audit row carries no identifier (no name, email or phone), only which
        contact kinds exist, so the hash chain records *that* a guest record was
        born and by whom (audit 2026-11-13, S2).
        """
        name = require_text(full_name, "full_name", max_len=200).strip()
        if len(name) < 2:
            raise RequirementRejected("full_name is too short", code="VALIDATION_ERROR")
        if preferred_currency not in GUEST_CURRENCIES:
            raise RequirementRejected(
                f"preferred_currency must be one of {GUEST_CURRENCIES}", code="VALIDATION_ERROR",
            )
        mail = _normalise_email(email)
        tel = _normalise_phone(phone)
        if mail is None and tel is None:
            raise RequirementRejected(
                "a guest needs an email or a phone number", code="VALIDATION_ERROR",
            )
        existing = self.find_guest(email=mail, phone=tel)
        if existing is not None:
            raise DuplicateGuest(
                "a guest with this contact already exists in this agency",
                code="RESOURCE_ALREADY_EXISTS", guest_id=str(existing["guest_id"]),
            )
        guest_id = self.ids.new("guest")
        stamp = to_iso(self.clock.now())
        locale_value = require_text(locale, "locale", max_len=35)
        own_tx = not self.conn.in_transaction
        if own_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO guests (guest_id, agency_id, full_name, email, phone, locale,"
                " preferred_currency, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
                (guest_id, self.agency_id, name, mail, tel, locale_value,
                 preferred_currency, stamp, stamp),
            )
            append_event(
                self.conn, clock=self.clock, ids=self.ids,
                action="guest.registered", entity_type="guest", entity_id=guest_id,
                actor=actor, actor_role=actor_role,
                after={"status": "ACTIVE", "locale": locale_value,
                       "preferred_currency": preferred_currency,
                       "mail_on_file": mail is not None, "tel_on_file": tel is not None},
            )
        except BaseException:
            if own_tx:
                self.conn.execute("ROLLBACK")
            raise
        if own_tx:
            self.conn.execute("COMMIT")
        return guest_id

    def find_guest(self, *, email: Any = None, phone: Any = None) -> Optional[Mapping[str, Any]]:
        """Match on either contact. Deliberately narrow: no fuzzy name matching."""
        mail = _normalise_email(email)
        tel = _normalise_phone(phone)
        clauses, params = [], []
        if mail is not None:
            clauses.append("email = ?")
            params.append(mail)
        if tel is not None:
            clauses.append("phone = ?")
            params.append(tel)
        if not clauses:
            return None
        row = self.conn.execute(
            f"SELECT * FROM guests WHERE agency_id = ? AND status = 'ACTIVE'"
            f" AND ({' OR '.join(clauses)}) ORDER BY created_at LIMIT 1",
            (self.agency_id, *params),
        ).fetchone()
        return None if row is None else dict(row)

    def guest(self, guest_id: Any) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM guests WHERE guest_id = ? AND agency_id = ?",
            (require_ref(guest_id, "guest_id"), self.agency_id),
        ).fetchone()
        if row is None:
            raise GuestNotFound(f"guest not found in this agency: {guest_id}",
                                code="RESOURCE_NOT_FOUND")
        return dict(row)

    # -- inquiry ------------------------------------------------------------ #
    def open_inquiry(self, *, guest_id: Any, channel: Any, body: Any, idempotency_key: Any,
                     content_class: str, priority: str = "NORMAL",
                     owner_actor: Any = None, channel_ref: Any = None,
                     received_at: Any = None) -> Mapping[str, Any]:
        """Open an inquiry for a known guest, allocate `INQ-YYYY-NNNNN`, return the record.

        The case is created by `CaseService` (single writer), then the reference and the
        guest link are attached through the allocator's retrying insert, so a lost race
        on the number produces the next one instead of a duplicate.

        `content_class` (P0–P4, the message-body classification from the case spine) is
        deliberately **required**: it decides whether the body is stored, stored redacted,
        or not stored at all, and §19 makes that a decision for the channel that received
        the message, not a default a CRM helper should quietly pick.
        """
        guest = self.guest(guest_id)
        if self._case_service is None:
            raise CrmError("an inquiry needs a case service to create the case",
                           code="SOURCE_UNAVAILABLE")
        case, message = self._case_service.open_case(
            channel=channel, guest_ref=str(guest["guest_id"]), content_class=content_class,
            body=body, idempotency_key=idempotency_key, priority=priority,
            channel_ref=channel_ref, owner_actor=owner_actor, received_at=received_at,
        )
        case_id = str(case.case_id)

        def attach(reference: str) -> None:
            cursor = self.conn.execute(
                "UPDATE cases SET reference = ?, guest_id = ?, updated_at = ?"
                " WHERE case_id = ? AND agency_id = ?",
                (reference, str(guest["guest_id"]), to_iso(self.clock.now()), case_id,
                 self.agency_id),
            )
            if cursor.rowcount != 1:
                raise InquiryNotFound(f"case vanished during allocation: {case_id}",
                                      code="RESOURCE_NOT_FOUND")

        year = parse_iso(to_iso(self.clock.now())).year
        reference = self.references.allocate("INQUIRY", year=year, on_allocate=attach)
        return {
            "case_id": case_id,
            "message_id": str(message.case_message_id),
            "reference": reference,
            "guest_id": str(guest["guest_id"]),
            "status": str(case.status),
        }

    def inquiry(self, inquiry_id: Any) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ? AND agency_id = ?",
            (require_ref(inquiry_id, "case_id"), self.agency_id),
        ).fetchone()
        if row is None:
            raise InquiryNotFound(f"inquiry not found in this agency: {inquiry_id}",
                                  code="RESOURCE_NOT_FOUND")
        return dict(row)

    def inquiry_by_reference(self, reference: Any) -> Mapping[str, Any]:
        """Look an inquiry up by its human-readable reference.

        The reference is a label, not a credential: this returns the row and nothing
        else. Authorization still comes from the caller's role and scope (§Appendix B).
        """
        parse_reference(reference)
        row = self.conn.execute(
            "SELECT * FROM cases WHERE reference = ? AND agency_id = ?",
            (reference, self.agency_id),
        ).fetchone()
        if row is None:
            raise InquiryNotFound(f"no inquiry with reference {reference!r}",
                                  code="RESOURCE_NOT_FOUND")
        return dict(row)

    def open_inquiries_for_guest(self, guest_id: Any) -> tuple[Mapping[str, Any], ...]:
        rows = self.conn.execute(
            "SELECT * FROM cases WHERE guest_id = ? AND agency_id = ?"
            " AND status NOT LIKE 'CLOSED_%' ORDER BY created_at",
            (require_ref(guest_id, "guest_id"), self.agency_id),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    # -- confirmed requirements --------------------------------------------- #
    def record_requirement(self, inquiry_id: Any, *, field_name: Any, field_value: Any = None,
                           value_type: str = "text", recorded_by: Any, source: str = "HUMAN_ENTRY",
                           source_intake_field_id: Any = None) -> Mapping[str, Any]:
        """Record a confirmed trip requirement, superseding any previous active value."""
        inquiry = self.inquiry(inquiry_id)
        name = require_text(field_name, "field_name", max_len=64).strip()
        if len(name) < 2:
            raise RequirementRejected("field_name is too short", code="VALIDATION_ERROR")
        kind = _require_value_type(value_type)
        origin = _require_source(source)
        actor = require_ref(recorded_by, "recorded_by")
        value = _coerce(name, field_value, kind)

        intake_field_id = None
        if origin == "ACCEPTED_INTAKE":
            if source_intake_field_id is None:
                raise RequirementRejected(
                    "an intake-sourced requirement must cite its intake field",
                    code="VERIFICATION_REQUIRED", field_name=name,
                )
            intake = self.conn.execute(
                "SELECT field_name, status, case_id FROM intake_fields WHERE intake_field_id = ?",
                (require_ref(source_intake_field_id, "source_intake_field_id"),),
            ).fetchone()
            if intake is None or str(intake["case_id"]) != str(inquiry["case_id"]):
                raise RequirementRejected(
                    "the cited intake field belongs to another inquiry",
                    code="RESOURCE_NOT_FOUND", field_name=name,
                )
            if str(intake["status"]) != "ACCEPTED":
                raise RequirementRejected(
                    "only an ACCEPTED intake field can become a confirmed requirement",
                    code="VERIFICATION_REQUIRED", field_name=name,
                    intake_status=str(intake["status"]),
                )
            intake_field_id = str(source_intake_field_id)

        stamp = to_iso(self.clock.now())
        previous = self.current_requirement_row(str(inquiry["case_id"]), name)
        requirement_id = self.ids.new("req")
        # Supersede first, insert second, both inside one savepoint. The active-only
        # unique index refuses two ACTIVE values for the same field, so the other order
        # cannot work; and without the savepoint a failure between the two statements
        # would leave the inquiry with no active value at all.
        self.conn.execute("SAVEPOINT record_requirement")
        try:
            if previous is not None:
                cursor = self.conn.execute(
                    "UPDATE inquiry_requirements SET status = 'SUPERSEDED'"
                    " WHERE requirement_id = ? AND agency_id = ? AND status = 'ACTIVE'",
                    (str(previous["requirement_id"]), self.agency_id),
                )
                if cursor.rowcount != 1:
                    raise CrmError(
                        "the previous requirement changed while this one was being recorded",
                        code="CONCURRENT_MODIFICATION",
                        requirement_id=str(previous["requirement_id"]),
                    )
            self.conn.execute(
                "INSERT INTO inquiry_requirements (requirement_id, agency_id, case_id, field_name,"
                " field_value, value_type, status, source, source_intake_field_id,"
                " supersedes_requirement_id, recorded_by, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?)",
                (requirement_id, self.agency_id, str(inquiry["case_id"]), name, value, kind, origin,
                 intake_field_id, None if previous is None else str(previous["requirement_id"]),
                 actor, stamp),
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT record_requirement")
            self.conn.execute("RELEASE SAVEPOINT record_requirement")
            raise
        self.conn.execute("RELEASE SAVEPOINT record_requirement")
        return {
            "requirement_id": requirement_id,
            "inquiry_id": str(inquiry["case_id"]),
            "field_name": name,
            "field_value": value,
            "value_type": kind,
            "source": origin,
            "superseded": None if previous is None else str(previous["requirement_id"]),
        }

    def current_requirement_row(self, inquiry_id: str, field_name: str) -> Optional[Mapping[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM inquiry_requirements WHERE agency_id = ? AND case_id = ?"
            " AND field_name = ? AND status = 'ACTIVE'",
            (self.agency_id, inquiry_id, field_name),
        ).fetchone()
        return None if row is None else dict(row)

    def requirements(self, inquiry_id: Any, *, include_history: bool = False) -> tuple[Mapping[str, Any], ...]:
        inquiry = self.inquiry(inquiry_id)
        clause = "" if include_history else " AND status = 'ACTIVE'"
        rows = self.conn.execute(
            f"SELECT * FROM inquiry_requirements WHERE agency_id = ? AND case_id = ?{clause}"
            " ORDER BY field_name, recorded_at",
            (self.agency_id, str(inquiry["case_id"])),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def confirmed_facts(self, inquiry_id: Any) -> Mapping[str, Any]:
        """Current trip requirements in typed form — the §6 authority for a stay.

        Read this, not the proposal layer: ``intake_fields`` holds extractions
        awaiting review, while these rows are what a human confirmed for the guest.
        Superseded and withdrawn values are already excluded, so what comes back is
        the current truth rather than a history.
        """
        facts: dict[str, Any] = {}
        for row in self.requirements(inquiry_id):
            name = str(row["field_name"])
            facts[name] = _typed_value(name, str(row["field_value"]), str(row["value_type"]))
        return facts

    def withdraw_requirement(self, requirement_id: Any, *, actor: Any) -> None:
        """Withdraw a requirement without deleting it: history is the audit trail."""
        row = self.conn.execute(
            "SELECT status FROM inquiry_requirements WHERE requirement_id = ? AND agency_id = ?",
            (require_ref(requirement_id, "requirement_id"), self.agency_id),
        ).fetchone()
        if row is None:
            raise RequirementRejected(f"requirement not found: {requirement_id}",
                                      code="RESOURCE_NOT_FOUND")
        if str(row["status"]) != "ACTIVE":
            raise RequirementRejected(
                f"only an ACTIVE requirement can be withdrawn (status={row['status']})",
                code="STATE_TRANSITION_NOT_ALLOWED",
            )
        require_ref(actor, "actor")
        self.conn.execute(
            "UPDATE inquiry_requirements SET status = 'WITHDRAWN' WHERE requirement_id = ?",
            (str(requirement_id),),
        )


__all__ = [
    "CrmError", "CrmService", "DuplicateGuest", "GuestNotFound", "InquiryNotFound",
    "REFERENCE_PREFIXES", "RequirementRejected", "SOURCES", "VALUE_TYPES",
]

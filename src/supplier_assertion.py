"""ADR-0074 (PROPOSED), E5: who a supplier is on the phone, and who may sign for the callback.

This module is a mixin for :class:`reservation_kernel.ReservationKernel`, so every method
below runs on the kernel's own connection, inside the kernel's own transaction, and writes
its audit row in that same transaction. It adds no second writer.

Trust boundary (the point of the module):

* **Identity, tenant, role and step-up are resolved here, from a session token** — never read
  from a caller-supplied identity, role, timestamp or MFA flag. ``SessionService.authenticate``
  resolves the token inside this agency; the role the session carries is then checked against
  the agency's own role-holder table (``agency_role_holders``/``agency_roles``, ADR-0035 D3):
  the session identity must be an ACTIVE holder of an ACTIVE role whose blueprint is the role
  being exercised. A session that merely *says* ``reservation_manager`` is refused
  (``SIGNER_ROLE_NOT_BOUND``).
* **Step-up is the existing one.** ``SessionService.require_step_up`` decides whether the session
  holds a fresh factor; the only addition is the 15-minute ceiling E5 asks for, read from the
  ``STEP_UP_GRANTED`` event ``satisfy_step_up`` already writes.
* **Nothing here activates a policy.** Enforcement is off until an agency sets
  ``reservation.supplier_assertion.enforced`` to ``true``; the 24-hour freshness window is a
  per-agency setting with a candidate default. Legacy evidence is never rewritten.

A recorded callback is a human attestation. Nothing here claims it is cryptographic
authentication.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any, NamedTuple, Optional

import identity as IDENTITY
import roles as ROLES
from primitives import DomainError, parse_iso, require_ref, require_text, sha256_hex, to_iso

#: Subject the step-up check is asked about. It is not one of the money subjects, so the
#: service is told explicitly that this one is high-risk.
SIGNING_SUBJECT = "supplier_block_assertion"

#: E5 candidate default (not an activated production policy): how old a step-up may be when
#: a signature is made. ``SessionService.step_up_window`` may be shorter, never longer here.
SIGNING_STEP_UP_MAX_AGE = timedelta(minutes=15)

#: E5 candidate default for how old the supplier's own confirmation may be.
DEFAULT_MAX_CONFIRMATION_AGE_HOURS = 24

SETTING_ENFORCED = "reservation.supplier_assertion.enforced"
SETTING_MAX_AGE_HOURS = "reservation.supplier_assertion.max_age_hours"

#: Who may register and check a supplier contact. Two *different* humans are required.
CONTACT_REGISTRY_ROLES: tuple[str, ...] = ("owner", "gm")

#: Registered contact methods. ``registration_basis`` is a closed list on purpose.
REGISTRATION_BASES: tuple[str, ...] = ("SIGNED_AGREEMENT", "OWNER_ATTESTED")

#: Who may read the registry (masked) and a booking's assertion state. The signer needs the
#: list to name a contact; the registry roles need it to check one.
CONTACT_READER_ROLES: tuple[str, ...] = tuple(
    sorted(set(CONTACT_REGISTRY_ROLES) | set(ROLES.SIGNER_ROLES))
)

_PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{5,30}[0-9]$")
_PAGE_TO_CANONICAL = {page: canon for canon, page in ROLES.PAGE_ROLE_BY_CANONICAL.items()}


class Principal(NamedTuple):
    """What the server resolved from a session token. Not caller-supplied."""

    identity: str
    role: str
    session_id: str


def _canonical(role: Any) -> Optional[str]:
    name = ROLES.canonical_role(role)
    if name is not None:
        return name
    return _PAGE_TO_CANONICAL.get(str(role).strip())


class SupplierAssertionMixin:
    """Contact registry, signer resolution and booking-specific supplier assertions."""

    # -- the trusted authorization boundary ---------------------------------- #
    def _resolve_principal(
        self,
        conn: sqlite3.Connection,
        session_token: Any,
        *,
        allowed: tuple[str, ...],
        step_up: bool,
    ) -> Principal:
        """Resolve a session token into a bound, allowed, optionally step-up-fresh human."""
        if not isinstance(session_token, str) or not session_token.strip():
            raise DomainError(
                "a signed-in session is required for this action", code="SESSION_REQUIRED"
            )
        service = IDENTITY.SessionService(
            conn,
            clock=self.clock,
            ids=self.ids,
            agency_id=self.agency_id,
            step_up_window=SIGNING_STEP_UP_MAX_AGE,
            high_risk_subject_types=(SIGNING_SUBJECT,),
        )
        session = service.authenticate(session_token.strip(), touch=False)
        if ROLES.is_machine_identity(session.identity) or ROLES.is_machine_role(session.role):
            raise DomainError(
                "a machine principal cannot sign", code="MACHINE_PRINCIPAL_REFUSED",
                actor=session.identity,
            )
        role = _canonical(session.role)
        if role is None or role not in allowed:
            raise DomainError(
                "this role may not perform the action",
                code="SIGNER_ROLE_NOT_ALLOWED",
                role=str(session.role),
                allowed=list(allowed),
            )
        # Authoritative binding: the tenant's own role-holder table, not the session's say-so.
        bound = conn.execute(
            "SELECT 1 FROM agency_role_holders h JOIN agency_roles r"
            " ON r.agency_id = h.agency_id AND r.role_id = h.role_id"
            " WHERE h.agency_id = ? AND h.holder_ref = ? AND h.status = 'ACTIVE'"
            " AND r.status = 'ACTIVE' AND r.based_on = ?",
            (self.agency_id, session.identity, role),
        ).fetchone()
        if bound is None:
            raise DomainError(
                "this identity does not hold that role in this agency",
                code="SIGNER_ROLE_NOT_BOUND",
                identity=session.identity,
                role=role,
            )
        if session.mfa_satisfied_at is None:
            raise DomainError(
                "this session never satisfied its second factor", code="MFA_REQUIRED",
                session_id=session.session_id,
            )
        if step_up:
            service.require_step_up(session, subject_type=SIGNING_SUBJECT)
            granted = conn.execute(
                "SELECT occurred_at FROM auth_events WHERE agency_id = ? AND session_id = ?"
                " AND kind = 'STEP_UP_GRANTED' ORDER BY occurred_at DESC, auth_event_id DESC"
                " LIMIT 1",
                (self.agency_id, session.session_id),
            ).fetchone()
            if granted is None or parse_iso(self._now()) - parse_iso(
                str(granted["occurred_at"])
            ) > SIGNING_STEP_UP_MAX_AGE:
                raise DomainError(
                    "the step-up is older than the signing window; verify a fresh code",
                    code="SIGNER_STEP_UP_STALE",
                    window_minutes=int(SIGNING_STEP_UP_MAX_AGE.total_seconds() // 60),
                )
        return Principal(session.identity, role, session.session_id)

    # -- per-agency settings (candidate defaults, never hard-coded policy) ---- #
    def _assertion_setting(self, conn: sqlite3.Connection, key: str, default: str) -> str:
        row = conn.execute(
            "SELECT value FROM agency_settings WHERE agency_id = ? AND setting_key = ?",
            (self.agency_id, key),
        ).fetchone()
        return default if row is None else str(row["value"]).strip()

    def _assertion_enforced(self, conn: sqlite3.Connection) -> bool:
        return self._assertion_setting(conn, SETTING_ENFORCED, "false").lower() == "true"

    def _assertion_max_age(self, conn: sqlite3.Connection) -> timedelta:
        raw = self._assertion_setting(
            conn, SETTING_MAX_AGE_HOURS, str(DEFAULT_MAX_CONFIRMATION_AGE_HOURS)
        )
        try:
            hours = int(raw)
        except ValueError:  # a corrupt setting fails closed to the stricter default
            hours = DEFAULT_MAX_CONFIRMATION_AGE_HOURS
        return timedelta(hours=max(1, min(hours, DEFAULT_MAX_CONFIRMATION_AGE_HOURS)))

    # -- supplier contacts ----------------------------------------------------- #
    def _contact_row(self, conn: sqlite3.Connection, contact_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM supplier_contacts WHERE contact_id = ? AND agency_id = ?",
            (contact_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "unknown supplier contact for this agency", code="CONTACT_NOT_FOUND",
                contact_id=contact_id,
            )
        return row

    def _contact_standing(self, conn: sqlite3.Connection, contact_id: str) -> tuple[str, Optional[str]]:
        """``(UNVERIFIED|VERIFIED|SUSPENDED, latest event id)`` — the latest event decides."""
        row = conn.execute(
            "SELECT event_id, kind FROM supplier_contact_events"
            " WHERE contact_id = ? AND agency_id = ? ORDER BY seq DESC LIMIT 1",
            (contact_id, self.agency_id),
        ).fetchone()
        if row is None:
            return "UNVERIFIED", None
        return ("SUSPENDED" if row["kind"] == "SUSPENDED" else "VERIFIED"), str(row["event_id"])

    def _active_supplier(self, conn: sqlite3.Connection, supplier_id: str, code: str) -> None:
        row = conn.execute(
            "SELECT status FROM suppliers WHERE supplier_id = ? AND agency_id = ?",
            (supplier_id, self.agency_id),
        ).fetchone()
        if row is None or row["status"] != "ACTIVE":
            raise DomainError(
                "only an active supplier of this agency can be used", code=code,
                supplier_id=supplier_id,
            )

    def register_supplier_contact(
        self,
        *,
        supplier_id: str,
        contact_value: str,
        registration_basis: str,
        session_token: str,
        idempotency_key: str,
        channel: str = "PHONE",
    ) -> str:
        """Register the number a callback will go to (the *maker* half of maker/checker).

        The number is stored once and never edited; a new number is a new contact. It is
        personal data: it appears in no audit payload, only the contact id does.
        """
        supplier_id = require_ref(supplier_id, "supplier_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key", max_len=120)
        if len(idempotency_key) < 8:
            raise DomainError("idempotency_key is too short", code="INVALID_INPUT")
        if str(channel).strip().upper() != "PHONE":
            raise DomainError("the pilot registers phone contacts only", code="CONTACT_CHANNEL_REFUSED")
        basis = str(registration_basis).strip().upper()
        if basis not in REGISTRATION_BASES:
            raise DomainError(
                f"registration_basis must be one of {list(REGISTRATION_BASES)}",
                code="CONTACT_BASIS_REFUSED",
            )
        value = require_text(contact_value, "contact_value", max_len=40)
        if not _PHONE_RE.match(value):
            raise DomainError("contact_value is not a phone number", code="CONTACT_VALUE_INVALID")
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token, allowed=CONTACT_REGISTRY_ROLES, step_up=False
            )
            request_hash = sha256_hex(
                "register_supplier_contact", supplier_id, "PHONE", value, basis, who.identity
            )
            replayed = self._idempotent_lookup(
                conn, "register_supplier_contact", idempotency_key, request_hash
            )
            if replayed is not None:
                return replayed
            self._active_supplier(conn, supplier_id, "CONTACT_SUPPLIER_NOT_ACTIVE")
            contact_id = self.ids.new("contact")
            try:
                conn.execute(
                    "INSERT INTO supplier_contacts (contact_id, agency_id, supplier_id, channel,"
                    " contact_value, registration_basis, created_by, created_role, created_at)"
                    " VALUES (?, ?, ?, 'PHONE', ?, ?, ?, ?, ?)",
                    (contact_id, self.agency_id, supplier_id, value, basis, who.identity,
                     who.role, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    "this supplier already has a live contact with that number",
                    code="CONTACT_DUPLICATE",
                ) from exc
            self._audit(
                conn,
                action="supplier_contact.registered",
                entity_type="supplier_contact",
                entity_id=contact_id,
                after={"supplier_id": supplier_id, "channel": "PHONE",
                       "registration_basis": basis, "created_role": who.role},
                reason=None, policy_version_id=None, evidence_ref=None, actor=who.identity,
            )
            self._record_idempotency(
                conn, "register_supplier_contact", idempotency_key, request_hash, contact_id
            )
        return contact_id

    def verify_supplier_contact(
        self, *, contact_id: str, session_token: str, idempotency_key: str
    ) -> str:
        """Check a registered contact (the *checker*). The checker may not be the maker."""
        contact_id = require_ref(contact_id, "contact_id")
        idempotency_key = require_ref(idempotency_key, "idempotency_key", max_len=120)
        if len(idempotency_key) < 8:
            raise DomainError("idempotency_key is too short", code="INVALID_INPUT")
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token, allowed=CONTACT_REGISTRY_ROLES, step_up=False
            )
            request_hash = sha256_hex("verify_supplier_contact", contact_id, who.identity)
            replayed = self._idempotent_lookup(
                conn, "verify_supplier_contact", idempotency_key, request_hash
            )
            if replayed is not None:
                return replayed
            contact = self._contact_row(conn, contact_id)
            if contact["revoked_at"] is not None:
                raise DomainError("this contact was revoked", code="CONTACT_REVOKED")
            if contact["created_by"] == who.identity:
                raise DomainError(
                    "a contact cannot be checked by the person who registered it",
                    code="CONTACT_CHECKER_IS_MAKER",
                )
            self._active_supplier(conn, str(contact["supplier_id"]), "CONTACT_SUPPLIER_NOT_ACTIVE")
            standing, _ = self._contact_standing(conn, contact_id)
            if standing != "UNVERIFIED":
                raise DomainError(
                    "this contact already has a verification history",
                    code="CONTACT_ALREADY_VERIFIED", standing=standing,
                )
            event_id = self._append_contact_event(conn, contact, "VERIFIED", who, None)
            self._audit(
                conn,
                action="supplier_contact.verified",
                entity_type="supplier_contact",
                entity_id=contact_id,
                after={"event_id": event_id, "checker_role": who.role},
                reason=None, policy_version_id=None, evidence_ref=None, actor=who.identity,
            )
            self._record_idempotency(
                conn, "verify_supplier_contact", idempotency_key, request_hash, event_id
            )
        return event_id

    def _append_contact_event(
        self, conn: sqlite3.Connection, contact: sqlite3.Row, kind: str, who: Principal,
        reason: Optional[str],
    ) -> str:
        last = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM supplier_contact_events WHERE contact_id = ?",
            (contact["contact_id"],),
        ).fetchone()[0]
        event_id = self.ids.new("cevent")
        try:
            conn.execute(
                "INSERT INTO supplier_contact_events (event_id, agency_id, supplier_id, contact_id,"
                " seq, kind, actor, actor_role, contact_created_by, reason, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, self.agency_id, contact["supplier_id"], contact["contact_id"],
                 int(last) + 1, kind, who.identity, who.role, contact["created_by"], reason,
                 self._now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainError(
                "a conflicting verification was recorded first; re-read the contact",
                code="CONTACT_VERIFICATION_CONFLICT",
            ) from exc
        return event_id

    def suspend_supplier_contact(
        self, *, contact_id: str, reason: str, session_token: str, idempotency_key: str
    ) -> str:
        """Suspend a verified contact. Takes effect at the next confirmation."""
        contact_id = require_ref(contact_id, "contact_id")
        reason = require_text(reason, "reason", max_len=300)
        idempotency_key = require_ref(idempotency_key, "idempotency_key", max_len=120)
        if len(reason) < 3 or len(idempotency_key) < 8:
            raise DomainError("a reason and a longer idempotency key are required", code="INVALID_INPUT")
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token, allowed=CONTACT_REGISTRY_ROLES, step_up=False
            )
            request_hash = sha256_hex("suspend_supplier_contact", contact_id, reason, who.identity)
            replayed = self._idempotent_lookup(
                conn, "suspend_supplier_contact", idempotency_key, request_hash
            )
            if replayed is not None:
                return replayed
            contact = self._contact_row(conn, contact_id)
            if contact["revoked_at"] is not None:
                raise DomainError("this contact was revoked", code="CONTACT_REVOKED")
            standing, _ = self._contact_standing(conn, contact_id)
            if standing != "VERIFIED":
                raise DomainError(
                    "only a verified contact can be suspended", code="CONTACT_NOT_VERIFIED",
                    standing=standing,
                )
            event_id = self._append_contact_event(conn, contact, "SUSPENDED", who, reason)
            self._audit(
                conn,
                action="supplier_contact.suspended",
                entity_type="supplier_contact",
                entity_id=contact_id,
                after={"event_id": event_id, "actor_role": who.role},
                reason=reason, policy_version_id=None, evidence_ref=None, actor=who.identity,
            )
            self._record_idempotency(
                conn, "suspend_supplier_contact", idempotency_key, request_hash, event_id
            )
        return event_id

    def revoke_supplier_contact(
        self, *, contact_id: str, reason: str, session_token: str, idempotency_key: str
    ) -> str:
        """Retire a contact for good. The row stays; the number is never edited or reused."""
        contact_id = require_ref(contact_id, "contact_id")
        reason = require_text(reason, "reason", max_len=300)
        idempotency_key = require_ref(idempotency_key, "idempotency_key", max_len=120)
        if len(reason) < 3 or len(idempotency_key) < 8:
            raise DomainError("a reason and a longer idempotency key are required", code="INVALID_INPUT")
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token, allowed=CONTACT_REGISTRY_ROLES, step_up=False
            )
            request_hash = sha256_hex("revoke_supplier_contact", contact_id, reason, who.identity)
            replayed = self._idempotent_lookup(
                conn, "revoke_supplier_contact", idempotency_key, request_hash
            )
            if replayed is not None:
                return replayed
            contact = self._contact_row(conn, contact_id)
            if contact["revoked_at"] is not None:
                raise DomainError("this contact was already revoked", code="CONTACT_REVOKED")
            conn.execute(
                "UPDATE supplier_contacts SET revoked_at = ?, revoked_by = ?, revoke_reason = ?"
                " WHERE contact_id = ? AND agency_id = ?",
                (self._now(), who.identity, reason, contact_id, self.agency_id),
            )
            self._audit(
                conn,
                action="supplier_contact.revoked",
                entity_type="supplier_contact",
                entity_id=contact_id,
                after={"actor_role": who.role},
                reason=reason, policy_version_id=None, evidence_ref=None, actor=who.identity,
            )
            self._record_idempotency(
                conn, "revoke_supplier_contact", idempotency_key, request_hash, contact_id
            )
        return contact_id

    def read_supplier_contact_for_callback(
        self, *, contact_id: str, session_token: str
    ) -> dict[str, str]:
        """The one place the stored number leaves the table: narrow, session-bound, audited.

        Allowed to the signer and to the two contact-registry roles. The audit row names the
        contact id and the reader, never the number.
        """
        contact_id = require_ref(contact_id, "contact_id")
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token,
                allowed=tuple(sorted(set(CONTACT_REGISTRY_ROLES) | set(ROLES.SIGNER_ROLES))),
                step_up=False,
            )
            contact = self._contact_row(conn, contact_id)
            if contact["revoked_at"] is not None:
                raise DomainError("this contact was revoked", code="CONTACT_REVOKED")
            self._audit(
                conn,
                action="supplier_contact.read",
                entity_type="supplier_contact",
                entity_id=contact_id,
                after={"reader_role": who.role},
                reason=None, policy_version_id=None, evidence_ref=None, actor=who.identity,
            )
            return {
                "contact_id": str(contact["contact_id"]),
                "supplier_id": str(contact["supplier_id"]),
                "channel": str(contact["channel"]),
                "contact_value": str(contact["contact_value"]),
            }

    # -- the booking-specific supplier assertion ---------------------------------- #
    def record_supplier_assertion(
        self,
        *,
        booking_id: str,
        supplier_id: str,
        relationship_id: str,
        contact_id: str,
        check_in: str,
        check_out: str,
        supplier_confirmed_at: str,
        session_token: str,
        idempotency_key: str,
        notes: Optional[str] = None,
    ) -> str:
        """Record, for one booking, that the supplier confirmed the block and a human called back.

        Writes the evidence row *and* its attribution in one transaction. The callback time is
        the server's clock; the actor is the session's identity — neither is a parameter.
        Returns the evidence id, which ``record_supplier_block_proof`` then binds as before.
        """
        import reservation_kernel as RK  # local: the kernel imports this module

        booking_id = require_ref(booking_id, "booking_id")
        supplier_id = require_ref(supplier_id, "supplier_id")
        relationship_id = require_ref(relationship_id, "relationship_id")
        contact_id = require_ref(contact_id, "contact_id")
        check_in = require_ref(check_in, "check_in")
        check_out = require_ref(check_out, "check_out")
        idempotency_key = require_ref(idempotency_key, "idempotency_key", max_len=120)
        if len(idempotency_key) < 8:
            raise DomainError("idempotency_key is too short", code="INVALID_INPUT")
        confirmed = parse_iso(require_ref(supplier_confirmed_at, "supplier_confirmed_at"))
        with self.transaction() as conn:
            who = self._resolve_principal(
                conn, session_token, allowed=ROLES.SIGNER_ROLES, step_up=True
            )
            request_hash = sha256_hex(
                "record_supplier_assertion", booking_id, supplier_id, relationship_id,
                contact_id, check_in, check_out, to_iso(confirmed), who.identity, who.role,
                notes or "",
            )
            replayed = self._idempotent_lookup(
                conn, "record_supplier_assertion", idempotency_key, request_hash
            )
            if replayed is not None:
                return replayed
            booking = self._require_booking(conn, booking_id)
            if booking["status"] not in RK.BLOCK_PROOF_BOOKING_STATES:
                raise DomainError(
                    "an assertion is recorded only for a booking that is still live",
                    code="BLOCK_PROOF_BOOKING_CLOSED", status=booking["status"],
                )
            if check_in != booking["check_in"] or check_out != booking["check_out"]:
                raise DomainError(
                    "the asserted stay does not match this booking", code="BLOCK_PROOF_WRONG_DATES",
                    asserted=(check_in, check_out),
                    booked=(booking["check_in"], booking["check_out"]),
                )
            now = self._now()
            now_moment = parse_iso(now)
            if confirmed > now_moment:
                raise DomainError(
                    "the supplier cannot have confirmed in the future",
                    code="ASSERTION_CONFIRMATION_FUTURE",
                )
            if confirmed < parse_iso(booking["created_at"]):
                raise DomainError(
                    "a confirmation older than the booking cannot be for it",
                    code="ASSERTION_PREDATES_BOOKING",
                )
            max_age = self._assertion_max_age(conn)
            if now_moment - confirmed > max_age:
                raise DomainError(
                    "the supplier's confirmation is older than the freshness window",
                    code="ASSERTION_CONFIRMATION_STALE",
                    max_age_hours=int(max_age.total_seconds() // 3600),
                )
            self._check_supplier_chain(
                conn, booking=booking, supplier_id=supplier_id, relationship_id=relationship_id,
                contact_id=contact_id,
            )
            standing, event_id = self._contact_standing(conn, contact_id)
            if standing != "VERIFIED":
                raise DomainError(
                    "the contact is not currently verified",
                    code="CONTACT_SUSPENDED" if standing == "SUSPENDED" else "CONTACT_NOT_VERIFIED",
                    contact_id=contact_id,
                )
            evidence_id = self.ids.new("evid")
            valid_until = min(
                now_moment + timedelta(hours=RK.DEFAULT_EVIDENCE_TTL_HOURS), confirmed + max_age
            )
            conn.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
                " evidence_level, source_type, source_ref, captured_at, valid_until, result,"
                " occupancy_ranges, can_quote, can_hold, can_confirm, notes, created_at)"
                " VALUES (?, ?, ?, 'B', 'manager_written', ?, ?, ?, 'AVAILABLE', '[]', 1, 1, 1,"
                " ?, ?)",
                (evidence_id, self.agency_id, booking["unit_id"],
                 f"callback_verified:{contact_id}", now, to_iso(valid_until), notes, now),
            )
            try:
                conn.execute(
                    "INSERT INTO evidence_supplier_attributions (evidence_id, agency_id, unit_id,"
                    " booking_id, supplier_id, relationship_id, contact_id, contact_event_id,"
                    " source_kind, block_start, block_end, supplier_confirmed_at, callback_at,"
                    " callback_by, callback_role, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CALLBACK_VERIFIED', ?, ?, ?, ?, ?, ?, ?)",
                    (evidence_id, self.agency_id, booking["unit_id"], booking_id, supplier_id,
                     relationship_id, contact_id, event_id, check_in, check_out,
                     to_iso(confirmed), now, who.identity, who.role, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    "the assertion's references do not all belong to this agency",
                    code="ASSERTION_REFERENCE_INVALID",
                ) from exc
            self._audit(
                conn,
                action="supplier_assertion.recorded",
                entity_type="availability_evidence",
                entity_id=evidence_id,
                after={
                    "booking_id": booking_id, "unit_id": booking["unit_id"],
                    "supplier_id": supplier_id, "relationship_id": relationship_id,
                    "contact_id": contact_id, "source_kind": "CALLBACK_VERIFIED",
                    "supplier_confirmed_at": to_iso(confirmed), "signer_role": who.role,
                },
                reason=None, policy_version_id=None, evidence_ref=evidence_id, actor=who.identity,
            )
            self._record_idempotency(
                conn, "record_supplier_assertion", idempotency_key, request_hash, evidence_id
            )
        return evidence_id

    def _check_supplier_chain(
        self, conn: sqlite3.Connection, *, booking: sqlite3.Row, supplier_id: str,
        relationship_id: str, contact_id: str,
    ) -> None:
        """Supplier ACTIVE, relationship ACTIVE for *this exact unit*, contact on that supplier.

        A relationship with no unit (property-wide) never authorises an assertion: ADR-0074 §2c.
        """
        self._active_supplier(conn, supplier_id, "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED")
        relationship = conn.execute(
            "SELECT * FROM villa_supplier_relationships"
            " WHERE villa_supplier_relationship_id = ? AND agency_id = ?",
            (relationship_id, self.agency_id),
        ).fetchone()
        if (
            relationship is None
            or relationship["supplier_id"] != supplier_id
            or relationship["status"] != "ACTIVE"
            or relationship["unit_id"] != booking["unit_id"]
        ):
            raise DomainError(
                "the supplier relationship does not cover this exact unit",
                code="BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED",
                relationship_id=relationship_id, unit_id=booking["unit_id"],
            )
        contact = self._contact_row(conn, contact_id)
        if contact["supplier_id"] != supplier_id:
            raise DomainError(
                "the contact belongs to a different supplier", code="CONTACT_SUPPLIER_MISMATCH",
            )
        if contact["revoked_at"] is not None:
            raise DomainError("this contact was revoked", code="CONTACT_REVOKED")

    # -- enforcement (off until an agency turns it on) -------------------------------- #
    def _attribution(self, conn: sqlite3.Connection, evidence_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM evidence_supplier_attributions WHERE evidence_id = ? AND agency_id = ?",
            (evidence_id, self.agency_id),
        ).fetchone()

    def _require_attributed_evidence(
        self, conn: sqlite3.Connection, *, booking: sqlite3.Row, evidence_id: str,
        supplier_id: str, relationship_id: str, at_confirmation: bool,
    ) -> sqlite3.Row:
        """Evidence may back a proof only if a verified callback assertion names *this* booking."""
        row = self._attribution(conn, evidence_id)
        if row is None:
            raise DomainError(
                "this evidence carries no verified supplier attribution; legacy and generic"
                " availability answers cannot authorise a new confirmation",
                code="BLOCK_PROOF_EVIDENCE_UNATTRIBUTED", evidence_id=evidence_id,
            )
        if (
            row["booking_id"] != booking["booking_id"]
            or row["unit_id"] != booking["unit_id"]
            or row["block_start"] != booking["check_in"]
            or row["block_end"] != booking["check_out"]
            or row["supplier_id"] != supplier_id
            or row["relationship_id"] != relationship_id
        ):
            raise DomainError(
                "the supplier assertion names a different booking, unit, stay or supplier",
                code="BLOCK_PROOF_EVIDENCE_NOT_FROM_SUPPLIER", evidence_id=evidence_id,
            )
        if at_confirmation:
            now_moment = parse_iso(self._now())
            if now_moment - parse_iso(row["supplier_confirmed_at"]) > self._assertion_max_age(conn):
                raise DomainError(
                    "the supplier's confirmation is no longer fresh",
                    code="ASSERTION_CONFIRMATION_STALE",
                )
            self._check_supplier_chain(
                conn, booking=booking, supplier_id=supplier_id, relationship_id=relationship_id,
                contact_id=row["contact_id"],
            )
            standing, _ = self._contact_standing(conn, row["contact_id"])
            if standing != "VERIFIED":
                raise DomainError(
                    "the contact behind this assertion is no longer verified",
                    code="CONTACT_SUSPENDED" if standing == "SUSPENDED" else "CONTACT_NOT_VERIFIED",
                )
            still_bound = conn.execute(
                "SELECT 1 FROM agency_role_holders h JOIN agency_roles r"
                " ON r.agency_id = h.agency_id AND r.role_id = h.role_id"
                " WHERE h.agency_id = ? AND h.holder_ref = ? AND h.status = 'ACTIVE'"
                " AND r.status = 'ACTIVE' AND r.based_on = ?",
                (self.agency_id, row["callback_by"], row["callback_role"]),
            ).fetchone()
            if still_bound is None:
                raise DomainError(
                    "the person who attested the callback no longer holds that role",
                    code="SIGNER_ROLE_NOT_BOUND", identity=row["callback_by"],
                )
        return row

    def _authorize_confirmation(
        self, conn: sqlite3.Connection, *, session_token: Any, actor: Optional[str]
    ) -> Principal:
        """Resolve *who* is closing this booking — before any state is read or written.

        This is the hard invariant (G2-A): a close is a privileged human decision, so it
        needs a verified identity, an approved role assignment and current authorization
        no matter what ``reservation.supplier_assertion.enforced`` says. The caller's
        ``actor`` string is a claim; only the session is evidence, and the claim is
        accepted only when it matches the resolved identity.
        """
        who = self._resolve_principal(
            conn, session_token, allowed=ROLES.SIGNER_ROLES, step_up=True
        )
        bound = self._bound_principal()
        if bound and (
            ROLES.is_machine_identity(getattr(self, "actor", ""))
            or ROLES.is_machine_role(getattr(self, "actor_role", ""))
        ):
            raise DomainError(
                "a machine-bound kernel cannot sign as a human",
                code="BLOCK_PROOF_MACHINE_VERIFIER",
                actor=getattr(self, "actor", None),
                role=getattr(self, "actor_role", None),
            )
        supplied = actor or who.identity
        if supplied not in (who.identity, f"human:{who.identity}") or (
            bound and self.actor not in (who.identity, f"human:{who.identity}")
        ):
            raise DomainError(
                "the confirming actor must be the signed-in signer",
                code="BLOCK_PROOF_ACTOR_NOT_BOUND",
                supplied=supplied,
                signer=who.identity,
            )
        return who

    def _enforce_at_confirmation(
        self, conn: sqlite3.Connection, *, booking: sqlite3.Row, proof: sqlite3.Row,
        session_token: Any, actor: Optional[str],
    ) -> str:
        """Return the signer's identity, always authorizing; evidence only when enforced."""
        who = self._authorize_confirmation(conn, session_token=session_token, actor=actor)
        if self._assertion_enforced(conn):
            self._require_attributed_evidence(
                conn, booking=booking, evidence_id=proof["evidence_id"],
                supplier_id=proof["supplier_id"], relationship_id=proof["relationship_id"],
                at_confirmation=True,
            )
        return who.identity

    # -- read models for the operator screen (no writes, no number in bulk) ---------- #
    def list_supplier_contacts(self, *, session_token: str) -> tuple[dict[str, Any], ...]:
        """Every contact of this agency, with its standing and the number masked.

        The screen and the write path must not disagree about what "verified" means, so the
        standing here is derived from the same latest-event rule the gate uses. The full
        number still leaves through exactly one place, ``read_supplier_contact_for_callback``.
        """
        self._resolve_principal(self.conn, session_token, allowed=CONTACT_READER_ROLES, step_up=False)
        rows = self.conn.execute(
            "SELECT c.contact_id, c.supplier_id, s.name AS supplier_name, c.channel,"
            " c.contact_value, c.registration_basis, c.created_by, c.created_role,"
            " c.created_at, c.revoked_at, c.revoke_reason,"
            " (SELECT e.kind FROM supplier_contact_events e"
            "   WHERE e.contact_id = c.contact_id AND e.agency_id = c.agency_id"
            "   ORDER BY e.seq DESC LIMIT 1) AS last_kind,"
            " (SELECT e.occurred_at FROM supplier_contact_events e"
            "   WHERE e.contact_id = c.contact_id AND e.agency_id = c.agency_id"
            "   ORDER BY e.seq DESC LIMIT 1) AS last_event_at"
            " FROM supplier_contacts c"
            " LEFT JOIN suppliers s ON s.supplier_id = c.supplier_id AND s.agency_id = c.agency_id"
            " WHERE c.agency_id = ? ORDER BY c.created_at DESC, c.contact_id",
            (self.agency_id,),
        ).fetchall()
        return tuple(
            {
                "contact_id": str(row["contact_id"]),
                "supplier_id": str(row["supplier_id"]),
                "supplier_name": None if row["supplier_name"] is None else str(row["supplier_name"]),
                "channel": str(row["channel"]),
                "masked_value": mask_contact_value(str(row["contact_value"])),
                "registration_basis": str(row["registration_basis"]),
                "created_by": str(row["created_by"]),
                "created_role": str(row["created_role"]),
                "created_at": str(row["created_at"]),
                "revoked_at": None if row["revoked_at"] is None else str(row["revoked_at"]),
                "revoke_reason": None if row["revoke_reason"] is None else str(row["revoke_reason"]),
                "standing": standing_of(row["revoked_at"], row["last_kind"]),
                "last_event_at": None if row["last_event_at"] is None else str(row["last_event_at"]),
            }
            for row in rows
        )

    def supplier_assertion_for_booking(
        self, *, booking_id: str, session_token: str
    ) -> Optional[dict[str, Any]]:
        """The verified callback attestation behind a booking's proof, and whether it is fresh.

        ``None`` means no attribution exists yet: the booking has no supplier callback on
        record. ``state`` reuses the gate's own window, so a row the screen calls ``FRESH`` is
        a row the confirmation path will still accept.
        """
        booking_id = require_ref(booking_id, "booking_id")
        self._resolve_principal(self.conn, session_token, allowed=CONTACT_READER_ROLES, step_up=False)
        row = self.conn.execute(
            "SELECT * FROM evidence_supplier_attributions WHERE booking_id = ? AND agency_id = ?"
            " ORDER BY recorded_at DESC, evidence_id DESC LIMIT 1",
            (booking_id, self.agency_id),
        ).fetchone()
        if row is None:
            return None
        max_age = self._assertion_max_age(self.conn)
        age = parse_iso(self._now()) - parse_iso(str(row["supplier_confirmed_at"]))
        return {
            "evidence_id": str(row["evidence_id"]),
            "booking_id": str(row["booking_id"]),
            "supplier_id": str(row["supplier_id"]),
            "relationship_id": str(row["relationship_id"]),
            "contact_id": str(row["contact_id"]),
            "supplier_confirmed_at": str(row["supplier_confirmed_at"]),
            "callback_at": str(row["callback_at"]),
            "callback_by": str(row["callback_by"]),
            "callback_role": str(row["callback_role"]),
            "max_age_hours": int(max_age.total_seconds() // 3600),
            "state": "EXPIRED" if age > max_age else "FRESH",
        }


def mask_contact_value(value: str) -> str:
    """A phone number with only its last four digits kept — enough to recognise, not to dial."""
    digits = [character for character in value if character.isdigit()]
    if len(digits) <= 4:
        return "\u2022" * len(digits)
    return "\u2026" + "".join(digits[-4:])


def standing_of(revoked_at: Any, last_kind: Any) -> str:
    """``REVOKED``, ``SUSPENDED``, ``VERIFIED`` or ``UNVERIFIED`` — revocation wins.

    The same rule as ``_contact_standing``; kept as a pure function so a read model can
    derive it without re-reading the contact row.
    """
    if revoked_at is not None:
        return "REVOKED"
    if last_kind is None:
        return "UNVERIFIED"
    return "SUSPENDED" if str(last_kind) == "SUSPENDED" else "VERIFIED"

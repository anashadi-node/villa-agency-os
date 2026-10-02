"""A tenant's own role set: rename freely, staff it, and never outrank the blueprint.

What a tenant owns, and what it does not
----------------------------------------
ADR-0035 decided the platform's roles; the owner then decided (D3) that each agency must be able to
**edit** them, because every agency names its people differently. This module draws that line:

* A tenant may **rename** a role (its own label), **disable** one, and **appoint holders**.
* A tenant may **not** move authority. Every role carries `based_on` — a canonical blueprint id
  from :mod:`roles` — and rank, capability and MFA duty are read from that blueprint. Renaming
  "Finance & Accounting Manager" to "Bagian Keuangan" cannot change what it may approve, because
  nothing in the ladder reads the label.
* A tenant may **not** resurrect what the owner deleted. The AI maker roles are refused as
  blueprints: an agency cannot appoint a human to `reservation_agent` or `finance_staff` and
  rebuild the maker function the owner removed.

Holders are opaque references, never names
------------------------------------------
The platform enforces *that* a ladder role has a holder; it does not need to know who the person
is. A staff name in this table would put personal data behind every gate, dashboard and audit read
for no enforcement benefit, so `holder_ref` is an opaque reference the tenant chooses (`mr_ceo`) —
exactly the shape the platform already uses for actors (`owner-sakti-1`).

Why staffing is part of readiness
---------------------------------
The money ladder is only real when it has people. A tenant with no holder for `finance_manager` or
`gm` cannot approve a payment at all, so :meth:`AgencyRoles.unstaffed_ladder_roles` names those
roles, and preflight refuses go-live while any of them is empty.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional

import roles as ROLES
from audit import append_event
from primitives import Clock, DomainError, IdFactory, SystemClock, require_ref, to_iso

__all__ = [
    "AgencyRole",
    "AgencyRoles",
    "AgencyRolesError",
    "BlueprintNotAssignable",
    "BLUEPRINT_LABELS",
    "BLUEPRINT_SEED",
    "LADDER_STAFFING_REQUIRED",
    "NON_ASSIGNABLE_BLUEPRINTS",
    "ROLE_EDITOR_ROLES",
    "RoleInUse",
    "RoleNotFound",
    "RoleRefused",
]

#: Who may edit the tenant's role set: the agency's own owner, its GM, and its system admin. Not
#: the AI, not finance, not an auditor, and not a reservation desk.
ROLE_EDITOR_ROLES: tuple[str, ...] = ("owner", "gm", "system_admin")

#: Blueprints a tenant may staff. The AI maker ids are refused: ADR-0035 deleted those *human*
#: roles, and a tenant must not be able to rebuild the maker by appointing someone to it.
NON_ASSIGNABLE_BLUEPRINTS: tuple[str, ...] = tuple(
    sorted(set(ROLES.AI_ROLE_IDS) | {"reservation_agent", "finance_staff"})
)

#: The eight human roles ADR-0035 names, seeded into a new agency so it starts from the blueprint
#: instead of an empty list. Seeding is idempotent: running it twice changes nothing.
BLUEPRINT_SEED: tuple[str, ...] = (
    "owner",
    "gm",
    "commercial_manager",
    "partnership_manager",
    "reservation_manager",
    "finance_manager",
    "system_admin",
    "greeting_service",
)

#: The default label each blueprint is seeded with: the names ADR-0035 already uses, so a new
#: agency starts from the owner's own wording instead of a slug like "gm". The label is the
#: tenant's to change; this is only the starting point.
BLUEPRINT_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "owner": "Owner",
        "gm": "GM/CEO",
        "commercial_manager": "Commercial & Marketing Manager",
        "partnership_manager": "Partnership Manager",
        "reservation_manager": "Operations/Reservation Manager",
        "finance_manager": "Finance & Accounting Manager",
        "system_admin": "Technology-Security Manager",
        "greeting_service": "Greeting Service Team",
    }
)

#: Blueprints whose *staffing* decides whether an agency may go live: the money-out ladder
#: (ADR-0036). Each needs at least one ACTIVE holder, or nobody can approve a payment.
LADDER_STAFFING_REQUIRED: frozenset[str] = ROLES.MONEY_OUT_APPROVER_ROLES

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ROLE_STATUSES: tuple[str, ...] = ("ACTIVE", "DISABLED")
HOLDER_STATUSES: tuple[str, ...] = ("ACTIVE", "INACTIVE")
MIN_LABEL_CHARS = 3
MAX_LABEL_CHARS = 80


class AgencyRolesError(DomainError):
    code = "AGENCY_ROLES_ERROR"


class RoleNotFound(AgencyRolesError):
    code = "ROLE_NOT_FOUND"


class RoleRefused(AgencyRolesError):
    """A role id or label the tenant may not use."""

    code = "ROLE_REFUSED"


class BlueprintNotAssignable(AgencyRolesError):
    """The blueprint is the AI maker or a deleted human role: not staffable by a tenant."""

    code = "BLUEPRINT_NOT_ASSIGNABLE"


class RoleInUse(AgencyRolesError):
    """The role still has an active holder, so disabling it would orphan authority."""

    code = "ROLE_IN_USE"


def _identifier(value: Any, field: str) -> str:
    text = require_ref(value, field, max_len=64)
    if not _ID_RE.match(text):
        raise RoleRefused(
            f"{field} must be lowercase letters, digits, dash or underscore "
            "(starting with a letter, 2 to 64 characters)",
            field=field,
            got=repr(value),
        )
    return text


def _label(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) < MIN_LABEL_CHARS:
        raise RoleRefused("a role needs a name of at least 3 characters", label=repr(value))
    if len(text) > MAX_LABEL_CHARS:
        raise RoleRefused(f"a role name may be at most {MAX_LABEL_CHARS} characters", label=repr(value))
    return text


def _blueprint(value: Any) -> str:
    text = str(value or "").strip()
    if text in NON_ASSIGNABLE_BLUEPRINTS:
        raise BlueprintNotAssignable(
            "this blueprint is not staffable by a tenant: the owner deleted that human role "
            "(ADR-0035) and its function belongs to the AI service principal",
            blueprint=text,
            refused=sorted(NON_ASSIGNABLE_BLUEPRINTS),
        )
    if not ROLES.is_known_role(text):
        raise BlueprintNotAssignable(
            "this blueprint is not a known role", blueprint=repr(value), known=sorted(ROLES.ROLE_IDS)
        )
    return text


@dataclass(frozen=True)
class AgencyRole:
    """One of a tenant's roles, with the authority read from its blueprint."""

    role_id: str
    label: str
    based_on: str
    status: str
    rank: Optional[int]
    holders: tuple[str, ...]

    @property
    def is_ladder(self) -> bool:
        return self.rank is not None

    @property
    def on_money_ladder(self) -> bool:
        return self.based_on in LADDER_STAFFING_REQUIRED

    @property
    def is_staffed(self) -> bool:
        return bool(self.holders)


@contextmanager
def _write_transaction(conn: Any) -> Iterator[None]:
    """Join the caller's transaction, or open one: a change and its audit event are one unit."""
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


class AgencyRoles:
    """The single writer of ``agency_roles`` and ``agency_role_holders`` for one agency."""

    def __init__(
        self,
        conn: Any,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = "agency_default",
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdFactory = ids if ids is not None else IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)

    # -- internals --------------------------------------------------------- #
    def _now(self, at: Optional[datetime]) -> datetime:
        if at is None:
            return self._clock.now()
        if at.tzinfo is None:
            raise AgencyRolesError("at must carry a timezone", code="INVALID_TIMESTAMP")
        return at

    def _editor(self, actor: str, actor_role: str) -> tuple[str, str]:
        who = require_ref(actor, "actor", max_len=128)
        role = require_ref(actor_role, "actor_role", max_len=64)
        if role not in ROLE_EDITOR_ROLES:
            raise RoleRefused(
                "this role may not edit the agency's roles",
                role=role,
                allowed=list(ROLE_EDITOR_ROLES),
            )
        return who, role

    def _row(self, role_id: str) -> Optional[Mapping[str, Any]]:
        row = self._conn.execute(
            "SELECT role_id, label, based_on, status, revision FROM agency_roles"
            " WHERE agency_id = ? AND role_id = ?",
            (self.agency_id, role_id),
        ).fetchone()
        return None if row is None else dict(row)

    def _holder_refs(self, role_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT holder_ref FROM agency_role_holders WHERE agency_id = ? AND role_id = ?"
            " AND status = 'ACTIVE' ORDER BY holder_ref",
            (self.agency_id, role_id),
        ).fetchall()
        return tuple(str(row[0] if not hasattr(row, "keys") else row["holder_ref"]) for row in rows)

    def _audit(self, *, action: str, entity_id: str, actor: str, role: str,
               before: Optional[Mapping[str, Any]], after: Mapping[str, Any], reason: str) -> None:
        append_event(
            self._conn,
            clock=self._clock,
            ids=self._ids,
            action=action,
            entity_type="agency_role",
            entity_id=entity_id,
            actor=actor,
            actor_role=role,
            before=before,
            after=after,
            reason=reason,
        )

    # -- reading ----------------------------------------------------------- #
    def roles(self, *, status: Optional[str] = None) -> tuple[AgencyRole, ...]:
        """Every role this agency has, with authority read from its blueprint."""
        sql = ("SELECT role_id, label, based_on, status FROM agency_roles WHERE agency_id = ?")
        params: list[Any] = [self.agency_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY role_id"
        result: list[AgencyRole] = []
        for row in self._conn.execute(sql, tuple(params)).fetchall():
            record = dict(row) if hasattr(row, "keys") else {
                "role_id": row[0], "label": row[1], "based_on": row[2], "status": row[3]
            }
            result.append(
                AgencyRole(
                    role_id=str(record["role_id"]),
                    label=str(record["label"]),
                    based_on=str(record["based_on"]),
                    status=str(record["status"]),
                    rank=ROLES.role_rank(str(record["based_on"])),
                    holders=self._holder_refs(str(record["role_id"])),
                )
            )
        return tuple(result)

    def get(self, role_id: str) -> AgencyRole:
        wanted = _identifier(role_id, "role_id")
        for role in self.roles():
            if role.role_id == wanted:
                return role
        raise RoleNotFound("this agency has no such role", role_id=wanted)

    def unstaffed_ladder_roles(self) -> tuple[str, ...]:
        """Money-ladder blueprints this agency has no active holder for. D17 refuses go-live on it."""
        staffed = {
            role.based_on
            for role in self.roles(status="ACTIVE")
            if role.on_money_ladder and role.is_staffed
        }
        return tuple(sorted(LADDER_STAFFING_REQUIRED - staffed))

    # -- writing ----------------------------------------------------------- #
    def seed_blueprint(self, *, actor: str, actor_role: str,
                       at: Optional[datetime] = None) -> tuple[AgencyRole, ...]:
        """Create the eight ADR-0035 roles for this agency. Idempotent by role id."""
        who, role_name = self._editor(actor, actor_role)
        moment = self._now(at)
        with _write_transaction(self._conn):
            for blueprint in BLUEPRINT_SEED:
                if self._row(blueprint) is not None:
                    continue
                self._conn.execute(
                    "INSERT INTO agency_roles (role_id, agency_id, label, based_on, status,"
                    " revision, updated_by, updated_role, updated_at, created_at)"
                    " VALUES (?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?, ?)",
                    (blueprint, self.agency_id, _label(BLUEPRINT_LABELS.get(blueprint, blueprint.replace("_", " ").title())),
                     blueprint, who, role_name, to_iso(moment), to_iso(moment)),
                )
                self._audit(
                    action="agency_role.seeded",
                    entity_id=blueprint,
                    actor=who,
                    role=role_name,
                    before=None,
                    after={"label": blueprint, "based_on": blueprint, "status": "ACTIVE"},
                    reason="seed the ADR-0035 blueprint for this agency",
                )
        return self.roles()

    def add_role(
        self,
        role_id: str,
        *,
        label: str,
        based_on: str,
        actor: str,
        actor_role: str,
        at: Optional[datetime] = None,
    ) -> AgencyRole:
        """Add a role of this agency's own, inheriting a blueprint's authority."""
        who, role_name = self._editor(actor, actor_role)
        new_id = _identifier(role_id, "role_id")
        blueprint = _blueprint(based_on)
        text = _label(label)
        if self._row(new_id) is not None:
            raise RoleRefused("this agency already has a role with that id", role_id=new_id)
        moment = self._now(at)
        with _write_transaction(self._conn):
            self._conn.execute(
                "INSERT INTO agency_roles (role_id, agency_id, label, based_on, status, revision,"
                " updated_by, updated_role, updated_at, created_at)"
                " VALUES (?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?, ?)",
                (new_id, self.agency_id, text, blueprint, who, role_name, to_iso(moment),
                 to_iso(moment)),
            )
            self._audit(
                action="agency_role.added",
                entity_id=new_id,
                actor=who,
                role=role_name,
                before=None,
                after={"label": text, "based_on": blueprint, "status": "ACTIVE"},
                reason="add a role for this agency",
            )
        return self.get(new_id)

    def rename_role(
        self,
        role_id: str,
        *,
        label: str,
        actor: str,
        actor_role: str,
        at: Optional[datetime] = None,
    ) -> AgencyRole:
        """Change the tenant's own name for a role. Authority does not move with the label."""
        who, role_name = self._editor(actor, actor_role)
        wanted = _identifier(role_id, "role_id")
        row = self._row(wanted)
        if row is None:
            raise RoleNotFound("this agency has no such role", role_id=wanted)
        text = _label(label)
        moment = self._now(at)
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE agency_roles SET label = ?, revision = revision + 1, updated_by = ?,"
                " updated_role = ?, updated_at = ? WHERE agency_id = ? AND role_id = ?",
                (text, who, role_name, to_iso(moment), self.agency_id, wanted),
            )
            self._audit(
                action="agency_role.renamed",
                entity_id=wanted,
                actor=who,
                role=role_name,
                before={"label": row["label"]},
                after={"label": text, "based_on": row["based_on"]},
                reason="rename a role (the blueprint keeps the authority)",
            )
        return self.get(wanted)

    def disable_role(
        self, role_id: str, *, actor: str, actor_role: str, at: Optional[datetime] = None
    ) -> AgencyRole:
        """Deactivate a role. Refused while it still has an active holder."""
        who, role_name = self._editor(actor, actor_role)
        wanted = _identifier(role_id, "role_id")
        row = self._row(wanted)
        if row is None:
            raise RoleNotFound("this agency has no such role", role_id=wanted)
        holders = self._holder_refs(wanted)
        if holders:
            raise RoleInUse(
                "this role still has an active holder: remove them first, so nobody keeps "
                "authority through a role the agency believes is switched off",
                role_id=wanted,
                holders=list(holders),
            )
        moment = self._now(at)
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE agency_roles SET status = 'DISABLED', revision = revision + 1,"
                " updated_by = ?, updated_role = ?, updated_at = ?"
                " WHERE agency_id = ? AND role_id = ?",
                (who, role_name, to_iso(moment), self.agency_id, wanted),
            )
            self._audit(
                action="agency_role.disabled",
                entity_id=wanted,
                actor=who,
                role=role_name,
                before={"status": row["status"]},
                after={"status": "DISABLED"},
                reason="disable a role for this agency",
            )
        return self.get(wanted)

    def enable_role(
        self, role_id: str, *, actor: str, actor_role: str, at: Optional[datetime] = None
    ) -> AgencyRole:
        who, role_name = self._editor(actor, actor_role)
        wanted = _identifier(role_id, "role_id")
        row = self._row(wanted)
        if row is None:
            raise RoleNotFound("this agency has no such role", role_id=wanted)
        moment = self._now(at)
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE agency_roles SET status = 'ACTIVE', revision = revision + 1,"
                " updated_by = ?, updated_role = ?, updated_at = ?"
                " WHERE agency_id = ? AND role_id = ?",
                (who, role_name, to_iso(moment), self.agency_id, wanted),
            )
            self._audit(
                action="agency_role.enabled",
                entity_id=wanted,
                actor=who,
                role=role_name,
                before={"status": row["status"]},
                after={"status": "ACTIVE"},
                reason="enable a role for this agency",
            )
        return self.get(wanted)

    def assign_holder(
        self,
        role_id: str,
        *,
        holder_ref: str,
        actor: str,
        actor_role: str,
        note: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> AgencyRole:
        """Appoint an opaque holder reference to a role. No personal names are stored."""
        who, role_name = self._editor(actor, actor_role)
        wanted = _identifier(role_id, "role_id")
        row = self._row(wanted)
        if row is None:
            raise RoleNotFound("this agency has no such role", role_id=wanted)
        if row["status"] != "ACTIVE":
            raise RoleRefused(
                "this role is disabled: enable it before appointing a holder", role_id=wanted
            )
        holder = _identifier(holder_ref, "holder_ref")
        moment = self._now(at)
        existing = self._conn.execute(
            "SELECT holder_id, status FROM agency_role_holders"
            " WHERE agency_id = ? AND role_id = ? AND holder_ref = ?",
            (self.agency_id, wanted, holder),
        ).fetchone()
        with _write_transaction(self._conn):
            if existing is None:
                self._conn.execute(
                    "INSERT INTO agency_role_holders (holder_id, agency_id, role_id, holder_ref,"
                    " status, note, updated_by, updated_at, created_at)"
                    " VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)",
                    (self._ids.new("holder"), self.agency_id, wanted, holder, note, who,
                     to_iso(moment), to_iso(moment)),
                )
                action = "agency_role.holder_appointed"
            else:
                record = dict(existing) if hasattr(existing, "keys") else {
                    "holder_id": existing[0], "status": existing[1]}
                if record["status"] == "ACTIVE":
                    return self.get(wanted)  # already appointed: nothing to change
                self._conn.execute(
                    "UPDATE agency_role_holders SET status = 'ACTIVE', note = ?, updated_by = ?,"
                    " updated_at = ? WHERE holder_id = ?",
                    (note, who, to_iso(moment), record["holder_id"]),
                )
                action = "agency_role.holder_reappointed"
            self._audit(
                action=action,
                entity_id=wanted,
                actor=who,
                role=role_name,
                before=None,
                after={"holder_ref": holder},
                reason="appoint a holder to a role",
            )
        return self.get(wanted)

    def deactivate_holder(
        self,
        role_id: str,
        *,
        holder_ref: str,
        actor: str,
        actor_role: str,
        at: Optional[datetime] = None,
    ) -> AgencyRole:
        who, role_name = self._editor(actor, actor_role)
        wanted = _identifier(role_id, "role_id")
        holder = _identifier(holder_ref, "holder_ref")
        row = self._conn.execute(
            "SELECT holder_id, status FROM agency_role_holders"
            " WHERE agency_id = ? AND role_id = ? AND holder_ref = ?",
            (self.agency_id, wanted, holder),
        ).fetchone()
        if row is None:
            raise RoleNotFound(
                "no such holder on this role", role_id=wanted, holder_ref=holder
            )
        record = dict(row) if hasattr(row, "keys") else {"holder_id": row[0], "status": row[1]}
        moment = self._now(at)
        with _write_transaction(self._conn):
            self._conn.execute(
                "UPDATE agency_role_holders SET status = 'INACTIVE', updated_by = ?, updated_at = ?"
                " WHERE holder_id = ?",
                (who, to_iso(moment), record["holder_id"]),
            )
            self._audit(
                action="agency_role.holder_removed",
                entity_id=wanted,
                actor=who,
                role=role_name,
                before={"holder_ref": holder, "status": record["status"]},
                after={"holder_ref": holder, "status": "INACTIVE"},
                reason="remove a holder from a role",
            )
        return self.get(wanted)

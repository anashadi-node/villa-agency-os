"""Supplier route: villa↔supplier relationship, contract versions, rate versions.

This is the commercial foundation of WAVE 2, and it exists to make one sentence from
the Master Spec v1.1 enforceable:

    VILLA TRUTH — rate, feature, inclusion, exclusion, service and policy come from
    *verified applicable supplier/contract data* (§2), and there is no
    ``AI_EXTRACTED → ACTIVE`` transition (§12).

How that is enforced, and why each layer is needed:

1. **Provenance is a column, not a convention.** Every contract and rate version
   records ``source_doc_ref``, ``source_reference`` (section/page/quote) and
   ``extraction_method``. A commercial fact without a source is a rumour.
2. **The database refuses the forbidden transition.** ``ck_*_activation_is_human``
   rejects ``ACTIVE`` without a named verifier *and* approver, and
   ``ck_*_verified`` rejects ``VERIFIED`` without a verifier. So an AI extraction can
   only ever be a *draft source*: it cannot reach ACTIVE by any code path, because it
   cannot reach ACTIVE without a human, and the row says so.
3. **The service refuses machine actors on the human steps.** Verification, approval
   and activation are human authority (§18/§56/§21). A machine role is refused before
   anything is written, so "the AI activated a rate" is not a state this system can
   represent.
4. **A rate names its contract before it can be ACTIVE**, so a rate cannot float free
   of the agreement it came from.
5. **Applicability is deterministic.** ``applicable_rate`` answers only from an ACTIVE
   rate whose window covers the date and whose contract is itself ACTIVE — otherwise it
   refuses (``RATE_NOT_VERIFIED`` / ``RATE_NOT_APPLICABLE``) instead of guessing. An
   expired or future-effective version is never silently used (§12).
6. **Overlap is refused, not merged.** Activating a version supersedes the previous
   ACTIVE one in the same transaction; pre-existing overlap is reported by
   ``conflicts()`` as a data-quality defect rather than hidden.

Not in this wave: season segments, occupancy surcharges, discount rules, quotation
snapshots — those belong to Pricing & Quotation (WAVE 4) and Reservation (WAVE 5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    UTC,
    parse_iso_date,
    require_ref,
    require_text,
    to_iso,
    to_iso_date,
)

__all__ = [
    "CONTRACT_STATUSES",
    "EXTRACTION_METHODS",
    "MACHINE_ROLES",
    "RATE_STATUSES",
    "ContractConflict",
    "ContractNotFound",
    "InvalidTransition",
    "RateNotApplicable",
    "RateNotFound",
    "RateNotVerified",
    "RouteNotFound",
    "SupplierRouteError",
    "SupplierRouteService",
    "VerificationRequired",
]

CONTRACT_STATUSES: tuple[str, ...] = (
    "DRAFT", "PENDING_VERIFICATION", "VERIFIED", "APPROVED", "ACTIVE", "SUPERSEDED", "EXPIRED",
)
RATE_STATUSES = CONTRACT_STATUSES
EXTRACTION_METHODS: tuple[str, ...] = ("HUMAN", "AI_EXTRACTED")

#: Legal forward moves. Anything else is refused — the pipeline is not a slider.
TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "DRAFT": ("PENDING_VERIFICATION",),
    "PENDING_VERIFICATION": ("VERIFIED", "DRAFT"),
    "VERIFIED": ("APPROVED",),
    "APPROVED": ("ACTIVE",),
    "ACTIVE": ("SUPERSEDED", "EXPIRED"),
    "SUPERSEDED": (),
    "EXPIRED": (),
}

#: Identities that may never verify, approve or activate commercial data (§18, §21).
MACHINE_ROLES: tuple[str, ...] = ("ai_service", "system_service", "system")
MACHINE_ACTOR_PREFIXES: tuple[str, ...] = ("system:", "ai:", "service:")


class SupplierRouteError(DomainError):
    """Base class for deliberate refusals from this module."""


class RouteNotFound(SupplierRouteError):
    code = "ROUTE_NOT_FOUND"


class ContractNotFound(SupplierRouteError):
    code = "CONTRACT_NOT_FOUND"


class RateNotFound(SupplierRouteError):
    code = "RATE_NOT_FOUND"


class InvalidTransition(SupplierRouteError):
    code = "STATE_TRANSITION_NOT_ALLOWED"


class VerificationRequired(SupplierRouteError):
    code = "VERIFICATION_REQUIRED"


class RateNotVerified(SupplierRouteError):
    code = "RATE_NOT_VERIFIED"


class RateNotApplicable(SupplierRouteError):
    code = "RATE_NOT_APPLICABLE"


class ContractConflict(SupplierRouteError):
    code = "COMMERCIAL_DATA_RECONFIRMATION_REQUIRED"


@dataclass(frozen=True)
class ApplicableRate:
    """The one rate that may be quoted for a route on a date."""

    rate_version_id: str
    contract_version_id: str
    currency: str
    nightly_minor: int
    min_stay: int
    effective_from: str
    effective_to: str


def _require_human(actor: Any, actor_role: Any) -> tuple[str, str]:
    """Verification, approval and activation are human authority (§18/§21/§56)."""
    who = require_ref(actor, "actor", max_len=64)
    role = require_ref(actor_role, "actor_role", max_len=64)
    lowered = who.lower()
    if role.lower() in MACHINE_ROLES or any(lowered.startswith(p) for p in MACHINE_ACTOR_PREFIXES):
        raise VerificationRequired(
            "this step is human authority: a machine/AI identity is refused",
            code="VERIFICATION_REQUIRED",
            actor=who,
            actor_role=role,
        )
    return who, role


class SupplierRouteService:
    """The single writer of ``suppliers``/``villa_supplier_relationships`` and of the
    contract/rate version chains."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = DEFAULT_AGENCY_ID,
        actor: str = "system:supplier_route",
        actor_role: str = "system_service",
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self.actor = require_ref(actor, "actor", max_len=64)
        self.actor_role = require_ref(actor_role, "actor_role", max_len=64)

    # -- plumbing ---------------------------------------------------------- #
    def _stamp(self) -> str:
        return to_iso(self.clock.now().astimezone(UTC))

    def _row(self, table: str, key: str, value: str) -> sqlite3.Row:
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key} = ? AND agency_id = ?", (value, self.agency_id)
        ).fetchone()
        return row

    def _route_row(self, route_id: str) -> sqlite3.Row:
        row = self._row("villa_supplier_relationships", "villa_supplier_relationship_id",
                        require_ref(route_id, "villa_supplier_relationship_id"))
        if row is None:
            raise RouteNotFound(f"villa-supplier route not found: {route_id}",
                                code="ROUTE_NOT_FOUND", route_id=route_id)
        return row

    def _contract_row(self, contract_version_id: str) -> sqlite3.Row:
        row = self._row("contract_versions", "contract_version_id",
                        require_ref(contract_version_id, "contract_version_id"))
        if row is None:
            raise ContractNotFound(f"contract version not found: {contract_version_id}",
                                   code="CONTRACT_NOT_FOUND",
                                   contract_version_id=contract_version_id)
        return row

    def _rate_row(self, rate_version_id: str) -> sqlite3.Row:
        row = self._row("rate_versions", "rate_version_id",
                        require_ref(rate_version_id, "rate_version_id"))
        if row is None:
            raise RateNotFound(f"rate version not found: {rate_version_id}",
                               code="RATE_NOT_FOUND", rate_version_id=rate_version_id)
        return row

    @staticmethod
    def _require_status(status: Any, allowed: Sequence[str], field: str = "status") -> str:
        if not isinstance(status, str) or status not in allowed:
            raise InvalidTransition(f"unknown {field}: {status!r}",
                                    code="INVALID_STATUS", field=field, allowed=list(allowed))
        return status

    @staticmethod
    def _require_window(effective_from: Any, effective_to: Any) -> tuple[str, str]:
        try:
            start = to_iso_date(parse_iso_date(effective_from))
            end = to_iso_date(parse_iso_date(effective_to))
        except DomainError as exc:
            raise InvalidTransition(str(exc), code="INVALID_EFFECTIVE_WINDOW") from exc
        if start > end:
            raise InvalidTransition("effective_from must not be after effective_to",
                                    code="INVALID_EFFECTIVE_WINDOW",
                                    effective_from=start, effective_to=end)
        return start, end

    def _require_citable_evidence(self, evidence_id: Any) -> str:
        """Provenance must point at evidence that completed the quarantine pipeline.

        §12 puts a document at the head of the provenance chain, so citing a file still
        in quarantine — or one belonging to another agency — is refused here and not
        merely discouraged in a form.
        """
        evidence = require_ref(evidence_id, "source_evidence_id")
        row = self.conn.execute(
            "SELECT status FROM evidence_objects WHERE evidence_id = ? AND agency_id = ?",
            (evidence, self.agency_id),
        ).fetchone()
        if row is None:
            raise SupplierRouteError(
                f"source evidence not found in this agency: {evidence}",
                code="RESOURCE_NOT_FOUND", source_evidence_id=evidence,
            )
        if str(row["status"]) != "STORED":
            raise VerificationRequired(
                "source evidence has not completed the quarantine pipeline",
                code="VERIFICATION_REQUIRED",
                source_evidence_id=evidence, evidence_status=str(row["status"]),
            )
        return evidence

    def _next_version(self, table: str, route_id: str) -> int:
        row = self.conn.execute(
            f"SELECT COALESCE(MAX(version), 0) AS top FROM {table}"
            " WHERE villa_supplier_relationship_id = ? AND agency_id = ?",
            (route_id, self.agency_id),
        ).fetchone()
        return int(row["top"]) + 1

    def _require_transition(self, current: str, target: str, entity: str, key: str, value: str) -> None:
        if target not in TRANSITIONS.get(current, ()):
            raise InvalidTransition(
                f"{entity} cannot move from {current} to {target}",
                code="STATE_TRANSITION_NOT_ALLOWED",
                entity=entity,
                **{key: value},
                status=current,
                target=target,
                allowed=list(TRANSITIONS.get(current, ())),
            )

    def _set_status(self, table: str, key: str, value: str, status: str,
                    extra_columns: Sequence[str] = (), extra_values: Sequence[Any] = ()) -> None:
        assignments = ["status = ?", "updated_at = ?"] + [f"{c} = ?" for c in extra_columns]
        self.conn.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {key} = ? AND agency_id = ?",
            (status, self._stamp(), *extra_values, value, self.agency_id),
        )

    # -- master data ------------------------------------------------------- #
    def register_supplier(self, name: Any) -> str:
        """Create a supplier. MASTER data: mutable, audited by the caller."""
        label = require_text(name, "name", max_len=200)
        stamp = self._stamp()
        supplier_id = self.ids.new("supplier")
        self.conn.execute(
            "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
            (supplier_id, self.agency_id, label, stamp, stamp),
        )
        return supplier_id

    def link_villa(self, supplier_id: Any, *, property_id: Any = None, unit_id: Any = None) -> str:
        """The villa↔supplier relationship — the route a reservation must carry (§5).

        Exactly one of ``property_id``/``unit_id`` identifies the inventory side: a
        relationship to "the villa" and one to "a specific unit" are different
        commercial facts, and accepting both or neither would make applicability
        ambiguous.
        """
        supplier = require_ref(supplier_id, "supplier_id")
        if (property_id is None) == (unit_id is None):
            raise SupplierRouteError(
                "a route must name exactly one inventory side (property_id or unit_id)",
                code="INVALID_ROUTE_TARGET",
            )
        target_column = "property_id" if property_id is not None else "unit_id"
        target_value = require_ref(property_id if property_id is not None else unit_id,
                                   target_column, max_len=64)
        table = "properties" if property_id is not None else "units"
        key = "property_id" if property_id is not None else "unit_id"
        if self._row(table, key, target_value) is None:
            raise SupplierRouteError(
                f"{target_column} does not exist in this agency: {target_value}",
                code="INVENTORY_NOT_FOUND",
                **{target_column: target_value},
            )
        if self._row("suppliers", "supplier_id", supplier) is None:
            raise SupplierRouteError(f"supplier not found: {supplier}",
                                     code="SUPPLIER_NOT_FOUND", supplier_id=supplier)
        stamp = self._stamp()
        route_id = self.ids.new("route")
        self.conn.execute(
            "INSERT INTO villa_supplier_relationships (villa_supplier_relationship_id, agency_id,"
            " supplier_id, property_id, unit_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (route_id, self.agency_id, supplier,
             target_value if property_id is not None else None,
             target_value if unit_id is not None else None, stamp, stamp),
        )
        return route_id

    def route_for_unit(self, unit_id: Any) -> str:
        """The ACTIVE route a unit is sold through, or a refusal."""
        unit = require_ref(unit_id, "unit_id")
        row = self.conn.execute(
            "SELECT villa_supplier_relationship_id FROM villa_supplier_relationships"
            " WHERE unit_id = ? AND agency_id = ? AND status = 'ACTIVE'",
            (unit, self.agency_id),
        ).fetchone()
        if row is None:
            raise RouteNotFound(f"unit {unit} has no active supplier route",
                                code="ROUTE_NOT_FOUND", unit_id=unit)
        return str(row["villa_supplier_relationship_id"])

    # -- contract versions ------------------------------------------------- #
    def draft_contract(
        self,
        route_id: Any,
        *,
        source_doc_ref: Any,
        source_reference: Any,
        effective_from: Any,
        effective_to: Any,
        extraction_method: str = "HUMAN",
        source_evidence_id: Any = None,
    ) -> str:
        """Open a new contract version as a DRAFT, with its provenance attached."""
        route = self._route_row(route_id)
        evidence = (
            None if source_evidence_id is None else self._require_citable_evidence(source_evidence_id)
        )
        method = self._require_status(extraction_method, EXTRACTION_METHODS, "extraction_method")
        doc = require_text(source_doc_ref, "source_doc_ref", max_len=300)
        reference = require_text(source_reference, "source_reference", max_len=300)
        start, end = self._require_window(effective_from, effective_to)
        stamp = self._stamp()
        contract_version_id = self.ids.new("contract")
        self.conn.execute(
            "INSERT INTO contract_versions (contract_version_id, agency_id,"
            " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
            " source_doc_ref, source_reference, source_evidence_id, extraction_method,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?, ?)",
            (contract_version_id, self.agency_id, str(route["villa_supplier_relationship_id"]),
             self._next_version("contract_versions", str(route["villa_supplier_relationship_id"])),
             start, end, doc, reference, evidence, method, stamp, stamp),
        )
        return contract_version_id

    def submit_contract(self, contract_version_id: Any) -> None:
        row = self._contract_row(contract_version_id)
        self._require_transition(str(row["status"]), "PENDING_VERIFICATION", "contract",
                                 "contract_version_id", str(row["contract_version_id"]))
        self._set_status("contract_versions", "contract_version_id",
                         str(row["contract_version_id"]), "PENDING_VERIFICATION")

    def verify_contract(self, contract_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        who, _role = _require_human(actor, actor_role)
        row = self._contract_row(contract_version_id)
        self._require_transition(str(row["status"]), "VERIFIED", "contract",
                                 "contract_version_id", str(row["contract_version_id"]))
        self._set_status("contract_versions", "contract_version_id",
                         str(row["contract_version_id"]), "VERIFIED",
                         extra_columns=("verified_by", "verified_at"), extra_values=(who, self._stamp()))

    def approve_contract(self, contract_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        who, _role = _require_human(actor, actor_role)
        row = self._contract_row(contract_version_id)
        self._require_transition(str(row["status"]), "APPROVED", "contract",
                                 "contract_version_id", str(row["contract_version_id"]))
        self._set_status("contract_versions", "contract_version_id",
                         str(row["contract_version_id"]), "APPROVED",
                         extra_columns=("approved_by", "approved_at"), extra_values=(who, self._stamp()))

    def activate_contract(self, contract_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        """Make a contract version ACTIVE, superseding the route's previous ACTIVE one."""
        who, _role = _require_human(actor, actor_role)
        row = self._contract_row(contract_version_id)
        current = str(row["status"])
        self._require_transition(current, "ACTIVE", "contract",
                                 "contract_version_id", str(row["contract_version_id"]))
        route_id = str(row["villa_supplier_relationship_id"])
        stamp = self._stamp()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE contract_versions SET status = 'SUPERSEDED', updated_at = ?"
                " WHERE agency_id = ? AND villa_supplier_relationship_id = ?"
                " AND status = 'ACTIVE' AND contract_version_id <> ?",
                (stamp, self.agency_id, route_id, str(row["contract_version_id"])),
            )
            self.conn.execute(
                "UPDATE contract_versions SET status = 'ACTIVE', approved_by = COALESCE(approved_by, ?),"
                " approved_at = COALESCE(approved_at, ?), updated_at = ?"
                " WHERE contract_version_id = ? AND agency_id = ?",
                (who, stamp, stamp, str(row["contract_version_id"]), self.agency_id),
            )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- rate versions ----------------------------------------------------- #
    def draft_rate(
        self,
        route_id: Any,
        *,
        currency: Any,
        nightly_minor: Any,
        effective_from: Any,
        effective_to: Any,
        source_doc_ref: Any,
        source_reference: Any,
        contract_version_id: Any = None,
        min_stay: int = 1,
        extraction_method: str = "HUMAN",
        source_evidence_id: Any = None,
    ) -> str:
        """Open a new rate version as a DRAFT. Money is integer minor units."""
        route = self._route_row(route_id)
        evidence = (
            None if source_evidence_id is None else self._require_citable_evidence(source_evidence_id)
        )
        method = self._require_status(extraction_method, EXTRACTION_METHODS, "extraction_method")
        code = require_text(currency, "currency", max_len=3).upper()
        if len(code) != 3:
            raise SupplierRouteError("currency must be three letters (ISO 4217)",
                                     code="INVALID_CURRENCY", currency=currency)
        if not isinstance(nightly_minor, int) or isinstance(nightly_minor, bool) or nightly_minor < 0:
            raise SupplierRouteError("nightly_minor must be an integer minor-unit amount >= 0",
                                     code="INVALID_AMOUNT", nightly_minor=nightly_minor)
        if not isinstance(min_stay, int) or isinstance(min_stay, bool) or min_stay < 1:
            raise SupplierRouteError("min_stay must be an integer >= 1", code="INVALID_MIN_STAY",
                                     min_stay=min_stay)
        contract = None
        if contract_version_id is not None:
            contract_row = self._contract_row(contract_version_id)
            if str(contract_row["villa_supplier_relationship_id"]) != str(
                route["villa_supplier_relationship_id"]
            ):
                raise ContractConflict(
                    "that contract version belongs to another route",
                    code="COMMERCIAL_DATA_RECONFIRMATION_REQUIRED",
                    contract_version_id=str(contract_row["contract_version_id"]),
                    route_id=str(route["villa_supplier_relationship_id"]),
                )
            contract = str(contract_row["contract_version_id"])
        start, end = self._require_window(effective_from, effective_to)
        stamp = self._stamp()
        rate_version_id = self.ids.new("rate")
        route_key = str(route["villa_supplier_relationship_id"])
        self.conn.execute(
            "INSERT INTO rate_versions (rate_version_id, agency_id,"
            " villa_supplier_relationship_id, contract_version_id, version, status, currency,"
            " nightly_minor, min_stay, effective_from, effective_to, source_doc_ref,"
            " source_reference, source_evidence_id, extraction_method, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rate_version_id, self.agency_id, route_key, contract,
             self._next_version("rate_versions", route_key), code, nightly_minor, min_stay,
             start, end, require_text(source_doc_ref, "source_doc_ref", max_len=300),
             require_text(source_reference, "source_reference", max_len=300), evidence,
             method, stamp, stamp),
        )
        return rate_version_id

    def submit_rate(self, rate_version_id: Any) -> None:
        row = self._rate_row(rate_version_id)
        self._require_transition(str(row["status"]), "PENDING_VERIFICATION", "rate",
                                 "rate_version_id", str(row["rate_version_id"]))
        self._set_status("rate_versions", "rate_version_id", str(row["rate_version_id"]),
                         "PENDING_VERIFICATION")

    def verify_rate(self, rate_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        who, _role = _require_human(actor, actor_role)
        row = self._rate_row(rate_version_id)
        self._require_transition(str(row["status"]), "VERIFIED", "rate",
                                 "rate_version_id", str(row["rate_version_id"]))
        self._set_status("rate_versions", "rate_version_id", str(row["rate_version_id"]), "VERIFIED",
                         extra_columns=("verified_by", "verified_at"), extra_values=(who, self._stamp()))

    def approve_rate(self, rate_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        who, _role = _require_human(actor, actor_role)
        row = self._rate_row(rate_version_id)
        self._require_transition(str(row["status"]), "APPROVED", "rate",
                                 "rate_version_id", str(row["rate_version_id"]))
        self._set_status("rate_versions", "rate_version_id", str(row["rate_version_id"]), "APPROVED",
                         extra_columns=("approved_by", "approved_at"), extra_values=(who, self._stamp()))

    def activate_rate(self, rate_version_id: Any, *, actor: Any, actor_role: Any) -> None:
        """ACTIVE needs a human approver *and* a named contract (VILLA TRUTH, §2/§12)."""
        who, _role = _require_human(actor, actor_role)
        row = self._rate_row(rate_version_id)
        self._require_transition(str(row["status"]), "ACTIVE", "rate",
                                 "rate_version_id", str(row["rate_version_id"]))
        if row["contract_version_id"] is None:
            raise VerificationRequired(
                "a rate cannot be ACTIVE without the contract version that governs it",
                code="VERIFICATION_REQUIRED",
                rate_version_id=str(row["rate_version_id"]),
            )
        contract = self._contract_row(str(row["contract_version_id"]))
        if str(contract["status"]) not in ("VERIFIED", "APPROVED", "ACTIVE"):
            raise VerificationRequired(
                "the governing contract is not verified",
                code="VERIFICATION_REQUIRED",
                contract_version_id=str(contract["contract_version_id"]),
                contract_status=str(contract["status"]),
            )
        route_id = str(row["villa_supplier_relationship_id"])
        stamp = self._stamp()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE rate_versions SET status = 'SUPERSEDED', updated_at = ?"
                " WHERE agency_id = ? AND villa_supplier_relationship_id = ?"
                " AND status = 'ACTIVE' AND rate_version_id <> ?",
                (stamp, self.agency_id, route_id, str(row["rate_version_id"])),
            )
            self.conn.execute(
                "UPDATE rate_versions SET status = 'ACTIVE', approved_by = COALESCE(approved_by, ?),"
                " approved_at = COALESCE(approved_at, ?), updated_at = ?"
                " WHERE rate_version_id = ? AND agency_id = ?",
                (who, stamp, stamp, str(row["rate_version_id"]), self.agency_id),
            )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- reads ------------------------------------------------------------- #
    def applicable_rate(self, route_id: Any, on_date: Any) -> ApplicableRate:
        """The only rate that may be quoted for this route on this date.

        Refuses instead of guessing: a rate that is not ACTIVE, whose contract is not
        ACTIVE, or whose window does not cover the date is not "close enough" — it is
        not applicable (§12).
        """
        route = self._route_row(route_id)
        route_key = str(route["villa_supplier_relationship_id"])
        try:
            when = to_iso_date(parse_iso_date(on_date))
        except DomainError as exc:
            raise RateNotApplicable(str(exc), code="INVALID_DATE", on_date=on_date) from exc
        rows = self.conn.execute(
            "SELECT * FROM rate_versions WHERE agency_id = ? AND villa_supplier_relationship_id = ?"
            " AND status = 'ACTIVE' ORDER BY effective_from DESC",
            (self.agency_id, route_key),
        ).fetchall()
        if not rows:
            raise RateNotVerified("this route has no ACTIVE rate",
                                  code="RATE_NOT_VERIFIED", route_id=route_key)
        covering = [r for r in rows if str(r["effective_from"]) <= when <= str(r["effective_to"])]
        if not covering:
            raise RateNotApplicable("no ACTIVE rate covers that date",
                                    code="RATE_NOT_APPLICABLE", route_id=route_key, on_date=when,
                                    active_windows=[(str(r["effective_from"]), str(r["effective_to"]))
                                                    for r in rows])
        if len(covering) > 1:
            raise ContractConflict(
                "more than one ACTIVE rate covers the same date",
                code="COMMERCIAL_DATA_RECONFIRMATION_REQUIRED",
                route_id=route_key,
                on_date=when,
                rate_version_ids=[str(r["rate_version_id"]) for r in covering],
            )
        chosen = covering[0]
        contract_id = str(chosen["contract_version_id"])
        contract = self._contract_row(contract_id)
        if str(contract["status"]) != "ACTIVE":
            raise RateNotVerified(
                "the rate is ACTIVE but its governing contract is not ACTIVE",
                code="RATE_NOT_VERIFIED",
                rate_version_id=str(chosen["rate_version_id"]),
                contract_status=str(contract["status"]),
            )
        return ApplicableRate(
            rate_version_id=str(chosen["rate_version_id"]),
            contract_version_id=contract_id,
            currency=str(chosen["currency"]),
            nightly_minor=int(chosen["nightly_minor"]),
            min_stay=int(chosen["min_stay"]),
            effective_from=str(chosen["effective_from"]),
            effective_to=str(chosen["effective_to"]),
        )

    def conflicts(self, route_id: Any) -> tuple[Mapping[str, Any], ...]:
        """Data-quality view: ACTIVE versions that overlap each other on one route."""
        route = self._route_row(route_id)
        route_key = str(route["villa_supplier_relationship_id"])
        found: list[Mapping[str, Any]] = []
        for table, key, kind in (
            ("contract_versions", "contract_version_id", "CONTRACT"),
            ("rate_versions", "rate_version_id", "RATE"),
        ):
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE agency_id = ? AND"
                " villa_supplier_relationship_id = ? AND status = 'ACTIVE'"
                " ORDER BY effective_from",
                (self.agency_id, route_key),
            ).fetchall()
            for earlier, later in zip(rows, rows[1:]):
                if str(later["effective_from"]) <= str(earlier["effective_to"]):
                    found.append({
                        "kind": kind,
                        "route_id": route_key,
                        "earlier": str(earlier[key]),
                        "later": str(later[key]),
                        "overlap_from": str(later["effective_from"]),
                        "overlap_to": min(str(earlier["effective_to"]), str(later["effective_to"])),
                    })
        return tuple(found)

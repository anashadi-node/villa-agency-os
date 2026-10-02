"""Quotation versions, their snapshots, and the acceptance (§11).

§11 requires *immutable* versioning: a quotation is V1, V2, V3 …, and a version that
was sent or accepted is never overwritten. This module owns the chain, and the
database enforces the freeze (``trg_quotation_versions_frozen_content``), so the
guarantee does not depend on this module remembering to be careful.

The snapshots are the reason an acceptance can be settled later. §11 lists exactly
what an acceptance must name — ``quotation_version_id``, ``price_snapshot_id``,
``terms_snapshot_id``, ``policy_snapshot_id``, ``accepted_at`` — because "the price we
would calculate today" and "the price the guest agreed to" are different facts, and
only the second one can end a dispute. They are written when the version is *sent*,
never when it is accepted: the record has to be complete before the guest answers,
which is also why the schema refuses a new snapshot on an accepted version.

Sending is a client communication, so §56 classes it as medium risk and it goes
through the shared policy gate (``ai_service.require_tool_authority``) on the
catalogue's own ``quotation.send`` tool.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ai_service import require_tool_authority
from audit import append_event
from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    canonical_json,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "QUOTATION_STATES",
    "SEND_TOOL",
    "SNAPSHOT_KINDS",
    "Acceptance",
    "InvalidQuotation",
    "Quotation",
    "QuotationAlreadyAccepted",
    "QuotationAlreadySent",
    "QuotationError",
    "QuotationNotSent",
    "QuotationNotFound",
    "QuotationPolicyNotFound",
    "QuotationQuoteNotFound",
    "QuotationService",
    "QuotationTransitionRefused",
    "Snapshot",
    "can_transition",
]

#: Every state a ``quotation_versions`` row may hold, mirroring the CHECK constraint
#: verbatim. ``SUPERSEDED`` is the word the contract and rate chains already use for a
#: version a newer one replaced, so the vocabulary is reused rather than invented.
QUOTATION_STATES: tuple[str, ...] = ("DRAFT", "SENT", "ACCEPTED", "SUPERSEDED", "VOID")

#: §11 names three snapshots per version: what it costs, what it binds the guest to,
#: and which policy governed it. Two of the three would leave "what was agreed"
#: ambiguous, which is why the schema allows exactly one of each kind.
SNAPSHOT_KINDS: tuple[str, ...] = ("PRICE", "TERMS", "POLICY")

#: The §56 catalogue tool this module enforces (§56, ADR-0025). Sending a quotation
#: to a guest is a client communication, so it is medium risk and policy-controlled.
SEND_TOOL = "quotation.send"

#: The states a version never leaves. An accepted version is a settled fact; a
#: superseded one is history; a void one was withdrawn. Not exported as a name: the
#: only reader is the transition table below, and a second copy of the set would be a
#: second vocabulary.
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("SENT", "VOID"),
    # A sent version can be accepted, replaced by a newer version, or withdrawn.
    "SENT": ("ACCEPTED", "SUPERSEDED", "VOID"),
    "ACCEPTED": (),
    "SUPERSEDED": (),
    "VOID": (),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """Whether ``from_state -> to_state`` is a legal edge.

    An unknown state has no outgoing edges, so a typo refuses rather than passes.
    """
    return to_state in _TRANSITIONS.get(from_state, ())


class QuotationError(DomainError):
    """A deliberate refusal from the quotation domain."""


class QuotationNotFound(QuotationError):
    """No such quotation version inside this agency."""


class QuotationQuoteNotFound(QuotationError):
    """No such quote inside this agency: a version cannot be opened over nothing."""


class QuotationPolicyNotFound(QuotationError):
    """The quote points at a policy version this agency cannot read."""


class InvalidQuotation(QuotationError):
    """The requested change is not one this platform may store."""


class QuotationAlreadySent(QuotationError):
    """This version was already sent: change the offer into a new version instead."""


class QuotationNotSent(QuotationError):
    """Only a version that was sent can be accepted or superseded."""


class QuotationAlreadyAccepted(QuotationError):
    """This version already carries an acceptance; the guest's yes is recorded once."""


class QuotationTransitionRefused(QuotationError):
    """The requested state change is not an edge of the quotation chain (§11)."""


@dataclass(frozen=True)
class Quotation:
    """One immutable version of the offer for one stay."""

    quotation_version_id: str
    property_id: str
    unit_id: str
    check_in: str
    check_out: str
    version: int
    quote_id: str
    status: str
    supersedes_id: Optional[str]
    created_by: str
    created_at: str
    sent_at: Optional[str]


@dataclass(frozen=True)
class Snapshot:
    """One frozen view of a version: its price, its terms, or its governing policy."""

    snapshot_id: str
    quotation_version_id: str
    kind: str
    source_ref: Optional[str]
    payload: str
    fingerprint: str
    created_at: str


@dataclass(frozen=True)
class Acceptance:
    """§11's record of the guest's yes: the version, its three snapshots, and when."""

    acceptance_id: str
    quotation_version_id: str
    price_snapshot_id: str
    terms_snapshot_id: str
    policy_snapshot_id: str
    accepted_at: str
    recorded_by: str
    created_at: str


class QuotationService:
    """Owns the quotation version chain for one agency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        ids: Any = None,
        agency_id: str = DEFAULT_AGENCY_ID,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)

    # -- writes ------------------------------------------------------------------ #

    def draft(self, quote_id: Any, *, actor: Any, actor_role: Any) -> Quotation:
        """Open the next version of the quotation for this stay.

        The lineage is the stay — unit plus window — which is the identity the
        ``quotes`` table already uses, so re-quoting the same dates is a new version
        of the same quotation rather than a second, unrelated offer. The previous
        version is not touched here: it is superseded when the new one is *sent*,
        because until then it is still the offer the guest holds.

        Drafting is not a §56 capability on its own: nothing leaves the agency when a
        version starts in ``DRAFT``, and the catalogue names the act that does —
        :data:`SEND_TOOL`, which :meth:`send` gates. A caller that wants an offer to
        exist must hold that capability, not this method.
        """
        who = require_ref(actor, "actor", max_len=64)
        role = require_ref(actor_role, "actor_role", max_len=64)
        quote = self._require_quote(quote_id)
        lineage = (quote["agency_id"], quote["unit_id"], quote["check_in"], quote["check_out"])
        version = self._next_version(*lineage)
        previous = self._latest_sent(*lineage)
        quotation_version_id = self.ids.new("quotation_version")
        now = to_iso(self.clock.now())

        self.conn.execute("SAVEPOINT draft_quotation")
        try:
            self.conn.execute(
                "INSERT INTO quotation_versions (quotation_version_id, agency_id, property_id,"
                " unit_id, check_in, check_out, version, quote_id, status, supersedes_id,"
                " created_by, created_at, sent_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, NULL)",
                (
                    quotation_version_id,
                    quote["agency_id"],
                    quote["property_id"],
                    quote["unit_id"],
                    quote["check_in"],
                    quote["check_out"],
                    version,
                    quote["quote_id"],
                    None if previous is None else previous["quotation_version_id"],
                    who,
                    now,
                ),
            )
            self._audit(
                "quotation.drafted",
                quotation_version_id,
                actor=who,
                actor_role=role,
                after={"status": "DRAFT", "version": version, "quote_id": quote["quote_id"]},
                policy_version_id=quote["policy_version_id"],
                conn=self.conn,
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT draft_quotation")
            self.conn.execute("RELEASE SAVEPOINT draft_quotation")
            raise
        self.conn.execute("RELEASE SAVEPOINT draft_quotation")
        return self.version(quotation_version_id)

    def send(
        self,
        quotation_version_id: Any,
        *,
        actor: Any,
        actor_role: Any,
        principal_id: Optional[str] = None,
    ) -> Quotation:
        """Send this version to the guest: freeze its record, then mark it sent.

        §56: a machine actor must clear ``quotation.send`` through the AI service,
        which is deny-first until the owner publishes that tool on the allowance. A
        human actor is admitted directly. The gate runs before anything is written,
        so a refusal leaves no snapshot and no state change.
        """
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        who = require_ref(actor, "actor", max_len=64)
        role = require_ref(actor_role, "actor_role", max_len=64)
        require_tool_authority(
            self.conn,
            who,
            role,
            principal_id,
            tool=SEND_TOOL,
            clock=self.clock,
            ids=self.ids,
            agency_id=self.agency_id,
        )
        current = self.version(target)
        if current.status != "DRAFT":
            if current.status in ("SENT", "ACCEPTED", "SUPERSEDED"):
                raise QuotationAlreadySent(
                    "this version was already sent: a changed offer is a new version",
                    code="QUOTATION_ALREADY_SENT",
                    quotation_version_id=target,
                    status=current.status,
                )
            raise QuotationTransitionRefused(
                f"{current.status} -> SENT is not an edge of the quotation chain",
                code="QUOTATION_TRANSITION_REFUSED",
                quotation_version_id=target,
                from_state=current.status,
                to_state="SENT",
            )
        quote = self._require_quote(current.quote_id)
        payloads = self._snapshot_payloads(quote)
        now = to_iso(self.clock.now())

        self.conn.execute("SAVEPOINT send_quotation")
        try:
            for kind in SNAPSHOT_KINDS:
                source_ref, payload = payloads[kind]
                self.conn.execute(
                    "INSERT INTO quotation_snapshots (snapshot_id, agency_id, quotation_version_id,"
                    " kind, source_ref, payload, fingerprint, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.ids.new("quotation_snapshot"),
                        self.agency_id,
                        target,
                        kind,
                        source_ref,
                        canonical_json(payload),
                        sha256_hex(canonical_json(payload)),
                        now,
                    ),
                )
            self.conn.execute(
                "UPDATE quotation_versions SET status = 'SENT', sent_at = ?"
                " WHERE quotation_version_id = ?",
                (now, target),
            )
            replaced = self._supersede_previous(current.supersedes_id, conn=self.conn)
            self._audit(
                "quotation.sent",
                target,
                actor=who,
                actor_role=role,
                before={"status": "DRAFT"},
                after={"status": "SENT", "superseded": replaced},
                policy_version_id=quote["policy_version_id"],
                tool=SEND_TOOL,
                conn=self.conn,
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT send_quotation")
            self.conn.execute("RELEASE SAVEPOINT send_quotation")
            raise
        self.conn.execute("RELEASE SAVEPOINT send_quotation")
        return self.version(target)

    def accept(
        self,
        quotation_version_id: Any,
        *,
        actor: Any,
        actor_role: Any,
        accepted_at: Any = None,
    ) -> Acceptance:
        """Record the guest's yes against the snapshots they were shown (§11).

        The acceptance is not a §56 tool: the catalogue names no "accept" capability,
        and the act being recorded is the guest's decision, not the platform's. What
        the platform guarantees is that the record names the version *and* the three
        snapshots that existed before the decision — never a snapshot assembled
        afterwards.
        """
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        who = require_ref(actor, "actor", max_len=64)
        role = require_ref(actor_role, "actor_role", max_len=64)
        current = self.version(target)
        # The acceptance is the more specific fact, so it is reported first: a second
        # accept of an accepted version is not "the version was never sent".
        if self.acceptance(target) is not None:
            raise QuotationAlreadyAccepted(
                "this version already carries an acceptance",
                code="QUOTATION_ALREADY_ACCEPTED",
                quotation_version_id=target,
            )
        if current.status != "SENT":
            raise QuotationNotSent(
                "only a version that was sent can be accepted",
                code="QUOTATION_NOT_SENT",
                quotation_version_id=target,
                status=current.status,
            )
        by_kind = {snapshot.kind: snapshot for snapshot in self.snapshots(target)}
        missing = [kind for kind in SNAPSHOT_KINDS if kind not in by_kind]
        if missing:
            raise InvalidQuotation(
                "a sent version must carry all three snapshots before it can be accepted",
                code="QUOTATION_SNAPSHOTS_INCOMPLETE",
                quotation_version_id=target,
                missing=tuple(missing),
            )
        moment = to_iso(self.clock.now() if accepted_at is None else accepted_at)
        acceptance_id = self.ids.new("quotation_acceptance")
        now = to_iso(self.clock.now())

        self.conn.execute("SAVEPOINT accept_quotation")
        try:
            self.conn.execute(
                "INSERT INTO quotation_acceptances (acceptance_id, agency_id, quotation_version_id,"
                " price_snapshot_id, terms_snapshot_id, policy_snapshot_id, accepted_at, recorded_by,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    acceptance_id,
                    self.agency_id,
                    target,
                    by_kind["PRICE"].snapshot_id,
                    by_kind["TERMS"].snapshot_id,
                    by_kind["POLICY"].snapshot_id,
                    moment,
                    who,
                    now,
                ),
            )
            self._move(target, "ACCEPTED", conn=self.conn)
            self._audit(
                "quotation.accepted",
                target,
                actor=who,
                actor_role=role,
                before={"status": "SENT"},
                after={"status": "ACCEPTED", "accepted_at": moment, "acceptance_id": acceptance_id},
                evidence_ref=acceptance_id,
                conn=self.conn,
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT accept_quotation")
            self.conn.execute("RELEASE SAVEPOINT accept_quotation")
            raise
        self.conn.execute("RELEASE SAVEPOINT accept_quotation")
        recorded = self.acceptance(target)
        assert recorded is not None  # written in the transaction above
        return recorded

    def void(
        self,
        quotation_version_id: Any,
        *,
        actor: Any,
        actor_role: Any,
        reason: Any,
    ) -> Quotation:
        """Withdraw a version that will not be accepted.

        The state exists in the vocabulary, so it needs a writer: a state nothing can
        reach is a claim the platform cannot deliver. A void version is history, not a
        deletion — the row, its snapshots and its audit trail stay readable.
        """
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        who = require_ref(actor, "actor", max_len=64)
        role = require_ref(actor_role, "actor_role", max_len=64)
        why = require_text(reason, "reason", max_len=200)
        current = self.version(target)
        self.conn.execute("SAVEPOINT void_quotation")
        try:
            self._move(target, "VOID", conn=self.conn)
            self._audit(
                "quotation.voided",
                target,
                actor=who,
                actor_role=role,
                before={"status": current.status},
                after={"status": "VOID"},
                reason=why,
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT void_quotation")
            self.conn.execute("RELEASE SAVEPOINT void_quotation")
            raise
        self.conn.execute("RELEASE SAVEPOINT void_quotation")
        return self.version(target)

    # -- reads ------------------------------------------------------------------- #

    def version(self, quotation_version_id: Any) -> Quotation:
        """One version inside this agency, or a refusal."""
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        row = self.conn.execute(
            "SELECT * FROM quotation_versions WHERE quotation_version_id = ? AND agency_id = ?",
            (target, self.agency_id),
        ).fetchone()
        if row is None:
            raise QuotationNotFound(
                "no such quotation version in this agency",
                code="QUOTATION_NOT_FOUND",
                quotation_version_id=target,
            )
        return self._hydrate(row)

    def versions(self, *, unit_id: Any, check_in: Any, check_out: Any) -> tuple[Quotation, ...]:
        """The whole chain for one stay, oldest first — the V1/V2/V3 §11 describes."""
        unit = require_ref(unit_id, "unit_id", max_len=64)
        start = require_text(check_in, "check_in", max_len=32)
        end = require_text(check_out, "check_out", max_len=32)
        rows = self.conn.execute(
            "SELECT * FROM quotation_versions WHERE agency_id = ? AND unit_id = ?"
            " AND check_in = ? AND check_out = ? ORDER BY version",
            (self.agency_id, unit, start, end),
        ).fetchall()
        return tuple(self._hydrate(row) for row in rows)

    def current(self, *, unit_id: Any, check_in: Any, check_out: Any) -> Optional[Quotation]:
        """The newest version that is neither void nor superseded — the live offer."""
        chain = self.versions(unit_id=unit_id, check_in=check_in, check_out=check_out)
        live = [version for version in chain if version.status not in ("VOID", "SUPERSEDED")]
        return live[-1] if live else None

    def snapshots(self, quotation_version_id: Any) -> tuple[Snapshot, ...]:
        """Every snapshot of one version, in the §11 order: price, terms, policy."""
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        self.version(target)  # inside this agency, or it does not exist
        rows = self.conn.execute(
            "SELECT * FROM quotation_snapshots WHERE quotation_version_id = ?",
            (target,),
        ).fetchall()
        by_kind = {row["kind"]: self._hydrate_snapshot(row) for row in rows}
        return tuple(by_kind[kind] for kind in SNAPSHOT_KINDS if kind in by_kind)

    def snapshot(self, quotation_version_id: Any, kind: Any) -> Optional[Snapshot]:
        """One snapshot of one version, or ``None`` when that kind is not recorded."""
        wanted = require_text(kind, "kind", max_len=16)
        if wanted not in SNAPSHOT_KINDS:
            raise InvalidQuotation(
                f"unknown snapshot kind: {wanted!r}",
                code="QUOTATION_INVALID_SNAPSHOT_KIND",
                kind=wanted,
                known=SNAPSHOT_KINDS,
            )
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        self.version(target)
        row = self.conn.execute(
            "SELECT * FROM quotation_snapshots WHERE quotation_version_id = ? AND kind = ?",
            (target, wanted),
        ).fetchone()
        return None if row is None else self._hydrate_snapshot(row)

    def acceptance(self, quotation_version_id: Any) -> Optional[Acceptance]:
        """The recorded acceptance of one version, or ``None`` while there is none."""
        target = require_ref(quotation_version_id, "quotation_version_id", max_len=64)
        row = self.conn.execute(
            "SELECT * FROM quotation_acceptances WHERE quotation_version_id = ? AND agency_id = ?",
            (target, self.agency_id),
        ).fetchone()
        return None if row is None else self._hydrate_acceptance(row)

    # -- internals --------------------------------------------------------------- #

    def _move(self, quotation_version_id: str, to_state: str, *, conn: sqlite3.Connection) -> None:
        """The single writer of a version's state (§11 chain, one edge at a time)."""
        row = conn.execute(
            "SELECT status FROM quotation_versions WHERE quotation_version_id = ?"
            " AND agency_id = ?",
            (quotation_version_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise QuotationNotFound(
                "no such quotation version in this agency",
                code="QUOTATION_NOT_FOUND",
                quotation_version_id=quotation_version_id,
            )
        from_state = row["status"]
        if not can_transition(from_state, to_state):
            raise QuotationTransitionRefused(
                f"{from_state} -> {to_state} is not an edge of the quotation chain",
                code="QUOTATION_TRANSITION_REFUSED",
                quotation_version_id=quotation_version_id,
                from_state=from_state,
                to_state=to_state,
            )
        conn.execute(
            "UPDATE quotation_versions SET status = ? WHERE quotation_version_id = ?",
            (to_state, quotation_version_id),
        )

    def _supersede_previous(self, supersedes_id: Optional[str], *, conn: sqlite3.Connection) -> Optional[str]:
        """Mark the version this one replaces, when that version is still live.

        A version that was accepted or voided is not superseded: it is history, and
        §11 forbids overwriting it. The chain records what happened, not what the
        newest version would prefer.
        """
        if supersedes_id is None:
            return None
        row = conn.execute(
            "SELECT status FROM quotation_versions WHERE quotation_version_id = ?"
            " AND agency_id = ?",
            (supersedes_id, self.agency_id),
        ).fetchone()
        if row is None or row["status"] != "SENT":
            return None
        self._move(supersedes_id, "SUPERSEDED", conn=conn)
        return supersedes_id

    def _snapshot_payloads(
        self, quote: sqlite3.Row
    ) -> dict[str, tuple[Optional[str], Mapping[str, Any]]]:
        """The three records §11 asks for, each from the source that owns it.

        PRICE is the quote as stored — its amounts, lines and fingerprint — because
        that is what was calculated. TERMS is the priced policy's binding conditions
        (deposit, cancellation ladder, minimum stay, validity) because that is what
        the guest accepts. POLICY is the identity and provenance of the policy
        version itself, so "which policy said this was allowed" survives the
        publication of the next one.
        """
        policy = self._require_policy(quote["policy_version_id"])
        rules = _as_mapping(policy["payload"])
        price = {
            "quote_id": quote["quote_id"],
            "currency": quote["currency"],
            "nights": quote["nights"],
            "adults": quote["adults"],
            "children": quote["children"],
            "infants": quote["infants"],
            "accommodation_minor": quote["accommodation_minor"],
            "extra_guest_minor": quote["extra_guest_minor"],
            "cleaning_minor": quote["cleaning_minor"],
            "extras_minor": quote["extras_minor"],
            "discount_minor": quote["discount_minor"],
            "subtotal_minor": quote["subtotal_minor"],
            "service_fee_minor": quote["service_fee_minor"],
            "tax_minor": quote["tax_minor"],
            "total_minor": quote["total_minor"],
            "security_deposit_minor": quote["security_deposit_minor"],
            "rate_card_id": quote["rate_card_id"],
            "lines": _as_list(quote["lines"]),
            "warnings": _as_list(quote["warnings"]),
            "quote_fingerprint": quote["fingerprint"],
            "expires_at": quote["expires_at"],
        }
        terms = {
            "currency": policy["currency"],
            "deposit_pct": policy["deposit_pct"],
            "service_fee_pct": policy["service_fee_pct"],
            "tax_pbjt_pct": policy["tax_pbjt_pct"],
            "max_discount_pct": policy["max_discount_pct"],
            "quote_valid_hours": policy["quote_valid_hours"],
            "min_stay_nights": rules.get("min_stay_nights"),
            "cancellation_ladder": rules.get("cancellation_ladder"),
            "security_deposit_minor": rules.get("security_deposit_minor"),
            "expires_at": quote["expires_at"],
        }
        governing = {
            "policy_version_id": policy["policy_version_id"],
            "version": policy["version"],
            "property_id": policy["property_id"],
            "unit_scope": policy["unit_scope"],
            "effective_from": policy["effective_from"],
            "effective_to": policy["effective_to"],
            "fingerprint": policy["fingerprint"],
            "source_doc_ref": policy["source_doc_ref"],
            "approved_by": policy["approved_by"],
            "quote_fingerprint": quote["fingerprint"],
        }
        return {
            "PRICE": (quote["quote_id"], price),
            "TERMS": (policy["policy_version_id"], terms),
            "POLICY": (policy["policy_version_id"], governing),
        }

    def _require_quote(self, quote_id: Any) -> sqlite3.Row:
        target = require_ref(quote_id, "quote_id", max_len=64)
        row = self.conn.execute(
            "SELECT * FROM quotes WHERE quote_id = ? AND agency_id = ?",
            (target, self.agency_id),
        ).fetchone()
        if row is None:
            raise QuotationQuoteNotFound(
                "no such quote in this agency",
                code="QUOTATION_QUOTE_NOT_FOUND",
                quote_id=target,
            )
        return row

    def _require_policy(self, policy_version_id: Any) -> sqlite3.Row:
        target = require_ref(policy_version_id, "policy_version_id", max_len=64)
        row = self.conn.execute(
            "SELECT * FROM policy_versions WHERE policy_version_id = ? AND agency_id = ?",
            (target, self.agency_id),
        ).fetchone()
        if row is None:
            raise QuotationPolicyNotFound(
                "the quote names a policy version this agency cannot read",
                code="QUOTATION_POLICY_NOT_FOUND",
                policy_version_id=target,
            )
        return row

    def _next_version(self, agency_id: str, unit_id: str, check_in: str, check_out: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS newest FROM quotation_versions WHERE agency_id = ?"
            " AND unit_id = ? AND check_in = ? AND check_out = ?",
            (agency_id, unit_id, check_in, check_out),
        ).fetchone()
        newest = row["newest"]
        return 1 if newest is None else int(newest) + 1

    def _latest_sent(self, agency_id: str, unit_id: str, check_in: str, check_out: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM quotation_versions WHERE agency_id = ? AND unit_id = ?"
            " AND check_in = ? AND check_out = ? AND status = 'SENT' ORDER BY version DESC LIMIT 1",
            (agency_id, unit_id, check_in, check_out),
        ).fetchone()

    def _audit(
        self,
        action: str,
        entity_id: str,
        *,
        actor: str,
        actor_role: str,
        before: Optional[Mapping[str, Any]] = None,
        after: Optional[Mapping[str, Any]] = None,
        reason: Optional[str] = None,
        policy_version_id: Optional[str] = None,
        evidence_ref: Optional[str] = None,
        tool: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """Append the audit row inside the caller's transaction, never beside it."""
        event = append_event(
            conn if conn is not None else self.conn,
            clock=self.clock,
            ids=self.ids,
            action=action,
            entity_type="quotation_version",
            entity_id=entity_id,
            actor=actor,
            actor_role=actor_role,
            before=before,
            after=after,
            reason=reason,
            policy_version_id=policy_version_id,
            evidence_ref=evidence_ref,
            tool=tool,
        )
        return event.audit_event_id

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> Quotation:
        return Quotation(
            quotation_version_id=row["quotation_version_id"],
            property_id=row["property_id"],
            unit_id=row["unit_id"],
            check_in=row["check_in"],
            check_out=row["check_out"],
            version=int(row["version"]),
            quote_id=row["quote_id"],
            status=row["status"],
            supersedes_id=row["supersedes_id"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
        )

    @staticmethod
    def _hydrate_snapshot(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            snapshot_id=row["snapshot_id"],
            quotation_version_id=row["quotation_version_id"],
            kind=row["kind"],
            source_ref=row["source_ref"],
            payload=row["payload"],
            fingerprint=row["fingerprint"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _hydrate_acceptance(row: sqlite3.Row) -> Acceptance:
        return Acceptance(
            acceptance_id=row["acceptance_id"],
            quotation_version_id=row["quotation_version_id"],
            price_snapshot_id=row["price_snapshot_id"],
            terms_snapshot_id=row["terms_snapshot_id"],
            policy_snapshot_id=row["policy_snapshot_id"],
            accepted_at=row["accepted_at"],
            recorded_by=row["recorded_by"],
            created_at=row["created_at"],
        )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Read a stored JSON object, or ``{}`` when it is not one.

    A snapshot must never fail to build because a payload column holds something
    unexpected: the snapshot is the record, and a missing key inside it is visible,
    while an exception here would leave a sent version without its terms.
    """
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def _as_list(value: Any) -> Sequence[Any]:
    """Read a stored JSON array, or ``()`` when it is not one (see ``_as_mapping``)."""
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return ()
    return loaded if isinstance(loaded, list) else ()

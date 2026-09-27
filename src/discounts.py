"""The discount engine — §11 Pricing, Quote & Discount; §16 policy; §15 approval; §21 deny.

§11 asks a discount to be five things at once: a **rule evaluation**, an **authority** decision,
a **margin guard**, an **approval** (when the guard is breached), and an **audit** entry. It also
requires supplier-funded and agency-funded discounts to stay apart, and the margin floor to carry
**both** a percentage and an absolute guard whose override needs human authority.

How this module keeps those promises:

* **The two funding kinds are separated by whose money moves.** A ``SUPPLIER_FUNDED`` discount is
  paid by the supplier, so it leaves the agency's margin exactly where it was and there is nothing
  for a margin floor to guard. An ``AGENCY_FUNDED`` discount comes out of the agency's own margin
  and is therefore the only kind the floor governs.
* **Authority and floors are read from published policy, never from a literal here** (§16:
  "Thresholds adalah configuration, bukan hard-coded"). ``commercial.discount.authority_roles``
  names the roles that may grant a discount; ``commercial.margin_floor_pct`` and
  ``commercial.margin_floor_minor`` are the two halves of the floor. An unpublished value raises
  :class:`DiscountPolicyNotPublished` — the engine refuses, it never substitutes a default.
* **The absolute floor carries a currency, and the booking decides which.** One number cannot say
  what it is worth: ``commercial.margin_floor_minor`` is denominated in
  ``commercial.margin_floor_currency``, and a booking in any other currency has the floor
  converted through the published rates (:mod:`fx`) *before* the comparison. Without that step a
  rupiah booking would be measured against a dollar number and clear a guard it never met. No
  rate published for the pair refuses (:class:`fx.FxRateNotPublished`); so does a floor whose
  currency was never declared.
* **The margin basis is an input, not a guess.** The platform has no supplier-cost basis yet
  (``rate_versions.nightly_minor`` is the *selling* rate; ``contract_versions`` carries no amount),
  so a caller must hand over the revenue and the margin it is asking the engine to protect.
  :class:`MarginBasisRequired` is raised rather than inventing a number — the seam where §12
  contracts and the finance wave will supply it.
* **A breach is not applied and not self-approved.** The engine returns ``REQUIRES_OVERRIDE``
  with nothing moved, and can *raise* the decision for a human (:meth:`DiscountEngine.request_override`).
  It exposes no method that answers for one: §21 denies ``discount.approve`` and
  ``margin_override.approve`` to AI, so the capability is not built into this engine at all.
* **Every outcome is audited** — applied, refused, and breached alike.

This module writes nothing outside the caller's transaction and never places an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

import approvals as AP
import audit as AUDIT
import fx as FX
import policy_engine as PE
from primitives import (
    DomainError,
    Money,
    SystemClock,
    fingerprint,
    require_ref,
    require_text,
)

# --------------------------------------------------------------------------- #
# Vocabulary (§11)
# --------------------------------------------------------------------------- #

#: Who pays for the discount. The distinction is the whole point of §11's separation clause.
DISCOUNT_KINDS: tuple[str, ...] = ("SUPPLIER_FUNDED", "AGENCY_FUNDED")
SUPPLIER_FUNDED, AGENCY_FUNDED = DISCOUNT_KINDS

#: §16 policy keys. Read from the store; no value in this module may stand in for them.
AUTHORITY_KEY = "commercial.discount.authority_roles"
FLOOR_PCT_KEY = "commercial.margin_floor_pct"
FLOOR_MINOR_KEY = "commercial.margin_floor_minor"
#: The currency ``FLOOR_MINOR_KEY`` is denominated in — without it the number means nothing.
FLOOR_CURRENCY_KEY = "commercial.margin_floor_currency"

#: §21 / §56 capability names. The engine never performs either — a human does.
OVERRIDE_ACTION = "margin_override.approve"
OUT_OF_POLICY_ACTION = "discount.approve_out_of_policy"

OUTCOME_APPLIED = "APPLIED"
OUTCOME_REFUSED = "REFUSED"
OUTCOME_REQUIRES_OVERRIDE = "REQUIRES_OVERRIDE"

CODE_APPLIED = "DISCOUNT_APPLIED"
CODE_AUTHORITY_REFUSED = "DISCOUNT_AUTHORITY_REFUSED"
CODE_MARGIN_FLOOR_BREACHED = "MARGIN_FLOOR_BREACHED"

ACTION_APPLIED = "DISCOUNT_APPLIED"
ACTION_REFUSED = "DISCOUNT_REFUSED"
ACTION_BREACHED = "DISCOUNT_MARGIN_FLOOR_BREACHED"
ACTION_OVERRIDE_REQUESTED = "DISCOUNT_OVERRIDE_REQUESTED"

ENTITY_TYPE = "discount"
OVERRIDE_SUBJECT_TYPE = "discount"
OVERRIDE_RISK = "HIGH"
PERCENT_DENOMINATOR = Decimal(100)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class DiscountError(DomainError):
    """Base for every discount refusal."""

    code = "DISCOUNT_ERROR"


class DiscountKindInvalid(DiscountError):
    """The funding party is not one of the two §11 kinds."""

    code = "DISCOUNT_KIND_INVALID"


class DiscountPolicyNotPublished(DiscountError):
    """A §16 value the engine needs has never been published — refuse, do not default."""

    code = "DISCOUNT_POLICY_NOT_PUBLISHED"


class MarginBasisRequired(DiscountError):
    """The caller must say what it is protecting: no supplier cost basis exists yet."""

    code = "MARGIN_BASIS_REQUIRED"


class NothingToOverride(DiscountError):
    """Only a decision that breached the floor can be escalated to a human."""

    code = "DISCOUNT_NOTHING_TO_OVERRIDE"


def override_role() -> str:
    """The top of the platform's own role vocabulary (§18) — never a literal in this module.

    §11 asks an override to carry human authority without naming who. Reading the rank off the
    approval engine keeps one vocabulary for roles instead of a second, drifting copy.
    """
    return max(AP.ROLE_RANK, key=lambda role: AP.ROLE_RANK[role])


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarginFloor:
    """§11's two guards together: a share of the booking and an absolute amount in one currency.

    ``minor`` is always expressed in :attr:`currency` — the currency of the booking it will
    guard. When the published floor is denominated in something else, :attr:`base_minor` and
    :attr:`base_currency` keep the number the owner actually published, so an override decision
    shows both what was declared and what it became.
    """

    pct: Decimal
    minor: int
    currency: str
    base_minor: Optional[int] = None
    base_currency: Optional[str] = None

    @property
    def converted(self) -> bool:
        """True when this floor was converted out of the currency it was declared in."""
        return self.base_currency is not None

    def holds(self, *, margin_after_minor: int, revenue_minor: int) -> bool:
        """True when the margin left after the discount clears both guards."""
        if margin_after_minor < self.minor:
            return False
        rate = (Decimal(margin_after_minor) / Decimal(revenue_minor)) * PERCENT_DENOMINATOR
        return rate >= self.pct

    def to_dict(self) -> dict:
        """JSON-safe view: the guards, and the published number behind them when converted."""
        return {
            "pct": str(self.pct),
            "minor": self.minor,
            "currency": self.currency,
            "base_minor": self.base_minor,
            "base_currency": self.base_currency,
            "converted": self.converted,
        }


@dataclass(frozen=True)
class DiscountRequest:
    """What a caller asks for. The margin basis is stated, never inferred."""

    kind: str
    amount_minor: int
    currency: str
    reason: str
    actor: str
    actor_role: str
    revenue_minor: Optional[int] = None
    margin_before_minor: Optional[int] = None
    subject_ref: str = "quote_unknown"


@dataclass(frozen=True)
class DiscountDecision:
    """The engine's answer, complete enough to audit and to hand to a human."""

    discount_id: str
    outcome: str
    code: str
    kind: str
    amount_minor: int
    applied_minor: int
    currency: str
    subject_ref: str
    reason: str
    authority_roles: tuple[str, ...]
    revenue_minor: Optional[int] = None
    margin_before_minor: Optional[int] = None
    margin_after_minor: Optional[int] = None
    margin_if_applied_minor: Optional[int] = None
    floor_pct: Optional[Decimal] = None
    floor_minor: Optional[int] = None
    #: The currency :attr:`floor_minor` is expressed in — the booking's own currency.
    floor_currency: Optional[str] = None
    #: The number the owner published, when the floor had to be converted to reach the booking.
    floor_base_minor: Optional[int] = None
    floor_base_currency: Optional[str] = None

    @property
    def is_applied(self) -> bool:
        return self.outcome == OUTCOME_APPLIED

    @property
    def needs_override(self) -> bool:
        return self.outcome == OUTCOME_REQUIRES_OVERRIDE

    def to_dict(self) -> dict:
        """JSON-safe view — used for the audit payload and the decision fingerprint."""
        return {
            "discount_id": self.discount_id,
            "outcome": self.outcome,
            "code": self.code,
            "kind": self.kind,
            "amount_minor": self.amount_minor,
            "applied_minor": self.applied_minor,
            "currency": self.currency,
            "subject_ref": self.subject_ref,
            "revenue_minor": self.revenue_minor,
            "margin_before_minor": self.margin_before_minor,
            "margin_after_minor": self.margin_after_minor,
            "margin_if_applied_minor": self.margin_if_applied_minor,
            "floor_pct": None if self.floor_pct is None else str(self.floor_pct),
            "floor_minor": self.floor_minor,
            "floor_currency": self.floor_currency,
            "floor_base_minor": self.floor_base_minor,
            "floor_base_currency": self.floor_base_currency,
            "authority_roles": list(self.authority_roles),
        }

    def fingerprint(self) -> str:
        """The decision's content hash: two identical requests hash the same.

        ``discount_id`` is identity, not content: it advances between calls by design, so hashing
        it would make a replayed request look like a different decision.
        """
        payload = {key: value for key, value in self.to_dict().items() if key != "discount_id"}
        return fingerprint(payload)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class DiscountEngine:
    """Evaluates a discount request against published policy and the margin floor.

    Reads only. The engine appends audit rows inside the caller's transaction and, on request,
    raises an approval for a human — it never approves anything itself (§21).
    """

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any = None,
        ids: Any = None,
        agency_id: str = "agency_default",
        policy: Optional[PE.PolicyResolver] = None,
        approvals: Optional[AP.ApprovalService] = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or SystemClock()
        self._ids = ids
        self._agency_id = agency_id
        self._policy = policy or PE.PolicyResolver(conn, clock=self._clock, agency_id=agency_id)
        self._approvals = approvals or AP.ApprovalService(
            conn, clock=self._clock, ids=self._ids, agency_id=agency_id
        )

    # -- policy reads ------------------------------------------------------- #
    def authority_roles(self, *, at: Any = None) -> tuple[str, ...]:
        """Who may grant a discount, per ``commercial.discount.authority_roles``."""
        return self._resolve(AUTHORITY_KEY, at=at).as_roles()

    def margin_floor(self, *, at: Any = None, currency: Optional[str] = None) -> MarginFloor:
        """§11's floor, expressed in the currency of the booking it will guard.

        ``commercial.margin_floor_minor`` is one number and one number carries no currency: it is
        denominated in ``commercial.margin_floor_currency``. A booking in that same currency uses
        the published number unchanged; a booking in any other currency has the floor converted
        through ``commercial.fx.rates`` first, so the two sides of the comparison are always the
        same currency. A floor whose currency was never declared refuses, and so does a pair with
        no published rate — the engine will not read rupiah as dollars to make a comparison pass.
        """
        pct = self._resolve(FLOOR_PCT_KEY, at=at).as_decimal()
        minor = self._resolve(FLOOR_MINOR_KEY, at=at).as_int()
        declared = Money.zero(self._resolve(FLOOR_CURRENCY_KEY, at=at).as_text()).currency
        if currency is None:
            return MarginFloor(pct=pct, minor=minor, currency=declared)
        booking_currency = Money.zero(currency).currency
        if booking_currency == declared:
            return MarginFloor(pct=pct, minor=minor, currency=declared)
        table = FX.FxTable.from_policy(self._resolve(FX.FX_RATES_KEY, at=at).as_json())
        return MarginFloor(
            pct=pct,
            minor=table.convert_minor(minor, from_currency=declared, to_currency=booking_currency),
            currency=booking_currency,
            base_minor=minor,
            base_currency=declared,
        )

    def _resolve(self, key: str, *, at: Any = None) -> PE.PolicyValue:
        try:
            return self._policy.resolve(key, at=at)
        except PE.PolicyValueNotPublished as missing:
            raise DiscountPolicyNotPublished(
                f"{key} has never been published — the discount engine refuses to assume a value",
                code="DISCOUNT_POLICY_NOT_PUBLISHED",
                policy_key=key,
            ) from missing

    # -- evaluation --------------------------------------------------------- #
    def evaluate(self, request: DiscountRequest, *, at: Any = None) -> DiscountDecision:
        """Rule evaluation → authority → margin guard → audit. Never applies an overridden discount."""
        kind = self._kind_of(request)
        amount = self._amount_of(request)
        # Normalised and validated through the money primitive: an unknown ISO-4217 code has to
        # refuse here, before a floor is ever compared in it.
        currency = Money.zero(require_ref(request.currency, "currency")).currency
        subject_ref = require_ref(request.subject_ref, "subject_ref")
        reason = require_text(request.reason, "reason")
        actor = require_ref(request.actor, "actor")
        actor_role = require_ref(request.actor_role, "actor_role")

        authority = self.authority_roles(at=at)

        if actor_role not in authority:
            return self._record(
                outcome=OUTCOME_REFUSED,
                code=CODE_AUTHORITY_REFUSED,
                action=ACTION_REFUSED,
                kind=kind,
                amount_minor=amount,
                applied_minor=0,
                currency=currency,
                subject_ref=subject_ref,
                reason=reason,
                actor=actor,
                actor_role=actor_role,
                authority=authority,
                at=at,
            )

        if kind == SUPPLIER_FUNDED:
            # The supplier pays: the agency's margin is untouched, so no floor applies and no
            # basis is demanded. This asymmetry *is* §11's separation clause.
            return self._record(
                outcome=OUTCOME_APPLIED,
                code=CODE_APPLIED,
                action=ACTION_APPLIED,
                kind=kind,
                amount_minor=amount,
                applied_minor=amount,
                currency=currency,
                subject_ref=subject_ref,
                reason=reason,
                actor=actor,
                actor_role=actor_role,
                authority=authority,
                margin_before=request.margin_before_minor,
                margin_after=request.margin_before_minor,
                at=at,
            )

        revenue, margin_before = self._basis_of(request)
        floor = self.margin_floor(at=at, currency=currency)
        margin_after = margin_before - amount

        if floor.holds(margin_after_minor=margin_after, revenue_minor=revenue):
            return self._record(
                outcome=OUTCOME_APPLIED,
                code=CODE_APPLIED,
                action=ACTION_APPLIED,
                kind=kind,
                amount_minor=amount,
                applied_minor=amount,
                currency=currency,
                subject_ref=subject_ref,
                reason=reason,
                actor=actor,
                actor_role=actor_role,
                authority=authority,
                revenue=revenue,
                margin_before=margin_before,
                margin_after=margin_after,
                floor=floor,
                at=at,
            )

        # Breach: nothing moves, and nothing is escalated on the engine's own initiative (§15).
        return self._record(
            outcome=OUTCOME_REQUIRES_OVERRIDE,
            code=CODE_MARGIN_FLOOR_BREACHED,
            action=ACTION_BREACHED,
            kind=kind,
            amount_minor=amount,
            applied_minor=0,
            currency=currency,
            subject_ref=subject_ref,
            reason=reason,
            actor=actor,
            actor_role=actor_role,
            authority=authority,
            revenue=revenue,
            margin_before=margin_before,
            margin_after=margin_before,        # nothing moved — this is the state that stands
            margin_if_applied=margin_after,    # and this is what the guest asked for
            floor=floor,
            at=at,
        )

    # -- escalation --------------------------------------------------------- #
    def request_override(
        self,
        decision: DiscountDecision,
        *,
        requested_by: str,
        requested_role: str,
        reason: str,
        evidence_refs: Sequence[str] = (),
        idempotency_key: Optional[str] = None,
        at: Any = None,
    ) -> AP.Approval:
        """Raise a breached discount for a human to decide. The engine asks; it never answers."""
        if not decision.needs_override:
            raise NothingToOverride(
                "only a discount that breached the margin floor needs an override",
                code="DISCOUNT_NOTHING_TO_OVERRIDE",
                outcome=decision.outcome,
            )
        requester = require_ref(requested_by, "requested_by")
        role = require_ref(requested_role, "requested_role")
        why = require_text(reason, "reason")
        key = idempotency_key or f"discount-override:{decision.discount_id}"

        approval = self._approvals.request(
            subject_type=OVERRIDE_SUBJECT_TYPE,
            subject_id=decision.discount_id,
            action=OVERRIDE_ACTION,
            requested_by=requester,
            requested_role=role,
            reason=why,
            payload=decision.to_dict(),
            required_role=override_role(),
            required_approvals=1,
            risk_level=OVERRIDE_RISK,
            evidence_refs=tuple(evidence_refs),
            # No ``money`` on purpose: §11 asks for *human authority* over a discount, while §15's
            # dual-control list covers refunds, payouts and bank changes — not discounts. Passing an
            # amount here would silently turn a one-signature policy decision into two signatures.
            # The amount rides in the payload, where it stays auditable.
            idempotency_key=key,
            at=at,
        )
        self._append(
            action=ACTION_OVERRIDE_REQUESTED,
            entity_id=decision.discount_id,
            actor=requester,
            actor_role=role,
            reason=why,
            after={
                "approval_id": approval.approval_id,
                "action": OVERRIDE_ACTION,
                "required_role": approval.required_role,
                "risk_level": approval.risk_level,
                "outcome": decision.outcome,
                "code": decision.code,
                "amount_minor": decision.amount_minor,
                "margin_after_minor": decision.margin_after_minor,
                "floor_minor": decision.floor_minor,
                "floor_currency": decision.floor_currency,
                "floor_base_minor": decision.floor_base_minor,
                "floor_base_currency": decision.floor_base_currency,
            },
            at=at,
        )
        return approval

    # -- internals ---------------------------------------------------------- #
    def _kind_of(self, request: DiscountRequest) -> str:
        raw = request.kind
        kind = raw.strip().upper() if isinstance(raw, str) else ""
        if kind not in DISCOUNT_KINDS:
            raise DiscountKindInvalid(
                f"{raw!r} is not a §11 discount kind",
                code="DISCOUNT_KIND_INVALID",
                kind=raw,
                allowed=list(DISCOUNT_KINDS),
            )
        return kind

    def _amount_of(self, request: DiscountRequest) -> int:
        amount = request.amount_minor
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise DiscountError(
                "a discount amount must be a positive integer of minor units",
                code="DISCOUNT_AMOUNT_INVALID",
                amount_minor=amount,
            )
        return amount

    def _basis_of(self, request: DiscountRequest) -> tuple[int, int]:
        """The revenue and the margin the floor is measured against — stated, never inferred."""
        revenue = self._basis_value(request.revenue_minor, "revenue_minor", positive=True)
        margin = self._basis_value(request.margin_before_minor, "margin_before_minor", positive=False)
        return revenue, margin

    def _basis_value(self, value: Optional[int], field: str, *, positive: bool) -> int:
        if value is None:
            raise MarginBasisRequired(
                f"{field} is required: the platform has no supplier-cost basis to derive it",
                code="MARGIN_BASIS_REQUIRED",
                field=field,
            )
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DiscountError(
                f"{field} must be a non-negative integer of minor units",
                code="DISCOUNT_MARGIN_BASIS_INVALID",
                field=field,
                value=value,
            )
        if positive and value == 0:
            raise DiscountError(
                f"{field} must be positive: a margin share of nothing is undefined",
                code="DISCOUNT_MARGIN_BASIS_INVALID",
                field=field,
                value=value,
            )
        return value

    def _record(
        self,
        *,
        outcome: str,
        code: str,
        action: str,
        kind: str,
        amount_minor: int,
        applied_minor: int,
        currency: str,
        subject_ref: str,
        reason: str,
        actor: str,
        actor_role: str,
        authority: tuple[str, ...],
        revenue: Optional[int] = None,
        margin_before: Optional[int] = None,
        margin_after: Optional[int] = None,
        margin_if_applied: Optional[int] = None,
        floor: Optional[MarginFloor] = None,
        at: Any = None,
    ) -> DiscountDecision:
        """Build the decision, audit it, and hand it back. One place, so no path escapes the trail."""
        decision = DiscountDecision(
            discount_id=self._ids.new(ENTITY_TYPE),
            outcome=outcome,
            code=code,
            kind=kind,
            amount_minor=amount_minor,
            applied_minor=applied_minor,
            currency=currency,
            subject_ref=subject_ref,
            reason=reason,
            authority_roles=tuple(authority),
            revenue_minor=revenue,
            margin_before_minor=margin_before,
            margin_after_minor=margin_after,
            margin_if_applied_minor=margin_if_applied,
            floor_pct=None if floor is None else floor.pct,
            floor_minor=None if floor is None else floor.minor,
            floor_currency=None if floor is None else floor.currency,
            floor_base_minor=None if floor is None else floor.base_minor,
            floor_base_currency=None if floor is None else floor.base_currency,
        )
        self._append(
            action=action,
            entity_id=decision.discount_id,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            after=dict(decision.to_dict(), subject_ref=subject_ref),
            at=at,
        )
        return decision

    def _append(
        self,
        *,
        action: str,
        entity_id: str,
        actor: str,
        actor_role: str,
        reason: str,
        after: Mapping[str, Any],
        at: Any = None,
    ) -> None:
        with self._conn:
            AUDIT.append_event(
                self._conn,
                clock=self._clock,
                ids=self._ids,
                action=action,
                entity_type=ENTITY_TYPE,
                entity_id=entity_id,
                actor=actor,
                actor_role=actor_role,
                after=after,
                reason=reason,
            )


__all__ = [
    "AGENCY_FUNDED",
    "AUTHORITY_KEY",
    "DiscountDecision",
    "DiscountEngine",
    "DiscountError",
    "DiscountKindInvalid",
    "DiscountPolicyNotPublished",
    "DiscountRequest",
    "DISCOUNT_KINDS",
    "FLOOR_CURRENCY_KEY",
    "FLOOR_MINOR_KEY",
    "FLOOR_PCT_KEY",
    "MarginBasisRequired",
    "MarginFloor",
    "NothingToOverride",
    "OUT_OF_POLICY_ACTION",
    "OVERRIDE_ACTION",
    "SUPPLIER_FUNDED",
    "override_role",
]

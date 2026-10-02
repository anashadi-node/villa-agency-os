"""The human role vocabulary, in one place — ADR-0035 §5, ADR-0036, Master Spec §18.

Three lists used to disagree, and ADR-0035 §5 recorded it rather than smoothing it over:
``dashboard.ROLES`` named five page roles, ``approvals.ROLE_RANK`` four ladder ranks, and
``identity.MFA_REQUIRED_ROLES`` two roles. The reconciliation was deliberately left to its
own commit with the per-role matrix test §18 requires. This module is that reconciliation's
single home, and the three modules now read from it instead of carrying their own copy.

Three rules the rest of the module is built to keep:

* **Canonical ids are the ones a document names** (ADR-0035: ``finance_manager``, ``gm``,
  ``reservation_manager``, …). New writes use them.
* **Legacy names stay resolvable for good** (:data:`ROLE_ALIASES`). Approval rows are
  append-only, so ``required_role = "accountant"`` is written into history and the engine
  must still be able to decide that row. An alias is a fact about history, not a grace
  period with an end date.
* **A rank is only ever added.** No role that an already-written row names has moved up
  the ladder, and none has moved down. ``gm`` is the one new tier (ADR-0036: above the
  Finance Manager, below the Owner); the legacy four keep the exact integers they had, so
  this commit changes which *names* the code writes without changing any decision it makes.

The capability matrix below is the §18/ADR-0035 authority rules made executable, so the
per-role matrix test asserts one table instead of re-deriving a rule per test.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence

from primitives import DomainError

__all__ = [
    "ADJUDICATOR_ROLES",
    "AI_ROLE_IDS",
    "CAPABILITIES",
    "HUMAN_ROLE_IDS",
    "MONEY_OUT_APPROVER_ROLES",
    "MFA_REQUIRED_ROLES",
    "ROLE_ALIASES",
    "ROLE_CAPABILITIES",
    "ROLE_IDS",
    "ROLE_RANK",
    "UnknownRole",
    "approval_ladder",
    "canonical_role",
    "is_ai_role",
    "is_known_role",
    "may",
    "role_rank",
]


class UnknownRole(DomainError):
    """A role name that is not in the vocabulary and is not a known legacy alias."""

    code = "UNKNOWN_ROLE"


#: Every canonical role id, in the order ADR-0035 lists them. A role id is a *name*, not
#: a permission: ``finance_staff`` and ``reservation_agent`` are here even though the owner
#: deleted their human holders, because the maker function must still have a name to be
#: refused *for* (ADR-0035 amendment: a control with no maker is an open door).
ROLE_IDS: tuple[str, ...] = (
    "owner",
    "gm",
    "commercial_manager",
    "partnership_manager",
    "reservation_manager",
    "finance_manager",
    "system_admin",
    "greeting_service",
    "revenue_manager",
    "auditor",
    "legal_compliance",
    # Reserved for the AI service principal; no human holder (ADR-0035 amendment).
    "finance_staff",
    "reservation_agent",
)

#: The ids a *human* may be given. The two AI-reserved ids are excluded on purpose — the
#: platform refuses those actions rather than handing them to a fallback person.
HUMAN_ROLE_IDS: tuple[str, ...] = tuple(
    role for role in ROLE_IDS if role not in ("finance_staff", "reservation_agent")
)

#: Who outranks whom on the approval ladder. A request carries the role it needs; an
#: approver whose rank is **at least** that rank may decide it (``approvals._rank``).
#:
#: Ranks 1–3 are exactly the integers the ladder already used (``agent`` 1, ``manager`` 2,
#: ``accountant`` 3) under their canonical names, and ``owner`` stays the top. ``gm`` is
#: ADR-0036's new tier: above ``finance_manager``, below ``owner``.
ROLE_RANK: Mapping[str, int] = MappingProxyType(
    {
        "reservation_agent": 1,
        "reservation_manager": 2,
        "finance_manager": 3,
        "gm": 4,
        "owner": 5,
    }
)

#: Every role that may appear on the ladder — the list ``approvals`` validates against.
#: Named separately because the invariant worth asserting is a *negative*: no AI identity
#: may ever be a member (ADR-0036: "no AI identity can ever appear as an approver on a
#: money-out request").
ADJUDICATOR_ROLES: tuple[str, ...] = tuple(ROLE_RANK)

#: The AI service identity from ADR-0018. Kept out of the ladder, the page roles and every
#: human capability: an AI prepares, a human decides.
AI_ROLE_IDS: tuple[str, ...] = ("ai_service",)

#: Legacy names that already appear in written rows, published policy values or module
#: vocabularies. Each maps to the canonical id with the **same** rank it has today.
ROLE_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "agent": "reservation_agent",
        "manager": "reservation_manager",
        "accountant": "finance_manager",
        "finance": "finance_manager",
        "admin": "system_admin",
    }
)

#: What each role may do, in the words ADR-0035 and ADR-0036 use for the control that rides
#: on it. Absent key = the role may do nothing beyond being named; an empty frozenset is
#: the honest declaration for a role whose function moved elsewhere (the AI-reserved two).
CAPABILITIES: tuple[str, ...] = (
    "approve.money_out",
    "approve.pricing",
    "make.money_out",
    "finance.read",
    "finance.write",
    "booking.write",
    "operations.write",
    "stays.read",
    "notes.write",
    "audit.read",
    "system.write",
    "commercial.write",
    "partnership.write",
    "revenue.read",
    "legal.record",
    "report.read",
)

ROLE_CAPABILITIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # Receives the periodic reports and corrects anomalies; **not** the maker of what
        # it approves (ADR-0035).
        "owner": frozenset({"approve.money_out", "approve.pricing", "report.read", "audit.read"}),
        # ADR-0036 tier 2: the GM agrees before money leaves, and sees the reports.
        "gm": frozenset({"approve.money_out", "report.read", "audit.read"}),
        # The checker. May decide a money-out request and read finance — never prepare one.
        "finance_manager": frozenset({"approve.money_out", "finance.read", "finance.write"}),
        # Approves out-of-policy discounts; **must not** approve its own (§18).
        "reservation_manager": frozenset(
            {"approve.pricing", "booking.write", "operations.write", "stays.read"}
        ),
        # Zero business or financial authority, by ADR-0035.
        "system_admin": frozenset({"system.write"}),
        # ADR-0035 §4: reads who is staying where, records visit notes, nothing else.
        "greeting_service": frozenset({"stays.read", "notes.write"}),
        # Read-only, by master spec §18.
        "auditor": frozenset({"audit.read"}),
        "commercial_manager": frozenset({"commercial.write"}),
        "partnership_manager": frozenset({"partnership.write"}),
        "revenue_manager": frozenset({"revenue.read", "report.read"}),
        # External counsel acts through a recorded approval; the recorder is Owner/GM.
        "legal_compliance": frozenset({"legal.record"}),
        # The maker function, reserved for the AI service principal (ADR-0035 amendment).
        # No human holds these, so the platform refuses instead of falling back.
        "finance_staff": frozenset(),
        "reservation_agent": frozenset(),
    }
)

#: Roles whose approval is required before money leaves (ADR-0036's ladder, minus the AI
#: maker and the human hand that executes).
MONEY_OUT_APPROVER_ROLES: frozenset[str] = frozenset(
    {"finance_manager", "gm", "owner"}
)

#: Roles that may not open a session before the second factor is satisfied (§18, ADR-0036).
#: Canonical ids, by the owner's decision of 2026-10-02 ("ganti ke Finance Manager").
#: ``identity.is_mfa_role`` resolves the *page* word a session carries (``finance``) onto the
#: canonical id, so the published set and the session vocabulary cannot drift apart.
MFA_REQUIRED_ROLES: tuple[str, ...] = ("owner", "finance_manager", "gm")


def canonical_role(role: Any) -> Optional[str]:
    """The canonical id for ``role``, or ``None`` when nothing in the vocabulary knows it.

    Accepts a canonical id, a legacy alias, and surrounding whitespace — the same
    tolerance ``approvals._role`` had, kept in one place so both agree.
    """
    if not isinstance(role, str):
        return None
    name = role.strip()
    if not name:
        return None
    if name in ROLE_IDS:
        return name
    return ROLE_ALIASES.get(name)


def is_known_role(role: Any) -> bool:
    """True when ``role`` is a canonical id or a known legacy alias."""
    return canonical_role(role) is not None


def is_ai_role(role: Any) -> bool:
    """True for the AI service identity (ADR-0018) — never a satisfiable human role."""
    return isinstance(role, str) and role.strip() in AI_ROLE_IDS


def role_rank(role: Any) -> Optional[int]:
    """The ladder rank of ``role``, resolved through the aliases; ``None`` when it has none.

    ``None`` covers both an unknown name and a known role that simply is not on the money
    ladder (``auditor``, ``greeting_service``): neither may decide a request, and the
    caller needs to tell "unknown" from "not here" only when it writes the refusal.
    """
    canonical = canonical_role(role)
    if canonical is None:
        return None
    return ROLE_RANK.get(canonical)


def may(role: Any, capability: str) -> bool:
    """True when ``role`` holds ``capability``. An unknown role holds nothing."""
    canonical = canonical_role(role)
    if canonical is None:
        return False
    return capability in ROLE_CAPABILITIES.get(canonical, frozenset())


def approval_ladder(entries: Sequence[Any]) -> tuple[str, ...]:
    """Canonicalise and check an approval ladder, as policy numbers it.

    ``finance.approval.payment_thresholds_minor`` publishes ``ladder`` as a list of names.
    The published value and the engine's vocabulary can drift apart — the value was
    published naming ``["finance_manager", "gm"]`` before either name existed in
    ``ROLE_RANK``, which would have produced a refusal at the first real payment. This
    function is the seam where that drift becomes a loud error instead.

    Refuses: an empty ladder, a name nothing knows, a role that is not an adjudicator, the
    AI identity, a duplicate, and a ladder that does not climb (a tier list that descends
    describes an authority nobody holds).
    """
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        raise UnknownRole(
            "an approval ladder must be a sequence of role names",
            got=repr(entries),
        )
    resolved: list[str] = []
    for entry in entries:
        canonical = canonical_role(entry)
        if canonical is None:
            raise UnknownRole(
                "the approval ladder names a role the engine does not know",
                role=repr(entry),
                known=sorted(ROLE_IDS),
            )
        if canonical in AI_ROLE_IDS:
            raise UnknownRole(
                "an AI identity cannot appear on an approval ladder (ADR-0036)",
                role=canonical,
            )
        if canonical not in ROLE_RANK:
            raise UnknownRole(
                "the approval ladder names a role with no ladder rank",
                role=canonical,
                ladder=sorted(ROLE_RANK),
            )
        if canonical in resolved:
            raise UnknownRole(
                "the approval ladder names the same role twice",
                role=canonical,
            )
        resolved.append(canonical)

    if not resolved:
        raise UnknownRole("an approval ladder may not be empty", ladder=[])

    ranks = [ROLE_RANK[role] for role in resolved]
    if ranks != sorted(ranks):
        raise UnknownRole(
            "an approval ladder must climb: each tier outranks the one before it",
            ladder=list(resolved),
            ranks=ranks,
        )
    return tuple(resolved)

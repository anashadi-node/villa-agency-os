"""The canonical hold vocabulary (§7, Appendix A).

One home for the states a ``holds`` row may hold, the subsets the schema and the
kernel both need, and the transitions between them. The DDL CHECK, the partial
unique index, the PG exclusion constraint and ``observability`` each restate part
of this module; ``tests/test_wave4b_hold_lifecycle.py`` pins them together, so a
state added here without the matching schema change fails the suite.

``NOT_REQUESTED`` is deliberately absent: Appendix A names it as the absence of a
hold request — a row that does not exist — not as a state one can be in.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from primitives import DomainError, require_text

#: Every state a ``holds`` row may hold, mirroring the CHECK constraint verbatim.
HOLD_STATES: tuple[str, ...] = (
    "REQUESTED",
    "AWAITING_SUPPLIER",
    "CONFIRMED",
    "REJECTED",
    "EXTENSION_REQUESTED",
    "RELEASE_REQUESTED",
    "RELEASE_CONFIRMED",
    "EXPIRED_PENDING_VERIFICATION",
    "CONVERTED",
)

HOLD_REQUESTED = "REQUESTED"
HOLD_AWAITING_SUPPLIER = "AWAITING_SUPPLIER"
HOLD_CONFIRMED = "CONFIRMED"
HOLD_REJECTED = "REJECTED"
HOLD_EXTENSION_REQUESTED = "EXTENSION_REQUESTED"
HOLD_RELEASE_REQUESTED = "RELEASE_REQUESTED"
HOLD_RELEASE_CONFIRMED = "RELEASE_CONFIRMED"
HOLD_EXPIRED_PENDING_VERIFICATION = "EXPIRED_PENDING_VERIFICATION"
#: The one state Appendix A does not name: the terminal transition of a hold into a
#: booking, which the chain expresses as BOOKING CONFIRMED rather than as a hold state.
HOLD_CONVERTED = "CONVERTED"

#: The states that block their slot. Must match the partial unique index
#: ``uq_holds_active_slot`` and the PG exclusion constraint ``ex_holds_no_overlap``
#: — three restatements of one predicate, pinned by the tests.
#:
#: ``EXPIRED_PENDING_VERIFICATION`` is in the set on purpose: §7 forbids treating an
#: unverified expiry as a release, and a slot handed to a second guest while the
#: supplier may still be holding it is the exact double-booking that rule prevents.
OCCUPYING_HOLD_STATES: tuple[str, ...] = (
    "REQUESTED",
    "AWAITING_SUPPLIER",
    "CONFIRMED",
    "EXTENSION_REQUESTED",
    "RELEASE_REQUESTED",
    "EXPIRED_PENDING_VERIFICATION",
)

#: Occupying states whose TTL can still run out — what a sweep may expire.
LIVE_HOLD_STATES: tuple[str, ...] = (
    "REQUESTED",
    "AWAITING_SUPPLIER",
    "CONFIRMED",
    "EXTENSION_REQUESTED",
    "RELEASE_REQUESTED",
)

#: States in which the hold is over, so ``closed_at`` must be set. Overlaps
#: ``OCCUPYING_HOLD_STATES`` in exactly one member: the unverified expiry.
CLOSED_HOLD_STATES: tuple[str, ...] = (
    "REJECTED",
    "RELEASE_CONFIRMED",
    "EXPIRED_PENDING_VERIFICATION",
    "CONVERTED",
)

#: The statuses of a row in ``hold_extension_requests`` (§7: a requested extension is
#: not an extension — the supplier's answer is what moves the expiry).
EXTENSION_REQUEST_STATUSES: tuple[str, ...] = ("REQUESTED", "CONFIRMED", "REJECTED")

_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "REQUESTED": ("AWAITING_SUPPLIER", "CONFIRMED", "REJECTED"),
    "AWAITING_SUPPLIER": ("CONFIRMED", "REJECTED"),
    "CONFIRMED": (
        "EXTENSION_REQUESTED",
        "RELEASE_REQUESTED",
        # The internal release is one step: the platform is the source that says the
        # hold is over. A supplier-facing release goes through RELEASE_REQUESTED.
        "RELEASE_CONFIRMED",
        "CONVERTED",
        "EXPIRED_PENDING_VERIFICATION",
    ),
    # A pending request is still a hold: it may be answered, and its TTL may run out.
    "EXTENSION_REQUESTED": ("CONFIRMED", "EXPIRED_PENDING_VERIFICATION"),
    "RELEASE_REQUESTED": ("CONFIRMED", "RELEASE_CONFIRMED", "EXPIRED_PENDING_VERIFICATION"),
    # Only an authoritative source may close an unverified expiry (§7).
    "EXPIRED_PENDING_VERIFICATION": ("RELEASE_CONFIRMED",),
    "REJECTED": (),
    "RELEASE_CONFIRMED": (),
    "CONVERTED": (),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """Whether ``from_state -> to_state`` is a legal edge of §7's chain.

    An unknown state has no outgoing edges, so a typo refuses rather than passes.
    """
    return to_state in _TRANSITIONS.get(from_state, ())


def resolve_timezone(name: str) -> str:
    """The IANA name as stored on a hold, or a refusal naming what is wrong.

    §7 requires an expiry to carry a timezone; a name that is not a zone would store
    a timezone in appearance only.
    """
    candidate = require_text(name, "timezone", max_len=64)
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(
            f"not a known IANA timezone: {candidate}",
            code="HOLD_INVALID_TIMEZONE",
            timezone=candidate,
        ) from exc
    return candidate

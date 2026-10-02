"""One door for every AI action (§3 / §2A.6), with the §17 ladder deciding how wide it opens.

Why a gateway and not "call ``ai_service`` directly"
----------------------------------------------------
The AI service answers *identity* questions: is this principal allowed at all, is this tool on the
deny registry, has the owner published the medium-tool allowance. The gateway answers the
*operational* question on top of it: **how far may this principal go right now?** Without that
second question, "degraded" and "read-only" would be words on a status field rather than a limit
on behaviour — and a kill switch that does not change behaviour is decoration.

The two layers, deliberately:

======================  ==========================================================================
``ai_service``          who you are: registered, active band, tier, allowance, deny registry.
``AiGateway``           what the mode allows you to reach, and the record of every decision.
======================  ==========================================================================

Two invariants this module exists to hold
-----------------------------------------
1. **No unclassified tool.** The read-only rung is undecidable without knowing which tools write,
   so every tool in the catalogue must have an effect. An unclassified tool is *refused*, never
   guessed — and because ``ai_tools`` is append-only, the classification lives here in code with a
   parity test against the catalogue rather than as a column somebody could quietly edit.
2. **Nothing moves capability without a reason and a name.** Every decision (allowed or refused) is
   an audit row; every mode change is a lifecycle event carrying its actor. The circuit breaker is
   the only actor that is not a human, and it only ever *narrows*.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from roles import ROLE_IDS as _ROLE_IDS, may as _may
from ai_service import (
    AI_STATUS_ACTIVE,
    AI_STATUS_DEGRADED,
    AI_STATUS_LADDER,
    AI_STATUS_READ_ONLY,
    AI_STATUS_RETIRED,
    AI_STATUS_SUSPENDED,
    AiPrincipal,
    AiService,
)
from primitives import DomainError, require_ref, require_text

READ = "READ"
WRITE = "WRITE"

#: What each tool *changes*. Every tool in ``ai_tools`` must appear here, and no tool may appear
#: that the catalogue does not hold (both directions are tested). The default is earned by review:
#: "reports nothing" is READ, everything else — including a message to a guest — is WRITE.
TOOL_EFFECTS: Mapping[str, str] = {
    # LOW tier
    "arrival.inform_verified": WRITE,   # tells the guest something: a message leaves the system
    "booking.status": READ,
    "followup.routine": WRITE,
    "inquiry.clarify": WRITE,           # asks the guest a question
    "inquiry.intake": WRITE,
    "villa.answer_verified": READ,
    "villa.search": READ,
    "villa.suggest_alternatives": READ,  # proposes, changes nothing
    # MEDIUM tier
    "availability.request": WRITE,
    "comm.client_selected": WRITE,
    "comm.supplier_selected": WRITE,
    "hold.request_tentative": WRITE,
    "quotation.send": WRITE,
    "request.coordinate_special": WRITE,
    # HIGH tier
    "bank.change": WRITE,
    "contract.activate": WRITE,
    "discount.approve_out_of_policy": WRITE,
    "incident.compensate_serious": WRITE,
    "legal.dispute": WRITE,
    "margin_override.approve": WRITE,
    "payment.approve": WRITE,
    "payment.execute": WRITE,
    "rate.activate": WRITE,
    "rate.verify": WRITE,
    "refund.approve": WRITE,
    "refund.execute": WRITE,
}

#: The ceiling at each rung of the §17 ladder, as a sentence an operator can read in the audit.
MODE_CEILING: Mapping[str, str] = {
    AI_STATUS_ACTIVE: "everything the AI service permits",
    AI_STATUS_DEGRADED: "low-tier tools only",
    AI_STATUS_READ_ONLY: "tools that change nothing",
    AI_STATUS_SUSPENDED: "nothing",
    AI_STATUS_RETIRED: "nothing, permanently",
}

#: Tier ceiling per mode. ACTIVE and READ_ONLY list every tier because the *effect* decides there;
#: DEGRADED cuts by tier so a low-tier action still runs while the estate is on fire.
_MODE_TIERS: Mapping[str, tuple[str, ...]] = {
    AI_STATUS_ACTIVE: ("LOW", "MEDIUM", "HIGH"),
    AI_STATUS_DEGRADED: ("LOW",),
    AI_STATUS_READ_ONLY: ("LOW", "MEDIUM", "HIGH"),
}

#: Which mode change each verb performs — the kill switch has exactly one entry point.
_MODE_VERBS: Mapping[str, str] = {
    AI_STATUS_ACTIVE: "activate",
    AI_STATUS_DEGRADED: "degrade",
    AI_STATUS_READ_ONLY: "read_only",
    AI_STATUS_SUSPENDED: "suspend",
}

#: The capability a role must hold to move a principal up or down the ladder (§17). Held by the
#: Owner and the Technology-Security Manager; deliberately *not* by the GM or the auditor — nobody
#: may switch off the guard that watches them (ADR-0035: maker / checker / auditor).
AI_CONTROL_CAPABILITY = "ai.control"

#: Who may press the button, read out of the single role vocabulary rather than restated here.
AI_CONTROL_HOLDERS: tuple[str, ...] = tuple(r for r in _ROLE_IDS if _may(r, AI_CONTROL_CAPABILITY))

#: Circuit breaker: this many consecutive gateway failures for one principal, inside this window,
#: narrow it to READ_ONLY and open an incident. Numbers are operational, not commercial.
BREAKER_FAILURES = 3
BREAKER_WINDOW_MINUTES = 15

FAILURE_ACTION = "AI_GATEWAY_FAILURE"
AUTHORISED_ACTION = "AI_TOOL_AUTHORISED"


class GatewayError(DomainError):
    """Base class for gateway refusals."""

    code = "AI_GATEWAY_REFUSED"


class ToolEffectUnknown(GatewayError):
    """Fail closed: a tool nobody classified cannot be placed on the ladder."""

    code = "AI_TOOL_EFFECT_UNKNOWN"


class ModeRefused(GatewayError):
    """The identity may act, but the current mode does not reach this tool."""

    code = "AI_MODE_REFUSED"


class AiControlNotHeld(GatewayError):
    """§17/ADR-0035: the kill switch is a control with named holders, not a command anyone runs."""

    code = "AI_CONTROL_NOT_HELD"


class UnknownMode(GatewayError):
    """Only the five §17 statuses are modes; a typo must not pick one silently."""

    code = "AI_UNKNOWN_MODE"


@dataclass(frozen=True)
class GatewayDecision:
    """What the door did, and why — the same facts the audit row carries."""

    principal_id: str
    tool: str
    tier: str
    effect: str
    mode: str
    allowed: bool
    reason: str

    @property
    def is_write(self) -> bool:
        return self.effect == WRITE


@dataclass(frozen=True)
class BreakerState:
    """The breaker's verdict after one recorded failure."""

    principal_id: str
    consecutive_failures: int
    tripped: bool
    mode: str


class AiGateway:
    """Every AI action passes here. Not by convention: by test (:file:`tests/test_ai_gateway.py`)."""

    def __init__(
        self,
        ai: AiService,
        *,
        audit: Any,
        outbox: Optional[Any] = None,
        exceptions: Optional[Any] = None,
        clock: Any,
        ids: Any,
        agency_id: str = "agency_default",
    ) -> None:
        self.ai = ai
        self.audit = audit
        self.outbox = outbox
        self.exceptions = exceptions
        self.clock = clock
        self.ids = ids
        self.agency_id = str(agency_id)

    # -- the door ----------------------------------------------------------- #

    def mode(self, principal_id: str) -> str:
        """The §17 rung this principal currently stands on."""
        return self.ai.principal(principal_id).status

    def request(
        self,
        principal_id: str,
        tool: str,
        *,
        intent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        at: Optional[Any] = None,
    ) -> GatewayDecision:
        """Ask the door for one tool. Raises with an audited reason when refused.

        The order matters: the effect classification comes before the service's own decision so
        that an unclassified tool is refused as unclassified, not as unauthorised — an operator
        reading the audit must see *which* defence refused them.
        """
        principal = self.ai.principal(principal_id)
        definition = self.ai.tool(tool)
        effect = TOOL_EFFECTS.get(definition.tool)
        if effect is None:
            raise self._refuse(
                principal,
                definition,
                effect=effect,
                reason=f"tool {definition.tool} has no classified effect; refusing rather than guessing",
                error=ToolEffectUnknown(
                    "this tool is not classified as READ or WRITE", code="AI_TOOL_EFFECT_UNKNOWN",
                    tool=definition.tool,
                ),
                at=at,
            )

        # The identity layer first: registered, in the acting band, not denied, tier permitted.
        self.ai.authorize(principal_id, tool, intent=intent, at=at)

        mode = principal.status
        tiers = _MODE_TIERS.get(mode, ())
        if definition.tier not in tiers:
            reason = (
                f"mode {mode} reaches {MODE_CEILING.get(mode, 'nothing')}; "
                f"{definition.tool} is {definition.tier}"
            )
            raise self._refuse(
                principal,
                definition,
                effect=effect,
                reason=reason,
                error=ModeRefused(
                    reason, code="AI_MODE_REFUSED", tool=definition.tool, mode=mode
                ),
                at=at,
            )
        if mode == AI_STATUS_READ_ONLY and effect != READ:
            reason = (
                f"mode READ_ONLY reaches tools that change nothing; {definition.tool} is WRITE"
            )
            raise self._refuse(
                principal,
                definition,
                effect=effect,
                reason=reason,
                error=ModeRefused(
                    reason, code="AI_MODE_REFUSED", tool=definition.tool, mode=mode
                ),
                at=at,
            )

        reason = f"{definition.tool} ({definition.tier}/{effect}) allowed in mode {mode}"
        self.audit.append(
            action=AUTHORISED_ACTION,
            entity_type="ai_principal",
            entity_id=principal.principal_id,
            actor=principal.principal_id,
            actor_role="ai",
            reason=reason,
            tool=definition.tool,
            trace_id=correlation_id,
        )
        if effect == WRITE and self.outbox is not None:
            # A consequential action is recorded outwards as well: the gateway's yes is a fact
            # other components act on, so it must survive this process.
            self.outbox.emit(
                event_type="ai.action_authorised",
                aggregate_type="ai_principal",
                aggregate_id=principal.principal_id,
                payload={
                    "tool": definition.tool,
                    "tier": definition.tier,
                    "effect": effect,
                    "mode": mode,
                },
                actor=principal.principal_id,
                idempotency_key=f"ai-authorised:{principal.principal_id}:{definition.tool}:{self._stamp(at)}",
                correlation_id=correlation_id,
                at=at,
            )
        return GatewayDecision(
            principal_id=principal.principal_id,
            tool=definition.tool,
            tier=definition.tier,
            effect=effect,
            mode=mode,
            allowed=True,
            reason=reason,
        )

    # -- the switch --------------------------------------------------------- #

    def set_mode(
        self,
        principal_id: str,
        mode: str,
        *,
        actor: str,
        actor_role: str,
        reason: Optional[str] = None,
        at: Optional[Any] = None,
    ) -> AiPrincipal:
        """Move one principal to a rung of the ladder. The only way a mode changes.

        The authority check lives here and not only in the CLI: a button that any module can
        press is documentation, not a control. The breaker does *not* come through here — it is
        not a human, and it only ever narrows (see :meth:`record_failure`).
        """
        if not _may(actor_role, AI_CONTROL_CAPABILITY):
            raise AiControlNotHeld(
                f"the §17 kill switch is held by {', '.join(AI_CONTROL_HOLDERS)}"
                f"; {actor_role!r} does not hold {AI_CONTROL_CAPABILITY}",
                code="AI_CONTROL_NOT_HELD",
                actor_role=actor_role,
            )
        clean_mode = require_text(mode, "mode", max_len=20).strip().upper()
        if clean_mode not in AI_STATUS_LADDER:
            raise UnknownMode(
                f"not a §17 status: {clean_mode} (choose from {', '.join(AI_STATUS_LADDER)})",
                code="AI_UNKNOWN_MODE",
                mode=clean_mode,
            )
        verb = _MODE_VERBS.get(clean_mode)
        if verb is None:  # RETIRED — there is no going back to it, only arriving once
            return self.ai.retire(principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at)
        if clean_mode == AI_STATUS_SUSPENDED:
            return self.ai.suspend(principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at)
        if clean_mode == AI_STATUS_DEGRADED:
            return self.ai.degrade(principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at)
        if clean_mode == AI_STATUS_READ_ONLY:
            return self.ai.read_only(principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at)
        # The operator names a *state*; the domain picks the verb that legitimately reaches it.
        # ACTIVE is reachable from a narrowed rung (activate) and from SUSPENDED (reinstate);
        # asking an operator to remember which is which is how a kill switch goes unpressed.
        if self.ai.principal(principal_id).status == AI_STATUS_SUSPENDED:
            return self.ai.reinstate(
                principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at
            )
        return self.ai.activate(principal_id, actor=actor, actor_role=actor_role, reason=reason, at=at)

    def record_failure(
        self,
        principal_id: str,
        *,
        code: str,
        message: str,
        at: Optional[Any] = None,
    ) -> BreakerState:
        """Record one failure and trip the breaker if the streak is long enough.

        A policy refusal is *not* a failure — refusals are the system working, and the service
        already audits them. What this counts is an error: a tool the door permitted and that then
        failed. The streak is read back **out of the audit chain**, not out of a counter in memory: a
        breaker that forgets across restarts protects nothing, and the chain already holds every
        refusal with its time. A successful ``request`` for the same principal resets the streak.
        """
        principal = self.ai.principal(principal_id)
        clean_code = require_text(code, "code", max_len=80)
        clean_message = require_text(message, "message", max_len=300)
        self.audit.append(
            action=FAILURE_ACTION,
            entity_type="ai_principal",
            entity_id=principal.principal_id,
            actor=principal.principal_id,
            actor_role="ai",
            reason=f"{clean_code}: {clean_message}",
        )
        streak = self._consecutive_failures(principal.principal_id, at=at)
        if streak < BREAKER_FAILURES:
            return BreakerState(principal.principal_id, streak, False, principal.status)

        if principal.status in (AI_STATUS_ACTIVE, AI_STATUS_DEGRADED):
            principal = self.ai.read_only(
                principal.principal_id,
                actor=f"system:breaker:{clean_code}",
                reason=f"{streak} consecutive failures in {BREAKER_WINDOW_MINUTES} minutes",
                at=at,
            )
        if self.exceptions is not None:
            # A tripped breaker is an operational failure with an owner to chase, so it opens an
            # incident through the same engine as every other failure rather than a private log.
            self.exceptions.capture(
                category="AI_BREAKER",
                code=clean_code,
                message=f"AI principal {principal.principal_id} narrowed to {principal.status}: {clean_message}",
                source="ai_gateway",
                aggregate_id=principal.principal_id,
                at=at,
            )
        if self.outbox is not None:
            self.outbox.emit(
                event_type="ai.breaker_tripped",
                aggregate_type="ai_principal",
                aggregate_id=principal.principal_id,
                payload={"mode": principal.status, "streak": streak, "code": clean_code},
                actor="system:breaker",
                idempotency_key=f"ai-breaker:{principal.principal_id}:{self._stamp(at)}",
                at=at,
            )
        return BreakerState(principal.principal_id, streak, True, principal.status)

    # -- internals ---------------------------------------------------------- #

    def _consecutive_failures(self, principal_id: str, *, at: Optional[Any] = None) -> int:
        now = self._moment(at)
        horizon = now - _dt.timedelta(minutes=BREAKER_WINDOW_MINUTES)
        streak = 0
        for event in reversed(self.audit.for_entity("ai_principal", principal_id)):
            if event.action == AUTHORISED_ACTION:
                break
            if event.action != FAILURE_ACTION:
                continue
            if self._moment(event.occurred_at) < horizon:
                break
            streak += 1
        return streak

    def _refuse(
        self,
        principal: AiPrincipal,
        definition: Any,
        *,
        effect: Optional[str],
        reason: str,
        error: DomainError,
        at: Optional[Any],
    ) -> DomainError:
        """Audit the refusal, then hand back the error for the caller to raise."""
        self.audit.append(
            action="AI_TOOL_REFUSED",
            entity_type="ai_principal",
            entity_id=principal.principal_id,
            actor=principal.principal_id,
            actor_role="ai",
            reason=reason,
            tool=definition.tool,
        )
        return error

    def _moment(self, when: Optional[Any]) -> _dt.datetime:
        if when is None:
            when = self.clock.now()
        if isinstance(when, _dt.datetime):
            moment = when
        else:
            moment = _dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)

    def _stamp(self, at: Optional[Any]) -> str:
        return self._moment(at).isoformat()


def effect_of(tool: str) -> str:
    """The classification of one tool, refused (not guessed) when unknown."""
    name = require_ref(tool, "tool", max_len=80)
    try:
        return TOOL_EFFECTS[name]
    except KeyError:
        raise ToolEffectUnknown(
            "this tool is not classified as READ or WRITE", code="AI_TOOL_EFFECT_UNKNOWN", tool=name
        ) from None

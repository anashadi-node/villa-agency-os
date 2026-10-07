"""AI Control Tower / Orchestrator — Agent 1 of the AI workforce (§7 of V1.1).

Mission. Coordinate the AI workforce and durable workflows without becoming a
virtual CEO or acquiring domain authority. The tower:

1. **Routes** typed events to the correct bounded agent (§2A.3 boundaries),
2. **Watches** workflow and queue health (pending events, dead letters),
3. **Retries** only idempotent deliveries, inside the outbox's own budget,
4. **Escalates** to the responsible human owner through the one exception
   engine every other failure already goes through.

What it must never do (§7 Must Not — enforced by structure, not by hope): the
tower holds a machine principal like every other agent, and the AI gateway's
tier ceiling stops that principal from touching HIGH-tier tools. Approval and
money stay human. There is deliberately no method here that approves, pays,
rates or contracts anything.

§14: no free-form authority transfer. Routing is a typed, versioned event
through the transactional outbox — the receiver re-checks authoritative state
and never trusts the letter itself.

Deterministic by design: routing is a table lookup, health is two counters,
retry is the outbox's own ``publish_due``. No LLM in this module — a
coordinator that improvises is a coordinator nobody can audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from primitives import DomainError, Clock, IdFactory, SystemClock, require_ref, require_text

__all__ = [
    "ControlTowerError",
    "UnroutableEvent",
    "ROUTES",
    "ROUTE_EVENT_TYPES",
    "ROUTED_TO_KEY",
    "ESCALATION_EVENT",
    "RouteDecision",
    "ControlTower",
]


class ControlTowerError(DomainError):
    code = "CT_ERROR"


class UnroutableEvent(ControlTowerError):
    """This event type has no route: refusing beats guessing (§2A.3)."""
    code = "CT_EVENT_UNROUTABLE"


#: §2A.3 routing boundaries — the closed vocabulary of what the tower may route,
#: and the agent each kind belongs to. Adding a row is a spec change with a test,
#: not an edit made mid-incident.
ROUTES: Mapping[str, str] = {
    "MARKETING_CRM_LEAD": "commercial_ai",
    "RESERVATION_INTENT": "ai_reservation",
    "OPS_PROCESS": "operations_ai",
    "FINANCE_ADMIN": "finance_admin_ai",
    "REVENUE_BI": "revenue_bi_ai",
}

#: The event types that carry one of the routed intents. A type outside this
#: tuple is refused by :meth:`ControlTower.route` — the tower routes the kinds
#: of work the workforce exists for, and nothing else.
ROUTE_EVENT_TYPES: Mapping[str, str] = {
    "inquiry.received": "RESERVATION_INTENT",
    "lead.captured": "MARKETING_CRM_LEAD",
    "lead.qualified": "MARKETING_CRM_LEAD",
    "ops.task.needed": "OPS_PROCESS",
    "finance.receipt.reported": "FINANCE_ADMIN",
    "revenue.report.requested": "REVENUE_BI",
}

#: Payload key naming the receiving agent. §14: the receiver re-checks state;
#: this key says whose queue the event is for, it grants nothing.
ROUTED_TO_KEY = "routed_to"

#: §14: the escalation is a typed event too — the GM reads a queue, not a chat.
ESCALATION_EVENT = "ai.escalation.raised"

#: The tower's own tool names (§56 vocabulary extension) — *declared intent, not
#: registered tools* (ADR-0061 option B, owner decision 2026-10-07). The catalogue
#: ``ai_tools`` has one writer, the schema, so every row it holds is classified in
#: ``ai_gateway.TOOL_EFFECTS``; inserting these two on the construction path broke that
#: invariant silently. Note the second name: ``retry_safe`` *requeues* a delivery, so
#: its true effect is WRITE — classifying it READ would have been a wrong label, not
#: merely a wider rung. Health and retry stay deterministic calls on this module and on
#: the outbox's own budget.
TOWER_TOOLS: tuple[tuple[str, str, Optional[str], str], ...] = (
    ("workflow.status", "LOW", None, "Read workflow and queue health"),
    ("workflow.retry_safe", "LOW", None, "Requeue idempotent deliveries within budget"),
)


@dataclass(frozen=True)
class RouteDecision:
    """What one routed event produced."""

    outbox_event_id: str
    event_type: str
    routed_to: str


class _GatewayLike(Protocol):  # pragma: no cover - structural only
    def request(self, principal_id: str, tool: str, *, intent: Optional[str] = None,
                 correlation_id: Optional[str] = None, at: Any = None) -> Any: ...


class ControlTower:
    """Routes typed events, watches health, retries safely, escalates — nothing more."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Optional[Clock] = None,
        ids: Optional[IdFactory] = None,
        audit: Any = None,
        outbox: Any,
        exceptions: Any,
        ai: Any = None,
        gateway: Optional[_GatewayLike] = None,
        agency_id: str = "agency_default",
        actor: str = "ai.control_tower",
    ) -> None:
        self.conn = conn
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.ids = ids if ids is not None else IdFactory()
        self.audit = audit
        self.outbox = outbox
        self.exceptions = exceptions
        self.ai = ai
        self.gateway = gateway
        self.agency_id = require_ref(agency_id, "agency_id")
        self.actor = require_ref(actor, "actor")
        # ADR-0061 (owner decision 2026-10-07, option B): the tower *declares* its
        # read-tier names (see TOWER_TOOLS) and registers none of them. The catalogue
        # ``ai_tools`` has exactly one writer — the schema — so every row it holds is
        # classified in ``ai_gateway.TOOL_EFFECTS``. Inserting the declaration here
        # broke that two-way invariant on the *construction* path, silently: 26 rows
        # became 28 with two names no effect table could classify.

    # -- 1. routing --------------------------------------------------------- #

    def route(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
        idempotency_key: str = "",
        correlation_id: Optional[str] = None,
        at: Any = None,
    ) -> Any:
        """Emit one typed routing event through the transactional outbox.

        The route is a lookup in :data:`ROUTE_EVENT_TYPES` → :data:`ROUTES`.
        An unknown event type raises :class:`UnroutableEvent` — a silent guess
        would land the work where nobody reads it.
        """
        clean_type = require_text(event_type, "event_type", max_len=80)
        boundary = ROUTE_EVENT_TYPES.get(clean_type)
        if boundary is None:
            raise UnroutableEvent(
                "this event type has no routing boundary",
                code="CT_EVENT_UNROUTABLE",
                event_type=clean_type,
                known=sorted(ROUTE_EVENT_TYPES),
            )
        routed_to = ROUTES[boundary]
        clean_actor = require_ref(actor, "actor")
        body = dict(payload or {})
        body[ROUTED_TO_KEY] = routed_to
        key = idempotency_key or f"route-{require_ref(aggregate_id, 'aggregate_id')}-{clean_type}"
        return self.outbox.emit(
            event_type=clean_type,
            aggregate_type="routing",
            aggregate_id=aggregate_id,
            payload=body,
            actor=clean_actor,
            idempotency_key=key,
            correlation_id=correlation_id,
            at=at,
        )

    # -- 2. health ----------------------------------------------------------- #

    def health(self, *, at: Any = None) -> dict[str, Any]:
        """One read-only snapshot: queue depth, dead letters, escalation count."""
        now = (at or self.clock.now()).isoformat() if hasattr(at or self.clock.now(), "isoformat") else None
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status = 'PENDING' AND agency_id = ?",
            (self.agency_id,),
        ).fetchone()[0]
        dead = self.conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status = 'DEAD' AND agency_id = ?",
            (self.agency_id,),
        ).fetchone()[0]
        open_incidents = self.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE agency_id = ? AND status NOT IN"
            " ('RESOLVED', 'CLOSED')",
            (self.agency_id,),
        ).fetchone()[0]
        return {
            "pending_events": int(pending),
            "dead_letters": int(dead),
            "open_incidents": int(open_incidents),
            "checked_at": now,
            "healthy": int(dead) == 0,
        }

    # -- 3. safe retry -------------------------------------------------------- #

    def retry_due(self, *, actor: str, at: Any = None) -> int:
        """Publish every delivery that is due again, inside the outbox's budget.

        Dead letters are *never* touched here: they need a human decision, and
        a retry loop is not a decision.
        """
        # The actor is carried by the outbox's own delivery rows; the tower adds none.
        if not hasattr(self.outbox, "publish_due"):
            return 0
        report = self.outbox.publish_due(lambda event: None, at=at)
        return len(report.published)

    # -- 4. escalation --------------------------------------------------------- #

    def escalate(
        self,
        *,
        category: str,
        code: str,
        message: str,
        source: str,
        aggregate_id: Optional[str] = None,
        actor: str,
        personal_data_involved: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        at: Any = None,
    ) -> Any:
        """Open the failure through the one engine, then announce it in the queue.

        The tower does not resolve; it packages. The incident carries the
        playbook and severity; the outbox event is what the human owner reads.
        """
        clean_actor = require_ref(actor, "actor")
        capture = self.exceptions.capture(
            category=category,
            code=require_text(code, "code", max_len=64),
            message=require_text(message, "message", max_len=500),
            source=require_text(source, "source", max_len=64),
            aggregate_id=aggregate_id,
            personal_data_involved=personal_data_involved,
            details=details,
            at=at,
        )
        self.outbox.emit(
            event_type=ESCALATION_EVENT,
            aggregate_type="incident",
            aggregate_id=capture.incident_id,
            payload={
                "category": capture.category,
                "severity": capture.severity,
                "playbook": capture.playbook,
                "source": source,
                "aggregate_id": aggregate_id or "-",
            },
            actor=clean_actor,
            idempotency_key=f"esc-{capture.dedupe_key[:64]}",
            at=at,
        )
        if self.audit is not None:
            self.audit.append(
                action="control_tower.escalate",
                entity_type="incident",
                entity_id=capture.incident_id,
                actor=clean_actor,
                actor_role="gm",
                after={
                    "category": capture.category,
                    "severity": capture.severity,
                    "incident_id": capture.incident_id,
                },
            )
        return capture

    # -- 5. the tower's own identity ------------------------------------------ #

    def register_principal(self, *, secret_ref: str, owner_actor: str, at: Any = None) -> Any:
        """Register the tower as one machine principal among the workforce.

        It registers no tool of its own (ADR-0061 option B): :data:`TOWER_TOOLS` is a
        declaration of intent, and the gateway's tier and policy layer — not this
        module's good intentions — is what keeps a coordinator from approving money.
        """
        if self.ai is None:
            raise ControlTowerError(
                "no AI service wired: the tower cannot mint its own identity",
                code="CT_NO_AI_SERVICE",
            )
        clean_ref = require_ref(secret_ref, "secret_ref")
        clean_owner = require_ref(owner_actor, "owner_actor")
        return self.ai.register_principal(
            "ai-control-tower", secret_ref=clean_ref, actor=clean_owner, at=at
        )

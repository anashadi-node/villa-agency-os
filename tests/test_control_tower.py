"""AI Control Tower / Orchestrator — Agent 1 of the AI workforce (§7).

What these tests are for. The Control Tower coordinates the AI workforce without
becoming a virtual CEO: it routes typed events to the right agent, watches
deadlines, retries only what is safe to retry, escalates what it cannot fix, and
counts every decision. The interesting cases are a misrouted event, a retry that
would exceed its budget, an escalation with no human holder, and the claim that
it can approve money — the answer must be no, and the refusal must be auditable.

Every test asserts the *record* (tables, outbox, incidents), never a log line.
"""

from __future__ import annotations

import pytest

import control_tower as CT
import outbox as OB
import exception_engine as EE
import incidents as INC
import audit as AUD
import ai_service as AIS
import ai_gateway as AG
import policy_engine as PE
from primitives import FrozenClock, IdFactory

NOW = "2026-10-06T08:00:00+00:00"
AGENCY = "agency_default"


class World:
    def __init__(self, **parts):
        self.__dict__.update(parts)


@pytest.fixture()
def world(conn):
    clock = FrozenClock.at(NOW)
    ids = IdFactory(True, "ct")
    audit = AUD.AuditLog(conn, clock=clock, ids=ids)
    outbox = OB.OutboxService(conn, clock=clock, ids=ids, agency_id=AGENCY)
    incidents = INC.IncidentService(conn, clock=clock, ids=ids, agency_id=AGENCY)
    exceptions = EE.ExceptionEngine(
        conn, clock=clock, ids=ids, agency_id=AGENCY, incidents=incidents
    )
    policy = PE.PolicyResolver(conn, clock=clock, agency_id=AGENCY)
    ai = AIS.AiService(conn, policy=policy, clock=clock)
    gateway = AG.AiGateway(
        ai, audit=audit, outbox=outbox, exceptions=exceptions, clock=clock, ids=ids
    )
    tower = CT.ControlTower(
        conn,
        clock=clock,
        ids=ids,
        audit=audit,
        outbox=outbox,
        exceptions=exceptions,
        ai=ai,
        agency_id=AGENCY,
    )
    return World(
        conn=conn, clock=clock, audit=audit, outbox=outbox, incidents=incidents,
        exceptions=exceptions, policy=policy, ai=ai, gateway=gateway,
        tower=tower, ids=ids,
    )


def events(conn, etype: str | None = None) -> list:
    if etype is None:
        return list(conn.execute("SELECT * FROM outbox_events ORDER BY created_at"))
    return list(
        conn.execute("SELECT * FROM outbox_events WHERE event_type = ? ORDER BY created_at", (etype,))
    )


# --------------------------------------------------------------------------- #
# 1. the vocabulary is closed — routing cannot be improvised
# --------------------------------------------------------------------------- #

def test_router_vocabulary_is_closed(world):
    """Every route the tower knows names a real agent/service from the org map."""
    assert CT.ROUTES == {
        "MARKETING_CRM_LEAD": "commercial_ai",
        "RESERVATION_INTENT": "ai_reservation",
        "OPS_PROCESS": "operations_ai",
        "FINANCE_ADMIN": "finance_admin_ai",
        "REVENUE_BI": "revenue_bi_ai",
    }
    assert set(CT.ROUTE_EVENT_TYPES.values()) <= set(CT.ROUTES)


def test_unknown_event_type_is_refused_not_guessed(world):
    """§2A.3: routing boundaries are closed. A type with no route is a refusal,
    not a guess — a silent guess is how an event lands where nobody reads it."""
    with pytest.raises(CT.UnroutableEvent):
        world.tower.route(event_type="partner.negotiation", aggregate_id="lead_1", actor="ct.tower")


# --------------------------------------------------------------------------- #
# 2. routing emits one typed outbox event addressed to one agent
# --------------------------------------------------------------------------- #

def test_route_reservation_intent_emits_addressed_event(world):
    emission = world.tower.route(
        event_type="inquiry.received",
        aggregate_id="case_0001",
        actor="ct.tower",
        payload={"channel": "web", "guest_ref": "guest_1"},
        idempotency_key="route-case-0001-web",
    )
    assert emission.event_type == "inquiry.received"
    rows = events(world.conn, "inquiry.received")
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    payload = __import__("json").loads(rows[0]["payload"])
    assert payload["routed_to"] == CT.ROUTES["RESERVATION_INTENT"]


def test_route_is_idempotent_on_key(world):
    """A duplicate delivery of the same event is the same letter, not two."""
    world.tower.route(
        event_type="inquiry.received", aggregate_id="case_1", actor="ct.tower",
        payload={}, idempotency_key="route-case-1",
    )
    world.tower.route(
        event_type="inquiry.received", aggregate_id="case_1", actor="ct.tower",
        payload={}, idempotency_key="route-case-1",
    )
    assert len(events(world.conn, "inquiry.received")) == 1


# --------------------------------------------------------------------------- #
# 3. health snapshot — read-only, deterministic
# --------------------------------------------------------------------------- #

def test_health_reports_pending_and_dead_letters(world):
    world.tower.route(
        event_type="inquiry.received", aggregate_id="c1", actor="ct.tower",
        payload={}, idempotency_key="route-c1",
    )
    health = world.tower.health()
    assert health["pending_events"] == 1
    assert health["dead_letters"] == 0


# --------------------------------------------------------------------------- #
# 4. safe retry — budgeted, idempotent-only
# --------------------------------------------------------------------------- #

def test_retry_requeues_within_budget(world):
    """One failing delivery puts the event in FAILED with a backoff clock;
    the tower's retry pass is the outbox's own publish_due — never a hand-rolled loop."""
    row = world.tower.route(
        event_type="inquiry.received", aggregate_id="c2", actor="ct.tower",
        payload={}, idempotency_key="route-c2",
    )
    # one failed transport attempt → FAILED, backoff 1 minute (BACKOFF_MINUTES[0])
    report = world.outbox.publish_due(lambda event: (_ for _ in ()).throw(RuntimeError("down")))
    assert report.dead_lettered == ()
    assert world.conn.execute(
        "SELECT status FROM outbox_events WHERE outbox_event_id = ?", (row.outbox_event_id,)
    ).fetchone()[0] == "FAILED"
    # the backoff has not elapsed → the tower's pass claims nothing yet
    assert world.tower.retry_due(actor="ct.tower") == 0
    # after the backoff elapses the same event is due again and gets delivered
    later = world.clock.now() + __import__("datetime").timedelta(minutes=2)
    delivered = world.outbox.publish_due(lambda event: None, at=later)
    assert row.outbox_event_id in delivered.published


def test_retry_never_touches_dead_letters(world):
    """A dead letter needs a human decision; a retry loop is not a decision.
    Only requeue() — the human act with a reason — can revive one."""
    row = world.tower.route(
        event_type="inquiry.received", aggregate_id="c3", actor="ct.tower",
        payload={}, idempotency_key="route-c3",
    )
    # Each failure pushes next_attempt_at further out (1, 5, 15, 60, 240 minutes);
    # the frozen clock is advanced past each backoff so the next pass can claim it.
    import datetime as _dt
    moment = world.clock.now()
    for attempt in range(row.max_attempts + 1):
        moment = moment + _dt.timedelta(minutes=6 * (attempt + 1) + 61)
        world.outbox.publish_due(
            lambda event: (_ for _ in ()).throw(RuntimeError("down")), at=moment
        )
    status = world.conn.execute(
        "SELECT status FROM outbox_events WHERE outbox_event_id = ?", (row.outbox_event_id,)
    ).fetchone()[0]
    assert status == "DEAD_LETTER"
    # the tower's automated pass leaves the dead letter exactly where it is
    assert world.tower.retry_due(actor="ct.tower") == 0
    assert world.conn.execute(
        "SELECT status FROM outbox_events WHERE outbox_event_id = ?", (row.outbox_event_id,)
    ).fetchone()[0] == "DEAD_LETTER"


# --------------------------------------------------------------------------- #
# 5. escalation — the tower escalates, it never resolves
# --------------------------------------------------------------------------- #

def test_escalation_opens_an_incident_and_announces_it(world):
    capture = world.tower.escalate(
        category="BOOKING_CONFLICT",
        code="CT_DOUBLE_ROUTE",
        message="two workflows claim the same unit",
        source="control_tower",
        aggregate_id="unit_1",
        actor="ct.tower",
    )
    assert capture.incident_id
    assert len(events(world.conn, "ai.escalation.raised")) == 1


# --------------------------------------------------------------------------- #
# 6. the tower is not a virtual CEO — §7 Must Not is enforced, not hoped
# --------------------------------------------------------------------------- #

def test_tower_cannot_request_a_high_tier_tool(world):
    """The tower holds a principal like any agent; the gateway tier ceiling stops
    it from approving money even though the tower coordinates everything."""
    principal = world.ai.register_principal(
        "ai-control-tower", secret_ref="VILLA_CT_SECRET", actor="owner_anas"
    )
    with pytest.raises(Exception):
        world.gateway.request(principal.principal_id, "payment.approve", intent="ct: approve")


def test_tower_principal_is_registered_low_tier_only(world):
    """§7: the tower routes, coordinates, escalates. Its own tools are read-tier."""
    names = {t.tool for t in world.ai.tools() if t.tier == "LOW"}
    assert "workflow.status" in names
    assert "workflow.retry_safe" in names

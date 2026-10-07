"""The gateway is the single door (§3) and the §17 ladder is its width.

The tests are grouped the way the claims fail:

1. **The door is the only door.** No other module calls ``AiService.authorize`` (a static guard),
   and every tool in the catalogue has a classified effect in both directions.
2. **The ladder narrows, never widens.** DEGRADED reaches low-tier tools and nothing else, even
   when the owner has published the medium-tool allowance; READ_ONLY reaches tools that change
   nothing, even when they are low risk. That pair is the whole point of having five statuses
   instead of three — a switch that does not change behaviour is decoration.
3. **Every decision is readable afterwards.** Allowed actions are audited, refusals are audited
   *with the mode that refused them*, and only WRITE actions leave an outbox event.
4. **The breaker narrows on its own, and never widens.** Three consecutive failures inside the
   window narrow a principal, open an incident through the same engine as every other failure, and
   hand capability back only to a human.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re

import pytest

import outbox as OB
from ai_gateway import (
    AI_CONTROL_CAPABILITY,
    AI_CONTROL_HOLDERS,
    AUTHORISED_ACTION,
    BREAKER_FAILURES,
    FAILURE_ACTION,
    READ,
    TOOL_EFFECTS,
    WRITE,
    AiGateway,
    ModeRefused,
    ToolEffectUnknown,
    UnknownMode,
    effect_of,
)
from ai_service import (
    AI_STATUS_ACTIVE,
    AI_STATUS_DEGRADED,
    AI_STATUS_READ_ONLY,
    AI_STATUS_SUSPENDED,
    MEDIUM_ALLOWANCE_KEY,
    AiPrincipalNotActive,
    AiService,
)
from audit import AuditLog
from exception_engine import ExceptionEngine
from policy_engine import PolicyResolver
from primitives import DomainError, IdFactory

NOW = "2026-10-05T08:00:00+00:00"
AGENCY = "agency_default"
LOW_READ = "villa.search"
LOW_READ_2 = "booking.status"
LOW_WRITE = "inquiry.intake"
LOW_WRITE_2 = "followup.routine"
MEDIUM = "availability.request"
MEDIUM_2 = "quotation.send"
HIGH = "payment.approve"

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class SteppingClock:
    """A clock the test moves, so "inside the window" is a fact rather than a sleep."""

    def __init__(self, start: str) -> None:
        self._now = _dt.datetime.fromisoformat(start)

    def now(self) -> _dt.datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + _dt.timedelta(**kwargs)


class World:
    def __init__(self, **parts):
        self.__dict__.update(parts)


@pytest.fixture()
def world(conn):
    clock = SteppingClock(NOW)
    audit = AuditLog(conn, clock=clock, ids=IdFactory(True, "gw-audit"))
    outbox = OB.OutboxService(conn, clock=clock, ids=IdFactory(True, "gw-out"), agency_id=AGENCY)
    exceptions = ExceptionEngine(
        conn, clock=clock, ids=IdFactory(True, "gw-ex"), agency_id=AGENCY
    )
    policy = PolicyResolver(conn, clock=clock, agency_id=AGENCY)
    ai = AiService(conn, policy=policy, clock=clock)
    gateway = AiGateway(
        ai, audit=audit, outbox=outbox, exceptions=exceptions, clock=clock, ids=IdFactory(True, "gw")
    )
    principal = ai.register_principal(
        "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="owner_anas"
    )
    return World(
        conn=conn, clock=clock, audit=audit, outbox=outbox, exceptions=exceptions,
        policy=policy, ai=ai, gateway=gateway, principal=principal,
        # The system generates the id; "villa-concierge" is only the human-readable name.
        pid=principal.principal_id,
    )


def allow_medium(world, tools=(MEDIUM, MEDIUM_2)) -> None:
    """The owner publishes the §56 medium-tool allowance, effective today."""
    world.policy.publish(
        MEDIUM_ALLOWANCE_KEY,
        list(tools),
        effective_from="2026-10-05",
        effective_to="9999-12-31",
        source_doc_ref="§56 medium-risk policy-controlled tools",
        approved_by="owner_anas",
    )


def audit_actions(world, action: str) -> list:
    return [e for e in world.audit.for_entity("ai_principal", world.pid) if e.action == action]


# --------------------------------------------------------------------------- #
# 1. The door is the only door
# --------------------------------------------------------------------------- #
def test_no_module_outside_the_gateway_asks_the_ai_service_for_permission():
    """§3: agents act through the gateway. A second caller would be a second policy."""
    callers = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in {"ai_gateway.py", "ai_service.py"}:
            continue
        if re.search(r"\.authorize\(", path.read_text(encoding="utf-8")):
            callers.append(path.name)
    assert callers == [], f"these modules authorise AI actions directly: {callers}"


def test_every_registered_tool_is_classified_and_nothing_else_is(world):
    """Both directions: an unclassified tool is refused, and a stale entry is a lie."""
    from_catalogue = {row[0] for row in world.conn.execute("SELECT tool FROM ai_tools")}
    assert set(TOOL_EFFECTS) == from_catalogue
    assert set(TOOL_EFFECTS.values()) == {READ, WRITE}


def test_only_the_schema_writes_the_tool_catalogue():
    """ADR-0061 (owner decision 2026-10-07, option B). The two-way invariant above is
    only meaningful while the catalogue has one writer. The Control Tower used to insert
    its own declaration from its *constructor*, taking a seeded catalogue from 26 rows to
    28 — two names no effect table could classify — and nothing noticed, because every
    fixture built `ai_tools` from the seed rather than from the code under test. A module
    that registers a tool must classify that same name in the same commit; this scan is
    where that shows up."""
    writers = sorted(
        path.name
        for path in SRC.glob("*.py")
        if "INSERT INTO ai_tools" in path.read_text(encoding="utf-8")
    )
    assert writers == [], f"these modules write the tool catalogue: {writers}"


def test_the_workforces_declared_names_stay_out_of_the_door(world):
    """The workforce's declared names are library contracts, not gateway tools (ADR-0061
    option B). If a worker ever needs one *through* the door, that is option A — classify
    the effect READ and add the catalogue row in the same commit. This is the tripwire: it
    fails the moment a declaration is registered without a classification."""
    import control_tower as CT
    import finance_admin as FIN
    import operations_agent as OPS

    declared = (
        {name for name, *_ in CT.TOWER_TOOLS}
        | {name for name, *_ in OPS.OPERATIONS_TOOLS}
        | {name for name, *_ in FIN.FINANCE_TOOLS}
    )
    assert len(declared) == 8, sorted(declared)
    # Build the coordinator on this catalogue: this is the path that used to insert
    # the tower's two names, and a fixture that never took it saw nothing.
    CT.ControlTower(
        world.conn, clock=world.clock, ids=IdFactory(True, "gw-decl"),
        audit=world.audit, outbox=world.outbox, exceptions=world.exceptions,
        ai=world.ai, agency_id=AGENCY,
    )
    from_catalogue = {row[0] for row in world.conn.execute("SELECT tool FROM ai_tools")}
    assert declared.isdisjoint(from_catalogue)
    assert declared.isdisjoint(TOOL_EFFECTS)


def test_an_unclassified_tool_is_refused_rather_than_guessed(world):
    with pytest.raises(ToolEffectUnknown) as raised:
        effect_of("villa.teleport")
    assert raised.value.code == "AI_TOOL_EFFECT_UNKNOWN"


def test_the_read_set_is_the_four_tools_that_change_nothing():
    """Pinned deliberately: widening READ widens the read-only rung for every tenant at once."""
    assert {tool for tool, effect in TOOL_EFFECTS.items() if effect == READ} == {
        "villa.search",
        "villa.answer_verified",
        "villa.suggest_alternatives",
        "booking.status",
    }


# --------------------------------------------------------------------------- #
# 2. The ladder narrows, never widens
# --------------------------------------------------------------------------- #
def test_active_reaches_a_low_tool(world):
    decision = world.gateway.request(world.pid, LOW_READ)
    assert decision.allowed and decision.mode == AI_STATUS_ACTIVE and decision.effect == READ


def test_degraded_reaches_low_tier_and_refuses_medium_even_when_policy_allows_it(world):
    allow_medium(world)
    world.gateway.set_mode(world.pid, AI_STATUS_DEGRADED, actor="owner_anas", actor_role="owner", reason="drill")

    assert world.gateway.request(world.pid, LOW_WRITE).allowed
    with pytest.raises(ModeRefused) as raised:
        world.gateway.request(world.pid, MEDIUM)
    assert raised.value.code == "AI_MODE_REFUSED"
    assert "low-tier tools only" in str(raised.value)


def test_read_only_reaches_reading_tools_and_refuses_low_risk_writes(world):
    """The pair that justifies the whole ladder: low risk is not the same as no effect."""
    world.gateway.set_mode(world.pid, AI_STATUS_READ_ONLY, actor="owner_anas", actor_role="owner", reason="drill")

    assert world.gateway.request(world.pid, LOW_READ_2).allowed
    for tool in (LOW_WRITE, LOW_WRITE_2):
        with pytest.raises(ModeRefused) as raised:
            world.gateway.request(world.pid, tool)
        assert "change nothing" in str(raised.value)


def test_a_suspended_principal_is_stopped_by_the_identity_layer(world):
    world.gateway.set_mode(world.pid, AI_STATUS_SUSPENDED, actor="owner_anas", actor_role="owner", reason="incident")
    with pytest.raises(AiPrincipalNotActive):
        world.gateway.request(world.pid, LOW_READ)


def test_a_high_risk_tool_is_refused_even_in_active_mode(world):
    """The gateway does not soften the service's own rules; it only adds a ceiling."""
    with pytest.raises(DomainError) as raised:
        world.gateway.request(world.pid, HIGH)
    assert raised.value.code in {"AI_HUMAN_AUTHORITY_ONLY", "AI_DENIED_ACTION"}


def test_the_ladder_never_widens_through_the_narrowing_verb(world):
    from ai_service import AiLadderRefused

    world.gateway.set_mode(world.pid, AI_STATUS_SUSPENDED, actor="owner_anas", actor_role="owner")
    with pytest.raises(AiLadderRefused):
        world.gateway.set_mode(world.pid, AI_STATUS_DEGRADED, actor="owner_anas", actor_role="owner")


def test_activate_is_the_only_way_back_up(world):
    world.gateway.set_mode(world.pid, AI_STATUS_READ_ONLY, actor="owner_anas", actor_role="owner")
    assert world.gateway.mode(world.pid) == AI_STATUS_READ_ONLY
    world.gateway.set_mode(world.pid, AI_STATUS_ACTIVE, actor="owner_anas", actor_role="owner", reason="checked")
    assert world.gateway.mode(world.pid) == AI_STATUS_ACTIVE
    assert world.gateway.request(world.pid, LOW_WRITE).allowed


def test_an_unknown_mode_is_refused(world):
    with pytest.raises(UnknownMode):
        world.gateway.set_mode(world.pid, "SLOW", actor="owner_anas", actor_role="owner")


# --------------------------------------------------------------------------- #
# 3. Every decision is readable afterwards
# --------------------------------------------------------------------------- #
def test_an_allowed_action_is_audited_with_its_mode_and_effect(world):
    world.gateway.request(world.pid, LOW_READ, correlation_id="corr-1")
    rows = audit_actions(world, AUTHORISED_ACTION)
    assert len(rows) == 1
    assert "villa.search (LOW/READ) allowed in mode ACTIVE" == rows[0].reason
    assert rows[0].tool == "villa.search"


def test_a_refusal_names_the_mode_that_refused_it(world):
    world.gateway.set_mode(world.pid, AI_STATUS_READ_ONLY, actor="owner_anas", actor_role="owner")
    with pytest.raises(ModeRefused):
        world.gateway.request(world.pid, LOW_WRITE)
    rows = audit_actions(world, "AI_TOOL_REFUSED")
    assert len(rows) == 1
    assert "READ_ONLY" in rows[0].reason and "inquiry.intake" in rows[0].reason


def test_only_actions_that_change_something_leave_the_door_as_an_event(world):
    world.gateway.request(world.pid, LOW_READ)
    assert world.conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    world.gateway.request(world.pid, LOW_WRITE)
    row = world.conn.execute(
        "SELECT event_type, payload, actor FROM outbox_events"
    ).fetchone()
    assert row["event_type"] == "ai.action_authorised"
    assert row["actor"] == world.pid
    payload = json.loads(row["payload"])
    assert payload["tool"] == LOW_WRITE and payload["effect"] == WRITE


# --------------------------------------------------------------------------- #
# 4. The breaker narrows on its own, and never widens
# --------------------------------------------------------------------------- #
def fail(world, code: str = "TOOL_TIMEOUT", message: str = "upstream timed out"):
    return world.gateway.record_failure(world.pid, code=code, message=message)


def test_three_consecutive_failures_narrow_the_principal_and_open_an_incident(world):
    for _ in range(BREAKER_FAILURES - 1):
        state = fail(world)
        assert state.tripped is False
    tripped = fail(world)
    assert tripped.tripped is True and tripped.mode == AI_STATUS_READ_ONLY
    assert world.gateway.mode(world.pid) == AI_STATUS_READ_ONLY

    row = world.conn.execute(
        "SELECT severity, playbook, source, status, summary FROM incidents"
    ).fetchone()
    assert tuple(row)[:4] == ("SEV3", "PROVIDER_FAILURE", "ai_gateway", "OPEN")
    assert "narrowed to READ_ONLY" in row["summary"]


def test_a_tripped_breaker_still_permits_reading(world):
    for _ in range(BREAKER_FAILURES):
        fail(world)
    assert world.gateway.request(world.pid, LOW_READ).allowed
    with pytest.raises(ModeRefused):
        world.gateway.request(world.pid, LOW_WRITE)


def test_a_success_resets_the_streak(world):
    fail(world)
    fail(world)
    world.gateway.request(world.pid, LOW_READ)  # the door opened: the streak is over
    fail(world)
    state = fail(world)
    assert state.consecutive_failures == 2 and state.tripped is False


def test_failures_older_than_the_window_do_not_accumulate(world):
    fail(world)
    fail(world)
    world.clock.advance(minutes=20)
    state = fail(world)
    assert state.consecutive_failures == 1 and state.tripped is False


def test_the_breaker_does_not_widen_a_suspended_principal(world):
    world.gateway.set_mode(world.pid, AI_STATUS_SUSPENDED, actor="owner_anas", actor_role="owner")
    for _ in range(BREAKER_FAILURES):
        state = fail(world)
    assert state.tripped is True
    assert world.gateway.mode(world.pid) == AI_STATUS_SUSPENDED
    assert world.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_the_breaker_announces_itself_on_the_outbox(world):
    for _ in range(BREAKER_FAILURES):
        fail(world)
    kinds = [row[0] for row in world.conn.execute("SELECT event_type FROM outbox_events")]
    assert "ai.breaker_tripped" in kinds


def test_a_human_can_hand_capability_back_after_a_trip(world):
    for _ in range(BREAKER_FAILURES):
        fail(world)
    world.gateway.set_mode(
        world.pid, AI_STATUS_ACTIVE, actor="owner_anas", actor_role="owner", reason="root cause fixed"
    )
    assert world.gateway.request(world.pid, LOW_WRITE).allowed
    assert len(audit_actions(world, FAILURE_ACTION)) == BREAKER_FAILURES


# -- 4. who may press the button -------------------------------- 2026-10-02 -- #


def test_ai_control_is_held_by_the_owner_and_the_security_manager_only():
    """§17 with ADR-0035's separation: nobody switches off the guard that watches them."""
    import roles

    holders = {role for role in roles.ROLE_IDS if roles.may(role, AI_CONTROL_CAPABILITY)}
    assert holders == {"owner", "system_admin"}
    assert AI_CONTROL_HOLDERS == tuple(r for r in roles.ROLE_IDS if r in holders)
    # The auditor reads and records; the GM approves money. Neither holds the switch.
    assert not roles.may("auditor", AI_CONTROL_CAPABILITY)
    assert not roles.may("gm", AI_CONTROL_CAPABILITY)


def test_a_role_without_ai_control_is_refused_in_the_domain_not_just_the_cli(world):
    from ai_gateway import AiControlNotHeld

    for role in ("gm", "auditor", "reservation_manager"):
        with pytest.raises(AiControlNotHeld) as raised:
            world.gateway.set_mode(
                world.pid, AI_STATUS_READ_ONLY, actor="mr_gm", actor_role=role, reason="drill"
            )
        assert "ai.control" in str(raised.value)
    assert world.gateway.mode(world.pid) == AI_STATUS_ACTIVE


def test_the_audit_row_carries_the_role_that_was_verified(world):
    world.gateway.set_mode(
        world.pid, AI_STATUS_READ_ONLY, actor="mr_tsm", actor_role="system_admin", reason="drill"
    )
    row = world.conn.execute(
        "SELECT actor, actor_role FROM audit_events WHERE action = 'AI_PRINCIPAL_READ_ONLY'"
    ).fetchone()
    assert (row["actor"], row["actor_role"]) == ("mr_tsm", "system_admin")


def test_active_is_reachable_from_every_narrowed_rung(world):
    """The operator names a state; the domain picks the verb (activate vs reinstate)."""
    world.gateway.set_mode(
        world.pid, AI_STATUS_DEGRADED, actor="mr_owner", actor_role="owner", reason="drill"
    )
    assert world.gateway.set_mode(
        world.pid, AI_STATUS_ACTIVE, actor="mr_owner", actor_role="owner", reason="back"
    ).status == AI_STATUS_ACTIVE

    world.gateway.set_mode(
        world.pid, AI_STATUS_SUSPENDED, actor="mr_owner", actor_role="owner", reason="incident"
    )
    back = world.gateway.set_mode(
        world.pid, AI_STATUS_ACTIVE, actor="mr_owner", actor_role="owner", reason="cleared"
    )
    assert back.status == AI_STATUS_ACTIVE
    kinds = [
        row[0]
        for row in world.conn.execute(
            "SELECT kind FROM ai_principal_events ORDER BY seq"
        ).fetchall()
    ]
    assert kinds == ["REGISTERED", AI_STATUS_DEGRADED, "ACTIVATED", "SUSPENDED", "REINSTATED"]


def test_the_hint_an_operator_reads_matches_what_the_guard_allows(world):
    """Every mode offered by `reachable_modes` must actually work, and nothing else may."""
    from ai_service import AI_STATUS_LADDER, AI_STATUS_RETIRED

    allowed = set(world.ai.reachable_modes(world.pid))
    assert allowed == {AI_STATUS_ACTIVE, AI_STATUS_DEGRADED, AI_STATUS_READ_ONLY,
                       AI_STATUS_SUSPENDED, AI_STATUS_RETIRED}
    for mode in AI_STATUS_LADDER[:-1]:  # RETIRED is terminal, tested on its own below
        world.gateway.set_mode(
            world.pid, AI_STATUS_ACTIVE, actor="mr_owner", actor_role="owner", reason="reset"
        )
        reachable = set(world.ai.reachable_modes(world.pid))
        assert mode in reachable, f"{mode} is reachable but not offered"
        moved = world.gateway.set_mode(
            world.pid, mode, actor="mr_owner", actor_role="owner", reason="drill"
        )
        assert moved.status == mode

    # A retired principal reaches nothing: the ladder ends there, and the hint says so.
    world.gateway.set_mode(
        world.pid, AI_STATUS_RETIRED, actor="mr_owner", actor_role="owner", reason="decommission"
    )
    assert world.ai.reachable_modes(world.pid) == ()
    with pytest.raises(Exception) as raised:
        world.gateway.set_mode(
            world.pid, AI_STATUS_ACTIVE, actor="mr_owner", actor_role="owner", reason="oops"
        )
    assert getattr(raised.value, "code", "") == "AI_PRINCIPAL_RETIRED"


def test_a_mode_change_and_its_audit_row_are_one_write(world, monkeypatch):
    """Autocommit would otherwise leave a narrowed principal with no audit row (2026-10-02)."""
    class ExplodingAudit:
        def append(self, **kwargs):
            raise RuntimeError("chain refused the row")

    world.ai.audit = ExplodingAudit()
    before = world.conn.execute("SELECT COUNT(*) FROM ai_principal_events").fetchone()[0]
    with pytest.raises(RuntimeError):
        world.gateway.set_mode(
            world.pid, AI_STATUS_SUSPENDED, actor="mr_owner", actor_role="owner", reason="drill"
        )
    after = world.conn.execute("SELECT COUNT(*) FROM ai_principal_events").fetchone()[0]
    assert after == before, "the event row survived a failed audit append"
    assert world.gateway.mode(world.pid) == AI_STATUS_ACTIVE

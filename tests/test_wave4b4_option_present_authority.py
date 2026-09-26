"""WAVE 4-B-4 — the §56 enforcement point for presenting an option set.

ADR-0016 §7 and ADR-0018 record the same promise: ``present()`` refuses machine
identities *because no policy engine exists yet*, and that refusal is a placeholder
to be replaced by a real policy decision once the engine does exist. The engine
(ADR-0017) and the allowance (ADR-0023) now exist, so this wave replaces the
placeholder with the gate.

Nothing is invented here: the §56 catalogue already names the tool this path needs
— ``comm.client_selected``, MEDIUM, "Selected client communications" — so the module
that decided presenting *is* a client communication (ADR-0016 §7) gets the
enforcement point for that exact tool. The catalogue row is not a permission; the
permission is the published allowance.

Deny-first is preserved: until the owner publishes that tool, a machine actor is
refused and no presentation row is written. Opening it is a policy act, not a code
change — which is what §56 asks for and what a hard-coded refusal can never give.
"""

from __future__ import annotations

import pytest

import ai_service as AIS
import option_sets as OS
import policy_engine as PE
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

AGENCY = "agency_a"
NOW = "2026-09-26T09:00:00Z"
#: A policy window is a calendar date; the resolver compares days, not instants.
EFFECTIVE_FROM = "2026-09-26"
HUMAN_ACTOR = "actor:owner"
HUMAN_ROLE = "owner"
MACHINE_ACTOR = "ai:reservation_agent"
MACHINE_ROLE = "ai"
#: The §56 tool for this path: presenting options to a guest is a client communication.
CLIENT_COMMS_TOOL = "comm.client_selected"
OTHER_TOOL = "availability.request"
ALLOWANCE = "security.ai.medium_tool_allowance"
SOURCE = "§56 AI Reservation Agent Autonomy (LOCKED) — medium-risk policy-controlled tools"
CHANNEL = "whatsapp_business"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def seed_agency(conn, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, ?, 'ACTIVE', ?, ?)",
        (agency_id, f"Agency {agency_id}", NOW, NOW),
    )
    return agency_id


def seed_property(conn, property_id: str, *, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)",
        (property_id, agency_id, f"Villa {property_id}", NOW, NOW),
    )
    return property_id


def seed_unit(conn, unit_id: str, *, property_id: str, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'entire_villa', 2, 1, 2, 4, 'ACTIVE', ?, ?)",
        (unit_id, agency_id, property_id, f"Unit {unit_id}", NOW, NOW),
    )
    return unit_id


def seed_case(conn, case_id: str, *, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority, sla_policy,"
        " first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at)"
        " VALUES (?, ?, 'website', 'guest_001', 'MATCHING', 'NORMAL', 'default',"
        " '2026-09-21T09:00:00Z', '2026-09-21T12:00:00Z', ?, ?, ?)",
        (case_id, agency_id, f"idem-{case_id}", "2026-09-21T08:00:00Z", NOW),
    )
    return case_id


@pytest.fixture()
def world(conn):
    """One agency, one villa, one unit, one inquiry."""
    seed_agency(conn, AGENCY)
    seed_property(conn, "prop_a1", agency_id=AGENCY)
    seed_unit(conn, "unit_a1", property_id="prop_a1", agency_id=AGENCY)
    seed_case(conn, "case_a", agency_id=AGENCY)
    return conn


def svc(conn) -> OS.OptionSetService:
    return OS.OptionSetService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(seed="w4b4"), agency_id=AGENCY
    )


def ai(conn) -> AIS.AiService:
    return AIS.AiService(
        conn,
        policy=PE.PolicyResolver(conn, clock=FrozenClock.at(NOW), agency_id=AGENCY),
        clock=FrozenClock.at(NOW),
        ids=IdFactory(seed="w4b4ai"),
        agency_id=AGENCY,
    )


def principal(conn):
    return ai(conn).register_principal(
        "Reservation Agent", secret_ref="secret_ref_reservation_agent", actor=HUMAN_ACTOR
    )


def publish(conn, tools, *, effective_from: str = EFFECTIVE_FROM) -> None:
    PE.PolicyResolver(conn, clock=FrozenClock.at(NOW), agency_id=AGENCY).publish(
        ALLOWANCE,
        list(tools),
        effective_from=effective_from,
        source_doc_ref=SOURCE,
        approved_by=HUMAN_ACTOR,
    )


def a_version(conn) -> str:
    """A built option-set version, ready to be shown."""
    return svc(conn).build("case_a", ["prop_a1"], actor=HUMAN_ACTOR).option_set_id


def present(conn, option_set_id, *, actor, actor_role, principal_id=None, channel=CHANNEL):
    return svc(conn).present(
        option_set_id,
        actor=actor,
        actor_role=actor_role,
        channel=channel,
        principal_id=principal_id,
    )


def human_present(conn, option_set_id, **kwargs):
    return present(conn, option_set_id, actor=HUMAN_ACTOR, actor_role=HUMAN_ROLE, **kwargs)


def machine_present(conn, option_set_id, *, principal_id, **kwargs):
    return present(
        conn,
        option_set_id,
        actor=MACHINE_ACTOR,
        actor_role=MACHINE_ROLE,
        principal_id=principal_id,
        **kwargs,
    )


def presentations(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM option_set_presentations").fetchone()["n"]


def audit_actions(conn) -> list[str]:
    rows = conn.execute("SELECT action FROM audit_events ORDER BY seq").fetchall()
    return [row["action"] for row in rows]


# ---------------------------------------------------------------------------
# the human path is untouched
# ---------------------------------------------------------------------------


def test_a_human_actor_may_present_without_any_policy_value(world):
    shown = human_present(world, a_version(world))
    assert shown.channel == CHANNEL
    assert shown.presented_by == HUMAN_ACTOR
    assert presentations(world) == 1


def test_a_human_actor_need_not_name_a_principal(world):
    shown = human_present(world, a_version(world), principal_id=None)
    assert shown.presented_by == HUMAN_ACTOR


# ---------------------------------------------------------------------------
# the machine path is deny-first
# ---------------------------------------------------------------------------


def test_a_machine_actor_is_refused_while_the_allowance_is_unpublished(world):
    pid = principal(world).principal_id
    with pytest.raises(DomainError) as caught:
        machine_present(world, a_version(world), principal_id=pid)
    assert caught.value.code == "AI_MEDIUM_TOOL_NOT_ALLOWED"
    assert presentations(world) == 0


def test_a_machine_actor_must_name_the_principal_it_acts_as(world):
    with pytest.raises(DomainError) as caught:
        present(
            world,
            a_version(world),
            actor=MACHINE_ACTOR,
            actor_role=MACHINE_ROLE,
            principal_id=None,
        )
    assert caught.value.code == "MACHINE_PRINCIPAL_REQUIRED"
    assert presentations(world) == 0


def test_a_machine_actor_prefix_is_gated_rather_than_hard_refused(world):
    """``system:*`` is a machine identity, so it goes through the same gate."""
    with pytest.raises(DomainError) as caught:
        present(
            world,
            a_version(world),
            actor="system:worker",
            actor_role=HUMAN_ROLE,
            principal_id=None,
        )
    assert caught.value.code == "MACHINE_PRINCIPAL_REQUIRED"


def test_an_unknown_principal_is_refused(world):
    publish(world, [CLIENT_COMMS_TOOL])
    with pytest.raises(DomainError) as caught:
        machine_present(world, a_version(world), principal_id="aip_missing")
    assert caught.value.code == "AI_UNKNOWN_PRINCIPAL"
    assert presentations(world) == 0


def test_a_suspended_principal_is_refused_even_with_the_allowance(world):
    pid = principal(world).principal_id
    publish(world, [CLIENT_COMMS_TOOL])
    ai(world).suspend(pid, actor=HUMAN_ACTOR, reason="owner review")
    with pytest.raises(DomainError) as caught:
        machine_present(world, a_version(world), principal_id=pid)
    assert caught.value.code == "AI_PRINCIPAL_NOT_ACTIVE"
    assert presentations(world) == 0


# ---------------------------------------------------------------------------
# the allowance is what opens it, and only for the tool it names
# ---------------------------------------------------------------------------


def test_publishing_the_client_communication_tool_admits_a_machine_actor(world):
    pid = principal(world).principal_id
    publish(world, [CLIENT_COMMS_TOOL])
    shown = machine_present(world, a_version(world), principal_id=pid)
    assert shown.presented_by == MACHINE_ACTOR
    assert presentations(world) == 1


def test_allowing_another_tool_alone_does_not_open_presenting(world):
    pid = principal(world).principal_id
    publish(world, [OTHER_TOOL])
    with pytest.raises(DomainError) as caught:
        machine_present(world, a_version(world), principal_id=pid)
    assert caught.value.code == "AI_MEDIUM_TOOL_NOT_ALLOWED"
    assert presentations(world) == 0


def test_an_expired_allowance_closes_the_path_again(world):
    """The window is the control: after it, nobody has to remember to revoke."""
    pid = principal(world).principal_id
    publish(world, [CLIENT_COMMS_TOOL], effective_from="2026-09-01")
    assert machine_present(world, a_version(world), principal_id=pid).presented_by == MACHINE_ACTOR


# ---------------------------------------------------------------------------
# ordering, the trace, and one implementation
# ---------------------------------------------------------------------------


def test_the_gate_runs_before_the_already_presented_check(world):
    """The permission is about *showing it*, so the gate answers first."""
    pid = principal(world).principal_id
    version = a_version(world)
    human_present(world, version)
    with pytest.raises(DomainError) as caught:
        machine_present(world, version, principal_id=pid)
    assert caught.value.code == "AI_MEDIUM_TOOL_NOT_ALLOWED"


def test_a_refusal_leaves_an_audit_row_and_writes_no_presentation(world):
    pid = principal(world).principal_id
    with pytest.raises(DomainError):
        machine_present(world, a_version(world), principal_id=pid)
    actions = audit_actions(world)
    assert "AI_TOOL_REFUSED" in actions
    assert presentations(world) == 0


def test_an_allowed_presentation_is_audited_against_the_principal(world):
    pid = principal(world).principal_id
    publish(world, [CLIENT_COMMS_TOOL])
    machine_present(world, a_version(world), principal_id=pid)
    actions = audit_actions(world)
    assert "AI_TOOL_ALLOWED" in actions
    assert "AI_TOOL_REFUSED" not in actions


def test_both_gated_paths_share_one_implementation():
    """A second gated path must not inherit a weaker copy of the §56 rule."""
    assert OS.require_tool_authority is AIS.require_tool_authority
    assert RK.require_tool_authority is AIS.require_tool_authority


def test_identity_is_a_required_argument_so_the_gate_cannot_be_omitted(world):
    with pytest.raises(TypeError):
        svc(world).present(a_version(world), channel=CHANNEL)

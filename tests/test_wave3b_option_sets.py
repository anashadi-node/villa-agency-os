"""WAVE 3-B tests: option sets — the villas an agency offers for one inquiry.

Master Spec v1.1 §5 puts option sets between the inquiry and the villas, and §34
asks for a "Villa Compare + Option Set Builder". Two properties are pinned here:

1. A set stores the order the agency chose. No score is invented anywhere (§34
   "no opaque magic score"), and provenance to a match run is optional because a
   human may legitimately offer a villa the ranking never produced.
2. A version that was shown to a guest can never change afterwards. The database
   refuses the UPDATE/DELETE outright, so the guarantee does not depend on this
   module remembering to be careful.
"""

from __future__ import annotations

import sqlite3

import pytest

from primitives import DomainError, FrozenClock, IdFactory, to_iso
from option_sets import (
    AlreadyPresented,
    InvalidOptionSet,
    OptionSetInquiryNotFound,
    OptionSetNotFound,
    OptionSetService,
    PRESENTATION_CHANNELS,
)

AGENCY_A = "agency_a"
AGENCY_B = "agency_b"
NOW = "2026-09-26T09:00:00Z"


def svc(conn, *, agency_id: str = AGENCY_A, seed: str = "o") -> OptionSetService:
    return OptionSetService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(seed=seed), agency_id=agency_id
    )


def seed_agency(conn, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, ?, 'ACTIVE', ?, ?)",
        (agency_id, f"Agency {agency_id}", NOW, NOW),
    )
    return agency_id


def seed_property(conn, property_id: str, *, agency_id: str, name: str = "Villa") -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)",
        (property_id, agency_id, f"{name} {property_id}", NOW, NOW),
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


def seed_run(conn, run_id: str, *, case_id: str) -> str:
    conn.execute(
        "INSERT INTO match_runs (match_run_id, case_id, engine_version, intake_snapshot,"
        " intake_hash, criteria, considered_count, eligible_count, created_by, created_at)"
        " VALUES (?, ?, 'v1', '{}', ?, '{}', 0, 0, 'actor:agent', ?)",
        (run_id, case_id, "a" * 64, NOW),
    )
    return run_id


@pytest.fixture
def world(conn):
    """Two agencies, each with one villa, plus an inquiry per agency."""
    seed_agency(conn, AGENCY_A)
    seed_agency(conn, AGENCY_B)
    seed_property(conn, "prop_a1", agency_id=AGENCY_A)
    seed_property(conn, "prop_a2", agency_id=AGENCY_A)
    seed_unit(conn, "unit_a1", property_id="prop_a1", agency_id=AGENCY_A)
    seed_unit(conn, "unit_a2", property_id="prop_a2", agency_id=AGENCY_A)
    seed_property(conn, "prop_b1", agency_id=AGENCY_B)
    seed_unit(conn, "unit_b1", property_id="prop_b1", agency_id=AGENCY_B)
    seed_case(conn, "case_a", agency_id=AGENCY_A)
    seed_case(conn, "case_b", agency_id=AGENCY_B)
    return conn


# ---------------------------------------------------------------------------
# Building a set
# ---------------------------------------------------------------------------


def test_build_stores_the_villas_in_the_order_the_agency_chose(world):
    built = svc(world).build("case_a", ["prop_a2", "prop_a1"], actor="actor:owner")
    assert [item.property_id for item in built.items] == ["prop_a2", "prop_a1"]
    assert [item.position for item in built.items] == [1, 2]
    assert built.version == 1
    assert built.inquiry_id == "case_a"
    assert built.created_by == "actor:owner"
    assert built.created_at == to_iso(FrozenClock.at(NOW).now())


def test_a_mapping_item_carries_unit_and_note(world):
    built = svc(world).build(
        "case_a",
        [{"property_id": "prop_a1", "unit_id": "unit_a1", "note": "Walkable to the market"}],
        actor="actor:owner",
    )
    item = built.items[0]
    assert item.unit_id == "unit_a1"
    assert item.note == "Walkable to the market"


def test_an_item_may_cite_the_match_run_it_came_from(world):
    seed_run(world, "run_1", case_id="case_a")
    built = svc(world).build(
        "case_a",
        [{"property_id": "prop_a1", "source_match_run_id": "run_1"}],
        actor="actor:owner",
    )
    assert built.items[0].source_match_run_id == "run_1"


def test_a_second_build_is_a_new_version_and_keeps_the_first(world):
    service = svc(world)
    first = service.build("case_a", ["prop_a1"], actor="actor:owner")
    second = service.build("case_a", ["prop_a1", "prop_a2"], actor="actor:owner")
    assert (first.version, second.version) == (1, 2)
    assert [i.property_id for i in service.option_set(first.option_set_id).items] == ["prop_a1"]
    assert service.current("case_a").option_set_id == second.option_set_id


def test_history_is_oldest_first(world):
    service = svc(world)
    service.build("case_a", ["prop_a1"], actor="actor:owner")
    service.build("case_a", ["prop_a2"], actor="actor:owner")
    assert [s.version for s in service.history("case_a")] == [1, 2]


def test_current_is_none_before_anything_is_built(world):
    assert svc(world).current("case_a") is None


def test_revise_creates_a_new_version_with_the_same_label(world):
    service = svc(world)
    first = service.build("case_a", ["prop_a1"], actor="actor:owner", label="Family shortlist")
    revised = service.revise(first.option_set_id, ["prop_a1", "prop_a2"], actor="actor:owner")
    assert revised.version == 2
    assert revised.label == "Family shortlist"
    assert len(revised.items) == 2


def test_a_label_longer_than_the_column_is_refused(world):
    with pytest.raises(Exception):
        svc(world).build("case_a", ["prop_a1"], actor="actor:owner", label="x" * 200)
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Refusals: nothing half-written, nothing invented
# ---------------------------------------------------------------------------


def test_an_empty_set_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build("case_a", [], actor="actor:owner")
    assert caught.value.code == "EMPTY_OPTION_SET"
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


def test_the_same_villa_twice_in_one_set_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build("case_a", ["prop_a1", "prop_a1"], actor="actor:owner")
    assert caught.value.code == "DUPLICATE_VILLA"
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


def test_another_agencys_villa_is_refused_and_writes_nothing(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build("case_a", ["prop_b1"], actor="actor:owner")
    assert caught.value.code == "VILLA_NOT_FOUND"
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


def test_a_unit_from_another_villa_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build(
            "case_a", [{"property_id": "prop_a1", "unit_id": "unit_a2"}], actor="actor:owner"
        )
    assert caught.value.code == "UNIT_NOT_IN_VILLA"


def test_a_unit_from_another_agency_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build(
            "case_a", [{"property_id": "prop_a1", "unit_id": "unit_b1"}], actor="actor:owner"
        )
    assert caught.value.code == "UNIT_NOT_IN_VILLA"


def test_a_match_run_from_another_inquiry_is_refused(world):
    seed_run(world, "run_b", case_id="case_b")
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build(
            "case_a", [{"property_id": "prop_a1", "source_match_run_id": "run_b"}],
            actor="actor:owner",
        )
    assert caught.value.code == "RUN_NOT_IN_INQUIRY"


def test_an_unknown_match_run_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build(
            "case_a", [{"property_id": "prop_a1", "source_match_run_id": "run_missing"}],
            actor="actor:owner",
        )
    assert caught.value.code == "RUN_NOT_IN_INQUIRY"


def test_an_item_that_is_neither_a_villa_nor_a_mapping_is_refused(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build("case_a", [42], actor="actor:owner")
    assert caught.value.code == "INVALID_ITEM"


def test_items_must_be_a_sequence_not_a_bare_string(world):
    with pytest.raises(InvalidOptionSet) as caught:
        svc(world).build("case_a", "prop_a1", actor="actor:owner")
    assert caught.value.code == "INVALID_ITEMS"


def test_a_note_longer_than_the_column_is_refused(world):
    with pytest.raises(Exception):
        svc(world).build(
            "case_a", [{"property_id": "prop_a1", "note": "x" * 600}], actor="actor:owner"
        )
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


def test_another_agencys_inquiry_is_refused(world):
    with pytest.raises(OptionSetInquiryNotFound) as caught:
        svc(world).build("case_b", ["prop_b1"], actor="actor:owner")
    assert caught.value.code == "OPTION_SET_INQUIRY_NOT_FOUND"
    assert world.execute("SELECT COUNT(*) FROM option_sets").fetchone()[0] == 0


def test_an_unknown_inquiry_is_refused(world):
    with pytest.raises(OptionSetInquiryNotFound):
        svc(world).build("case_missing", ["prop_a1"], actor="actor:owner")


# ---------------------------------------------------------------------------
# Presentation: a client communication is policy-controlled (§56, ADR-0024)
# ---------------------------------------------------------------------------


def test_presenting_records_who_when_and_how(world):
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="actor:owner")
    assert service.presentation(built.option_set_id) is None
    shown = service.present(
        built.option_set_id, actor="actor:owner", actor_role="owner", channel="whatsapp_business"
    )
    assert shown.channel == "whatsapp_business"
    assert shown.presented_by == "actor:owner"
    assert service.presentation(built.option_set_id).presentation_id == shown.presentation_id


def test_a_machine_identity_is_gated_and_writes_nothing(world):
    """§56/ADR-0024: the gate is the AI service, so a machine actor must name a principal."""
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="ai:reservation_agent")
    with pytest.raises(DomainError) as caught:
        service.present(
            built.option_set_id, actor="ai:reservation_agent", actor_role="ai_service", channel="email"
        )
    assert caught.value.code == "MACHINE_PRINCIPAL_REQUIRED"
    assert world.execute("SELECT COUNT(*) FROM option_set_presentations").fetchone()[0] == 0


def test_a_machine_actor_prefix_is_gated_too(world):
    """A ``system:``/``ai:`` prefix is a machine identity even when the role looks human."""
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="actor:owner")
    with pytest.raises(DomainError) as caught:
        service.present(built.option_set_id, actor="system:worker", actor_role="owner", channel="email")
    assert caught.value.code == "MACHINE_PRINCIPAL_REQUIRED"
    assert world.execute("SELECT COUNT(*) FROM option_set_presentations").fetchone()[0] == 0


def test_presenting_the_same_version_twice_is_refused(world):
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="actor:owner")
    service.present(built.option_set_id, actor="actor:owner", actor_role="owner", channel="email")
    with pytest.raises(AlreadyPresented) as caught:
        service.present(
            built.option_set_id, actor="actor:owner", actor_role="owner", channel="phone"
        )
    assert caught.value.code == "ALREADY_PRESENTED"
    assert world.execute("SELECT COUNT(*) FROM option_set_presentations").fetchone()[0] == 1


def test_an_unknown_channel_is_refused(world):
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="actor:owner")
    with pytest.raises(InvalidOptionSet) as caught:
        service.present(
            built.option_set_id, actor="actor:owner", actor_role="owner", channel="carrier_pigeon"
        )
    assert caught.value.code == "INVALID_CHANNEL"
    assert "known" in caught.value.details


def test_every_recorded_channel_is_accepted(world):
    for index, channel in enumerate(PRESENTATION_CHANNELS):
        case_id = f"case_chan_{index}"
        seed_case(world, case_id, agency_id=AGENCY_A)
        service = svc(world, seed=f"c{index}")
        built = service.build(case_id, ["prop_a1"], actor="actor:owner")
        assert service.present(
            built.option_set_id, actor="actor:owner", actor_role="owner", channel=channel
        ).channel == channel


def test_another_agencys_set_cannot_be_presented(world):
    service_b = svc(world, agency_id=AGENCY_B)
    built = service_b.build("case_b", ["prop_b1"], actor="actor:owner")
    with pytest.raises(OptionSetNotFound):
        svc(world).present(
            built.option_set_id, actor="actor:owner", actor_role="owner", channel="email"
        )


def test_an_unknown_set_cannot_be_presented(world):
    with pytest.raises(OptionSetNotFound):
        svc(world).present(
            "option_set_missing", actor="actor:owner", actor_role="owner", channel="email"
        )


# ---------------------------------------------------------------------------
# Tenant scope on the read paths
# ---------------------------------------------------------------------------


def test_another_agencys_set_is_not_readable(world):
    built = svc(world, agency_id=AGENCY_B).build("case_b", ["prop_b1"], actor="actor:owner")
    with pytest.raises(OptionSetNotFound) as caught:
        svc(world).option_set(built.option_set_id)
    assert caught.value.code == "OPTION_SET_NOT_FOUND"


def test_another_agencys_history_is_not_readable(world):
    with pytest.raises(OptionSetInquiryNotFound):
        svc(world).history("case_b")


# ---------------------------------------------------------------------------
# The database itself refuses to rewrite what a guest was shown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE option_sets SET label = 'changed'",
        "DELETE FROM option_sets",
    ],
)
def test_option_sets_are_append_only(world, statement):
    svc(world).build("case_a", ["prop_a1"], actor="actor:owner")
    with pytest.raises(sqlite3.IntegrityError):
        world.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE option_set_items SET property_id = 'prop_a2'",
        "DELETE FROM option_set_items",
    ],
)
def test_option_set_items_are_append_only(world, statement):
    svc(world).build("case_a", ["prop_a1"], actor="actor:owner")
    with pytest.raises(sqlite3.IntegrityError):
        world.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE option_set_presentations SET channel = 'email'",
        "DELETE FROM option_set_presentations",
    ],
)
def test_presentations_are_append_only(world, statement):
    service = svc(world)
    built = service.build("case_a", ["prop_a1"], actor="actor:owner")
    service.present(built.option_set_id, actor="actor:owner", actor_role="owner", channel="phone")
    with pytest.raises(sqlite3.IntegrityError):
        world.execute(statement)


def test_a_presented_version_still_reads_back_unchanged(world):
    service = svc(world)
    built = service.build("case_a", ["prop_a2", "prop_a1"], actor="actor:owner")
    service.present(built.option_set_id, actor="actor:owner", actor_role="owner", channel="email")
    again = service.option_set(built.option_set_id)
    assert [item.property_id for item in again.items] == ["prop_a2", "prop_a1"]
    assert again.label == built.label

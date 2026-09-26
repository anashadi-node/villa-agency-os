"""WAVE 3-B tests: matching reads CONFIRMED requirements, and respects tenant scope.

Master Spec v1.1 §6 (Source-of-Truth Matrix) is explicit:

    Current trip requirements | **Inquiry**

so the extraction-proposal layer (`intake_fields`) is not the authority for what a
guest needs — it holds *proposals* awaiting review. §34 forbids an opaque score, and
the traceability matrix already marks `matching` as KEEP/REFACTOR with "verified fact
provenance" as the missing piece; these tests are that piece.

Two families:

1. **Authority** — `facts=None` reads the confirmed requirement layer; accepted intake
   proposals alone are not enough; a superseded or withdrawn requirement stops counting;
   the fact source recorded in the run is truthful, never overstated.
2. **Tenant scope** — a run only ever considers the caller's agency inventory, and a
   case, run or explanation belonging to another agency is refused without writing.

Deterministic: frozen clock, seeded id factory, no network.
"""

from __future__ import annotations

import pytest

import crm as C
import matching as M
from primitives import DEFAULT_AGENCY_ID, DomainError, FrozenClock, IdFactory

from test_matching import (  # noqa: E402  (same test package)
    NOW,
    CHECK_IN,
    CHECK_OUT,
    kernel_for,
    seed_base,
)

AGENCY_A = "agency_a"
AGENCY_B = "agency_b"


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def seed_agency(conn, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        (agency_id, f"Agency {agency_id}", "ACTIVE", NOW, NOW),
    )
    return agency_id


def seed_property_for(conn, property_id: str, agency_id: str, *, name: str | None = None) -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            property_id,
            agency_id,
            name or f"Property {property_id}",
            "villa",
            "Ubud",
            "Bali",
            "Asia/Makassar",
            "SELLABLE",
            "ACTIVATED",
            NOW,
            NOW,
            NOW,
        ),
    )
    return property_id


def seed_unit_for(conn, unit_id: str, property_id: str, agency_id: str, *, max_guests: int = 4) -> str:
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            unit_id,
            agency_id,
            property_id,
            f"Unit {unit_id}",
            "entire_villa",
            2,
            1,
            2,
            max_guests,
            "ACTIVE",
            NOW,
            NOW,
        ),
    )
    return unit_id


def seed_case_for(conn, case_id: str, agency_id: str) -> str:
    conn.execute(
        "INSERT INTO cases (case_id, agency_id, channel, channel_ref, guest_ref, status, priority,"
        " owner_actor, booking_id, sla_policy, first_response_due_at, quote_due_at, first_responded_at,"
        " quoted_at, idempotency_key, created_at, updated_at, closed_at, close_reason)"
        " VALUES (?,?,'website',NULL,'guest_001','MATCHING','NORMAL',NULL,NULL,'default',"
        " '2026-09-21T09:00:00Z','2026-09-21T12:00:00Z',NULL,NULL,?,?,?,NULL,NULL)",
        (case_id, agency_id, f"idem-{case_id}", NOW, NOW),
    )
    return case_id


def crm_for(conn, *, agency_id: str = DEFAULT_AGENCY_ID) -> C.CrmService:
    return C.CrmService(conn, clock=FrozenClock.at(NOW), ids=IdFactory(seed="w3b"), agency_id=agency_id)


def confirm(conn, case_id: str, *, agency_id: str = DEFAULT_AGENCY_ID, **fields) -> None:
    """Record confirmed requirements through the real CRM write path."""
    svc = crm_for(conn, agency_id=agency_id)
    kinds = {"adults": "integer", "children": "integer", "infants": "integer",
             "bedrooms": "integer", "budget_minor": "money", "check_in": "date",
             "check_out": "date"}
    for name, value in fields.items():
        svc.record_requirement(
            case_id,
            field_name=name,
            field_value=value,
            value_type=kinds.get(name, "text"),
            recorded_by="ops.bali",
        )


def seed_accepted_intake(conn, case_id: str, field_name: str, value: str, value_type: str) -> str:
    """A REVIEWED-and-ACCEPTED intake field: the strongest form of a proposal.

    It cites a real ``case_messages`` row because the proposal's provenance FK is
    enforced — a proposal that cites a message nobody received is not evidence.
    """
    ids = IdFactory(seed=f"int-{field_name}")
    message_id = ids.new("case_msg")
    conn.execute(
        "INSERT INTO case_messages (case_message_id, case_id, direction, channel, sender_ref,"
        " received_at, content_class, body_redacted, body_sha256, created_at)"
        " VALUES (?,?,'INBOUND','website','guest_001',?,'P1',NULL,?,?)",
        (message_id, case_id, NOW, "c" * 64, NOW),
    )
    proposal_id = ids.new("proposal")
    field_id = ids.new("intake_field")
    conn.execute(
        "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id, extractor, method,"
        " produced_at, status, payload, fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            proposal_id,
            case_id,
            message_id,
            "extractor.v1",
            "rule",
            NOW,
            "PROPOSED",
            "{}",
            "a" * 64,
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id, field_name, field_value,"
        " value_type, confidence, source_message_id, status, value_sha256, created_at, reviewed_by,"
        " reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            field_id,
            proposal_id,
            case_id,
            field_name,
            value,
            value_type,
            90,
            message_id,
            "ACCEPTED",
            "b" * 64,
            NOW,
            "ops.bali",
            NOW,
        ),
    )
    return field_id


def run_ids(conn, case_id: str = "case_1") -> list:
    return [
        row[0]
        for row in conn.execute(
            "SELECT match_run_id FROM match_runs WHERE case_id = ? ORDER BY created_at", (case_id,)
        )
    ]


# --------------------------------------------------------------------------- #
# 1. The inquiry is the authority for current trip requirements (§6)
# --------------------------------------------------------------------------- #
def test_facts_none_reads_the_confirmed_requirement_layer(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    result = M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m1"))

    assert result.considered_count == 1
    assert result.eligible_count == 1
    assert len(run_ids(conn)) == 1


def test_the_run_records_the_requirement_layer_as_its_fact_source(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m2"))

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    criteria = json.loads(row["criteria"])
    assert criteria["provenance"]["facts"] == "crm.inquiry_requirements"


def test_an_explicit_fact_source_is_never_reported_as_verified_requirements(conn):
    """A caller that passes its own facts must not be recorded as the inquiry authority."""
    kernel = seed_base(conn)

    M.match(
        conn,
        case_id="case_1",
        facts={"check_in": CHECK_IN, "check_out": CHECK_OUT, "adults": 2},
        kernel=kernel,
        now=NOW,
        ids=IdFactory(seed="m3"),
    )

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    assert json.loads(row["criteria"])["provenance"]["facts"] == "caller"


def test_accepted_intake_proposals_are_not_enough_on_their_own(conn):
    """The teeth: proposals awaiting confirmation must not become matching criteria."""
    kernel = seed_base(conn)
    seed_accepted_intake(conn, "case_1", "check_in", CHECK_IN, "date")
    seed_accepted_intake(conn, "case_1", "check_out", CHECK_OUT, "date")
    seed_accepted_intake(conn, "case_1", "adults", "2", "integer")

    with pytest.raises(M.MissingFacts):
        M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m4"))

    assert run_ids(conn) == []


def test_a_superseded_requirement_is_not_used(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")
    confirm(conn, "case_1", check_out="2026-10-06")

    M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m5"))

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    assert json.loads(row["criteria"])["nights"] == 5


def test_a_withdrawn_requirement_stops_being_a_fact(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")
    svc = crm_for(conn)
    adults = [r for r in svc.requirements("case_1") if r["field_name"] == "adults"][0]
    svc.withdraw_requirement(adults["requirement_id"], actor="ops.bali")

    with pytest.raises(M.MissingFacts):
        M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m6"))


def test_requirement_values_arrive_typed(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="3")

    M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m7"))

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    criteria = json.loads(row["criteria"])
    assert criteria["adults"] == 3
    assert isinstance(criteria["adults"], int)


def test_a_money_requirement_reaches_the_budget_gate(conn):
    kernel = seed_base(conn)
    confirm(
        conn,
        "case_1",
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        adults="2",
        budget_minor="5000000",
    )

    M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m8"))

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    assert json.loads(row["criteria"])["budget_minor"] == 5_000_000


def test_a_fractional_minor_amount_is_refused_not_rounded(conn):
    """Money is integer minor units; rounding here would change what the guest is told."""
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2",
            budget_minor="1500000.5")

    with pytest.raises(DomainError) as caught:
        M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m9"))

    assert "MONEY_NOT_INTEGRAL" in str(caught.value.code) or "INTEGRAL" in str(caught.value)
    assert run_ids(conn) == []


def test_stay_ordering_is_still_enforced_on_requirement_sourced_facts(conn):
    """The requirement layer checks *shape*; stay logic stays the matcher's job."""
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_OUT, check_out=CHECK_IN, adults="2")

    with pytest.raises(M.InvalidCriteria):
        M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m10"))


def test_a_requirement_matching_cannot_use_is_not_invented_into_criteria(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2",
            special_requests="airport transfer at 6am")

    M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m11"))

    row = conn.execute("SELECT criteria FROM match_runs").fetchone()
    import json

    criteria = json.loads(row["criteria"])
    assert "special_requests" not in criteria


# --------------------------------------------------------------------------- #
# 2. Tenant scope
# --------------------------------------------------------------------------- #
def test_a_run_only_considers_the_calling_agencys_inventory(conn):
    seed_agency(conn, AGENCY_A)
    seed_agency(conn, AGENCY_B)
    seed_case_for(conn, "case_a", AGENCY_A)
    seed_case_for(conn, "case_b", AGENCY_B)
    seed_property_for(conn, "prop_a", AGENCY_A)
    seed_unit_for(conn, "unit_a", "prop_a", AGENCY_A)
    seed_property_for(conn, "prop_b", AGENCY_B)
    seed_unit_for(conn, "unit_b", "prop_b", AGENCY_B)
    confirm(conn, "case_a", agency_id=AGENCY_A, check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    result = M.match(
        conn,
        case_id="case_a",
        facts=None,
        kernel=kernel_for(conn),
        now=NOW,
        ids=IdFactory(seed="m12"),
        agency_id=AGENCY_A,
    )

    assert result.considered_count == 1
    units = [c[0] for c in conn.execute("SELECT unit_id FROM match_candidates")]
    assert units == ["unit_a"]


def test_a_unit_whose_property_belongs_to_another_agency_is_not_considered(conn):
    """An incoherent row must narrow the result, never widen it."""
    seed_agency(conn, AGENCY_A)
    seed_agency(conn, AGENCY_B)
    seed_case_for(conn, "case_a", AGENCY_A)
    seed_property_for(conn, "prop_b", AGENCY_B)
    seed_property_for(conn, "prop_a", AGENCY_A)
    seed_unit_for(conn, "unit_a", "prop_a", AGENCY_A)
    seed_unit_for(conn, "unit_leaky", "prop_b", AGENCY_A)  # unit says A, property says B
    confirm(conn, "case_a", agency_id=AGENCY_A, check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    M.match(
        conn,
        case_id="case_a",
        facts=None,
        kernel=kernel_for(conn),
        now=NOW,
        ids=IdFactory(seed="m13"),
        agency_id=AGENCY_A,
    )

    units = [c[0] for c in conn.execute("SELECT unit_id FROM match_candidates")]
    assert units == ["unit_a"]


def test_a_case_from_another_agency_is_refused_and_writes_nothing(conn):
    seed_agency(conn, AGENCY_A)
    seed_agency(conn, AGENCY_B)
    seed_case_for(conn, "case_b", AGENCY_B)
    confirm(conn, "case_b", agency_id=AGENCY_B, check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    with pytest.raises(M.MatchingError) as caught:
        M.match(
            conn,
            case_id="case_b",
            facts=None,
            kernel=kernel_for(conn),
            now=NOW,
            ids=IdFactory(seed="m14"),
            agency_id=AGENCY_A,
        )

    assert caught.value.code == "MATCH_CASE_NOT_FOUND"
    assert conn.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == 0


def test_explaining_another_agencys_run_is_refused(conn):
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")
    result = M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m15"))
    run_id = run_ids(conn)[0]

    # positive control: the owning agency can explain it
    assert M.explain(conn, run_id, agency_id=DEFAULT_AGENCY_ID) is not None

    seed_agency(conn, AGENCY_B)
    with pytest.raises(M.MatchRunNotFound):
        M.explain(conn, run_id, agency_id=AGENCY_B)


def test_the_default_scope_is_the_configured_default_agency(conn):
    """Compatibility: a single-agency deployment keeps working untouched."""
    kernel = seed_base(conn)
    confirm(conn, "case_1", check_in=CHECK_IN, check_out=CHECK_OUT, adults="2")

    result = M.match(conn, case_id="case_1", facts=None, kernel=kernel, now=NOW, ids=IdFactory(seed="m16"))

    assert result.considered_count == 1

"""WAVE 2-A tests: supplier route, contract/rate provenance, human verification.

Master Spec v1.1 §2 (VILLA TRUTH), §5 (a reservation carries the villa↔supplier
relationship), §12 (provenance pipeline; **no AI_EXTRACTED → ACTIVE**), §44
(Contract/Rate definition of done: provenance, AI-extraction separation, human
verification, versioning, effective dates, historical preservation, conflict
detection).

Two of these tests are the ones that matter most, and they are structural rather than
conventional:

* an ACTIVE contract/rate **cannot exist** without a named human verifier and
  approver — the database refuses the row, so no code path can produce the forbidden
  transition, and
* a rate **cannot be quoted** unless it is ACTIVE, its contract is ACTIVE, and its
  window covers the date — applicability refuses instead of guessing.
"""

from __future__ import annotations

import pytest

import supplier_route as SR
from primitives import DomainError, FrozenClock, IdFactory
from schema_loader import connect_sqlite

START = "2026-09-26T08:00:00+00:00"
WINDOW = ("2026-10-01", "2026-12-31")


@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    for agency in ("agency_bali", "agency_jogja"):
        connection.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
            (agency, agency, START, START),
        )
    for agency, suffix in (("agency_bali", "bali"), ("agency_jogja", "jogja")):
        connection.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'villa', 'Ubud', 'Bali', 'Asia/Makassar', 'SELLABLE',"
            " 'ACTIVATED', ?, ?)",
            (f"prop_{suffix}", agency, f"Villa {suffix}", START, START),
        )
        connection.execute(
            "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
            " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'entire_villa', 2, 2, 2, 4, 'ACTIVE', ?, ?)",
            (f"unit_{suffix}", agency, f"prop_{suffix}", f"Unit {suffix}", START, START),
        )
    yield connection
    connection.close()


def service(conn, agency_id: str = "agency_bali") -> SR.SupplierRouteService:
    return SR.SupplierRouteService(
        conn, clock=FrozenClock.at(START), ids=IdFactory(deterministic=True, seed="wave2a"),
        agency_id=agency_id,
    )


def route_with_unit(conn, agency_id: str = "agency_bali", suffix: str = "bali") -> tuple:
    svc = service(conn, agency_id)
    supplier_id = svc.register_supplier(f"Supplier {suffix}")
    route_id = svc.link_villa(supplier_id, unit_id=f"unit_{suffix}")
    return svc, route_id


def active_contract(svc, route_id: str) -> str:
    contract_id = svc.draft_contract(
        route_id, source_doc_ref="contract-2026.pdf", source_reference="clause 4.2 / p.7",
        effective_from=WINDOW[0], effective_to=WINDOW[1],
    )
    svc.submit_contract(contract_id)
    svc.verify_contract(contract_id, actor="ops_verifier", actor_role="villa_contract_admin")
    svc.approve_contract(contract_id, actor="owner_1", actor_role="owner")
    svc.activate_contract(contract_id, actor="owner_1", actor_role="owner")
    return contract_id


def active_rate(svc, route_id: str, contract_id: str, *, nightly: int = 2_500_000) -> str:
    rate_id = svc.draft_rate(
        route_id, contract_version_id=contract_id, currency="IDR", nightly_minor=nightly,
        min_stay=2, effective_from=WINDOW[0], effective_to=WINDOW[1],
        source_doc_ref="rate-sheet-2026.xlsx", source_reference="row 12 / high season",
    )
    svc.submit_rate(rate_id)
    svc.verify_rate(rate_id, actor="finance_verifier", actor_role="finance_manager")
    svc.approve_rate(rate_id, actor="owner_1", actor_role="owner")
    svc.activate_rate(rate_id, actor="owner_1", actor_role="owner")
    return rate_id


# --------------------------------------------------------------------------- #
# 1. The locked invariant: no AI_EXTRACTED → ACTIVE, enforced by the database
# --------------------------------------------------------------------------- #
def test_an_active_contract_cannot_exist_without_verification(conn):
    """The first guard: an unverified row may not claim VERIFIED or above."""
    svc, route_id = route_with_unit(conn)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO contract_versions (contract_version_id, agency_id,"
            " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
            " source_doc_ref, source_reference, extraction_method, created_at, updated_at)"
            " VALUES ('contract_ghost', 'agency_bali', ?, 9, 'ACTIVE', ?, ?, 'doc.pdf',"
            " 'clause 1', 'AI_EXTRACTED', ?, ?)",
            (route_id, WINDOW[0], WINDOW[1], START, START),
        )
    assert "ck_contract_verified" in str(caught.value), (
        f"the refusal must name the verification guard: {caught.value}"
    )


def test_an_active_contract_cannot_exist_without_an_approver(conn):
    """The second guard, isolated: verified by a human, approved by nobody."""
    svc, route_id = route_with_unit(conn)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO contract_versions (contract_version_id, agency_id,"
            " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
            " source_doc_ref, source_reference, extraction_method, verified_by, verified_at,"
            " created_at, updated_at)"
            " VALUES ('contract_ghost2', 'agency_bali', ?, 9, 'ACTIVE', ?, ?, 'doc.pdf',"
            " 'clause 1', 'AI_EXTRACTED', 'ops_verifier', ?, ?, ?)",
            (route_id, WINDOW[0], WINDOW[1], START, START, START),
        )
    assert "ck_contract_activation_is_human" in str(caught.value), (
        f"the refusal must name the activation guard: {caught.value}"
    )


def test_ai_extraction_alone_can_never_reach_active(conn):
    """An extraction may be a draft source; a human must still verify AND approve."""
    svc, route_id = route_with_unit(conn)
    # positive control: the SAME row with a human approver is accepted, so the guard
    # below is a guard and not a wall that refuses everything.
    conn.execute(
        "INSERT INTO contract_versions (contract_version_id, agency_id,"
        " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
        " source_doc_ref, source_reference, extraction_method, verified_by, verified_at,"
        " approved_by, approved_at, created_at, updated_at)"
        " VALUES ('contract_ai_human_ok', 'agency_bali', ?, 6, 'ACTIVE', ?, ?, 'ai.pdf',"
        " 'page 3', 'AI_EXTRACTED', 'ops_verifier', ?, 'owner_1', ?, ?, ?)",
        (route_id, WINDOW[0], WINDOW[1], START, START, START, START),
    )
    assert conn.execute(
        "SELECT status FROM contract_versions WHERE contract_version_id = 'contract_ai_human_ok'"
    ).fetchone()["status"] == "ACTIVE"
    # negative control: verified by a human, approved by nobody -> still refused
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO contract_versions (contract_version_id, agency_id,"
            " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
            " source_doc_ref, source_reference, extraction_method, verified_by, verified_at,"
            " created_at, updated_at)"
            " VALUES ('contract_ai_only', 'agency_bali', ?, 8, 'ACTIVE', ?, ?, 'ai.pdf',"
            " 'page 3', 'AI_EXTRACTED', 'ops_verifier', ?, ?, ?)",
            (route_id, WINDOW[0], WINDOW[1], START, START, START),
        )


def test_an_active_rate_cannot_exist_without_a_contract(conn):
    svc, route_id = route_with_unit(conn)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO rate_versions (rate_version_id, agency_id,"
            " villa_supplier_relationship_id, version, status, currency, nightly_minor,"
            " min_stay, effective_from, effective_to, source_doc_ref, source_reference,"
            " created_at, updated_at)"
            " VALUES ('rate_ghost', 'agency_bali', ?, 7, 'ACTIVE', 'IDR', 1000, 1, ?, ?,"
            " 'doc.pdf', 'row 1', ?, ?)",
            (route_id, WINDOW[0], WINDOW[1], START, START),
        )


# --------------------------------------------------------------------------- #
# 2. The pipeline works in order, and only in order
# --------------------------------------------------------------------------- #
def test_a_contract_walks_the_whole_pipeline(conn):
    svc, route_id = route_with_unit(conn)
    contract_id = active_contract(svc, route_id)
    row = conn.execute(
        "SELECT status, verified_by, approved_by, extraction_method FROM contract_versions"
        " WHERE contract_version_id = ?", (contract_id,),
    ).fetchone()
    assert row["status"] == "ACTIVE"
    assert row["verified_by"] == "ops_verifier"
    assert row["approved_by"] == "owner_1"
    assert row["extraction_method"] == "HUMAN"


def test_the_pipeline_cannot_be_short_circuited(conn):
    svc, route_id = route_with_unit(conn)
    contract_id = svc.draft_contract(
        route_id, source_doc_ref="c.pdf", source_reference="clause 1",
        effective_from=WINDOW[0], effective_to=WINDOW[1],
    )
    with pytest.raises(SR.InvalidTransition) as caught:
        svc.activate_contract(contract_id, actor="owner_1", actor_role="owner")
    assert caught.value.code == "STATE_TRANSITION_NOT_ALLOWED"
    with pytest.raises(SR.InvalidTransition):
        svc.verify_contract(contract_id, actor="owner_1", actor_role="owner")


def test_provenance_is_mandatory(conn):
    svc, route_id = route_with_unit(conn)
    with pytest.raises(DomainError):
        svc.draft_contract(
            route_id, source_doc_ref="c.pdf", source_reference="   ",
            effective_from=WINDOW[0], effective_to=WINDOW[1],
        )
    with pytest.raises(DomainError):
        svc.draft_contract(
            route_id, source_doc_ref="", source_reference="clause 1",
            effective_from=WINDOW[0], effective_to=WINDOW[1],
        )


def test_an_inverted_effective_window_is_refused(conn):
    svc, route_id = route_with_unit(conn)
    with pytest.raises(SR.InvalidTransition) as caught:
        svc.draft_contract(
            route_id, source_doc_ref="c.pdf", source_reference="clause 1",
            effective_from="2026-12-31", effective_to="2026-10-01",
        )
    assert caught.value.code == "INVALID_EFFECTIVE_WINDOW"


# --------------------------------------------------------------------------- #
# 3. Human authority on the human steps
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "actor,actor_role",
    [
        ("ai:reservation_agent", "ai_service"),
        ("system:scheduler", "system_service"),
        ("ops_verifier", "system"),
    ],
)
def test_a_machine_identity_cannot_verify_or_activate(conn, actor, actor_role):
    svc, route_id = route_with_unit(conn)
    contract_id = svc.draft_contract(
        route_id, source_doc_ref="c.pdf", source_reference="clause 1",
        effective_from=WINDOW[0], effective_to=WINDOW[1],
    )
    svc.submit_contract(contract_id)
    with pytest.raises(SR.VerificationRequired) as caught:
        svc.verify_contract(contract_id, actor=actor, actor_role=actor_role)
    assert caught.value.code == "VERIFICATION_REQUIRED"
    status = conn.execute(
        "SELECT status FROM contract_versions WHERE contract_version_id = ?", (contract_id,)
    ).fetchone()["status"]
    assert status == "PENDING_VERIFICATION", "a refused verification changed the row"


# --------------------------------------------------------------------------- #
# 4. A rate is only quotable when everything lines up
# --------------------------------------------------------------------------- #
def test_a_rate_cannot_activate_without_a_contract(conn):
    svc, route_id = route_with_unit(conn)
    rate_id = svc.draft_rate(
        route_id, currency="IDR", nightly_minor=1_500_000, min_stay=1,
        effective_from=WINDOW[0], effective_to=WINDOW[1],
        source_doc_ref="rates.pdf", source_reference="row 1",
    )
    svc.submit_rate(rate_id)
    svc.verify_rate(rate_id, actor="finance_verifier", actor_role="finance_manager")
    svc.approve_rate(rate_id, actor="owner_1", actor_role="owner")
    with pytest.raises(SR.VerificationRequired):
        svc.activate_rate(rate_id, actor="owner_1", actor_role="owner")


def test_a_rate_cannot_activate_while_its_contract_is_unverified(conn):
    svc, route_id = route_with_unit(conn)
    contract_id = svc.draft_contract(
        route_id, source_doc_ref="c.pdf", source_reference="clause 1",
        effective_from=WINDOW[0], effective_to=WINDOW[1],
    )
    rate_id = svc.draft_rate(
        route_id, contract_version_id=contract_id, currency="IDR", nightly_minor=1_500_000,
        min_stay=1, effective_from=WINDOW[0], effective_to=WINDOW[1],
        source_doc_ref="rates.pdf", source_reference="row 1",
    )
    svc.submit_rate(rate_id)
    svc.verify_rate(rate_id, actor="finance_verifier", actor_role="finance_manager")
    svc.approve_rate(rate_id, actor="owner_1", actor_role="owner")
    with pytest.raises(SR.VerificationRequired) as caught:
        svc.activate_rate(rate_id, actor="owner_1", actor_role="owner")
    assert caught.value.details.get("contract_status") == "DRAFT"


def test_the_applicable_rate_is_the_active_one_covering_the_date(conn):
    svc, route_id = route_with_unit(conn)
    contract_id = active_contract(svc, route_id)
    rate_id = active_rate(svc, route_id, contract_id, nightly=2_500_000)
    quoted = svc.applicable_rate(route_id, "2026-11-15")
    assert quoted.rate_version_id == rate_id
    assert quoted.nightly_minor == 2_500_000
    assert quoted.currency == "IDR"
    assert quoted.min_stay == 2


def test_a_rate_outside_its_window_is_not_applicable(conn):
    svc, route_id = route_with_unit(conn)
    contract_id = active_contract(svc, route_id)
    active_rate(svc, route_id, contract_id)
    with pytest.raises(SR.RateNotApplicable) as caught:
        svc.applicable_rate(route_id, "2027-02-01")
    assert caught.value.code == "RATE_NOT_APPLICABLE"


def test_a_route_with_no_active_rate_says_so_instead_of_guessing(conn):
    svc, route_id = route_with_unit(conn)
    with pytest.raises(SR.RateNotVerified) as caught:
        svc.applicable_rate(route_id, "2026-11-15")
    assert caught.value.code == "RATE_NOT_VERIFIED"


def test_a_verified_but_inactive_rate_is_not_quotable(conn):
    """VERIFIED is not quotable: only ACTIVE (and its ACTIVE contract) may be used."""
    svc, route_id = route_with_unit(conn)
    contract_id = active_contract(svc, route_id)
    rate_id = svc.draft_rate(
        route_id, contract_version_id=contract_id, currency="IDR", nightly_minor=9_000_000,
        min_stay=1, effective_from=WINDOW[0], effective_to=WINDOW[1],
        source_doc_ref="rates.pdf", source_reference="row 9",
    )
    svc.submit_rate(rate_id)
    svc.verify_rate(rate_id, actor="finance_verifier", actor_role="finance_manager")
    with pytest.raises(SR.RateNotVerified):
        svc.applicable_rate(route_id, "2026-11-15")


def test_a_rate_stops_being_quotable_when_its_contract_is_demoted(conn):
    """Applicability is checked at read time, not only at activation time.

    A contract can be superseded or expired *after* a rate was activated. If the
    read path trusted the rate's own status alone, the platform would keep quoting a
    price whose agreement no longer stands — exactly the silent reuse §12 forbids.
    """
    svc, route_id = route_with_unit(conn)
    contract_id = active_contract(svc, route_id)
    active_rate(svc, route_id, contract_id)
    assert svc.applicable_rate(route_id, "2026-11-15").nightly_minor == 2_500_000

    conn.execute(
        "UPDATE contract_versions SET status = 'VERIFIED' WHERE contract_version_id = ?",
        (contract_id,),
    )
    with pytest.raises(SR.RateNotVerified) as caught:
        svc.applicable_rate(route_id, "2026-11-15")
    assert caught.value.code == "RATE_NOT_VERIFIED"
    assert caught.value.details.get("contract_status") == "VERIFIED"


# --------------------------------------------------------------------------- #
# 5. One ACTIVE at a time, history preserved, overlap reported
# --------------------------------------------------------------------------- #
def test_activating_a_new_version_supersedes_the_old_one(conn):
    svc, route_id = route_with_unit(conn)
    first = active_contract(svc, route_id)
    second = svc.draft_contract(
        route_id, source_doc_ref="contract-2027.pdf", source_reference="clause 1",
        effective_from="2027-01-01", effective_to="2027-12-31",
    )
    svc.submit_contract(second)
    svc.verify_contract(second, actor="ops_verifier", actor_role="villa_contract_admin")
    svc.approve_contract(second, actor="owner_1", actor_role="owner")
    svc.activate_contract(second, actor="owner_1", actor_role="owner")

    statuses = {
        row["contract_version_id"]: row["status"]
        for row in conn.execute(
            "SELECT contract_version_id, status FROM contract_versions ORDER BY version"
        )
    }
    assert statuses[first] == "SUPERSEDED", "history must be preserved, not deleted"
    assert statuses[second] == "ACTIVE"
    assert list(statuses.values()).count("ACTIVE") == 1


def test_versions_increment_per_route_and_are_unique(conn):
    svc, route_id = route_with_unit(conn)
    first = svc.draft_contract(route_id, source_doc_ref="a.pdf", source_reference="1",
                               effective_from="2026-10-01", effective_to="2026-10-31")
    second = svc.draft_contract(route_id, source_doc_ref="b.pdf", source_reference="2",
                                effective_from="2026-11-01", effective_to="2026-11-30")
    versions = {
        row["contract_version_id"]: row["version"]
        for row in conn.execute("SELECT contract_version_id, version FROM contract_versions")
    }
    assert versions[first] == 1 and versions[second] == 2


def test_overlapping_active_versions_are_reported_as_a_conflict(conn):
    """Legacy/imported data can overlap; the platform must say so, not average it."""
    svc, route_id = route_with_unit(conn)
    active_contract(svc, route_id)
    conn.execute(
        "INSERT INTO contract_versions (contract_version_id, agency_id,"
        " villa_supplier_relationship_id, version, status, effective_from, effective_to,"
        " source_doc_ref, source_reference, verified_by, verified_at, approved_by, approved_at,"
        " created_at, updated_at)"
        " VALUES ('contract_legacy', 'agency_bali', ?, 5, 'ACTIVE', '2026-11-01', '2027-01-31',"
        " 'legacy.pdf', 'imported', 'ops_verifier', ?, 'owner_1', ?, ?, ?)",
        (route_id, START, START, START, START),
    )
    conflicts = svc.conflicts(route_id)
    assert conflicts, "overlapping ACTIVE versions were not reported"
    assert conflicts[0]["kind"] == "CONTRACT"
    assert "overlap_from" in conflicts[0]


# --------------------------------------------------------------------------- #
# 6. Route lookup and tenant scope
# --------------------------------------------------------------------------- #
def test_a_unit_resolves_to_its_active_route(conn):
    svc, route_id = route_with_unit(conn)
    assert svc.route_for_unit("unit_bali") == route_id


def test_an_unknown_unit_has_no_route(conn):
    svc, _route_id = route_with_unit(conn)
    with pytest.raises(SR.RouteNotFound):
        svc.route_for_unit("unit_tidak_ada")


def test_another_agencys_route_is_invisible(conn):
    svc_bali, route_bali = route_with_unit(conn, "agency_bali", "bali")
    svc_jogja = service(conn, "agency_jogja")
    with pytest.raises(SR.RouteNotFound):
        svc_jogja.draft_contract(
            route_bali, source_doc_ref="x.pdf", source_reference="1",
            effective_from=WINDOW[0], effective_to=WINDOW[1],
        )


def test_a_relationship_must_name_exactly_one_inventory_side(conn):
    svc = service(conn)
    supplier_id = svc.register_supplier("Supplier Ambigu")
    with pytest.raises(SR.SupplierRouteError) as caught:
        svc.link_villa(supplier_id)
    assert caught.value.code == "INVALID_ROUTE_TARGET"
    with pytest.raises(SR.SupplierRouteError):
        svc.link_villa(supplier_id, property_id="prop_bali", unit_id="unit_bali")

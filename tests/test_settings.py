"""Agency settings and tenant readiness (ADR-0037, decision D17).

What these tests are for. D17 says the platform may be ready while a *tenant* is not: an agency
with no payment provider, no WhatsApp number, no sellable property and no active unit must be
refused, by name, before it takes real guests and real money. The interesting cases are therefore
the refusals — an unknown key, a value that is not a number, a role that may not write, and a
readiness report that names exactly what is missing.
"""

from __future__ import annotations

import pytest

import settings as S
from audit import AuditLog
from primitives import FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
OTHER = "agency_other"


def settings_for(conn, agency_id: str = AGENCY) -> S.AgencySettings:
    return S.AgencySettings(
        conn,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=f"settings-{agency_id}"),
        agency_id=agency_id,
    )


def add_property(conn, *, property_id: str = "prop_s1", agency_id: str = AGENCY,
                 status: str = "SELLABLE") -> str:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES (?, ?, 'Villa Uji', 'villa', 'Ubud', 'Bali', 'Asia/Makassar', ?, 'ACTIVATED', ?, ?)",
        (property_id, agency_id, status, NOW, NOW),
    )
    return property_id


def add_unit(conn, property_id: str = "prop_s1", *, unit_id: str = "unit_s1",
             agency_id: str = AGENCY, status: str = "ACTIVE") -> str:
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Unit Uji', 'entire_villa', 2, 2, 2, 4, ?, ?, ?)",
        (unit_id, agency_id, property_id, status, NOW, NOW),
    )
    return unit_id


def fill_required(conn, *, actor: str = "owner_anas", role: str = "owner") -> None:
    service = settings_for(conn)
    values = {
        "payment.provider": "midtrans",
        "payment.rails": '["CARD","BANK_TRANSFER"]',
        "messaging.whatsapp_number": "+628123456789",
        "tax.pbjt_source": "peraturan daerah kabupaten Badung",
        "operations.oncall_primary": "Mr. TS",
    }
    for key, value in values.items():
        service.set(key, value, actor=actor, actor_role=role)


# --------------------------------------------------------------------------- #
# 1. The catalogue is closed: a key nobody reads is refused
# --------------------------------------------------------------------------- #
def test_an_unknown_key_is_refused_on_write_and_on_read(conn):
    service = settings_for(conn)
    with pytest.raises(S.InvalidSettingKey):
        service.set("payment.provider_typo", "x", actor="owner_anas", actor_role="owner")
    with pytest.raises(S.InvalidSettingKey):
        service.get("payment.provider_typo")
    assert conn.execute("SELECT COUNT(*) FROM agency_settings").fetchone()[0] == 0


def test_the_required_keys_are_exactly_the_five_this_release_needs():
    """Pinned so adding a required key is a deliberate change, not a silent new blocker."""
    assert S.required_keys() == (
        "messaging.whatsapp_number",
        "operations.oncall_primary",
        "payment.provider",
        "payment.rails",
        "tax.pbjt_source",
    )
    for key in S.required_keys():
        assert S.SETTING_DEFS[key].required_before_go_live is True


# --------------------------------------------------------------------------- #
# 2. Values are validated at the door
# --------------------------------------------------------------------------- #
def test_a_whatsapp_number_that_is_not_a_number_is_refused(conn):
    service = settings_for(conn)
    for bad in ("nomor saya", "+62 812", "abc123", "+62-812-3456-78901234567890"):
        with pytest.raises(S.InvalidSettingValue):
            service.set("messaging.whatsapp_number", bad, actor="owner_anas", actor_role="owner")
    assert conn.execute("SELECT COUNT(*) FROM agency_settings").fetchone()[0] == 0


def test_a_good_whatsapp_number_is_stored_normalised(conn):
    service = settings_for(conn)
    item = service.set("messaging.whatsapp_number", " +62812 3456 789 ",
                       actor="owner_anas", actor_role="owner")
    assert item.value == "+628123456789"


def test_a_rail_list_must_be_a_non_empty_list_of_names(conn):
    service = settings_for(conn)
    with pytest.raises(S.InvalidSettingValue):
        service.set("payment.rails", "[]", actor="owner_anas", actor_role="owner")
    with pytest.raises(S.InvalidSettingValue):
        service.set("payment.rails", '["CARD",""]', actor="owner_anas", actor_role="owner")
    item = service.set("payment.rails", '["CARD","BANK_TRANSFER"]',
                       actor="owner_anas", actor_role="owner")
    assert item.value == '["CARD","BANK_TRANSFER"]'


def test_a_year_count_must_be_a_whole_number_of_at_least_one(conn):
    service = settings_for(conn)
    with pytest.raises(S.InvalidSettingValue):
        service.set("retention.data_years", "tujuh", actor="owner_anas", actor_role="owner")
    with pytest.raises(S.InvalidSettingValue):
        service.set("retention.data_years", 0, actor="owner_anas", actor_role="owner")
    assert service.set("retention.data_years", 7, actor="owner_anas",
                       actor_role="owner").value == "7"


# --------------------------------------------------------------------------- #
# 3. Who may write, and what the write leaves behind
# --------------------------------------------------------------------------- #
def test_a_role_without_tenant_authority_may_not_write(conn):
    service = settings_for(conn)
    for role in ("reservation", "finance", "auditor", "agent"):
        with pytest.raises(S.SettingRoleRefused):
            service.set("payment.provider", "midtrans", actor="agent_x", actor_role=role)
    assert conn.execute("SELECT COUNT(*) FROM agency_settings").fetchone()[0] == 0


def test_a_write_lands_in_the_audit_chain_with_its_before_and_after(conn):
    service = settings_for(conn)
    service.set("payment.provider", "midtrans", actor="owner_anas", actor_role="owner",
                reason="menyetel provider untuk agency ini")
    service.set("payment.provider", "xendit", actor="gm_ceo", actor_role="gm",
                reason="ganti provider")
    events = conn.execute(
        "SELECT actor, actor_role, action, before_state, after_state FROM audit_events"
        " WHERE action = 'setting.set' ORDER BY seq"
    ).fetchall()
    assert len(events) == 2
    assert events[0]["actor"] == "owner_anas"
    assert events[0]["before_state"] is None
    assert "midtrans" in events[0]["after_state"]
    assert "midtrans" in events[1]["before_state"], "the previous value must be recorded"
    assert "xendit" in events[1]["after_state"]
    # and the chain still verifies: a settings write is not a hole in the evidence
    report = AuditLog(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "verify")).verify()
    assert report is not None


def test_the_row_keeps_a_revision_and_the_last_writer(conn):
    service = settings_for(conn)
    service.set("payment.provider", "midtrans", actor="owner_anas", actor_role="owner")
    service.set("payment.provider", "xendit", actor="gm_ceo", actor_role="gm")
    row = conn.execute(
        "SELECT revision, updated_by, updated_role FROM agency_settings WHERE setting_key = ?",
        ("payment.provider",),
    ).fetchone()
    assert (row["revision"], row["updated_by"], row["updated_role"]) == (2, "gm_ceo", "gm")


# --------------------------------------------------------------------------- #
# 4. Readiness: D17's question, answered by name
# --------------------------------------------------------------------------- #
def test_a_fresh_agency_is_not_ready_and_the_report_names_what_is_missing(conn):
    report = settings_for(conn).readiness()
    assert report.ready is False
    assert report.missing_required == S.required_keys()
    assert len(report.items) == len(S.SETTING_DEFS)
    blockers = report.blockers
    for key in S.required_keys():
        assert any(key in reason for reason in blockers)
    assert any("SELLABLE" in reason for reason in blockers)
    assert any("ACTIVE" in reason for reason in blockers)


def test_filling_every_required_key_and_having_a_sellable_unit_makes_it_ready(conn):
    fill_required(conn)
    add_property(conn)
    add_unit(conn)
    report = settings_for(conn).readiness()
    assert report.missing_required == ()
    assert report.ready is True
    assert report.blockers == ()


def test_a_property_waiting_for_review_does_not_count_as_ready(conn):
    """SELLABLE_WITH_REVIEW is not SELLABLE: readiness must not round up."""
    fill_required(conn)
    add_property(conn, status="SELLABLE_WITH_REVIEW")
    add_unit(conn)
    report = settings_for(conn).readiness()
    assert report.sellable_properties == 0
    assert report.ready is False


def test_another_agencys_property_does_not_make_this_agency_ready(conn):
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, 'Other Agency', 'ACTIVE', ?, ?)", (OTHER, NOW, NOW))
    fill_required(conn)
    add_property(conn, property_id="prop_other", agency_id=OTHER)
    add_unit(conn, property_id="prop_other", unit_id="unit_other", agency_id=OTHER)
    report = settings_for(conn).readiness()
    assert report.sellable_properties == 0
    assert report.active_units == 0
    assert report.ready is False


def test_two_agencies_keep_their_own_values(conn):
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, 'Other Agency', 'ACTIVE', ?, ?)", (OTHER, NOW, NOW))
    settings_for(conn).set("messaging.whatsapp_number", "+628111111111",
                           actor="owner_anas", actor_role="owner")
    other = settings_for(conn, OTHER)
    assert other.get("messaging.whatsapp_number") is None
    other.set("messaging.whatsapp_number", "+628222222222", actor="gm_other", actor_role="gm")
    assert settings_for(conn).get("messaging.whatsapp_number") == "+628111111111"
    assert other.get("messaging.whatsapp_number") == "+628222222222"


def test_the_readiness_helper_writes_nothing(conn):
    fill_required(conn)
    before = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    S.readiness_for(conn, AGENCY, clock=FrozenClock.at(NOW))
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before

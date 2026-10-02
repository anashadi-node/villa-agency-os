"""The tenant's role editor: renaming is free, outranking is not (D3, ADR-0035).

The tests below are written around that boundary. A guard that only checked "add and remove work"
would let a tenant rename its way into authority it was never given, or appoint a human to the
maker role the owner deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agency_roles as AR
import roles as ROLES
from audit import AuditLog
from primitives import DomainError, FrozenClock, IdFactory
from schema_loader import connect_sqlite

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
OTHER = "agency_other"


def service(conn, agency_id: str = AGENCY) -> AR.AgencyRoles:
    return AR.AgencyRoles(
        conn,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=f"roles-{agency_id}"),
        agency_id=agency_id,
    )


def seeded(conn, agency_id: str = AGENCY) -> AR.AgencyRoles:
    instance = service(conn, agency_id)
    instance.seed_blueprint(actor="owner_anas", actor_role="owner")
    return instance


def add_agency(conn, agency_id: str = OTHER) -> None:
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES (?, 'Other', 'ACTIVE', ?, ?)", (agency_id, NOW, NOW))


# --------------------------------------------------------------------------- #
# 1. Seeding: the blueprint, once
# --------------------------------------------------------------------------- #
def test_seeding_creates_the_eight_human_roles_of_the_blueprint(conn):
    seeded_roles = seeded(conn)
    assert {role.role_id for role in seeded_roles.roles()} == set(AR.BLUEPRINT_SEED)
    assert all(role.status == "ACTIVE" for role in seeded_roles.roles())


def test_seeding_twice_changes_nothing(conn):
    seeded(conn)
    before = conn.execute("SELECT COUNT(*) FROM agency_roles").fetchone()[0]
    seeded(conn)
    assert conn.execute("SELECT COUNT(*) FROM agency_roles").fetchone()[0] == before == 8


def test_a_role_without_ladder_rank_may_be_seeded_but_never_decides_money(conn):
    instance = seeded(conn)
    greeting = instance.get("greeting_service")
    assert greeting.rank is None
    assert greeting.is_ladder is False
    assert greeting.on_money_ladder is False


# --------------------------------------------------------------------------- #
# 2. Renaming is free; authority does not move
# --------------------------------------------------------------------------- #
def test_renaming_a_role_keeps_its_blueprint_and_rank(conn):
    instance = seeded(conn)
    renamed = instance.rename_role(
        "finance_manager", label="Bagian Keuangan & Pajak", actor="gm_ceo", actor_role="gm"
    )
    assert renamed.label == "Bagian Keuangan & Pajak"
    assert renamed.based_on == "finance_manager"
    assert renamed.rank == ROLES.role_rank("finance_manager")


def test_a_tenant_cannot_add_a_role_that_outranks_its_blueprint(conn):
    """Authority is `based_on`, so a new role can never be invented above the ladder."""
    instance = seeded(conn)
    new = instance.add_role(
        "finance_helper", label="Asisten Keuangan", based_on="finance_manager",
        actor="owner_anas", actor_role="owner",
    )
    assert new.rank == ROLES.role_rank("finance_manager")
    assert new.rank < ROLES.role_rank("gm")
    assert not ROLES.is_ai_role(new.based_on)


def test_the_ai_maker_roles_cannot_be_staffed_by_a_tenant(conn):
    instance = seeded(conn)
    for blueprint in ("ai_service", "reservation_agent", "finance_staff"):
        with pytest.raises(AR.BlueprintNotAssignable):
            instance.add_role("peran_baru", label="Peran Baru", based_on=blueprint,
                              actor="owner_anas", actor_role="owner")


def test_an_unknown_blueprint_is_refused(conn):
    instance = seeded(conn)
    with pytest.raises(AR.BlueprintNotAssignable):
        instance.add_role("peran_baru", label="Peran Baru", based_on="supervisor",
                          actor="owner_anas", actor_role="owner")


# --------------------------------------------------------------------------- #
# 3. Role ids and labels are validated
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["Rekening", "k", "1keuangan", "keuangan utama", ""])
def test_a_bad_role_id_is_refused(conn, bad):
    instance = seeded(conn)
    with pytest.raises(DomainError):
        instance.add_role(bad, label="Peran Baru", based_on="finance_manager",
                          actor="owner_anas", actor_role="owner")


@pytest.mark.parametrize("bad", ["", "ab", "X" * 81])
def test_a_bad_label_is_refused(conn, bad):
    instance = seeded(conn)
    with pytest.raises(AR.RoleRefused):
        instance.add_role("peran_baru", label=bad, based_on="finance_manager",
                          actor="owner_anas", actor_role="owner")


def test_a_duplicate_role_id_is_refused(conn):
    instance = seeded(conn)
    with pytest.raises(AR.RoleRefused):
        instance.add_role("finance_manager", label="Duplikat", based_on="gm",
                          actor="owner_anas", actor_role="owner")


# --------------------------------------------------------------------------- #
# 4. Who may edit roles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["reservation", "finance", "auditor", "agent", "reservation_manager"])
def test_a_role_without_editor_authority_is_refused(conn, role):
    instance = seeded(conn)
    with pytest.raises(AR.RoleRefused):
        instance.add_role("peran_baru", label="Peran Baru", based_on="finance_manager",
                          actor="someone", actor_role=role)
    with pytest.raises(AR.RoleRefused):
        instance.rename_role("gm", label="Bukan CEO", actor="someone", actor_role=role)


def test_the_owner_gm_and_system_admin_may_edit(conn):
    instance = seeded(conn)
    instance.rename_role("greeting_service", label="Tim Penerima Tamu",
                         actor="owner_anas", actor_role="owner")
    instance.rename_role("greeting_service", label="Tim Sambut Tamu",
                         actor="gm_ceo", actor_role="gm")
    instance.rename_role("greeting_service", label="Tim Front Office",
                         actor="ts_admin", actor_role="system_admin")
    assert instance.get("greeting_service").label == "Tim Front Office"


# --------------------------------------------------------------------------- #
# 5. Disabling, and the authority that must not be orphaned
# --------------------------------------------------------------------------- #
def test_a_role_with_a_holder_cannot_be_disabled_until_the_holder_is_removed(conn):
    instance = seeded(conn)
    instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    with pytest.raises(AR.RoleInUse) as caught:
        instance.disable_role("gm", actor="owner_anas", actor_role="owner")
    assert caught.value.details["holders"] == ["mr_ceo"]
    instance.deactivate_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    disabled = instance.disable_role("gm", actor="owner_anas", actor_role="owner")
    assert disabled.status == "DISABLED"


def test_a_disabled_role_may_not_receive_a_holder(conn):
    instance = seeded(conn)
    instance.disable_role("commercial_manager", actor="owner_anas", actor_role="owner")
    with pytest.raises(AR.RoleRefused):
        instance.assign_holder("commercial_manager", holder_ref="mr_cmm",
                               actor="owner_anas", actor_role="owner")
    enabled = instance.enable_role("commercial_manager", actor="owner_anas", actor_role="owner")
    assert enabled.status == "ACTIVE"


def test_appointing_the_same_holder_twice_is_one_appointment(conn):
    instance = seeded(conn)
    instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    again = instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    assert again.holders == ("mr_ceo",)
    assert conn.execute("SELECT COUNT(*) FROM agency_role_holders").fetchone()[0] == 1


def test_a_holder_ref_is_an_identifier_not_a_name(conn):
    instance = seeded(conn)
    for bad in ("Mr. CEO", "Budi Santoso", "budi@villa.id"):
        with pytest.raises(DomainError):
            instance.assign_holder("gm", holder_ref=bad, actor="owner_anas", actor_role="owner")


# --------------------------------------------------------------------------- #
# 6. D17: the money ladder must have people
# --------------------------------------------------------------------------- #
def test_the_money_ladder_is_unstaffed_until_holders_are_appointed(conn):
    instance = seeded(conn)
    assert instance.unstaffed_ladder_roles() == tuple(sorted(AR.LADDER_STAFFING_REQUIRED))
    instance.assign_holder("finance_manager", holder_ref="ms_fam",
                           actor="owner_anas", actor_role="owner")
    instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    instance.assign_holder("owner", holder_ref="mr_owner", actor="owner_anas", actor_role="owner")
    assert instance.unstaffed_ladder_roles() == ()


def test_a_non_ladder_role_does_not_satisfy_the_ladder(conn):
    instance = seeded(conn)
    instance.assign_holder("greeting_service", holder_ref="ms_greeting",
                           actor="owner_anas", actor_role="owner")
    assert "gm" in instance.unstaffed_ladder_roles()


def test_a_removed_holder_leaves_the_ladder_unstaffed_again(conn):
    instance = seeded(conn)
    instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    instance.deactivate_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    assert "gm" in instance.unstaffed_ladder_roles()


# --------------------------------------------------------------------------- #
# 7. Two agencies, two role sets — and both in the audit chain
# --------------------------------------------------------------------------- #
def test_an_agencys_roles_are_its_own(conn):
    add_agency(conn)
    mine = seeded(conn)
    mine.rename_role("gm", label="Direktur Utama", actor="owner_anas", actor_role="owner")
    other = service(conn, OTHER)
    other.seed_blueprint(actor="gm_other", actor_role="gm")
    assert other.get("gm").label == "GM/CEO"
    assert mine.get("gm").label == "Direktur Utama"
    assert "Direktur Utama" not in {role.label for role in other.roles()}


def test_every_role_change_lands_in_the_audit_chain(conn):
    instance = seeded(conn)
    instance.rename_role("gm", label="Direktur Utama", actor="owner_anas", actor_role="owner")
    instance.assign_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    instance.deactivate_holder("gm", holder_ref="mr_ceo", actor="owner_anas", actor_role="owner")
    actions = [
        row[0]
        for row in conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'agency_role.%' ORDER BY seq"
        ).fetchall()
    ]
    assert actions.count("agency_role.seeded") == 8
    assert "agency_role.renamed" in actions
    assert "agency_role.holder_appointed" in actions
    assert "agency_role.holder_removed" in actions
    AuditLog(conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "verify")).verify()


def test_a_refused_change_writes_nothing(conn):
    instance = seeded(conn)
    before = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(AR.BlueprintNotAssignable):
        instance.add_role("peran_x", label="Peran X", based_on="ai_service",
                          actor="owner_anas", actor_role="owner")
    assert conn.execute("SELECT COUNT(*) FROM agency_roles").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before


# --------------------------------------------------------------------------- #
# 8. The CLI: plan by default, and the roster it prints
# --------------------------------------------------------------------------- #
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

CLI = Path(__file__).resolve().parents[1] / "scripts" / "manage_roles.py"


def run_cli(*args: str, env: dict | None = None):
    merged = dict(os.environ)
    merged.pop("VILLA_DB", None)
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=120, env=merged)


def test_the_cli_lists_roles_and_names_the_unstaffed_ladder(tmp_path):
    db = tmp_path / "villa.db"
    connect_sqlite(str(db)).close()
    env = {"VILLA_DB": str(db)}
    seeded_run = run_cli("seed", "--apply", "--actor", "owner_anas", env=env)
    assert seeded_run.returncode == 0, seeded_run.stdout
    listed = run_cli("list", env=env)
    assert listed.returncode == 0
    assert "finance_manager" in listed.stdout
    assert "money ladder with no holder" in listed.stdout
    assert "go-live stays refused" in listed.stdout


def test_the_cli_needs_villa_db_and_an_actor_to_write(tmp_path):
    db = tmp_path / "villa.db"
    connect_sqlite(str(db)).close()
    without_db = run_cli("seed", "--apply", "--actor", "owner_anas")
    assert without_db.returncode == 2
    assert "VILLA_DB" in without_db.stderr
    without_actor = run_cli("seed", "--apply", env={"VILLA_DB": str(db)})
    assert without_actor.returncode == 2
    assert "--actor" in without_actor.stderr


def test_the_cli_plans_without_writing(tmp_path):
    db = tmp_path / "villa.db"
    connect_sqlite(str(db)).close()
    planned = run_cli("seed", "--actor", "owner_anas", env={"VILLA_DB": str(db)})
    assert planned.returncode == 0
    assert "PLAN ONLY" in planned.stdout
    conn = connect_sqlite(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM agency_roles").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_cli_refuses_the_ai_maker_blueprint(tmp_path):
    db = tmp_path / "villa.db"
    connect_sqlite(str(db)).close()
    refused = run_cli(
        "add", "--role", "pembuat", "--label", "Pembuat", "--based-on", "ai_service",
        "--apply", "--actor", "owner_anas", env={"VILLA_DB": str(db)},
    )
    assert refused.returncode == 1
    assert "BLUEPRINT_NOT_ASSIGNABLE" in refused.stdout


def test_the_cli_appoints_a_holder_and_the_ladder_shrinks(tmp_path):
    db = tmp_path / "villa.db"
    connect_sqlite(str(db)).close()
    env = {"VILLA_DB": str(db)}
    run_cli("seed", "--apply", "--actor", "owner_anas", env=env)
    for role, holder in (("owner", "mr_owner"), ("gm", "mr_ceo"), ("finance_manager", "ms_fam")):
        done = run_cli("appoint", "--role", role, "--holder", holder, "--apply",
                       "--actor", "owner_anas", env=env)
        assert done.returncode == 0, done.stdout
    listed = run_cli("list", env=env)
    assert "money ladder with no holder" not in listed.stdout

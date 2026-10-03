"""The tenant-onboarding path: the documented commands a customer runs to configure itself.

Why this file exists
--------------------
``docs/TENANT_SETUP.md`` is the white-label promise in executable form: import a whole villa
list from one file, name your own roles, fill the money ladder, publish the policy values that
are yours to decide. Until this file existed **none of those commands was ever run by anything**
— the seeder and the role CLI had their own tests, but the *documented sequence*, in the
documented order, on a fresh database, had no witness.

It has one now, and it caught a real defect on the first run: the documentation taught
``--actor "Mr. Owner"``, which the platform refuses with ``actor must not contain whitespace``.
The docs are fixed and this file pins the refusal, so a future edit cannot quietly reintroduce
a command that cannot run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from schema_loader import connect_sqlite

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run(db: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["VILLA_DB"] = str(db)
    return subprocess.run(
        [PYTHON, str(REPO / "scripts" / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=120,
    )


def _scalar(db: Path, sql: str) -> object:
    conn = connect_sqlite(str(db))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def listing(tmp_path: Path) -> Path:
    """The CSV the tool itself prints — the customer's starting point."""
    csv_path = tmp_path / "villa-list.csv"
    result = subprocess.run(
        [PYTHON, str(REPO / "scripts" / "import_villas.py"), "--template"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    csv_path.write_text(result.stdout, encoding="utf-8")
    return csv_path


@pytest.fixture()
def tenant(tmp_path: Path, listing: Path) -> tuple[Path, Path]:
    """A configured tenant: villas imported, roles seeded, money ladder filled."""
    db = tmp_path / "tenant.db"
    applied = _run(
        db, "import_villas.py", "--file", str(listing), "--agency", "agency_default",
        "--actor", "owner_anas", "--actor-role", "owner",
        "--source-ref", "villa-list-okt-2026.csv", "--apply",
    )
    assert applied.returncode == 0, applied.stderr + applied.stdout
    seeded = _run(db, "manage_roles.py", "seed", "--apply", "--actor", "owner_anas",
                  "--actor-role", "owner")
    assert seeded.returncode == 0, seeded.stderr
    for role, holder in (("finance_manager", "mr_fam"), ("gm", "mr_ceo"), ("owner", "mr_owner")):
        appointed = _run(db, "manage_roles.py", "appoint", "--role", role,
                         "--holder", holder, "--apply", "--actor", "owner_anas")
        assert appointed.returncode == 0, appointed.stderr
    return db, listing


# -- the import path ---------------------------------------------------------- #


def test_plan_only_writes_nothing(tmp_path: Path, listing: Path):
    """The doc's safety claim, measured: step one leaves the database untouched."""
    db = tmp_path / "plan.db"
    result = _run(db, "import_villas.py", "--file", str(listing))
    assert result.returncode == 0, result.stderr
    assert "PLAN ONLY" in result.stdout
    assert _scalar(db, "SELECT COUNT(*) FROM properties") == 0


def test_the_documented_import_applies_the_whole_file(tenant):
    db, _ = tenant
    assert _scalar(db, "SELECT COUNT(*) FROM properties") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM units") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM audit_events") > 0


def test_an_actor_with_whitespace_is_refused(tmp_path: Path, listing: Path):
    """The refusal the documentation used to walk into — pinned so it stays honest.

    It is the *documented* form (``--actor "Mr. Owner"``) that is wrong, not the rule: an
    actor is a reference, and a reference with a space in it is a display name the platform
    would then have to trust. The docs now teach a token; this test teaches the reason.
    """
    db = tmp_path / "actor.db"
    result = _run(
        db, "import_villas.py", "--file", str(listing), "--agency", "agency_default",
        "--actor", "Mr. Owner", "--actor-role", "owner", "--apply",
    )
    assert result.returncode != 0
    assert "INVALID_FIELD" in result.stdout + result.stderr
    assert _scalar(db, "SELECT COUNT(*) FROM properties") == 0, "a refused run wrote rows"


# -- the role path ------------------------------------------------------------ #


def test_seed_starts_from_the_platform_blueprint(tmp_path: Path):
    db = tmp_path / "roles.db"
    before = _run(db, "manage_roles.py", "list")
    assert "no roles yet" in before.stdout
    seeded = _run(db, "manage_roles.py", "seed", "--apply", "--actor", "owner_anas",
                  "--actor-role", "owner")
    assert seeded.returncode == 0
    after = _run(db, "manage_roles.py", "list")
    assert "finance_manager" in after.stdout and "gm" in after.stdout


def test_the_money_ladder_is_complete_once_every_role_has_a_holder(tenant):
    """D17's go-live condition, read through the tool a customer actually runs."""
    db, _ = tenant
    listed = _run(db, "manage_roles.py", "list")
    assert "money ladder with no holder" not in listed.stdout
    for role, holder in (("finance_manager", "mr_fam"), ("gm", "mr_ceo"), ("owner", "mr_owner")):
        held = _scalar(
            db,
            "SELECT COUNT(*) FROM agency_role_holders"
            f" WHERE role_id = '{role}' AND holder_ref = '{holder}' AND status = 'ACTIVE'",
        )
        assert int(held) == 1, f"{role} is not held by {holder}"


def test_a_tenant_can_rename_and_extend_its_own_roles(tenant):
    """Free names, fixed authority — and a new role inherits a blueprint."""
    db, _ = tenant
    renamed = _run(db, "manage_roles.py", "rename", "--role", "finance_manager",
                   "--label", "Bagian Keuangan Pajak", "--apply", "--actor", "owner_anas")
    assert renamed.returncode == 0
    assert "authority unchanged" in renamed.stdout
    added = _run(db, "manage_roles.py", "add", "--role", "finance_helper",
                 "--label", "Asisten Keuangan", "--based-on", "finance_manager",
                 "--apply", "--actor", "owner_anas")
    assert added.returncode == 0
    disabled = _run(db, "manage_roles.py", "disable", "--role", "commercial_manager",
                    "--apply", "--actor", "owner_anas")
    assert disabled.returncode == 0


def test_a_tenant_publishes_its_own_policy_value(tenant):
    db, _ = tenant
    published = _run(
        db, "publish_policy_value.py", "set",
        "--key", "operations.booking.required_document_kinds",
        "--value", '["CONFIRMATION","INVOICE"]',
        "--source-doc-ref", "SOP-penutupan-booking-2026.pdf",
        "--approved-by", "owner_anas",
    )
    assert published.returncode == 0, published.stderr
    assert "PUBLISHED" in published.stdout
    shown = _run(db, "publish_policy_value.py", "show",
                 "--key", "operations.booking.required_document_kinds")
    assert "CONFIRMATION" in shown.stdout


def test_preflight_stops_naming_the_money_ladder_once_it_is_filled(tenant, tmp_path: Path):
    """Before/after through the gate that actually refuses go-live.

    A fresh database is told its money ladder has no holder; a configured one no longer is.
    The remaining refusals (payment provider, WhatsApp number, anchors) are the customer's
    decisions, not the platform's gaps — so they are asserted only to be *present*, not fixed.
    """
    db, _ = tenant
    result = _run(db, "preflight.py", "--db", str(db), "--anchor-dir", str(tmp_path / "anchors"))
    output = result.stdout + result.stderr
    assert "money ladder has no holder" not in output
    assert "required setting not filled" in output, "the unfilled settings must still be named"

"""Tenant scope on the dashboard: another agency's rows must never render.

Master Spec §18/§39 keeps agency scope in the backend. The dashboard is a reader,
so a missing ``agency_id`` predicate is a cross-tenant read, not a display quirk:
``_rows``/``_count``/``_one`` inject nothing — the caller owns the filter, and a
new query can silently forget it.

The proof is a dump of one database whose scoped rows *all* belong to
``agency_b``, taken for both agencies. ``agency_default`` must render no row and
zero counters; ``agency_b`` must render the rows. That pair fails the moment a
filter is dropped, which is why the guard is a dump and not a grep.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dashboard as D
from schema_loader import connect_sqlite
from test_dashboard import kpi, make_app, render, seed

REPO_ROOT = Path(D.REPO_ROOT)

#: Text that exists only in the seeded rows (``seed()`` names the first property).
MARKER = "Villa Anantara"

#: Tables the dashboard reads that carry their own ``agency_id``. Moving the
#: seeded rows out of ``agency_default`` is what makes a missing filter visible.
SCOPED_TABLES = (
    "properties",
    "units",
    "bookings",
    "holds",
    "cases",
    "sla_breaches",
    "invoices",
    "payments",
    "ops_tasks",
    "incidents",
    "approval_requests",
    "reconciliation_cases",
)

#: Tables the seed must populate for the move to mean anything.
SEEDED_TABLES = (
    "properties",
    "units",
    "bookings",
    "holds",
    "cases",
    "sla_breaches",
    "invoices",
    "payments",
    "ops_tasks",
    "incidents",
    "approval_requests",
)

#: Independent expectation of the per-agency counters: the audit chain is a
#: global log, every other counter must be scoped.
GLOBAL_KPI = frozenset({"audit_chain"})
SCOPED_KPI = frozenset(
    {
        "cases_by_status",
        "open_sla_breaches",
        "pending_approvals",
        "holds_expiring_today",
        "unpaid_invoices",
        "unmatched_payments",
        "late_ops_tasks",
        "open_incidents",
    }
)

#: Pages that read scoped tables only (``audit`` is a global chain, by design).
SCOPED_PAGES = ("cases", "inventory", "finance", "operations", "approvals", "properties")

#: Pages that also render the global audit chain: their recent-events table names
#: subjects from every agency, which is the chain working as specified.
GLOBAL_CHAIN_PAGES = frozenset({"audit.html", "index.html"})


@pytest.fixture()
def other_agency(tmp_path, clock):
    """A seeded database whose every scoped row belongs to ``agency_b``."""
    db = tmp_path / "two-agencies.db"
    conn = connect_sqlite(db)
    seed(conn, clock)
    now = clock.now().isoformat()
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_b', 'Agency B', 'ACTIVE', ?, ?)",
        (now, now),
    )
    moved = {}
    for table in SCOPED_TABLES:
        moved[table] = conn.execute(
            f"UPDATE {table} SET agency_id = 'agency_b' WHERE agency_id = 'agency_default'"
        ).rowcount
    conn.commit()
    for table in SEEDED_TABLES:
        assert moved[table] > 0, f"seed() tidak mengisi {table}: setup tes tidak sah"
    assert set(D.KPI_KEYS) - GLOBAL_KPI == SCOPED_KPI, "katalog KPI bergeser: tinjau ulang"
    yield conn, db
    conn.close()


def _dump(conn, clock, agency: str, directory: Path) -> dict[str, str]:
    """Every page this agency's owner may open, as ``name → html``."""
    files = make_app(conn, clock, agency_id=agency).dump(directory)
    assert files, f"dump {agency} tidak menulis berkas apa pun"
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def test_agency_default_renders_no_row_of_agency_b(other_agency, clock):
    """The negative control: the rows exist, but not for this agency."""
    conn, _ = other_agency
    app = make_app(conn, clock, agency_id="agency_default")

    page = render(app, "command_center", "owner")
    for key in sorted(SCOPED_KPI):
        assert kpi(page, key) == "0", f"KPI {key} membocorkan baris agency_b"

    for name in SCOPED_PAGES:
        body = render(app, name, "owner")
        assert MARKER not in body, f"halaman {name} menampilkan properti agency_b"
        assert "case-0001" not in body, f"halaman {name} menampilkan kasus agency_b"


def test_agency_b_renders_its_own_rows(other_agency, clock):
    """The positive control: the same database, read by the agency that owns it."""
    conn, _ = other_agency
    app = make_app(conn, clock, agency_id="agency_b")

    page = render(app, "command_center", "owner")
    assert int(kpi(page, "cases_by_status")) == 3
    assert int(kpi(page, "open_sla_breaches")) == 1
    assert int(kpi(page, "open_incidents")) == 1
    assert MARKER in render(app, "inventory", "owner")
    assert MARKER in render(app, "properties", "owner")


def test_dump_keeps_every_page_inside_one_agency(other_agency, clock, tmp_path):
    """The whole page set, including the per-case detail files."""
    conn, _ = other_agency

    mine = _dump(conn, clock, "agency_b", tmp_path / "b")
    assert MARKER in mine["inventory.html"]
    assert any(name.startswith("case-") for name in mine), "detail kasus tidak ikut ter-dump"
    assert any("case-0001" in body for body in mine.values())

    theirs = _dump(conn, clock, "agency_default", tmp_path / "a")
    for name, body in theirs.items():
        assert MARKER not in body, f"{name} memuat properti agency_b"
        if name in GLOBAL_CHAIN_PAGES:
            continue  # rantai audit global: subjeknya boleh menyebut kasus agensi mana pun
        assert "case-0001" not in body, f"{name} memuat kasus agency_b"
    assert not [name for name in theirs if name.startswith("case-")], (
        "dump agency_default menulis detail kasus milik agency_b"
    )
    # Positif untuk sifat global itu sendiri: rantai audit memang terlihat dari
    # kedua agensi (bukan kebocoran yang harus ditutup).
    assert "case:case-0001" in theirs["index.html"]


def test_the_dump_cli_binds_a_run_to_one_agency(other_agency, clock, tmp_path):
    """The operator entry point, not just the class: ``--agency`` must bind."""
    _, db = other_agency

    def run(agency: str, out: Path, *, flag: bool) -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        argv = [sys.executable, str(REPO_ROOT / "src" / "dashboard.py"), "--db", str(db)]
        argv += ["--dump", str(out)]
        if flag:
            argv += ["--agency", agency]
        else:
            env["VILLA_AGENCY_ID"] = agency
        done = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=REPO_ROOT)
        assert done.returncode == 0, done.stderr
        return "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(out.glob("*.html"))
        )

    by_flag = run("agency_b", tmp_path / "flag", flag=True)
    assert MARKER in by_flag, "CLI --agency tidak menyaring halaman"
    by_env = run("agency_default", tmp_path / "env", flag=False)
    assert MARKER not in by_env, "CLI VILLA_AGENCY_ID tidak menyaring halaman"

"""The kill-switch CLI: the button has to work twice, and it has to refuse the right people.

Driven through separate processes on purpose. The bug this file exists because of was invisible
in-process: a deterministic id factory restarts at 1 in every process, so the *second* run of
``set`` collided with the ids the first run had already stored (2026-10-02). Only a real second
process reproduces that, which is why these tests are subprocess tests and not unit tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from audit import AuditLog  # noqa: E402
from policy_engine import PolicyResolver  # noqa: E402
from primitives import IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402
import ai_service as AIS  # noqa: E402

CLI = REPO / "scripts" / "ai_mode.py"


def run_cli(*args: str, db: Path, extra_env: dict | None = None):
    merged = dict(os.environ)
    merged.pop("VILLA_DB", None)
    merged["VILLA_DB"] = str(db)
    if extra_env:
        merged.update(extra_env)
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=120, env=merged
    )


@pytest.fixture()
def populated(tmp_path):
    """A copy of the real schema with one AI principal in it, plus its id."""
    db = tmp_path / "villa.db"
    conn = connect_sqlite(db)
    clock = SystemClock()
    service = AIS.AiService(
        conn,
        policy=PolicyResolver(conn, clock=clock),
        audit=AuditLog(conn, clock=clock, ids=IdFactory()),
        clock=clock,
    )
    principal = service.register_principal(
        "villa-concierge", secret_ref="VILLA_AI_SERVICE_SECRET", actor="mr_owner"
    )
    conn.close()
    return db, principal.principal_id


def mode_in_db(db: Path, principal_id: str) -> str:
    conn = connect_sqlite(db)
    try:
        service = AIS.AiService(
            conn, policy=PolicyResolver(conn, clock=SystemClock()), clock=SystemClock()
        )
        return service.principal(principal_id).status
    finally:
        conn.close()


def test_the_list_shows_the_principal_its_mode_and_the_ladder(populated):
    db, principal_id = populated
    listed = run_cli("list", db=db)
    assert listed.returncode == 0
    assert principal_id in listed.stdout
    assert "villa-concierge" in listed.stdout
    assert "ACTIVE" in listed.stdout
    assert "ACTIVE > DEGRADED > READ_ONLY > SUSPENDED > RETIRED" in listed.stdout


def test_the_plan_writes_nothing(populated):
    db, principal_id = populated
    planned = run_cli(
        "set", "--principal", principal_id, "--mode", "read_only",
        "--actor", "mr_owner", "--actor-role", "owner", db=db,
    )
    assert planned.returncode == 0
    assert "PLAN ONLY" in planned.stdout and "nothing was written" in planned.stdout
    assert mode_in_db(db, principal_id) == AIS.AI_STATUS_ACTIVE


def test_pressing_it_twice_in_two_processes_both_succeed(populated):
    """The regression: run 2 used to die on `UNIQUE constraint failed: audit_events`."""
    db, principal_id = populated
    first = run_cli(
        "set", "--principal", principal_id, "--mode", "read_only", "--actor", "mr_owner",
        "--actor-role", "owner", "--reason", "first", "--apply", db=db,
    )
    assert first.returncode == 0, first.stderr
    second = run_cli(
        "set", "--principal", principal_id, "--mode", "suspended", "--actor", "mr_owner",
        "--actor-role", "owner", "--reason", "second", "--apply", db=db,
    )
    assert second.returncode == 0, second.stderr
    assert "VERDICT: APPLIED" in second.stdout
    assert mode_in_db(db, principal_id) == AIS.AI_STATUS_SUSPENDED
    conn = connect_sqlite(db)
    try:
        rows = conn.execute("SELECT action FROM audit_events ORDER BY seq").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in rows] == ["AI_PRINCIPAL_READ_ONLY", "AI_PRINCIPAL_SUSPENDED"]


def test_the_gm_cannot_press_it(populated):
    db, principal_id = populated
    refused = run_cli(
        "set", "--principal", principal_id, "--mode", "suspended", "--actor", "mr_gm",
        "--actor-role", "gm", "--apply", db=db,
    )
    assert refused.returncode == 1
    assert "AI_CONTROL_NOT_HELD" in refused.stdout
    assert "owner, system_admin" in refused.stdout
    assert mode_in_db(db, principal_id) == AIS.AI_STATUS_ACTIVE


def test_an_unsigned_press_is_a_usage_error(populated):
    db, principal_id = populated
    unsigned = run_cli("set", "--principal", principal_id, "--mode", "read_only", db=db)
    assert unsigned.returncode == 2
    assert "--actor-role" in unsigned.stderr
    assert mode_in_db(db, principal_id) == AIS.AI_STATUS_ACTIVE


def test_a_mode_change_needs_the_database_named_explicitly(populated):
    """No default target: a switch that can quietly hit the live database is not a switch."""
    _, principal_id = populated
    env = dict(os.environ)
    env.pop("VILLA_DB", None)
    unsigned = subprocess.run(
        [sys.executable, str(CLI), "set", "--principal", principal_id, "--mode", "read_only",
         "--actor", "mr_owner", "--actor-role", "owner", "--apply"],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(REPO),
    )
    assert unsigned.returncode == 2
    assert "VILLA_DB" in unsigned.stderr


def test_the_history_names_who_moved_it_and_what_is_reachable_now(populated):
    db, principal_id = populated
    run_cli(
        "set", "--principal", principal_id, "--mode", "suspended", "--actor", "mr_tsm",
        "--actor-role", "system_admin", "--reason", "guest data check", "--apply", db=db,
    )
    shown = run_cli("show", "--principal", principal_id, db=db)
    assert shown.returncode == 0
    assert "REGISTERED" in shown.stdout and "SUSPENDED" in shown.stdout
    assert "mr_tsm" in shown.stdout and "guest data check" in shown.stdout
    assert "reachable from here:" in shown.stdout
    offered = shown.stdout.split("reachable from here:")[1].strip().split(", ")
    assert set(offered) == {"suspended", "active", "retired"}


def test_a_bad_mode_is_a_usage_error_not_a_traceback(populated):
    db, principal_id = populated
    bad = run_cli("set", "--principal", principal_id, "--mode", "wide_open", db=db)
    assert bad.returncode == 2
    assert "Traceback" not in bad.stderr


def test_the_ladder_hint_never_names_a_step_the_guard_refuses(populated):
    """The CLI's promise and the service's guard are read from the same table."""
    db, principal_id = populated
    conn = connect_sqlite(db)
    try:
        service = AIS.AiService(
            conn, policy=PolicyResolver(conn, clock=SystemClock()), clock=SystemClock()
        )
        reachable = service.reachable_modes(principal_id)
    finally:
        conn.close()
    assert "RETIRED" in reachable

    def current_reachable() -> set[str]:
        conn = connect_sqlite(db)
        try:
            service = AIS.AiService(
                conn, policy=PolicyResolver(conn, clock=SystemClock()), clock=SystemClock()
            )
            return set(service.reachable_modes(principal_id))
        finally:
            conn.close()

    # From wherever it is now, every offered step must work — and the plan must be re-read each
    # time, because a move changes what is reachable next (that is the whole point of a ladder).
    for _ in range(4):
        offered = current_reachable()
        movable = sorted(m for m in offered if m != AIS.AI_STATUS_RETIRED)
        assert movable, "nothing offered, yet the principal is not retired"
        step = movable[0]
        ok = run_cli(
            "set", "--principal", principal_id, "--mode", step.lower(), "--actor", "mr_owner",
            "--actor-role", "owner", "--apply", db=db,
        )
        assert ok.returncode == 0, f"{step} was offered but refused: {ok.stdout}{ok.stderr}"
        assert json.dumps(mode_in_db(db, principal_id)) == json.dumps(step)

"""Null is not a refusal — regression tests for the "reason required" guards.

Why this file exists. On 2026-11-14 the workforce contract tests found that five CHECK
constraints written as ``length(reason) >= 3`` accepted an *empty* reason: in SQL,
``length(NULL)`` evaluates to ``NULL``, and a CHECK rejects only when the expression is
``FALSE`` — ``NULL`` passes. A rule that reads "a void must say why" could therefore be
satisfied by saying nothing at all.

The fix was to write the guard as ``reason IS NOT NULL AND length(reason) >= 3``. This file
pins that fix in three layers so a later migration cannot quietly undo it:

1. **Static** — every ``CHECK`` in the schema that demands a minimum length also demands
   ``IS NOT NULL``; checked for both the module file and the merged boot file.
2. **Behavioural** — for each affected table, ``NULL`` is refused *and* a positive control
   is inserted successfully, so the test cannot pass by refusing everything.
3. **Parity** — SQLite and Postgres twins must carry the same guard text (a schema whose two
   dialects disagree is two schemas).

Values are synthetic only; the public tree never carries real personal data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

NOW = "2026-11-14T00:00:00Z"
LATER = "2026-11-14T09:00:00Z"
AGENCY = "agency_default"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "src" / "schema"

MODULE_FILES = ("018_workforce_ops.sql", "001_initial.sql", "001_initial.pg.sql")


# --------------------------------------------------------------------------------------
# 1. static guard — the shape of every "must say why" rule
# --------------------------------------------------------------------------------------


def _check_constraints(sql: str) -> list[tuple[str, str]]:
    """Every ``CONSTRAINT <name> CHECK ( ... )`` body, paren-balanced."""
    bodies: list[tuple[str, str]] = []
    for match in re.finditer(r"CONSTRAINT\s+(\w+)\s+CHECK\s*\(", sql, re.IGNORECASE):
        depth = 1
        i = match.end()
        while i < len(sql) and depth:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
            i += 1
        bodies.append((match.group(1), sql[match.end():i - 1]))
    return bodies


@pytest.mark.parametrize("filename", MODULE_FILES)
def test_every_minimum_length_guard_also_rejects_null(filename):
    path = SCHEMA_DIR / filename
    if filename.endswith(".pg.sql") and not path.exists():
        pytest.skip(f"{filename} not generated yet")
    offenders = [
        name
        for name, body in _check_constraints(path.read_text(encoding="utf-8"))
        if re.search(r"length\s*\(", body)
        and re.search(r">=\s*\d+", body)
        and "IS NOT NULL" not in body.upper()
    ]
    assert offenders == [], (
        "these CHECK constraints demand a minimum length but let NULL through: "
        f"{offenders} — write '<col> IS NOT NULL AND length(<col>) >= n' instead"
    )


def test_positive_control_the_static_scan_sees_constraints():
    """A scanner that finds nothing proves nothing: assert it actually parses guards."""
    bodies = _check_constraints((SCHEMA_DIR / "018_workforce_ops.sql").read_text(encoding="utf-8"))
    length_guards = [
        name for name, body in bodies
        if re.search(r"length\s*\(", body) and re.search(r">=\s*\d+", body)
    ]
    assert len(length_guards) >= 7, f"expected the workforce minimum-length guards, saw {length_guards}"
    expected = {
        "ck_workforce_eligibility_reason",
        "ck_workforce_roster_reason",
        "ck_workforce_attendance_manual",
        "ck_workforce_leave_rejection_reason",
        "ck_workforce_timesheet_rejected",
        "ck_workforce_assignment_dropped",
        "ck_workforce_payroll_void",
    }
    assert set(length_guards) == expected, (
        "the set of minimum-length guards changed: "
        f"missing={sorted(expected - set(length_guards))} new={sorted(set(length_guards) - expected)}"
    )


def test_guard_text_is_identical_in_sqlite_and_postgres_twins():
    sqlite_sql = (SCHEMA_DIR / "001_initial.sql").read_text(encoding="utf-8")
    pg_path = SCHEMA_DIR / "001_initial.pg.sql"
    if not pg_path.exists():
        pytest.skip("Postgres twin not generated yet")
    pg_sql = pg_path.read_text(encoding="utf-8")

    def guards(sql: str) -> dict[str, str]:
        found = {}
        for name, body in _check_constraints(sql):
            if "IS NOT NULL" in body.upper() and re.search(r"length\s*\(", body):
                found[name] = re.sub(r"\s+", " ", body.strip())
        return found

    sqlite_guards, pg_guards = guards(sqlite_sql), guards(pg_sql)
    assert sqlite_guards, "positive control: no guarded constraints found in the merged SQLite schema"
    missing = {k: v for k, v in sqlite_guards.items() if pg_guards.get(k) != v}
    assert missing == {}, f"twin drift (sqlite vs postgres): {sorted(missing)}"


# --------------------------------------------------------------------------------------
# 2. behavioural — seed, then prove refusal *and* acceptance
# --------------------------------------------------------------------------------------


def _seed(conn) -> None:
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_n1', ?, 'Villa Uji', 'villa', 'Ubud', 'Bali', 'Asia/Makassar',"
        " 'SELLABLE', 'ACTIVATED', ?, ?)",
        (AGENCY, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_n2', ?, 'Villa Uji Dua', 'villa', 'Ubud', 'Bali', 'Asia/Makassar',"
        " 'SELLABLE', 'ACTIVATED', ?, ?)",
        (AGENCY, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO workforce_staff (staff_id, agency_id, employee_no, display_name, role_code,"
        " employment_type, home_property_id, status, hired_on, created_at, updated_at)"
        " VALUES ('stf_n1', ?, 'EMP-N1', 'Kadek Uji', 'HOUSEKEEPING', 'PERMANENT', 'prop_n1',"
        " 'ACTIVE', '2026-01-01', ?, ?)",
        (AGENCY, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO workforce_shift_templates (shift_template_id, agency_id, property_id,"
        " shift_code, starts_local, ends_local, effective_from)"
        " VALUES ('tpl_n1', ?, 'prop_n1', 'MORNING', '07:00', '15:00', '2026-01-01')",
        (AGENCY,),
    )
    conn.execute(
        "INSERT INTO ops_tasks (task_id, agency_id, kind, due_at, priority, status,"
        " idempotency_key, created_at, updated_at)"
        " VALUES ('task_n1', ?, 'CLEANING', ?, 'NORMAL', 'OPEN', 'idem-task_n1', ?, ?)",
        (AGENCY, LATER, NOW, NOW),
    )


def _reason_is_refused(conn, insert_sql: str, params: tuple) -> None:
    with pytest.raises(Exception) as caught:
        conn.execute(insert_sql, params)
    assert "CHECK" in type(caught.value).__name__.upper() or "constraint" in str(caught.value).lower() \
        or "CHECK" in str(caught.value).upper(), f"refusal was not a CHECK: {caught.value!r}"


# --- property eligibility: a refusal must say why --------------------------------


def test_eligibility_refusal_needs_a_reason(conn):
    _seed(conn)
    sql = ("INSERT INTO workforce_property_eligibility"
           " (agency_id, staff_id, property_id, eligible, reason, decided_by, decided_at)"
           " VALUES (?, 'stf_n1', 'prop_n1', 0, ?, 'owner_anas', ?)")
    for silent in (None, "", "  "):
        _reason_is_refused(conn, sql, (AGENCY, silent, NOW))
    # positive control: a stated refusal is accepted
    conn.execute(sql, (AGENCY, "not certified for this villa", NOW))
    # positive control: an allowance may stand silent
    conn.execute("INSERT INTO workforce_property_eligibility"
                 " (agency_id, staff_id, property_id, eligible, reason, decided_by, decided_at)"
                 " VALUES (?, 'stf_n1', 'prop_n2', 1, NULL, 'owner_anas', ?)", (AGENCY, NOW))


# --- roster: swap / cancel / no-show must say why --------------------------------


def _roster_sql(status: str, reason_value):
    return ("INSERT INTO workforce_roster_entries"
            " (roster_entry_id, agency_id, staff_id, shift_template_id, property_id, work_date,"
            " starts_at_utc, ends_at_utc, status, published_by, published_at, reason,"
            " created_at, updated_at)"
            f" VALUES ('roster_n1', ?, 'stf_n1', 'tpl_n1', 'prop_n1', '2026-11-15',"
            f" '2026-11-15T00:00:00Z', '2026-11-15T08:00:00Z', '{status}', 'owner_anas',"
            " ?, ?, ?, ?)")


@pytest.mark.parametrize("status", ["SWAPPED", "CANCELLED", "NO_SHOW"])
def test_roster_decision_without_reason_is_refused(conn, status):
    _seed(conn)
    sql = _roster_sql(status, None)
    _reason_is_refused(conn, sql, (AGENCY, NOW, None, NOW, NOW))
    # positive control: same row with a real reason is accepted
    conn.execute(sql, (AGENCY, NOW, "staff called in sick", NOW, NOW))


def test_roster_draft_needs_no_reason(conn):
    """Positive control on the other side: silence is legitimate before a decision."""
    _seed(conn)
    conn.execute("INSERT INTO workforce_roster_entries"
                 " (roster_entry_id, agency_id, staff_id, shift_template_id, property_id, work_date,"
                 " starts_at_utc, ends_at_utc, status, created_at, updated_at)"
                 " VALUES ('roster_d1', ?, 'stf_n1', 'tpl_n1', 'prop_n1', '2026-11-16',"
                 " '2026-11-16T00:00:00Z', '2026-11-16T08:00:00Z', 'DRAFT', ?, ?)",
                 (AGENCY, NOW, NOW))


# --- timesheet: rejection must say why ------------------------------------------


def test_rejected_timesheet_needs_a_reason(conn):
    _seed(conn)
    sql = ("INSERT INTO workforce_timesheets"
           " (timesheet_id, agency_id, staff_id, period_start, period_end, status,"
           " rejected_reason) VALUES ('ts_n1', ?, 'stf_n1', '2026-11-01', '2026-11-15',"
           " 'REJECTED', ?)")
    for silent in (None, "no"):
        _reason_is_refused(conn, sql, (AGENCY, silent))
    conn.execute(sql, (AGENCY, "hours do not match the roster"))


# --- task assignment: dropping a task must say why ------------------------------


def test_dropped_assignment_needs_a_reason(conn):
    _seed(conn)
    sql = ("INSERT INTO workforce_task_assignments"
           " (assignment_id, agency_id, task_id, staff_id, planned_minutes, status,"
           " assigned_by, assigned_at, drop_reason, idempotency_key)"
           " VALUES ('asg_n1', ?, 'task_n1', 'stf_n1', 60, 'DROPPED', 'owner_anas', ?, ?,"
           " 'idem-asg_n1')")
    _reason_is_refused(conn, sql, (AGENCY, NOW, None))
    conn.execute(sql, (AGENCY, NOW, "villa closed for maintenance"))


def test_completed_assignment_needs_the_completion_instant(conn):
    """Adjacent rule, same failure family: 'done' with no time is not done."""
    _seed(conn)
    with pytest.raises(Exception):
        conn.execute("INSERT INTO workforce_task_assignments"
                     " (assignment_id, agency_id, task_id, staff_id, planned_minutes, status,"
                     " assigned_by, assigned_at, completed_at_utc, idempotency_key)"
                     " VALUES ('asg_n2', ?, 'task_n1', 'stf_n1', 60, 'DONE', 'owner_anas', ?,"
                     " NULL, 'idem-asg_n2')", (AGENCY, NOW))


# --- payroll run: voiding money must say why ------------------------------------


def test_voided_payroll_run_needs_a_reason(conn):
    sql = ("INSERT INTO workforce_payroll_runs"
           " (payroll_run_id, agency_id, period_start, period_end, status, currency,"
           " void_reason, created_at) VALUES ('run_n1', ?, '2026-11-01', '2026-11-30',"
           " 'VOID', 'IDR', ?, ?)")
    for silent in (None, ""):
        _reason_is_refused(conn, sql, (AGENCY, silent, NOW))
    conn.execute(sql, (AGENCY, "duplicate run for the same period", NOW))


def test_draft_payroll_run_needs_no_reason(conn):
    conn.execute("INSERT INTO workforce_payroll_runs"
                 " (payroll_run_id, agency_id, period_start, period_end, status, currency,"
                 " created_at) VALUES ('run_d1', ?, '2026-12-01', '2026-12-31', 'DRAFT',"
                 " 'IDR', ?)", (AGENCY, NOW))

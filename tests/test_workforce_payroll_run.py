"""T2.4 — alur status payroll run.

Yang dikunci di sini: run tidak boleh melompat, tidak boleh mundur, ``PAID``
dan ``VOID`` final, pembatalan wajib alasan, dan uang hanya keluar untuk angka
yang masih cocok dengan segelnya. Setiap transisi meninggalkan satu peristiwa
audit dengan status sebelum/sesudah.
"""

from __future__ import annotations

import json

import pytest

from audit import AuditEvent, verify_chain
from primitives import DomainError, FrozenClock
from workforce.payslip import IllegalRunTransition, PayrollRunFrozen, PayslipInconsistent

from test_workforce_payslip import (
    FIXED_NOW,
    calc_for,
    open_run,
    seed_package,
    writer,
)

SECOND_STAFF = "stf_2"


# --------------------------------------------------------------------------------------
# pembantu
# --------------------------------------------------------------------------------------


def calculated_run(conn):
    """Run dengan satu slip tersimpan: status ``CALCULATED``."""
    seed_package(conn)
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")
    return w, w.get_run(run["payroll_run_id"]), slip


def approved_run(conn):
    w, run, slip = calculated_run(conn)
    w.transition_run(
        run["payroll_run_id"],
        "APPROVED",
        actor="finance_lead",
        actor_role="PAYROLL_APPROVER",
        expected_status="CALCULATED",
    )
    return w, w.get_run(run["payroll_run_id"]), slip


def run_events(conn, run_id: str) -> list[AuditEvent]:
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE entity_id = ? AND action = ? ORDER BY seq",
        (run_id, "WORKFORCE_PAYROLL_RUN_STATUS_CHANGED"),
    ).fetchall()
    return [AuditEvent.from_row(row) for row in rows]


# --------------------------------------------------------------------------------------
# jalur bahagia
# --------------------------------------------------------------------------------------


def test_run_walks_calculated_to_approved_to_paid_and_stamps_each_step(conn):
    seed_package(conn)
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    run = open_run(conn, w)
    slip = w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")

    approved = w.transition_run(run["payroll_run_id"], "APPROVED", actor="finance_lead", actor_role="PAYROLL_APPROVER")
    assert approved["status"] == "APPROVED"
    assert approved["approved_by"] == "finance_lead"
    assert approved["approved_at"] == "2026-11-15T00:00:00+00:00"
    assert approved["calculated_by"] == "ops_lead"

    paid = w.transition_run(run["payroll_run_id"], "PAID", actor="finance_lead")
    assert paid["status"] == "PAID"
    assert paid["paid_at"] == "2026-11-15T00:00:00+00:00"
    assert paid["slip_count"] == 1
    assert paid["total_gross_minor"] == slip["gross_minor"]
    assert paid["total_net_minor"] == slip["net_minor"]


def test_lowercase_target_is_normalised_not_silently_ignored(conn):
    w, run, _ = calculated_run(conn)
    out = w.transition_run(run["payroll_run_id"], " approved ", actor="finance_lead")
    assert out["status"] == "APPROVED"


def test_void_from_calculated_keeps_payslips_and_records_the_reason(conn):
    w, run, slip = calculated_run(conn)
    voided = w.transition_run(
        run["payroll_run_id"], "VOID", actor="finance_lead", reason="wrong period"
    )
    assert voided["status"] == "VOID"
    assert voided["void_reason"] == "wrong period"
    assert [row["payslip_id"] for row in w.for_run(run["payroll_run_id"])] == [slip["payslip_id"]]


# --------------------------------------------------------------------------------------
# yang harus ditolak
# --------------------------------------------------------------------------------------


def test_run_without_payslips_never_becomes_calculated(conn):
    seed_package(conn)
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    run = open_run(conn, w)
    assert run["status"] == "DRAFT"

    with pytest.raises(IllegalRunTransition) as err:
        w.transition_run(run["payroll_run_id"], "CALCULATED", actor="ops_lead")
    assert "without payslips" in str(err.value)
    assert w.get_run(run["payroll_run_id"])["status"] == "DRAFT"


@pytest.mark.parametrize("target", ["PAID", "DRAFT", "CALCULATED"])
def test_skipping_or_reversing_a_status_is_refused(conn, target):
    w, run, _ = calculated_run(conn)
    with pytest.raises(IllegalRunTransition) as err:
        w.transition_run(run["payroll_run_id"], target, actor="finance_lead")
    assert err.value.details["current_status"] == "CALCULATED"
    assert w.get_run(run["payroll_run_id"])["status"] == "CALCULATED"


def test_void_without_a_reason_is_refused(conn):
    w, run, _ = calculated_run(conn)
    with pytest.raises(IllegalRunTransition) as err:
        w.transition_run(run["payroll_run_id"], "VOID", actor="finance_lead", reason="  ")
    assert err.value.details["reason_min_len"] == 3
    assert w.get_run(run["payroll_run_id"])["status"] == "CALCULATED"


def test_unknown_target_status_is_refused(conn):
    w, run, _ = calculated_run(conn)
    with pytest.raises(IllegalRunTransition) as err:
        w.transition_run(run["payroll_run_id"], "SETTLED", actor="finance_lead")
    assert err.value.details["target_status"] == "SETTLED"


def test_stale_expected_status_is_refused_instead_of_overwritten(conn):
    w, run, _ = calculated_run(conn)
    with pytest.raises(IllegalRunTransition) as err:
        w.transition_run(
            run["payroll_run_id"], "APPROVED", actor="finance_lead", expected_status="DRAFT"
        )
    assert err.value.details == {
        "payroll_run_id": run["payroll_run_id"],
        "expected": "DRAFT",
        "actual": "CALCULATED",
    }


def test_paid_run_is_final_for_both_forward_and_void(conn):
    w, run, _ = approved_run(conn)
    w.transition_run(run["payroll_run_id"], "PAID", actor="finance_lead")
    for target in ("VOID", "CALCULATED", "PAID"):
        with pytest.raises(PayrollRunFrozen):
            w.transition_run(run["payroll_run_id"], target, actor="finance_lead", reason="late fix")
    assert w.get_run(run["payroll_run_id"])["status"] == "PAID"


def test_void_run_refuses_new_payslips(conn):
    w, run, _ = calculated_run(conn)
    w.transition_run(run["payroll_run_id"], "VOID", actor="finance_lead", reason="duplicate run")
    with pytest.raises(PayrollRunFrozen):
        w.issue(run["payroll_run_id"], calc_for(conn), issued_by="ops_lead")


def test_payment_refuses_a_slip_whose_numbers_were_edited_after_approval(conn):
    w, run, slip = approved_run(conn)
    conn.execute(
        "UPDATE workforce_payslips SET gross_minor = gross_minor + 1, net_minor = net_minor + 1"
        " WHERE payslip_id = ?",
        (slip["payslip_id"],),
    )
    with pytest.raises(PayslipInconsistent) as err:
        w.transition_run(run["payroll_run_id"], "PAID", actor="finance_lead")
    assert err.value.details["payslip_id"] == slip["payslip_id"]
    assert "gross_minor" in err.value.details["drift"]
    assert w.get_run(run["payroll_run_id"])["status"] == "APPROVED"


# --------------------------------------------------------------------------------------
# total & audit
# --------------------------------------------------------------------------------------


def test_approval_recomputes_totals_from_stored_slips_not_from_the_caller(conn):
    w, run, first = calculated_run(conn)
    seed_package(conn, staff_id=SECOND_STAFF, monthly=6_000_000)
    second = w.issue(run["payroll_run_id"], calc_for(conn, staff_id=SECOND_STAFF), issued_by="ops_lead")

    approved = w.transition_run(run["payroll_run_id"], "APPROVED", actor="finance_lead")
    assert approved["slip_count"] == 2
    assert approved["total_gross_minor"] == first["gross_minor"] + second["gross_minor"]
    assert approved["total_net_minor"] == first["net_minor"] + second["net_minor"]
    stored = conn.execute(
        "SELECT total_gross_minor, total_net_minor FROM workforce_payroll_runs WHERE payroll_run_id = ?",
        (run["payroll_run_id"],),
    ).fetchone()
    assert tuple(stored) == (approved["total_gross_minor"], approved["total_net_minor"])


def test_every_transition_leaves_one_audit_event_with_before_and_after(conn):
    w, run, _ = approved_run(conn)
    w.transition_run(run["payroll_run_id"], "PAID", actor="finance_lead")

    events = run_events(conn, run["payroll_run_id"])

    def after(event: AuditEvent) -> dict:
        assert event.after_state is not None
        return json.loads(event.after_state)

    def before(event: AuditEvent) -> dict:
        assert event.before_state is not None
        return json.loads(event.before_state)

    assert [after(e)["status"] for e in events] == ["APPROVED", "PAID"]
    assert before(events[0])["status"] == "CALCULATED"
    assert before(events[1])["status"] == "APPROVED"
    assert [e.actor for e in events] == ["finance_lead", "finance_lead"]
    assert events[0].actor_role == "PAYROLL_APPROVER"
    assert events[0].reason == "payroll run CALCULATED -> APPROVED"
    assert after(events[1])["total_net_minor"] > 0

    report = verify_chain(AuditEvent.from_row(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY seq"))
    assert report.count == conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]


def test_refused_transition_leaves_no_audit_event(conn):
    w, run, _ = calculated_run(conn)
    with pytest.raises(IllegalRunTransition):
        w.transition_run(run["payroll_run_id"], "PAID", actor="finance_lead")
    assert run_events(conn, run["payroll_run_id"]) == []


def test_actor_is_required_for_every_transition(conn):
    w, run, _ = calculated_run(conn)
    with pytest.raises(DomainError) as err:
        w.transition_run(run["payroll_run_id"], "APPROVED", actor="")
    assert err.value.code == "MISSING_FIELD"
    assert w.get_run(run["payroll_run_id"])["status"] == "CALCULATED"


# --------------------------------------------------------------------------------------
# detail galat run dibuka: rata, bukan bersarang di bawah kunci "details"
# --------------------------------------------------------------------------------------


def test_open_run_reports_a_backwards_period_as_flat_details(conn):
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    with pytest.raises(PayslipInconsistent) as err:
        w.open_run("2026-11-30", "2026-11-01", "IDR", created_by="ops_lead")
    assert err.value.details == {"period_start": "2026-11-30", "period_end": "2026-11-01"}


def test_open_run_reports_a_short_currency_code_as_flat_details(conn):
    w = writer(conn, clock=FrozenClock(FIXED_NOW))
    with pytest.raises(PayslipInconsistent) as err:
        w.open_run("2026-11-01", "2026-11-30", "ID", created_by="ops_lead")
    assert err.value.details == {"currency": "ID"}

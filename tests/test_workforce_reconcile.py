"""Gate G2: rekonsiliasi upah 0 selisih pada dataset sintetis 30 hari x 12 staf.

Yang dikunci di sini, dan mengapa
---------------------------------
Gerbang G2 hanya bermakna kalau **bisa gagal**. Karena itu setiap kelas cacat yang
diklaim terdeteksi oleh ``scripts/workforce_reconcile.py`` diuji dengan cara
merusak datanya lebih dulu, lalu memastikan rekonsiliasi menandai tahap yang tepat:

* menit timesheet dirusak -> R1;
* nominal baris slip dirusak -> R3 (baris) dan R4 (tajuk);
* nominal tarif dirusak -> R3 (bruto);
* tajuk slip dirusak -> R4 dan R5;
* event audit slip dihapus -> R6;
* rantai audit disunting -> R7.

Tanpa kontrol negatif ini, keluaran "0 temuan" tidak membedakan sistem yang bersih
dari pemindai yang tidak bekerja.

Seluruh data sintetis: nama ``Ops Staff NN``, nominal bulat, dan angka policy pajak
bertanda ``synthetic`` di payload-nya. Tidak ada satu pun upah orang nyata di sini.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "workforce_reconcile.py"


def _load() -> Any:
    """Muat skrip gerbang sebagai modul.

    Modul wajib terdaftar di ``sys.modules`` sebelum dieksekusi: ``@dataclass``
    mencari module-nya sendiri untuk menyelesaikan anotasi, dan tanpa pendaftaran
    itu pemuatan gagal dengan ``'NoneType' object has no attribute '__dict__'``.
    """
    spec = importlib.util.spec_from_file_location("workforce_reconcile", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


G2 = _load()
AGENCY = G2.AGENCY


class World:
    """Satu dunia sintetis yang sudah lengkap: staf, kerja, slip, run, audit."""

    def __init__(self, tamper_before_issue=None) -> None:
        """``tamper_before_issue`` menyisipkan kerusakan SEBELUM slip terbit.

        Ini satu-satunya cara menguji batas gerbang: apa pun yang berubah setelah
        slip terbit sudah tertangkap R3 (slip tidak lagi cocok dengan tarif/policy),
        tetapi perubahan yang terjadi lebih dulu menghasilkan slip yang konsisten
        dengan data yang salah.
        """
        self.conn = G2.connect_sqlite(":memory:")
        self.clock = G2.FrozenClock.at(G2.CLOCK_NOW)
        self.ids = G2.IdFactory(deterministic=True, seed="reconcile")
        self.writer = G2.PayslipWriter(
            self.conn, agency_id=AGENCY, clock=self.clock, ids=self.ids
        )
        G2.seed_world(self.conn, self.clock.now().isoformat())
        self.extra = G2.build_attendance(self.conn, self.clock, self.ids)
        self.sheets = G2.close_timesheets(self.conn, self.clock, self.ids)
        if tamper_before_issue is not None:
            tamper_before_issue(self.conn)
        G2.issue_slips(self.conn, self.writer, self.clock, self.ids, self.sheets)

    def check(self):
        report = G2.ReconcileReport()
        G2.reconcile(self.conn, report, self.writer)
        G2.summarise(self.conn, report, {"unrostered_days": self.extra["unrostered_days"]})
        return report

    def close(self) -> None:
        self.conn.close()


@pytest.fixture()
def world():
    instance = World()
    yield instance
    instance.close()


def stages(report) -> set[str]:
    return {finding.stage for finding in report.findings}


# --------------------------------------------------------------------------- #
# Gerbang lulus saat data bersih
# --------------------------------------------------------------------------- #
def test_a_clean_dataset_reconciles_with_zero_difference(world):
    report = world.check()
    assert report.findings == [], [
        (f.stage, f.subject, f.expected, f.actual) for f in report.findings
    ]


def test_every_stage_actually_ran_and_checked_something(world):
    """Tahap yang tidak memeriksa apa pun tidak boleh dihitung sebagai lulus."""
    report = world.check()
    expected_stages = {
        "R1 timesheet minutes",
        "R2 timesheet -> slip minutes",
        "R3 rate+policy -> slip gross",
        "R3 rate+policy -> slip lines",
        "R4 lines -> slip header",
        "R5 slips -> run total",
        "R6 trace",
        "R7 audit chain",
        "R8 policy+rate integrity",
    }
    assert expected_stages <= set(report.checks)
    for stage in expected_stages:
        assert report.checks[stage] > 0, stage


def test_the_dataset_is_the_size_the_gate_claims(world):
    facts = world.check().facts["dataset"]
    assert facts["staff"] == 12
    assert facts["properties"] == 2
    assert facts["runs"] == 6, "1 run bulanan + 5 run pekanan"
    assert facts["payslips"] == 36
    assert facts["timesheets"] == 60
    assert facts["gross_minor"] > 0 and facts["net_minor"] > 0
    assert facts["net_minor"] == facts["gross_minor"] - facts["deduction_minor"]


def test_the_dataset_covers_all_thirty_days(world):
    rows = world.conn.execute(
        "SELECT MIN(occurred_at_utc), MAX(occurred_at_utc) FROM workforce_attendance_events"
    ).fetchone()
    assert str(rows[0])[:10] == "2026-11-01"
    assert str(rows[1])[:10] == "2026-11-30"


def test_no_run_is_paid_in_this_sprint(world):
    """Aturan sprint: ``PAID`` di luar lingkup; gerbang berhenti di ``APPROVED``."""
    statuses = [
        str(row[0])
        for row in world.conn.execute("SELECT status FROM workforce_payroll_runs")
    ]
    assert set(statuses) <= {"CALCULATED", "APPROVED", "VOID"}
    assert "PAID" not in statuses


def test_both_run_shapes_are_exercised_with_their_own_trace(world):
    monthly = [row for row in world.conn.execute(
        "SELECT * FROM workforce_payroll_runs WHERE period_start = '2026-11-01'"
        " AND period_end = '2026-11-30'")]
    assert len(monthly) == 1
    monthly_run_id = str(monthly[0]["payroll_run_id"])
    monthly_slips = [row for row in world.conn.execute(
        "SELECT * FROM workforce_payslips WHERE payroll_run_id = ?", (monthly_run_id,))]
    assert len(monthly_slips) == 6
    assert all(row["timesheet_id"] is None for row in monthly_slips), (
        "run bulanan merangkum lima timesheet pekanan; tidak ada satu id tunggal"
    )

    weekly = [row for row in world.conn.execute(
        "SELECT * FROM workforce_payroll_runs WHERE payroll_run_id != ?", (monthly_run_id,))]
    assert len(weekly) == 5
    weekly_slips = [row for row in world.conn.execute(
        "SELECT * FROM workforce_payslips WHERE payroll_run_id != ?", (monthly_run_id,))]
    assert weekly_slips and all(row["timesheet_id"] is not None for row in weekly_slips)


def test_one_dataset_run_is_reproducible(world):
    """Dataset yang tidak reproducible membuat gerbangnya tidak bisa dibandingkan."""
    first = world.check().facts["dataset"]
    second = World()
    try:
        other = second.check().facts["dataset"]
    finally:
        second.close()
    for key in ("gross_minor", "deduction_minor", "net_minor", "payable_minutes",
                "payslips", "payslip_lines", "timesheets", "attendance_events"):
        assert first[key] == other[key], key


# --------------------------------------------------------------------------- #
# Kontrol negatif: gerbang harus benar-benar bisa gagal
# --------------------------------------------------------------------------- #
def test_tampered_timesheet_minutes_are_caught_by_r1(world):
    victim = world.conn.execute(
        "SELECT * FROM workforce_timesheets ORDER BY staff_id LIMIT 1").fetchone()
    world.conn.execute(
        "UPDATE workforce_timesheets SET regular_minutes = regular_minutes + 60"
        " WHERE timesheet_id = ?", (str(victim["timesheet_id"]),))
    report = world.check()
    assert "R1 timesheet minutes" in stages(report)
    # diff = yang tersimpan - hasil hitung ulang dari punch; semua simpangan +60.
    assert all(f.diff == 60 for f in report.for_stage("R1 timesheet minutes"))


def test_tampered_rate_amount_is_caught_by_r3(world):
    victim = world.conn.execute(
        "SELECT * FROM workforce_pay_rates WHERE component_code = 'BASE_DAILY' LIMIT 1"
    ).fetchone()
    world.conn.execute(
        "UPDATE workforce_pay_rates SET amount_minor = amount_minor + 1000 WHERE pay_rate_id = ?",
        (str(victim["pay_rate_id"]),),
    )
    report = world.check()
    assert "R3 rate+policy -> slip gross" in stages(report)


def test_tampered_payslip_line_is_caught_by_r3_and_r4(world):
    victim = world.conn.execute(
        "SELECT * FROM workforce_payslip_lines WHERE kind = 'EARNING' LIMIT 1").fetchone()
    world.conn.execute(
        "UPDATE workforce_payslip_lines SET amount_minor = amount_minor + 7000"
        " WHERE payslip_line_id = ?", (str(victim["payslip_line_id"]),))
    report = world.check()
    assert "R3 rate+policy -> slip lines" in stages(report)
    assert "R4 lines -> slip header" in stages(report)


def test_the_database_refuses_an_arithmetically_impossible_slip_header(world):
    """Lapis pertama: CHECK constraint menolak tajuk yang tidak konsisten."""
    victim = world.conn.execute(
        "SELECT * FROM workforce_payslips ORDER BY staff_id LIMIT 1").fetchone()
    with pytest.raises(Exception) as excinfo:
        world.conn.execute(
            "UPDATE workforce_payslips SET net_minor = net_minor + 500000 WHERE payslip_id = ?",
            (str(victim["payslip_id"]),),
        )
    assert "ck_workforce_payslip_arithmetic" in str(excinfo.value)


def test_a_consistent_but_wrong_slip_header_is_caught_by_r4_and_r5(world):
    """Lapis kedua: penyerang yang menjaga aritmetika tetap rapi tetap tertangkap.

    Bruto dan bersih dinaikkan bersama sehingga CHECK constraint lolos — yang tidak
    bisa ditipu adalah baris slip, karena baris dijumlahkan ulang dari bukti.
    """
    victim = world.conn.execute(
        "SELECT * FROM workforce_payslips ORDER BY staff_id LIMIT 1").fetchone()
    world.conn.execute(
        "UPDATE workforce_payslips SET gross_minor = gross_minor + 500000,"
        " net_minor = net_minor + 500000 WHERE payslip_id = ?",
        (str(victim["payslip_id"]),),
    )
    report = world.check()
    assert "R4 lines -> slip header" in stages(report)
    assert "R5 slips -> run total" in stages(report)


def test_a_policy_payload_edited_in_place_is_caught_by_r8_and_stops_r3(world):
    """Mengubah ``tax_percent`` tanpa versi baru: checksum tidak lagi cocok.

    Ini serangan yang paling menarik secara bisnis — menurunkan/menaikkan potongan
    tanpa meninggalkan jejak versi. R8 membandingkan checksum tersimpan dengan
    checksum yang dihitung dari payload, dan R3 menolak menghitung ulang memakai
    policy yang tidak bisa dipercaya (temuan, bukan traceback).
    """
    world.conn.execute(
        "UPDATE workforce_policy_versions SET payload_json ="
        " replace(payload_json, '\"5\"', '\"7\"') WHERE policy_code = 'PAYROLL_TAX'")
    report = world.check()
    assert "R8 policy+rate integrity" in stages(report)
    assert "R3 rate+policy -> slip gross" in stages(report)


def test_the_schema_refuses_a_rate_without_an_approver(world):
    """Lapis pertama: NULL dan string terlalu pendek ditolak constraint."""
    with pytest.raises(Exception) as excinfo:
        world.conn.execute(
            "UPDATE workforce_pay_rates SET approved_by = NULL WHERE component_code = 'BASE_DAILY'")
    assert "approved_by" in str(excinfo.value)
    with pytest.raises(Exception) as excinfo:
        world.conn.execute(
            "UPDATE workforce_pay_rates SET approved_by = '' WHERE component_code = 'BASE_DAILY'")
    assert "approved_by" in str(excinfo.value)


def test_a_rate_that_lost_its_approver_is_caught_by_r8(world):
    """Lapis kedua: dengan constraint dimatikan, R8 yang menangkap tarif tak berpenanggung.

    ``PRAGMA ignore_check_constraints`` menyerupai operator/penyerang yang memegang
    basis data secara langsung. Tanpa R8, tarif tanpa penanggung jawab akan lolos sebagai
    "tarif yang sah" — padahal tarif yang tidak bisa ditunjuk pengesahnya bukan bukti.
    """
    world.conn.execute("PRAGMA ignore_check_constraints = ON")
    world.conn.execute(
        "UPDATE workforce_pay_rates SET approved_by = '' WHERE component_code = 'BASE_DAILY'")
    report = world.check()
    assert "R8 policy+rate integrity" in stages(report)


def test_internal_consistency_cannot_vouch_for_a_wrong_but_signed_rate(world):
    """Batas gerbang ini, dikunci sebagai uji — bukan disembunyikan.

    Tarif yang salah **sebelum** slip terbit akan menghasilkan slip yang konsisten
    dengan tarif itu, sehingga R1–R5 tidak punya alasan untuk mengeluh: yang
    diperiksa adalah kecocokan slip dengan tarif dan policy *yang ada di basis data*,
    bukan apakah tarifnya sesuai kontrak owner. Yang bisa dikatakan gerbang ini adalah
    "tarif ini utuh dan ditandatangani" (R8). Membandingkan tarif dengan rate card yang
    disetujui owner adalah kontrol lain — keputusan manusia, di luar lingkup rekonsiliasi.
    """
    def wrong_rate(conn) -> None:
        conn.execute(
            "UPDATE workforce_pay_rates SET amount_minor = amount_minor + 7000"
            " WHERE component_code = 'BASE_DAILY'")

    world = World(tamper_before_issue=wrong_rate)
    try:
        report = world.check()
        assert report.findings == [], (
            "kalau ini gagal, ada tahap yang tiba-tiba bisa menilai kewajaran tarif: "
            f"{[(f.stage, f.subject) for f in report.findings]}"
        )
        # R8 tetap hijau: tarifnya memang utuh dan ditandatangani — hanya salah.
        assert report.checks["R8 policy+rate integrity"] > 0
    finally:
        world.close()


def _disarm_append_only_guard(conn) -> None:
    """Menyerupai penyerang yang punya hak DDL: trigger dulu, lalu data.

    Trigger append-only adalah lapis pertama dan sudah menolak UPDATE/DELETE
    (lihat ``test_audit_events_reject_tampering_outright``). Lapis kedua adalah
    rantai hash; tes di bawah membuktikan lapis kedua masih menangkap bila lapis
    pertama sudah dilewati.
    """
    conn.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_delete")


def test_audit_events_reject_tampering_outright(world):
    with pytest.raises(Exception) as excinfo:
        world.conn.execute(
            "UPDATE audit_events SET reason = 'edited' WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'")
    assert "append-only" in str(excinfo.value).lower()


def test_a_missing_payslip_audit_event_is_caught_by_r6(world):
    victim = world.conn.execute(
        "SELECT entity_id FROM audit_events WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'"
        " ORDER BY seq LIMIT 1").fetchone()
    _disarm_append_only_guard(world.conn)
    world.conn.execute(
        "DELETE FROM audit_events WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'"
        " AND entity_id = ?", (str(victim["entity_id"]),))
    report = world.check()
    assert "R6 trace" in stages(report)


def test_a_rewritten_audit_row_is_caught_by_r7(world):
    _disarm_append_only_guard(world.conn)
    world.conn.execute(
        "UPDATE audit_events SET reason = 'reason edited after the fact'"
        " WHERE action = 'WORKFORCE_PAYSLIP_ISSUED'")
    report = world.check()
    assert "R7 audit chain" in stages(report)


def test_a_slip_that_never_got_a_seal_is_caught(world):
    """Slip tanpa event issu tidak punya segel — harus terlihat, bukan dianggap wajar."""
    victim = world.conn.execute(
        "SELECT * FROM workforce_payslips ORDER BY staff_id LIMIT 1").fetchone()
    _disarm_append_only_guard(world.conn)
    world.conn.execute(
        "DELETE FROM audit_events WHERE entity_id = ?", (str(victim["payslip_id"]),))
    report = world.check()
    assert "R6 trace" in stages(report)

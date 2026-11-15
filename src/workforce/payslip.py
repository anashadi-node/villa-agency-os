"""Slip gaji: satu-satunya penulis ``workforce_payslips`` + segelnya (T2.3).

Kontrak modul:

1. **Satu orang, satu slip, satu run.** Kunci unik ``(payroll_run_id, staff_id)``
   sudah ada di skema; modul ini tidak pernah menimpa slip yang sudah ada.
   Menjalankan ulang hitung untuk orang yang sama adalah ``PayslipExists``,
   bukan pembayaran kedua dan bukan ``UPDATE`` diam-diam.
2. **Angka slip dihitung ulang dari barisnya, bukan dipercaya dari pemanggil.**
   ``PayrollCalculation`` yang diserahkan diperiksa: jumlah baris ``EARNING``
   harus sama dengan ``gross_minor``, jumlah ``DEDUCTION``+``TAX`` sama dengan
   ``deduction_minor``, dan ``net = gross - deduction``. Kalau tidak cocok,
   slip tidak dibuat — objek hitung buatan tangan tidak boleh jadi gaji orang.
3. **Run yang sudah dibekukan tidak menerima slip baru.** ``APPROVED`` dan
   ``PAID`` adalah keputusan manusia; ``VOID`` adalah pembatalan. Ketiganya
   menolak dengan ``PayrollRunFrozen``.
4. **Slip hanya dari timesheet yang bisa dibayar.** Kalau ``timesheet_id``
   diberikan, statusnya harus ``APPROVED`` atau ``LOCKED``, dan timesheet itu
   harus milik orang yang sama di agency yang sama. Membayar periode yang belum
   disetujui berarti membayar klaim, bukan bukti.
5. **Segel = sidik jari hitung, dicatat di rantai audit.** ``digest()`` dari
   T2.2 masuk ke ``audit_events.after_state`` dan ``evidence_ref`` dalam
   transaksi yang sama dengan slipnya, sehingga tidak ada slip tanpa jejak dan
   tidak ada jejak tanpa slip. ``verify_seal()`` menghitung ulang sidik jari dari
   jejak audit dan membandingkan setiap baris tersimpan dengannya.
6. **Total run selalu dihitung ulang dari slip tersimpan**, tidak pernah dari
   argumen pemanggil. Total yang bisa melenceng dari isinya adalah laporan yang
   tidak bisa dipertanggungjawabkan ke pemilik villa.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Mapping, Optional

from audit import append_event
from primitives import (
    CurrencyMismatch,
    EntityNotFound,
    IdFactory,
    SystemClock,
    fingerprint,
    parse_iso_date,
    require_text,
    to_iso,
    to_iso_date,
)
from workforce.payroll_calc import (
    EARNING,
    NET_REDUCING_KINDS,
    PayrollCalculation,
)
from workforce.repository import WorkforceError, require_identifier, write_transaction

#: Status run yang masih boleh menerima slip.
OPEN_RUN_STATUSES = ("DRAFT", "CALCULATED")
#: Status run yang menolak slip baru: keputusan manusia sudah diambil.
FROZEN_RUN_STATUSES = ("APPROVED", "PAID", "VOID")
#: Transisi status run yang sah (T2.4). Kunci = status sekarang, nilai = tujuan.
#: ``PAID`` dan ``VOID`` final: tidak ada jalan kembali ke belakang.
RUN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("CALCULATED", "VOID"),
    "CALCULATED": ("APPROVED", "VOID"),
    "APPROVED": ("PAID", "VOID"),
    "PAID": (),
    "VOID": (),
}
#: Panjang minimum alasan pembatalan run (selaras CHECK di skema).
VOID_REASON_MIN_LEN = 3
#: Status timesheet yang boleh dibayar.
PAYABLE_TIMESHEET_STATUSES = ("APPROVED", "LOCKED")

ACTION_ISSUED = "WORKFORCE_PAYSLIP_ISSUED"
ACTION_RUN_OPENED = "WORKFORCE_PAYROLL_RUN_OPENED"
ACTION_RUN_TRANSITION = "WORKFORCE_PAYROLL_RUN_STATUS_CHANGED"
SEAL_DOMAIN = "workforce.payroll"
ENTITY_TYPE = "workforce_payslip"
ENTITY_RUN = "workforce_payroll_run"


class PayslipError(WorkforceError):
    """Kegagalan yang disengaja saat menerbitkan slip gaji."""

    code = "WORKFORCE_PAYSLIP_ERROR"


class PayslipExists(PayslipError):
    code = "WORKFORCE_PAYSLIP_EXISTS"


class PayrollRunFrozen(PayslipError):
    code = "WORKFORCE_PAYROLL_RUN_FROZEN"


class PayslipInconsistent(PayslipError):
    code = "WORKFORCE_PAYSLIP_INCONSISTENT"


class EmptyPayslip(PayslipError):
    code = "WORKFORCE_PAYSLIP_EMPTY"


class TimesheetNotPayable(PayslipError):
    code = "WORKFORCE_TIMESHEET_NOT_PAYABLE"


class IllegalRunTransition(PayslipError):
    """Status run dilompati, dibalik, atau diminta tanpa alasan (T2.4)."""

    code = "WORKFORCE_PAYROLL_ILLEGAL_TRANSITION"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    name = require_identifier(table, "table")
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def _sum_minor(lines: Any, kinds: tuple[str, ...]) -> int:
    return sum(int(line.amount_minor) for line in lines if line.kind in kinds)


class PayslipWriter:
    """Terbitkan slip gaji dari satu ``PayrollCalculation`` yang sudah jadi.

    ``issue()`` adalah satu-satunya jalur tulis. Tidak ada ``update``/``delete``:
    slip yang salah dibatalkan lewat run baru, bukan dengan menyunting angka.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        agency_id: str = "agency_default",
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        verify_on_append: bool = True,
    ) -> None:
        self.conn = conn
        self.agency_id = require_text(agency_id, "agency_id", max_len=64)
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else IdFactory()
        self._verify_on_append = bool(verify_on_append)

    # -- waktu & id -------------------------------------------------------- #
    def _now_iso(self) -> str:
        return to_iso(self._clock.now())

    # -- baca -------------------------------------------------------------- #
    def _row(self, sql: str, params: tuple[Any, ...], *, missing: str) -> dict[str, Any]:
        cursor = self.conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise EntityNotFound(missing, entity=missing.split(" ")[0])
        return dict(zip([d[0] for d in cursor.description], row))

    def get_run(self, payroll_run_id: str) -> dict[str, Any]:
        run_id = require_text(payroll_run_id, "payroll_run_id", max_len=64)
        return self._row(
            "SELECT * FROM workforce_payroll_runs WHERE agency_id = ? AND payroll_run_id = ?",
            (self.agency_id, run_id),
            missing=f"payroll run {run_id}",
        )

    def get(self, payslip_id: str) -> dict[str, Any]:
        slip_id = require_text(payslip_id, "payslip_id", max_len=64)
        return self._row(
            "SELECT * FROM workforce_payslips WHERE agency_id = ? AND payslip_id = ?",
            (self.agency_id, slip_id),
            missing=f"payslip {slip_id}",
        )

    def lines(self, payslip_id: str) -> list[dict[str, Any]]:
        slip = self.get(payslip_id)
        selected = _column_names(self.conn, "workforce_payslip_lines")
        wanted = [
            "component_code",
            "kind",
            "amount_minor",
            "quantity_minutes",
            "basis_note",
            "policy_version_id",
        ]
        columns = [name for name in wanted if name in selected]
        sql = (
            f"SELECT {', '.join(columns)} FROM workforce_payslip_lines"
            " WHERE agency_id = ? AND payslip_id = ? ORDER BY kind, component_code"
        )
        rows = self.conn.execute(sql, (self.agency_id, str(slip["payslip_id"])))
        return [dict(zip(columns, row)) for row in rows]

    def for_run(self, payroll_run_id: str) -> list[dict[str, Any]]:
        run = self.get_run(payroll_run_id)
        rows = self.conn.execute(
            "SELECT * FROM workforce_payslips WHERE agency_id = ? AND payroll_run_id = ?"
            " ORDER BY staff_id",
            (self.agency_id, str(run["payroll_run_id"])),
        )
        names = [d[0] for d in rows.description]
        return [dict(zip(names, row)) for row in rows]

    # -- run --------------------------------------------------------------- #
    def open_run(
        self,
        period_start: Any,
        period_end: Any,
        currency: str,
        *,
        created_by: str,
        actor_role: str = "PAYROLL_OPERATOR",
    ) -> tuple[dict[str, Any], bool]:
        """Buka run untuk satu periode; kalau sudah ada yang sama, kembalikan itu.

        Idempoten lewat kunci unik ``(agency_id, period_start, period_end)`` —
        bukan lewat pencarian yang bisa balapan dengan penulis lain.
        """
        actor = require_text(created_by, "created_by", max_len=64)
        role = require_text(actor_role, "actor_role", max_len=64)
        start = (
            period_start
            if isinstance(period_start, date)
            else parse_iso_date(str(period_start))
        )
        end = period_end if isinstance(period_end, date) else parse_iso_date(str(period_end))
        code = require_text(currency, "currency", max_len=3)
        if len(code) != 3:
            raise PayslipInconsistent("currency must be a three-letter code", currency=code)
        if end < start:
            raise PayslipInconsistent(
                "the period ends before it starts",
                period_start=to_iso_date(start),
                period_end=to_iso_date(end),
            )

        existing = self.conn.execute(
            "SELECT payroll_run_id, currency FROM workforce_payroll_runs"
            " WHERE agency_id = ? AND period_start = ? AND period_end = ?",
            (self.agency_id, to_iso_date(start), to_iso_date(end)),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) != code:
                raise CurrencyMismatch(
                    "a run already exists for this period in another currency",
                    payroll_run_id=str(existing[0]),
                    run_currency=str(existing[1]),
                    requested=code,
                )
            return self.get_run(str(existing[0])), False

        run_id = self._ids.new("prun")
        with write_transaction(self.conn):
            inserted = self.conn.execute(
                "INSERT INTO workforce_payroll_runs"
                " (payroll_run_id, agency_id, period_start, period_end, status, currency,"
                "  created_at) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?)"
                " ON CONFLICT (agency_id, period_start, period_end) DO NOTHING",
                (
                    run_id,
                    self.agency_id,
                    to_iso_date(start),
                    to_iso_date(end),
                    code,
                    self._now_iso(),
                ),
            )
            if inserted.rowcount == 0:  # pragma: no cover - balapan penulis lain
                raise PayslipExists(
                    "another writer opened this period run inside the same transaction",
                    period_start=to_iso_date(start),
                    period_end=to_iso_date(end),
                )
            append_event(
                self.conn,
                clock=self._clock,
                ids=self._ids,
                action=ACTION_RUN_OPENED,
                entity_type=ENTITY_RUN,
                entity_id=run_id,
                actor=actor,
                actor_role=role,
                after={
                    "period_start": to_iso_date(start),
                    "period_end": to_iso_date(end),
                    "currency": code,
                    "status": "DRAFT",
                },
                reason="payroll run opened",
                verify_on_append=self._verify_on_append,
            )
        return self.get_run(run_id), True

    def run_totals(self, payroll_run_id: str) -> dict[str, Any]:
        """Total run dihitung dari slip yang benar-benar tersimpan."""
        run = self.get_run(payroll_run_id)
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(gross_minor), 0),"
            " COALESCE(SUM(deduction_minor), 0), COALESCE(SUM(net_minor), 0)"
            " FROM workforce_payslips WHERE agency_id = ? AND payroll_run_id = ?",
            (self.agency_id, str(run["payroll_run_id"])),
        ).fetchone()
        return {
            "payroll_run_id": str(run["payroll_run_id"]),
            "slip_count": int(row[0]),
            "total_gross_minor": int(row[1]),
            "total_deduction_minor": int(row[2]),
            "total_net_minor": int(row[3]),
            "currency": str(run["currency"]),
        }

    # -- alur status run (T2.4) -------------------------------------------- #
    def transition_run(
        self,
        payroll_run_id: str,
        target_status: str,
        *,
        actor: str,
        reason: Optional[str] = None,
        actor_role: str = "PAYROLL_OPERATOR",
        expected_status: Optional[str] = None,
    ) -> dict[str, Any]:
        """Pindahkan status run satu langkah: ``DRAFT->CALCULATED->APPROVED->PAID``.

        Aturan yang ditegakkan di sini, bukan diserahkan ke pemanggil:

        * **satu langkah saja** — ``DRAFT->PAID``, ``CALCULATED->PAID``, dan
          transisi mundur ditolak ``IllegalRunTransition``;
        * ``PAID`` dan ``VOID`` final — tidak ada jalan keluar;
        * ``CALCULATED``/``APPROVED``/``PAID`` wajib punya minimal satu slip,
          karena total run dihitung ulang dari slip tersimpan, bukan dari argumen;
        * ``VOID`` wajib alasan minimal ``VOID_REASON_MIN_LEN`` karakter;
        * ``PAID`` hanya kalau ``calculated_at`` **dan** ``approved_at`` terisi,
          dan setiap slip masih cocok dengan segelnya — uang keluar hanya untuk
          angka yang belum berubah sejak diterbitkan.

        ``expected_status`` adalah penjaga balapan (optimistic check): kalau
        status tersimpan sudah berbeda dari yang dilihat pemanggil, transisi
        ditolak, bukan ditimpa.
        """
        actor_id = require_text(actor, "actor", max_len=64)
        role = require_text(actor_role, "actor_role", max_len=64)
        target = require_text(target_status, "target_status", max_len=16).strip().upper()
        if target not in RUN_TRANSITIONS:
            raise IllegalRunTransition(
                "unknown payroll run status",
                target_status=target,
                allowed=sorted(RUN_TRANSITIONS),
            )

        run = self.get_run(payroll_run_id)
        run_id = str(run["payroll_run_id"])
        current = str(run["status"])
        if expected_status is not None:
            expected = require_text(
                expected_status, "expected_status", max_len=16
            ).strip().upper()
            if expected != current:
                raise IllegalRunTransition(
                    "payroll run moved since it was read",
                    payroll_run_id=run_id,
                    expected=expected,
                    actual=current,
                )
        if current in ("PAID", "VOID"):
            raise PayrollRunFrozen(
                "a final payroll run accepts no further transition",
                payroll_run_id=run_id,
                status=current,
            )
        allowed = RUN_TRANSITIONS.get(current, ())
        if target not in allowed:
            raise IllegalRunTransition(
                "payroll run status cannot jump or go backwards",
                payroll_run_id=run_id,
                current_status=current,
                target_status=target,
                allowed=list(allowed),
            )

        note = (reason or "").strip()
        if target == "VOID":
            if len(note) < VOID_REASON_MIN_LEN:
                raise IllegalRunTransition(
                    "voiding a payroll run requires a reason",
                    payroll_run_id=run_id,
                    target_status="VOID",
                    reason_min_len=VOID_REASON_MIN_LEN,
                )
        elif not note:
            note = f"payroll run {current} -> {target}"

        totals = self.run_totals(run_id)
        if target in ("CALCULATED", "APPROVED", "PAID") and totals["slip_count"] == 0:
            raise IllegalRunTransition(
                "a payroll run without payslips never becomes payable",
                payroll_run_id=run_id,
                target_status=target,
            )
        if target == "PAID":
            if not run.get("calculated_at") or not run.get("approved_at"):
                raise IllegalRunTransition(
                    "a run is paid only after it was calculated and approved",
                    payroll_run_id=run_id,
                    calculated_at=run.get("calculated_at"),
                    approved_at=run.get("approved_at"),
                )
            for slip in self.for_run(run_id):
                seal = self.verify_seal(str(slip["payslip_id"]))
                if not seal["matches"]:
                    raise PayslipInconsistent(
                        "a payslip no longer matches its seal; the run is not payable",
                        payroll_run_id=run_id,
                        payslip_id=seal["payslip_id"],
                        drift=seal["drift"],
                    )

        before = {
            "status": current,
            "total_gross_minor": int(run["total_gross_minor"]),
            "total_net_minor": int(run["total_net_minor"]),
        }
        after = {
            "status": target,
            "slip_count": totals["slip_count"],
            "total_gross_minor": totals["total_gross_minor"],
            "total_net_minor": totals["total_net_minor"],
        }
        stamp = self._now_iso()
        columns = ["status = ?"]
        params: list[Any] = [target]
        if target in ("CALCULATED", "APPROVED", "PAID"):
            columns += ["total_gross_minor = ?", "total_net_minor = ?"]
            params += [totals["total_gross_minor"], totals["total_net_minor"]]
        if target == "CALCULATED":
            columns += ["calculated_by = ?", "calculated_at = ?"]
            params += [actor_id, stamp]
        elif target == "APPROVED":
            columns += ["approved_by = ?", "approved_at = ?"]
            params += [actor_id, stamp]
        elif target == "PAID":
            columns += ["paid_at = ?"]
            params += [stamp]
        elif target == "VOID":
            columns += ["void_reason = ?"]
            params += [note]
        params += [self.agency_id, run_id]

        with write_transaction(self.conn):
            updated = self.conn.execute(
                f"UPDATE workforce_payroll_runs SET {', '.join(columns)}"
                " WHERE agency_id = ? AND payroll_run_id = ?",
                tuple(params),
            )
            if updated.rowcount != 1:
                raise PayrollRunFrozen(
                    "payroll run disappeared during the transition",
                    payroll_run_id=run_id,
                    status=current,
                )
            append_event(
                self.conn,
                clock=self._clock,
                ids=self._ids,
                action=ACTION_RUN_TRANSITION,
                entity_type=ENTITY_RUN,
                entity_id=run_id,
                actor=actor_id,
                actor_role=role,
                before=before,
                after=after,
                reason=note,
                verify_on_append=self._verify_on_append,
            )
        result = self.get_run(run_id)
        result["slip_count"] = totals["slip_count"]
        return result

    # -- terbitkan --------------------------------------------------------- #
    def issue(
        self,
        payroll_run_id: str,
        calculation: PayrollCalculation,
        *,
        issued_by: str,
        timesheet_id: Optional[str] = None,
        actor_role: str = "PAYROLL_OPERATOR",
    ) -> dict[str, Any]:
        """Tulis satu slip + barisnya + segelnya dalam satu transaksi."""
        actor = require_text(issued_by, "issued_by", max_len=64)
        role = require_text(actor_role, "actor_role", max_len=64)
        if not isinstance(calculation, PayrollCalculation):
            raise PayslipInconsistent(
                "issue() hanya menerima PayrollCalculation",
                received=type(calculation).__name__,
            )
        run = self.get_run(payroll_run_id)
        status = str(run["status"])
        if status in FROZEN_RUN_STATUSES:
            raise PayrollRunFrozen(
                "a frozen run accepts no new payslips",
                payroll_run_id=str(run["payroll_run_id"]),
                status=status,
            )
        if status not in OPEN_RUN_STATUSES:  # pragma: no cover - skema menutup ini
            raise PayrollRunFrozen(
                "unknown run status",
                payroll_run_id=str(run["payroll_run_id"]),
                status=status,
            )

        currency = str(run["currency"])
        if calculation.currency != currency:
            raise CurrencyMismatch(
                "calculation currency differs from the run currency",
                payslip_currency=calculation.currency,
                run_currency=currency,
            )
        run_start = parse_iso_date(str(run["period_start"]))
        run_end = parse_iso_date(str(run["period_end"]))
        if (calculation.period_start, calculation.period_end) != (run_start, run_end):
            raise PayslipInconsistent(
                "calculation period differs from the run period",
                run_period_start=str(run["period_start"]),
                run_period_end=str(run["period_end"]),
                calc_period_start=to_iso_date(calculation.period_start),
                calc_period_end=to_iso_date(calculation.period_end),
            )

        lines = list(calculation.lines)
        if not lines:
            raise EmptyPayslip(
                "a calculation with no lines never becomes a payslip",
                staff_id=calculation.staff_id,
            )
        gross = _sum_minor(lines, (EARNING,))
        deduction = _sum_minor(lines, NET_REDUCING_KINDS)
        if gross != calculation.gross_minor or deduction != calculation.deduction_minor:
            raise PayslipInconsistent(
                "gross/deduction does not equal the sum of its lines",
                staff_id=calculation.staff_id,
                lines_gross_minor=gross,
                declared_gross_minor=calculation.gross_minor,
                lines_deduction_minor=deduction,
                declared_deduction_minor=calculation.deduction_minor,
            )
        if calculation.net_minor != gross - deduction:
            raise PayslipInconsistent(
                "upah bersih bukan bruto dikurangi potongan",
                staff_id=calculation.staff_id,
                gross_minor=gross,
                deduction_minor=deduction,
                net_minor=calculation.net_minor,
            )
        if gross <= 0 or calculation.net_minor <= 0:
            raise EmptyPayslip(
                "a payslip with no positive net pay is never issued",
                staff_id=calculation.staff_id,
                gross_minor=gross,
                net_minor=calculation.net_minor,
            )

        slip = self.conn.execute(
            "SELECT payslip_id FROM workforce_payslips"
            " WHERE payroll_run_id = ? AND staff_id = ?",
            (str(run["payroll_run_id"]), calculation.staff_id),
        ).fetchone()
        if slip is not None:
            raise PayslipExists(
                "this staff member already has a payslip in this run; it is not overwritten but voided through a new run",
                payroll_run_id=str(run["payroll_run_id"]),
                staff_id=calculation.staff_id,
                payslip_id=str(slip[0]),
            )

        sheet_id = self._check_timesheet(timesheet_id, calculation.staff_id)

        payslip_id = self._ids.new("slip")
        stamp = self._now_iso()
        policy_ids = sorted(
            {
                str(line.policy_version_id)
                for line in lines
                if line.policy_version_id is not None
            }
        )
        payload = calculation.to_payload()
        seal = fingerprint(payload, domain=SEAL_DOMAIN)

        with write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO workforce_payslips"
                " (payslip_id, agency_id, payroll_run_id, staff_id, timesheet_id, currency,"
                "  gross_minor, deduction_minor, net_minor, worked_minutes, overtime_minutes,"
                "  generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payslip_id,
                    self.agency_id,
                    str(run["payroll_run_id"]),
                    calculation.staff_id,
                    sheet_id,
                    currency,
                    gross,
                    deduction,
                    calculation.net_minor,
                    int(calculation.regular_minutes),
                    int(calculation.overtime_minutes),
                    stamp,
                ),
            )
            for line in lines:
                self.conn.execute(
                    "INSERT INTO workforce_payslip_lines"
                    " (payslip_line_id, agency_id, payslip_id, component_code, kind,"
                    "  amount_minor, quantity_minutes, basis_note, policy_version_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._ids.new("sline"),
                        self.agency_id,
                        payslip_id,
                        line.component_code,
                        line.kind,
                        int(line.amount_minor),
                        line.quantity_minutes,
                        line.basis_note,
                        line.policy_version_id,
                        stamp,
                    ),
                )
            totals = self.run_totals(str(run["payroll_run_id"]))
            self.conn.execute(
                "UPDATE workforce_payroll_runs SET status = 'CALCULATED',"
                " total_gross_minor = ?, total_net_minor = ?, calculated_by = ?,"
                " calculated_at = ? WHERE agency_id = ? AND payroll_run_id = ?",
                (
                    totals["total_gross_minor"],
                    totals["total_net_minor"],
                    actor,
                    stamp,
                    self.agency_id,
                    str(run["payroll_run_id"]),
                ),
            )
            append_event(
                self.conn,
                clock=self._clock,
                ids=self._ids,
                action=ACTION_ISSUED,
                entity_type=ENTITY_TYPE,
                entity_id=payslip_id,
                actor=actor,
                actor_role=role,
                after={"payload": payload, "seal": seal},
                reason=f"payroll run {run['payroll_run_id']}",
                policy_version_id=policy_ids[0] if len(policy_ids) == 1 else None,
                evidence_ref=seal,
                verify_on_append=self._verify_on_append,
            )

        stored = self.get(payslip_id)
        stored["seal"] = seal
        stored["line_count"] = len(lines)
        return stored

    def _check_timesheet(self, timesheet_id: Optional[str], staff_id: str) -> Optional[str]:
        if timesheet_id is None:
            return None
        sheet_id = require_text(timesheet_id, "timesheet_id", max_len=64)
        row = self.conn.execute(
            "SELECT staff_id, status FROM workforce_timesheets"
            " WHERE agency_id = ? AND timesheet_id = ?",
            (self.agency_id, sheet_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound(f"timesheet {sheet_id}", entity="timesheet")
        if str(row[0]) != staff_id:
            raise TimesheetNotPayable(
                "a timesheet owned by someone else cannot be paid to this staff member",
                timesheet_id=sheet_id,
                timesheet_staff_id=str(row[0]),
                payslip_staff_id=staff_id,
            )
        if str(row[1]) not in PAYABLE_TIMESHEET_STATUSES:
            raise TimesheetNotPayable(
                "only an approved timesheet can be paid",
                timesheet_id=sheet_id,
                status=str(row[1]),
            )
        return sheet_id

    # -- segel ------------------------------------------------------------- #
    def verify_seal(self, payslip_id: str) -> dict[str, Any]:
        """Bandingkan slip tersimpan dengan segelnya di rantai audit.

        Bukti yang diberikan jujur: kalau ``matches`` benar, baris tersimpan sama
        dengan yang disegel dan sidik jarinya utuh. Modul ini tidak mengklaim
        mendeteksi penulisan langsung ke database yang juga menyunting rantai
        auditnya — itu tugas verifikasi rantai (``audit.verify_chain``).
        """
        slip = self.get(payslip_id)
        row = self.conn.execute(
            "SELECT after_state, evidence_ref FROM audit_events"
            " WHERE entity_type = ? AND entity_id = ? AND action = ?"
            " ORDER BY seq DESC LIMIT 1",
            (ENTITY_TYPE, str(slip["payslip_id"]), ACTION_ISSUED),
        ).fetchone()
        if row is None:
            raise EntityNotFound(
                f"seal for payslip {slip['payslip_id']}", entity="payslip_seal"
            )
        after = row[0]
        seal = str(row[1])
        payload = _payload_from_after(after)
        recomputed = fingerprint(payload, domain=SEAL_DOMAIN)
        stored_lines = self.lines(str(slip["payslip_id"]))
        sealed_lines = list(payload.get("lines") or [])
        drift = []
        if recomputed != seal:
            drift.append("seal")
        if int(payload.get("gross_minor", -1)) != int(slip["gross_minor"]):
            drift.append("gross_minor")
        if int(payload.get("deduction_minor", -1)) != int(slip["deduction_minor"]):
            drift.append("deduction_minor")
        if int(payload.get("net_minor", -1)) != int(slip["net_minor"]):
            drift.append("net_minor")
        if len(stored_lines) != len(sealed_lines):
            drift.append("line_count")
        else:
            for stored, sealed in zip(stored_lines, _sorted_lines(sealed_lines)):
                if (
                    str(stored["component_code"]) != str(sealed.get("component_code"))
                    or str(stored["kind"]) != str(sealed.get("kind"))
                    or int(stored["amount_minor"]) != int(sealed.get("amount_minor", -1))
                ):
                    drift.append(f"line:{stored['component_code']}")
        return {
            "payslip_id": str(slip["payslip_id"]),
            "seal": seal,
            "recomputed": recomputed,
            "matches": not drift,
            "drift": drift,
        }


def _payload_from_after(after_state: Any) -> dict[str, Any]:
    import json

    if isinstance(after_state, Mapping):
        loaded: Any = dict(after_state)
    else:
        try:
            loaded = json.loads(str(after_state))
        except (TypeError, ValueError):
            loaded = None
    if not isinstance(loaded, Mapping) or not isinstance(loaded.get("payload"), Mapping):
        raise PayslipInconsistent("the audit seal carries no calculation payload")
    return dict(loaded["payload"])


def _sorted_lines(lines: Any) -> list[dict[str, Any]]:
    return sorted(
        (dict(line) for line in lines),
        key=lambda line: (str(line.get("kind")), str(line.get("component_code"))),
    )

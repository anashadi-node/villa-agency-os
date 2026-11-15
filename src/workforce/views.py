"""Role screens for Workforce Ops: supervisor, finance and owner (T2.6).

Three people look at the same payroll facts and need three different pictures:

* **supervisor** — the shift that is happening now: who is on duty, who has not
  clocked in, who is late, which timesheets are waiting for a signature;
* **finance** — the money: every payroll run, its totals, and the slips still
  short of an approval;
* **owner** — the portfolio: how many people, what payroll cost per property, and
  what is still unpaid.

Four rules shape this module, and each one is enforced by a test:

1. **Money is IDR with a USD reference, or it says why there is none.** Every
   figure goes through :mod:`money_display`, the same display layer the rest of
   the dashboard uses. The reference is read from policy
   (``commercial.fx.rates``) and disappears the moment the published rate is
   stale — this module never carries a rate of its own.
2. **No identifiers and no machine words on a screen.** Nothing here prints a
   ``staff_id``, a payroll run id, a status code or a table name. Rows carry the
   human facts; the words a reader sees live in ``templates/workforce_*.html``,
   which is also why this file holds no UI copy at all.
3. **Read-only.** Views never approve, never pay, never mutate. Decisions stay
   with the humans in the dashboard's own write paths.
4. **Money is not recomputed here.** Totals come from the committed payroll rows;
   if a total is missing the screen shows nothing rather than a guess.

The page fragments are rendered by :func:`dashboard.render_page`, so the escaping
rules, the base shell and the theme stay in one place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct import from scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from primitives import DomainError, to_iso  # noqa: E402
from schema_loader import applied_version  # noqa: E402

import money_display as money_display_module  # noqa: E402
import policy_engine as policy_engine_module  # noqa: E402
import time_display as time_display_module  # noqa: E402

# The dashboard owns the one HTML renderer in this codebase; importing it keeps the
# escaping rules, the page shell and the read-only badge shared instead of copied.
from dashboard import (  # noqa: E402
    READ_ONLY_BADGE,
    Markup,
    escape_value,
    render_page,
    rows_markup,
)

#: Screens this module can draw. The name is the template name and the page key.
SCREENS: tuple[str, ...] = ("workforce_supervisor", "workforce_finance", "workforce_owner")

#: Which dashboard role is allowed to see which screen. A role missing here sees none.
SCREEN_ROLES: Mapping[str, tuple[str, ...]] = {
    "workforce_supervisor": ("operations",),
    "workforce_finance": ("finance",),
    "workforce_owner": ("owner",),
}

#: Timesheet states that still need a person to act, in the order a supervisor wants them.
AWAITING_SIGNATURE = ("OPEN", "SUBMITTED", "REJECTED")

#: Attendance day states that mean "something is wrong with this day".
NEEDS_ATTENTION = ("LATE", "ABSENT", "INCOMPLETE")

#: Run states a finance screen still has to do something about.
UNSETTLED_RUNS = ("DRAFT", "CALCULATED", "APPROVED")

#: Run states whose slips are still waiting for an approval (a subset of the above: an
#: ``APPROVED`` run is not settled yet, but its slips no longer need a person to sign them).
AWAITING_APPROVAL_RUNS = ("DRAFT", "CALCULATED")

#: The words a reader sees for a state code.
#:
#: They live in a **data** file, not in this module, because two rules pull in opposite
#: directions: ``src/workforce/*.py`` must emit no Indonesian at all (ADR-0011 §55), while
#: the people reading these screens work in Bali and Yogyakarta — the same reason the guest
#: message catalogue is Indonesian (ADR-0037). ``scripts/check_workforce_language.py`` pins
#: the count in this file, so growing it is a recorded decision rather than a silent one.
LABELS_PATH = Path(__file__).with_name("screen_labels.json")


def screen_labels() -> dict[str, dict[str, str]]:
    """The label catalogue, read on each call so an edited catalogue takes effect at once."""
    try:
        raw = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable catalogue must not take a screen down
        return {}
    return {
        str(group): {str(code): str(text) for code, text in values.items()}
        for group, values in raw.items()
        if isinstance(values, dict)
    }


class UnknownScreen(DomainError):
    """The requested screen does not exist, or this role may not see it."""

    code = "WORKFORCE_SCREEN_NOT_ALLOWED"


def screens_for_role(role: Any) -> tuple[str, ...]:
    """The screens ``role`` may open, in a stable order (deny by default)."""
    wanted = str(role or "").strip()
    return tuple(name for name in SCREENS if wanted in SCREEN_ROLES[name])


class WorkforceViews:
    """Builds the three role screens from committed workforce rows.

    The object is deliberately stateless between calls: every method re-reads the
    database, so a page can never show a total that was true an hour ago.
    """

    def __init__(
        self,
        conn: Any,
        *,
        clock: Any,
        agency_id: str,
        templates_dir: Optional[Any] = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.agency_id = agency_id
        self.templates_dir = templates_dir

    # -- reading ----------------------------------------------------------------- #
    @staticmethod
    def _in(values: Sequence[str]) -> str:
        """``?, ?, ?`` — the one place a list of states is turned into SQL.

        Kept here so a state list exists once, in a named constant, instead of being
        re-typed inside four different queries where one copy silently drifts.
        """
        return ", ".join("?" for _ in values)

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row

    def word(self, group: str, code: Any) -> str:
        """The reader's word for a state code; an empty cell when there is no word.

        An unknown code is left blank on purpose. Inventing a word for a state this module
        does not recognise would put a guess on a screen about people's pay.
        """
        return screen_labels().get(group, {}).get(str(code), "")

    def today_local(self) -> str:
        """Today's date at the agency's clock, as ``YYYY-MM-DD``."""
        return str(self.clock.now())[:10]

    # -- money ------------------------------------------------------------------- #
    def money_reference_source(self, *, at: Any = None) -> Optional[Any]:
        """The published rates behind the USD reference, or ``None``.

        Same contract as the dashboard: the policy store is asked on every render,
        and a store that cannot answer yields rupiah alone rather than a guess.
        """
        try:
            resolver = policy_engine_module.PolicyResolver(
                self.conn, clock=self.clock, agency_id=self.agency_id
            )
        except Exception:  # noqa: BLE001 - no resolver ⇒ no reference, never a dead page
            return None
        return money_display_module.source_from_policy(
            resolver, at=at if at is not None else self.clock.now()
        )

    def money(self, minor: Any, currency: str = "IDR", *, source: Any = None) -> str:
        """``IDR 4.042.500 (≈ USD 248,77)`` — rupiah first, reference second."""
        return money_display_module.line(
            minor,
            currency,
            source=source,
            today=self.clock.now(),
        )

    def money_note(self, source: Any = None) -> str:
        """The line that names the rate and the day, or says why no figure is shown."""
        return money_display_module.footnote(source, today=self.clock.now())

    # -- shared facts ------------------------------------------------------------ #
    def _staff_names(self) -> dict[str, str]:
        return {
            str(row["staff_id"]): str(row["display_name"])
            for row in self._rows(
                "SELECT staff_id, display_name FROM workforce_staff WHERE agency_id = ?",
                (self.agency_id,),
            )
        }

    def _property_names(self) -> dict[str, str]:
        return {
            str(row["property_id"]): str(row["name"])
            for row in self._rows(
                "SELECT property_id, name FROM properties WHERE agency_id = ?",
                (self.agency_id,),
            )
        }

    def _roster_today(self) -> list[Any]:
        return self._rows(
            """
            SELECT r.staff_id, r.property_id, r.status, r.starts_at_utc, r.ends_at_utc,
                   s.shift_code, s.starts_local, s.ends_local
              FROM workforce_roster_entries r
              JOIN workforce_shift_templates s
                ON s.shift_template_id = r.shift_template_id
             WHERE r.agency_id = ?
               AND r.work_date = ?
               AND r.status IN ('PUBLISHED', 'CONFIRMED', 'SWAPPED')
             ORDER BY r.starts_at_utc, s.shift_code
            """,
            (self.agency_id, self.today_local()),
        )

    def _attendance_today(self) -> dict[str, Any]:
        return {
            str(row["staff_id"]): row
            for row in self._rows(
                "SELECT * FROM workforce_attendance_days WHERE agency_id = ? AND work_date = ?",
                (self.agency_id, self.today_local()),
            )
        }

    # -- supervisor -------------------------------------------------------------- #
    def supervisor_facts(self) -> dict[str, Any]:
        """The seven numbers a supervisor checks before the shift starts."""
        roster = self._roster_today()
        attendance = self._attendance_today()
        rostered = {str(row["staff_id"]) for row in roster}
        clocked_in = {
            staff_id
            for staff_id, row in attendance.items()
            if staff_id in rostered and row["first_in_utc"]
        }
        watching = [
            staff_id
            for staff_id in rostered
            if staff_id in attendance and str(attendance[staff_id]["status"]) in NEEDS_ATTENTION
        ]
        signatures = self._one(
            f"""
            SELECT COUNT(*) AS total FROM workforce_timesheets
             WHERE agency_id = ? AND status IN ({self._in(AWAITING_SIGNATURE)})
            """,
            (self.agency_id, *AWAITING_SIGNATURE),
        )
        return {
            "date": self.today_local(),
            "on_duty": len(rostered),
            "clocked_in": len(clocked_in),
            "not_clocked_in": len(rostered - clocked_in),
            "needs_attention": len(watching),
            "awaiting_signature": int(signatures["total"] if signatures else 0),
        }

    def supervisor_shift_rows(self) -> Markup:
        """One row per rostered person today, with the state of their day."""
        names = self._staff_names()
        properties = self._property_names()
        attendance = self._attendance_today()
        rows: list[tuple[Any, ...]] = []
        for row in self._roster_today():
            staff_id = str(row["staff_id"])
            day = attendance.get(staff_id)
            if day is None:
                state = "NOT_CLOCKED_IN"
                late = 0
            elif str(day["status"]) in NEEDS_ATTENTION:
                state = str(day["status"])
                late = int(day["late_minutes"] or 0)
            else:
                state = "PRESENT"
                late = 0
            rows.append(
                (
                    names.get(staff_id, ""),
                    properties.get(str(row["property_id"]), ""),
                    f"{row['starts_local']}–{row['ends_local']}",
                    self.word("attendance_state", state),
                    late,
                )
            )
        return rows_markup(rows, columns=5, num=(4,))

    def supervisor_signature_rows(self, *, source: Any = None) -> Markup:
        """Timesheets still waiting for a signature, oldest period first."""
        names = self._staff_names()
        rows = self._rows(
            f"""
            SELECT staff_id, period_start, period_end, status,
                   regular_minutes, overtime_minutes
              FROM workforce_timesheets
             WHERE agency_id = ? AND status IN ({self._in(AWAITING_SIGNATURE)})
             ORDER BY period_start, staff_id
            """,
            (self.agency_id, *AWAITING_SIGNATURE),
        )
        pending = []
        for row in rows:
            minutes = int(row["regular_minutes"] or 0) + int(row["overtime_minutes"] or 0)
            gross = self._gross_for_period(
                str(row["staff_id"]), str(row["period_start"]), str(row["period_end"])
            )
            pending.append(
                (
                    names.get(str(row["staff_id"]), ""),
                    str(row["period_start"]),
                    str(row["period_end"]),
                    f"{minutes / 60:.1f}",
                    self.money(gross, source=source) if gross is not None else "",
                    self.word("timesheet_status", row["status"]),
                )
            )
        return rows_markup(pending, columns=6, num=(3, 4))

    def _gross_for_period(self, staff_id: str, period_start: str, period_end: str) -> Optional[int]:
        """The gross of the slip written for this period, or ``None`` if there is none.

        A figure is either read from the committed slip or not shown: this module does
        not compute a wage the payroll kernel has not written.
        """
        row = self._one(
            """
            SELECT p.gross_minor FROM workforce_payslips p
              JOIN workforce_payroll_runs r ON r.payroll_run_id = p.payroll_run_id
             WHERE p.agency_id = ? AND p.staff_id = ?
               AND r.period_start <= ? AND r.period_end >= ?
             ORDER BY r.period_start DESC
             LIMIT 1
            """,
            (self.agency_id, staff_id, period_end, period_start),
        )
        return int(row["gross_minor"]) if row is not None else None

    def view_supervisor(self) -> dict[str, Any]:
        source = self.money_reference_source()
        return {
            **self.supervisor_facts(),
            "shift_rows": self.supervisor_shift_rows(),
            "signature_rows": self.supervisor_signature_rows(source=source),
            "money_note": escape_value(self.money_note(source)),
        }

    # -- finance ----------------------------------------------------------------- #
    def run_rows(self, *, source: Any = None, unsettled_only: bool = False) -> Markup:
        """One row per payroll run: period, shape, headcount, totals, state."""
        clause = ""
        params: list[Any] = [self.agency_id]
        if unsettled_only:
            clause = f" AND r.status IN ({self._in(UNSETTLED_RUNS)})"
            params.extend(UNSETTLED_RUNS)
        rows = self._rows(
            f"""
            SELECT r.*, COUNT(p.payslip_id) AS slip_count,
                   COALESCE(SUM(p.deduction_minor), 0) AS deduction_total
              FROM workforce_payroll_runs r
              LEFT JOIN workforce_payslips p ON p.payroll_run_id = r.payroll_run_id
             WHERE r.agency_id = ?{clause}
             GROUP BY r.payroll_run_id
             ORDER BY r.period_start DESC, r.payroll_run_id
            """,
            params,
        )
        shapes = self._run_shapes()
        built = [
            (
                f"{row['period_start']} → {row['period_end']}",
                self.word("run_shape", shapes.get(str(row["payroll_run_id"]), "")),
                int(row["slip_count"] or 0),
                self.money(row["total_gross_minor"], str(row["currency"] or "IDR"), source=source),
                self.money(row["deduction_total"], str(row["currency"] or "IDR"), source=source),
                self.money(row["total_net_minor"], str(row["currency"] or "IDR"), source=source),
                self.word("run_status", row["status"]),
            )
            for row in rows
        ]
        return rows_markup(built, columns=7, num=(2, 3, 4, 5))

    def _run_shapes(self) -> dict[str, str]:
        """Monthly or weekly, decided by the slips: a run whose slips cite a timesheet is weekly."""
        rows = self._rows(
            """
            SELECT payroll_run_id,
                   SUM(CASE WHEN timesheet_id IS NULL THEN 1 ELSE 0 END) AS without_timesheet,
                   COUNT(*) AS slips
              FROM workforce_payslips WHERE agency_id = ?
             GROUP BY payroll_run_id
            """,
            (self.agency_id,),
        )
        shapes: dict[str, str] = {}
        for row in rows:
            slips = int(row["slips"] or 0)
            without = int(row["without_timesheet"] or 0)
            if slips == 0:
                shapes[str(row["payroll_run_id"])] = "UNKNOWN"
            else:
                shapes[str(row["payroll_run_id"])] = "MONTHLY" if without else "WEEKLY"
        return shapes

    def approval_rows(self, *, source: Any = None) -> Markup:
        """Slips whose run has not been approved yet, largest net first."""
        names = self._staff_names()
        rows = self._rows(
            f"""
            SELECT p.staff_id, p.net_minor, p.currency, p.gross_minor, p.generated_at,
                   r.period_start, r.period_end, r.status
              FROM workforce_payslips p
              JOIN workforce_payroll_runs r ON r.payroll_run_id = p.payroll_run_id
             WHERE p.agency_id = ? AND r.status IN ({self._in(AWAITING_APPROVAL_RUNS)})
             ORDER BY p.net_minor DESC, p.staff_id
            """,
            (self.agency_id, *AWAITING_APPROVAL_RUNS),
        )
        return rows_markup(
            [
                (
                    names.get(str(row["staff_id"]), ""),
                    f"{row['period_start']} → {row['period_end']}",
                    self.money(row["net_minor"], str(row["currency"] or "IDR"), source=source),
                    self.word("run_status", row["status"]),
                )
                for row in rows
            ],
            columns=4,
            num=(2,),
        )

    def finance_facts(self) -> dict[str, Any]:
        """The five numbers a finance reader wants before opening any table."""
        totals = self._one(
            f"""
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(CASE WHEN status IN ({self._in(UNSETTLED_RUNS)})
                                     THEN total_net_minor ELSE 0 END), 0) AS unsettled
              FROM workforce_payroll_runs WHERE agency_id = ?
            """,
            (*UNSETTLED_RUNS, self.agency_id),
        )
        slips = self._one(
            f"""
            SELECT COUNT(*) AS waiting
              FROM workforce_payslips p
              JOIN workforce_payroll_runs r ON r.payroll_run_id = p.payroll_run_id
             WHERE p.agency_id = ? AND r.status IN ({self._in(AWAITING_APPROVAL_RUNS)})
            """,
            (self.agency_id, *AWAITING_APPROVAL_RUNS),
        )
        periods = self._one(
            """
            SELECT MIN(period_start) AS first_period, MAX(period_end) AS last_period
              FROM workforce_payroll_runs WHERE agency_id = ?
            """,
            (self.agency_id,),
        )
        return {
            "runs": int(totals["runs"] if totals else 0),
            "unsettled_minor": int(totals["unsettled"] if totals else 0),
            "waiting_slips": int(slips["waiting"] if slips else 0),
            "first_period": (periods["first_period"] if periods else None),
            "last_period": (periods["last_period"] if periods else None),
        }

    def view_finance(self) -> dict[str, Any]:
        source = self.money_reference_source()
        facts = self.finance_facts()
        return {
            **facts,
            "unsettled": self.money(facts["unsettled_minor"], source=source),
            "run_rows": self.run_rows(source=source),
            "approval_rows": self.approval_rows(source=source),
            "money_note": escape_value(self.money_note(source)),
        }

    # -- owner ------------------------------------------------------------------- #
    def property_rows(self, *, source: Any = None) -> Markup:
        """Payroll cost per property, read from the slips a property's people were paid."""
        properties = self._property_names()
        rows = self._rows(
            """
            SELECT COALESCE(s.home_property_id, '') AS property_id,
                   COUNT(DISTINCT p.staff_id) AS people,
                   COALESCE(SUM(p.gross_minor), 0) AS gross,
                   COALESCE(SUM(p.deduction_minor), 0) AS deduction,
                   COALESCE(SUM(p.net_minor), 0) AS net,
                   COUNT(p.payslip_id) AS slips,
                   p.currency AS currency
              FROM workforce_payslips p
              JOIN workforce_staff s ON s.staff_id = p.staff_id
             WHERE p.agency_id = ?
             GROUP BY property_id, p.currency
             ORDER BY net DESC, property_id
            """,
            (self.agency_id,),
        )
        return rows_markup(
            [
                (
                    properties.get(str(row["property_id"]), ""),
                    int(row["people"] or 0),
                    int(row["slips"] or 0),
                    self.money(row["gross"], str(row["currency"] or "IDR"), source=source),
                    self.money(row["net"], str(row["currency"] or "IDR"), source=source),
                )
                for row in rows
            ],
            columns=5,
            num=(1, 2, 3, 4),
        )

    def owner_facts(self) -> dict[str, Any]:
        staff = self._one(
            """
            SELECT COUNT(*) AS active,
                   SUM(CASE WHEN status = 'SUSPENDED' THEN 1 ELSE 0 END) AS suspended
              FROM workforce_staff WHERE agency_id = ? AND status <> 'OFFBOARDED'
            """,
            (self.agency_id,),
        )
        properties = self._one(
            "SELECT COUNT(*) AS homes FROM properties WHERE agency_id = ?", (self.agency_id,)
        )
        money = self._one(
            """
            SELECT COALESCE(SUM(net_minor), 0) AS paid_net,
                   COALESCE(SUM(deduction_minor), 0) AS deductions
              FROM workforce_payslips WHERE agency_id = ?
            """,
            (self.agency_id,),
        )
        runs = self._one(
            """
            SELECT SUM(CASE WHEN status = 'PAID' THEN 1 ELSE 0 END) AS paid,
                   SUM(CASE WHEN status IN ('DRAFT','CALCULATED','APPROVED') THEN 1 ELSE 0 END)
                       AS unsettled
              FROM workforce_payroll_runs WHERE agency_id = ?
            """,
            (self.agency_id,),
        )
        attention = self._one(
            """
            SELECT COUNT(*) AS days FROM workforce_attendance_days
             WHERE agency_id = ? AND status IN ('LATE','ABSENT','INCOMPLETE')
            """,
            (self.agency_id,),
        )
        return {
            "staff_active": int(staff["active"] or 0) if staff else 0,
            "staff_suspended": int(staff["suspended"] or 0) if staff else 0,
            "properties": int(properties["homes"] or 0) if properties else 0,
            "paid_net_minor": int(money["paid_net"] or 0) if money else 0,
            "deductions_minor": int(money["deductions"] or 0) if money else 0,
            "runs_paid": int(runs["paid"] or 0) if runs else 0,
            "runs_unsettled": int(runs["unsettled"] or 0) if runs else 0,
            "days_needing_attention": int(attention["days"] or 0) if attention else 0,
        }

    def view_owner(self) -> dict[str, Any]:
        source = self.money_reference_source()
        facts = self.owner_facts()
        return {
            **facts,
            "paid_net": self.money(facts["paid_net_minor"], source=source),
            "deductions": self.money(facts["deductions_minor"], source=source),
            "property_rows": self.property_rows(source=source),
            "money_note": escape_value(self.money_note(source)),
        }

    # -- rendering --------------------------------------------------------------- #
    def context(self, screen: str) -> dict[str, Any]:
        """The page values for ``screen``, ready for :func:`dashboard.render_page`."""
        builders = {
            "workforce_supervisor": self.view_supervisor,
            "workforce_finance": self.view_finance,
            "workforce_owner": self.view_owner,
        }
        if screen not in builders:
            raise UnknownScreen("unknown workforce screen", screen=screen, known=SCREENS)
        return builders[screen]()

    def shell_context(self, *, actor: str = "", title: str = "") -> dict[str, Any]:
        """The values ``templates/base.html`` asks for.

        The rail is left empty on purpose: these screens are not yet routed by the HTTP
        dashboard, so there is no page list to advertise — each screen carries its own
        line of links. A caller that has a human title (and an actor name) passes them in;
        nothing here invents a page name, and the schema version is left blank rather than
        described in words this module would have to own.
        """
        return {
            "title": title,
            "actor": actor,
            "actor_role": actor,
            "actor_role_label": actor,
            "now": time_display_module.humanize(to_iso(self.clock.now())),
            "schema_version": applied_version(self.conn) or "",
            "nav": Markup(""),
            "read_only_banner": READ_ONLY_BADGE,
        }

    def render(
        self,
        screen: str,
        *,
        role: str = "",
        actor: str = "",
        title: str = "",
    ) -> str:
        """Render ``screen`` inside the shared page shell.

        ``role`` is checked first so that a screen cannot be drawn for a role that is
        not entitled to it, even if a caller forgets to consult
        :func:`screens_for_role`.
        """
        if role and screen not in screens_for_role(role):
            raise UnknownScreen("this role may not open that screen", screen=screen, role=role)
        context = self.shell_context(actor=actor, title=title)
        context.update(self.context(screen))
        return render_page(screen, context, templates_dir=self.templates_dir)

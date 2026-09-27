"""Tests for the human approval queue (:mod:`approvals`).

The queue is the last gate in front of money and legal actions, so these tests
are written the way an auditor would read the module:

* the *schema* is the contract — enums, column lists and triggers are compared
  against the DDL text, never against the module's own constants alone;
* a status is derived from the decision log, so a writable ``status`` column can
  never fake an approval;
* every refusal is fail-closed *and* trace-free: nothing is written when a
  decision is rejected;
* a decision is bound to the payload it was shown, so a payload edited in the
  database afterwards is detected instead of silently reusing approvals.

Stdlib only: ``unittest`` over the real SQLite schema in memory, with a
``FrozenClock`` and a deterministic ``IdFactory``.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import unittest
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # pragma: no cover - mirrors tests/conftest.py
    sys.path.insert(0, str(SRC))

from approvals import (  # noqa: E402
    APPEND_ONLY_TRIGGERS,
    APPROVAL_STATUSES,
    COLUMNS,
    DECISION_COLUMNS,
    DECISION_TABLE,
    DECISION_VALUES,
    DUAL_CONTROL_MIN_APPROVALS,
    DUAL_CONTROL_SUBJECTS,
    ERROR_CODES,
    EXPIRY_MINUTES,
    MAX_REQUIRED_APPROVALS,
    MIN_ACTION_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
    MIN_REASON_LENGTH,
    MIN_REQUIRED_APPROVALS,
    REQUEST_TABLE,
    RISK_LEVELS,
    ROLE_RANK,
    SUBJECT_TYPES,
    Approval,
    ApprovalAppendOnlyUnenforced,
    ApprovalService,
    decision_signature,
    guard_append_only,
)
from primitives import (  # noqa: E402
    DomainError,
    FrozenClock,
    IdFactory,
    Money,
    canonical_json,
    parse_iso,
    sha256_hex,
)
from schema_loader import connect_sqlite, read_schema  # noqa: E402

START = "2026-09-21T08:00:00+00:00"


# --------------------------------------------------------------------------- #
# The DDL, parsed as an independent source of truth
# --------------------------------------------------------------------------- #
def _ddl() -> str:
    return read_schema("sqlite")


def _table_columns(table: str) -> tuple[str, ...]:
    """Column names as declared, ignoring constraints and their continuations."""
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", _ddl(), re.S)
    assert match is not None, f"no CREATE TABLE for {table}"
    columns: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("CONSTRAINT"):
            continue
        name = stripped.split()[0]
        if re.fullmatch(r"[a-z][a-z0-9_]*", name):
            columns.append(name)
    return tuple(columns)


def _table_body(table: str) -> str:
    """The declared body of one table, so a search cannot drift into another table.

    Columns named ``status``, ``action`` or ``reason`` exist in several tables of
    this schema; a constraint must be read from the approval table it belongs to.
    """
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", _ddl(), re.S)
    assert match is not None, f"no CREATE TABLE for {table}"
    return match.group(1)


def _check_literals(column: str, table: str = REQUEST_TABLE) -> set[str]:
    """The literals of ``COLUMN TEXT NOT NULL CHECK (column IN (...))``."""
    match = re.search(
        rf"{column}\s+TEXT\s+NOT NULL CHECK \({column} IN \((.*?)\)\)",
        _table_body(table),
        re.S,
    )
    assert match is not None, f"no CHECK IN (...) for {table}.{column}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _length_floor(column: str, table: str = REQUEST_TABLE) -> int:
    match = re.search(rf"length\({column}\) (>=|<=) (\d+)", _table_body(table))
    assert match is not None, f"no length() constraint for {table}.{column}"
    return int(match.group(2))


def _required_approvals_range() -> tuple[int, int]:
    match = re.search(r"required_approvals BETWEEN (\d+) AND (\d+)", _table_body(REQUEST_TABLE))
    assert match is not None, "no required_approvals BETWEEN constraint"
    return int(match.group(1)), int(match.group(2))


def _dual_control_literals() -> set[str]:
    match = re.search(
        r"ck_approval_dual_for_money CHECK \(required_approvals >= \d+ OR "
        r"subject_type NOT IN \((.*?)\)\)",
        _ddl(),
        re.S,
    )
    assert match is not None, "no dual-control constraint in the DDL"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _trigger_names(table: str) -> set[str]:
    return set(
        re.findall(rf"CREATE TRIGGER (\w+)\nBEFORE (?:UPDATE|DELETE) ON {table}", _ddl())
    )


class ApprovalTestCase(unittest.TestCase):
    """Real schema in memory, frozen clock, deterministic ids."""

    def setUp(self) -> None:
        self.conn = connect_sqlite(":memory:")
        self.clock = FrozenClock(parse_iso(START))
        self.ids = IdFactory(deterministic=True, seed="test")
        self.svc = ApprovalService(self.conn, clock=self.clock, ids=self.ids)
        self.addCleanup(self.conn.close)

    # -- helpers ----------------------------------------------------------- #
    def refuse(self, code: str, func, *args, **kwargs) -> DomainError:
        """Assert ``func`` fails with exactly ``code`` and return the error."""
        with self.assertRaises(DomainError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, msg=str(caught.exception))
        return caught.exception

    def request(self, **overrides) -> Approval:
        kwargs = dict(
            subject_type="discount",
            subject_id="quote_1001",
            action="grant_discount",
            requested_by="agent_ani",
            requested_role="agent",
            reason="Diskon 10% untuk repeat guest, sudah dicek rate card.",
            payload={"percent": 10, "quote_id": "quote_1001"},
            idempotency_key="idem_quote_1001",
        )
        kwargs.update(overrides)
        return self.svc.request(**kwargs)

    def approve(self, approval_id: str, **overrides) -> Approval:
        kwargs = dict(
            approver="owner_bayu",
            approver_role="owner",
            decision="APPROVE",
            note="Setuju, sudah dicek.",
        )
        kwargs.update(overrides)
        return self.svc.decide(approval_id, **kwargs)

    def scalar(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()[0]

    def row_of(self, approval_id: str):
        return self.conn.execute(
            f"SELECT * FROM {REQUEST_TABLE} WHERE approval_id = ?", (approval_id,)
        ).fetchone()

    def requests_count(self) -> int:
        return self.scalar(f"SELECT COUNT(*) FROM {REQUEST_TABLE}")

    def decisions_count(self, approval_id: str | None = None) -> int:
        if approval_id is None:
            return self.scalar(f"SELECT COUNT(*) FROM {DECISION_TABLE}")
        return self.scalar(
            f"SELECT COUNT(*) FROM {DECISION_TABLE} WHERE approval_id = ?",
            (approval_id,),
        )

    def refund(self, **overrides) -> Approval:
        """A 5,000,000 IDR refund: the canonical dual-control request.

        Two *distinct* signatures at accountant rank or above are required, so in
        production owner + accountant covers it. This is the money path the schema
        refuses to see singly approved.
        """
        kwargs = dict(
            subject_type="refund",
            subject_id="booking_777",
            action="refund_guest",
            reason="Refund penuh untuk tamu batal H-1, sudah dicek manager.",
            payload={"booking_id": "booking_777", "amount_minor": 5_000_000},
            money=Money(5_000_000, "IDR"),
            risk_level="HIGH",
            required_role="accountant",
            required_approvals=2,
            idempotency_key="idem_refund_booking_777",
        )
        kwargs.update(overrides)
        return self.request(**kwargs)


# --------------------------------------------------------------------------- #
# 1. The schema is the contract
# --------------------------------------------------------------------------- #
class TestSchemaContract(ApprovalTestCase):
    def test_the_column_lists_are_the_ddl_columns(self):
        """The module's INSERT order and row mapping cannot drift from the DDL."""
        self.assertEqual(COLUMNS, _table_columns(REQUEST_TABLE))
        self.assertEqual(DECISION_COLUMNS, _table_columns(DECISION_TABLE))
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))

    def test_the_module_enums_are_the_ddl_enums(self):
        """A subject/risk/status/decision the DDL forbids must be refused here too."""
        self.assertEqual(set(SUBJECT_TYPES), _check_literals("subject_type"))
        self.assertEqual(set(RISK_LEVELS), _check_literals("risk_level"))
        self.assertEqual(set(APPROVAL_STATUSES), _check_literals("status"))
        self.assertEqual(
            set(DECISION_VALUES), _check_literals("decision", DECISION_TABLE)
        )

    def test_the_dual_control_subjects_are_the_money_subjects_the_ddl_guards(self):
        """The three money-moving subjects the schema refuses to see singly approved."""
        self.assertEqual(set(DUAL_CONTROL_SUBJECTS), _dual_control_literals())
        self.assertLessEqual(set(DUAL_CONTROL_SUBJECTS), set(SUBJECT_TYPES))
        self.assertEqual(DUAL_CONTROL_MIN_APPROVALS, 2)

    def test_role_ranks_and_expiry_defaults_cover_every_risk_level(self):
        """Ranking is total and ordered; every risk level has an expiry default."""
        self.assertEqual(set(ROLE_RANK), {"agent", "manager", "accountant", "owner"})
        self.assertEqual(
            [ROLE_RANK[role] for role in ("agent", "manager", "accountant", "owner")],
            [1, 2, 3, 4],
        )
        self.assertEqual(set(EXPIRY_MINUTES), set(RISK_LEVELS))
        self.assertEqual(EXPIRY_MINUTES, {"LOW": 1440, "MEDIUM": 720, "HIGH": 240})

    def test_the_field_floors_are_the_ddl_floors(self):
        """Code-level minimums mirror the CHECK constraints rather than inventing them."""
        self.assertEqual(
            (MIN_REQUIRED_APPROVALS, MAX_REQUIRED_APPROVALS), _required_approvals_range()
        )
        self.assertEqual(MIN_REASON_LENGTH, _length_floor("reason"))
        self.assertEqual(MIN_IDEMPOTENCY_KEY_LENGTH, _length_floor("idempotency_key"))
        self.assertEqual(MIN_ACTION_LENGTH, _length_floor("action"))

    def test_the_declared_error_codes_are_the_documented_set(self):
        """Callers branch on codes: no accidental additions or silent removals."""
        self.assertEqual(
            set(ERROR_CODES),
            {
                "APPROVAL_NOT_FOUND",
                "INVALID_SUBJECT_TYPE",
                "INVALID_RISK_LEVEL",
                "INVALID_DECISION",
                "SELF_APPROVAL_FORBIDDEN",
                "DUPLICATE_APPROVER",
                "APPROVAL_CLOSED",
                "APPROVAL_EXPIRED",
                "ROLE_TOO_LOW",
                "INVALID_DUAL_CONTROL",
                "IDEMPOTENCY_CONFLICT",
                "APPROVAL_TAMPERED",
                "INVALID_APPROVER",
                "MISSING_REASON",
                "INVALID_EXPIRY",
                "APPROVAL_APPEND_ONLY_UNENFORCED",
            },
        )

    def test_the_append_only_guard_names_the_ddl_triggers(self):
        """Decisions are append-only in the schema; the guard must agree."""
        self.assertEqual(set(guard_append_only(self.conn)), set(APPEND_ONLY_TRIGGERS))
        self.assertEqual(set(APPEND_ONLY_TRIGGERS), _trigger_names(DECISION_TABLE))

    def test_the_service_refuses_a_schema_whose_append_only_triggers_were_dropped(self):
        """A decision log that can be rewritten is not an audit trail — fail closed."""
        self.conn.execute("DROP TRIGGER trg_approval_decisions_no_update")
        self.refuse(
            "APPROVAL_APPEND_ONLY_UNENFORCED",
            ApprovalService,
            self.conn,
            clock=self.clock,
            ids=self.ids,
        )
        with self.assertRaises(ApprovalAppendOnlyUnenforced):
            ApprovalService(self.conn, clock=self.clock, ids=self.ids)


# --------------------------------------------------------------------------- #
# 2. Request validation: fail closed, write nothing
# --------------------------------------------------------------------------- #
class TestRequestValidation(ApprovalTestCase):
    def test_an_unknown_subject_type_is_refused(self):
        for bad in ("invoice", "REFUND", "", None, 42):
            with self.subTest(subject_type=bad):
                self.refuse("INVALID_SUBJECT_TYPE", self.request, subject_type=bad)
        self.assertEqual(self.requests_count(), 0)

    def test_an_unknown_risk_level_is_refused(self):
        for bad in ("CRITICAL", "high", "", None):
            with self.subTest(risk_level=bad):
                self.refuse("INVALID_RISK_LEVEL", self.request, risk_level=bad)
        self.assertEqual(self.requests_count(), 0)

    def test_a_missing_or_too_short_reason_is_refused(self):
        """The DDL demands 10 characters of "why"; the module refuses earlier."""
        for bad in (None, "", "   ", "short"):
            with self.subTest(reason=bad):
                self.refuse("MISSING_REASON", self.request, reason=bad)
        self.assertEqual(self.requests_count(), 0)

    def test_a_short_action_or_idempotency_key_is_refused(self):
        """Both are load-bearing: the action names the power, the key stops duplicates."""
        self.refuse("INVALID_FIELD", self.request, action="ok")
        self.refuse("MISSING_FIELD", self.request, idempotency_key=None)
        self.refuse("INVALID_FIELD", self.request, idempotency_key="short")
        self.assertEqual(self.requests_count(), 0)

    def test_required_approvals_outside_the_schema_range_is_refused(self):
        for bad in (0, -1, 4, 99, "2", None, True):
            with self.subTest(required_approvals=bad):
                self.refuse(
                    "INVALID_DUAL_CONTROL", self.request, required_approvals=bad
                )
        self.assertEqual(self.requests_count(), 0)

    def test_money_and_currency_travel_together_and_negatives_are_refused(self):
        """money_minor/currency are a pair (DDL CHECK) and amounts are never negative."""
        plain = self.request(idempotency_key="idem_plain_1")
        self.assertIsNone(plain.money_minor)
        self.assertIsNone(plain.currency)
        self.assertIsNone(plain.money)
        row = self.row_of(plain.approval_id)
        self.assertIsNone(row["money_minor"])
        self.assertIsNone(row["currency"])

        self.refuse("INVALID_MONEY", self.request, money=5_000_000, idempotency_key="idem_m1")
        self.refuse(
            "INVALID_MONEY", self.request, money=Money(-1, "IDR"), idempotency_key="idem_m2"
        )
        self.refuse(
            "INVALID_MONEY",
            self.request,
            money="5000000",
            idempotency_key="idem_m3",
        )

        paid = self.refund()
        self.assertEqual((paid.money_minor, paid.currency), (5_000_000, "IDR"))
        self.assertEqual(paid.money, Money(5_000_000, "IDR"))
        self.assertEqual(paid.money.to_major_str(), "5000000")

        # A zero-amount placeholder is not "money moving": only the subject decides.
        zero = self.request(money=Money(0, "IDR"), idempotency_key="idem_zero_1")
        self.assertEqual((zero.money_minor, zero.currency), (0, "IDR"))
        self.assertEqual(zero.required_approvals, 1)

    def test_expiry_defaults_follow_the_risk_level_and_bad_values_are_refused(self):
        expected = {"LOW": 1440, "MEDIUM": 720, "HIGH": 240}
        for risk, minutes in expected.items():
            with self.subTest(risk_level=risk):
                approval = self.request(
                    risk_level=risk, idempotency_key=f"idem_risk_{risk.lower()}"
                )
                span = parse_iso(approval.expires_at) - parse_iso(approval.requested_at)
                self.assertEqual(span, timedelta(minutes=minutes))
        for bad in (0, -5, 1.5, "600", True):
            with self.subTest(expires_minutes=bad):
                self.refuse(
                    "INVALID_EXPIRY",
                    self.request,
                    expires_minutes=bad,
                    idempotency_key="idem_expiry_bad",
                )
        explicit = self.request(expires_minutes=15, idempotency_key="idem_expiry_ok")
        self.assertEqual(
            parse_iso(explicit.expires_at) - parse_iso(explicit.requested_at),
            timedelta(minutes=15),
        )

    def test_unknown_roles_are_refused_on_both_sides_of_the_request(self):
        self.refuse(
            "INVALID_APPROVER",
            self.request,
            requested_role="director",
            idempotency_key="idem_role_1",
        )
        self.refuse(
            "INVALID_APPROVER", self.request, requested_role=None, idempotency_key="idem_role_2"
        )
        self.refuse(
            "INVALID_APPROVER",
            self.request,
            required_role="director",
            idempotency_key="idem_role_3",
        )
        # `manager` replaced `supervisor` in the 2026-09-27 rename, so the retired name is now
        # just another role the kernel does not know — a stale caller is refused, never accepted.
        self.refuse(
            "INVALID_APPROVER",
            self.request,
            required_role="supervisor",
            idempotency_key="idem_role_retired",
        )
        # A blank identifier is a *missing* field and a malformed one an *invalid*
        # field: the kernel's shape codes, not the semantic approvals codes.
        self.refuse(
            "MISSING_FIELD",
            self.request,
            requested_by="   ",
            idempotency_key="idem_role_4",
        )
        self.refuse(
            "INVALID_FIELD",
            self.request,
            requested_by="two words",
            idempotency_key="idem_role_5",
        )
        self.assertEqual(self.requests_count(), 0)


# --------------------------------------------------------------------------- #
# 3. Dual control
# --------------------------------------------------------------------------- #
class TestDualControl(ApprovalTestCase):
    def test_a_money_moving_subject_cannot_be_requested_with_one_approver(self):
        """Refused, not silently upgraded: the caller's intent is never rewritten."""
        for subject in DUAL_CONTROL_SUBJECTS:
            with self.subTest(subject_type=subject):
                self.refuse(
                    "INVALID_DUAL_CONTROL",
                    self.request,
                    subject_type=subject,
                    action="move_money",
                    required_approvals=1,
                    idempotency_key=f"idem_single_{subject}",
                )
        # Any request that actually moves money needs two signatures, whatever it is called.
        self.refuse(
            "INVALID_DUAL_CONTROL",
            self.request,
            money=Money(5_000_000, "IDR"),
            required_approvals=1,
            idempotency_key="idem_single_discount",
        )
        self.assertEqual(self.requests_count(), 0)

        allowed = self.request(required_approvals=1, idempotency_key="idem_single_ok")
        self.assertEqual(allowed.required_approvals, 1)
        dual = self.refund()
        self.assertEqual(dual.required_approvals, 2)
        self.assertTrue(dual.is_dual_control)
        self.assertEqual(self.requests_count(), 2)

    def test_a_refund_of_five_million_needs_two_different_approvers(self):
        """Invariant 1: one signature is not enough, two distinct ones close it."""
        approval = self.refund()
        self.assertEqual(approval.status, "PENDING")
        self.assertEqual(approval.approvals_count, 0)
        self.assertIsNone(approval.closed_at)

        first = self.approve(approval.approval_id, approver="owner_bayu")
        self.assertEqual(first.status, "PENDING")
        self.assertEqual(first.approvals_count, 1)
        self.assertEqual(first.rejections_count, 0)
        self.assertIsNone(first.closed_at)
        self.assertEqual(self.row_of(approval.approval_id)["status"], "PENDING")
        self.assertEqual(self.decisions_count(approval.approval_id), 1)

        second = self.approve(
            approval.approval_id, approver="accountant_sari", approver_role="accountant"
        )
        self.assertEqual(second.approval_id, approval.approval_id)
        self.assertEqual(second.status, "APPROVED")
        self.assertEqual(second.approvals_count, 2)
        self.assertIsNotNone(second.closed_at)
        self.assertEqual(self.row_of(approval.approval_id)["status"], "APPROVED")
        self.assertEqual(
            [decision.approver for decision in self.svc.decisions(approval.approval_id)],
            ["owner_bayu", "accountant_sari"],
        )
        self.assertEqual(
            [decision.decision for decision in self.svc.decisions(approval.approval_id)],
            ["APPROVE", "APPROVE"],
        )

    def test_the_status_follows_the_decision_log_not_the_status_column(self):
        """A writable column is not evidence: only decision rows can approve."""
        approval = self.refund()
        self.conn.execute(
            f"UPDATE {REQUEST_TABLE} SET status = 'APPROVED', closed_at = ? "
            "WHERE approval_id = ?",
            (START, approval.approval_id),
        )
        self.assertEqual(self.row_of(approval.approval_id)["status"], "APPROVED")

        read = self.svc.get(approval.approval_id)
        self.assertEqual(read.status, "PENDING")
        self.assertEqual(read.approvals_count, 0)
        self.assertIsNone(read.closed_at)
        # The queue looks rows up by the stored column (that is what the schema's
        # index is for) and never believes it, so a forged row leaves the queue
        # while remaining PENDING to every reader -- see the module docstring.
        self.assertEqual([item.approval_id for item in self.svc.outstanding()], [])

        one = self.approve(approval.approval_id, approver="owner_bayu")
        self.assertEqual(one.status, "PENDING")
        self.assertEqual(self.row_of(approval.approval_id)["status"], "APPROVED")
        two = self.approve(approval.approval_id, approver="owner_citra")
        self.assertEqual(two.status, "APPROVED")
        self.assertEqual(two.approvals_count, 2)
        self.assertIsNotNone(two.closed_at)

    def test_a_money_request_survives_a_rigged_required_approvals_column(self):
        """The money rule is policy, not data -- and the DDL's own guard is narrower.

        ``ck_approval_dual_for_money`` only names the three money *subject types*,
        so a payout-sized amount riding on another subject (a quote here) can be
        edited down to a single signature in the column. Readers still demand two.
        """
        approval = self.request(
            subject_type="quote",
            subject_id="quote_900",
            action="quote_override",
            payload={"quote_id": "quote_900", "amount_minor": 5_000_000},
            money=Money(5_000_000, "IDR"),
            required_approvals=2,
            idempotency_key="idem_rigged_money",
        )
        self.conn.execute(
            f"UPDATE {REQUEST_TABLE} SET required_approvals = 1 WHERE approval_id = ?",
            (approval.approval_id,),
        )
        self.assertEqual(self.svc.get(approval.approval_id).required_approvals, 2)
        self.assertEqual(
            [item.required_approvals for item in self.svc.outstanding()], [2]
        )

        first = self.approve(approval.approval_id, approver="owner_bayu")
        self.assertEqual((first.status, first.approvals_count), ("PENDING", 1))
        self.assertEqual(self.row_of(approval.approval_id)["status"], "PENDING")

        second = self.approve(approval.approval_id, approver="owner_citra")
        self.assertEqual((second.status, second.approvals_count), ("APPROVED", 2))
        self.assertIsNotNone(second.closed_at)
        self.assertEqual(self.row_of(approval.approval_id)["status"], "APPROVED")

    def test_one_approver_cannot_supply_both_signatures(self):
        approval = self.refund()
        self.approve(approval.approval_id, approver="owner_bayu")
        error = self.refuse(
            "DUPLICATE_APPROVER",
            self.approve,
            approval.approval_id,
            approver="owner_bayu",
        )
        self.assertEqual(error.details["approver"], "owner_bayu")
        self.assertEqual(self.decisions_count(approval.approval_id), 1)
        self.assertEqual(self.svc.get(approval.approval_id).status, "PENDING")


# --------------------------------------------------------------------------- #
# 4. Nobody signs their own paperwork
# --------------------------------------------------------------------------- #
class TestSelfApproval(ApprovalTestCase):
    def test_the_requester_cannot_approve_their_own_request(self):
        """Invariant 2: also true for an owner, whose rank would otherwise allow it."""
        by_owner = self.request(
            requested_by="owner_bayu",
            requested_role="owner",
            idempotency_key="idem_self_owner",
        )
        self.refuse(
            "SELF_APPROVAL_FORBIDDEN",
            self.approve,
            by_owner.approval_id,
            approver="owner_bayu",
            approver_role="owner",
        )
        by_agent = self.request(
            requested_by="agent_ani",
            subject_id="quote_2002",
            idempotency_key="idem_self_agent",
        )
        self.refuse(
            "SELF_APPROVAL_FORBIDDEN",
            self.approve,
            by_agent.approval_id,
            approver="agent_ani",
            approver_role="owner",
        )
        self.assertEqual(self.decisions_count(), 0)
        self.assertEqual(self.svc.get(by_owner.approval_id).status, "PENDING")
        self.assertEqual(self.svc.get(by_agent.approval_id).status, "PENDING")

    def test_the_requester_cannot_reject_their_own_request(self):
        approval = self.request(
            requested_by="owner_bayu", requested_role="owner", idempotency_key="idem_self_reject"
        )
        self.refuse(
            "SELF_APPROVAL_FORBIDDEN",
            self.approve,
            approval.approval_id,
            approver="owner_bayu",
            approver_role="owner",
            decision="REJECT",
            note="Tidak setuju.",
        )
        self.assertEqual(self.decisions_count(), 0)
        self.assertEqual(self.row_of(approval.approval_id)["status"], "PENDING")


# --------------------------------------------------------------------------- #
# 5. A rejection is final
# --------------------------------------------------------------------------- #
class TestRejectionCloses(ApprovalTestCase):
    def test_one_rejection_closes_the_request_for_good(self):
        """Invariant 3: a REJECT closes immediately and nothing is written after it."""
        approval = self.request(
            subject_type="bank_change",
            subject_id="payout_5001",
            action="change_bank_account",
            reason="Ganti rekening penerima payout ke BCA atas permintaan owner.",
            risk_level="HIGH",
            required_approvals=3,
            idempotency_key="idem_bank_change_5001",
        )
        first = self.approve(approval.approval_id, approver="owner_bayu")
        self.assertEqual((first.status, first.approvals_count), ("PENDING", 1))

        rejected = self.approve(
            approval.approval_id,
            approver="owner_citra",
            decision="REJECT",
            note="Rekening belum diverifikasi bank.",
        )
        self.assertEqual(rejected.status, "REJECTED")
        self.assertEqual(rejected.rejections_count, 1)
        self.assertEqual(rejected.approvals_count, 1)
        self.assertIsNotNone(rejected.closed_at)
        row = self.row_of(approval.approval_id)
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["closed_at"], rejected.closed_at)
        self.assertEqual(self.decisions_count(approval.approval_id), 2)

        self.refuse(
            "APPROVAL_CLOSED",
            self.approve,
            approval.approval_id,
            approver="owner_dewi",
        )
        self.assertEqual(self.decisions_count(approval.approval_id), 2)
        after = self.row_of(approval.approval_id)
        self.assertEqual(after["status"], "REJECTED")
        self.assertEqual(after["closed_at"], row["closed_at"])
        self.assertEqual(self.svc.get(approval.approval_id).status, "REJECTED")


# --------------------------------------------------------------------------- #
# 6. Expiry
# --------------------------------------------------------------------------- #
class TestExpiry(ApprovalTestCase):
    def test_deciding_a_request_at_or_after_expiry_expires_it_first(self):
        """Invariant 4: the EXPIRED mark is durable even though the call is refused."""
        early = self.request(
            risk_level="HIGH", subject_id="quote_3001", idempotency_key="idem_high_early"
        )
        late = self.request(
            risk_level="HIGH", subject_id="quote_3002", idempotency_key="idem_high_late"
        )
        self.assertEqual(
            parse_iso(late.expires_at) - parse_iso(late.requested_at), timedelta(minutes=240)
        )

        self.clock.advance(minutes=239)  # one minute before expiry: still decidable
        allowed = self.approve(early.approval_id, approver="owner_bayu")
        self.assertEqual(allowed.status, "APPROVED")

        self.clock.advance(minutes=1)  # exactly at expires_at
        self.refuse(
            "APPROVAL_EXPIRED",
            self.approve,
            late.approval_id,
            approver="owner_bayu",
        )
        row = self.row_of(late.approval_id)
        self.assertEqual(row["status"], "EXPIRED")
        self.assertEqual(row["closed_at"], late.expires_at)
        self.assertEqual(self.decisions_count(late.approval_id), 0)
        read = self.svc.get(late.approval_id)
        self.assertEqual(read.status, "EXPIRED")
        self.assertEqual(read.closed_at, late.expires_at)
        self.assertEqual(read.approvals_count, 0)

        # The request is closed now, so a second attempt is refused as closed.
        self.refuse(
            "APPROVAL_CLOSED", self.approve, late.approval_id, approver="owner_citra"
        )
        self.assertEqual(self.decisions_count(late.approval_id), 0)

    def test_expire_due_is_idempotent_and_writes_no_decisions(self):
        first = self.request(
            risk_level="HIGH", subject_id="quote_4001", idempotency_key="idem_high_4001"
        )
        self.clock.advance(minutes=60)
        second = self.request(
            risk_level="HIGH", subject_id="quote_4002", idempotency_key="idem_high_4002"
        )
        self.clock.advance(minutes=600)  # both are long overdue

        expired = self.svc.expire_due()
        self.assertEqual(expired, (first.approval_id, second.approval_id))
        self.assertEqual(self.svc.expire_due(), ())
        for approval in (first, second):
            with self.subTest(approval_id=approval.approval_id):
                row = self.row_of(approval.approval_id)
                self.assertEqual(row["status"], "EXPIRED")
                self.assertEqual(row["closed_at"], approval.expires_at)
                self.assertEqual(self.svc.get(approval.approval_id).status, "EXPIRED")
        self.assertEqual(self.decisions_count(), 0)
        self.assertEqual(self.svc.outstanding(), [])

    def test_expire_due_leaves_requests_that_are_not_due_alone(self):
        long_lived = self.request(
            risk_level="LOW", subject_id="quote_5001", idempotency_key="idem_low_5001"
        )
        short_lived = self.request(
            risk_level="HIGH", subject_id="quote_5002", idempotency_key="idem_high_5002"
        )
        self.assertEqual(self.svc.expire_due(), ())
        self.clock.advance(minutes=300)

        self.assertEqual(self.svc.expire_due(), (short_lived.approval_id,))
        self.assertEqual(self.row_of(long_lived.approval_id)["status"], "PENDING")
        self.assertEqual(self.row_of(short_lived.approval_id)["status"], "EXPIRED")
        self.assertEqual(
            [item.approval_id for item in self.svc.outstanding()], [long_lived.approval_id]
        )

    def test_a_partially_approved_request_still_expires_and_keeps_its_history(self):
        approval = self.refund()
        self.approve(approval.approval_id, approver="owner_bayu")
        self.clock.advance(minutes=300)

        self.assertEqual(self.svc.expire_due(), (approval.approval_id,))
        read = self.svc.get(approval.approval_id)
        self.assertEqual(read.status, "EXPIRED")
        self.assertEqual(read.approvals_count, 1)
        self.assertEqual(read.rejections_count, 0)
        self.assertEqual(self.decisions_count(approval.approval_id), 1)
        self.assertEqual(self.svc.decisions(approval.approval_id)[0].approver, "owner_bayu")

    def test_an_overdue_pending_row_is_not_rewritten_by_a_read(self):
        """``outstanding`` is read-only; ``expire_due`` owns the transition."""
        approval = self.request(
            risk_level="HIGH", subject_id="quote_6001", idempotency_key="idem_high_6001"
        )
        self.clock.advance(minutes=300)
        self.assertEqual(self.svc.outstanding(), [])
        self.assertEqual(self.row_of(approval.approval_id)["status"], "PENDING")
        self.assertEqual(self.svc.expire_due(), (approval.approval_id,))


# --------------------------------------------------------------------------- #
# 7. Role gate
# --------------------------------------------------------------------------- #
class TestRoleGate(ApprovalTestCase):
    def test_a_role_below_the_required_rank_cannot_decide(self):
        """Invariant 5: refused by rank, and no decision row is written."""
        approval = self.request(required_role="owner", idempotency_key="idem_role_owner")
        self.refuse(
            "ROLE_TOO_LOW",
            self.approve,
            approval.approval_id,
            approver="agent_budi",
            approver_role="agent",
        )
        self.refuse(
            "ROLE_TOO_LOW",
            self.approve,
            approval.approval_id,
            approver="accountant_sari",
            approver_role="accountant",
        )
        supervisory = self.request(
            required_role="manager",
            subject_id="quote_7001",
            idempotency_key="idem_role_manager",
        )
        self.refuse(
            "ROLE_TOO_LOW",
            self.approve,
            supervisory.approval_id,
            approver="agent_budi",
            approver_role="agent",
        )
        self.assertEqual(self.decisions_count(), 0)
        self.assertEqual(self.svc.get(approval.approval_id).status, "PENDING")
        self.assertEqual(self.svc.get(supervisory.approval_id).status, "PENDING")

    def test_a_role_at_or_above_the_required_rank_may_decide(self):
        equal = self.request(
            required_role="manager", subject_id="quote_8001", idempotency_key="idem_rank_eq"
        )
        self.assertEqual(
            self.approve(
                equal.approval_id, approver="manager_dian", approver_role="manager"
            ).status,
            "APPROVED",
        )
        above = self.request(
            required_role="manager", subject_id="quote_8002", idempotency_key="idem_rank_above"
        )
        self.assertEqual(
            self.approve(
                above.approval_id, approver="accountant_sari", approver_role="accountant"
            ).status,
            "APPROVED",
        )
        top = self.request(
            required_role="accountant", subject_id="quote_8003", idempotency_key="idem_rank_top"
        )
        self.assertEqual(self.approve(top.approval_id, approver="owner_bayu").status, "APPROVED")

    def test_an_unknown_role_on_the_decision_is_refused(self):
        approval = self.request()
        self.refuse(
            "INVALID_APPROVER",
            self.approve,
            approval.approval_id,
            approver="owner_bayu",
            approver_role="wizard",
        )
        self.refuse(
            "INVALID_APPROVER",
            self.approve,
            approval.approval_id,
            approver="owner_bayu",
            approver_role="",
        )
        self.assertEqual(self.decisions_count(), 0)


# --------------------------------------------------------------------------- #
# 8. Idempotency
# --------------------------------------------------------------------------- #
class TestIdempotency(ApprovalTestCase):
    def test_the_same_key_and_request_returns_the_same_approval(self):
        """Invariant 6: a retry is a read, not a second request."""
        first = self.request()
        before = self.requests_count()
        replayed = self.request()
        self.assertEqual(replayed.approval_id, first.approval_id)
        self.assertEqual(replayed, first)
        self.assertEqual(replayed.payload_sha256, first.payload_sha256)
        self.assertEqual(self.requests_count(), before)

    def test_the_same_key_with_a_different_request_is_a_conflict(self):
        first = self.request()
        for overrides in (
            {"subject_id": "quote_9999"},
            {"subject_type": "hold"},
            {"requested_by": "agent_lain"},
            {"reason": "Alasan yang berbeda sekali untuk permintaan yang sama."},
            {"payload": {"percent": 25, "quote_id": "quote_1001"}},
            {"required_role": "manager"},
        ):
            with self.subTest(**overrides):
                self.refuse("IDEMPOTENCY_CONFLICT", self.request, **overrides)
        self.assertEqual(self.requests_count(), 1)
        self.assertEqual(self.row_of(first.approval_id)["payload"], first.payload and canonical_json(first.payload))

    def test_an_identical_replay_after_a_decision_is_still_the_same_request(self):
        approval = self.refund()
        self.approve(approval.approval_id, approver="owner_bayu")
        replayed = self.refund()
        self.assertEqual(replayed.approval_id, approval.approval_id)
        self.assertEqual(replayed.approvals_count, 1)
        self.assertEqual(self.decisions_count(approval.approval_id), 1)


# --------------------------------------------------------------------------- #
# 9. Tamper evidence
# --------------------------------------------------------------------------- #
class TestIntegrity(ApprovalTestCase):
    def test_a_payload_edited_in_the_database_is_detected_everywhere(self):
        """Invariant 8: the digest is the only thing that may be believed."""
        approval = self.request()
        self.conn.execute(
            f"UPDATE {REQUEST_TABLE} SET payload = ? WHERE approval_id = ?",
            ('{"percent": 99, "quote_id": "quote_1001"}', approval.approval_id),
        )
        self.refuse("APPROVAL_TAMPERED", self.approve, approval.approval_id)
        self.refuse("APPROVAL_TAMPERED", self.svc.get, approval.approval_id)
        self.refuse("APPROVAL_TAMPERED", self.svc.decisions, approval.approval_id)
        self.refuse("APPROVAL_TAMPERED", self.svc.outstanding)
        self.refuse("APPROVAL_TAMPERED", self.svc.expire_due)
        self.assertEqual(self.decisions_count(), 0)
        self.assertEqual(self.row_of(approval.approval_id)["status"], "PENDING")

    def test_a_payload_rehashed_after_a_decision_is_still_detected(self):
        """An attacker who also refreshes the digest contradicts the signed decision."""
        approval = self.refund()
        self.approve(approval.approval_id, approver="owner_bayu")
        body = {"booking_id": "booking_777", "amount_minor": 500_000}
        self.conn.execute(
            f"UPDATE {REQUEST_TABLE} SET payload = ?, payload_sha256 = ? WHERE approval_id = ?",
            (
                canonical_json(body),
                sha256_hex(canonical_json(body)),
                approval.approval_id,
            ),
        )
        self.refuse(
            "APPROVAL_TAMPERED",
            self.approve,
            approval.approval_id,
            approver="accountant_sari",
            approver_role="accountant",
        )
        self.assertEqual(self.decisions_count(approval.approval_id), 1)
        stored = self.svc_decision_request_hash(approval.approval_id)
        self.assertNotEqual(stored, self.row_of(approval.approval_id)["payload_sha256"])

    def svc_decision_request_hash(self, approval_id: str) -> str:
        return self.scalar(
            f"SELECT request_hash FROM {DECISION_TABLE} WHERE approval_id = ?", (approval_id,)
        )

    def test_the_decision_log_is_append_only_in_the_database(self):
        approval = self.request()
        self.approve(approval.approval_id, approver="owner_bayu")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"UPDATE {DECISION_TABLE} SET decision = 'REJECT' WHERE approval_id = ?",
                (approval.approval_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"DELETE FROM {DECISION_TABLE} WHERE approval_id = ?", (approval.approval_id,)
            )
        self.assertEqual(self.decisions_count(approval.approval_id), 1)


# --------------------------------------------------------------------------- #
# 10. Queue, reads and decision guards
# --------------------------------------------------------------------------- #
class TestQueue(ApprovalTestCase):
    def test_outstanding_lists_pending_requests_oldest_first_with_age_and_filters(self):
        first = self.request(
            subject_id="quote_9001", required_role="owner", idempotency_key="idem_q_9001"
        )
        self.clock.advance(minutes=30)
        second = self.refund(subject_id="booking_9002", idempotency_key="idem_q_9002")
        self.clock.advance(minutes=30)
        third = self.request(
            subject_type="message_send",
            subject_id="guest_9003",
            action="send_broadcast",
            required_role="manager",
            risk_level="LOW",
            idempotency_key="idem_q_9003",
        )

        items = self.svc.outstanding()
        self.assertEqual(
            [item.approval_id for item in items],
            [first.approval_id, second.approval_id, third.approval_id],
        )
        self.assertEqual([item.age_minutes for item in items], [60, 30, 0])
        self.assertEqual(items[1].required_approvals, 2)
        self.assertEqual((items[1].money_minor, items[1].currency), (5_000_000, "IDR"))
        self.assertEqual(items[1].risk_level, "HIGH")
        self.assertEqual(items[1].reason, second.reason)
        self.assertEqual(
            [item.approval_id for item in self.svc.outstanding(required_role="manager")],
            [third.approval_id],
        )

        self.approve(first.approval_id, approver="owner_bayu")
        self.approve(
            second.approval_id, approver="owner_citra", decision="REJECT", note="Nominal salah."
        )
        self.assertEqual(
            [item.approval_id for item in self.svc.outstanding()], [third.approval_id]
        )
        self.assertEqual(self.svc.get(first.approval_id).status, "APPROVED")
        self.assertEqual(self.svc.get(second.approval_id).status, "REJECTED")

    def test_an_unknown_approval_id_reads_as_not_found(self):
        self.refuse("APPROVAL_NOT_FOUND", self.svc.get, "approval_missing_1")
        self.refuse("APPROVAL_NOT_FOUND", self.svc.decisions, "approval_missing_1")
        self.refuse(
            "APPROVAL_NOT_FOUND",
            self.approve,
            "approval_missing_1",
            approver="owner_bayu",
        )
        self.refuse("APPROVAL_NOT_FOUND", self.svc.get, "")
        self.refuse("APPROVAL_NOT_FOUND", self.svc.get, None)

    def test_for_subject_returns_every_request_for_one_subject_oldest_first(self):
        first = self.request(subject_id="quote_9101", idempotency_key="idem_s_9101")
        self.clock.advance(minutes=45)
        second = self.request(
            subject_id="quote_9101",
            required_role="manager",
            required_approvals=2,
            idempotency_key="idem_s_9101_b",
        )
        other = self.request(subject_id="quote_9102", idempotency_key="idem_s_9102")

        history = self.svc.for_subject("discount", "quote_9101")
        self.assertEqual([item.approval_id for item in history], [first.approval_id, second.approval_id])
        self.assertEqual(history[1].required_role, "manager")
        self.assertNotIn(other.approval_id, [item.approval_id for item in history])
        self.assertEqual(self.svc.for_subject("hold", "quote_9101"), [])
        self.refuse("INVALID_SUBJECT_TYPE", self.svc.for_subject, "invoice", "quote_9101")


class TestDecisionGuards(ApprovalTestCase):
    def test_an_invalid_decision_or_approver_or_missing_note_is_refused(self):
        approval = self.request()
        self.refuse(
            "INVALID_DECISION",
            self.approve,
            approval.approval_id,
            decision="MAYBE",
        )
        for bad in ("", "   ", None, 7, "APPROVES"):
            with self.subTest(decision=bad):
                self.refuse(
                    "INVALID_DECISION",
                    self.approve,
                    approval.approval_id,
                    decision=bad,
                )
        for bad in ("", "   ", None, 7):
            with self.subTest(approver=bad):
                self.refuse(
                    "INVALID_APPROVER",
                    self.approve,
                    approval.approval_id,
                    approver=bad,
                )
        self.refuse(
            "INVALID_APPROVER",
            self.approve,
            approval.approval_id,
            approver="system:kernel",
        )
        self.refuse(
            "INVALID_APPROVER",
            self.approve,
            approval.approval_id,
            approver="cron:nightly",
        )
        self.refuse(
            "INVALID_APPROVER",
            self.approve,
            approval.approval_id,
            approver="not a human@reference",
        )
        for bad in (None, "", "no"):
            with self.subTest(note=bad):
                self.refuse(
                    "MISSING_REASON",
                    self.approve,
                    approval.approval_id,
                    note=bad,
                )
        self.assertEqual(self.decisions_count(approval.approval_id), 0)
        self.assertEqual(self.svc.get(approval.approval_id).status, "PENDING")

        # Case is normalised rather than refused: a human typing "Reject" gets the
        # canonical REJECT recorded, so nothing downstream has to guess.
        lowercased = self.request(subject_id="quote_lower", idempotency_key="idem_lower")
        self.assertEqual(
            self.approve(
                lowercased.approval_id,
                decision="Reject",
                note="Tidak setuju, rate card tidak cocok.",
            ).status,
            "REJECTED",
        )
        self.assertEqual(
            [item.decision for item in self.svc.decisions(lowercased.approval_id)],
            ["REJECT"],
        )

    def test_the_signature_binds_approver_decision_time_and_payload(self):
        approval = self.refund()
        decided = self.approve(approval.approval_id, approver="owner_bayu")
        self.assertEqual(decided.approvals_count, 1)
        decision = self.svc.decisions(approval.approval_id)[0]

        self.assertEqual(decision.request_hash, approval.payload_sha256)
        self.assertEqual(len(decision.signature_hash), 64)
        expected = sha256_hex(
            canonical_json(
                {
                    "approval_id": approval.approval_id,
                    "approver": "owner_bayu",
                    "decision": "APPROVE",
                    "decided_at": decision.decided_at,
                    "payload_sha256": approval.payload_sha256,
                }
            )
        )
        self.assertEqual(decision.signature_hash, expected)
        self.assertEqual(
            decision.signature_hash,
            decision_signature(
                approval.approval_id,
                "owner_bayu",
                "APPROVE",
                decision.decided_at,
                approval.payload_sha256,
            ),
        )
        for other in (
            decision_signature(
                approval.approval_id, "owner_citra", "APPROVE", decision.decided_at, approval.payload_sha256
            ),
            decision_signature(
                approval.approval_id, "owner_bayu", "REJECT", decision.decided_at, approval.payload_sha256
            ),
            decision_signature(
                approval.approval_id, "owner_bayu", "APPROVE", "2026-09-21T09:00:00.000000+00:00", approval.payload_sha256
            ),
            decision_signature(
                approval.approval_id, "owner_bayu", "APPROVE", decision.decided_at, "0" * 64
            ),
        ):
            self.assertNotEqual(decision.signature_hash, other)

    def test_decisions_are_returned_oldest_first_with_their_notes(self):
        approval = self.request(
            required_approvals=2, subject_id="quote_9301", idempotency_key="idem_notes_9301"
        )
        self.approve(approval.approval_id, approver="owner_bayu", note="Setuju, cek rate card.")
        self.clock.advance(minutes=10)
        self.approve(
            approval.approval_id,
            approver="owner_citra",
            note="Setuju, margin masih aman.",
        )
        stored = self.svc.decisions(approval.approval_id)
        self.assertEqual([d.approver for d in stored], ["owner_bayu", "owner_citra"])
        self.assertEqual([d.note for d in stored], ["Setuju, cek rate card.", "Setuju, margin masih aman."])
        self.assertEqual([d.decision for d in stored], ["APPROVE", "APPROVE"])
        self.assertEqual(stored[0].decided_at, START)
        self.assertEqual(
            [d.approver_role for d in stored], ["owner", "owner"]
        )
        self.assertEqual(stored[1].approval_id, approval.approval_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

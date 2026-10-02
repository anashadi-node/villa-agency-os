"""Preflight tests: the gate must refuse exactly the things it claims to refuse.

A go-live gate is only worth its output if a *failure* is proven to stop the
deploy, so most of these tests assert the refusal, not the happy path.

**Every class in this file must start with ``Test``.** pytest collects a plain class
by the ``python_classes`` prefix (``Test*``), so a class named ``DemoDataTests`` is never
run — its four refusal tests sat unexecuted until the rename, and one of them had gone
stale in the meantime. ``unittest.TestCase`` subclasses are exempt: they are collected
regardless of their name.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import preflight as PF  # noqa: E402
from primitives import FrozenClock, IdFactory  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402
import settings as SETTINGS  # noqa: E402
from seed_support import SeedMixin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

ALL_DECIDED = """# Keputusan

| # | Keputusan | Blocking | Status | Nilai | Oleh | Tanggal | Bukti |
|---|-----------|----------|--------|-------|------|---------|-------|
| D1 | Provider pembayaran | BLOCKING | DECIDED | midtrans hosted | Anas | 2026-09-21 | docs/pay.md |
| D2 | Property pilot | BLOCKING | DECIDED | 8 villa | Anas | 2026-09-21 | docs/pilot.md |
| D15 | Bukti inventori | NON-BLOCKING | OPEN | | | | |
"""

ONE_OPEN = """# Keputusan

| # | Keputusan | Blocking | Status | Nilai | Oleh | Tanggal | Bukti |
|---|-----------|----------|--------|-------|------|---------|-------|
| D1 | Provider pembayaran | BLOCKING | DECIDED | midtrans | Anas | 2026-09-21 | docs/pay.md |
| D7 | Ambang shadow mode | BLOCKING | OPEN | | | | |
"""

NO_ROWS = "# Keputusan\n\nbelum ada tabel\n"


class TestDecisions:
    pass


def test_all_decided_passes(tmp_path):
    path = tmp_path / "decisions.md"
    path.write_text(ALL_DECIDED, encoding="utf-8")
    checks, open_blocking = PF.parse_decisions(path)
    assert [c.verdict for c in checks] == [PF.OK]
    assert open_blocking == []
    assert "3/3" in checks[0].detail or "2/3" in checks[0].detail


def test_one_open_blocking_row_fails_and_names_it(tmp_path):
    path = tmp_path / "decisions.md"
    path.write_text(ONE_OPEN, encoding="utf-8")
    checks, open_blocking = PF.parse_decisions(path)
    assert checks[0].verdict == PF.FAIL
    assert open_blocking == ["D7"]


def test_a_file_without_rows_is_a_failure(tmp_path):
    path = tmp_path / "decisions.md"
    path.write_text(NO_ROWS, encoding="utf-8")
    checks, _ = PF.parse_decisions(path)
    assert checks[0].verdict == PF.FAIL


def test_missing_decisions_file_is_a_failure(tmp_path):
    checks, _ = PF.parse_decisions(tmp_path / "nope.md")
    assert checks[0].verdict == PF.FAIL


def test_the_decisions_file_is_green_and_readiness_is_the_real_blocker():
    """The refusal moved, and the guard moved with it — it does not merely disappear.

    History, because this test keeps changing shape on purpose: it first asserted `>= 10` open
    blocking rows, then "at least one", and as of 2026-10-02 the owner answered the last
    BLOCKING row (D17 became DECIDED — the tenant-readiness *checker* now exists). A guard that
    simply turned green would have lost its teeth, so it asserts the current truth instead: the
    decisions file is clean, and go-live is still refused — now by tenant readiness, on a
    database whose agency has filled nothing.
    """

    checks, open_blocking = PF.parse_decisions(REPO / "docs/GO_LIVE_DECISIONS.md")
    assert open_blocking == [], f"a BLOCKING row went back to OPEN: {open_blocking}"
    assert checks[0].verdict == PF.OK

    conn = connect_sqlite(":memory:")
    try:
        readiness = PF.check_tenant_readiness(conn)
    finally:
        conn.close()
    assert readiness.verdict == PF.FAIL, "the real blocker must still refuse go-live"
    assert "agency_default" in readiness.detail


class TestDemoData(SeedMixin):
    def setup_method(self):
        self.conn = connect_sqlite(":memory:")
        self.inventory()

    def teardown_method(self):
        self.conn.close()

    def test_clean_inventory_passes(self):
        assert PF.check_demo_data(self.conn).verdict == PF.OK

    def test_a_demo_prefixed_id_is_refused(self):
        # The rename trips a foreign key (units point at the property). This test is about the
        # demo *detector*, not about referential integrity, so enforcement is relaxed for the
        # setup — it never ran before because the class did not start with "Test".
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute(
            "UPDATE properties SET property_id = 'demo_prop_1' WHERE property_id = 'prop_0001'"
        )
        check = PF.check_demo_data(self.conn)
        assert check.verdict == PF.FAIL
        assert "demo" in check.detail.lower()

    def test_a_property_named_contoh_is_refused(self):
        self.conn.execute("UPDATE properties SET name = 'Villa Contoh Ubud'")
        check = PF.check_demo_data(self.conn)
        assert check.verdict == PF.FAIL
        assert "example name" in check.detail

    def test_a_demo_booking_id_is_refused(self):
        self.booking()
        self.conn.execute(
            "UPDATE bookings SET booking_id = 'demo_book_1' WHERE booking_id = 'book_0001'"
        )
        assert PF.check_demo_data(self.conn).verdict == PF.FAIL


def test_backup_checks(tmp_path):
    missing = PF.check_backup(tmp_path / "nope.sqlite")
    assert missing.verdict == PF.FAIL

    tiny = tmp_path / "tiny.sqlite"
    tiny.write_bytes(b"x")
    assert PF.check_backup(tiny).verdict == PF.FAIL

    fresh = tmp_path / "fresh.sqlite"
    fresh.write_bytes(b"x" * 8192)
    assert PF.check_backup(fresh).verdict == PF.OK

    assert PF.check_backup(None).verdict == PF.WARN


def test_repository_checks_pass_in_this_tree():
    assert PF.check_twin_parity().verdict == PF.OK
    verdicts = {c.name: c.verdict for c in PF.check_scans()}
    assert verdicts["secret scan"] == PF.OK
    assert verdicts["unsafe config scan"] == PF.OK


def test_tracked_env_file_check_runs():
    assert PF.check_tracked_env_file().verdict in (PF.OK, PF.WARN)


def test_environment_check_reports_missing_names():
    check = PF.check_env_present(["VILLA_THIS_IS_NOT_SET_ANYWHERE"])
    assert check.verdict == PF.WARN
    assert "VILLA_THIS_IS_NOT_SET_ANYWHERE" in check.detail

    os.environ["VILLA_PREFLIGHT_TEST"] = "1"
    try:
        assert PF.check_env_present(["VILLA_PREFLIGHT_TEST"]).verdict == PF.OK
    finally:
        os.environ.pop("VILLA_PREFLIGHT_TEST", None)


class TestCli:
    """The CLI is what a human runs at 23:00 before a launch."""

    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        merged = dict(os.environ)
        merged.pop("VILLA_DB", None)
        merged.pop("VILLA_ANCHOR_DIR", None)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(REPO / "scripts/preflight.py"), *args],
            capture_output=True, text=True, timeout=300, cwd=str(REPO), env=merged,
        )


def _make_db(path: Path) -> None:
    conn = connect_sqlite(str(path))
    conn.close()


def test_no_go_when_decisions_are_open_and_anchor_is_missing(tmp_path):
    db = tmp_path / "villa.db"
    _make_db(db)
    result = TestCli()._run("--db", str(db), "--anchor-dir", str(tmp_path / "anchors"))
    assert result.returncode == 1
    assert "NO-GO" in result.stdout
    assert "published anchor" in result.stdout
    assert "go-live decisions" in result.stdout


def _ready_tenant(conn) -> None:
    """Fill a tenant so D17's check can pass: settings, a SELLABLE property, an ACTIVE unit.

    Added 2026-10-02: before this, "everything checks out" could mean "no agency has filled
    anything yet". A go-live scenario that skips the tenant is not a go-live scenario.
    """
    now = "2026-09-21T08:00:00+00:00"
    conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('prop_go', 'agency_default', 'Villa Uji', 'villa', 'Ubud', 'Bali',"
        " 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
        " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES ('unit_go', 'agency_default', 'prop_go', 'Unit Uji', 'entire_villa', 2, 2, 2, 4,"
        " 'ACTIVE', ?, ?)", (now, now))
    service = SETTINGS.AgencySettings(conn, clock=FrozenClock.at(now), ids=IdFactory(True, "go"))
    for key, value in {
        "payment.provider": "midtrans",
        "payment.rails": '["CARD"]',
        "messaging.whatsapp_number": "+628123456789",
        "tax.pbjt_source": "perda Badung",
        "operations.oncall_primary": "Mr. TS",
    }.items():
        service.set(key, value, actor="owner_anas", actor_role="owner")


def test_go_when_everyhing_checks_out(tmp_path):
    from anchors import publish_anchor
    from audit import AuditLog

    db = tmp_path / "villa.db"
    _make_db(db)
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    conn = connect_sqlite(str(db))
    try:
        _ready_tenant(conn)
        publish_anchor(
            conn=conn,
            audit=AuditLog(conn, clock=FrozenClock.at("2026-09-21T08:00:00Z"),
                           ids=IdFactory(True, "pf")),
            clock=FrozenClock.at("2026-09-21T08:00:00Z"),
            directory=anchors, actor="ops.anchor", actor_role="operations",
        )
    finally:
        conn.close()

    decisions = tmp_path / "decisions.md"
    decisions.write_text(ALL_DECIDED, encoding="utf-8")
    backup = tmp_path / "villa-backup.sqlite"
    backup.write_bytes(b"x" * 8192)

    result = TestCli()._run(
        "--db", str(db), "--anchor-dir", str(anchors), "--decisions", str(decisions),
        "--backup", str(backup),
    )
    assert result.returncode == 0, result.stdout
    assert "VERDICT: GO" in result.stdout


def test_a_stale_anchor_stops_the_deploy(tmp_path, monkeypatch):
    """An anchor that has not been refreshed cannot prove recent rows exist."""

    from anchors import publish_anchor
    from audit import AuditLog

    db = tmp_path / "villa.db"
    _make_db(db)
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    conn = connect_sqlite(str(db))
    try:
        publish_anchor(
            conn=conn,
            audit=AuditLog(conn, clock=FrozenClock.at("2026-01-01T08:00:00Z"),
                           ids=IdFactory(True, "pf")),
            clock=FrozenClock.at("2026-01-01T08:00:00Z"),
            directory=anchors, actor="ops.anchor", actor_role="operations",
        )
        checks = PF.check_database(db, anchors)
    finally:
        conn.close()
    verdicts = {c.name: c.verdict for c in checks}
    assert verdicts["published anchor"] == PF.FAIL


class TestPublishedPolicyLadderRoles:
    """A published policy value may not name a role the engine cannot rank (ADR-0035/0036).

    The drift this catches was real: ``finance.approval.payment_thresholds_minor`` was
    published naming ``["finance_manager", "gm"]`` while neither name existed in the approval
    ladder. Nothing read the value yet, so nothing failed, and the first real payment would
    have been refused with a cause that named no cause.
    """

    KEY = PF.PAYMENT_POLICY_KEY

    def _publish(self, conn, value: str, version: int = 1) -> None:
        from policy_engine import PolicyResolver

        resolver = PolicyResolver(
            conn, clock=FrozenClock.at("2026-09-26T09:00:00Z"), agency_id="agency_default"
        )
        resolver.publish(
            self.KEY,
            value,
            effective_from="2026-09-26",
            effective_to="9999-12-31",
            source_doc_ref="ADR-0036 ladder",
            approved_by="owner_anas",
        )

    def test_an_unpublished_key_is_a_warning_not_a_failure(self, conn):
        """The owner has not decided yet — that is a state, not a defect."""
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.WARN
        assert self.KEY in check.detail

    def test_the_adr_0036_ladder_passes(self, conn):
        self._publish(conn, '{"ladder": ["finance_manager", "gm"], "min_approvals": 2}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.OK, check.detail
        assert "finance_manager" in check.detail and "gm" in check.detail

    def test_the_pre_reconciliation_shape_still_passes(self, conn):
        """The older ``levels`` shape names legacy aliases, which must keep resolving."""
        self._publish(conn, '{"levels": [{"min_minor": 0, "roles": ["finance", "manager"]}]}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.OK, check.detail

    def test_a_role_the_ladder_does_not_know_fails(self, conn):
        self._publish(conn, '{"ladder": ["finance_manager", "head_of_everything"]}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.FAIL
        assert "head_of_everything" in check.detail

    def test_a_role_with_no_rank_fails(self, conn):
        self._publish(conn, '{"ladder": ["finance_manager", "auditor"]}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.FAIL
        assert "auditor" in check.detail

    def test_a_ladder_that_descends_fails(self, conn):
        self._publish(conn, '{"ladder": ["gm", "finance_manager"]}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.FAIL

    def test_a_value_naming_no_role_fails(self, conn):
        self._publish(conn, '{"min_approvals": 2}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.FAIL

    def test_the_latest_revision_is_the_one_checked(self, conn):
        """An old revision still governs old decisions, but the gate reads what is in force."""
        self._publish(conn, '{"ladder": ["nobody"]}', version=1)
        self._publish(conn, '{"ladder": ["finance_manager", "gm"]}')
        check = PF.check_published_ladder_roles(conn)
        assert check.verdict == PF.OK, check.detail


class TestTenantReadiness:
    """D17 as a preflight check: the platform may pass while a tenant still owes settings."""

    def _make_db(self, path: Path) -> None:
        conn = connect_sqlite(str(path))
        conn.close()

    def test_an_agency_with_nothing_filled_is_refused_by_name(self, tmp_path):
        db = tmp_path / "villa.db"
        self._make_db(db)
        conn = connect_sqlite(str(db))
        try:
            check = PF.check_tenant_readiness(conn)
        finally:
            conn.close()
        assert check.verdict == PF.FAIL
        assert "agency_default" in check.detail
        assert "required setting not filled" in check.detail
        assert "SELLABLE" in check.detail

    def test_a_filled_agency_passes(self, tmp_path):
        db = tmp_path / "villa.db"
        self._make_db(db)
        conn = connect_sqlite(str(db))
        try:
            now = "2026-10-02T08:00:00+00:00"
            conn.execute(
                "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
                " province, timezone, status, contract_pack_status, created_at, updated_at)"
                " VALUES ('prop_pf', 'agency_default', 'Villa Uji', 'villa', 'Ubud', 'Bali',"
                " 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
                " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
                " VALUES ('unit_pf', 'agency_default', 'prop_pf', 'Unit Uji', 'entire_villa',"
                " 2, 2, 2, 4, 'ACTIVE', ?, ?)", (now, now))
            service = SETTINGS.AgencySettings(
                conn, clock=FrozenClock.at(now), ids=IdFactory(True, "pf-settings"))
            for key, value in {
                "payment.provider": "midtrans",
                "payment.rails": '["CARD"]',
                "messaging.whatsapp_number": "+628123456789",
                "tax.pbjt_source": "perda Badung",
                "operations.oncall_primary": "Mr. TS",
            }.items():
                service.set(key, value, actor="owner_anas", actor_role="owner")
            check = PF.check_tenant_readiness(conn)
        finally:
            conn.close()
        assert check.verdict == PF.OK
        assert "ready" in check.detail

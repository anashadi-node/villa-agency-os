"""Preflight tests: the gate must refuse exactly the things it claims to refuse.

A go-live gate is only worth its output if a *failure* is proven to stop the
deploy, so most of these tests assert the refusal, not the happy path.
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


class DecisionTests:
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


def test_the_real_decisions_file_blocks_go_live():
    """The repository ships with the decisions genuinely undecided."""

    checks, open_blocking = PF.parse_decisions(REPO / "docs/GO_LIVE_DECISIONS.md")
    assert checks[0].verdict == PF.FAIL
    assert len(open_blocking) >= 10


class DemoDataTests(SeedMixin):
    def setup_method(self):
        self.conn = connect_sqlite(":memory:")
        self.inventory()

    def teardown_method(self):
        self.conn.close()

    def test_clean_inventory_passes(self):
        assert PF.check_demo_data(self.conn).verdict == PF.OK

    def test_a_demo_prefixed_id_is_refused(self):
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


class CliTests:
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
    result = CliTests()._run("--db", str(db), "--anchor-dir", str(tmp_path / "anchors"))
    assert result.returncode == 1
    assert "NO-GO" in result.stdout
    assert "published anchor" in result.stdout
    assert "go-live decisions" in result.stdout


def test_go_when_everyhing_checks_out(tmp_path):
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

    result = CliTests()._run(
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

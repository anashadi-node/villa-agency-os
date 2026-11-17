"""FR-918 restore proof: a backup is accepted only after it was restored and verified.

Every database below is synthetic and lives under ``tmp_path``; ``data/villa.db`` is never
opened. The refusal tests are only evidence if a checker that skips validation would fail
them, so ``test_negative_control_*`` weakens the checker and expects the same file to pass.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import preflight as PF  # noqa: E402
import restore_proof as RP  # noqa: E402
from anchors import publish_anchor  # noqa: E402
from audit import APPEND_ONLY_TRIGGERS, AUDIT_TABLE, AuditLog  # noqa: E402
from primitives import IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite, required_migrations  # noqa: E402

EVENTS = 3


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _live(tmp_path: Path, names=None, events: int = EVENTS) -> sqlite3.Connection:
    """A synthetic, fully migrated database with a few audit events."""
    conn = connect_sqlite(str(tmp_path / "live.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT)"
    )
    for name in required_migrations() if names is None else names:
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, '2026-11-17T00:00:00+00:00', 'synthetic')",
            (name,),
        )
    audit = AuditLog(conn, clock=SystemClock(), ids=IdFactory(True, "rp"))
    for i in range(events):
        audit.append(
            action="synthetic.event", entity_type="synthetic", entity_id=f"e{i}",
            actor="tester", actor_role="owner",
        )
    return conn


def _publish(conn: sqlite3.Connection, directory: Path) -> None:
    directory.mkdir(exist_ok=True)
    publish_anchor(
        conn=conn, audit=AuditLog(conn, clock=SystemClock(), ids=IdFactory(True, "pub")),
        clock=SystemClock(), directory=directory, actor="ops.anchor", actor_role="operations",
    )


def _backup(conn: sqlite3.Connection, dest: Path) -> Path:
    """The Online Backup API, i.e. what ``sqlite3 db .backup`` does."""
    target = sqlite3.connect(dest)
    conn.backup(target)
    target.close()
    return dest


@pytest.fixture
def good(tmp_path):
    """(backup, anchor_dir, expected_chain_seq): a correct backup taken after the anchor."""
    conn = _live(tmp_path)
    anchors = tmp_path / "anchors"
    _publish(conn, anchors)
    seq = conn.execute(f"SELECT COUNT(*) FROM {AUDIT_TABLE}").fetchone()[0]
    path = _backup(conn, tmp_path / "villa-backup.sqlite")
    conn.close()
    return path, anchors, seq


class TestPositive:
    def test_a_correct_backup_is_restored_and_verified(self, good):
        backup, anchors, seq = good
        proof = RP.prove_restore(backup, anchor_dir=anchors)
        assert proof.verdict == RP.OK, proof.detail
        for fact in ("integrity ok", "ledger complete", f"audit chain intact (seq {seq})",
                     "verified"):
            assert fact in proof.detail  # the restored copy really carries the data
        assert proof.restore_seconds is not None and proof.restore_seconds >= 0
        assert proof.started_at.endswith("+00:00")
        assert "measured on this file only" in proof.detail  # not presented as a production RTO

    def test_the_source_backup_and_its_directory_are_untouched(self, good):
        backup, anchors, _ = good
        before_sha = _sha(backup)
        before_dir = sorted(p.name for p in backup.parent.iterdir())
        RP.prove_restore(backup, anchor_dir=anchors)
        assert _sha(backup) == before_sha
        assert sorted(p.name for p in backup.parent.iterdir()) == before_dir  # no -wal/-shm

    def test_the_check_adds_no_table_to_the_source(self, good):
        backup, anchors, _ = good
        names = lambda: sorted(  # noqa: E731
            r[0] for r in sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
            .execute("SELECT name FROM sqlite_master")
        )
        before = names()
        RP.prove_restore(backup, anchor_dir=anchors)
        assert names() == before

    def test_only_its_own_temporary_directory_is_removed(self, good, tmp_path, monkeypatch):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        bystander = scratch / "someone-elses.txt"
        bystander.write_text("keep", encoding="utf-8")
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        backup, anchors, _ = good
        assert RP.prove_restore(backup, anchor_dir=anchors).verdict == RP.OK
        assert [p.name for p in scratch.iterdir()] == ["someone-elses.txt"]

    def test_without_an_anchor_directory_the_anchor_is_not_verified_not_ok(self, good):
        proof = RP.prove_restore(good[0], anchor_dir=None)
        assert proof.verdict == RP.NOT_VERIFIED
        assert "anchor not verified" in proof.detail


class TestRefusals:
    def _refused(self, backup, anchors=None, *needles):
        proof = RP.prove_restore(backup, anchor_dir=anchors)
        assert proof.verdict == RP.FAIL, proof.detail
        for needle in needles:
            assert needle in proof.detail, proof.detail
        return proof

    def test_an_8192_byte_junk_file_is_refused(self, tmp_path):
        junk = tmp_path / "junk.sqlite"
        junk.write_bytes(b"x" * 8192)
        self._refused(junk, None, "cannot restore or read the backup")

    def test_a_valid_sqlite_file_without_the_villa_schema_is_refused(self, tmp_path):
        empty = tmp_path / "empty.sqlite"
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE pad (x TEXT)")
        conn.executemany("INSERT INTO pad VALUES (?)", [("y" * 200,)] * 60)
        conn.commit()
        conn.close()
        assert empty.stat().st_size > RP_MIN
        self._refused(empty, None, "not a Villa Agency database")
        tables = sqlite3.connect(empty).execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert tables == [("pad",)]  # the check did not build a schema on it

    def test_an_incomplete_ledger_names_what_is_missing(self, tmp_path):
        names = [n for n in required_migrations() if n != "010_agency_roles"]
        conn = _live(tmp_path, names)
        backup = _backup(conn, tmp_path / "b.sqlite")
        conn.close()
        self._refused(backup, None, "1 migration(s) not recorded", "010_agency_roles")

    def test_a_backup_without_migration_018_is_refused(self, tmp_path):
        names = [n for n in required_migrations() if n != "018_workforce_ops"]
        conn = _live(tmp_path, names)
        backup = _backup(conn, tmp_path / "b.sqlite")
        conn.close()
        self._refused(backup, None, "018_workforce_ops")

    def test_a_backup_without_a_ledger_table_is_refused(self, tmp_path):
        conn = _live(tmp_path)
        conn.execute("DROP TABLE schema_migrations")
        backup = _backup(conn, tmp_path / "b.sqlite")
        conn.close()
        self._refused(backup, None, "missing table(s): schema_migrations")

    def test_a_tampered_audit_chain_is_refused(self, tmp_path):
        conn = _live(tmp_path)
        backup = _backup(conn, tmp_path / "b.sqlite")
        conn.close()
        edit = sqlite3.connect(backup)
        for trigger in APPEND_ONLY_TRIGGERS:  # the attacker works on a copy, triggers dropped
            edit.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        edit.execute(f"UPDATE {AUDIT_TABLE} SET actor = 'someone_else' WHERE seq = 1")
        edit.commit()
        edit.close()
        self._refused(backup, None, "audit chain")

    def test_a_rewritten_row_with_triggers_restored_is_caught_by_the_hash_chain(self, tmp_path):
        conn = _live(tmp_path)
        backup = _backup(conn, tmp_path / "b.sqlite")
        conn.close()
        edit = sqlite3.connect(backup)
        ddl = [r[0] for r in edit.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (AUDIT_TABLE,))]
        for trigger in APPEND_ONLY_TRIGGERS:
            edit.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        edit.execute(f"UPDATE {AUDIT_TABLE} SET actor = 'someone_else' WHERE seq = 2")
        for statement in ddl:
            edit.execute(statement)  # the tamperer puts the guards back
        edit.commit()
        edit.close()
        proof = self._refused(backup, None, "audit chain")
        assert "UNENFORCED" not in proof.detail  # it was the hash, not the missing guard

    def test_a_corrupt_database_is_refused(self, good):
        backup, anchors, _ = good
        data = bytearray(backup.read_bytes())
        middle = (len(data) // 2) // 4096 * 4096
        data[middle:middle + 4096] = b"\xff" * 4096
        backup.write_bytes(bytes(data))
        self._refused(backup, anchors)

    def test_a_missing_backup_is_refused(self, tmp_path):
        self._refused(tmp_path / "nope.sqlite", None, "backup not found")

    def test_a_pending_wal_file_is_refused(self, good):
        backup, anchors, _ = good
        Path(str(backup) + "-wal").write_bytes(b"pending")
        self._refused(backup, anchors, "pending -wal")

    def test_a_pending_rollback_journal_is_refused(self, good):
        """immutable=1 would ignore a hot journal and read a half-written snapshot."""
        backup, anchors, _ = good
        Path(str(backup) + "-journal").write_bytes(b"pending")
        self._refused(backup, anchors, "pending -journal")

    def test_a_backup_that_changes_while_it_is_read_is_refused(self, good, monkeypatch):
        """The before/after sha256 is the proof that the 'stable snapshot' assumption held."""
        backup, anchors, _ = good
        real = RP._verify_restored

        def touch_then_verify(ro, anchor_dir):
            with backup.open("ab") as handle:
                handle.write(b"\0")
            return real(ro, anchor_dir)

        monkeypatch.setattr(RP, "_verify_restored", touch_then_verify)
        self._refused(backup, anchors, "changed during the proof")

    def test_a_missing_anchor_directory_is_refused(self, good, tmp_path):
        self._refused(good[0], tmp_path / "no-such-dir", "anchor directory missing")

    def test_an_empty_anchor_directory_is_refused(self, good, tmp_path):
        empty = tmp_path / "empty-anchors"
        empty.mkdir()
        self._refused(good[0], empty, "ANCHOR_NOT_FOUND")

    def test_a_backup_older_than_the_published_anchor_is_refused(self, tmp_path):
        """Restoring it would lose events the anchor already vouches for."""
        conn = _live(tmp_path)
        backup = _backup(conn, tmp_path / "old.sqlite")
        anchors = tmp_path / "anchors"
        _publish(conn, anchors)  # published after the backup was taken
        conn.close()
        self._refused(backup, anchors, "anchor")

    def test_a_restore_that_overruns_its_budget_is_refused(self, good):
        proof = RP.prove_restore(good[0], anchor_dir=good[1], budget_seconds=0)
        assert proof.verdict == RP.FAIL
        assert "over the 0s budget" in proof.detail

    def test_negative_control_a_weakened_checker_lets_the_same_files_through(
        self, tmp_path, monkeypatch
    ):
        """The refusals above prove nothing unless a checker that skips them goes green."""
        empty = tmp_path / "empty.sqlite"
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE pad (x TEXT)")
        conn.commit()
        conn.close()
        assert RP.prove_restore(empty).verdict == RP.FAIL
        monkeypatch.setattr(RP, "_verify_restored", lambda ro, anchor_dir: ([], "weak", RP.OK))
        assert RP.prove_restore(empty).verdict == RP.OK


RP_MIN = 4096  # the old gate's size floor: the empty file above would have passed it


class TestPreflightWiring:
    def test_no_backup_given_is_unchecked_and_production_refuses_it(self):
        check = PF.check_restore_proof(None, None)
        assert check.verdict == PF.WARN
        assert PF.escalate_unchecked(check).verdict == PF.FAIL

    def test_junk_that_passed_the_old_gate_now_fails(self, tmp_path):
        junk = tmp_path / "villa-backup.sqlite"
        junk.write_bytes(b"x" * 8192)
        assert PF.check_backup(junk).verdict == PF.OK  # the old, file-level gate: fooled
        assert PF.check_restore_proof(junk, None).verdict == PF.FAIL

    def test_a_proven_backup_passes_and_an_unanchored_one_is_unchecked(self, good):
        backup, anchors, _ = good
        assert PF.check_restore_proof(backup, anchors).verdict == PF.OK
        unanchored = PF.check_restore_proof(backup, None)
        assert unanchored.verdict == PF.WARN and unanchored.detail.startswith("not checked")
        assert PF.escalate_unchecked(unanchored).verdict == PF.FAIL

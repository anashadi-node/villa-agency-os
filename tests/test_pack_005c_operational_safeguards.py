"""PACK-005C focused tests for the backup engine, restore drill, and permissions.

All databases are synthetic files under ``tmp_path``. ``data/villa.db`` is never
opened by this module, and every failure test asserts that a false PASS is
impossible: an interrupted, corrupted, or mismatched backup remains unpublished or
is rejected by the restore verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import backup_sqlite  # noqa: E402
import check_backup_age  # noqa: E402
import harden_db_permissions  # noqa: E402
import verify_backup  # noqa: E402

SECRET = "guest-private-value@example.test"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dest(tmp_path: Path, name: str = "backups") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _source(tmp_path: Path, name: str = "source.db") -> tuple[sqlite3.Connection, Path]:
    """A small, migrated-shaped WAL source with one private-looking value."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT)"
    )
    for version in ("001_initial", "020_supplier_contacts"):
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, '2026-11-23T00:00:00+00:00', 'synthetic')",
            (version,),
        )
    conn.execute("CREATE TABLE schema_meta (schema_version TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES ('020_supplier_contacts')")
    conn.execute("CREATE TABLE guests (id INTEGER PRIMARY KEY, contact TEXT NOT NULL)")
    conn.execute("INSERT INTO guests(contact) VALUES (?)", (SECRET,))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes(body) VALUES ('synthetic note')")
    conn.commit()
    return conn, path


def _backup(tmp_path: Path, *, name: str | None = None):
    conn, source = _source(tmp_path)
    try:
        destination = _dest(tmp_path)
        manifest = backup_sqlite.create_backup(source, destination, name=name)
        return manifest, source, destination
    finally:
        conn.close()


class TestBackupEngine:
    def test_t005c_01_online_backup_is_a_consistent_sqlite_snapshot(self, tmp_path):
        manifest, source, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        restored = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            assert restored.execute("SELECT COUNT(*) FROM guests").fetchone()[0] == 1
            assert restored.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()
        assert not Path(str(backup) + "-wal").exists()
        assert not Path(str(backup) + "-shm").exists()

    def test_t005c_01b_closed_wal_source_does_not_trigger_false_mutation(self, tmp_path):
        conn, source = _source(tmp_path)
        conn.close()
        destination = _dest(tmp_path)
        manifest = backup_sqlite.create_backup(source, destination)
        assert manifest["source"]["sha256"] == _sha(source)
        assert not list(destination.glob("*.db.partial"))

    def test_t005c_01c_source_owner_is_verified(self, tmp_path, monkeypatch):
        conn, source = _source(tmp_path)
        try:
            destination = _dest(tmp_path)
            real_geteuid = os.geteuid
            monkeypatch.setattr(backup_sqlite.os, "geteuid", lambda: real_geteuid() + 1)
            with pytest.raises(backup_sqlite.BackupError) as caught:
                backup_sqlite.create_backup(source, destination)
            assert caught.value.code == "BACKUP_SOURCE_OWNER"
        finally:
            conn.close()

    def test_t005c_02_wal_contents_are_included(self, tmp_path):
        conn, source = _source(tmp_path)
        try:
            # The only committed row is still in the WAL; do not checkpoint or close.
            assert Path(str(source) + "-wal").exists()
            wal_before = _sha(Path(str(source) + "-wal"))
            destination = _dest(tmp_path)
            manifest = backup_sqlite.create_backup(source, destination)
            backup = destination / manifest["backup"]["file"]
            check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
            try:
                assert check.execute("SELECT contact FROM guests").fetchone()[0] == SECRET
            finally:
                check.close()
            assert _sha(Path(str(source) + "-wal")) == wal_before
        finally:
            conn.close()

    def test_t005c_03_backup_preserves_the_original_database(self, tmp_path):
        # Create the source and take the backup in one helper so the same open
        # connection is used for both the before and after hashes.
        conn, source = _source(tmp_path)
        try:
            before = {
                source: _sha(source),
                Path(str(source) + "-wal"): _sha(Path(str(source) + "-wal")),
            }
            destination = _dest(tmp_path)
            manifest = backup_sqlite.create_backup(source, destination)
            assert manifest["source"]["sha256"] == before[source]
            assert _sha(source) == before[source]
            assert _sha(Path(str(source) + "-wal")) == before[Path(str(source) + "-wal")]
            assert manifest["source"]["inventory"]["counts"]["guests"] == 1
            assert destination.stat().st_mode & 0o777 == 0o700
        finally:
            conn.close()

    def test_t005c_04_existing_backup_cannot_be_overwritten(self, tmp_path):
        conn, source = _source(tmp_path)
        try:
            destination = _dest(tmp_path)
            existing = destination / "fixed.db"
            existing.write_bytes(b"pre-existing")
            before = existing.read_bytes()
            with pytest.raises(backup_sqlite.BackupError) as caught:
                backup_sqlite.create_backup(source, destination, name="fixed.db")
            assert caught.value.code == "BACKUP_EXISTS"
            assert existing.read_bytes() == before
            assert not (destination / "fixed.db.manifest.json").exists()
        finally:
            conn.close()

    def test_t005c_05_backup_files_have_restrictive_permissions(self, tmp_path):
        manifest, source, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        manifest_file = destination / (backup.name + ".manifest.json")
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o600
        # The source's own mode is not silently changed by the backup.
        assert stat.S_IMODE(source.stat().st_mode) == 0o644

    def test_t005c_06_simultaneous_jobs_do_not_corrupt_output(self, tmp_path):
        conn, source = _source(tmp_path)
        try:
            destination = _dest(tmp_path)
            lock = backup_sqlite.lock_path(destination, source)
            with backup_sqlite.exclusive_lock(lock):
                env = dict(os.environ)
                env["PYTHONPATH"] = str(SCRIPTS)
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "backup_sqlite.py"),
                        "--db",
                        str(source),
                        "--dest-dir",
                        str(destination),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )
            assert proc.returncode == 1
            assert "BACKUP_ALREADY_RUNNING" in proc.stderr
            published = list(destination.glob("*.db"))
            assert published == []
        finally:
            conn.close()

    def test_t005c_07_interrupted_backup_is_not_marked_successful(self, tmp_path, monkeypatch):
        conn, source = _source(tmp_path)
        try:
            destination = _dest(tmp_path)

            def interrupt(source_path, target_path):
                target_path.write_bytes(b"incomplete")
                raise backup_sqlite.BackupError("BACKUP_INTERRUPTED", "simulated interruption")

            monkeypatch.setattr(backup_sqlite, "_copy_online", interrupt)
            with pytest.raises(backup_sqlite.BackupError) as caught:
                backup_sqlite.create_backup(source, destination, name="interrupted.db")
            assert caught.value.code == "BACKUP_INTERRUPTED"
            assert not (destination / "interrupted.db").exists()
            assert not (destination / "interrupted.db.manifest.json").exists()
            assert not list(destination.glob(".interrupted.db*"))
        finally:
            conn.close()

    def test_t005c_11_backup_failure_has_actionable_evidence_and_no_secret(self, tmp_path, capsys):
        destination = _dest(tmp_path)
        missing = tmp_path / "does-not-exist.db"
        result = backup_sqlite.main(["--db", str(missing), "--dest-dir", str(destination)])
        captured = capsys.readouterr()
        assert result == 1
        assert "BACKUP_SOURCE_MISSING" in captured.err
        assert str(missing) in captured.err
        assert "ACTION:" in captured.err
        assert SECRET not in captured.out + captured.err

    def test_manifest_and_operator_output_contain_no_row_values(self, tmp_path, capsys):
        conn, source = _source(tmp_path)
        try:
            destination = _dest(tmp_path)
            result = backup_sqlite.main(["--db", str(source), "--dest-dir", str(destination)])
            captured = capsys.readouterr()
            assert result == 0
            manifest_text = next(destination.glob("*.manifest.json")).read_text(encoding="utf-8")
            assert SECRET not in manifest_text
            assert SECRET not in captured.out + captured.err
        finally:
            conn.close()

    def test_insecure_destination_is_refused(self, tmp_path):
        conn, source = _source(tmp_path)
        try:
            destination = tmp_path / "shared"
            destination.mkdir()
            os.chmod(destination, 0o775)
            with pytest.raises(backup_sqlite.BackupError) as caught:
                backup_sqlite.create_backup(source, destination)
            assert caught.value.code == "BACKUP_DIR_INSECURE"
        finally:
            conn.close()


class TestRestoreVerification:
    def test_t005c_08_restore_passes_integrity_and_fk_checks(self, tmp_path):
        manifest, source, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        before = _sha(backup)
        result = verify_backup.verify_backup(backup)
        assert result["ok"] is True
        assert result["code"] == "RESTORE_VERIFIED"
        assert result["checks"]["integrity_check"] == "ok"
        assert result["checks"]["foreign_key_check_count"] == 0
        assert result["checks"]["migration_ledger"] == 2
        assert _sha(backup) == before
        assert not Path(str(backup) + "-wal").exists()

    def test_t005c_09_corrupted_backup_is_rejected(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        payload = bytearray(backup.read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        backup.write_bytes(payload)
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "BACKUP_CHECKSUM_FAILED"

    def test_t005c_09b_unmanifested_sidecar_is_rejected(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        Path(str(backup) + "-wal").write_bytes(b"unmanifested")
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "BACKUP_HAS_SIDECAR"

    def test_t005c_10_migration_ledger_mismatch_is_reported(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        manifest_file = destination / (backup.name + ".manifest.json")
        changed = json.loads(manifest_file.read_text(encoding="utf-8"))
        changed["source"]["inventory"]["migration_ledger"] = ["999_missing"]
        changed["backup"]["inventory"]["migration_ledger"] = ["999_missing"]
        manifest_file.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "RESTORE_LEDGER_MISMATCH"

    def test_t005c_10b_count_mismatch_is_reported(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        manifest_file = destination / (backup.name + ".manifest.json")
        changed = json.loads(manifest_file.read_text(encoding="utf-8"))
        changed["source"]["inventory"]["counts"]["guests"] = 999
        manifest_file.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "RESTORE_COUNT_MISMATCH"

    def test_t005c_11_missing_manifest_is_actionable(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        manifest_file = destination / (backup.name + ".manifest.json")
        manifest_file.unlink()
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "MANIFEST_MISSING"
        assert "not a verified backup" in caught.value.detail

    def test_t005c_14_restore_does_not_touch_an_operational_database(self, tmp_path):
        manifest, source, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        source_before = _sha(source)
        backup_before = _sha(backup)
        verify_backup.verify_backup(backup)
        assert _sha(source) == source_before
        assert _sha(backup) == backup_before
        assert sorted(p.name for p in destination.iterdir()) == sorted(
            [".backup-" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16] + ".lock",
             backup.name, backup.name + ".manifest.json"]
        )


class TestSchedulerAndPermissions:
    def test_t005c_12_timer_configuration_remains_disabled(self):
        root = SCRIPTS.parent / "deploy" / "systemd"
        service = (root / "villa-backup.service").read_text(encoding="utf-8")
        timer = (root / "villa-backup.timer").read_text(encoding="utf-8")
        assert "WantedBy=" not in timer
        assert not any(line.strip() == "[Install]" for line in timer.splitlines())
        assert "WantedBy=" not in service
        assert "ExecStart=" in service
        assert "VILLA_BACKUP_DB" in service
        assert "VILLA_BACKUP_DIR" in service
        assert "UMask=0077" in service

    def test_t005c_12b_backup_age_monitor_reports_fresh_and_stale(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        fresh = check_backup_age.audit(destination, max_age_hours=24)
        assert fresh["ok"] is True
        assert fresh["code"] == "BACKUP_AGE_OK"
        assert fresh["age_hours"] is not None

        manifest_file = destination / (manifest["backup"]["file"] + ".manifest.json")
        changed = json.loads(manifest_file.read_text(encoding="utf-8"))
        changed["created_at"] = "2000-01-01T00:00:00+00:00"
        manifest_file.write_text(json.dumps(changed), encoding="utf-8")
        stale = check_backup_age.audit(destination, max_age_hours=24)
        assert stale["ok"] is False
        assert stale["code"] == "BACKUP_AGE_STALE"

    def test_t005c_13_permission_hardening_works_on_disposable_files(self, tmp_path):
        conn, source = _source(tmp_path)
        try:
            directory = source.parent
            os.chmod(directory, 0o775)
            os.chmod(source, 0o644)
            wal = Path(str(source) + "-wal")
            if wal.exists():
                os.chmod(wal, 0o644)

            before = harden_db_permissions.plan(source)
            assert before["needs_hardening"] is True
            result = harden_db_permissions.apply(source)
            assert result["applied"] is True
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
            for target in result["targets"]:
                if target["kind"] != "directory":
                    assert stat.S_IMODE(Path(target["path"]).stat().st_mode) == 0o600
        finally:
            conn.close()

    def test_t005c_15_corrupt_input_cannot_be_promoted_to_a_pass(self, tmp_path):
        manifest, _, destination = _backup(tmp_path)
        backup = destination / manifest["backup"]["file"]
        original = backup.read_bytes()
        backup.write_bytes(original[:-1])
        with pytest.raises(verify_backup.BackupError) as caught:
            verify_backup.verify_backup(backup)
        assert caught.value.code == "BACKUP_CHECKSUM_FAILED"
        # No retry path creates a manifest or mutates the truncated file.
        assert backup.read_bytes() == original[:-1]
        assert (destination / (backup.name + ".manifest.json")).exists()

#!/usr/bin/env python3
"""Restore a backup into a disposable database and prove it is usable.

A file existing is not a restore proof. This tool checks the manifest checksum,
opens the backup read-only, restores it with SQLite's Online Backup API into a
private temporary directory, and compares the restored schema, row counts, and
migration ledger with both the source snapshot and the backup snapshot.

The operational database is never opened read-write and never replaced. The
temporary copy is deleted on every path, including failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from backup_common import (
    BACKUP_FORMAT,
    MANIFEST_SUFFIX,
    BackupError,
    connect_readonly,
    inventory,
    sha256_file,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackupError(
            "MANIFEST_MISSING",
            f"manifest does not exist: {path}",
            detail="a backup without its manifest is not a verified backup",
        ) from exc
    except (OSError, ValueError) as exc:
        raise BackupError("MANIFEST_INVALID", f"cannot read manifest: {path}", detail=str(exc)) from exc
    if not isinstance(value, dict) or value.get("format") != BACKUP_FORMAT:
        raise BackupError("MANIFEST_INVALID", f"unexpected manifest format: {path}")
    for key in ("source", "backup"):
        if not isinstance(value.get(key), dict):
            raise BackupError("MANIFEST_INVALID", f"manifest is missing {key}: {path}")
    for key in ("inventory",):
        if not isinstance(value["source"].get(key), dict):
            raise BackupError("MANIFEST_INVALID", f"manifest source is missing {key}: {path}")
        if not isinstance(value["backup"].get(key), dict):
            raise BackupError("MANIFEST_INVALID", f"manifest backup is missing {key}: {path}")
    if not isinstance(value["backup"].get("sha256"), str):
        raise BackupError("MANIFEST_INVALID", f"manifest backup checksum is missing: {path}")
    return value


def _check_backup_file(backup: Path, manifest: dict[str, Any]) -> None:
    if not backup.is_file():
        raise BackupError("BACKUP_MISSING", f"backup does not exist: {backup}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(backup) + suffix)
        if sidecar.exists():
            raise BackupError(
                "BACKUP_HAS_SIDECAR",
                f"backup has an unmanifested SQLite sidecar: {sidecar.name}",
                detail=(
                    "sidecars are not covered by the manifest checksum; restore from a "
                    "single-file backup produced by scripts/backup_sqlite.py"
                ),
            )
    declared_name = str(manifest["backup"].get("file", ""))
    if Path(declared_name).name != declared_name:
        raise BackupError("MANIFEST_INVALID", "manifest backup.file is not a bare file name")
    if declared_name != backup.name:
        raise BackupError(
            "MANIFEST_MISMATCH",
            "manifest describes a different backup file",
            detail=f"manifest={declared_name!r} actual={backup.name!r}",
        )
    actual_size = backup.stat().st_size
    if manifest["backup"].get("size") != actual_size:
        raise BackupError(
            "BACKUP_CHECKSUM_FAILED",
            "backup size does not match its manifest",
            detail=f"manifest={manifest['backup'].get('size')} actual={actual_size}",
        )
    actual_hash = sha256_file(backup)
    if actual_hash != manifest["backup"]["sha256"]:
        raise BackupError(
            "BACKUP_CHECKSUM_FAILED",
            "backup SHA-256 does not match its manifest",
            detail="the file was modified or truncated; restore it from a trusted copy",
        )


def _restore_to_private_temp(backup: Path) -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="villa-restore-"))
    # Owner-only directory traversal; 0700 is the restrictive target here.
    os.chmod(directory, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    target = directory / "restored.db"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    source_conn: Optional[sqlite3.Connection] = None
    target_conn: Optional[sqlite3.Connection] = None
    try:
        source_conn = connect_readonly(backup)
        target_conn = sqlite3.connect(str(target))
        source_conn.backup(target_conn)
        target_conn.commit()
    finally:
        if target_conn is not None:
            target_conn.close()
        if source_conn is not None:
            source_conn.close()
    os.chmod(target, 0o600)
    return directory, target


def _verify_restored(restored: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    conn = connect_readonly(restored, immutable=True)
    try:
        integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        restored_inventory = inventory(conn)
    finally:
        conn.close()
    if integrity_rows != ["ok"]:
        raise BackupError(
            "RESTORE_INTEGRITY_FAILED",
            "restored database failed SQLite integrity_check",
            detail=", ".join(integrity_rows)[:500],
        )
    if foreign_keys:
        raise BackupError(
            "RESTORE_FOREIGN_KEY_FAILED",
            f"restored database has {len(foreign_keys)} foreign-key violation(s)",
        )

    expected_backup = manifest["backup"]["inventory"]
    expected_source = manifest["source"]["inventory"]
    for key, code in (
        ("tables", "RESTORE_INVENTORY_MISMATCH"),
        ("indexes", "RESTORE_INVENTORY_MISMATCH"),
        ("triggers", "RESTORE_INVENTORY_MISMATCH"),
        ("migration_ledger", "RESTORE_LEDGER_MISMATCH"),
        ("schema_version", "RESTORE_INVENTORY_MISMATCH"),
        ("counts", "RESTORE_COUNT_MISMATCH"),
    ):
        if restored_inventory.get(key) != expected_backup.get(key):
            raise BackupError(
                code,
                f"restored {key} does not match the backup manifest",
                detail="the backup is incomplete or was not produced by this backup engine",
            )
        if key == "counts" and expected_source.get(key) != restored_inventory.get(key):
            raise BackupError(
                "RESTORE_COUNT_MISMATCH",
                "restored record counts do not match the source snapshot",
                detail="do not treat this backup as recoverable; investigate before retention decisions",
            )
    if expected_source.get("migration_ledger") != restored_inventory.get("migration_ledger"):
        raise BackupError(
            "RESTORE_LEDGER_MISMATCH",
            "restored migration ledger does not match the source snapshot",
        )
    if not restored_inventory.get("migration_ledger"):
        raise BackupError(
            "RESTORE_LEDGER_MISSING",
            "restored database has no migration ledger",
            detail="expected schema_migrations rows in an operational backup",
        )
    return {
        "tables": len(restored_inventory["tables"]),
        "indexes": len(restored_inventory["indexes"]),
        "triggers": len(restored_inventory["triggers"]),
        "migration_ledger": len(restored_inventory["migration_ledger"]),
        "schema_version": restored_inventory.get("schema_version"),
        "integrity_check": "ok",
        "foreign_key_check_count": 0,
    }


def verify_backup(backup_path: Path, manifest_path: Optional[Path] = None) -> dict[str, Any]:
    backup = Path(backup_path).expanduser().resolve()
    manifest_file = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else Path(str(backup) + MANIFEST_SUFFIX)
    )
    manifest = _load_manifest(manifest_file)

    before_hash = sha256_file(backup) if backup.is_file() else None
    _check_backup_file(backup, manifest)
    directory: Optional[Path] = None
    try:
        directory, restored = _restore_to_private_temp(backup)
        checks = _verify_restored(restored, manifest)
    finally:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    after_hash = sha256_file(backup)
    if before_hash != after_hash:
        raise BackupError(
            "BACKUP_CHANGED_DURING_VERIFICATION",
            "the backup file changed while it was being verified",
        )
    return {
        "ok": True,
        "code": "RESTORE_VERIFIED",
        "backup": str(backup),
        "manifest": str(manifest_file),
        "sha256": manifest["backup"]["sha256"],
        "source_counts": manifest["source"]["inventory"].get("counts", {}),
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore one backup into a disposable database and verify it."
    )
    parser.add_argument("--backup", required=True, help="backup .db file")
    parser.add_argument("--manifest", default=None, help="manifest path (default: <backup>.manifest.json)")
    parser.add_argument("--json", action="store_true", help="print a machine-readable result")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_backup(Path(args.backup), Path(args.manifest) if args.manifest else None)
    except BackupError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"ACTION: {exc.detail}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:
        print(f"ERROR RESTORE_UNEXPECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if args.json:
        # The per-table count map is evidence, not guest data; keep it out of the
        # default log to stay small and avoid accidental screenshots of schemas.
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"OK restore_verified={Path(result['backup']).name} "
            f"tables={result['checks']['tables']} "
            f"ledger={result['checks']['migration_ledger']} "
            f"schema={result['checks']['schema_version']}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

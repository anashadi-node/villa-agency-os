#!/usr/bin/env python3
"""Create a consistent, private SQLite backup with a checksum manifest.

The source is opened read-only and copied through SQLite's Online Backup API, so a
database in WAL mode is captured as one consistent snapshot without a `cp` race.
Publication is exclusive and crash-safe: a completed database is linked into place
first, and its manifest is linked last as the completion marker. A file without a
manifest is not a verified backup.

No row values, guest data, credentials, or payment data are written to the
manifest or the operator log. The manifest contains names, counts, and hashes.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from backup_common import (
    BACKUP_FORMAT,
    MANIFEST_SUFFIX,
    BackupError,
    connect_readonly,
    ensure_private_dir,
    exclusive_lock,
    identity_differences,
    inventory,
    lock_path,
    publish_exclusive,
    secure_file,
    sha256_file,
    source_identity,
    unique_name,
    utc_now,
    write_new_json,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _validate_source(path: Path) -> Path:
    try:
        source = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise BackupError(
            "BACKUP_SOURCE_MISSING",
            f"source database does not exist: {path}",
            detail="check --db and the service's read access",
        ) from exc
    if not source.is_file():
        raise BackupError("BACKUP_SOURCE_NOT_FILE", f"source is not a file: {source}")
    info = source.stat()
    if info.st_uid != os.geteuid():
        raise BackupError(
            "BACKUP_SOURCE_OWNER",
            f"source is owned by uid {info.st_uid}, not the current user: {source}",
            detail="run the backup as the database owner; do not broaden source permissions",
        )
    return source


def _open_private_destination(path: Path) -> None:
    """Pre-create the target so SQLite cannot choose a broader mode."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise BackupError("FILE_EXISTS", f"refusing to overwrite: {path}") from exc
    os.close(fd)
    os.chmod(path, 0o600)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_online(source: Path, target: Path) -> None:
    """Copy committed pages, including committed WAL contents, without writing source."""
    _open_private_destination(target)
    source_conn = connect_readonly(source)
    target_conn: Optional[sqlite3.Connection] = None
    try:
        target_conn = sqlite3.connect(str(target))
        source_conn.backup(target_conn)
        target_conn.commit()
        # A backup must be one self-contained file. The source's WAL flag can be
        # copied into page 1; checkpoint and switch the copy to DELETE so no
        # uncommitted-looking -wal/-shm pair is left beside the published file.
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        if target_conn is not None:
            target_conn.close()
        source_conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(target) + suffix).unlink()
        except FileNotFoundError:
            pass
    os.chmod(target, 0o600)
    _fsync_file(target)


def _integrity_and_fk(path: Path) -> tuple[dict[str, Any], str, int]:
    conn = connect_readonly(path)
    try:
        integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        value = inventory(conn)
    finally:
        conn.close()
    if integrity_rows != ["ok"]:
        raise BackupError(
            "BACKUP_INTEGRITY_FAILED",
            f"SQLite integrity_check did not return ok for {path.name}",
            detail=", ".join(integrity_rows)[:500],
        )
    if foreign_keys:
        raise BackupError(
            "BACKUP_FOREIGN_KEY_FAILED",
            f"foreign_key_check found {len(foreign_keys)} violation(s) in {path.name}",
            detail="the backup was not published; inspect the source's referential integrity",
        )
    return value, "ok", len(foreign_keys)


def _compare_inventory(source: dict[str, Any], restored: dict[str, Any]) -> None:
    for key in ("tables", "indexes", "triggers", "counts", "migration_ledger", "schema_version"):
        if source.get(key) != restored.get(key):
            raise BackupError(
                "BACKUP_INVENTORY_MISMATCH",
                f"backup {key} differs from the source snapshot",
                detail="the source changed during the backup or the copy is incomplete; no backup was published",
            )


def create_backup(
    db_path: Path,
    dest_dir: Path,
    *,
    label: Optional[str] = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """Create and publish one backup; return its manifest."""
    source = _validate_source(Path(db_path))
    destination = ensure_private_dir(Path(dest_dir))
    chosen_name = name or unique_name(source, label=label)
    if Path(chosen_name).name != chosen_name:
        raise BackupError("BACKUP_NAME_INVALID", "--name must be a bare file name")
    final_db = destination / chosen_name
    final_manifest = destination / (chosen_name + MANIFEST_SUFFIX)
    if final_db.exists() or final_manifest.exists():
        raise BackupError("BACKUP_EXISTS", f"refusing to overwrite: {chosen_name}")

    with exclusive_lock(lock_path(destination, source)):
        before = source_identity(source)
        source_conn = connect_readonly(source)
        try:
            source_snapshot = inventory(source_conn)
        finally:
            source_conn.close()

        temp_db = destination / f".{chosen_name}.partial"
        temp_manifest = destination / f".{chosen_name}{MANIFEST_SUFFIX}.partial"
        try:
            _copy_online(source, temp_db)
            secure_file(temp_db)
            backup_snapshot, integrity, fk_count = _integrity_and_fk(temp_db)
            _compare_inventory(source_snapshot, backup_snapshot)

            after = source_identity(source)
            differences = identity_differences(before, after)
            if differences:
                raise BackupError(
                    "SOURCE_CHANGED_DURING_BACKUP",
                    "the source database changed while the backup was running",
                    detail=", ".join(differences) + "; retry when writers are quiet",
                )
            source_after_conn = connect_readonly(source)
            try:
                source_after = inventory(source_after_conn)
            finally:
                source_after_conn.close()
            if source_after != source_snapshot:
                raise BackupError(
                    "SOURCE_CHANGED_DURING_BACKUP",
                    "the source's schema or counts changed while the backup was running",
                    detail="retry when writers are quiet; no backup was published",
                )

            manifest: dict[str, Any] = {
                "format": BACKUP_FORMAT,
                "created_at": utc_now().isoformat(),
                "source": {**before, "inventory": source_snapshot},
                "backup": {
                    "file": chosen_name,
                    "sha256": sha256_file(temp_db),
                    "size": temp_db.stat().st_size,
                    "inventory": backup_snapshot,
                    "integrity_check": integrity,
                    "foreign_key_check_count": fk_count,
                },
                "tool": {
                    "name": "villa-backup-sqlite",
                    "format_version": 1,
                    "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version,
                },
            }
            write_new_json(temp_manifest, manifest)
            secure_file(temp_manifest)
            # Database first; manifest last is the completion marker. A crash
            # between these steps leaves an unverified database, never a false PASS.
            publish_exclusive(temp_db, final_db)
            publish_exclusive(temp_manifest, final_manifest)
            return manifest
        finally:
            for temporary in (
                temp_db,
                temp_manifest,
                Path(str(temp_db) + "-wal"),
                Path(str(temp_db) + "-shm"),
                Path(str(temp_db) + "-journal"),
            ):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "format": manifest["format"],
        "created_at": manifest["created_at"],
        "backup": {
            "file": manifest["backup"]["file"],
            "sha256": manifest["backup"]["sha256"],
            "size": manifest["backup"]["size"],
        },
        "source": {
            "path": manifest["source"]["path"],
            "sha256": manifest["source"]["sha256"],
            "size": manifest["source"]["size"],
        },
        "tables": len(manifest["backup"]["inventory"]["tables"]),
        "schema_version": manifest["backup"]["inventory"]["schema_version"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a private, checksum-manifested SQLite backup."
    )
    parser.add_argument("--db", required=True, help="source SQLite database path")
    parser.add_argument("--dest-dir", required=True, help="private backup directory (0700)")
    parser.add_argument("--label", default=None, help="optional short label for the file name")
    parser.add_argument("--name", default=None, help="exact bare backup file name (tests/ops)")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = create_backup(
            Path(args.db), Path(args.dest_dir), label=args.label, name=args.name
        )
    except BackupError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"ACTION: {exc.detail}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # fail closed, with a code the operator can search
        print(f"ERROR BACKUP_UNEXPECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    summary = _summary(manifest)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"OK backup={summary['backup']['file']} "
            f"sha256={summary['backup']['sha256'][:16]}... "
            f"tables={summary['tables']} schema={summary['schema_version']}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

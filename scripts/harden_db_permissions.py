#!/usr/bin/env python3
"""Inspect or apply the planned SQLite database permission hardening.

Default is check-only. ``--apply`` is deliberately explicit and refuses to touch a
path it does not own. This tool exists so the C4 plan can be exercised against a
disposable directory; it is not run against a live database by this pack.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

DIR_MODE = 0o700
FILE_MODE = 0o600
SIDECARS = ("-wal", "-shm", "-journal")
EXIT_OK = 0
EXIT_NEEDS_HARDENING = 1
EXIT_USAGE = 2


def plan(db_path: Path) -> dict[str, Any]:
    requested = Path(db_path).expanduser()
    if requested.is_symlink():
        raise ValueError(f"database path is a symlink: {requested}")
    db = requested.resolve(strict=True)
    if not db.is_file():
        raise ValueError(f"database is not a regular file: {db}")
    directory = db.parent
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"database directory is not a regular directory: {directory}")

    targets: list[dict[str, Any]] = [
        {
            "path": str(directory),
            "kind": "directory",
            "mode": stat.S_IMODE(directory.stat().st_mode),
            "target_mode": DIR_MODE,
            "owner_uid": directory.stat().st_uid,
        },
        {
            "path": str(db),
            "kind": "database",
            "mode": stat.S_IMODE(db.stat().st_mode),
            "target_mode": FILE_MODE,
            "owner_uid": db.stat().st_uid,
        },
    ]
    for suffix in SIDECARS:
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError(f"sidecar is not a regular file: {sidecar}")
            targets.append(
                {
                    "path": str(sidecar),
                    "kind": f"sidecar{suffix}",
                    "mode": stat.S_IMODE(sidecar.stat().st_mode),
                    "target_mode": FILE_MODE,
                    "owner_uid": sidecar.stat().st_uid,
                }
            )
    for target in targets:
        target["needs_hardening"] = stat.S_IMODE(target["mode"]) != target["target_mode"]
    return {
        "database": str(db),
        "directory": str(directory),
        "targets": targets,
        "needs_hardening": any(target["needs_hardening"] for target in targets),
        "service_user_uid": os.geteuid(),
        "owned_by_current_user": all(
            target["owner_uid"] == os.geteuid() for target in targets
        ),
    }


def _chmod_nofollow(path: Path, mode: int, *, directory: bool = False) -> int:
    """fchmod through a no-follow descriptor; return the resulting mode."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise PermissionError(f"path is not owned by the current user: {path}")
        os.fchmod(fd, mode)
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def apply(db_path: Path) -> dict[str, Any]:
    result = plan(db_path)
    if not result["owned_by_current_user"]:
        raise PermissionError(
            "refusing to change ownership/paths not owned by the current user; "
            "run as the service account that owns the database"
        )
    for target in result["targets"]:
        path = Path(target["path"])
        current = stat.S_IMODE(path.lstat().st_mode)
        if target["kind"] == "directory":
            # Make the directory searchable only by its owner before touching files.
            after = _chmod_nofollow(path, DIR_MODE, directory=True)
        else:
            after = _chmod_nofollow(path, FILE_MODE)
        if after != target["target_mode"]:
            raise OSError(f"chmod did not reach {oct(target['target_mode'])}: {path}")
        target["previous_mode"] = current
        target["mode"] = after
        target["needs_hardening"] = False
    result["needs_hardening"] = False
    result["applied"] = True
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check (default) or apply private SQLite/database permissions."
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--apply", action="store_true", help="apply chmod (owner only)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = apply(Path(args.db)) if args.apply else plan(Path(args.db))
    except (ValueError, OSError, PermissionError) as exc:
        print(f"ERROR PERMISSION_PLAN: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for target in result["targets"]:
            print(f"{target['kind']}: {target['path']} {oct(target['mode'])} -> {oct(target['target_mode'])}")
        print("state:", "APPLIED" if result.get("applied") else ("NEEDS_HARDENING" if result["needs_hardening"] else "COMPLIANT"))
    return EXIT_OK if not result["needs_hardening"] else EXIT_NEEDS_HARDENING


if __name__ == "__main__":
    raise SystemExit(main())

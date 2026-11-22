#!/usr/bin/env python3
"""Read-only backup-age check for the disabled PACK-005C scheduler.

This tool never creates, deletes, or moves a backup. It reads the newest manifest
timestamp and returns a non-zero code when the configured RPO is exceeded, so an
operator or timer failure handler can surface a stale backup without implementing
an unapproved retention policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backup_common import BACKUP_FORMAT, MANIFEST_SUFFIX, BackupError

EXIT_OK = 0
EXIT_STALE = 1
EXIT_USAGE = 2
DEFAULT_MAX_AGE_HOURS = 24.0


def _parse_created_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("created_at is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at has no timezone")
    return parsed.astimezone(timezone.utc)


def audit(backup_dir: Path, *, max_age_hours: float = DEFAULT_MAX_AGE_HOURS, now: Optional[datetime] = None) -> dict[str, Any]:
    directory = Path(backup_dir).expanduser()
    if not directory.is_dir():
        raise BackupError("BACKUP_DIR_MISSING", f"backup directory does not exist: {directory}")
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    manifests = sorted(directory.glob("*" + MANIFEST_SUFFIX))
    if not manifests:
        return {
            "ok": False,
            "code": "BACKUP_AGE_NO_BACKUP",
            "directory": str(directory),
            "max_age_hours": max_age_hours,
        }

    newest: Optional[dict[str, Any]] = None
    newest_at: Optional[datetime] = None
    for manifest_file in manifests:
        try:
            value = json.loads(manifest_file.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("format") != BACKUP_FORMAT:
                continue
            created = _parse_created_at(value.get("created_at"))
            backup_name = value.get("backup", {}).get("file")
            if not isinstance(backup_name, str) or Path(backup_name).name != backup_name:
                continue
            backup_file = directory / backup_name
            if not backup_file.is_file():
                continue
        except (OSError, ValueError, TypeError):
            continue
        if newest_at is None or created > newest_at:
            newest_at = created
            newest = {
                "manifest": str(manifest_file),
                "backup": str(backup_file),
                "created_at": created.isoformat(),
                "age_hours": None,
            }

    if newest is None or newest_at is None:
        return {
            "ok": False,
            "code": "BACKUP_AGE_NO_VALID_MANIFEST",
            "directory": str(directory),
            "max_age_hours": max_age_hours,
        }

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_hours = (current - newest_at).total_seconds() / 3600.0
    newest["age_hours"] = round(age_hours, 3)
    fresh = age_hours <= max_age_hours
    return {
        "ok": fresh,
        "code": "BACKUP_AGE_OK" if fresh else "BACKUP_AGE_STALE",
        "directory": str(directory),
        "max_age_hours": max_age_hours,
        **newest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report whether the newest backup is within its RPO.")
    parser.add_argument("--backup-dir", required=True, help="private backup directory")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = audit(Path(args.backup_dir), max_age_hours=args.max_age_hours)
    except (BackupError, ValueError, OSError) as exc:
        print(f"ERROR BACKUP_AGE: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"{result['code']} age_hours={result.get('age_hours')} "
            f"max_age_hours={result['max_age_hours']} directory={result['directory']}"
        )
    return EXIT_OK if result["ok"] else EXIT_STALE


if __name__ == "__main__":
    raise SystemExit(main())

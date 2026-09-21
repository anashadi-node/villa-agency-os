#!/usr/bin/env python3
"""Publish or verify the audit-chain anchor from the command line.

Run this on a schedule (weekly is enough) from a *different* machine or account
than the database writer: that is what turns the hash chain into evidence that
old rows still exist.

    VILLA_DB=data/villa.db python3 scripts/publish_anchor.py publish
    VILLA_DB=data/villa.db python3 scripts/publish_anchor.py verify

Environment:
    VILLA_DB            path to the SQLite database (default: data/villa.db)
    VILLA_ANCHOR_DIR    where anchor files are written (default: data/anchors)
    VILLA_ANCHOR_LABEL  optional free-text label stored in the anchor file
    VILLA_ACTOR         actor recorded in the audit chain (default: ops.anchor)

Exit codes: 0 = success, 1 = the operation was refused (chain problem or tamper),
2 = usage/configuration problem. A non-zero exit from ``verify`` means the audit
chain or its published anchors must be treated as suspect *now*.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anchors import (  # noqa: E402  (path setup must run first)
    AnchorError,
    latest_published,
    publish_anchor,
    verify_published_anchors,
)
from audit import AuditLog  # noqa: E402
from primitives import DomainError, IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_DB = "data/villa.db"
DEFAULT_ANCHOR_DIR = "data/anchors"


def _settings() -> tuple[Path, Path, str, str]:
    db_path = Path(os.environ.get("VILLA_DB", DEFAULT_DB))
    anchor_dir = Path(os.environ.get("VILLA_ANCHOR_DIR", DEFAULT_ANCHOR_DIR))
    actor = os.environ.get("VILLA_ACTOR", "ops.anchor")
    label = os.environ.get("VILLA_ANCHOR_LABEL", "")
    return db_path, anchor_dir, actor, label


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("publish", "verify", "status"),
        help="publish a new anchor, verify against published anchors, or show the latest",
    )
    parser.add_argument(
        "--actor-role", default="operations",
        help="role recorded with the publication audit event",
    )
    args = parser.parse_args(argv)

    db_path, anchor_dir, actor, label = _settings()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    if args.command in {"publish", "verify"} and not anchor_dir.is_dir():
        print(
            f"anchor directory not found: {anchor_dir} — create it first "
            "(it should live outside the database volume)",
            file=sys.stderr,
        )
        return 2

    conn = connect_sqlite(str(db_path))
    try:
        audit = AuditLog(conn, clock=SystemClock(), ids=IdFactory())
        if args.command == "publish":
            published = publish_anchor(
                conn=conn, audit=audit, clock=SystemClock(), directory=anchor_dir,
                actor=actor, actor_role=args.actor_role,
                label=label or None,
            )
            print(
                f"anchor published: file={published.file_name} "
                f"count={published.count} head={published.head_hash[:16]}… "
                f"sha256={published.file_sha256[:16]}…"
            )
            return 0
        if args.command == "verify":
            report = verify_published_anchors(
                conn=conn, audit=audit, directory=anchor_dir,
            )
            print(
                f"anchor verified: file={report.latest_file} count={report.count} "
                f"head={report.head_hash[:16]}… files_checked={report.files_checked} "
                "issues=0"
            )
            return 0

        latest = latest_published(anchor_dir) if anchor_dir.is_dir() else None
        if latest is None:
            print("no published anchor yet")
            return 0
        print(
            f"latest anchor: file={latest.file_name} count={latest.count} "
            f"head={latest.head_hash[:16]}… published_at={latest.published_at}"
        )
        return 0
    except AnchorError as exc:
        print(f"ANCHOR REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except DomainError as exc:
        print(f"CHAIN REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

#!/usr/bin/env python3
"""Apply pending SQLite migrations to an existing villa-agency database.

Why this exists: ``schema_loader.apply_schema`` seeds the catalogue when the database file
is *created*, and refuses to re-apply the initial schema to a database that already exists
("write a migration instead of re-applying the initial schema"). Rows added to the initial
schema after a database was built therefore never reach that database — which is exactly how
a new policy key becomes invisible on a running system, and how the discount engine ends up
refusing every request with ``POLICY_UNKNOWN_KEY`` for a key that is right there in the
schema file.

The bookkeeping lives in its own ``schema_migrations`` table, deliberately **not** in
``schema_meta``: ``applied_version()`` reads the newest row there and ``apply_schema`` raises
when it does not equal the build's ``SCHEMA_VERSION`` — so recording a migration in that
table would make every migrated database refuse to open.

Discipline, as with the policy CLI: ``VILLA_DB`` must be set explicitly. A migration that
runs against whichever ``data/villa.db`` happens to sit in the working directory is not a
decision anyone made.

Usage:
    VILLA_DB=data/villa.db python3 scripts/apply_migrations.py --dry-run
    VILLA_DB=data/villa.db python3 scripts/apply_migrations.py
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import schema_loader as SL  # noqa: E402

BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT
)
"""


def fail(message: str, code: int = 2) -> NoReturn:
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(code)


def db_path() -> Path:
    """The database to migrate — named explicitly, never guessed."""
    raw = os.environ.get("VILLA_DB", "").strip()
    if not raw:
        fail(
            "VILLA_DB is not set. Migrations must name their database explicitly "
            "(e.g. VILLA_DB=data/villa.db) — migrating whichever database happens to be "
            "in the working directory is not a decision anyone made."
        )
    path = Path(raw)
    if not path.exists():
        fail(f"VILLA_DB={raw} does not exist; nothing to migrate")
    return path


def migrations() -> list[tuple[str, Path]]:
    """Every migration to apply: the numbered .sql files that are not the initial schema.

    The PostgreSQL dialect file is skipped: this runner speaks SQLite. The initial schema
    is skipped because it is applied at creation, not migrated into.
    """
    initial = SL.DIALECT_FILES["sqlite"]
    found = []
    for path in sorted(SL.SCHEMA_DIR.glob("*.sql")):
        if path.name == initial or path.name.endswith(".pg.sql"):
            continue
        found.append((path.stem, path))
    return found


def applied(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def describe(path: Path) -> str:
    """The first comment line that is not the filename, for the record."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and len(stripped) > 4:
            return stripped[2:].strip()
    return path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending SQLite migrations.")
    parser.add_argument("--dry-run", action="store_true", help="list what would run, write nothing")
    args = parser.parse_args(argv)

    path = db_path()
    conn = SL.connect_sqlite(str(path))
    try:
        if not args.dry_run:
            conn.execute(BOOKKEEPING_DDL)
            conn.commit()
        try:
            done = applied(conn)
        except Exception:
            done = set()

        pending = [(name, p) for name, p in migrations() if name not in done]
        if not pending:
            print(f"nothing to do — {path} has every migration applied")
            return 0

        for name, p in pending:
            print(f"{'would apply' if args.dry_run else 'applying'} {name} — {describe(p)}")

        if args.dry_run:
            print(f"{len(pending)} pending; nothing was written")
            return 0

        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for name, p in pending:
            statements = SL.split_statements(p.read_text(encoding="utf-8"))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                    (name, stamp, describe(p)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            print(f"applied {name}")
        print(f"{len(pending)} migration(s) applied to {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

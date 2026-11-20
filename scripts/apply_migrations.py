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
import sqlite3
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
    """What the ledger says has been applied — failing closed when it cannot be read.

    A ledger that cannot be read is not an empty ledger: treating it as empty would
    re-run every migration over a database that may already be part-way through them.
    A *missing* ledger table is different and legitimate — it means this database has
    never been migrated, and the caller creates it before reading.
    """
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone():
            return set()
        return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    except sqlite3.DatabaseError as error:
        fail(
            f"cannot read the migration ledger ({error}). Refusing to guess whether "
            "migrations were applied — repair or inspect the database first."
        )


def describe(path: Path) -> str:
    """The first comment line that is not the filename, for the record."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and len(stripped) > 4:
            return stripped[2:].strip()
    return path.name


# --- G1-R: upgrading a database that predates the attribution constraints --------------
#
# 020 declares the attribution table and the derived callback column as part of a *fresh*
# schema. A database created before that commit already has both objects in their older
# shape, and SQLite cannot add a STORED generated column or a foreign key to an existing
# table. Two facts make a small upgrade possible instead of a full schema rebuild:
#   * a VIRTUAL generated column *can* be added with ALTER TABLE, and the values, the
#     unique parent key and a child foreign key against it behave identically (proven in
#     this pack on SQLite 3.46.1); and
#   * only the attribution table needs rebuilding, because it needs new columns *and* new
#     foreign keys — and it carries no inbound references, so the rebuild is contained.
# The upgrade never invents authority: an existing attribution is carried over only when
# its referenced event really is VERIFIED and its evidence really carries that contact.

SUPPLIER_CONTACTS_MIGRATION = "020_supplier_contacts"
ATTRIBUTION_TABLE = "evidence_supplier_attributions"
CALLBACK_COLUMN = "callback_verified_contact"
STATE_COMPLETE = "COMPLETE"      # fresh schema, or already upgraded: no structural work
STATE_HISTORICAL = "HISTORICAL"  # pre-G1 shape: the upgrade path applies
STATE_UNKNOWN = "UNKNOWN"        # partial or inconsistent: refuse to touch it


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Every column, including generated ones — `table_info` hides those."""
    return [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def schema_state(conn: sqlite3.Connection) -> str:
    """Which of the three states this database is in — and nothing in between.

    COMPLETE means 020 needs no structural work. HISTORICAL covers every database whose
    G1 objects are still in their older shape *and* any database where an object is merely
    absent (020 is what creates it); the upgrade steps are idempotent, so both are handled
    by the same path. UNKNOWN is reserved for shapes that cannot be repaired without
    guessing: the evidence table is gone, or an attribution table already carries the new
    constraint while the column it points at is missing.
    """
    if not _table_exists(conn, "availability_evidence"):
        return STATE_UNKNOWN
    has_column = CALLBACK_COLUMN in _columns(conn, "availability_evidence")
    has_table = _table_exists(conn, ATTRIBUTION_TABLE)
    has_kind = has_table and "contact_event_kind" in _columns(conn, ATTRIBUTION_TABLE)
    if has_kind and not has_column:
        return STATE_UNKNOWN
    if has_column and has_kind:
        return STATE_COMPLETE
    return STATE_HISTORICAL


def _fresh_definition(table: str) -> str:
    """The CREATE TABLE text from the initial schema — the single source of truth.

    Both the rebuild and the generated-column expression are read from here, so an
    upgraded database cannot drift from a fresh one: there is one definition, not a copy
    that someone has to remember to update.
    """
    path = SL.SCHEMA_DIR / SL.DIALECT_FILES["sqlite"]
    needle = f"CREATE TABLE {table} ("
    for statement in SL.split_statements(path.read_text(encoding="utf-8")):
        index = statement.find(needle)
        if index != -1:
            return statement[index:].strip().rstrip(";") + ";"
    raise RuntimeError(f"the initial schema no longer defines {table}")


def _derived_callback_expression() -> str:
    """The generated-column expression, taken from the schema rather than retyped."""
    definition = _fresh_definition("availability_evidence")
    marker = "GENERATED ALWAYS AS ("
    start = definition.find(marker)
    end = definition.find(") STORED", start)
    if start == -1 or end == -1:
        raise RuntimeError(
            "the initial schema no longer declares a STORED generated callback column;"
            " the upgrade path must be revisited"
        )
    return definition[start + len(marker):end].strip()


def _check_attributions_can_be_carried(conn: sqlite3.Connection) -> None:
    """Refuse the upgrade if an existing attribution cannot honestly satisfy the new pins.

    Every existing row was written by the assertion path against a VERIFIED event and a
    ``callback_verified:<contact>`` capture. If any row says otherwise, the database and
    the code disagree, and copying it would either fabricate authority or abort halfway.
    """
    unruly = conn.execute(
        f"""
        SELECT a.evidence_id
          FROM {ATTRIBUTION_TABLE} a
          LEFT JOIN supplier_contact_events e
                 ON e.event_id = a.contact_event_id AND e.contact_id = a.contact_id
          LEFT JOIN availability_evidence v ON v.evidence_id = a.evidence_id
          LEFT JOIN bookings b ON b.agency_id = a.agency_id AND b.booking_id = a.booking_id
         WHERE e.kind IS NOT 'VERIFIED'
            OR v.source_ref IS NOT ('callback_verified:' || a.contact_id)
            OR b.booking_id IS NULL
            OR b.unit_id IS NOT a.unit_id
            OR b.check_in IS NOT a.block_start
            OR b.check_out IS NOT a.block_end
        """
    ).fetchall()
    if unruly:
        ids = ", ".join(row[0] for row in unruly[:5])
        fail(
            f"{len(unruly)} existing attribution(s) cannot satisfy the new constraints "
            f"without inventing authority ({ids}). The database and the code disagree; "
            "inspect these rows before migrating."
        )


def _upgrade_attributions(conn: sqlite3.Connection) -> None:
    """Rebuild the attribution table in the fresh shape, carrying its rows verbatim.

    SQLite's documented safe procedure: foreign keys are already off (the caller does
    that before opening the transaction), the copy names every column explicitly, and the
    old table is dropped only after the new one is populated.
    """
    old_columns = _columns(conn, ATTRIBUTION_TABLE)
    fresh_ddl = _fresh_definition(ATTRIBUTION_TABLE)
    staged = fresh_ddl.replace(
        f"CREATE TABLE {ATTRIBUTION_TABLE}", f"CREATE TABLE {ATTRIBUTION_TABLE}__new", 1
    )
    conn.execute(staged)
    new_columns = _columns(conn, ATTRIBUTION_TABLE + "__new")
    carried = [name for name in new_columns if name != "contact_event_kind"]
    missing = [name for name in carried if name not in old_columns]
    if missing:
        raise RuntimeError(
            f"cannot carry {ATTRIBUTION_TABLE}: existing table lacks {missing}"
        )
    conn.execute(
        f"""
        INSERT INTO {ATTRIBUTION_TABLE}__new ({", ".join(new_columns)})
        SELECT {", ".join(
            "(SELECT e.kind FROM supplier_contact_events e"
            " WHERE e.event_id = a.contact_event_id AND e.contact_id = a.contact_id)"
            if name == "contact_event_kind" else f"a.{name}"
            for name in new_columns
        )}
          FROM {ATTRIBUTION_TABLE} a
        """
    )
    conn.execute(f"DROP TABLE {ATTRIBUTION_TABLE}")
    conn.execute(f"ALTER TABLE {ATTRIBUTION_TABLE}__new RENAME TO {ATTRIBUTION_TABLE}")


def _upgrade_supplier_contacts(conn: sqlite3.Connection) -> None:
    """Bring a HISTORICAL database up to the shape 020 expects, before 020 runs."""
    if CALLBACK_COLUMN not in _columns(conn, "availability_evidence"):
        conn.execute(
            f"ALTER TABLE availability_evidence ADD COLUMN {CALLBACK_COLUMN} TEXT "
            f"GENERATED ALWAYS AS ({_derived_callback_expression()}) VIRTUAL"
        )
    if _table_exists(conn, ATTRIBUTION_TABLE):
        _check_attributions_can_be_carried(conn)
        _upgrade_attributions(conn)


def _violation_profile(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """Count foreign-key violations by (table, parent) — stable across a rebuild.

    `PRAGMA foreign_key_check` reports rowids and parent ids, and both change when a table
    is rebuilt, so a set difference would hide a new violation behind a reused rowid. What
    must not change is how many violating rows each table has, per parent table.
    """
    profile: dict[tuple[str, str], int] = {}
    for table, _rowid, parent, _fkid in conn.execute("PRAGMA foreign_key_check"):
        profile[(table, parent)] = profile.get((table, parent), 0) + 1
    return profile


def _verify_upgrade(conn: sqlite3.Connection, violations_before: dict) -> None:
    """Post-conditions: the shape 020 promises, and no *new* dangling references.

    A database can already contain foreign-key violations that were there before the
    migration (the runner has never policed rows written with enforcement off). Failing
    for those would make an honest upgrade impossible; the check is therefore that the
    upgrade introduced none, and counted per table rather than per rowid.
    """
    if CALLBACK_COLUMN not in _columns(conn, "availability_evidence"):
        raise RuntimeError("upgrade did not add the derived callback column")
    if "contact_event_kind" not in _columns(conn, ATTRIBUTION_TABLE):
        raise RuntimeError("upgrade did not rebuild the attribution table")
    violations_after = _violation_profile(conn)
    introduced = {
        key: count for key, count in violations_after.items()
        if count > violations_before.get(key, 0)
    }
    if introduced:
        raise RuntimeError(f"upgrade introduced foreign-key violation(s): {introduced}")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("upgrade left the database failing integrity_check")
    staging = [
        row[0] for row in conn.execute("SELECT name FROM sqlite_master")
        if row[0].startswith(f"{ATTRIBUTION_TABLE}__")
    ]
    if staging:
        raise RuntimeError(f"upgrade left staging tables behind: {staging}")


def _guard_supplier_state(conn: sqlite3.Connection, applied_versions: set[str]) -> None:
    """Refuse a database whose ledger and structure disagree, even with nothing pending.

    `schema_state` only runs for a migration that is about to be applied. If the ledger
    claims 020 is done while the objects are still in the old shape, no migration is
    pending and the runner would say "nothing to do" over a database that cannot enforce
    the attribution pins.
    """
    if not _table_exists(conn, "availability_evidence"):
        if applied_versions:
            fail(
                "the evidence table is missing from a database whose migration ledger is"
                " not empty — this is an unknown or partially-migrated G1 state."
                " Refusing to touch it; inspect the database first."
            )
        return  # a brand-new database: nothing to be inconsistent with yet
    state = schema_state(conn)
    if state == STATE_UNKNOWN:
        fail(
            "the database is in an unknown or partially-migrated G1 state (the attribution"
            " table carries the new constraint while the column it points at is missing)."
            " Refusing to guess what is authoritative — inspect it before migrating."
        )
    if SUPPLIER_CONTACTS_MIGRATION in applied_versions and state != STATE_COMPLETE:
        fail(
            "the ledger records 020 as applied but the G1 objects are not in the fresh"
            " shape — this is an unknown or partially-migrated G1 state. Refusing to"
            " treat it as done; inspect the database first."
        )


def _apply_migration(conn: sqlite3.Connection, name: str, path: Path, stamp: str) -> None:
    """Apply one migration, with the G1-R structural step when that migration needs it.

    Migration 020 is the only one that changes the shape of an existing table, so it is
    the only one that has to know whether the database predates it. Foreign keys are
    switched off *outside* the transaction (SQLite ignores the pragma inside one), the
    upgrade and the migration text commit together or not at all, and the integrity
    checks run before COMMIT so a bad copy still rolls back.
    """
    statements = SL.split_statements(path.read_text(encoding="utf-8"))
    structural = name == SUPPLIER_CONTACTS_MIGRATION
    state = schema_state(conn) if structural else None
    if structural and state == STATE_UNKNOWN:
        fail(
            "the database is in an unknown or partially-migrated G1 state "
            "(one half of the attribution shape is present). Refusing to guess which "
            "half is authoritative — inspect it before migrating."
        )
    if structural:
        conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The state above was read outside the write lock. Re-check the ledger once the
        # lock is held: a second runner that arrived first must not make this one redo
        # structural work on a database that is already upgraded.
        if structural and SUPPLIER_CONTACTS_MIGRATION in applied(conn):
            if state != STATE_COMPLETE:
                fail(
                    "the ledger records 020 as applied but the G1 objects are not in the"
                    " fresh shape. Refusing to treat that as done — inspect the database."
                )
            conn.execute("COMMIT")
            return
        violations_before = _violation_profile(conn) if structural else {}
        if structural and state == STATE_HISTORICAL:
            _upgrade_supplier_contacts(conn)
        for statement in statements:
            conn.execute(statement)
        if structural:
            _verify_upgrade(conn, violations_before)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
            (name, stamp, describe(path)),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if structural:
            conn.execute("PRAGMA foreign_keys = ON")


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
        done = applied(conn)
        _guard_supplier_state(conn, done)

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
            _apply_migration(conn, name, p, stamp)
            print(f"applied {name}")
        print(f"{len(pending)} migration(s) applied to {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

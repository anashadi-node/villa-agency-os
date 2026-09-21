#!/usr/bin/env python3
"""Generate / verify the PostgreSQL twin of the SQLite development schema.

Why generated instead of hand-maintained: two hand-written DDL files drift, and
drift is only discovered when production breaks. Here the SQLite file is the
single source of truth and every dialect delta is an explicit, reviewable rule
in this script. ``--check`` re-generates in memory and fails when the committed
PostgreSQL file differs, so drift cannot be merged silently.

Usage:
    python3 scripts/generate_pg_twin.py --write   # regenerate the twin
    python3 scripts/generate_pg_twin.py --check   # fail on drift (CI / make check)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from schema_loader import (  # noqa: E402  (path bootstrap must run first)
    split_statements,
    statement_kind,
    _strip_leading_comments,  # type: ignore[attr-defined]
)

SQLITE_FILE = SRC_DIR / "schema" / "001_initial.sql"
PG_FILE = SRC_DIR / "schema" / "001_initial.pg.sql"

HEADER = """\
-- Villa Agency OS — initial schema (PostgreSQL, production dialect).
-- GENERATED from src/schema/001_initial.sql by scripts/generate_pg_twin.py.
-- Do not edit by hand: edit the SQLite source, then run
--     python3 scripts/generate_pg_twin.py --write
--
-- Semantic twin of 001_initial.sql. The complete list of dialect deltas:
--   * INTEGER -> BIGINT. Both map to the "int" family in the parity model;
--     PostgreSQL production uses 64-bit integers for money and counters.
--   * Append-only guards: each SQLite RAISE(ABORT) trigger becomes one trigger
--     with the SAME name, table and event, calling the shared plpgsql function
--     villa_reject_mutation(). The function is created once, before the first
--     trigger.
--   * "PRAGMA foreign_keys = ON" is SQLite-only (PostgreSQL enforces foreign
--     keys natively).
-- Everything else — tables, column order, nullability, defaults, primary keys,
-- unique constraints, foreign keys, CHECK constraint names and expressions,
-- enum value sets, indexes and partial-index predicates — is byte-identical
-- text, and tests/test_schema.py fails when it is not.
"""

REJECT_FUNCTION = """\
CREATE FUNCTION villa_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$"""

_TRIGGER_RE = re.compile(
    r"CREATE\s+TRIGGER\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.I | re.S,
)


def _split_leading_comments(raw: str) -> tuple[str, str]:
    """Return ``(leading_comments, body)`` for one raw statement."""
    body = _strip_leading_comments(raw)
    return raw[: len(raw) - len(body)], body


def _pg_trigger(name: str, timing: str, event: str, table: str) -> str:
    # PostgreSQL requires an explicit timing keyword (BEFORE / AFTER / INSTEAD OF);
    # the SQLite source already carries it, so it is copied verbatim.
    return (
        f"CREATE TRIGGER {name}\n"
        f"    {timing.upper()} {event.upper()} ON {table}\n"
        f"    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation()"
    )


def generate(sqlite_sql: str) -> str:
    """Return the PostgreSQL twin text for ``sqlite_sql``."""
    out: list[str] = [HEADER]
    reject_emitted = False
    stats = {"tables": 0, "indexes": 0, "triggers": 0, "inserts": 0}

    for raw in split_statements(sqlite_sql):
        comments, body = _split_leading_comments(raw)
        if not body:
            continue
        kind = statement_kind(body)

        if kind == "pragma":
            # SQLite-only: foreign-key enforcement is native in PostgreSQL.
            continue

        if kind == "create_trigger":
            m = _TRIGGER_RE.match(body)
            if not m:
                raise SystemExit(f"unparsable CREATE TRIGGER: {body[:120]!r}")
            name, timing, event, table = (
                m.group(1).lower(),
                m.group(2),
                m.group(3),
                m.group(4).lower(),
            )
            if not reject_emitted:
                out.append(REJECT_FUNCTION)
                reject_emitted = True
            out.append(f"{comments}{_pg_trigger(name, timing, event, table)}")
            stats["triggers"] += 1
            continue

        pg_body = re.sub(r"\bINTEGER\b", "BIGINT", body)
        pg_body = pg_body.rstrip().rstrip(";")
        if kind == "create_table":
            stats["tables"] += 1
        elif kind == "create_index":
            stats["indexes"] += 1
        elif kind == "insert":
            stats["inserts"] += 1
        out.append(f"{comments}{pg_body}")

    text = ";\n\n".join(part.rstrip() for part in out if part.strip()) + ";\n"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="write the twin file")
    group.add_argument("--check", action="store_true", help="fail when the twin drifts")
    args = parser.parse_args(argv)

    sqlite_sql = SQLITE_FILE.read_text(encoding="utf-8")
    generated = generate(sqlite_sql)

    if args.write:
        PG_FILE.write_text(generated, encoding="utf-8")
        print(f"wrote {PG_FILE.relative_to(REPO_ROOT)} ({len(generated)} chars)")
        return 0

    current = PG_FILE.read_text(encoding="utf-8") if PG_FILE.exists() else ""
    if current == generated:
        print(f"OK: {PG_FILE.relative_to(REPO_ROOT)} matches the generated twin")
        return 0

    current_lines = current.splitlines()
    generated_lines = generated.splitlines()
    for index, (a, b) in enumerate(zip(current_lines, generated_lines), start=1):
        if a != b:
            print(
                f"DRIFT at line {index}:\n  committed: {a}\n  generated: {b}",
                file=sys.stderr,
            )
            break
    else:
        print(
            f"DRIFT: line counts differ (committed={len(current_lines)}, "
            f"generated={len(generated_lines)})",
            file=sys.stderr,
        )
    print(
        "FAIL: the PostgreSQL twin is stale — run "
        "python3 scripts/generate_pg_twin.py --write",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Capture the PostgreSQL guard definitions the runner is allowed to certify.

The inventory is derived from the approved SQL, not typed by hand: apply 001_initial.pg.sql
(the twin baseline) and every statement of 002_pg_range_exclusions.pg.sql in a scratch
database, then record every validated CHECK and EXCLUDE on ``holds`` and ``bookings`` plus the
``villa_iso_date`` body, verbatim.

Why derived: reviewing by hand missed ``ck_bookings_stay`` once and the status-enum CHECKs
after that. A hand-written list is a promise to remember; a query is a fact. A status outside
the enum is never compared against the exclusion predicate, so dropping or widening that CHECK
protects nothing — and a runner that never looks at it will call the database safe.

Nothing here normalises the text. ``pg_get_constraintdef`` output IS the approved artefact;
stripping casts would also strip them inside quoted literals, which is how a forged
``'REQ::textUESTED'`` reads as ``'REQUESTED'``.

    python3 scripts/capture_guard_definitions.py --dsn postgresql://... --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import secrets
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "src" / "schema"
APPROVED_FILE = SCHEMA_DIR / "002_pg_range_exclusions.pg.sql"
BASELINE_FILE = SCHEMA_DIR / "001_initial.pg.sql"
FIXTURE = SCHEMA_DIR / "002_pg_range_exclusions.approved.json"
FUNCTION = "villa_iso_date"
GUARD_TABLES = ("bookings", "holds")
KINDS = {"c": "check", "x": "exclusion"}
PREFIX = {"check": "CHECK (", "exclusion": "EXCLUDE USING"}
SCHEMA_VERSION = 2


def _verbatim(text: str) -> str:
    """Not a normaliser. PostgreSQL's deparse output IS the approved artefact.

    Stripping "::text" is the mistake this file exists to prevent: it happens inside quoted
    literals too, so 'REQ::textUESTED' would collapse to 'REQUESTED' and a guard that rejects
    every row would compare equal to one that rejects none. Cross-version deparse drift must
    surface as a refusal and a re-capture, which is honest; silent tolerance is not.
    """
    return text


def capture(dsn: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    sys.path.insert(0, str(REPO_ROOT / "src"))
    import schema_loader as SL  # noqa: PLC0415 - kept local: the script must run standalone

    source_text = APPROVED_FILE.read_text(encoding="utf-8")
    if FUNCTION not in source_text:
        raise SystemExit(f"{FUNCTION} is missing from {APPROVED_FILE.name}")
    statements = [
        body
        for body in (SL._strip_leading_comments(raw) for raw in SL.split_statements(source_text))
        if body
    ]

    scratch = f"villa_capture_{secrets.token_hex(4)}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch))
        )
    try:
        target = make_conninfo(dsn, dbname=scratch)
        SL.connect_pg(target, apply=True)  # twin baseline = 001_initial.pg.sql
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source_file": APPROVED_FILE.name,
            "file_sha256": hashlib.sha256(APPROVED_FILE.read_bytes()).hexdigest(),
            "baseline_file": BASELINE_FILE.name,
            "baseline_file_sha256": hashlib.sha256(BASELINE_FILE.read_bytes()).hexdigest(),
            "captured_by": "scripts/capture_guard_definitions.py",
            "capture_procedure": (
                "apply src/schema/001_initial.pg.sql twin, then every statement of"
                " 002_pg_range_exclusions.pg.sql; record every validated CHECK and EXCLUDE on"
                " holds/bookings plus the villa_iso_date body, verbatim, with created_by"
                " provenance from a before/after constraint snapshot"
            ),
            "server_version": "",
            "server_version_num": "",
            "objects": [],
            "function": {},
        }
        with psycopg.connect(target, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_setting('server_version'),"
                    " current_setting('server_version_num')"
                )
                version, version_num = cur.fetchone()
                payload["server_version"] = str(version)
                payload["server_version_num"] = str(version_num)
                cur.execute("SET search_path = public, pg_catalog")
                cur.execute(
                    "SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public'"
                )
                from_baseline = {row[0] for row in cur.fetchall()}
                for statement in statements:
                    cur.execute(sql.SQL(statement))
                cur.execute(
                    "SELECT t.relname, c.conname, c.contype, pg_get_constraintdef(c.oid)"
                    " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE n.nspname = 'public' AND t.relname = ANY(%s)"
                    " AND c.contype IN ('c', 'x') AND c.convalidated"
                    " AND (c.contype = 'c' OR EXISTS (SELECT 1 FROM pg_index i"
                    "      WHERE i.indexrelid = c.conindid AND i.indisvalid))"
                    " ORDER BY t.relname, c.conname",
                    (list(GUARD_TABLES),),
                )
                for table, name, contype, definition in cur.fetchall():
                    kind = KINDS[str(contype)]
                    text = _verbatim(str(definition))
                    if PREFIX[kind] not in text:
                        raise SystemExit(f"{table}.{name} does not look like a {kind}: {text[:60]}")
                    payload["objects"].append(
                        {
                            "kind": kind,
                            "table": table,
                            "name": name,
                            "definition": text,
                            "created_by": "001" if name in from_baseline else "002",
                        }
                    )
                if not payload["objects"]:
                    raise SystemExit(
                        "the approved SQL created no verified constraint on holds/bookings"
                    )
                cur.execute("SELECT pg_get_functiondef(%s::regproc)", (FUNCTION,))
                found = cur.fetchone()
                if found is None:
                    raise SystemExit(f"approved file did not create {FUNCTION}")
                payload["function"][FUNCTION] = _verbatim(str(found[0]))
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
            )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture canonical guard definitions")
    parser.add_argument("--dsn", required=True, help="disposable PostgreSQL DSN")
    parser.add_argument("--write", action="store_true", help="rewrite the committed fixture")
    args = parser.parse_args(argv)
    payload = capture(args.dsn)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.write:
        FIXTURE.write_text(rendered, encoding="utf-8")
        print(f"wrote {FIXTURE.relative_to(REPO_ROOT)}")
        return 0
    current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""
    if current != rendered:
        print("fixture differs from what the approved file produces", file=sys.stderr)
        return 1
    print(
        f"fixture matches the approved file — postgresql {payload['server_version']}:"
        f" {sum(1 for o in payload['objects'] if o['kind'] == 'exclusion')} EXCLUDE,"
        f" {sum(1 for o in payload['objects'] if o['kind'] == 'check')} CHECK,"
        f" {len(payload['function'])} function"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

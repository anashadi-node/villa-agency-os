#!/usr/bin/env python3
"""Capture the PostgreSQL guard definitions the runner is allowed to certify.

The inventory is derived from the approved SQL, not typed by hand: apply 001_initial.pg.sql
(the twin baseline) and then every statement of each PostgreSQL-only migration in order, in a
scratch database, and record verbatim:

* every validated CHECK and EXCLUDE on ``holds`` and ``bookings``;
* every non-internal trigger on those two tables (definition and enabled state);
* the body of every function the PostgreSQL-only files create.

Why derived: reviewing by hand missed ``ck_bookings_stay`` once and the status-enum CHECKs
after that. A hand-written list is a promise to remember; a query is a fact. A status outside
the enum is never compared against the exclusion predicate, so dropping or widening that CHECK
protects nothing — and a runner that never looks at it will call the database safe. The same
argument is why 021's triggers are captured: a guard the runner cannot name is a guard it
cannot notice has been dropped.

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
BASELINE_FILE = SCHEMA_DIR / "001_initial.pg.sql"
# Ordered: each file is applied on top of the previous one, and its hash is pinned.
PG_ONLY_FILES = (
    SCHEMA_DIR / "002_pg_range_exclusions.pg.sql",
    SCHEMA_DIR / "021_pg_cross_table_occupancy.pg.sql",
)
# The fixture keeps its historical name: the ledger and the runner already refer to it, and
# renaming a committed artefact buys nothing.
FIXTURE = SCHEMA_DIR / "002_pg_range_exclusions.approved.json"
GUARD_TABLES = ("bookings", "holds")
KINDS = {"c": "check", "x": "exclusion"}
PREFIX = {"check": "CHECK (", "exclusion": "EXCLUDE USING", "trigger": "CREATE "}
SCHEMA_VERSION = 3


def _verbatim(text: str) -> str:
    """Not a normaliser. PostgreSQL's deparse output IS the approved artefact.

    Stripping "::text" is the mistake this file exists to prevent: it happens inside quoted
    literals too, so 'REQ::textUESTED' would collapse to 'REQUESTED' and a guard that rejects
    every row would compare equal to one that rejects none. Cross-version deparse drift must
    surface as a refusal and a re-capture, which is honest; silent tolerance is not.
    """
    return text


def _statements(path: pathlib.Path, sl: Any) -> list[str]:
    return [
        body
        for body in (
            sl._strip_leading_comments(raw)
            for raw in sl.split_statements(path.read_text(encoding="utf-8"))
        )
        if body
    ]


def _constraints(cur: Any) -> dict[str, tuple[str, str, str]]:
    cur.execute(
        "SELECT t.relname, c.conname, c.contype, pg_get_constraintdef(c.oid)"
        " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE n.nspname = 'public' AND t.relname = ANY(%s)"
        " AND c.contype IN ('c', 'x') AND c.convalidated"
        " AND (c.contype = 'c' OR EXISTS (SELECT 1 FROM pg_index i"
        "      WHERE i.indexrelid = c.conindid AND i.indisvalid))",
        (list(GUARD_TABLES),),
    )
    return {row[1]: (row[0], str(row[2]), str(row[3])) for row in cur.fetchall()}


def _triggers(cur: Any) -> dict[str, tuple[str, str, str]]:
    cur.execute(
        "SELECT t.relname, g.tgname, pg_get_triggerdef(g.oid), g.tgenabled"
        " FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE n.nspname = 'public' AND t.relname = ANY(%s) AND NOT g.tgisinternal",
        (list(GUARD_TABLES),),
    )
    return {row[1]: (row[0], str(row[2]), str(row[3])) for row in cur.fetchall()}


def _functions(cur: Any, names: list[str]) -> dict[str, str]:
    """Every declaration, by name — not a name-shaped guess.

    The inventory used to ask the catalogue for ``proname LIKE 'villa!_%'``. A function the
    approved SQL declares under any other name then never entered the fixture, and a function
    the fixture omits is never verified: missing, replaced or drifted, it verified clean. The
    names come from the declarations themselves, so the two directions cannot disagree.
    """
    cur.execute(
        "SELECT p.proname, pg_get_functiondef(p.oid)"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.proname = ANY(%s)",
        (list(names),),
    )
    rows = cur.fetchall()
    names = [row[0] for row in rows]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        # A dict keyed by name would keep one body and silently drop the other overload.
        raise SystemExit(f"the catalogue holds overloaded function(s) {duplicated}; a fixture keyed by name cannot describe both")
    return {row[0]: str(row[1]) for row in rows}


def capture(dsn: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    sys.path.insert(0, str(REPO_ROOT / "src"))
    import pg_migrations as PGM  # noqa: PLC0415 - the declaration parser lives with the runner
    import schema_loader as SL  # noqa: PLC0415 - kept local: the script must run standalone

    for path in PG_ONLY_FILES:
        if not path.exists():
            raise SystemExit(f"{path.name} is missing")
    prepared = {path.name: _statements(path, SL) for path in PG_ONLY_FILES}
    if not prepared[PG_ONLY_FILES[0].name]:
        raise SystemExit(f"{PG_ONLY_FILES[0].name} contains no statements")
    # One source of truth for names: the declarations, parsed exactly as the runner parses them.
    declared = sorted(PGM.declared_functions())
    if not declared:
        raise SystemExit("the approved files declare no function; the fixture would certify none")

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
            "source_file": PG_ONLY_FILES[0].name,
            "file_sha256": hashlib.sha256(PG_ONLY_FILES[0].read_bytes()).hexdigest(),
            "sources": [
                {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in PG_ONLY_FILES
            ],
            "baseline_file": BASELINE_FILE.name,
            "baseline_file_sha256": hashlib.sha256(BASELINE_FILE.read_bytes()).hexdigest(),
            "captured_by": "scripts/capture_guard_definitions.py",
            "capture_procedure": (
                "apply src/schema/001_initial.pg.sql twin, then every statement of each"
                " PostgreSQL-only migration in order (002, 021); record every validated CHECK"
                " and EXCLUDE on holds/bookings, every non-internal trigger on those tables"
                " with its enabled state, and every villa_* function body, verbatim, with"
                " created_by provenance from a before/after catalogue snapshot"
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
                baseline_constraints = _constraints(cur)
                recorded: set[tuple[str, str]] = set()
                functions = _functions(cur, declared)
                for path in PG_ONLY_FILES:
                    credited = path.stem.split("_", 1)[0]
                    for statement in prepared[path.name]:
                        cur.execute(sql.SQL(statement))
                    after_constraints = _constraints(cur)
                    after_triggers = _triggers(cur)
                    after_functions = _functions(cur, declared)
                    for name, (table, contype, definition) in after_constraints.items():
                        if ("constraint", name) in recorded:
                            continue
                        kind = KINDS[str(contype)]
                        text = _verbatim(definition)
                        if PREFIX[kind] not in text:
                            raise SystemExit(
                                f"{table}.{name} does not look like a {kind}: {text[:60]}"
                            )
                        # Constraint names are unique per schema, so the baseline snapshot is
                        # enough to attribute provenance without trusting the SQL text.
                        payload["objects"].append(
                            {
                                "kind": kind,
                                "table": table,
                                "name": name,
                                "definition": text,
                                "created_by": (
                                    "001" if name in baseline_constraints else credited
                                ),
                            }
                        )
                        recorded.add(("constraint", name))
                    for name, (table, definition, enabled) in after_triggers.items():
                        if ("trigger", name) in recorded:
                            continue
                        if enabled != "O":
                            raise SystemExit(
                                f"{table}.{name} was created disabled (tgenabled={enabled})"
                            )
                        payload["objects"].append(
                            {
                                "kind": "trigger",
                                "table": table,
                                "name": name,
                                "definition": _verbatim(definition),
                                "created_by": credited,
                            }
                        )
                        recorded.add(("trigger", name))
                    for name, definition in after_functions.items():
                        if name not in functions:
                            payload["function"][name] = _verbatim(definition)
                    functions = after_functions
                if not payload["objects"]:
                    raise SystemExit(
                        "the approved SQL created no verified object on holds/bookings"
                    )
                # Fail closed rather than publish a fixture with a hole in it: a declaration the
                # catalogue does not hold is either a statement that never ran or a name the
                # capture cannot see — both are a fixture that certifies less than it claims.
                unbuilt = sorted(set(declared) - set(payload["function"]))
                if unbuilt:
                    raise SystemExit(
                        "the approved files declare function(s) the catalogue does not hold:"
                        f" {', '.join(unbuilt)}"
                    )
                payload["objects"].sort(
                    key=lambda item: (item["table"], item["kind"], item["name"])
                )
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
        print("fixture differs from what the approved files produce", file=sys.stderr)
        return 1
    counts = {
        kind: sum(1 for item in payload["objects"] if item["kind"] == kind)
        for kind in ("exclusion", "check", "trigger")
    }
    print(
        f"fixture matches the approved files — postgresql {payload['server_version']}:"
        f" {counts['exclusion']} EXCLUDE, {counts['check']} CHECK, {counts['trigger']} trigger,"
        f" {len(payload['function'])} function"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

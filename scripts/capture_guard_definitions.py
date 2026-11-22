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
import importlib.util
import json
import os
import pathlib
import re
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
#: The only fields that describe the server that ran the capture, not the schema it captured.
#: Everything else is compared verbatim; a patch level is not schema drift, a predicate is.
SERVER_KEYS = ("server_version", "server_version_num")
#: Where the capture may run: the loopback interface of this machine (or its unix socket). A DSN
#: is not trusted because of its own hostname alone — the disposable proof below still has to
#: pass — but a remote host is refused outright.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


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


def _guard_module() -> Any:
    """The disposable-server guard that already governs the destructive tests, loaded by path.

    Reusing it is the point: a second, friendlier copy of the same proof is how the capture would
    drift from the thing it is supposed to obey. ``tests/`` is not a package, so the module is
    loaded from its file rather than imported by name.
    """
    path = REPO_ROOT / "tests" / "test_pg_overlap_concurrency.py"
    spec = importlib.util.spec_from_file_location("_villa_pg_guard", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the disposable-server guard from {path}")
    module = importlib.util.module_from_spec(spec)
    # A module executed without being registered makes `dataclass` look up a missing
    # `sys.modules` entry; register it under the private name first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prove_disposable_target(dsn: str, env: dict[str, str] | None = None) -> None:
    """Refuse any target that is not this run's disposable service, before CREATE/DROP.

    The hostname check is not the proof — it only rules out a remote server early, so a
    copy-pasted production DSN cannot reach the CREATE DATABASE below even if the run token
    happened to match. The proof itself is the tests' guard: GitHub-hosted runner, per-run token,
    fresh server, no foreign database, non-superuser role.
    """
    from psycopg.conninfo import conninfo_to_dict

    parsed = conninfo_to_dict(dsn)
    host = parsed.get("host")
    if host not in LOOPBACK_HOSTS and host is not None:
        raise SystemExit(
            f"refusing to capture against host {host!r}: the capture creates and drops a"
            f" database and only runs against a loopback disposable service"
        )
    guard = _guard_module()
    try:
        guard.prove_disposable_server(dsn, env if env is not None else dict(os.environ))
    except guard.DisposableRefused as refusal:
        raise SystemExit(str(refusal)) from refusal


def _major(version: str) -> int:
    match = re.match(r"(\d+)", str(version))
    if not match:
        raise SystemExit(f"cannot read a PostgreSQL major version from {version!r}")
    return int(match.group(1))


def differences(approved: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Every material difference between the approved fixture and a fresh capture.

    The server version is metadata about *where* the capture ran, not about what it captured:
    the approved fixture was recorded on 16.14 and CI verifies on 16.16, and refusing that would
    forbid the only place the check can run. So the server fields are compared by major version
    and then excluded — a different major is a refusal, a different patch level is reported and
    tolerated. Nothing else is normalised: definitions, predicates, trigger text, casts, security
    and search-path attributes and source checksums all compare byte for byte.
    """
    problems: list[str] = []
    approved_major = _major(approved.get("server_version", ""))
    fresh_major = _major(fresh.get("server_version", ""))
    if approved_major != fresh_major:
        problems.append(
            f"server major version differs: fixture {approved.get('server_version')} was captured"
            f" on PostgreSQL {approved_major}, verification ran on {fresh.get('server_version')}"
            f" (major {fresh_major}); capture the fixture again on the same major"
        )
    approved_body = {k: v for k, v in approved.items() if k not in SERVER_KEYS}
    fresh_body = {k: v for k, v in fresh.items() if k not in SERVER_KEYS}
    problems.extend(_diff_paths(approved_body, fresh_body, ""))
    return problems


def _diff_paths(approved: Any, fresh: Any, prefix: str) -> list[str]:
    """Dotted paths of every difference, so a refusal names what drifted."""
    if type(approved) is not type(fresh):
        return [f"{prefix or '<root>'}: {type(approved).__name__} in the fixture,"
                f" {type(fresh).__name__} in the capture"]
    if isinstance(approved, dict):
        problems: list[str] = []
        for key in sorted(set(approved) | set(fresh)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in approved:
                problems.append(f"{path}: present in the capture, absent from the fixture")
            elif key not in fresh:
                problems.append(f"{path}: present in the fixture, absent from the capture")
            else:
                problems.extend(_diff_paths(approved[key], fresh[key], path))
        return problems
    if isinstance(approved, list):
        problems = []
        if len(approved) != len(fresh):
            problems.append(f"{prefix}: fixture lists {len(approved)} entries, capture {len(fresh)}")
        for index, (left, right) in enumerate(zip(approved, fresh)):
            # Name the object, not just its position: a reordered list of constraints would
            # otherwise blame the wrong row.
            label = left.get("name") if isinstance(left, dict) else None
            problems.extend(_diff_paths(left, right, f"{prefix}[{label or index}]"))
        return problems
    if approved != fresh:
        return [f"{prefix}: fixture {approved!r} != capture {fresh!r}"]
    return []


def capture(dsn: str, expect_major: int | None = None) -> dict[str, Any]:
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
    # Before any CREATE/DROP: the target must be this run's disposable service. The guard used
    # by the destructive tests is reused, so the capture cannot be the one path that skips it.
    prove_disposable_target(dsn)
    with psycopg.connect(dsn, autocommit=True) as admin:
        # A wrong major is refused before the scratch database exists: recapturing on PostgreSQL
        # 17 to discover that the fixture is 16 would be work done on a server it cannot describe.
        if expect_major is not None:
            version = admin.execute("SELECT current_setting('server_version')").fetchone()[0]
            if _major(version) != expect_major:
                raise SystemExit(
                    f"refusing to verify a PostgreSQL {expect_major} fixture on server {version}:"
                    f" the major version differs, so the captured definitions are not comparable"
                )
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


def _dsn_from_arguments(args: argparse.Namespace) -> str:
    """The DSN, from the environment when asked — never from argv in CI.

    ``--dsn`` stays for local maintenance, but a command line is world-readable in a runner's
    process table, so the workflow uses ``--dsn-env VILLA_PG_DSN``. The value is never printed.
    """
    if args.dsn and args.dsn_env:
        raise SystemExit("pass either --dsn or --dsn-env, not both")
    if args.dsn_env:
        value = os.environ.get(args.dsn_env)
        if not value:
            raise SystemExit(f"{args.dsn_env} is not set; refusing to capture without a target")
        return value
    if args.dsn:
        return args.dsn
    raise SystemExit("a target is required: --dsn <disposable DSN> or --dsn-env <VARIABLE>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture canonical guard definitions")
    parser.add_argument("--dsn", help="disposable PostgreSQL DSN (local maintenance)")
    parser.add_argument(
        "--dsn-env", metavar="VARIABLE", help="read the DSN from this environment variable"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixture matches the approved files (default)",
    )
    mode.add_argument(
        "--write", action="store_true", help="rewrite the committed fixture (local maintenance)"
    )
    mode.add_argument(
        "--prove-target",
        action="store_true",
        help="prove the target is this run's disposable server, then stop (no DDL)",
    )
    args = parser.parse_args(argv)
    dsn = _dsn_from_arguments(args)
    if args.prove_target:
        prove_disposable_target(dsn)
        print("the target is this run's disposable PostgreSQL server")
        return 0
    import psycopg  # noqa: PLC0415 - kept local: the script must run standalone
    if not FIXTURE.exists() and not args.write:
        print(f"{FIXTURE.relative_to(REPO_ROOT)} is missing", file=sys.stderr)
        return 1
    approved = None if args.write else json.loads(FIXTURE.read_text(encoding="utf-8"))
    try:
        payload = capture(dsn, None if approved is None else _major(approved["server_version"]))
    except psycopg.OperationalError as failure:
        # Never echo the driver's message: it quotes the connection string back.
        print(f"cannot reach the target: {type(failure).__name__}", file=sys.stderr)
        return 1
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.write:
        FIXTURE.write_text(rendered, encoding="utf-8")
        print(f"wrote {FIXTURE.relative_to(REPO_ROOT)}")
        return 0
    counts = {
        kind: sum(1 for item in payload["objects"] if item["kind"] == kind)
        for kind in ("exclusion", "check", "trigger")
    }
    problems = differences(approved, payload)
    if problems:
        print("fixture differs from what the approved files produce:", file=sys.stderr)
        for problem in problems[:20]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more difference(s)", file=sys.stderr)
        return 1
    print(
        f"fixture matches the approved files — approved server"
        f" {approved.get('server_version')} (major {_major(approved.get('server_version', ''))}),"
        f" verified on {payload['server_version']} (major {_major(payload['server_version'])}):"
        f" {counts['exclusion']} EXCLUDE, {counts['check']} CHECK, {counts['trigger']} trigger,"
        f" {len(payload['function'])} function"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

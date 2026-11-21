"""Apply approved migrations to a PostgreSQL database — bounded and fail-closed.

Why this exists: ``schema_loader.connect_pg(apply=True)`` applies only the generated
``001_initial.pg.sql`` twin. A PostgreSQL database therefore starts life at the *final*
shape of migration 001 and never learns which numbered SQLite migrations came after it:
the reference rows added by 003/004/005/011/015 are absent, a database built from an older
twin is missing whole tables, and nothing records what happened. SQLite has
``scripts/apply_migrations.py`` for that job; PostgreSQL had nothing.

The model here is deliberately small:

* The ledger (``pg_schema_migrations``) records the baseline twin's fingerprint, then one
  row per migration with the SHA-256 of the SQLite source it was derived from. No row is
  ever rewritten or deleted, and a checksum that no longer matches is a refusal, not a
  re-run.
* Each migration is classified from its own statements. Object DDL that the approved
  baseline already contains is *reconciled*, never replayed: recreating a table because a
  migration had one is how a runner destroys a production database. Reference rows are
  executed because every one of them carries its own ``WHERE NOT EXISTS`` guard.
* A schema that is neither the approved baseline nor a supported historical state is
  refused (``UNKNOWN_SCHEMA``/``CONFLICTED``). Partial object sets are the loudest case:
  some tables present, some absent, means somebody else edited the schema.
* One advisory lock per target database serialises runners, with a bounded wait.

``HISTORICAL_DATA_TRANSFORM`` migrations (a table rebuild in 011, the attribution rebuild
in 020) are *not* replayed here. The approved baseline already carries their final shape,
and a rebuild against live tenant data is an operator decision with a backup, not
something a CLI does automatically. If such a migration is still pending, the runner
refuses with that reason instead of guessing.

No DSN is ever logged; callers pass the target explicitly.
"""

from __future__ import annotations

import json

import dataclasses
import hashlib
import pathlib
import sys
from typing import Any, Callable, Iterable, Optional, Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import schema_loader as SL  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from generate_pg_twin import generate as generate_pg  # noqa: E402

SCHEMA_DIR = SL.SCHEMA_DIR
PG_BASELINE_FILE = "001_initial.pg.sql"
PG_ONLY_FILE = "002_pg_range_exclusions.pg.sql"
SQLITE_BASELINE_FILE = SL.DIALECT_FILES["sqlite"]
LEDGER_TABLE = "pg_schema_migrations"
LOCK_NAME = "villa-agency-os:migrate"
CALLBACK_COLUMN = "callback_verified_contact"
REQUIRED_RANGE_EXCLUSIONS = 2  # holds + bookings; see 002_pg_range_exclusions.pg.sql


def guard_objects() -> tuple[SchemaObject, ...]:
    """Every object the approved SQL says the guard is made of.

    Derived from the captured fixture, never typed here. A hand-written list is how two
    blocking defects happened: ck_bookings_stay was missing, and then the status-enum CHECKs
    were missing. A status outside the enum is never compared with the exclusion predicate, so
    the guard protects nothing while every listed object still matches. The inventory has to
    come from the same query that builds the approved artefact, or the next object is missed
    the same way.
    """
    payload = approved_guards()
    objects = [
        SchemaObject(str(item["kind"]), str(item["name"]), str(item["table"]))
        for item in payload["objects"]
    ]
    return tuple(objects) + tuple(
        SchemaObject("function", name) for name in payload["function"]
    )
BASELINE_ID = "__baseline__/" + PG_BASELINE_FILE

# Ledger states. A resumed run must be able to tell reconciled from applied: "the objects
# are already there" is evidence of a baseline, not evidence that a migration ever ran.
ALREADY_APPLIED = "ALREADY_APPLIED"
BASELINE = "BASELINE"
BASELINE_RECONCILED = "BASELINE_RECONCILED"
PENDING = "PENDING"
CONFLICTED = "CONFLICTED"
FAILED = "FAILED"

SCHEMA_OBJECT = "SCHEMA_OBJECT"
REFERENCE_DATA = "REFERENCE_DATA"
MIXED = "MIXED"
HISTORICAL_DATA_TRANSFORM = "HISTORICAL_DATA_TRANSFORM"
POSTGRESQL_ONLY = "POSTGRESQL_ONLY"

STATEMENTS = {
    "create_table": "table",
    "create_index": "index",
    "create_trigger": "trigger",
    "alter_table": "column",
    "drop_table": "transform",
    "update": "transform",
    "delete": "transform",
}

LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS public.{LEDGER_TABLE} (
    migration_id    TEXT PRIMARY KEY,
    source_sha256   TEXT NOT NULL,
    classification  TEXT NOT NULL,
    state           TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    baseline_sha256 TEXT,
    evidence        TEXT
)
"""


class MigrationRefused(Exception):
    """A state the runner will not touch. The code is part of the CLI contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class SchemaObject:
    kind: str  # table | index | trigger | column | constraint | exclusion
    name: str
    table: str = ""
    column: str = ""
    expected: str = ""  # exclusion only: the approved definition body

    def describe(self) -> str:
        return f"{self.kind}:{self.table + '.' if self.table else ''}{self.name}"


@dataclasses.dataclass(frozen=True)
class Migration:
    migration_id: str
    source: pathlib.Path
    sha256: str
    classification: str
    objects: tuple[SchemaObject, ...]
    ddl: tuple[str, ...]
    data: tuple[str, ...]
    fatal: Optional[str] = None


@dataclasses.dataclass
class Step:
    migration: Migration
    planned_state: str
    reason: str


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(match: Any, statement: str, kind: str) -> Any:
    if match is None:
        raise MigrationRefused(
            "UNPARSABLE_STATEMENT", f"cannot name the {kind} in: {statement[:120]!r}"
        )
    return match


def _object_from(statement: str, kind: str) -> SchemaObject:
    """Name the object a DDL statement creates, without guessing SQL into existence."""
    import re

    head = statement.lstrip()
    if kind == "create_table":
        m = _require(
            re.match(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", head, re.I),
            head,
            kind,
        )
        return SchemaObject("table", m.group(1).lower())
    if kind == "create_index":
        m = _require(
            re.match(
                r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)"
                r"\s+ON\s+([A-Za-z_][\w]*)",
                head,
                re.I,
            ),
            head,
            kind,
        )
        return SchemaObject("index", m.group(1).lower(), m.group(2).lower())
    if kind == "create_trigger":
        m = _require(
            re.match(
                r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s+"
                r"(?:BEFORE|AFTER|INSTEAD\s+OF)\s+"
                r"(?:INSERT|UPDATE|DELETE|OR\s+\w+)\s+ON\s+([A-Za-z_][\w]*)",
                head,
                re.I,
            ),
            head,
            kind,
        )
        return SchemaObject("trigger", m.group(1).lower(), m.group(2).lower())
    m = _require(
        re.match(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)\s+ADD\s+(?:COLUMN\s+)?"
            r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)",
            head,
            re.I,
        ),
        head,
        kind,
    )
    return SchemaObject("column", m.group(2).lower(), m.group(1).lower())


def _kind(statement: str) -> str:
    """Statement kind, with everything the loader cannot name treated as a transform."""
    try:
        return SL.statement_kind(statement)
    except Exception:
        import re

        head = statement.lstrip()
        if re.match(r"ALTER\s+TABLE\b.*\bADD\s+(COLUMN\s+)?\w", head, re.I | re.S):
            return "alter_table"
        if re.match(r"DROP\s+TABLE\b", head, re.I):
            return "drop_table"
        return "transform"


def classify(statements: Sequence[str]) -> tuple[str, Optional[str]]:
    """Return ``(classification, fatal_reason)`` for one migration's statements."""
    kinds = set()
    for statement in statements:
        kind = _kind(statement)
        if kind in ("drop_table", "update", "delete", "transform"):
            kinds.add(HISTORICAL_DATA_TRANSFORM)
            reason = (
                "rebuilds or rewrites existing rows; the approved baseline already carries "
                "its final shape, and replaying it needs an operator with a backup"
            )
            return HISTORICAL_DATA_TRANSFORM, reason
        if kind in ("create_table", "create_index", "create_trigger", "alter_table"):
            kinds.add(SCHEMA_OBJECT)
        elif kind == "insert":
            kinds.add(REFERENCE_DATA)
        else:
            kinds.add(SCHEMA_OBJECT)
    if kinds == {HISTORICAL_DATA_TRANSFORM}:
        return HISTORICAL_DATA_TRANSFORM, None
    if kinds == {REFERENCE_DATA}:
        return REFERENCE_DATA, None
    if SCHEMA_OBJECT in kinds and REFERENCE_DATA in kinds:
        return MIXED, None
    if SCHEMA_OBJECT in kinds:
        return SCHEMA_OBJECT, None
    return REFERENCE_DATA, None


def _pg_for(statement: str, kind: str) -> str:
    """Translate one statement to PostgreSQL.

    The twin generator speaks the initial schema's grammar; ``ALTER TABLE ... ADD COLUMN``
    is not part of it, so that one shape gets the same single dialect delta the twin applies
    (INTEGER -> BIGINT) and nothing else.
    """
    if kind == "alter_table":
        import re

        return re.sub(r"\bINTEGER\b", "BIGINT", statement).rstrip().rstrip(";")
    return generate_pg(statement)[len(_pg_header()):].strip()


def discover(schema_dir: Optional[pathlib.Path] = None) -> list[Migration]:
    """Every numbered SQLite migration, translated to PostgreSQL, in deterministic order."""
    directory = pathlib.Path(schema_dir or SCHEMA_DIR)
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        name = path.name
        if name == SQLITE_BASELINE_FILE or name.endswith(".pg.sql") or name == "README.md":
            continue
        statements = [
            body
            for body in (SL._strip_leading_comments(raw) for raw in SL.split_statements(path.read_text(encoding="utf-8")))
            if body
        ]
        # PostgreSQL has no `CREATE TRIGGER IF NOT EXISTS` (SQLite does). The trigger name is
        # still stated explicitly, so dropping the clause loses nothing: the twin generator
        # re-emits the name verbatim.
        import re as _re

        statements = [
            _re.sub(r"CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS", "CREATE TRIGGER", body, flags=_re.I)
            for body in statements
        ]
        classification, fatal = classify(statements)
        objects, ddl, data = [], [], []
        for statement in statements:
            kind = _kind(statement)
            if kind == "insert":
                data.append(_pg_for(statement, kind))
            elif kind in ("drop_table", "update", "delete", "transform"):
                continue
            else:
                objects.append(_object_from(statement, kind))
                ddl.append(_pg_for(statement, kind))
        if classification == HISTORICAL_DATA_TRANSFORM:
            # A rebuild leaves its scratch table behind only in the SQL text: `<name>_new`
            # never survives the rename, so it is not evidence of anything in a live schema.
            objects = [obj for obj in objects if not obj.name.endswith("_new")]
            # And its INSERTs feed that scratch table — the final rows already exist, so the
            # statement is not replayable outside the rebuild it belongs to.
            data = [sql for sql in data if "_new" not in sql.split("(")[0]]
        found.append(
            Migration(
                migration_id=path.stem,
                source=path,
                sha256=_sha256(path),
                classification=classification,
                objects=tuple(objects),
                ddl=tuple(ddl),
                data=tuple(data),
                fatal=fatal,
            )
        )
    return [pg_only_migration(directory)] + found


def _approved_exclusions(path: pathlib.Path) -> tuple[tuple[str, str, str], ...]:
    """Read each guard straight out of the approved file, WHERE clause included."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MigrationRefused("APPROVED_FILE_UNREADABLE", f"cannot read {path.name}: {error}") from error
    found: list[tuple[str, str, str]] = []
    for table, name in (("holds", "ex_holds_no_overlap"), ("bookings", "ex_bookings_no_overlap")):
        start = text.find(f"ADD CONSTRAINT {name}")
        if start < 0:
            raise MigrationRefused(
                "APPROVED_FILE_INCOMPLETE", f"{name} is missing from {path.name}"
            )
        end = text.find(";", start)
        if end < 0:
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE", f"{name} in {path.name} is not terminated"
            )
        found.append((name, table, text[start:end]))
    return tuple(found)


def pg_only_migration(schema_dir: Optional[pathlib.Path] = None) -> Migration:
    """002 is PostgreSQL-only hardening, not part of the twin.

    It holds the storage-level double-booking guard (two EXCLUDE constraints). The generator
    pair cannot carry it — SQLite has no range exclusion — so it needs its own tracked step.
    Without this, a runner could certify a database "green" whose only overlap protection is
    the kernel's in-transaction check, which a direct write bypasses.
    """
    directory = pathlib.Path(schema_dir or SCHEMA_DIR)
    path = directory / PG_ONLY_FILE
    statements = [
        body
        for body in (
            SL._strip_leading_comments(raw)
            for raw in SL.split_statements(path.read_text(encoding="utf-8"))
        )
        if body
    ]
    return Migration(
        migration_id=path.stem,
        source=path,
        sha256=_sha256(path),
        classification=POSTGRESQL_ONLY,
        objects=tuple(
            SchemaObject("exclusion", name, table, column="unit_id", expected=body)
            for name, table, body in _approved_exclusions(path)
        ),
        ddl=tuple(statements),
        data=(),
    )


def _pg_header() -> str:
    from generate_pg_twin import HEADER

    return HEADER


def baseline_path(schema_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(schema_dir or SCHEMA_DIR) / PG_BASELINE_FILE


def baseline_sha256(schema_dir: Optional[pathlib.Path] = None) -> str:
    return _sha256(baseline_path(schema_dir))


def connect(dsn: str, *, timeout_seconds: int = 10) -> Any:
    """Open one connection. The driver is a documented dev dependency, not a silent import."""
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MigrationRefused(
            "DRIVER_MISSING",
            "psycopg (v3) is required to migrate PostgreSQL: "
            "python -m pip install -r requirements-dev.txt",
        ) from exc
    return psycopg.connect(dsn, autocommit=False, connect_timeout=timeout_seconds)


def _execute(cur: Any, statement: str) -> None:
    """Run schema text verbatim.

    psycopg treats ``%`` as a placeholder introducer even with no parameters, and schema SQL
    legitimately contains it (LIKE patterns, modulo). ``sql.SQL`` sends the text untouched.
    """
    from psycopg import sql  # type: ignore

    cur.execute(sql.SQL(statement))


def ledger_exists(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{LEDGER_TABLE}",))
        return bool(cur.fetchone()[0])


LEDGER_COLUMNS = (
    "migration_id",
    "source_sha256",
    "classification",
    "state",
    "applied_at",
    "baseline_sha256",
    "evidence",
)


def ledger_incompatible(conn: Any) -> tuple[str, ...]:
    """Columns the runner needs but the ledger on disk does not have."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s",
            (LEDGER_TABLE,),
        )
        found = {row[0] for row in cur.fetchall()}
    return tuple(name for name in LEDGER_COLUMNS if name not in found)


def ledger_rows(conn: Any) -> dict[str, dict[str, str]]:
    if not ledger_exists(conn):
        return {}
    missing = ledger_incompatible(conn)
    if missing:
        conn.rollback()
        raise MigrationRefused(
            "LEDGER_INCOMPATIBLE",
            f"{LEDGER_TABLE} exists without {', '.join(missing)}: this is not a ledger this "
            f"runner wrote. Inspect it before migrating; do not drop it to make the runner work.",
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT migration_id, source_sha256, classification, state, applied_at,"
            f" baseline_sha256, evidence FROM public.{LEDGER_TABLE}"
        )
        return {
            row[0]: {
                "source_sha256": row[1],
                "classification": row[2],
                "state": row[3],
                "applied_at": row[4],
                "baseline_sha256": row[5] or "",
                "evidence": row[6] or "",
            }
            for row in cur.fetchall()
        }


def _pin_search_path(conn: Any) -> None:
    """Verify against a known namespace, not the caller's.

    pg_get_constraintdef qualifies names according to the *current* search_path, so the very
    same approved constraint reads as "public.villa_iso_date(...)" when public is not visible.
    Comparing that to the approved text would refuse a correct database; worse, a permissive
    comparison to absorb it would accept a wrong one.
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path = public, pg_catalog")


def object_present(conn: Any, obj: SchemaObject) -> bool:
    _pin_search_path(conn)
    with conn.cursor() as cur:
        if obj.kind == "table":
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{obj.name}",))
        elif obj.kind == "index":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public'"
                " AND indexname = %s)",
                (obj.name,),
            )
        elif obj.kind == "trigger":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE g.tgname = %s AND NOT g.tgisinternal AND n.nspname = 'public'"
                " AND (%s::text IS NULL OR t.relname = %s))",
                (obj.name, obj.table, obj.table),
            )
        elif obj.kind == "exclusion":
            # A name is not a guarantee. The guard only counts when the catalogue entry is a
            # validated exclusion constraint over the same unit/range expression AND the same
            # status predicate as the approved file. A same-named CHECK (probe A) and an
            # EXCLUDE whose WHERE clause drifted (probe C) both answer "present" by name and
            # protect the wrong rows.
            cur.execute(
                "SELECT COALESCE((SELECT pg_get_constraintdef(c.oid)"
                " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE c.conname = %s AND t.relname = %s AND n.nspname = 'public'"
                " AND c.contype = 'x' AND c.convalidated"
                " AND EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid = c.conindid"
                "             AND i.indisvalid)), '')",
                (obj.name, obj.table),
            )
            definition = str(cur.fetchone()[0])
            cur.execute(
                "SELECT %s", (bool(definition) and _definition_matches(obj, definition, conn),)
            )
        elif obj.kind in ("check", "function"):
            if obj.kind == "check":
                cur.execute(
                    "SELECT COALESCE((SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
                    " JOIN pg_class t ON t.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE c.conname = %s AND t.relname = %s AND n.nspname = 'public'"
                    " AND c.contype = 'c' AND c.convalidated), '')",
                    (obj.name, obj.table),
                )
            else:
                # A single name can answer with several signatures. Picking one at random is how
                # a runner certifies a body it never looked at.
                cur.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                    " ON n.oid = p.pronamespace WHERE p.proname = %s AND n.nspname = 'public'",
                    (obj.name,),
                )
                overloads = int(cur.fetchone()[0])
                if overloads > 1:
                    raise MigrationRefused(
                        "FUNCTION_AMBIGUOUS",
                        f"{overloads} functions named {obj.name}() exist in schema public; the"
                        f" guard's range expression must name exactly one function",
                    )
                cur.execute(
                    "SELECT COALESCE((SELECT pg_get_functiondef(p.oid) FROM pg_proc p"
                    " JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE p.proname = %s AND n.nspname = 'public'), '')",
                    (obj.name,),
                )
            definition = str(cur.fetchone()[0])
            cur.execute(
                "SELECT %s", (bool(definition) and _definition_matches(obj, definition, conn),)
            )
        elif obj.kind == "constraint":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE c.conname = %s AND n.nspname = 'public'"
                " AND (%s::text IS NULL OR t.relname = %s))",
                (obj.name, obj.table, obj.table),
            )
        elif obj.kind == "column":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = %s AND column_name = %s)",
                (obj.table, obj.name),
            )
        else:  # pragma: no cover - SchemaObject.kind is closed
            raise MigrationRefused("UNKNOWN_OBJECT_KIND", f"cannot inspect {obj.describe()}")
        return bool(cur.fetchone()[0])


APPROVED_FIXTURE = SCHEMA_DIR / "002_pg_range_exclusions.approved.json"


def _norm_definition(text: str) -> str:
    """Deprecated: kept only so an import of the old name fails loudly instead of silently.

    Normalising the deparse output is what allowed 'REQ::textUESTED' to read as 'REQUESTED'.
    Nothing may call this.
    """
    raise AssertionError("_norm_definition() must not be used: compare deparse output verbatim")


def approved_guards(conn: Any = None) -> dict[str, dict[str, str]]:
    """The canonical definitions, tied to the approved file by hash and server version."""
    try:
        payload = json.loads(APPROVED_FIXTURE.read_text(encoding="utf-8"))
    except OSError as error:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE", f"cannot read {APPROVED_FIXTURE.name}: {error}"
        ) from error
    except ValueError as error:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE", f"{APPROVED_FIXTURE.name} is not valid JSON: {error}"
        ) from error
    current = _sha256(SCHEMA_DIR / PG_ONLY_FILE)
    if payload.get("file_sha256") != current:
        raise MigrationRefused(
            "APPROVED_FILE_STALE",
            f"{APPROVED_FIXTURE.name} was captured from a different {PG_ONLY_FILE}: re-run"
            f" scripts/capture_guard_definitions.py --write and review the diff",
        )
    baseline_file = str(payload.get("baseline_file") or "")
    if not baseline_file:
        raise MigrationRefused(
            "APPROVED_FILE_UNPROVENANCED",
            f"{APPROVED_FIXTURE.name} does not record which baseline it was captured on;"
            f" half of the guard (ck_holds_stay, ck_bookings_stay) lives there",
        )
    try:
        baseline_current = _sha256(SCHEMA_DIR / baseline_file)
    except OSError as error:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE", f"cannot read baseline {baseline_file}: {error}"
        ) from error
    if payload.get("baseline_file_sha256") != baseline_current:
        raise MigrationRefused(
            "APPROVED_FILE_STALE",
            f"{APPROVED_FIXTURE.name} was captured from a different {baseline_file}: re-capture"
            f" and review the diff (the NOT NULL columns and two CHECKs come from there)",
        )
    captured_version = str(payload.get("server_version_num") or "")
    if not captured_version:
        raise MigrationRefused(
            "APPROVED_FILE_UNPROVENANCED",
            f"{APPROVED_FIXTURE.name} does not record the server version it was captured on",
        )
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')")
            live_version = str(cur.fetchone()[0])
        if live_version[:2] != captured_version[:2]:
            raise MigrationRefused(
                "APPROVED_DEFINITION_VERSION_MISMATCH",
                f"approved definitions were captured on PostgreSQL {payload.get('server_version')}"
                f" and this server reports version_num {live_version}: deparse output differs"
                f" between major versions, so re-capture and review rather than guess",
            )
    # Shape before value: a JSON file that is merely *valid* must not turn into an
    # AttributeError deep inside verification.
    if payload.get("schema_version") != 2:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE",
            f"{APPROVED_FIXTURE.name}: schema_version must be 2 (the inventory is an object"
            f" list, not three hand-kept dictionaries)",
        )
    objects = payload.get("objects")
    if not isinstance(objects, list) or not objects:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE",
            f"{APPROVED_FIXTURE.name}: objects must be a non-empty list of"
            f" table/name/definition entries",
        )
    seen: set[tuple[str, str, str]] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE", f"{APPROVED_FIXTURE.name}: objects entries must be objects"
            )
        kind = str(item.get("kind") or "")
        table = str(item.get("table") or "")
        name = str(item.get("name") or "")
        definition = item.get("definition")
        if kind not in ("check", "exclusion") or not table or not name:
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE",
                f"{APPROVED_FIXTURE.name}: {item!r} is not a check/exclusion with a table and name",
            )
        prefix = {"check": "CHECK (", "exclusion": "EXCLUDE USING"}[kind]
        if not isinstance(definition, str) or prefix not in definition:
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE",
                f"{APPROVED_FIXTURE.name}: {table}.{name} is not a {prefix!r} definition; the"
                f" fixture is the reviewed artefact, so it is not patched at runtime",
            )
        key = (kind, table, name)
        if key in seen:
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE", f"{APPROVED_FIXTURE.name}: {table}.{name} is listed twice"
            )
        seen.add(key)
    functions = payload.get("function")
    if not isinstance(functions, dict) or not functions:
        raise MigrationRefused(
            "APPROVED_FILE_UNREADABLE", f"{APPROVED_FIXTURE.name}: function must be a non-empty object"
        )
    for name, definition in functions.items():
        if not isinstance(definition, str) or "CREATE OR REPLACE FUNCTION" not in definition:
            raise MigrationRefused(
                "APPROVED_FILE_UNREADABLE",
                f"{APPROVED_FIXTURE.name}: function[{name}] is not a CREATE OR REPLACE FUNCTION",
            )
    return {"objects": objects, "function": dict(functions)}


def _approved_definition(obj: SchemaObject, conn: Any = None) -> str:
    payload = approved_guards(conn)
    if obj.kind == "function":
        expected = payload["function"].get(obj.name, "")
    else:
        expected = ""
        for item in payload["objects"]:
            if item["kind"] == obj.kind and item["name"] == obj.name and item["table"] == (obj.table or ""):
                expected = str(item["definition"])
                break
    if not expected:
        raise MigrationRefused(
            "APPROVED_FILE_INCOMPLETE", f"{obj.describe()} has no approved definition"
        )
    return expected


def _definition_matches(obj: SchemaObject, definition: str, conn: Any = None) -> bool:
    """Verbatim equality with the approved deparse output — no normaliser, ever.

    Stripping casts or whitespace would also strip them inside quoted literals, which is how a
    forged guard ('REQ::textUESTED') reads as approved while matching no row at all.
    """
    return definition.strip() == _approved_definition(obj, conn).strip()


def name_shadowed(conn: Any, obj: SchemaObject) -> bool:
    """True when something already answers to the object's name but is not the object."""
    with conn.cursor() as cur:
        if obj.kind == "exclusion":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE c.conname = %s AND t.relname = %s AND n.nspname = 'public')",
                (obj.name, obj.table),
            )
        elif obj.kind == "check":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE c.conname = %s AND t.relname = %s AND n.nspname = 'public')",
                (obj.name, obj.table),
            )
        elif obj.kind == "function":
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace WHERE p.proname = %s AND n.nspname = 'public')",
                (obj.name,),
            )
        elif obj.kind in ("table", "index", "trigger", "column", "constraint"):
            return object_present(conn, obj)
        else:  # pragma: no cover - SchemaObject.kind is closed
            raise MigrationRefused("UNKNOWN_OBJECT_KIND", f"cannot inspect {obj.describe()}")
        return bool(cur.fetchone()[0])


def present(conn: Any, objects: Iterable[SchemaObject]) -> tuple[int, int]:
    objects = tuple(objects)
    return sum(1 for obj in objects if object_present(conn, obj)), len(objects)


def survey(conn: Any) -> dict[str, int]:
    """Catalogue facts the plan is derived from — counts only, never row data."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        tables = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'villa\\_%'")
        functions = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = 'public' AND t.relname IN ('holds', 'bookings')"
            " AND c.contype = 'x' AND c.convalidated"
        )
        exclusions = int(cur.fetchone()[0])
    return {"tables": tables, "villa_functions": functions, "range_exclusions": exclusions}


def _unexpected_objects(conn: Any) -> list[str]:
    """Anything on holds/bookings that the approved SQL did not create.

    Presence-only verification says the guard objects are right; it cannot say nothing else is
    there. A BEFORE INSERT trigger on holds can rewrite unit_id or swallow the row, and every
    constraint would still read as approved. The approved files put no trigger on these two
    tables, so on them any trigger at all is drift — fail closed rather than reason about what
    a trigger might be doing.
    """
    _pin_search_path(conn)
    approved = {(obj.kind, obj.table, obj.name) for obj in guard_objects()}
    found: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.relname, g.tgname FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = 'public' AND t.relname IN ('holds', 'bookings') AND NOT g.tgisinternal"
            " ORDER BY t.relname, g.tgname"
        )
        for table, name in cur.fetchall():
            if ("trigger", table, name) not in approved:
                found.append(f"unexpected:trigger:{table}.{name}")
        cur.execute(
            "SELECT t.relname, c.conname, c.contype FROM pg_constraint c"
            " JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = 'public' AND t.relname IN ('holds', 'bookings')"
            " AND c.contype IN ('c', 'x') AND c.convalidated"
            " ORDER BY t.relname, c.conname"
        )
        for table, name, contype in cur.fetchall():
            kind = {"c": "check", "x": "exclusion"}[str(contype)]
            if (kind, table, name) not in approved:
                found.append(f"unexpected:{kind}:{table}.{name}")
    return found


def verify_guards(conn: Any) -> list[str]:
    """Guard objects that are absent or are not exactly the approved definition.

    Counting them is not verification: two EXCLUDE constraints can exist while a CHECK that
    makes a zero-night hold impossible has been replaced by CHECK (true).
    """
    _pin_search_path(conn)
    approved_guards(conn)  # refuses on stale/unprovenanced/foreign-version definitions
    return (
        [obj.describe() for obj in guard_objects() if not object_present(conn, obj)]
        + _missing_preconditions(conn)
        + _unexpected_objects(conn)
    )


# An EXCLUDE constraint treats NULLs as non-conflicting, so the two guards only protect rows
# while these columns are NOT NULL. The approved schema says they are; the certified database
# need not, and dropping one NOT NULL is invisible to a constraint-text comparison.
GUARD_COLUMNS = (
    ("holds", "unit_id"),
    ("holds", "check_in"),
    ("holds", "check_out"),
    ("holds", "status"),
    ("bookings", "unit_id"),
    ("bookings", "check_in"),
    ("bookings", "check_out"),
    ("bookings", "status"),
)


def _missing_preconditions(conn: Any) -> list[str]:
    _pin_search_path(conn)
    missing: list[str] = []
    for table, column in GUARD_COLUMNS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attnotnull FROM pg_attribute WHERE attrelid = to_regclass(%s)"
                " AND attname = %s AND attnum > 0 AND NOT attisdropped",
                (f"public.{table}", column),
            )
            row = cur.fetchone()
        value = None if row is None else row[0]
        if value is not True:
            missing.append(f"not-null:{table}.{column}")
    return missing


READY = "READY"
UNKNOWN = "UNKNOWN"


def database_state(conn: Any, migrations: Sequence[Migration]) -> dict[str, Any]:
    """Read-only answer to "may this database be migrated?" — used by --inspect.

    Never writes: a preview that creates a ledger has already changed the thing it describes.
    """
    _pin_search_path(conn)
    baseline = check_baseline(conn)
    steps = plan(conn, migrations)  # ledger is optional here: no row means "not recorded"
    guard_setup_pending = any(
        step.planned_state == PENDING and step.migration.classification == POSTGRESQL_ONLY
        for step in steps
    )
    missing = verify_guards(conn)
    conflicts = [step for step in steps if step.planned_state == CONFLICTED]
    if baseline == "EMPTY":
        status = UNKNOWN  # no baseline to recognise: the safe answer is "I cannot say"
    elif conflicts:
        status = CONFLICTED
    elif missing and not guard_setup_pending:
        status = CONFLICTED
    elif any(step.planned_state == PENDING for step in steps) or guard_setup_pending:
        status = PENDING
    else:
        status = READY
    return {
        "status": status,
        "baseline": baseline,
        "steps": [
            {
                "migration": step.migration.migration_id,
                "classification": step.migration.classification,
                "planned_state": step.planned_state,
                "reason": step.reason,
            }
            for step in steps
        ],
        "missing_guards": missing,
        "conflicts": [step.migration.migration_id for step in conflicts],
        "survey": survey(conn),
    }


def check_baseline(conn: Any, *, expected_sha: Optional[str] = None) -> str:
    """Refuse a database that is neither the approved baseline nor a known upgrade path.

    ``tables == 0`` is a fresh database; the caller applies the twin first. A non-empty
    schema whose ``holds``/``bookings`` tables are absent is not this product's schema.
    """
    facts = survey(conn)
    if expected_sha is not None and expected_sha != baseline_sha256():
        raise MigrationRefused(
            "BASELINE_PIN_MISMATCH",
            f"expected baseline {expected_sha[:12]} but the approved twin is "
            f"{baseline_sha256()[:12]}",
        )
    if facts["tables"] == 0:
        return "EMPTY"
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.holds') IS NOT NULL AND"
                    " to_regclass('public.bookings') IS NOT NULL")
        core = bool(cur.fetchone()[0])
    if not core:
        raise MigrationRefused(
            "UNKNOWN_SCHEMA",
            f"neither the approved schema nor a supported historical state "
            f"({facts['tables']} tables, no holds/bookings). Refusing to guess.",
        )
    return "MIGRATABLE"


def plan(conn: Any, migrations: Sequence[Migration]) -> list[Step]:
    """What a resumed run must do, migration by migration."""
    rows = ledger_rows(conn)
    current_baseline = baseline_sha256()
    steps: list[Step] = []
    for migration in migrations:
        row = rows.get(migration.migration_id)
        if (
            migration.classification == HISTORICAL_DATA_TRANSFORM
            and row is not None
            and row["source_sha256"] == migration.sha256
            and row["state"] in (ALREADY_APPLIED, BASELINE_RECONCILED)
        ):
            have, total = present(conn, migration.objects)
            if have == total:
                steps.append(Step(migration, ALREADY_APPLIED, f"ledger: {row['state']}"))
                continue
        if migration.classification == HISTORICAL_DATA_TRANSFORM:
            # Never replayable by this runner: the baseline carries the final shape, and a
            # rebuild needs an operator and a backup. A FAILED ledger row does not turn it
            # into a retry candidate — it is the same refusal with history attached.
            have, total = present(conn, migration.objects)
            if have == total:
                note = (
                    f"a previous attempt failed ({row['evidence'][:80]}) but the final shape "
                    f"is present; rebuild not replayed"
                    if row is not None and row["state"] == FAILED
                    else "final shape already in the approved baseline; rebuild not replayed"
                )
                steps.append(Step(migration, BASELINE_RECONCILED, note))
            else:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"needs a data transform ({have}/{total} objects present): "
                        f"{migration.fatal or 'operator intervention required'}",
                    )
                )
            continue
        if row is not None and row["state"] == FAILED:
            have, total = present(conn, migration.objects)
            if total and have == total:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"the ledger says the last attempt failed ({row['evidence'][:80]}) but "
                        f"all {total} object(s) exist: someone repaired this by hand. Verify the "
                        f"schema, then correct the ledger with evidence — do not replay.",
                    )
                )
                continue
            steps.append(
                Step(
                    migration,
                    PENDING,
                    f"previous attempt rolled back: {row['evidence'][:120]}",
                )
            )
            continue
        if row is not None:
            if row["source_sha256"] != migration.sha256:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"ledger fingerprint {row['source_sha256'][:12]} != source "
                        f"{migration.sha256[:12]}: the migration changed after it ran",
                    )
                )
                continue
            have, total = present(conn, migration.objects)
            if have != total:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"ledger says {row['state']} but only {have}/{total} object(s) exist: "
                        f"the ledger and the schema disagree",
                    )
                )
                continue
            steps.append(Step(migration, ALREADY_APPLIED, f"ledger: {row['state']}"))
            continue
        have, total = present(conn, migration.objects)
        if migration.classification == HISTORICAL_DATA_TRANSFORM:
            if have == total:
                steps.append(
                    Step(
                        migration,
                        BASELINE_RECONCILED,
                        "final shape already in the approved baseline; rebuild not replayed",
                    )
                )
            else:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"needs a data transform ({have}/{total} objects present): "
                        f"{migration.fatal or 'operator intervention required'}",
                    )
                )
            continue
        if total == 0:
            steps.append(Step(migration, PENDING, "reference data only"))
            continue
        if have == total:
            steps.append(
                Step(
                    migration,
                    BASELINE_RECONCILED,
                    f"all {total} object(s) already present in the approved baseline",
                )
            )
        elif have == 0:
            shadowed = [
                obj.describe()
                for obj in migration.objects
                if obj.kind == "exclusion" and name_shadowed(conn, obj)
            ]
            if shadowed:
                steps.append(
                    Step(
                        migration,
                        CONFLICTED,
                        f"{', '.join(shadowed)} exists by name but is not the approved "
                        f"constraint definition: an operator must inspect and drop it",
                    )
                )
                continue
            steps.append(Step(migration, PENDING, f"0/{total} object(s) present"))
        else:
            steps.append(
                Step(
                    migration,
                    CONFLICTED,
                    f"{have}/{total} object(s) present — partial schema, not a known state",
                )
            )
    baseline_row = rows.get(BASELINE_ID)
    if baseline_row is not None and baseline_row["source_sha256"] != current_baseline:
        steps.insert(
            0,
            Step(
                Migration(
                    migration_id=BASELINE_ID,
                    source=SCHEMA_DIR / PG_BASELINE_FILE,
                    sha256=current_baseline,
                    classification=POSTGRESQL_ONLY,
                    objects=(SchemaObject("table", "bookings"),),
                    ddl=(),
                    data=(),
                ),
                CONFLICTED,
                f"database was baselined from twin {baseline_row['source_sha256'][:12]} but the "
                f"approved twin is {current_baseline[:12]}: re-baseline is an operator decision",
            ),
        )
    return steps


def _now(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')")
        return str(cur.fetchone()[0])


def _ledger_write(conn: Any, migration: Migration, state: str, evidence: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO public.{LEDGER_TABLE} (migration_id, source_sha256, classification, state,"
            " applied_at, baseline_sha256, evidence) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (migration_id) DO UPDATE SET state = EXCLUDED.state,"
            " applied_at = EXCLUDED.applied_at, evidence = EXCLUDED.evidence,"
            " source_sha256 = EXCLUDED.source_sha256,"
            " classification = EXCLUDED.classification,"
            " baseline_sha256 = EXCLUDED.baseline_sha256",
            (
                migration.migration_id,
                migration.sha256,
                migration.classification,
                state,
                _now(conn),
                baseline_sha256(),
                evidence,
            ),
        )


def _generated_callback_expression() -> str:
    """The stored generated-column expression, read from the approved twin.

    Retiping it here would be a second source of truth for an attribution rule; the twin
    already states it, and ``git show`` of the old schema proves the column was absent.
    """
    text = baseline_path().read_text(encoding="utf-8")
    marker = "GENERATED ALWAYS AS ("
    start = text.find(marker, text.find("availability_evidence"))
    end = text.find(") STORED", start)
    if start == -1 or end == -1:
        raise MigrationRefused(
            "COMPAT_EXPRESSION_MISSING",
            "the approved twin no longer declares the stored generated callback column; "
            "the 020 compatibility step must be revisited before migrating",
        )
    return text[start + len(marker):end].strip()


def _compat_020(conn: Any) -> list[str]:
    """Bring a pre-G1 PostgreSQL database up to the shape 020 expects.

    Two objects differ between the historical schema and the approved baseline: the derived
    callback column (absent before G1) and the attribution table's new event-kind column.
    The column can be added truthfully — its value is *derived* from columns that already
    exist. The attribution table cannot: carrying rows over needs the same evidence proof
    the SQLite runner demands, so it is refused rather than guessed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'"
            " AND table_name='availability_evidence' AND column_name=%s)",
            (CALLBACK_COLUMN,),
        )
        has_column = bool(cur.fetchone()[0])
        cur.execute("SELECT to_regclass('public.evidence_supplier_attributions') IS NOT NULL")
        has_attributions = bool(cur.fetchone()[0])
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'"
            " AND table_name='evidence_supplier_attributions' AND column_name='contact_event_kind')"
        )
        modern_attributions = bool(cur.fetchone()[0])
    if has_attributions and not modern_attributions:
        raise MigrationRefused(
            "OPERATOR_REQUIRED",
            "evidence_supplier_attributions exists in its pre-G1 shape; carrying those rows "
            "over requires the attribution proof the SQLite runner performs. Refusing to "
            "fabricate authority — run the documented operator upgrade with a backup.",
        )
    steps: list[str] = []
    if not has_column:
        steps.append(
            f"ALTER TABLE availability_evidence ADD COLUMN {CALLBACK_COLUMN} TEXT "
            f"GENERATED ALWAYS AS ({_generated_callback_expression()}) STORED"
        )
    return steps


COMPAT_STEPS: dict[str, Callable[[Any], list[str]]] = {
    "020_supplier_contacts": _compat_020,
}


def _function_body(statement: str) -> Optional[str]:
    """The body of a ``CREATE FUNCTION`` statement, between its own dollar quotes."""
    import re

    markers = list(re.finditer(r"\$[A-Za-z_]*\$", statement))
    if len(markers) < 2:
        return None
    return statement[markers[0].end() : markers[1].start()]


def _skip_covered(conn: Any, statement: str) -> bool:
    """True when a DDL statement is already satisfied — shared helpers are the common case.

    The twin generator emits ``CREATE FUNCTION villa_reject_mutation()`` once per generated
    text. An upgrade replays a single migration, so the function usually already exists from
    the baseline; recreating it is an error, not progress.
    """
    import re

    added = re.match(
        r"ALTER\s+TABLE\s+(?:public\.)?([A-Za-z_][\w]*)\s+ADD\s+CONSTRAINT\s+([A-Za-z_][\w]*)",
        statement,
        re.I,
    )
    if added:
        kind = "exclusion" if re.search(r"\bEXCLUDE\b", statement, re.I) else "check"
        obj = SchemaObject(kind, added.group(2), added.group(1))
        if not name_shadowed(conn, obj):
            return False
        if object_present(conn, obj):
            return True  # already exactly the approved constraint
        raise MigrationRefused(
            "CONSTRAINT_CONFLICT",
            f"{added.group(1)}.{added.group(2)} already exists with a different definition;"
            f" an operator must inspect it — this runner will not overwrite it",
        )
    if re.match(r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS", statement, re.I):
        return False  # its own IF NOT EXISTS is the guard; skipping it would skip the operator class
    m = re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][\w]*)\s*\(", statement, re.I)
    if not m:
        return False
    name = m.group(1).lower()
    body = _function_body(statement)
    if name in approved_guards()["function"]:
        obj = SchemaObject("function", name)
        if object_present(conn, obj):
            return True
        if name_shadowed(conn, obj):
            raise MigrationRefused(
                "FUNCTION_CONFLICT",
                f"{name}() already exists with a different body; the guard's range expression"
                f" is opaque to the constraint text, so this must be inspected by an operator",
            )
        return False
    # A shared helper redeclared by a migration (the twin emits CREATE FUNCTION once per
    # migration). Skipping on the name alone would leave a replaced body in place, so the body
    # itself is compared before anything is skipped.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.proname = %s AND n.nspname = 'public'",
            (name,),
        )
        live = [str(row[0]) for row in cur.fetchall()]
    if not live:
        return False
    if body is None:
        raise MigrationRefused(
            "FUNCTION_CONFLICT",
            f"{name}() already exists and the migration's body cannot be read, so this runner"
            f" will not assume it is the same function",
        )
    if len(live) > 1:
        raise MigrationRefused(
            "FUNCTION_AMBIGUOUS", f"{len(live)} functions named {name}() exist in schema public"
        )
    if live[0].strip() != body.strip():
        raise MigrationRefused(
            "FUNCTION_CONFLICT",
            f"{name}() exists with a different body than the migration declares; an operator"
            f" must inspect it",
        )
    return True


def _statement_timeout(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        return str(cur.fetchone()[0])


def acquire_lock(conn: Any, timeout_seconds: int = 10) -> None:
    """Serialise runners. A bounded wait that expires is a refusal, not a queued surprise."""
    previous = _statement_timeout(conn)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{int(timeout_seconds)}s'")
        try:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        except Exception as exc:  # psycopg errors carry the SQLSTATE we want to keep
            conn.rollback()  # the cancelled statement left the transaction aborted
            with conn.cursor() as clean:
                clean.execute(f"SET statement_timeout = '{previous}'")
            conn.commit()
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate in ("57014", "55P03"):  # query_canceled / lock_not_available
                raise MigrationRefused(
                    "LOCK_TIMEOUT",
                    f"another migration runner holds the lock ({type(exc).__name__}); "
                    f"waited {timeout_seconds}s",
                ) from exc
            raise MigrationRefused(
                "LOCK_FAILED",
                f"could not take the migration lock ({type(exc).__name__}: {str(exc)[:160]})",
            ) from exc
        cur.execute(f"SET statement_timeout = '{previous}'")
    conn.commit()


def release_lock(conn: Any) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
        conn.commit()
    except Exception:  # pragma: no cover - closing the connection releases it anyway
        conn.rollback()


@dataclasses.dataclass
class RunReport:
    baseline: str
    applied: list[str] = dataclasses.field(default_factory=list)
    reconciled: list[str] = dataclasses.field(default_factory=list)
    already: list[str] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)
    survey: dict[str, int] = dataclasses.field(default_factory=dict)
    missing_guards: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def ensure_ledger(conn: Any, search_path: str = "public, pg_catalog") -> None:
    if ledger_exists(conn):
        missing = ledger_incompatible(conn)
        if missing:
            conn.rollback()
            raise MigrationRefused(
                "LEDGER_INCOMPATIBLE",
                f"{LEDGER_TABLE} exists without {', '.join(missing)}: this is not a ledger this "
                f"runner wrote. Inspect it before migrating; do not drop it to make the runner work.",
            )
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL search_path = {search_path}")
        cur.execute(LEDGER_DDL)
        cur.execute(
            f"INSERT INTO public.{LEDGER_TABLE} (migration_id, source_sha256, classification, state,"
            " applied_at, baseline_sha256, evidence)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (migration_id) DO NOTHING",
            (
                BASELINE_ID,
                baseline_sha256(),
                "BASELINE",
                BASELINE,
                _now(conn),
                baseline_sha256(),
                "approved twin applied by schema_loader.connect_pg(apply=True)",
            ),
        )
    conn.commit()


def _record_failure(conn: Any, migration: Migration, evidence: str) -> None:
    """Failure evidence in its own transaction: a rolled-back migration must not look successful."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path = public, pg_catalog")
            cur.execute(LEDGER_DDL)
        _ledger_write(conn, migration, FAILED, evidence)
        conn.commit()
    except Exception:  # pragma: no cover - the original error is what the operator needs
        conn.rollback()


def apply(
    conn: Any,
    *,
    migrations: Optional[Sequence[Migration]] = None,
    dry_run: bool = False,
    expect_baseline: Optional[str] = None,
    lock_timeout: int = 10,
    on_progress: Optional[Callable[[str], None]] = None,
) -> RunReport:
    """Plan, then apply what the plan says is pending. Refuses anything else."""
    migrations = tuple(migrations or discover())
    state = check_baseline(conn, expected_sha=expect_baseline)
    if state == "EMPTY":
        raise MigrationRefused(
            "EMPTY_DATABASE",
            "empty database: apply the approved baseline first "
            "(schema_loader.connect_pg(apply=True)), then migrate",
        )
    acquire_lock(conn, timeout_seconds=lock_timeout)
    try:
        steps = plan(conn, migrations)
        conflicts = [step for step in steps if step.planned_state == CONFLICTED]
        if conflicts:
            first = conflicts[0]
            raise MigrationRefused(
                "CONFLICTED",
                f"{len(conflicts)} migration(s) in an unknown state; first: "
                f"{first.migration.migration_id} — {first.reason}",
            )
        # Pre-flight: verify the storage guard BEFORE a single success is written down. A
        # reconciliation row that survives a failed gate is a false record of a migration.
        guard_setup_pending = any(
            step.planned_state == PENDING and step.migration.classification == POSTGRESQL_ONLY
            for step in steps
        )
        missing_guards = [] if guard_setup_pending else verify_guards(conn)
        if missing_guards and not dry_run:
            raise MigrationRefused(
                "MIGRATION_INCOMPLETE",
                f"storage-level overlap guard(s) missing or not matching the approved "
                f"definition: {', '.join(missing_guards)}. Nothing was written: a direct write "
                f"could create a double booking; repair the guard before migrating.",
            )
        try:
            ensure_ledger(conn)
        except MigrationRefused:
            raise
        except Exception as exc:  # read-only database, role without CREATE on public, ...
            conn.rollback()
            raise MigrationRefused(
                "LEDGER_UNWRITABLE",
                f"cannot create the migration ledger in this database"
                f" ({type(exc).__name__}: {str(exc)[:160]})",
            ) from exc
        report = RunReport(baseline=baseline_sha256())
        for step in steps:
            migration = step.migration
            if step.planned_state == ALREADY_APPLIED:
                report.already.append(migration.migration_id)
                continue
            evidence = step.reason
            statements = list(migration.ddl) if step.planned_state == PENDING else []
            statements += list(migration.data)
            if dry_run:
                (report.reconciled if step.planned_state == BASELINE_RECONCILED else report.applied).append(
                    migration.migration_id
                )
                if on_progress:
                    on_progress(f"DRY {step.planned_state} {migration.migration_id}: {evidence}")
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = public, pg_catalog")
                    compat = COMPAT_STEPS.get(migration.migration_id)
                    if compat is not None and step.planned_state == PENDING:
                        for statement in compat(conn):
                            _execute(cur, statement)
                    for statement in statements:
                        if _skip_covered(conn, statement):
                            continue
                        _execute(cur, statement)
                    if migration.classification == POSTGRESQL_ONLY:
                        # Same transaction as the DDL: the guard is verified before the row
                        # that says the guard was verified. Otherwise a failed gate leaves a
                        # committed success record behind, which is the lie M1 was about.
                        premature = verify_guards(conn)
                        if premature:
                            raise MigrationRefused(
                                "MIGRATION_INCOMPLETE",
                                f"{migration.migration_id} ran but the guard is still not the"
                                f" approved one: {', '.join(premature)}",
                            )
                    _ledger_write(
                        conn,
                        migration,
                        BASELINE_RECONCILED
                        if step.planned_state == BASELINE_RECONCILED
                        else ALREADY_APPLIED,
                        evidence,
                    )
                conn.commit()
            except MigrationRefused as exc:
                # A deliberate refusal (operator-only state, unsupported shape) is not a SQL
                # failure: rewriting it as MIGRATION_FAILED hides the instruction that says
                # "stop and run the documented upgrade with a backup".
                conn.rollback()
                report.failed.append(migration.migration_id)
                _record_failure(conn, migration, f"{exc.code}: {exc.message}")
                raise
            except Exception as exc:
                conn.rollback()
                report.failed.append(migration.migration_id)
                # The failure itself is recorded in its own transaction: a rolled-back
                # migration must not leave a ledger row claiming success, and an operator
                # needs the row that says it was attempted.
                _record_failure(conn, migration, f"{type(exc).__name__}: {str(exc)[:200]}")
                raise MigrationRefused(
                    "MIGRATION_FAILED",
                    f"{migration.migration_id} failed and was rolled back "
                    f"({type(exc).__name__}: {str(exc)[:160]})",
                ) from exc
            if step.planned_state == BASELINE_RECONCILED:
                report.reconciled.append(migration.migration_id)
            else:
                report.applied.append(migration.migration_id)
            if on_progress:
                on_progress(f"OK  {migration.migration_id}: {evidence}")
        report.survey = survey(conn)
        # Post-condition: the loop may have reported nothing to do while a guard is absent.
        missing_guards = verify_guards(conn)
        report.missing_guards = missing_guards
        if missing_guards and not dry_run:
            raise MigrationRefused(
                "MIGRATION_INCOMPLETE",
                f"storage-level overlap guard(s) missing or not matching the approved "
                f"definition: {', '.join(missing_guards)}. A direct write could create a "
                f"double booking; fix 002 before using this database.",
            )
        return report
    finally:
        release_lock(conn)


def mask_dsn(dsn: str) -> str:
    """Never print a password. The shape stays recognisable for the operator."""
    import re

    masked = re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", dsn)
    return re.sub(
        r"(?i)\b(password|sslpassword)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)",
        r"\1=***",
        masked,
    )

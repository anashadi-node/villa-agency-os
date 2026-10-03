"""Schema loading, statement splitting and cross-dialect parity parsing.

The repository keeps two semantically identical schema files:

* ``src/schema/001_initial.sql``     — SQLite (development, test, CI)
* ``src/schema/001_initial.pg.sql``  — PostgreSQL (production target)

This module is the single place that knows how to read them. Two independent
capabilities live here:

1. **Apply** — :func:`connect_sqlite` / :func:`connect_pg` open a connection and
   :func:`apply_schema` runs the DDL exactly once, guarded by ``schema_meta``.
2. **Parse** — :func:`parse_schema_model` turns DDL text into a normalised
   structural model (tables, columns, keys, indexes, triggers, enum literals).
   ``tests/test_schema.py`` diffs the two dialects' models and, with ``VILLA_PG_DSN``
   set, applies both files to a live PostgreSQL server, so drift fails the suite
   instead of silently reaching production.

Fail-closed rules: an unsupported construct raises :class:`SchemaParseError`
instead of being skipped, and a statement that cannot be classified raises
rather than being dropped.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
SCHEMA_VERSION = "001_initial"

DIALECT_FILES: Mapping[str, str] = {
    "sqlite": "001_initial.sql",
    "pg": "001_initial.pg.sql",
    "postgres": "001_initial.pg.sql",
    "postgresql": "001_initial.pg.sql",
}

# SQLite INTEGER and PostgreSQL BIGINT are the same *family*: exact integers.
# The parity model compares families, never raw type text.
_TYPE_FAMILIES: Mapping[str, str] = {
    "INTEGER": "int",
    "INT": "int",
    "BIGINT": "int",
    "SMALLINT": "int",
    "TEXT": "text",
    "VARCHAR": "text",
    "CHARACTER VARYING": "text",
}


class SchemaError(Exception):
    """Base class for schema-layer failures."""


class SchemaParseError(SchemaError):
    """Raised when DDL text contains a construct this parser does not model."""


class SchemaNotFound(SchemaError):
    """Raised when a dialect has no schema file on disk."""


class DriverUnavailable(SchemaError):
    """Raised when the driver/dependency for a dialect is not installed."""


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class _SplitterState:
    """Mutable cursor state for :func:`split_statements` (explicit, not a closure)."""

    depth: int = 0


def split_statements(sql: str) -> list[str]:
    """Split DDL text into complete statements.

    Handles the three things that break a naive ``sql.split(';')``:

    * single-quoted string literals (with ``''`` escaping),
    * line (``--``) and block (``/* */``, nestable) comments,
    * dollar-quoted bodies (``$$ … $$``, ``$tag$ … $tag$``) and SQLite
      ``CREATE TRIGGER … BEGIN … END;`` blocks, whose internal semicolons must
      not terminate the statement.

    Raises:
        SchemaParseError: ``END`` appears without an enclosing ``BEGIN`` inside a
            trigger body, or the text ends inside an unterminated construct.
            (``CASE … END`` inside a trigger body is deliberately unsupported:
            this schema does not use it, and guessing would be worse than
            failing.)
    """
    statements: list[tuple[str, int]] = []  # (text, terminated_at_index)
    buf: list[str] = []
    words: list[str] = []
    head: list[str] = []
    state = _SplitterState()
    pending_word: list[str] = []
    i = 0
    n = len(sql)

    def flush_word() -> None:
        if not pending_word:
            return
        token = "".join(pending_word)
        pending_word.clear()
        words.append(token.upper())
        if len(head) < 3:
            head.append(token.upper())
        if state.depth or (len(head) >= 2 and head[0] == "CREATE" and head[1] == "TRIGGER"):
            if token.upper() == "BEGIN":
                state.depth += 1
            elif token.upper() == "END":
                state.depth -= 1
                if state.depth < 0:
                    raise SchemaParseError(
                        "unbalanced END inside CREATE TRIGGER body (CASE…END is unsupported)"
                    )

    def reset_statement() -> None:
        nonlocal head
        state.depth = 0
        head = []

    while i < n:
        ch = sql[i]

        # -- comments -------------------------------------------------------
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i : j + 1])
                i = j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise SchemaParseError("unterminated block comment")
            buf.append(sql[i:j])
            i = j
            continue

        # -- string / quoted identifier ------------------------------------
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:  # '' escape
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise SchemaParseError(f"unterminated {quote} literal")
            flush_word()
            buf.append(sql[i : j + 1])
            i = j + 1
            continue

        # -- dollar quoting (plpgsql bodies) -------------------------------
        if ch == "$":
            m = re.match(r"\$[A-Za-z0-9_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j == -1:
                    raise SchemaParseError(f"unterminated dollar quote {tag}")
                flush_word()
                buf.append(sql[i : j + len(tag)])
                i = j + len(tag)
                continue

        # -- words / terminators -------------------------------------------
        if _WORD.match(ch):
            pending_word.append(ch)
            buf.append(ch)
            i += 1
            continue

        flush_word()

        if ch == "(":
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            if state.depth > 0:
                # semicolon inside BEGIN…END — part of the same statement
                buf.append(ch)
                i += 1
                continue
            text = "".join(buf).strip()
            if text:
                statements.append((text, i))
            buf.clear()
            words.clear()
            reset_statement()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush_word()
    tail = "".join(buf).strip()
    if tail:
        if state.depth > 0:
            raise SchemaParseError("unterminated CREATE TRIGGER body")
        if not _is_comment_only(tail):
            statements.append((tail, n))
    if state.depth > 0:
        raise SchemaParseError("unterminated CREATE TRIGGER body")
    return [s for s, _ in statements]


def _is_comment_only(text: str) -> bool:
    without_line = re.sub(r"--[^\n]*", "", text)
    without_block = re.sub(r"/\*.*?\*/", "", without_line, flags=re.S)
    return not without_block.strip()


def _strip_leading_comments(text: str) -> str:
    """Drop leading ``--`` / ``/* */`` comment lines so a header comment never
    becomes part of the statement head (it would defeat classification)."""
    rest = text.lstrip()
    while rest:
        if rest.startswith("--"):
            nl = rest.find("\n")
            if nl == -1:
                return ""
            rest = rest[nl + 1 :].lstrip()
            continue
        if rest.startswith("/*"):
            end = rest.find("*/", 2)
            if end == -1:
                raise SchemaParseError("unterminated block comment in statement")
            rest = rest[end + 2 :].lstrip()
            continue
        break
    return rest


def statement_kind(statement: str) -> str:
    """Classify a statement into a coarse kind (fail-closed on unknown)."""
    head = re.sub(r"\s+", " ", _strip_leading_comments(statement).strip())
    upper = head.upper()
    if upper.startswith("PRAGMA"):
        return "pragma"
    if upper.startswith("SET "):
        return "set"
    if upper.startswith("CREATE TABLE"):
        return "create_table"
    if upper.startswith("CREATE UNIQUE INDEX") or upper.startswith("CREATE INDEX"):
        return "create_index"
    if upper.startswith("CREATE TRIGGER"):
        return "create_trigger"
    if upper.startswith("CREATE FUNCTION") or upper.startswith("CREATE OR REPLACE FUNCTION"):
        return "create_function"
    if upper.startswith("INSERT INTO"):
        return "insert"
    if upper.startswith("COMMENT ON"):
        return "comment"
    raise SchemaParseError(f"unclassified statement: {head[:80]!r}")


def load_statements(dialect: str = "sqlite") -> list[str]:
    """Read a dialect's schema file and return its statements."""
    stats = read_schema(dialect)
    return split_statements(stats)


def schema_path(dialect: str = "sqlite") -> Path:
    key = dialect.lower()
    if key not in DIALECT_FILES:
        raise SchemaNotFound(f"unknown dialect {dialect!r}; expected one of {sorted(DIALECT_FILES)}")
    path = SCHEMA_DIR / DIALECT_FILES[key]
    if not path.exists():
        raise SchemaNotFound(f"schema file missing: {path}")
    return path


def read_schema(dialect: str = "sqlite") -> str:
    return schema_path(dialect).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Applying the schema
# ---------------------------------------------------------------------------


def connect_sqlite(path: str | os.PathLike[str] = ":memory:") -> sqlite3.Connection:
    """Open SQLite with the repo's required pragmas and the schema applied.

    ``foreign_keys`` is ON (off by default in SQLite), ``Row`` factory is set,
    and WAL is enabled for file-backed databases so a daemon and a report job
    can read concurrently.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    apply_schema(conn, "sqlite")
    return conn


def applied_version(conn: sqlite3.Connection) -> Optional[str]:
    """Return the applied schema version, or ``None`` for an empty database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "SELECT schema_version FROM schema_meta ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    return None if cur is None else str(cur[0])


def apply_schema(conn: sqlite3.Connection, dialect: str = "sqlite") -> str:
    """Apply the schema if not already applied. Idempotent. Returns the version."""
    if dialect.lower() != "sqlite":
        raise SchemaError("apply_schema(conn) supports the sqlite driver; use apply_pg_schema()")
    version = applied_version(conn)
    if version == SCHEMA_VERSION:
        return version
    if version is not None:
        raise SchemaError(
            f"schema_meta reports {version!r} but this build expects {SCHEMA_VERSION!r}; "
            "write a migration instead of re-applying the initial schema"
        )
    statements = load_statements("sqlite")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


def connect_pg(dsn: Optional[str] = None, *, apply: bool = True) -> Any:
    """Open PostgreSQL (production dialect).

    Requires ``psycopg`` (v3) or ``psycopg2``. When neither is installed the
    error says exactly what to install — the kernel never silently degrades to
    SQLite, because production behaviour must be the same code path.

    DSN resolution: argument → ``VILLA_PG_DSN`` → :class:`DriverUnavailable`.
    """
    dsn = dsn or os.environ.get("VILLA_PG_DSN")
    if not dsn:
        raise DriverUnavailable(
            "no PostgreSQL DSN: pass one or set VILLA_PG_DSN "
            "(see .env.template; never commit a real DSN)"
        )
    try:  # pragma: no cover - depends on the host having a driver
        import psycopg  # type: ignore
    except ImportError:  # pragma: no cover
        try:
            import psycopg2 as psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise DriverUnavailable(
                "PostgreSQL driver missing — install it with: python3 -m pip install 'psycopg[binary]'"
            ) from exc
    conn = psycopg.connect(dsn)  # pragma: no cover - exercised only with a live DB
    if apply:  # pragma: no cover
        apply_pg_schema(conn)
    return conn


def apply_pg_schema(conn: Any) -> str:  # pragma: no cover - needs a live server
    """Apply the PostgreSQL schema when it is not already present (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_meta')")
        row = cur.fetchone()
        present = row is not None and row[0] is not None
        if present:
            cur.execute("SELECT schema_version FROM schema_meta ORDER BY applied_at DESC LIMIT 1")
            got = cur.fetchone()
            if got and got[0] == SCHEMA_VERSION:
                return SCHEMA_VERSION
            raise SchemaError(
                f"schema_meta reports {got[0] if got else None!r} but this build expects "
                f"{SCHEMA_VERSION!r}; write a migration instead of re-applying"
            )
        for stmt in load_statements("pg"):
            cur.execute(stmt)
    conn.commit()
    return SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Structural parsing (used by the parity test)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    type_family: str
    nullable: bool
    default: Optional[str] = None
    inline_pk: bool = False

    def key(self) -> tuple[Any, ...]:
        return (self.name, self.type_family, self.nullable, _norm_expr(self.default), self.inline_pk)


@dataclass(frozen=True)
class ForeignKey:
    table: str
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: Optional[str] = None


@dataclass(frozen=True)
class Index:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool
    predicate: Optional[str] = None


@dataclass(frozen=True)
class Trigger:
    name: str
    table: str
    events: tuple[str, ...]
    timing: str


@dataclass
class Table:
    name: str
    columns: tuple[Column, ...] = ()
    primary_key: tuple[str, ...] = ()
    uniques: tuple[tuple[str, ...], ...] = ()
    checks: tuple[tuple[Optional[str], str], ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


@dataclass
class SchemaModel:
    dialect: str
    tables: dict[str, Table] = field(default_factory=dict)
    indexes: dict[str, Index] = field(default_factory=dict)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    functions: tuple[str, ...] = ()
    inserts: tuple[str, ...] = ()
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "tables": sorted(self.tables),
            "indexes": sorted(self.indexes),
            "triggers": sorted(self.triggers),
            "functions": sorted(self.functions),
            "enum_columns": {k: list(v) for k, v in sorted(self.enums.items())},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


_CONSTRAINT_HEAD = re.compile(r"^CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$", re.S | re.I)
_INLINE_CHECK = re.compile(r"\bCHECK\s*\(", re.I)
_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_IN_VALUES = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\(([^)]*)\)", re.I)


def _norm_expr(expr: Optional[str]) -> Optional[str]:
    if expr is None:
        return None
    text = re.sub(r"\s+", " ", expr.strip())
    text = re.sub(r"\bREAL\b", "REAL", text, flags=re.I)
    return text.lower().rstrip(";")


def _norm_predicate(pred: Optional[str]) -> Optional[str]:
    if pred is None:
        return None
    text = re.sub(r"\s+", " ", pred.strip()).lower()
    text = text.replace("::text", "")
    return text


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` at paren depth 0, ignoring quoted strings."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if body[j] == "'":
                    if j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise SchemaParseError("unterminated string literal while splitting")
            buf.append(body[i : j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise SchemaParseError("unbalanced ')' while splitting")
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if depth != 0:
        raise SchemaParseError("unbalanced '(' while splitting")
    tail = "".join(buf)
    if tail.strip():
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _kw_startswith(text: str, keyword: str) -> bool:
    """True when ``text`` starts with ``keyword`` as a whole word sequence.

    Plain ``str.startswith`` is wrong here: a column named ``check_in`` starts
    with ``"CHECK"`` and would be mistaken for a CHECK constraint. Requiring a
    word boundary after the keyword removes that class of bug.
    """
    pattern = keyword.replace(" ", r"\s+")
    return re.match(rf"{pattern}\b", text.strip(), re.I) is not None


def _extract_paren_group(text: str, start: int) -> tuple[str, int]:
    """Return the content of the parenthesis group starting at ``start``."""
    if text[start] != "(":
        raise SchemaParseError("expected '('")
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    raise SchemaParseError("unbalanced parenthesis group")


def _id_list(text: str) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", "", part).lower() for part in _split_top_level(text))


def _parse_column(defn: str, table: str) -> Column:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$", defn, re.S)
    if not m:
        raise SchemaParseError(f"unparsable column definition in {table}: {defn!r}")
    name, rest = m.group(1).lower(), m.group(2).strip()
    type_match = re.match(r"^([A-Za-z]+(?:\s+[A-Za-z]+)?)", rest)
    if not type_match:
        raise SchemaParseError(f"missing type for {table}.{name}: {rest!r}")
    raw_type = re.sub(r"\s+", " ", type_match.group(1).strip()).upper()
    family = _TYPE_FAMILIES.get(raw_type)
    if family is None:
        # trailing words may belong to the next construct ("TEXT NOT NULL")
        first = raw_type.split()[0]
        family = _TYPE_FAMILIES.get(first)
    if family is None:
        raise SchemaParseError(
            f"unknown type {raw_type!r} on {table}.{name}; add it to _TYPE_FAMILIES"
        )
    upper = rest.upper()
    nullable = "NOT NULL" not in upper
    inline_pk = "PRIMARY KEY" in upper
    default = None
    dm = re.search(r"\bDEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|CHECK\b|PRIMARY\b|REFERENCES\b|UNIQUE\b)|$)", rest, re.S | re.I)
    if dm:
        default = dm.group(1).strip()
    return Column(name=name, type_family=family, nullable=nullable, default=default, inline_pk=inline_pk)


def _strip_line_comments(text: str) -> str:
    """Drop ``--`` comments, leaving string literals untouched.

    Comments are legal SQL and this repository's own DDL uses them. Without this,
    a comment inside a table body reaches the column splitter and the *whole*
    schema becomes unreadable — every derived enum fails, not just that table.
    A ``--`` inside a quoted literal (a default value, say) is data, so the
    scanner steps over literals before it ever looks for a comment.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_table(statement: str) -> Table:
    m = re.match(r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", statement, re.I | re.S)
    if not m:
        raise SchemaParseError(f"unparsable CREATE TABLE: {statement[:80]!r}")
    table_name = m.group(1).lower()
    body, _ = _extract_paren_group(statement, m.end() - 1)

    columns: list[Column] = []
    pk: tuple[str, ...] = ()
    uniques: list[tuple[str, ...]] = []
    checks: list[tuple[Optional[str], str]] = []
    fks: list[ForeignKey] = []

    for part in _split_top_level(body):
        cname = None
        cm = _CONSTRAINT_HEAD.match(part)
        if cm:
            cname = cm.group(1).lower()
            part_tail = cm.group(2).strip()
        else:
            part_tail = part
        if _kw_startswith(part_tail, "PRIMARY KEY"):
            pk = _id_list(_extract_paren_group(part_tail, part_tail.index("("))[0])
            continue
        if _kw_startswith(part_tail, "UNIQUE"):
            uniques.append(_id_list(_extract_paren_group(part_tail, part_tail.index("("))[0]))
            continue
        if _kw_startswith(part_tail, "FOREIGN KEY"):
            local = _id_list(_extract_paren_group(part_tail, part_tail.index("("))[0])
            rm = re.search(r"REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", part_tail, re.I)
            if not rm:
                raise SchemaParseError(f"FOREIGN KEY without REFERENCES in {table_name}: {part_tail!r}")
            ref_cols = _id_list(_extract_paren_group(part_tail, rm.end() - 1)[0])
            od = re.search(r"ON\s+DELETE\s+(\w+(?:\s+\w+)?)", part_tail, re.I)
            on_delete = re.sub(r"\s+", " ", od.group(1).upper()) if od else None
            fks.append(
                ForeignKey(
                    table=table_name,
                    columns=local,
                    ref_table=rm.group(1).lower(),
                    ref_columns=ref_cols,
                    on_delete=on_delete,
                )
            )
            continue
        if _kw_startswith(part_tail, "CHECK"):
            inner = _extract_paren_group(part_tail, part_tail.index("("))[0]
            checks.append((cname, _norm_expr(inner) or ""))
            continue
        if _kw_startswith(part_tail, "CONSTRAINT"):
            raise SchemaParseError(f"unsupported table constraint in {table_name}: {part_tail!r}")
        column = _parse_column(part_tail, table_name)
        columns.append(column)
        im = _INLINE_CHECK.search(part_tail)
        if im:
            inner = _extract_paren_group(part_tail, im.end() - 1)[0]
            checks.append((None, _norm_expr(inner) or ""))
            if "PRIMARY KEY" in part_tail.upper():
                pk = (column.name,)

    if not pk and any(c.inline_pk for c in columns):
        pk = tuple(c.name for c in columns if c.inline_pk)
    return Table(
        name=table_name,
        columns=tuple(columns),
        primary_key=pk,
        uniques=tuple(sorted(uniques)),
        checks=tuple(sorted(checks, key=lambda item: (item[0] or "", item[1]))),
        foreign_keys=tuple(sorted(fks, key=lambda fk: (fk.columns, fk.ref_table, fk.ref_columns))),
    )


def _parse_index(statement: str) -> Index:
    m = re.match(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        statement,
        re.I | re.S,
    )
    if not m:
        raise SchemaParseError(f"unparsable CREATE INDEX: {statement[:80]!r}")
    cols, end = _extract_paren_group(statement, m.end() - 1)
    rest = statement[end:]
    pred = None
    pm = re.search(r"\bWHERE\b(.*)$", rest, re.I | re.S)
    if pm:
        pred = _norm_predicate(pm.group(1))
    return Index(
        name=m.group(2).lower(),
        table=m.group(3).lower(),
        columns=_id_list(cols),
        unique=bool(m.group(1)),
        predicate=pred,
    )


def _parse_trigger(statement: str) -> Trigger:
    m = re.match(
        r"CREATE\s+TRIGGER\s+([A-Za-z_][A-Za-z0-9_]*)\s+(BEFORE|AFTER|INSTEAD\s+OF)\s+"
        r"((?:UPDATE|INSERT|DELETE)(?:\s+OR\s+(?:UPDATE|INSERT|DELETE))*)"
        r"\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)",
        statement,
        re.I | re.S,
    )
    if not m:
        raise SchemaParseError(f"unparsable CREATE TRIGGER: {statement[:80]!r}")
    events = tuple(sorted(e.strip().upper() for e in re.split(r"\s+OR\s+", m.group(3), flags=re.I)))
    return Trigger(
        name=m.group(1).lower(),
        table=m.group(4).lower(),
        events=events,
        timing=re.sub(r"\s+", " ", m.group(2).upper()),
    )


def _string_literals(text: str) -> tuple[str, ...]:
    return tuple(m.group(1).replace("''", "'") for m in _STRING_LITERAL.finditer(text))


def parse_schema_model(dialect: str = "sqlite", *, text: Optional[str] = None) -> SchemaModel:
    """Parse a schema file into a normalised structural model.

    Raises:
        SchemaParseError: any construct this parser cannot model.
        SchemaNotFound: no file for the requested dialect.
    """
    sql = text if text is not None else read_schema(dialect)
    model = SchemaModel(dialect=dialect.lower())
    for raw_statement in split_statements(sql):
        statement = _strip_leading_comments(raw_statement)
        if not statement:
            continue
        # Comments must go before anything scans the text: the parenthesis scanner
        # walks the raw statement, so a lone '(' inside a comment breaks it, and a
        # comment sitting between definitions is handed to the column parser as if it
        # were a column.
        statement = _strip_line_comments(statement)
        kind = statement_kind(statement)
        if kind == "create_table":
            table = _parse_table(statement)
            model.tables[table.name] = table
            for cname, expr in table.checks:
                for col, values in _IN_VALUES.findall(expr):
                    literals = _string_literals(values)
                    if literals:
                        key = f"{table.name}.{col.lower()}"
                        existing = set(model.enums.get(key, ()))
                        merged = tuple(sorted(existing | set(literals)))
                        model.enums[key] = merged
        elif kind == "create_index":
            index = _parse_index(statement)
            model.indexes[index.name] = index
        elif kind == "create_trigger":
            trigger = _parse_trigger(statement)
            model.triggers[trigger.name] = trigger
        elif kind == "create_function":
            fm = re.search(r"FUNCTION\s+([A-Za-z_][A-Za-z0-9_]*)", statement, re.I)
            if not fm:
                raise SchemaParseError(f"unparsable CREATE FUNCTION: {statement[:80]!r}")
            model.functions = tuple(sorted(model.functions + (fm.group(1).lower(),)))
        elif kind == "insert":
            model.inserts = model.inserts + (re.sub(r"\s+", " ", statement.strip()).lower(),)
        elif kind in ("pragma", "set", "comment"):
            continue
        else:  # pragma: no cover - statement_kind raises first
            raise SchemaParseError(f"unhandled statement kind {kind!r}")
    if not model.tables:
        raise SchemaParseError(f"no tables found in {dialect} schema")
    return model


def iter_columns(model: SchemaModel) -> Iterator[tuple[str, Column]]:
    for table in model.tables.values():
        for column in table.columns:
            yield table.name, column

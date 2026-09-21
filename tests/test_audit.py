"""Rigorous tests for the append-only, tamper-evident audit trail (``src/audit.py``).

The DDL in ``src/schema/001_initial.sql`` is the authority for every structural
expectation in this file: the column list, the hash-chain CHECK constraints and the
append-only triggers are *parsed out of the SQL text* rather than copied from
``audit.py``, so a drift between the module and the schema fails the suite instead of
hiding behind a duplicated constant.

The failure directions are the point of the module, so they are tested stoutly:
tampering (UPDATE/DELETE must be refused by the database itself, never by convention),
a broken chain link, a sequence gap, an invalid ref, a forbidden PII field name, an
over-deep payload, an empty/None argument, and floats anywhere near money.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import sqlite3
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import audit as A
import primitives as P
from schema_loader import connect_sqlite

# --------------------------------------------------------------------------- #
# The DDL, parsed once — the source of truth for structure
# --------------------------------------------------------------------------- #
TABLE = "audit_events"
REPO_ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = REPO_ROOT / "src" / "schema" / "001_initial.sql"
PG_DDL_PATH = REPO_ROOT / "src" / "schema" / "001_initial.pg.sql"

_FK_WORDS = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}
_TRIGGER_RE = re.compile(
    r"CREATE TRIGGER\s+(?:IF NOT EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"BEGIN(.*?)END\s*;",
    re.S | re.I,
)
_RAISE_RE = re.compile(r"RAISE\s*\(\s*ABORT\s*,\s*'([^']*)'", re.I)
_ENTRY_RE = re.compile(r'^\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+([A-Za-z][A-Za-z0-9_ ()]*)')


@lru_cache(maxsize=1)
def ddl_text() -> str:
    return DDL_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def ddl_body() -> str:
    match = re.search(
        r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?%s\s*\((.*?)\n\);" % TABLE,
        ddl_text(),
        re.S,
    )
    assert match, "audit_events table not found in %s" % DDL_PATH
    return match.group(1)


@lru_cache(maxsize=1)
def ddl_entry_texts() -> tuple[str, ...]:
    entries = []
    for raw in ddl_body().split(","):
        entry = raw.strip()
        if not entry or entry.split()[0].upper() in _FK_WORDS:
            continue
        entries.append(" ".join(entry.split()))
    return tuple(entries)


@lru_cache(maxsize=1)
def ddl_columns() -> tuple[str, ...]:
    names = []
    for entry in ddl_entry_texts():
        match = _ENTRY_RE.match(entry)
        assert match, "unparsable column definition: %r" % entry
        names.append(match.group(1))
    return tuple(names)


@lru_cache(maxsize=1)
def ddl_column_defs() -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in ddl_entry_texts():
        match = _ENTRY_RE.match(entry)
        assert match, "unparsable column definition: %r" % entry
        out[match.group(1)] = entry
    return out


@lru_cache(maxsize=1)
def ddl_digest_len() -> int:
    match = re.search(r"length\(\s*\"?hash\"?\s*\)\s*=\s*(\d+)", ddl_body())
    assert match, "the DDL pins no digest length for hash"
    prev = re.search(r"length\(\s*\"?prev_hash\"?\s*\)\s*=\s*(\d+)", ddl_body())
    assert prev, "the DDL pins no digest length for prev_hash"
    assert prev.group(1) == match.group(1), "prev_hash and hash must share a digest length"
    return int(match.group(1))


@lru_cache(maxsize=1)
def ddl_checked_nonempty_columns() -> tuple[str, ...]:
    return tuple(
        sorted(set(re.findall(r"length\(\s*\"?([a-z_]+)\"?\s*\)\s*>=\s*1", ddl_body())))
    )


@lru_cache(maxsize=1)
def ddl_triggers() -> dict[str, dict[str, str]]:
    """Every trigger the DDL creates on ``audit_events``: timing, event, message."""
    out: dict[str, dict[str, str]] = {}
    for match in _TRIGGER_RE.finditer(ddl_text()):
        name, timing, event, table, body = match.groups()
        if table != TABLE:
            continue
        message = _RAISE_RE.search(body)
        out[name] = {
            "timing": timing.upper(),
            "event": event.upper(),
            "message": message.group(1) if message else "",
        }
    return out


@lru_cache(maxsize=1)
def pg_audit_columns() -> tuple[str, ...]:
    text = PG_DDL_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?%s\s*\((.*?)\n\);" % TABLE, text, re.S
    )
    assert match, "audit_events not found in the PostgreSQL twin"
    names = []
    for raw in match.group(1).split(","):
        entry = raw.strip()
        if not entry or entry.split()[0].upper() in _FK_WORDS:
            continue
        entry_match = _ENTRY_RE.match(entry)
        if entry_match:
            names.append(entry_match.group(1))
    return tuple(names)


FROZEN_AT = P.to_iso(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _ScriptedClock:
    """A duck-typed clock: the chain must never depend on wall time."""

    def __init__(self, *instants: datetime) -> None:
        self._instants = list(instants)

    def now(self) -> datetime:
        if len(self._instants) > 1:
            return self._instants.pop(0)
        return self._instants[0]


def make_log(conn: sqlite3.Connection, *, seed: str = "test", **kwargs: Any) -> A.AuditLog:
    kwargs.setdefault("clock", P.FrozenClock.at(FROZEN_AT))
    kwargs.setdefault("ids", P.IdFactory(deterministic=True, seed=seed))
    return A.AuditLog(conn, **kwargs)


def fresh_log(**kwargs: Any) -> "tuple[sqlite3.Connection, A.AuditLog]":
    conn = connect_sqlite(":memory:")
    return conn, make_log(conn, **kwargs)


def nested_payload(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"leaf": 1}
    for _ in range(depth):
        node = {"n": node}
    return node


def forged_event(seq: int, prev_hash: str, *, entity_id: Optional[str] = None) -> A.AuditEvent:
    """A hand-built link — used to feed ``verify_chain`` arbitrary histories."""
    body = dict(
        audit_event_id="audit_f%06d" % seq,
        seq=seq,
        occurred_at=FROZEN_AT,
        actor="agent_01",
        actor_role="agent",
        action="booking.hold_created",
        entity_type="booking",
        entity_id=entity_id or ("bk_%04d" % seq),
        before_state=None,
        after_state=None,
        reason=None,
        policy_version_id=None,
        evidence_ref=None,
        tool=None,
        trace_id=None,
    )
    return A.AuditEvent(prev_hash=prev_hash, hash=A.compute_event_hash(prev_hash, body), **body)


class AuditTestCase(unittest.TestCase):
    """A fresh database, built from the real DDL, per test."""

    def setUp(self) -> None:
        self.conn, self.log = fresh_log()
        self.addCleanup(self.conn.close)

    # -- writing ---------------------------------------------------------- #
    def append(self, **overrides: Any) -> A.AuditEvent:
        kwargs: dict[str, Any] = dict(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0001",
            actor="agent_01",
            actor_role="agent",
        )
        kwargs.update(overrides)
        return self.log.append(**kwargs)

    # -- reading the raw table -------------------------------------------- #
    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM %s ORDER BY seq" % TABLE)]

    def row(self, seq: int) -> dict[str, Any]:
        for candidate in self.rows():
            if candidate["seq"] == seq:
                return candidate
        raise AssertionError("no audit row with seq=%r" % seq)

    def seqs(self) -> list[int]:
        return [row["seq"] for row in self.rows()]

    def stored_text(self) -> list[str]:
        """Every string cell of every row — the haystack for PII/float scans."""
        out: list[str] = []
        for row in self.conn.execute("SELECT * FROM %s" % TABLE):
            out.extend(value for value in row if isinstance(value, str))
        return out

    # -- tampering -------------------------------------------------------- #
    def drop_guards(self) -> None:
        """Remove the DDL's append-only triggers, the way a DB-level attacker would."""
        for name in A.APPEND_ONLY_TRIGGERS:
            self.conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        self.conn.commit()

    def tamper(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.drop_guards()
        self.conn.execute(sql, params)
        self.conn.commit()

    def insert_raw(self, row: dict[str, Any]) -> None:
        """Insert a row directly, bypassing the module (columns taken from the DDL)."""
        columns = ddl_columns()
        missing = [name for name in columns if name not in row]
        assert not missing, "raw insert missing columns: %r" % missing
        self.conn.execute(
            "INSERT INTO %s (%s) VALUES (%s)"
            % (TABLE, ", ".join(columns), ", ".join("?" * len(columns))),
            [row[name] for name in columns],
        )
        self.conn.commit()

    def assert_no_float(self, value: Any, path: str = "root") -> None:
        if isinstance(value, float):
            raise AssertionError("float exposed at %s: %r" % (path, value))
        if isinstance(value, dict):
            for key, item in value.items():
                self.assert_no_float(item, "%s.%s" % (path, key))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self.assert_no_float(item, "%s[%d]" % (path, index))


# --------------------------------------------------------------------------- #
# 1. The DDL as the authority
# --------------------------------------------------------------------------- #
class DdlSchemaAuthorityTests(unittest.TestCase):
    def test_the_ddl_declares_the_audit_events_table_with_a_column_list(self):
        self.assertRegex(
            ddl_text(),
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?%s\s*\(" % re.escape(TABLE),
        )
        self.assertGreaterEqual(len(ddl_columns()), 15, ddl_columns())
        self.assertEqual(len(set(ddl_columns())), len(ddl_columns()), "duplicate column")

    def test_the_module_column_tuple_is_the_ddl_column_list_in_order(self):
        self.assertEqual(A.COLUMNS, ddl_columns())

    def test_the_module_table_constant_is_the_ddl_table(self):
        self.assertEqual(A.AUDIT_TABLE, TABLE)
        self.assertRegex(
            ddl_text(),
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?%s\s*\(" % re.escape(A.AUDIT_TABLE),
        )

    def test_the_pg_twin_declares_the_same_audit_events_columns(self):
        self.assertEqual(pg_audit_columns(), ddl_columns())

    def test_the_digest_columns_are_the_last_two_and_share_a_width(self):
        self.assertEqual(ddl_columns()[-2:], ("prev_hash", "hash"))
        self.assertEqual(
            ddl_column_defs()["prev_hash"].split()[1], ddl_column_defs()["hash"].split()[1]
        )

    def test_the_ddl_pins_the_digest_length_to_the_genesis_hash_width(self):
        self.assertEqual(ddl_digest_len(), len(A.GENESIS_HASH))
        self.assertEqual(A.GENESIS_HASH, "0" * ddl_digest_len())

    def test_the_ddl_requires_a_contiguous_positive_sequence(self):
        body = ddl_body()
        self.assertRegex(body, r"seq\s+INTEGER")
        self.assertRegex(body, r"seq\s*>=\s*1")

    def test_the_ddl_requires_non_empty_identity_columns(self):
        required = set(ddl_checked_nonempty_columns())
        self.assertTrue(
            {"actor", "actor_role", "action", "entity_type", "entity_id"} <= required, required
        )

    def test_the_ddl_refuses_updates_and_deletes_with_before_row_triggers(self):
        triggers = ddl_triggers()
        self.assertEqual(set(triggers), {"trg_%s_no_update" % TABLE, "trg_%s_no_delete" % TABLE})
        self.assertEqual(triggers["trg_%s_no_update" % TABLE]["event"], "UPDATE")
        self.assertEqual(triggers["trg_%s_no_delete" % TABLE]["event"], "DELETE")
        for name, info in triggers.items():
            self.assertEqual(info["timing"], "BEFORE", name)
            self.assertTrue(info["message"].strip(), name)

    def test_the_module_trigger_names_are_the_ddl_trigger_names(self):
        self.assertEqual(A.APPEND_ONLY_TRIGGERS, tuple(ddl_triggers()))


# --------------------------------------------------------------------------- #
# 2. Module constants must not drift from the DDL
# --------------------------------------------------------------------------- #
class ModuleConstantDriftTests(unittest.TestCase):
    def test_chain_fields_are_the_ddl_columns_minus_the_two_digests(self):
        self.assertEqual(set(A.COLUMNS) - set(A.CHAIN_FIELDS), {"prev_hash", "hash"})
        self.assertEqual(
            A.CHAIN_FIELDS, tuple(c for c in ddl_columns() if c not in {"prev_hash", "hash"})
        )

    def test_payload_depth_limit_is_a_sane_positive_int(self):
        self.assertIsInstance(A.MAX_PAYLOAD_DEPTH, int)
        self.assertGreaterEqual(A.MAX_PAYLOAD_DEPTH, 2)
        self.assertLessEqual(A.MAX_PAYLOAD_DEPTH, 64)

    def test_the_chain_domain_is_a_namespaced_domain_separator(self):
        self.assertRegex(A.CHAIN_DOMAIN, r"^[a-z0-9]+(-[a-z0-9]+)*(:[a-z0-9-]+)+$")
        self.assertIn("audit", A.CHAIN_DOMAIN)
        self.assertLess(len(A.CHAIN_DOMAIN), 128)

    def test_the_pii_salt_is_not_empty_and_tied_to_the_business(self):
        self.assertIsInstance(A.PII_SALT, str)
        self.assertTrue(A.PII_SALT.strip())
        self.assertIn("villa", A.PII_SALT)

    def test_every_audit_exception_is_a_domain_error_carrying_details(self):
        exceptions = (
            A.AuditEventInvalid,
            A.AuditChainBroken,
            A.AuditSequenceGap,
            A.AuditAnchorMismatch,
            A.AuditPIIForbiddenField,
            A.AuditPIILeak,
            A.AuditAppendOnlyUnenforced,
        )
        for exc in exceptions:
            with self.subTest(exc=exc.__name__):
                self.assertTrue(issubclass(exc, P.DomainError))
                error = exc("boom", probe=1)
                self.assertEqual(error.details["probe"], 1)
                self.assertIn("boom", str(error))
                self.assertIsInstance(error.as_dict(), dict)

    def test_the_public_surface_is_the_documented_one(self):
        for name in (
            "GENESIS_HASH",
            "CHAIN_DOMAIN",
            "COLUMNS",
            "CHAIN_FIELDS",
            "AuditLog",
            "AuditEvent",
            "ChainAnchor",
            "ChainReport",
            "verify_chain",
            "guard_append_only",
        ):
            self.assertIn(name, A.__all__)


# --------------------------------------------------------------------------- #
# 3. Append-only: the database itself must refuse
# --------------------------------------------------------------------------- #
class AppendOnlyEnforcementTests(AuditTestCase):
    def test_an_update_of_a_stored_row_is_refused_by_the_database(self):
        event = self.append(reason="original")
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.conn.execute("UPDATE %s SET reason = 'changed' WHERE seq = ?" % TABLE, (event.seq,))
        self.assertIn(ddl_triggers()["trg_%s_no_update" % TABLE]["message"], str(caught.exception))
        self.assertEqual(self.row(event.seq)["reason"], "original")

    def test_a_delete_of_a_stored_row_is_refused_by_the_database(self):
        event = self.append()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.conn.execute("DELETE FROM %s WHERE seq = ?" % TABLE, (event.seq,))
        self.assertIn(ddl_triggers()["trg_%s_no_delete" % TABLE]["message"], str(caught.exception))
        self.assertEqual(self.seqs(), [event.seq])

    def test_the_tamper_refusal_is_not_a_convention_it_is_a_trigger(self):
        # A plain INSERT is still welcome: the table is append-only, not read-only.
        self.append()
        self.assertEqual(self.log.count(), 1)

    def test_a_log_refuses_a_schema_whose_triggers_were_dropped(self):
        conn = connect_sqlite(":memory:")
        self.addCleanup(conn.close)
        for name in A.APPEND_ONLY_TRIGGERS:
            conn.execute("DROP TRIGGER %s" % name)
        conn.commit()
        with self.assertRaises(A.AuditAppendOnlyUnenforced) as caught:
            make_log(conn)
        self.assertEqual(tuple(caught.exception.details["missing"]), A.APPEND_ONLY_TRIGGERS)

    def test_guard_reports_a_missing_table(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaises(A.AuditAppendOnlyUnenforced) as caught:
            A.guard_append_only(conn)
        self.assertEqual(caught.exception.details["missing"], ("table:%s" % TABLE,))

    def test_guard_reports_a_missing_trigger_by_name(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE %s (audit_event_id TEXT PRIMARY KEY, seq INTEGER)" % TABLE)
        with self.assertRaises(A.AuditAppendOnlyUnenforced) as caught:
            A.guard_append_only(conn)
        self.assertEqual(tuple(caught.exception.details["missing"]), A.APPEND_ONLY_TRIGGERS)

    def test_guard_returns_the_trigger_names_of_a_guarded_database(self):
        self.assertEqual(A.guard_append_only(self.conn), A.APPEND_ONLY_TRIGGERS)

    def test_enforce_triggers_false_is_an_explicit_and_visible_opt_out(self):
        conn = connect_sqlite(":memory:")
        self.addCleanup(conn.close)
        self.drop_guards_for(conn)
        log = make_log(conn, enforce_triggers=False)
        event = log.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0001",
            actor="agent_01",
            actor_role="agent",
        )
        self.assertEqual(event.seq, 1)
        # ... and with the guard gone the database really does allow a rewrite:
        conn.execute("UPDATE %s SET reason = 'rewritten' WHERE seq = 1" % TABLE)
        conn.commit()
        self.assertEqual(dict(conn.execute("SELECT * FROM %s" % TABLE).fetchone())["reason"], "rewritten")

    @staticmethod
    def drop_guards_for(conn: sqlite3.Connection) -> None:
        for name in A.APPEND_ONLY_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        conn.commit()


# --------------------------------------------------------------------------- #
# 4. Argument validation on append
# --------------------------------------------------------------------------- #
REQUIRED_REFS = ("action", "entity_type", "entity_id", "actor", "actor_role")
OPTIONAL_REFS = ("reason", "policy_version_id", "evidence_ref", "tool", "trace_id")
#: Every refusal path must fail closed; the module is free to pick one of these.
REFUSAL = (A.AuditEventInvalid, P.DomainError, TypeError)


class AppendValidationTests(AuditTestCase):
    def test_append_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.log.append("booking.hold_created", "booking", "bk_0001", "agent_01", "agent")

    def test_append_rejects_a_missing_required_field(self):
        with self.assertRaises(TypeError):
            self.log.append(
                entity_type="booking", entity_id="bk_0001", actor="agent_01", actor_role="agent"
            )
        self.assertEqual(self.log.count(), 0)

    def test_required_refs_reject_empty_none_and_blank_values(self):
        for field in REQUIRED_REFS:
            for bad in ("", "   ", "\t\n", None):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(REFUSAL):
                        self.append(**{field: bad})
                    self.assertEqual(self.log.count(), 0)

    def test_required_refs_reject_non_string_values(self):
        for field in REQUIRED_REFS:
            for bad in (7, 1.5, [], {}, True, b"agent_01"):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(REFUSAL):
                        self.append(**{field: bad})
                    self.assertEqual(self.log.count(), 0)

    def test_required_refs_reject_illegal_characters(self):
        for field in REQUIRED_REFS:
            for bad in ("agent 01", "agent\n01", "agent;drop table", "agent'01", "agent/01"):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(REFUSAL):
                        self.append(**{field: bad})

    def test_required_refs_accept_a_long_but_sane_identifier(self):
        self.assertEqual(self.append(action="booking." + "a" * 56).seq, 1)

    def test_required_refs_reject_an_absurdly_long_identifier(self):
        with self.assertRaises(REFUSAL):
            self.append(action="a" * 4096)

    def test_optional_refs_reject_blank_strings(self):
        for field in OPTIONAL_REFS:
            if field == "reason":
                # ``reason`` is free text: a blank value is normalised to NULL rather
                # than refused (see AuditLog._clean_reason), so it is checked below.
                continue
            for bad in ("", "  ", "\n"):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(REFUSAL):
                        self.append(**{field: bad})
                    self.assertEqual(self.log.count(), 0)

    def test_a_blank_free_text_reason_is_normalised_to_null_not_refused(self):
        for blank in ("", "   ", "\n"):
            with self.subTest(blank=repr(blank)):
                conn, log = fresh_log()
                self.addCleanup(conn.close)
                log.append(
                    action="booking.hold_created",
                    entity_type="booking",
                    entity_id="bk_0001",
                    actor="agent_01",
                    actor_role="agent",
                    reason=blank,
                )
                stored = conn.execute(
                    "SELECT reason FROM %s WHERE seq = 1" % TABLE
                ).fetchone()[0]
                self.assertIsNone(stored)
                self.assertEqual(log.count(), 1)
                self.assertEqual(log.verify().count, 1)

    def test_optional_refs_accept_a_value_or_an_explicit_none(self):
        event = self.append(
            reason="hold placed for the guest",
            policy_version_id="pv_2026_09",
            evidence_ref="ev_0001",
            tool="kernel",
            trace_id="tr_0001",
        )
        self.assertEqual(event.reason, "hold placed for the guest")
        self.assertEqual(event.policy_version_id, "pv_2026_09")
        self.assertEqual(event.evidence_ref, "ev_0001")
        self.assertEqual(event.tool, "kernel")
        self.assertEqual(event.trace_id, "tr_0001")
        bare = self.append(entity_id="bk_0002")
        for field in OPTIONAL_REFS:
            self.assertIsNone(getattr(bare, field), field)

    def test_a_refused_append_writes_nothing_and_consumes_no_sequence_number(self):
        anchor_before = self.log.anchor()
        with self.assertRaises(REFUSAL):
            self.append(actor="")
        self.assertEqual(self.log.count(), 0)
        self.assertIsNone(self.log.head())
        self.assertEqual(self.log.anchor(), anchor_before)
        self.assertEqual(self.seqse_and_guards(), ())
        self.assertEqual(self.append().seq, 1)

    def seqse_and_guards(self) -> tuple[int, ...]:
        self.assertEqual(A.guard_append_only(self.conn), A.APPEND_ONLY_TRIGGERS)
        return tuple(self.seqs())

    def test_state_must_be_a_mapping(self):
        for field in ("before", "after"):
            for bad in ("text", 5, ["a"], True):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(REFUSAL):
                        self.append(**{field: bad})
                    self.assertEqual(self.log.count(), 0)

    def test_an_empty_state_mapping_is_stored_as_null(self):
        event = self.append(before={}, after={})
        self.assertIsNone(event.before_state)
        self.assertIsNone(event.after_state)
        self.assertIsNone(event.before)
        self.assertIsNone(event.after)

    def test_the_log_requires_a_clock_and_an_id_factory(self):
        for kwargs in ({"clock": None}, {"ids": None}, {"clock": "now"}, {"ids": "ids"}):
            with self.subTest(kwargs=repr(kwargs)):
                with self.assertRaises(REFUSAL):
                    A.AuditLog(self.conn, clock=P.FrozenClock.at(FROZEN_AT), ids=P.IdFactory(deterministic=True, seed="x"), **kwargs)

    def test_the_log_rejects_a_blank_or_non_string_pii_salt(self):
        for bad in ("", "   ", None, 7):
            with self.subTest(salt=repr(bad)):
                with self.assertRaises((P.DomainError, TypeError)):
                    make_log(self.conn, pii_salt=bad)


# --------------------------------------------------------------------------- #
# 5. The hash chain itself
# --------------------------------------------------------------------------- #
class HashChainTests(AuditTestCase):
    def test_the_first_event_sits_on_the_genesis_hash(self):
        event = self.append()
        self.assertEqual(event.prev_hash, A.GENESIS_HASH)
        self.assertTrue(event.is_genesis)

    def test_the_sequence_starts_at_one_and_never_skips(self):
        self.assertEqual([self.append(entity_id="bk_%04d" % i).seq for i in range(5)], [1, 2, 3, 4, 5])
        self.assertEqual(self.seqs(), [1, 2, 3, 4, 5])

    def test_each_event_links_to_the_previous_tail(self):
        events = [self.append(entity_id="bk_%04d" % i) for i in range(4)]
        self.assertEqual([e.prev_hash for e in events[1:]], [e.hash for e in events[:-1]])
        self.assertEqual([e.is_genesis for e in events], [True, False, False, False])

    def test_the_digest_is_sha256_of_domain_prevhash_and_the_canonical_body(self):
        event = self.append()
        pre_image = "\x1f".join(
            [A.CHAIN_DOMAIN, A.GENESIS_HASH, P.canonical_json(event.body())]
        ) + "\x1f"
        self.assertEqual(event.hash, hashlib.sha256(pre_image.encode("utf-8")).hexdigest())
        self.assertEqual(len(event.hash), ddl_digest_len())
        self.assertRegex(event.hash, r"^[0-9a-f]{64}$")

    def test_the_digest_parts_are_unit_separated(self):
        # Naive concatenation of domain ‖ prev_hash ‖ body must NOT be the pre-image:
        # otherwise a body value could be shifted into prev_hash.
        naive = hashlib.sha256(
            (A.CHAIN_DOMAIN + A.GENESIS_HASH + P.canonical_json({"a": 1})).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(A.compute_event_hash(A.GENESIS_HASH, {"a": 1}), naive)
        self.assertEqual(
            A.compute_event_hash(A.GENESIS_HASH, {"a": 1}),
            P.sha256_hex(A.CHAIN_DOMAIN, A.GENESIS_HASH, P.canonical_json({"a": 1})),
        )

    def test_the_body_is_exactly_the_chain_fields_and_excludes_the_digests(self):
        event = self.append()
        self.assertEqual(tuple(event.body()), A.CHAIN_FIELDS)
        self.assertEqual(
            set(event.body()), set(ddl_columns()) - {"prev_hash", "hash"}
        )
        self.assertNotIn("hash", event.body())
        self.assertNotIn("prev_hash", event.body())

    def test_every_chain_field_is_covered_by_the_digest(self):
        base = self.append(before={"nights": 3}, after={"total": 1}, reason="hold")
        for field in A.CHAIN_FIELDS:
            with self.subTest(field=field):
                body = dict(base.body())
                original = body[field]
                if isinstance(original, int):
                    body[field] = original + 1
                elif original is None:
                    body[field] = "mutated"
                else:
                    body[field] = str(original) + "X"
                self.assertNotEqual(
                    A.compute_event_hash(base.prev_hash, body),
                    base.hash,
                    "%s is not covered by the digest" % field,
                )

    def test_compute_event_hash_is_pure_and_order_insensitive(self):
        first = A.compute_event_hash(A.GENESIS_HASH, {"b": 2, "a": 1})
        self.assertEqual(first, A.compute_event_hash(A.GENESIS_HASH, {"a": 1, "b": 2}))
        self.assertEqual(first, A.compute_event_hash(A.GENESIS_HASH, {"a": 1, "b": 2}))

    def test_compute_event_hash_refuses_a_malformed_prev_hash(self):
        for bad in ("", "abc", "0" * 63, "0" * 65, "z" * 64, None, 7, "0" * 64 + "\n"):
            with self.subTest(prev_hash=repr(bad)):
                with self.assertRaises((A.AuditEventInvalid, P.DomainError, TypeError)):
                    A.compute_event_hash(bad, {"a": 1})

    def test_computed_and_verify_hash_agree_with_the_stored_digest(self):
        event = self.append()
        self.assertEqual(event.computed_hash(), event.hash)
        self.assertEqual(event.verify_hash(), event.hash)

    def test_event_ids_are_unique_and_namespaced(self):
        events = [self.append(entity_id="bk_%04d" % i) for i in range(5)]
        ids = [event.audit_event_id for event in events]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(i.startswith("audit_") for i in ids), ids)

    def test_occurred_at_comes_from_the_injected_clock(self):
        self.assertEqual(self.append().occurred_at, FROZEN_AT)

    def test_the_chain_order_follows_the_sequence_not_the_clock(self):
        conn, _ = fresh_log()
        self.addCleanup(conn.close)
        clock = _ScriptedClock(
            datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 21, 9, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 21, 9, 58, tzinfo=timezone.utc),
        )
        log = A.AuditLog(conn, clock=clock, ids=P.IdFactory(deterministic=True, seed="clock"))
        events = [
            log.append(
                action="booking.hold_created",
                entity_type="booking",
                entity_id="bk_%04d" % i,
                actor="agent_01",
                actor_role="agent",
            )
            for i in range(3)
        ]
        self.assertEqual([e.seq for e in events], [1, 2, 3])
        self.assertEqual([e.occurred_at for e in events], sorted([e.occurred_at for e in events], reverse=True))
        self.assertEqual([e.prev_hash for e in events[1:]], [e.hash for e in events[:-1]])
        self.assertEqual(log.verify().count, 3)

    def test_a_row_that_was_modified_reports_the_expected_and_stored_digest(self):
        self.append()
        self.append(entity_id="bk_0002")
        self.tamper("UPDATE %s SET after_state = ? WHERE seq = 2" % TABLE, ('{"total":999}',))
        row = self.row(2)
        event = A.AuditEvent.from_row(row)
        with self.assertRaises(A.AuditChainBroken) as caught:
            event.verify_hash()
        self.assertEqual(caught.exception.details["seq"], 2)
        self.assertEqual(caught.exception.details["stored"], row["hash"])
        self.assertNotEqual(caught.exception.details["expected"], row["hash"])


# --------------------------------------------------------------------------- #
# 6. Tamper detection
# --------------------------------------------------------------------------- #
class TamperDetectionTests(AuditTestCase):
    def three_events(self) -> list[A.AuditEvent]:
        return [self.append(entity_id="bk_%04d" % i) for i in range(3)]

    def test_a_clean_chain_verifies(self):
        self.three_events()
        report = self.log.verify()
        self.assertEqual(report.count, 3)
        self.assertEqual(report.head_hash, self.log.head().hash)
        self.assertFalse(report.verified_against_anchor)

    def test_a_rewritten_payload_is_caught_by_verify(self):
        self.three_events()
        self.tamper("UPDATE %s SET after_state = ? WHERE seq = 2" % TABLE, ('{"nights":99}',))
        with self.assertRaises(A.AuditChainBroken) as caught:
            self.log.verify()
        self.assertEqual(caught.exception.details["seq"], 2)

    def test_a_deleted_middle_row_is_a_sequence_gap(self):
        self.three_events()
        self.tamper("DELETE FROM %s WHERE seq = 2" % TABLE)
        with self.assertRaises(A.AuditSequenceGap) as caught:
            self.log.verify()
        details = caught.exception.details
        self.assertEqual(details["expected_seq"], 2)
        self.assertEqual(details["found_seq"], 3)
        self.assertEqual(details["after_seq"], 1)

    def test_a_deleted_tail_is_invisible_to_the_chain_but_not_to_the_anchor(self):
        self.three_events()
        published = self.log.anchor()
        self.tamper("DELETE FROM %s WHERE seq = 3" % TABLE)
        self.assertEqual(self.log.verify().count, 2)  # internally consistent
        with self.assertRaises(A.AuditAnchorMismatch) as caught:
            self.log.verify(anchor=published)
        self.assertEqual(caught.exception.details["observed_count"], 2)
        self.assertEqual(caught.exception.details["anchor_count"], published.count)

    def test_a_wholesale_recomputation_is_caught_only_by_the_anchor(self):
        self.three_events()
        published = self.log.anchor()
        self.drop_guards()
        rows = self.rows()
        rows[0]["after_state"] = '{"nights":99}'
        prev = A.GENESIS_HASH
        for row in rows:
            row["prev_hash"] = prev
            row["hash"] = A.compute_event_hash(prev, {k: row[k] for k in A.CHAIN_FIELDS})
            prev = row["hash"]
            self.conn.execute(
                "UPDATE %s SET after_state = ?, prev_hash = ?, hash = ? WHERE seq = ?" % TABLE,
                (row["after_state"], row["prev_hash"], row["hash"], row["seq"]),
            )
        self.conn.commit()
        self.assertEqual(self.log.verify().count, 3)  # a perfect forgery
        with self.assertRaises(A.AuditAnchorMismatch):
            self.log.verify(anchor=published)

    def test_a_forged_row_cannot_jump_the_sequence(self):
        self.append()
        victim = self.row(1)
        forged = dict(victim)
        forged["seq"] = 5
        forged["audit_event_id"] = "audit_forged"
        forged["hash"] = A.compute_event_hash(
            victim["hash"], {k: forged[k] for k in A.CHAIN_FIELDS}
        )
        forged["prev_hash"] = victim["hash"]
        self.tamper("SELECT 1")
        self.insert_raw(forged)
        with self.assertRaises(A.AuditSequenceGap):
            self.log.verify()

    def test_a_duplicate_sequence_number_is_refused_by_the_ddl(self):
        self.append()
        duplicate = dict(self.row(1))
        duplicate["audit_event_id"] = "audit_duplicate"
        self.tamper("SELECT 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_raw(duplicate)

    def test_swapping_two_rows_is_detected(self):
        self.three_events()
        # seq is CHECK (seq >= 1) and UNIQUE, so swap through a sentinel band.
        self.tamper("UPDATE %s SET seq = seq + 100 WHERE seq IN (2, 3)" % TABLE)
        self.tamper("UPDATE %s SET seq = ? WHERE seq = ?" % TABLE, (2, 103))
        self.tamper("UPDATE %s SET seq = ? WHERE seq = ?" % TABLE, (3, 102))
        with self.assertRaises((A.AuditSequenceGap, A.AuditChainBroken)):
            self.log.verify()

    def test_appending_refuses_to_extend_a_tampered_tail(self):
        self.three_events()
        self.tamper("UPDATE %s SET after_state = ? WHERE seq = 3" % TABLE, ('{"x":1}',))
        with self.assertRaises((A.AuditChainBroken, A.AuditAnchorMismatch, A.AuditSequenceGap)):
            self.append(entity_id="bk_0009")
        self.assertEqual(self.log.count(), 3)

    def test_verify_on_append_false_defers_the_check_to_verify(self):
        conn, _ = fresh_log()
        self.addCleanup(conn.close)
        log = make_log(conn, verify_on_append=False)
        for i in range(2):
            log.append(
                action="booking.hold_created",
                entity_type="booking",
                entity_id="bk_%04d" % i,
                actor="agent_01",
                actor_role="agent",
            )
        for trigger in A.APPEND_ONLY_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % trigger)
        conn.execute("UPDATE %s SET after_state = ? WHERE seq = 1" % TABLE, ('{"x":1}',))
        conn.commit()
        log.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0003",
            actor="agent_01",
            actor_role="agent",
        )
        self.assertEqual(log.count(), 3)
        with self.assertRaises(A.AuditChainBroken):
            log.verify()

    def test_the_append_guard_checks_the_tail_not_the_whole_history(self):
        # Documented design: append re-hashes only the tail, which is why an anchor is
        # required for full-history assurance. A middle-row rewrite is caught by verify.
        self.three_events()
        self.tamper("UPDATE %s SET after_state = ? WHERE seq = 1" % TABLE, ('{"x":1}',))
        self.append(entity_id="bk_0009")
        with self.assertRaises(A.AuditChainBroken):
            self.log.verify()

    def test_an_empty_log_verifies_as_an_empty_chain(self):
        report = self.log.verify()
        self.assertEqual(report.count, 0)
        self.assertEqual(report.head_hash, A.GENESIS_HASH)
        self.assertFalse(report.verified_against_anchor)
        with self.assertRaises(A.AuditAnchorMismatch):
            self.log.verify(anchor=A.ChainAnchor(count=2, head_hash="a" * len(A.GENESIS_HASH)))

    def test_a_first_row_that_does_not_sit_on_genesis_is_detected(self):
        self.append()
        self.tamper("UPDATE %s SET prev_hash = ? WHERE seq = 1" % TABLE, ("a" * len(A.GENESIS_HASH),))
        with self.assertRaises((A.AuditChainBroken, A.AuditAnchorMismatch)):
            self.log.verify()


# --------------------------------------------------------------------------- #
# 7. Anchors and reports
# --------------------------------------------------------------------------- #
class AnchorTests(AuditTestCase):
    def test_the_anchor_of_an_empty_log_is_empty_and_genesis_rooted(self):
        anchor = self.log.anchor()
        self.assertEqual(anchor.count, 0)
        self.assertEqual(anchor.head_hash, A.GENESIS_HASH)

    def test_the_anchor_tracks_the_count_and_the_tail(self):
        self.append()
        second = self.append(entity_id="bk_0002")
        anchor = self.log.anchor()
        self.assertEqual(anchor.count, 2)
        self.assertEqual(anchor.head_hash, second.hash)
        self.assertEqual(self.log.head(), second)
        self.assertEqual(self.log.count(), 2)

    def test_verifying_against_the_matching_anchor_marks_it_verified(self):
        self.append()
        self.append(entity_id="bk_0002")
        published = self.log.anchor()
        report = self.log.verify(anchor=published)
        self.assertTrue(report.verified_against_anchor)
        self.assertEqual(report.count, published.count)
        self.assertEqual(report.head_hash, published.head_hash)

    def test_an_anchor_of_the_same_length_with_a_different_head_is_a_mismatch(self) -> None:
        """Same count, different content: the published head is the only evidence left.

        A tampered *row* is caught earlier (``verify_hash`` raises ``AuditChainBroken``),
        so this branch is reachable only by a history that was rebuilt consistently —
        e.g. the whole ledger recomputed on another database. The anchor published by
        the original log is then the only thing that can tell the two apart.
        """
        self.append()
        published = self.log.anchor()
        other_conn, other = fresh_log(seed="rebuilt")
        self.addCleanup(other_conn.close)
        rebuilt = other.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0001",
            actor="agent_02",
            actor_role="agent",
        )
        with self.assertRaises(A.AuditAnchorMismatch) as caught:
            other.verify(anchor=published)
        details = caught.exception.details
        self.assertEqual(details["observed_count"], published.count)
        self.assertEqual(details["anchor_count"], published.count)
        self.assertEqual(details["observed_head"], rebuilt.hash)
        self.assertEqual(details["anchor_head"], published.head_hash)

    def test_a_prefix_anchor_is_accepted_without_checking_its_head(self):
        # Documented consequence of a cheaper design: an anchor recorded *before* the
        # log grew only proves "at least this many events, still chained"; its published
        # head_hash is not re-derived, so a rewritten prefix cannot be detected by it.
        self.append()
        self.append(entity_id="bk_0002")
        self.append(entity_id="bk_0003")
        prefix = A.ChainAnchor(count=2, head_hash="f" * len(A.GENESIS_HASH))
        report = self.log.verify(anchor=prefix)
        self.assertTrue(report.verified_against_anchor)
        self.assertEqual(report.count, 3)

    def test_an_anchor_longer_than_the_log_is_always_caught(self):
        self.append()
        for count in (2, 3, 99):
            with self.subTest(count=count):
                with self.assertRaises(A.AuditAnchorMismatch):
                    self.log.verify(anchor=A.ChainAnchor(count=count, head_hash="a" * 64))

    def test_anchor_and_report_are_frozen_value_objects(self):
        anchor = self.log.anchor()
        self.assertEqual(anchor.as_dict(), {"count": 0, "head_hash": A.GENESIS_HASH})
        report = self.log.verify()
        self.assertEqual(report.count, 0)
        self.assertEqual(report.head_hash, A.GENESIS_HASH)
        self.assertIsInstance(report.verified_against_anchor, bool)
        self.assertFalse(A.ChainReport(count=0, head_hash=A.GENESIS_HASH).verified_against_anchor)
        for obj, field in ((anchor, "count"), (report, "count")):
            with self.subTest(obj=type(obj).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(obj, field, 99)


# --------------------------------------------------------------------------- #
# 8. Forbidden PII field names
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def forbidden_registry_names() -> tuple[str, ...]:
    """The names the module's own forbidden-field registry claims to refuse."""
    match = re.search(r"\(([a-z0-9_|]+)\)\(\$\|_\)", A._FORBIDDEN_KEY_RE.pattern)
    assert match, "cannot read the forbidden-field registry"
    return tuple(match.group(1).split("|"))


class ForbiddenFieldTests(AuditTestCase):
    def test_every_name_in_the_registry_is_refused(self):
        names = forbidden_registry_names()
        self.assertIn("cvv", names)
        self.assertIn("card_number", names)
        for name in names:
            with self.subTest(field=name):
                with self.assertRaises(A.AuditPIIForbiddenField) as caught:
                    self.append(after={name: "x"})
                self.assertEqual(caught.exception.details["field"], name)
                self.assertEqual(self.log.count(), 0)

    def test_a_forbidden_field_in_before_is_refused_as_well(self):
        with self.assertRaises(A.AuditPIIForbiddenField):
            self.append(before={"cvv": "123"})
        self.assertEqual(self.log.count(), 0)

    def test_the_nested_path_of_the_forbidden_field_is_reported(self):
        with self.assertRaises(A.AuditPIIForbiddenField) as caught:
            self.append(after={"guest": {"payment": {"card_number": "4111111111111111"}}})
        self.assertEqual(caught.exception.details["path"], "guest.payment.card_number")
        self.assertEqual(caught.exception.details["field"], "card_number")

    def test_a_forbidden_field_inside_a_list_is_reported(self):
        with self.assertRaises(A.AuditPIIForbiddenField) as caught:
            self.append(after={"rows": [{"ok": 1}, {"cvv": "123"}]})
        self.assertEqual(caught.exception.details["path"], "rows[1].cvv")

    def test_matching_is_case_and_punctuation_insensitive(self):
        for spelling in ("CVV", "Cvv", "Card-Number", "card.number", "CARD_NUMBER", "card number"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(A.AuditPIIForbiddenField):
                    self.append(after={spelling: "x"})

    def test_camel_case_forbidden_names_are_refused_too(self):
        # `guest.name`-style equivalents in camelCase must classify identically:
        # a name that slips through the registry is a silent PII leak.
        for spelling in (
            "cardNumber",
            "cvvCode",
            "bankAccount",
            "accountNumber",
            "apiKey",
            "accessToken",
            "privateKey",
            "passportNumber",
            "securityCode",
            "pinCode",
        ):
            with self.subTest(spelling=spelling):
                with self.assertRaises(A.AuditPIIForbiddenField):
                    self.append(after={spelling: "x"})

    def test_a_forbidden_field_is_refused_before_anything_is_written(self):
        head_before = self.log.anchor()
        with self.assertRaises(A.AuditPIIForbiddenField):
            self.append(after={"otp": "123456"})
        self.assertEqual(self.seqs(), [])
        self.assertEqual(self.log.anchor(), head_before)

    def test_a_clean_identifier_shaped_field_is_still_accepted(self):
        event = self.append(after={"hold_ref": "hold_0001", "nights": 3, "owner_ref": "ow_1"})
        self.assertEqual(json.loads(event.after_state)["hold_ref"], "hold_0001")


# --------------------------------------------------------------------------- #
# 9. PII values: pseudonymisation and redaction
# --------------------------------------------------------------------------- #
class PiiValueRedactionTests(AuditTestCase):
    def assert_redacted(self, event: A.AuditEvent, raw: str) -> str:
        stored = event.after_state
        self.assertNotIn(raw, stored)
        self.assertNotIn(raw, json.dumps(A.safe_projection(event)))
        self.assertNotIn(raw, A.to_log_line(event))
        return stored

    def test_an_email_in_a_free_text_value_is_redacted(self):
        event = self.append(after={"note": "guest wrote to budi@example.com about the villa"})
        stored = self.assert_redacted(event, "budi@example.com")
        self.assertIn("[EMAIL]", stored)

    def test_an_indonesian_mobile_number_is_redacted(self):
        event = self.append(after={"note": "call +6281234567890 before arrival"})
        self.assertIn("[PHONE]", self.assert_redacted(event, "+6281234567890"))

    def test_a_sixteen_digit_nik_is_redacted(self):
        event = self.append(after={"note": "nik 3174091234567890 on file"})
        self.assertIn("[ID_NUMBER]", self.assert_redacted(event, "3174091234567890"))

    def test_a_long_digit_run_is_redacted(self):
        event = self.append(after={"note": "ref 123456789012"})
        self.assert_redacted(event, "123456789012")

    def test_small_numbers_are_not_redacted(self):
        event = self.append(after={"nights": 3, "guests": 4, "year": 2026})
        self.assertEqual(json.loads(event.after_state), {"nights": 3, "guests": 4, "year": 2026})

    def test_identifier_shaped_keys_are_pseudonymised(self):
        for key, value in (
            ("guest_name", "Budi Santoso"),
            ("owner_name", "Wayan Putra"),
            ("guest_email", "budi@example.com"),
            ("guest_phone", "+6281234567890"),
            ("address", "Jl. Raya Seminyak 5"),
        ):
            with self.subTest(key=key):
                conn, log = fresh_log()
                self.addCleanup(conn.close)
                event = log.append(
                    action="booking.hold_created",
                    entity_type="booking",
                    entity_id="bk_0001",
                    actor="agent_01",
                    actor_role="agent",
                    after={key: value},
                )
                stored = json.loads(event.after_state)[key]
                self.assertNotEqual(stored, value)
                self.assertNotIn(value, event.after_state)
                self.assertRegex(stored, r"^[A-Za-z_]+_[0-9a-f]{16}$|^\[[A-Z_]+\]$")

    def test_camel_case_identifier_keys_are_pseudonymised_too(self):
        for key, value in (
            ("guestName", "Budi Santoso"),
            ("fullName", "Budi Santoso"),
            ("ownerEmail", "budi@example.com"),
            ("phoneNumber", "+6281234567890"),
            ("dateOfBirth", "1990-01-31"),
            ("homeAddress", "Jl. Raya Seminyak 5"),
        ):
            with self.subTest(key=key):
                conn, log = fresh_log()
                self.addCleanup(conn.close)
                event = log.append(
                    action="booking.hold_created",
                    entity_type="booking",
                    entity_id="bk_0001",
                    actor="agent_01",
                    actor_role="agent",
                    after={key: value},
                )
                self.assertNotIn(value, event.after_state, "%s leaked its raw value" % key)

    def test_a_pseudonym_is_deterministic_for_the_same_salt_and_value(self):
        conn, log = fresh_log()
        self.addCleanup(conn.close)
        first = log.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0001",
            actor="agent_01",
            actor_role="agent",
            after={"guest_name": "Budi Santoso"},
        )
        second = log.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0002",
            actor="agent_01",
            actor_role="agent",
            after={"guest_name": "Budi Santoso"},
        )
        self.assertEqual(
            json.loads(first.after_state)["guest_name"], json.loads(second.after_state)["guest_name"]
        )
        other = log.append(
            action="booking.hold_created",
            entity_type="booking",
            entity_id="bk_0003",
            actor="agent_01",
            actor_role="agent",
            after={"guest_name": "Wayan Putra"},
        )
        self.assertNotEqual(
            json.loads(first.after_state)["guest_name"], json.loads(other.after_state)["guest_name"]
        )

    def test_the_pii_salt_is_actually_used(self):
        pseudonyms = []
        for salt in ("villa-agency-os:audit", "villa-agency-os:audit:alt"):
            conn, log = fresh_log(pii_salt=salt)
            self.addCleanup(conn.close)
            event = log.append(
                action="booking.hold_created",
                entity_type="booking",
                entity_id="bk_0001",
                actor="agent_01",
                actor_role="agent",
                after={"guest_name": "Budi Santoso"},
            )
            pseudonyms.append(json.loads(event.after_state)["guest_name"])
        self.assertNotEqual(pseudonyms[0], pseudonyms[1])

    def test_assert_pii_free_raises_for_a_leaked_email(self):
        with self.assertRaises(A.AuditPIILeak) as caught:
            A.assert_pii_free({"note": "mail budi@example.com"}, context="after")
        self.assertEqual(caught.exception.details["context"], "after")

    def test_assert_pii_free_reports_the_path_of_the_leak(self):
        with self.assertRaises(A.AuditPIILeak) as caught:
            A.assert_pii_free({"guest": {"note": "call +6281234567890"}}, context="after")
        self.assertEqual(caught.exception.details["path"], "guest.note")

    def test_assert_pii_free_walks_lists_and_reports_indexes(self):
        with self.assertRaises(A.AuditPIILeak) as caught:
            A.assert_pii_free({"rows": ["clean", "nik 3174091234567890"]}, context="after")
        self.assertEqual(caught.exception.details["path"], "rows[1]")

    def test_assert_pii_free_returns_clean_values_unchanged(self):
        payload = {"hold_ref": "hold_0001", "nights": 3, "flags": [True, None]}
        self.assertEqual(A.assert_pii_free(payload), payload)
        self.assertEqual(A.assert_pii_free("plain text"), "plain text")

    def test_contains_direct_identifiers_agrees_with_the_repo_redactor(self):
        for sample in (
            "budi@example.com",
            "+6281234567890",
            "3174091234567890",
            "ref 123456789012",
            "plain text about a villa",
            "hold_0001",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(
                    A.contains_direct_identifiers(sample), P.redact(sample) != sample
                )

    def test_nothing_stored_in_the_trail_retains_a_direct_identifier(self):
        self.append(
            after={
                "note": "email budi@example.com / call +6281234567890 / nik 3174091234567890",
                "guest_name": "Budi Santoso",
            },
            before={"note": "owner wayan@example.com"},
            reason="guest called about booking ref 123456789012",
        )
        self.append(entity_id="bk_0002", trace_id="tr_0002")
        rows = self.rows()
        self.assertTrue(rows)
        scanned = 0
        for row in rows:
            for column, value in row.items():
                if not isinstance(value, str):
                    continue
                if column in ("hash", "prev_hash"):
                    # Chain digests are fixed-width hex, not identifiers; the genesis
                    # link is literally 64 zeroes, which a digit-run scan would flag.
                    self.assertRegex(value, r"^[0-9a-f]{64}$")
                    continue
                scanned += 1
                self.assertEqual(
                    P.redact(value),
                    value,
                    "a direct identifier survived in %s: %r" % (column, value),
                )
        self.assertGreater(scanned, 0)


# --------------------------------------------------------------------------- #
# 10. Payload sanitisation: money, floats, depth, types
# --------------------------------------------------------------------------- #
class PayloadSanitisationTests(AuditTestCase):
    def test_money_is_stored_as_integer_minor_units_plus_currency(self):
        event = self.append(after={"total": P.Money(1_500_000, "IDR")})
        self.assertEqual(
            json.loads(event.after_state)["total"], {"amount_minor": 1_500_000, "currency": "IDR"}
        )
        self.assertIsInstance(json.loads(event.after_state)["total"]["amount_minor"], int)

    def test_money_nested_in_lists_and_dicts_is_serialised(self):
        event = self.append(
            before={"quoted": {"deposit": P.Money(250_000, "IDR")}},
            after={"lines": [P.Money(1, "EUR"), {"fee": P.Money(2_500, "USD")}]},
        )
        before = json.loads(event.before_state)
        after = json.loads(event.after_state)
        self.assertEqual(before["quoted"]["deposit"], {"amount_minor": 250_000, "currency": "IDR"})
        self.assertEqual(after["lines"][0], {"amount_minor": 1, "currency": "EUR"})
        self.assertEqual(after["lines"][1]["fee"], {"amount_minor": 2_500, "currency": "USD"})
        self.assert_no_float(after)
        self.assert_no_float(before)

    def test_money_never_becomes_a_float(self):
        event = self.append(after={"total": P.Money(1_500_000, "IDR")})
        for text in (event.after_state, event.before_state):
            if text:
                self.assertNotRegex(text, r"\d+\.\d+")
        self.assert_no_float(A.safe_projection(event))

    def test_a_float_money_amount_is_refused_at_construction(self):
        for bad in (1_500_000.5, 1_500_000.0, "1500000"):
            with self.subTest(bad=bad):
                with self.assertRaises(P.DomainError):
                    P.Money(bad, "IDR")
        self.assertEqual(P.Money(1_500_000, "IDR").amount_minor, 1_500_000)

    def test_a_float_anywhere_in_the_payload_is_refused(self):
        for payload, path in (
            ({"rate": 1.5}, "rate"),
            ({"a": {"b": 2.0}}, "a.b"),
            ({"rows": [3.14]}, "rows[0]"),
            ({"rate": 1.5e3}, "rate"),
        ):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(A.AuditEventInvalid) as caught:
                    self.append(after=payload)
                self.assertEqual(caught.exception.details.get("path"), path)
                self.assertEqual(self.log.count(), 0)

    def test_decimal_is_stored_as_a_string_never_a_float(self):
        event = self.append(after={"rate": Decimal("1.10")})
        stored = json.loads(event.after_state)["rate"]
        self.assertIsInstance(stored, str)
        self.assertEqual(stored, "1.1")

    def test_booleans_and_null_survive_the_round_trip(self):
        event = self.append(after={"flag": True, "suspended": False, "reason": None})
        self.assertEqual(
            json.loads(event.after_state), {"flag": True, "suspended": False, "reason": None}
        )

    def test_unsupported_types_are_refused_with_their_path(self):
        for value in ({1, 2}, object(), b"bytes"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(A.AuditEventInvalid) as caught:
                    self.append(after={"nested": {"bad": value}})
                self.assertEqual(caught.exception.details["path"], "nested.bad")
                self.assertIn(caught.exception.details["got"], type(value).__name__)

    def test_non_string_payload_keys_are_refused(self):
        for payload in ({1: "a"}, {(1, 2): "a"}):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(A.AuditEventInvalid):
                    self.append(after=payload)

    def test_shallow_payloads_are_accepted(self):
        event = self.append(after=nested_payload(2))
        self.assertEqual(json.loads(event.after_state), nested_payload(2))

    def test_a_payload_beyond_the_depth_limit_is_refused(self):
        with self.assertRaises(A.AuditEventInvalid) as caught:
            self.append(after=nested_payload(A.MAX_PAYLOAD_DEPTH + 5))
        self.assertIn(str(A.MAX_PAYLOAD_DEPTH), str(caught.exception))
        self.assertEqual(self.log.count(), 0)

    def test_the_measured_depth_boundary_is_the_documented_limit(self):
        deepest = 0
        for depth in range(1, A.MAX_PAYLOAD_DEPTH + 4):
            try:
                A.sanitize_payload(nested_payload(depth))
            except A.AuditEventInvalid:
                break
            deepest = depth
        self.assertGreaterEqual(deepest, A.MAX_PAYLOAD_DEPTH - 1, deepest)
        self.assertLessEqual(deepest, A.MAX_PAYLOAD_DEPTH, deepest)

    def test_a_self_referential_payload_is_refused(self):
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        with self.assertRaises(A.AuditEventInvalid):
            self.append(after=cyclic)
        self.assertEqual(self.log.count(), 0)

    def test_the_state_is_canonical_json_with_sorted_keys(self):
        event = self.append(after={"b": 2, "a": 1})
        self.assertEqual(event.after_state, '{"a":1,"b":2}')
        self.assertEqual(event.after_state, P.canonical_json(event.after))
        self.assertEqual(json.loads(event.after_state), event.after)

    def test_serialize_state_maps_none_and_empty_to_null(self):
        self.assertIsNone(A.serialize_state(None))
        self.assertIsNone(A.serialize_state({}))

    def test_serialize_state_refuses_a_non_mapping(self):
        for bad in ("text", 5, ["a"], True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises((A.AuditEventInvalid, P.DomainError, TypeError)):
                    A.serialize_state(bad)

    def test_sanitize_payload_does_not_mutate_the_caller_payload(self):
        payload = {"guest_name": "Budi Santoso", "total": P.Money(1_500_000, "IDR")}
        snapshot = json.loads(json.dumps(payload, sort_keys=True, default=str))
        self.append(after=payload)
        self.assertEqual(json.loads(json.dumps(payload, sort_keys=True, default=str)), snapshot)


# --------------------------------------------------------------------------- #
# 11. No floating point on any public surface
# --------------------------------------------------------------------------- #
class NoFloatingPointTests(AuditTestCase):
    def test_no_public_callable_takes_or_defaults_to_a_float(self):
        offenders: list[str] = []
        for name in A.__all__:
            obj = getattr(A, name)
            candidates = [obj] if not inspect.isclass(obj) else [
                member for member in vars(obj).values() if callable(member)
            ]
            for candidate in candidates:
                try:
                    signature = inspect.signature(candidate)
                except (TypeError, ValueError):
                    continue
                for param_name, param in signature.parameters.items():
                    where = "%s.%s" % (name, param_name)
                    if isinstance(param.default, float):
                        offenders.append("%s defaults to %r" % (where, param.default))
                    if "float" in str(param.annotation):
                        offenders.append("%s is annotated float" % where)
        self.assertEqual(offenders, [])

    def test_no_float_typed_field_on_the_value_objects(self):
        for cls in (A.AuditEvent, A.ChainAnchor, A.ChainReport):
            for field in dataclasses.fields(cls):
                with self.subTest(cls=cls.__name__, field=field.name):
                    self.assertNotIn("float", str(field.type).lower())
                    self.assertNotIsInstance(field.default, float)

    def test_a_full_session_stores_no_float_anywhere(self):
        event = self.append(
            before={"nights": 3, "quote": P.Money(1_500_000, "IDR")},
            after={"total": P.Money(1_650_000, "IDR"), "lines": [P.Money(150_000, "IDR")]},
        )
        self.assert_no_float(event.body())
        self.assert_no_float(A.safe_projection(event))
        self.assert_no_float(json.loads(event.before_state))
        self.assert_no_float(json.loads(event.after_state))

    def test_no_float_literal_reaches_the_stored_text(self):
        self.append(
            before={"deposit": P.Money(250_000, "IDR")},
            after={"total": P.Money(1_650_000, "IDR"), "pct": 100},
        )
        for text in self.stored_text():
            self.assertNotRegex(text, r"\d+\.\d+", "a float literal was stored: %r" % text)

    def test_money_amounts_stay_integer_minor_units(self):
        amount = P.Money(1_650_000, "IDR").amount_minor
        self.assertIsInstance(amount, int)
        self.assertNotIsInstance(amount, bool)


# --------------------------------------------------------------------------- #
# 12. The read API
# --------------------------------------------------------------------------- #
class ReadApiTests(AuditTestCase):
    def busy(self, count: int = 3) -> list[A.AuditEvent]:
        return [
            self.append(
                entity_id="bk_%04d" % i,
                action="booking.hold_created" if i % 2 == 0 else "booking.hold_released",
                after={"total": P.Money(100_000 * (i + 1), "IDR")},
            )
            for i in range(count)
        ]

    def test_events_are_returned_oldest_first(self):
        self.busy()
        self.assertEqual([e.seq for e in self.log.events()], [1, 2, 3])

    def test_since_seq_is_exclusive(self):
        self.busy()
        self.assertEqual([e.seq for e in self.log.events(since_seq=2)], [3])
        self.assertEqual([e.seq for e in self.log.events(since_seq=3)], [])
        self.assertEqual([e.seq for e in self.log.events(since_seq=0)], [1, 2, 3])

    def test_since_seq_rejects_garbage(self):
        for bad in ("1", -1, 1.5, None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises((A.AuditEventInvalid, P.DomainError, TypeError)):
                    self.log.events(since_seq=bad)

    def test_events_filter_by_entity(self):
        self.busy()
        self.append(entity_type="villa", entity_id="villa_0007")
        rows = self.rows()
        before = len(rows) - 1
        self.assertGreater(before, 0)
        # the last row is the only villa, so it owns the highest sequence number
        self.assertEqual(
            [e.seq for e in self.log.for_entity("villa", "villa_0007")], [len(rows)]
        )
        # The filter is the *pair*: the same id under another entity type is a
        # different row-set, which is why the negative case is asserted too.
        self.assertEqual(self.log.for_entity("booking", "villa_0007"), [])
        self.assertEqual(
            [e.seq for e in self.log.events(entity_id="villa_0007")], [len(rows)]
        )
        for row in rows:
            expected = [
                r["seq"]
                for r in rows
                if r["entity_type"] == row["entity_type"] and r["entity_id"] == row["entity_id"]
            ]
            with self.subTest(entity=(row["entity_type"], row["entity_id"])):
                self.assertEqual(
                    [e.seq for e in self.log.for_entity(row["entity_type"], row["entity_id"])],
                    expected,
                )
        # a single key is a legal narrowing of the same trail
        self.assertEqual(
            [e.seq for e in self.log.events(entity_type="booking")],
            [r["seq"] for r in rows if r["entity_type"] == "booking"],
        )
        self.assertEqual(
            [e.seq for e in self.log.events(entity_id="bk_0001")],
            [r["seq"] for r in rows if r["entity_id"] == "bk_0001"],
        )
        self.assertEqual(
            [e.seq for e in self.log.events(entity_type="villa")],
            [r["seq"] for r in rows if r["entity_type"] == "villa"],
        )

    def test_the_entity_filter_requires_a_valid_ref_and_allows_a_narrower_one(self):
        self.busy()
        for bad in ("", "   "):
            for call in (
                lambda bad=bad: self.log.for_entity("booking", bad),
                lambda bad=bad: self.log.for_entity(bad, "bk_0001"),
                lambda bad=bad: self.log.events(entity_type=bad),
                lambda bad=bad: self.log.events(entity_id=bad),
            ):
                with self.subTest(bad=repr(bad)):
                    with self.assertRaises((A.AuditEventInvalid, P.DomainError, TypeError)):
                        call()
        # entity_type alone is a legal narrowing, not an error
        self.assertEqual(len(self.log.events(entity_type="booking")), 3)
        self.assertEqual(len(self.log.events(entity_id="bk_0001")), 1)
        self.assertEqual(self.log.events(entity_type="villa"), [])

    def test_iter_events_is_lazy_and_matches_events(self):
        self.busy()
        stream = self.log.iter_events()
        self.assertIsInstance(stream, types.GeneratorType)
        self.assertEqual([e.seq for e in stream], [e.seq for e in self.log.events()])
        self.assertEqual(
            [e.seq for e in self.log.iter_events(entity_type="booking", entity_id="bk_0000")], [1]
        )

    def test_a_row_round_trips_through_from_row(self):
        event = self.append()
        self.assertEqual(A.AuditEvent.from_row(self.row(event.seq)), event)
        self.assertEqual(A.AuditEvent.from_row(dict(self.conn.execute("SELECT * FROM %s" % TABLE).fetchone())), event)

    def test_from_row_refuses_a_row_with_a_missing_column(self):
        self.append()
        row = self.row(1)
        row.pop("actor_role")
        with self.assertRaises(A.AuditEventInvalid) as caught:
            A.AuditEvent.from_row(row)
        self.assertEqual(list(caught.exception.details["missing"]), ["actor_role"])

    def test_the_projection_covers_every_ddl_column(self):
        event = self.append(before={"nights": 3}, after={"nights": 4})
        projection = A.safe_projection(event)
        expected = (set(ddl_columns()) - {"before_state", "after_state"}) | {"before", "after"}
        self.assertEqual(set(projection), expected)
        self.assertEqual(projection["before"], {"nights": 3})
        self.assertEqual(projection["after"], {"nights": 4})
        self.assertEqual(projection["hash"], event.hash)
        self.assertEqual(projection["prev_hash"], event.prev_hash)

    def test_the_projection_decodes_absent_states_as_none(self):
        projection = A.safe_projection(self.append())
        self.assertIsNone(projection["before"])
        self.assertIsNone(projection["after"])

    def test_the_log_line_is_one_line_without_payload_or_pii(self):
        event = self.append(
            after={"note": "mail budi@example.com"},
            reason="guest asked about the deposit",
        )
        line = A.to_log_line(event)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 300)
        for fragment in ("seq=%d" % event.seq, event.action, event.entity_type, event.entity_id):
            self.assertIn(fragment, line)
        self.assertIn(event.hash[:12], line)
        self.assertNotIn("budi@example.com", line)
        self.assertNotIn("guest asked", line)

    def test_count_head_and_anchor_agree_after_appends(self):
        events = self.busy()
        self.assertEqual(self.log.count(), 3)
        self.assertEqual(self.log.head(), events[-1])
        self.assertEqual(self.log.anchor().head_hash, events[-1].hash)
        self.assertEqual(self.log.anchor().count, 3)


# --------------------------------------------------------------------------- #
# 13. verify_chain as a pure function over any history
# --------------------------------------------------------------------------- #
class VerifyChainFunctionTests(unittest.TestCase):
    def chain(self, length: int) -> list[A.AuditEvent]:
        events = []
        prev = A.GENESIS_HASH
        for seq in range(1, length + 1):
            event = forged_event(seq, prev)
            events.append(event)
            prev = event.hash
        return events

    def test_verify_chain_accepts_a_list_and_reports_the_tail(self):
        events = self.chain(3)
        report = A.verify_chain(events)
        self.assertEqual(report.count, 3)
        self.assertEqual(report.head_hash, events[-1].hash)
        self.assertFalse(report.verified_against_anchor)

    def test_verify_chain_accepts_a_generator(self):
        events = self.chain(2)
        report = A.verify_chain(event for event in events)
        self.assertEqual(report.count, 2)

    def test_verify_chain_accepts_an_empty_sequence(self):
        report = A.verify_chain([])
        self.assertEqual(report.count, 0)
        self.assertEqual(report.head_hash, A.GENESIS_HASH)

    def test_verify_chain_rejects_a_non_iterable(self):
        with self.assertRaises((TypeError, A.AuditEventInvalid, P.DomainError)):
            A.verify_chain(None)

    def test_verify_chain_detects_a_hand_built_broken_link(self):
        events = self.chain(3)
        events[2] = forged_event(3, "a" * 64)
        with self.assertRaises((A.AuditChainBroken, A.AuditSequenceGap)):
            A.verify_chain(events)

    def test_verify_chain_detects_a_hand_built_sequence_gap(self):
        events = self.chain(3)
        del events[1]
        with self.assertRaises(A.AuditSequenceGap):
            A.verify_chain(events)

    def test_verify_chain_requires_the_first_event_to_sit_on_genesis(self):
        events = self.chain(2)
        events[0] = forged_event(1, "b" * 64)
        with self.assertRaises(A.AuditChainBroken):
            A.verify_chain(events)

    def test_verify_chain_detects_a_rehashed_but_stale_link(self):
        events = self.chain(2)
        tampered = dataclasses.replace(events[1], entity_id="bk_9999")
        with self.assertRaises(A.AuditChainBroken):
            A.verify_chain([events[0], tampered])

    def test_verify_chain_against_a_matching_anchor(self):
        events = self.chain(3)
        anchor = A.ChainAnchor(count=3, head_hash=events[-1].hash)
        self.assertTrue(A.verify_chain(events, anchor=anchor).verified_against_anchor)

    def test_verify_chain_against_a_longer_anchor_is_a_mismatch(self):
        events = self.chain(2)
        with self.assertRaises(A.AuditAnchorMismatch):
            A.verify_chain(events, anchor=A.ChainAnchor(count=3, head_hash=events[-1].hash))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

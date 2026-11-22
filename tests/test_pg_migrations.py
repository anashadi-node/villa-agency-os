"""PGM-T01..T20 — the PostgreSQL migration runner against a disposable PostgreSQL 16.

Two ways to reach a disposable server, both explicit:

* CI: ``VILLA_PG_DSN`` points at the ``postgres-parity`` job's service container, and the
  existing guard from ``tests/test_pg_overlap_concurrency.py`` proves the cluster is this
  run's own (GitHub-hosted, fresh postmaster, no foreign databases, least-privilege role).
* Local development: ``VILLA_PG_LOCAL_DSN`` must point at **loopback** and the harness
  creates (and drops) its own ``villa_pgm_<random>`` database there. It refuses anything
  that is not loopback and never touches a database it did not create. This exists so the
  SQL can be exercised without a CI round trip; it is not a production path and it is not
  consulted when ``VILLA_PG_DSN`` is set.

No test reads ``data/villa.db`` or any operational DSN: PGM-T17 asserts both.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Iterator

import pytest
from psycopg import sql as _pgsql

from psycopg.conninfo import conninfo_to_dict

import pg_migrations as PGM
import schema_loader as SL

from test_pg_overlap_concurrency import disposable_database
from test_pg_overlap_concurrency import _driver as pg_driver

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORICAL = REPO_ROOT / "tests" / "fixtures" / "pg" / "historical_001_f1612cd.pg.sql"
PG_ONLY = Path(SL.SCHEMA_DIR) / "002_pg_range_exclusions.pg.sql"
# 021 installs the cross-table occupancy guard on top of 002. A "guarded" database now means
# both files: 002 alone leaves hold x booking overlap unprotected, which is the P0 this pack
# exists to close, so a test that stopped at 002 would be certifying half a guard.
PG_ONLY_FILES = (PG_ONLY, Path(SL.SCHEMA_DIR) / "021_pg_cross_table_occupancy.pg.sql")
LOCAL_PREFIX = "villa_pgm_"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
OBJECT = PGM.SCHEMA_OBJECT


def _target() -> tuple[str, bool]:
    """``(dsn, is_ci)`` for the disposable server this run may use."""
    ci = (os.environ.get("VILLA_PG_DSN") or "").strip()
    if ci:
        return ci, True
    local = (os.environ.get("VILLA_PG_LOCAL_DSN") or "").strip()
    if local:
        return local, False
    pytest.skip(
        "no disposable PostgreSQL target: set VILLA_PG_DSN (CI) or VILLA_PG_LOCAL_DSN "
        "(loopback dev container)"
    )
    raise AssertionError("unreachable")


def _local_problem(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict  # type: ignore

    info = conninfo_to_dict(dsn)
    host = (info.get("host") or "").strip()
    if host not in LOOPBACK:
        return f"VILLA_PG_LOCAL_DSN must point at loopback, not {host!r}"
    if not info.get("port"):
        return "VILLA_PG_LOCAL_DSN must name an explicit port"
    return ""


@contextlib.contextmanager
def disposable() -> Iterator[str]:
    dsn, is_ci = _target()
    if is_ci:
        with disposable_database(dsn) as scoped:
            yield scoped
        return
    problem = _local_problem(dsn)
    if problem:
        pytest.fail(problem)
    psycopg = pg_driver()
    from psycopg import sql  # type: ignore
    from psycopg.conninfo import make_conninfo  # type: ignore

    name = LOCAL_PREFIX + secrets.token_hex(6)
    created = False
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        created = True
        yield make_conninfo(dsn, dbname=name)
    finally:
        if created and name.startswith(LOCAL_PREFIX):
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def connect(dsn: str) -> Any:
    return PGM.connect(dsn)


def run_script(conn: Any, path: Path) -> None:
    """Apply a .sql file statement by statement (the twin carries $$ bodies)."""
    text = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for statement in SL.split_statements(text):
            body = SL._strip_leading_comments(statement)
            if body:
                # Schema text contains `%` (LIKE patterns); send it untouched.
                cur.execute(_pgsql.SQL(body))
    conn.commit()


def apply_twin(dsn: str, path: Path | None = None) -> None:
    if path is None:
        SL.connect_pg(dsn, apply=True)
        return
    conn = connect(dsn)
    try:
        run_script(conn, path)
    finally:
        conn.close()


def migrate(dsn: str, **kwargs: Any) -> PGM.RunReport:
    conn = connect(dsn)
    try:
        return PGM.apply(conn, **kwargs)
    finally:
        conn.close()


def one(conn: Any, query: str, params: tuple = ()) -> Any:
    with conn.cursor() as cur:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        row = cur.fetchone()
    return row[0] if row else None


def table_exists(conn: Any, name: str) -> bool:
    return bool(one(conn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)))


def reference_definitions(conn: Any) -> dict[str, Any]:
    """Independent oracle: read the catalogue with raw SQL, nothing from pg_migrations.

    A test that asks the production matcher "does this look right?" reproduces the production
    defect. This enumerates every validated CHECK and EXCLUDE on holds/bookings — the guard's
    whole surface, discovered rather than remembered — plus the villa_iso_date body, verbatim.
    """
    with conn.cursor() as cur:
        cur.execute("SET search_path = public, pg_catalog")
        cur.execute(
            "SELECT t.relname, c.conname, c.contype, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = 'public' AND t.relname IN ('holds', 'bookings')"
            " AND c.contype IN ('c', 'x') AND c.convalidated"
            " AND (c.contype = 'c' OR EXISTS (SELECT 1 FROM pg_index i"
            "      WHERE i.indexrelid = c.conindid AND i.indisvalid))"
            " ORDER BY t.relname, c.conname"
        )
        objects = [
            {
                "kind": {"c": "check", "x": "exclusion"}[str(contype)],
                "table": str(table),
                "name": str(name),
                "definition": str(definition),
            }
            for table, name, contype, definition in cur.fetchall()
        ]
        cur.execute(
            "SELECT t.relname, g.tgname, pg_get_triggerdef(g.oid)"
            " FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = 'public' AND t.relname IN ('holds', 'bookings')"
            " AND NOT g.tgisinternal ORDER BY t.relname, g.tgname"
        )
        objects += [
            {"kind": "trigger", "table": str(table), "name": str(name), "definition": str(definition)}
            for table, name, definition in cur.fetchall()
        ]
        cur.execute(
            "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.proname LIKE 'villa!_%' ESCAPE '!'"
            " ORDER BY p.proname"
        )
        functions = {str(name): str(definition) for name, definition in cur.fetchall()}
    if not objects:
        raise AssertionError("the approved SQL created no constraint on holds/bookings")
    if not functions:
        raise AssertionError("approved SQL did not create its helper functions")
    return {"objects": objects, "function": functions}


def seed_booking(conn: Any, *, booking_id: str = "bk_pgm_1") -> None:
    """One booking with every foreign key it needs, inserted the way the schema demands."""
    now = "2026-09-21T00:00:00+00:00"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO properties (property_id, name, property_type, region, province,"
            " timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES ('prop_pgm', 'PGM Villa', 'villa', 'Bali', 'Bali', 'Asia/Makassar',"
            " 'SELLABLE', 'ACTIVATED', %s, %s)",
            (now, now),
        )
        cur.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES ('unit_pgm', 'prop_pgm', 'PGM Unit', 'entire_villa', 3, 3, 2, 6,"
            " 'ACTIVE', %s, %s)",
            (now, now),
        )
        cur.execute(
            "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
            " effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
            " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
            " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('pv_pgm', 'prop_pgm', 'unit', 1, '2026-01-01', '2027-01-01', 'IDR',"
            " '30', '5', '10', '10', 24, 60, 2, '{}', %s, 'test', 'owner-1', %s)",
            ("0" * 64, now),
        )
        cur.execute(
            "INSERT INTO bookings (booking_id, agency_id, villa_supplier_relationship_id,"
            " property_id, unit_id, check_in, check_out, nights, status, guest_ref, adults,"
            " children, infants, currency, policy_version_id, policy_fingerprint, total_minor,"
            " amount_paid_minor, security_deposit_minor, payment_status, idempotency_key,"
            " version_lock, created_at, updated_at)"
            " VALUES (%s, 'agency_default', 'route_default', 'prop_pgm', 'unit_pgm',"
            " '2026-10-01', '2026-10-04', 3, 'PAID_IN_FULL', 'guest_pgm', 2, 0, 0, 'IDR',"
            " 'pv_pgm', %s, 5000000, 5000000, 0, 'CLEARED_FULL', %s, 1, %s, %s)",
            (booking_id, "0" * 64, f"idem_{booking_id}", now, now),
        )
    conn.commit()


class MigrationCase(unittest.TestCase):
    """One disposable database per test — a shared one would hide ordering bugs."""

    def setUp(self) -> None:
        self._ctx = disposable()
        self.dsn = self._ctx.__enter__()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def fresh(self, guarded: bool = False) -> Any:
        apply_twin(self.dsn)
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        if guarded:
            for path in PG_ONLY_FILES:
                run_script(conn, path)
        return conn

    def historical(self) -> Any:
        apply_twin(self.dsn, HISTORICAL)
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        return conn

    def migrated(self) -> Any:
        conn = self.fresh()
        PGM.apply(conn)
        return conn

    def declared_credit(self, name: str) -> str:
        """Independent oracle: which approved file contains the declaration."""
        found = [
            path.stem.split("_", 1)[0]
            for path in PG_ONLY_FILES
            if f"FUNCTION {name}(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(len(found), 1, f"{name} must be declared by exactly one approved file")
        return found[0]


class TestFreshBaseline(MigrationCase):
    def test_T01_fresh_database_initialises_and_reconciles(self):
        conn = self.fresh()
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertEqual(report.failed, [])
        self.assertEqual(len(report.applied) + len(report.reconciled), len(PGM.discover()))
        self.assertTrue(table_exists(conn, "supplier_contacts"))
        self.assertTrue(table_exists(conn, PGM.LEDGER_TABLE))
        self.assertEqual(one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE state='BASELINE'"), 1)

    def test_T03_repeated_migration_is_idempotent(self):
        conn = self.fresh()
        first = PGM.apply(conn)
        oid = one(conn, "SELECT 'public.supplier_contacts'::regclass::oid")
        second = PGM.apply(conn)
        self.assertEqual((second.applied, second.reconciled, second.failed), ([], [], []))
        self.assertEqual(len(second.already), len(PGM.discover()))
        self.assertEqual(one(conn, "SELECT 'public.supplier_contacts'::regclass::oid"), oid)
        self.assertEqual(
            len(first.applied) + len(first.already) + len(first.reconciled), len(PGM.discover())
        )

    def test_T18_reopening_a_migrated_database_is_safe(self):
        conn = self.fresh()
        PGM.apply(conn)
        conn.close()
        again = connect(self.dsn)
        self.addCleanup(again.close)
        report = PGM.apply(again)
        self.assertEqual(report.applied, [])
        self.assertEqual(report.failed, [])

    def test_T12_reference_data_parity(self):
        conn = self.fresh()
        PGM.apply(conn)
        keys = {
            row[0]
            for row in self._rows(conn, "SELECT policy_key FROM policy_defs")
        }
        self.assertIn("commercial.margin_floor_minor", keys)
        self.assertIn("security.ai.denied_actions", keys)
        self.assertIn("commercial.fx.max_age_hours", keys)
        self.assertGreaterEqual(
            one(
                conn,
                "SELECT count(*) FROM policy_revisions WHERE policy_key ="
                " 'security.ai.denied_actions' AND version = 2",
            ),
            1,
        )
        self.assertGreater(one(conn, "SELECT count(*) FROM policy_defs"), 0)

    def _rows(self, conn: Any, query: str) -> list[Any]:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()

    def test_T13_duplicate_objects_are_not_recreated(self):
        conn = self.fresh()
        PGM.apply(conn)
        before = one(
            conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='public' AND c.relkind IN ('r','i')"
        )
        PGM.apply(conn)
        after = one(
            conn, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='public' AND c.relkind IN ('r','i')"
        )
        self.assertEqual(before, after)

    def test_T19_pg_only_exclusions_verified(self):
        conn = self.fresh()
        run_script(conn, PG_ONLY)
        PGM.apply(conn)
        self.assertTrue(bool(one(conn, "SELECT to_regprocedure('villa_iso_date(text)') IS NOT NULL")))
        self.assertEqual(
            one(
                conn,
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid"
                " WHERE t.relname IN ('holds','bookings') AND c.contype='x'",
            ),
            2,
        )
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)


class TestReviewFindings(MigrationCase):
    """Regressions for the three blocking findings of the bounded independent review."""

    def test_T21_runner_applies_the_range_exclusions_itself(self):
        # B1 / F1: a green run over a schema with zero EXCLUDE constraints is the one outcome
        # the runner must never produce.
        conn = self.fresh()
        seed_booking(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
                " evidence_level, source_type, source_ref, captured_at, valid_until, result,"
                " can_quote, can_hold, can_confirm, created_at)"
                " VALUES ('ev_pgm', 'agency_default', 'unit_pgm', 'A', 'property_api', 'ref_pgm',"
                " '2026-09-21', '2026-12-31', 'AVAILABLE', 1, 1, 1, '2026-09-21')"
            )
        conn.commit()
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 0)
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertIn(PGM.pg_only_migration().migration_id, report.applied)
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)
        now = "2026-09-21T00:00:00+00:00"
        hold = (
            "INSERT INTO holds (hold_id, agency_id, unit_id, property_id, check_in, check_out,"
            " nights, status, guest_ref, policy_version_id, evidence_id, idempotency_key,"
            " created_at, expires_at, expires_timezone, extended_count)"
            " VALUES (%s, 'agency_default', 'unit_pgm', 'prop_pgm', %s, %s, 2, 'CONFIRMED',"
            " 'guest_pgm', 'pv_pgm', 'ev_pgm', %s, %s, %s, 'Asia/Makassar', 0)"
        )
        with conn.cursor() as cur:
            cur.execute(hold, ("h1", "2026-10-01", "2026-10-03", "idem_h1_pgm", now, "2026-09-22T00:00:00+00:00"))
        conn.commit()
        # The storage guard is real: PostgreSQL rejects the partial overlap itself.
        with self.assertRaises(Exception) as ctx:
            with conn.cursor() as cur:
                cur.execute(hold, ("h2", "2026-10-02", "2026-10-04", "idem_h2_pgm", now, "2026-09-22T00:00:00+00:00"))
        self.assertIn("exclusion", str(ctx.exception).lower())
        conn.rollback()
        self.assertEqual(one(conn, "SELECT count(*) FROM holds"), 1)
        # Idempotent: a second run neither duplicates nor drops them.
        PGM.apply(conn)
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)

    def test_T22_repaired_failure_does_not_poison_the_ledger(self):
        # B2 / F2: repairing a failed migration changes its source; the ledger must follow.
        conn = self.fresh()
        PGM.apply(conn)
        broken = PGM.Migration(
            migration_id="999_repairable",
            source=Path("999_repairable.sql"),
            sha256="1" * 64,
            classification=OBJECT,
            objects=(PGM.SchemaObject("table", "pgm_repairable"),),
            ddl=("CREATE TABLE pgm_repairable (id TEXT)", "CREATE TABLE pgm_repairable (id TEXT)"),
            data=(),
        )
        with self.assertRaises(PGM.MigrationRefused):
            PGM.apply(conn, migrations=tuple(PGM.discover()) + (broken,))
        self.assertEqual(
            one(conn, "SELECT state FROM pg_schema_migrations WHERE migration_id='999_repairable'"), PGM.FAILED
        )
        repaired = PGM.Migration(
            migration_id="999_repairable",
            source=Path("999_repairable.sql"),
            sha256="2" * 64,
            classification=OBJECT,
            objects=broken.objects,
            ddl=("CREATE TABLE pgm_repairable (id TEXT)",),
            data=(),
        )
        second = PGM.apply(conn, migrations=tuple(PGM.discover()) + (repaired,))
        self.assertIn("999_repairable", second.applied)
        self.assertEqual(
            one(conn, "SELECT source_sha256 FROM pg_schema_migrations WHERE migration_id='999_repairable'"),
            "2" * 64,
        )
        third = PGM.apply(conn, migrations=tuple(PGM.discover()) + (repaired,))
        self.assertEqual(third.failed, [])
        self.assertIn("999_repairable", third.already)

    def test_T23_refusal_codes_survive_the_apply_loop(self):
        # B3 / F5: OPERATOR_REQUIRED tells an operator to stop; MIGRATION_FAILED invites a retry.
        conn = self.fresh()
        PGM.apply(conn)

        def refuse(_conn: Any) -> tuple[str, ...]:
            raise PGM.MigrationRefused("OPERATOR_REQUIRED", "unsupported pre-G1 shape needs an operator")

        guarded = PGM.Migration(
            migration_id="998_operator_only",
            source=Path("998_operator_only.sql"),
            sha256="3" * 64,
            classification=OBJECT,
            objects=(PGM.SchemaObject("table", "pgm_operator_only"),),
            ddl=("CREATE TABLE pgm_operator_only (id TEXT)",),
            data=(),
        )
        compat = dict(PGM.COMPAT_STEPS)
        compat[guarded.migration_id] = refuse
        original = PGM.COMPAT_STEPS
        PGM.COMPAT_STEPS = compat
        try:
            with self.assertRaises(PGM.MigrationRefused) as ctx:
                PGM.apply(conn, migrations=tuple(PGM.discover()) + (guarded,))
        finally:
            PGM.COMPAT_STEPS = original
        self.assertEqual(ctx.exception.code, "OPERATOR_REQUIRED")
        self.assertFalse(table_exists(conn, "pgm_operator_only"))
        evidence = one(
            conn, "SELECT evidence FROM pg_schema_migrations WHERE migration_id='998_operator_only'"
        )
        self.assertIn("OPERATOR_REQUIRED", evidence)

    def test_T25_same_name_constraint_that_is_not_an_exclusion_is_refused(self):
        # Reviewer probe A: a CHECK answering to the guard's name protects nothing.
        conn = self.fresh()
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap CHECK (true)")
            cur.execute("ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap CHECK (true)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "CONFLICTED")
        self.assertIn("ex_holds_no_overlap", ctx.exception.message)
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 0)

    def test_T26_exclusion_with_the_wrong_predicate_is_refused(self):
        # Reviewer probe C: correct names and columns, stale status predicate -> no protection.
        conn = self.fresh()
        run_script(conn, PG_ONLY)  # the helper function, then replace the guards
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap")
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap")
            cur.execute(
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'REQUESTED')"
            )
            cur.execute(
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')"
            )
        conn.commit()
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)  # counted, but not trusted
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "CONFLICTED")

    def test_T27_apply_fails_closed_when_the_storage_guard_is_absent(self):
        # Even if 002 is somehow skipped, the loop must not return success without the guard.
        conn = self.fresh()
        without_002 = tuple(m for m in PGM.discover() if not m.migration_id.startswith("002"))
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn, migrations=without_002)
        # 021 is written against the immutability helper 002 installs, so without 002 it fails
        # while running rather than while verifying. Both are refusals; neither may leave an
        # exclusion or a success row behind.
        self.assertIn(ctx.exception.code, ("MIGRATION_INCOMPLETE", "MIGRATION_FAILED"))
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 0)
        # The ledger table itself may exist (it is created before the loop); what must not
        # exist is a row claiming anything succeeded.
        self.assertEqual(
            one(conn, "SELECT count(*) FROM public.pg_schema_migrations")
            if one(conn, "SELECT to_regclass('public.pg_schema_migrations')") else 0,
            0,
        )

    def test_T28_negated_predicate_with_the_same_literals_is_refused(self):
        # Reviewer probe B: same names, same literals, same columns -- predicate negated.
        conn = self.fresh()
        run_script(conn, PG_ONLY)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap")
            cur.execute(
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status <> ALL (ARRAY['REQUESTED','AWAITING_SUPPLIER',"
                "'CONFIRMED','EXTENSION_REQUESTED','RELEASE_REQUESTED',"
                "'EXPIRED_PENDING_VERIFICATION']))"
            )
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap")
            cur.execute(
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status <> ALL (ARRAY['CONFIRMED','BALANCE_DUE',"
                "'BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY','CANCELLATION_REQUESTED',"
                "'CANCELLATION_REVIEW','DISPUTED']))"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "CONFLICTED")

    def test_T29_gate_verifies_definitions_even_when_002_is_outside_the_plan(self):
        # Reviewer H1: a count is not a verification. Drift + 002 out of the plan must still refuse.
        conn = self.fresh()
        run_script(conn, PG_ONLY)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap")
            cur.execute(
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')"
            )
        conn.commit()
        without_002 = tuple(m for m in PGM.discover() if not m.migration_id.startswith("002"))
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn, migrations=without_002)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)  # counted, still not trusted

    def test_T30_legacy_ledger_shape_is_refused_not_crashed(self):
        conn = self.fresh()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE pg_schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT)"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "LEDGER_INCOMPATIBLE")
        self.assertIn("pg_schema_migrations", ctx.exception.message)

    def test_T31_failed_ledger_row_with_objects_present_is_drift(self):
        conn = self.fresh()
        PGM.apply(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pg_schema_migrations SET state=%s, evidence='hand repaired'"
                " WHERE migration_id=%s",
                (PGM.FAILED, PGM.pg_only_migration().migration_id),
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "CONFLICTED")

    def test_T32_manually_applied_002_is_reconciled_and_incomplete_repair_is_refused(self):
        conn = self.fresh()
        run_script(conn, PG_ONLY)  # operator already applied 002 by hand; ledger has no row
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 2)
        # Half a guard is not a guard: dropping one ISO prerequisite must fail closed by name.
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_check_in_iso")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        # The ledger already recorded 002 as successful, so the right refusal is "this database
        # conflicts with its own ledger", and it must still name the missing object.
        self.assertIn(ctx.exception.code, ("MIGRATION_INCOMPLETE", "CONFLICTED"))
        self.assertIn("ck_holds_check_in_iso", ctx.exception.message)

    def test_T33_approved_definitions_have_not_drifted_from_the_approved_file(self):
        # Independent oracle: build a REFERENCE database from the approved SQL in this test and
        # compare its catalogue output to the fixture. Nothing here calls the production matcher
        # or any production normaliser, so a defect in either cannot make this test pass.
        fixture = json.loads(PGM.APPROVED_FIXTURE.read_text())
        self.assertEqual(fixture["schema_version"], 3)
        self.assertEqual(fixture["file_sha256"], PGM._sha256(PGM.SCHEMA_DIR / PGM.PG_ONLY_FILE))
        self.assertEqual(
            [source["file"] for source in fixture["sources"]],
            [Path(path).name for path in PGM.PG_ONLY_FILES],
            "the fixture must pin every PostgreSQL-only file, not just the first one",
        )
        for source in fixture["sources"]:
            self.assertEqual(
                source["sha256"], PGM._sha256(PGM.SCHEMA_DIR / source["file"]), source["file"]
            )
        self.assertEqual(
            fixture["baseline_file_sha256"], PGM._sha256(PGM.SCHEMA_DIR / fixture["baseline_file"])
        )
        self.assertEqual(fixture["source_file"], PGM.PG_ONLY_FILE)
        self.assertTrue(fixture["server_version_num"].startswith("16"), fixture["server_version"])

        conn = self.fresh(guarded=True)
        reference = reference_definitions(conn)  # raw catalogue reads, no production code

        # The inventory is discovered from the catalogue, so "the list is complete" is not a
        # promise: adding a CHECK to holds/bookings without re-capturing fails HERE. That is
        # how ck_bookings_stay and the status-enum CHECKs were missed twice.
        def identity(item: dict[str, Any]) -> tuple[str, str, str, str]:
            return (item["kind"], item["table"], item["name"], item["definition"])

        self.assertEqual(
            sorted(identity(item) for item in fixture["objects"]),
            sorted(identity(item) for item in reference["objects"]),
            "the fixture no longer matches what the approved SQL produces: re-capture and review",
        )
        self.assertEqual(
            sum(1 for item in fixture["objects"] if item["kind"] == "exclusion"),
            2,
            "zero-night holds and bookings are excluded by NOTHING without both EXCLUDEs",
        )
        provenance = {item["name"]: item["created_by"] for item in fixture["objects"]}
        self.assertEqual(provenance["ck_bookings_stay"], "001")
        self.assertEqual(provenance["ex_holds_no_overlap"], "002")
        # Only the functions the PostgreSQL-only files create are pinned; the twin's own
        # villa_reject_mutation() belongs to 001 and the runner does not certify it.
        for name, definition in fixture["function"].items():
            self.assertEqual(definition, reference["function"].get(name), f"{name} drifted")
        self.assertIn("villa_iso_date", fixture["function"])
        self.assertIn("villa_occupying_states", fixture["function"])

        # Coverage of the production inventory too: every object the catalogue holds must be
        # one the runner actually checks. A count can be right while an object is missing.
        covered = {obj.name for obj in PGM.guard_objects()}
        self.assertIn("villa_iso_date", covered)
        for item in reference["objects"]:
            self.assertIn(item["name"], covered, f"{item['table']}.{item['name']} is not verified")

        # The production rung must agree with the oracle on a database built from the same file.
        for obj in PGM.guard_objects():
            with self.subTest(obj=obj.describe()):
                self.assertTrue(PGM.object_present(conn, obj))

    def test_T34_drifted_guards_are_refused(self):
        # Every shape the independent review used to get a GREEN run over an unprotected
        # database. Each one must be a refusal, not a success report.
        statuses_holds = (
            "'REQUESTED','AWAITING_SUPPLIER','CONFIRMED','EXTENSION_REQUESTED',"
            "'RELEASE_REQUESTED','EXPIRED_PENDING_VERIFICATION'"
        )
        drifts = {
            "extra conjunction": [
                "ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap",
                "ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap",
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                f" '[)') WITH &&) WHERE (((status = ANY (ARRAY[{statuses_holds}])) AND false))",
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')",
            ],
            "negated predicate": [
                "ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap",
                "ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap",
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                f" '[)') WITH &&) WHERE (NOT (status = ANY (ARRAY[{statuses_holds}])))",
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')",
            ],
            "empty range": [
                "ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap",
                "ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap",
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_in),"
                " '[)') WITH &&) WHERE (status = 'CONFIRMED')",
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_in),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')",
            ],
            "narrowed to one agency": [
                "ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap",
                "ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap",
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, agency_id WITH =, daterange(villa_iso_date(check_in),"
                " villa_iso_date(check_out), '[)') WITH &&) WHERE (status = 'CONFIRMED')",
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')",
            ],
            "lowercase status literals": [
                "ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap",
                "ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap",
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = ANY (ARRAY['confirmed','requested']))",
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = 'CANCELLED')",
            ],
            "replaced helper body": [
                "CREATE OR REPLACE FUNCTION villa_iso_date(value text) RETURNS date"
                " LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT '2026-01-01'::date $$",
            ],
            "check constraint neutered": [
                "ALTER TABLE holds DROP CONSTRAINT ck_holds_check_in_iso",
                "ALTER TABLE holds ADD CONSTRAINT ck_holds_check_in_iso CHECK (true)",
            ],
        }
        for label, statements in drifts.items():
            with self.subTest(drift=label), disposable() as dsn:
                apply_twin(dsn)
                conn = connect(dsn)
                try:
                    run_script(conn, PG_ONLY)
                    with conn.cursor() as cur:
                        for statement in statements:
                            cur.execute(statement)
                    conn.commit()
                    with self.assertRaises(PGM.MigrationRefused) as ctx:
                        PGM.apply(conn)
                    self.assertIn(
                        ctx.exception.code,
                        {
                            "CONFLICTED",
                            "MIGRATION_INCOMPLETE",
                            "CONSTRAINT_CONFLICT",
                            "FUNCTION_CONFLICT",
                        },
                        f"{label}: {ctx.exception.code}",
                    )
                finally:
                    conn.close()

    def test_T35_dry_run_previews_a_database_that_is_not_yet_guarded(self):
        conn = self.fresh()
        report = PGM.apply(conn, dry_run=True)
        self.assertEqual(report.failed, [])
        self.assertTrue(report.missing_guards, "a preview must say what is still missing")
        self.assertEqual(PGM.survey(conn)["range_exclusions"], 0)

    def test_T24_session_statement_timeout_is_restored(self):
        # F6: leaving statement_timeout at 0 silently disarms every later query on the connection.
        conn = self.fresh()
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '700ms'")
        conn.commit()
        PGM.apply(conn)
        self.assertEqual(one(conn, "SHOW statement_timeout"), "700ms")


class TestHistoricalUpgrade(MigrationCase):
    def test_T02_historical_schema_upgrades(self):
        conn = self.historical()
        self.assertFalse(table_exists(conn, "supplier_contacts"))
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertIn("020_supplier_contacts", report.applied)
        self.assertTrue(table_exists(conn, "supplier_contacts"))
        self.assertTrue(table_exists(conn, "evidence_supplier_attributions"))
        self.assertEqual(
            one(conn, "SELECT state FROM pg_schema_migrations WHERE migration_id='020_supplier_contacts'"),
            PGM.ALREADY_APPLIED,
        )

    def test_T08_existing_booking_data_is_preserved(self):
        conn = self.historical()
        seed_booking(conn)
        PGM.apply(conn)
        self.assertEqual(one(conn, "SELECT status FROM bookings WHERE booking_id='bk_pgm_1'"), "PAID_IN_FULL")
        self.assertEqual(one(conn, "SELECT count(*) FROM bookings"), 1)

    def test_T09_existing_supplier_data_is_preserved(self):
        conn = self.historical()
        now = "2026-09-21T00:00:00+00:00"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
                " VALUES ('sup_pgm', 'agency_default', 'PGM Supplier', 'ACTIVE', %s, %s)",
                (now, now),
            )
            cur.execute(
                "INSERT INTO villa_supplier_relationships (villa_supplier_relationship_id,"
                " agency_id, supplier_id, property_id, unit_id, status, created_at, updated_at)"
                " VALUES ('route_pgm', 'agency_default', 'sup_pgm', NULL, NULL, 'ACTIVE', %s, %s)",
                (now, now),
            )
        conn.commit()
        PGM.apply(conn)
        self.assertEqual(one(conn, "SELECT count(*) FROM suppliers WHERE supplier_id='sup_pgm'"), 1)
        self.assertEqual(
            one(conn, "SELECT count(*) FROM villa_supplier_relationships"
                      " WHERE villa_supplier_relationship_id='route_pgm'"),
            1,
        )

    def test_T10_tenant_isolation_is_preserved(self):
        conn = self.historical()
        seed_booking(conn)
        PGM.apply(conn)
        self.assertEqual(one(conn, "SELECT agency_id FROM bookings WHERE booking_id='bk_pgm_1'"), "agency_default")
        self.assertEqual(
            one(conn, "SELECT count(*) FROM bookings WHERE agency_id <> 'agency_default'"), 0
        )
        # The ledger is migration-scoped, never tenant-scoped: no agency column at all.
        self.assertEqual(
            one(
                conn,
                "SELECT count(*) FROM information_schema.columns WHERE table_name=%s"
                " AND column_name IN ('agency_id','tenant_id')",
                (PGM.LEDGER_TABLE,),
            ),
            0,
        )

    def test_T14_legacy_evidence_stays_untrusted(self):
        conn = self.historical()
        now = "2026-09-21T00:00:00+00:00"
        seed_booking(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id, source_type,"
                " evidence_level, result, can_quote, can_hold, can_confirm, source_ref, captured_at,"
                " valid_until, created_at) VALUES ('ev_legacy', 'agency_default', 'unit_pgm',"
                " 'manager_written', 'B', 'AVAILABLE', 1, 1, 1, 'letter', %s, '2026-12-31', %s)",
                (now, now),
            )
        conn.commit()
        PGM.apply(conn)
        self.assertEqual(one(conn, "SELECT count(*) FROM evidence_supplier_attributions"), 0)
        self.assertTrue(
            bool(one(conn, "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
                           " WHERE table_name='availability_evidence'"
                           " AND column_name='callback_verified_contact')"))
        )
        self.assertIsNone(
            one(conn, "SELECT callback_verified_contact FROM availability_evidence"
                      " WHERE evidence_id='ev_legacy'")
        )

    def test_T11_append_only_guards_survive(self):
        conn = self.historical()
        seed_booking(conn)
        PGM.apply(conn)
        now = "2026-09-21T00:00:00+00:00"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO availability_checks (check_id, agency_id, unit_id, check_in,"
                " check_out, requested_by, requested_role, requested_at, notes)"
                " VALUES ('chk_pgm', 'agency_default', 'unit_pgm', '2026-10-01', '2026-10-03',"
                " 'rm-1', 'reservation_manager', %s, 'pgm gate')",
                (now,),
            )
        conn.commit()
        with self.assertRaises(Exception):
            with conn.cursor() as cur:
                cur.execute("UPDATE availability_checks SET notes='changed' WHERE check_id='chk_pgm'")
        conn.rollback()
        self.assertEqual(one(conn, "SELECT notes FROM availability_checks WHERE check_id='chk_pgm'"), "pgm gate")


class TestRefusals(MigrationCase):
    def test_T04_unknown_schema_is_rejected(self):
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE something_else (id TEXT)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "UNKNOWN_SCHEMA")

    def test_T05_changed_checksum_is_rejected(self):
        conn = self.fresh()
        PGM.apply(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pg_schema_migrations SET source_sha256='0'*64"
                " WHERE migration_id='003_policy_catalogue_and_deny_registry'"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "CONFLICTED")
        self.assertIn("003_policy", ctx.exception.message)

    def test_T15_inconsistent_ledger_fails_closed(self):
        conn = self.historical()
        PGM.apply(conn)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE supplier_contacts CASCADE")
        conn.commit()
        # The ledger still claims 020 ran; the objects are gone. Neither state is trusted.
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertIn(ctx.exception.code, {"CONFLICTED", "MIGRATION_FAILED"})

    def test_T16_requires_an_explicit_target(self):
        import subprocess
        import sys

        script = REPO_ROOT / "scripts" / "pg_migrate.py"
        env = {k: v for k, v in os.environ.items() if k not in {"VILLA_PG_DSN", "VILLA_PG_LOCAL_DSN"}}
        result = subprocess.run(
            [sys.executable, str(script), "--inspect"], capture_output=True, text=True, env=env
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("no target", result.stderr)
        sqlite_target = subprocess.run(
            [sys.executable, str(script), "--inspect", "--dsn", "file:data/villa.db"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(sqlite_target.returncode, 2)
        self.assertIn("REFUSED", sqlite_target.stderr)
        self.assertIn("SQLite", sqlite_target.stderr)

    def test_T07_failure_rolls_back_and_records_nothing_but_failed(self):
        conn = self.fresh()
        PGM.apply(conn)
        broken = PGM.Migration(
            migration_id="999_deliberately_broken",
            source=Path("999_deliberately_broken.sql"),
            sha256="f" * 64,
            classification=OBJECT,
            objects=(),
            ddl=("CREATE TABLE pgm_should_not_exist (id TEXT)", "CREATE TABLE pgm_should_not_exist (id TEXT)"),
            data=(),
        )
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn, migrations=tuple(PGM.discover()) + (broken,))
        self.assertEqual(ctx.exception.code, "MIGRATION_FAILED")
        self.assertFalse(table_exists(conn, "pgm_should_not_exist"))
        self.assertEqual(
            one(conn, "SELECT state FROM pg_schema_migrations WHERE migration_id='999_deliberately_broken'"),
            PGM.FAILED,
        )
        # And the refusal is stable: a second run still sees the failure, not success.
        with self.assertRaises(PGM.MigrationRefused):
            PGM.apply(conn, migrations=tuple(PGM.discover()) + (broken,))


class TestLockingAndBoundaries(MigrationCase):
    def test_T06_concurrent_runners_serialise(self):
        conn = self.fresh()
        PGM.apply(conn)
        holder = connect(self.dsn)
        self.addCleanup(holder.close)
        PGM.acquire_lock(holder, timeout_seconds=5)
        late = connect(self.dsn)
        self.addCleanup(late.close)
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.acquire_lock(late, timeout_seconds=1)
        self.assertEqual(ctx.exception.code, "LOCK_TIMEOUT")
        PGM.release_lock(holder)
        PGM.acquire_lock(late, timeout_seconds=5)  # the lock is genuinely free again
        PGM.release_lock(late)

    def test_T06b_two_apply_calls_leave_one_consistent_ledger(self):
        self.fresh()
        results: list[Any] = []

        def run() -> None:
            try:
                results.append(migrate(self.dsn))
            except Exception as error:  # pragma: no cover - asserted below
                results.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertNotIsInstance(result, Exception, f"concurrent runner failed: {result!r}")
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        self.assertEqual(
            one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE migration_id <> %s",
                (PGM.BASELINE_ID,)),
            len(PGM.discover()),
        )
        self.assertEqual(
            one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE state IN ('FAILED','CONFLICTED')"),
            0,
        )

    def test_T06c_concurrent_first_migration_of_a_new_database(self):
        self.fresh()  # twin applied, ledger table does not exist yet
        results: list[Any] = []

        def run() -> None:
            try:
                results.append(migrate(self.dsn))
            except Exception as error:  # pragma: no cover - asserted below
                results.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for result in results:
            self.assertNotIsInstance(result, Exception, f"concurrent first run failed: {result!r}")
        conn = connect(self.dsn)
        self.addCleanup(conn.close)
        self.assertEqual(
            one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE migration_id=%s", (PGM.BASELINE_ID,)), 1
        )
        self.assertEqual(
            one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE migration_id <> %s", (PGM.BASELINE_ID,)),
            len(PGM.discover()),
        )

    def test_T17_no_operational_database_is_touched(self):
        production = REPO_ROOT / "data" / "villa.db"
        before = (production.stat().st_mtime_ns, production.stat().st_size) if production.exists() else None
        conn = self.fresh()
        PGM.apply(conn)
        if before is not None:
            after = (production.stat().st_mtime_ns, production.stat().st_size)
            self.assertEqual(before, after, "the runner touched data/villa.db")
        self.assertNotIn("VILLA_DB", os.environ.get("VILLA_PG_LOCAL_DSN", ""))
        self.assertTrue(_local_problem("postgresql://probe@192.0.2.10:5432/x"))
        self.assertEqual(_local_problem("postgresql://u@127.0.0.1:5432/x"), "")

    def test_T20_fresh_and_migrated_schemas_agree(self):
        fresh_conn = self.fresh()
        PGM.apply(fresh_conn)
        fresh_tables = self._tables(fresh_conn)

        with disposable() as other:
            apply_twin(other, HISTORICAL)
            conn = connect(other)
            try:
                PGM.apply(conn)
                migrated = self._tables(conn)
            finally:
                conn.close()
        self.assertEqual(migrated, fresh_tables)

    def _tables(self, conn: Any) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            return {row[0] for row in cur.fetchall()}


class TestBlockerRecoveryR6(MigrationCase):
    """PG-R6-T01..T14 — the confirmed blocker and the safety defects around it.

    Every test names the defect it pins. The oracle is the catalogue, read by this file, never
    by the production matcher.
    """

    def _cli(self, *args: str) -> Any:
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "pg_migrate.py"), *args],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k not in {"VILLA_PG_DSN"}},
        )

    def test_T36_literal_containing_the_cast_noise_cannot_match(self):
        # PG-R6-T01: the confirmed exploit, verbatim. 'REQ::textUESTED' must not read as
        # 'REQUESTED' — the guard then matches no row and protects nothing.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ex_holds_no_overlap")
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap")
            cur.execute(
                "ALTER TABLE holds ADD CONSTRAINT ex_holds_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = ANY (ARRAY['REQ::textUESTED','AWAITING_SUPPLIER',"
                "'CONFIRMED','EXTENSION_REQUESTED','RELEASE_REQUESTED',"
                "'EXPIRED_PENDING_VERIFICATION']))"
            )
            cur.execute(
                "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist"
                " (unit_id WITH =, daterange(villa_iso_date(check_in), villa_iso_date(check_out),"
                " '[)') WITH &&) WHERE (status = ANY (ARRAY['REQ::textUESTED','BALANCE_DUE',"
                "'BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY','CANCELLATION_REQUESTED',"
                "'CANCELLATION_REVIEW','DISPUTED']))"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        # Either the plan notices a name it cannot trust (CONFLICTED) or the gate refuses after
        # the loop (MIGRATION_INCOMPLETE). Both stop before anything is written.
        self.assertIn(ctx.exception.code, {"CONFLICTED", "MIGRATION_INCOMPLETE"})
        self.assertTrue(PGM.verify_guards(conn))

    def test_T37_check_true_is_rejected_by_apply(self):
        # PG-R6-T02
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_stay")
            cur.execute("ALTER TABLE holds ADD CONSTRAINT ck_holds_stay CHECK (true)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("ck_holds_stay", ctx.exception.message)

    def test_T38_inspect_reports_check_true_as_conflicted(self):
        # PG-R6-T03: a preview that says READY over CHECK (true) is the same lie as apply().
        conn = self.fresh(guarded=True)
        PGM.apply(conn)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_check_in_iso")
            cur.execute("ALTER TABLE holds ADD CONSTRAINT ck_holds_check_in_iso CHECK (true)")
        conn.commit()
        state = PGM.database_state(conn, PGM.discover())
        self.assertEqual(state["status"], PGM.CONFLICTED)
        self.assertTrue(
            any("ck_holds_check_in_iso" in item for item in state["missing_guards"]),
            state["missing_guards"],
        )
        cli = self._cli("--inspect", "--dsn", self.dsn)
        self.assertEqual(cli.returncode, 2, cli.stdout + cli.stderr)
        self.assertIn("CONFLICTED", cli.stdout + cli.stderr)

    def test_T39_inspect_writes_nothing(self):
        # PG-R6-T04: no ledger creation, no reconciliation row, no catalogue change.
        conn = self.fresh()
        before = one(conn, "SELECT count(*) FROM pg_class")
        ledger_before = one(conn, "SELECT to_regclass('public.pg_schema_migrations')")
        cli = self._cli("--inspect", "--dsn", self.dsn)
        self.assertIn(cli.returncode, (0, 2), cli.stderr)
        self.assertEqual(one(conn, "SELECT count(*) FROM pg_class"), before)
        self.assertIsNone(ledger_before)
        self.assertIsNone(one(conn, "SELECT to_regclass('public.pg_schema_migrations')"))

    def test_T40_failed_guard_check_writes_no_reconciliation_row(self):
        # PG-R6-T05: the pre-flight refusal must happen before any success is recorded.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_stay")
            cur.execute("ALTER TABLE holds ADD CONSTRAINT ck_holds_stay CHECK (true)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused):
            PGM.apply(conn)
        self.assertIsNone(
            one(conn, "SELECT to_regclass('public.pg_schema_migrations')"),
            "a refused run must not leave a ledger claiming anything succeeded",
        )

    def test_T41_function_overload_is_a_structured_refusal(self):
        # PG-R6-T06
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION villa_iso_date(value integer) RETURNS date LANGUAGE sql"
                " IMMUTABLE AS $$ SELECT '2026-01-01'::date $$"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "FUNCTION_AMBIGUOUS")

    def test_T42_restricted_search_path_does_not_corrupt_or_false_pass(self):
        # PG-R6-T07: the runner pins search_path inside its own transaction, so a caller-level
        # pg_catalog-only setting must neither hide the ledger nor fake a success.
        conn = self.fresh()
        with conn.cursor() as cur:
            cur.execute("SET search_path = pg_catalog")
        conn.commit()
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertEqual(PGM.verify_guards(conn), [])
        self.assertIsNotNone(one(conn, "SELECT to_regclass('public.pg_schema_migrations')"))

    def test_T43_missing_ck_holds_stay_is_rejected(self):
        # PG-R6-T08
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_stay")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("ck_holds_stay", ctx.exception.message)

    def test_T44_changed_ck_holds_stay_is_rejected(self):
        # PG-R6-T09: a weaker but plausible constraint (<= instead of <) must not pass.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT ck_holds_stay")
            cur.execute("ALTER TABLE holds ADD CONSTRAINT ck_holds_stay CHECK (check_in <= check_out)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("ck_holds_stay", ctx.exception.message)

    def test_T45_approved_guards_still_pass(self):
        # PG-R6-T10: the strictness must not refuse a correct database.
        conn = self.fresh()
        report = PGM.apply(conn)
        self.assertEqual(report.failed, [])
        self.assertEqual(PGM.verify_guards(conn), [])
        self.assertEqual(PGM.database_state(conn, PGM.discover())["status"], PGM.READY)

    def test_T46_keyword_value_dsn_is_accepted_for_a_loopback_target(self):
        # PG-R6-T11
        from pg_migrate import resolve_target

        disposable_dsn = self.dsn
        from psycopg.conninfo import conninfo_to_dict

        params = conninfo_to_dict(disposable_dsn)
        keyword = " ".join(f"{key}={value}" for key, value in params.items() if value)
        resolved = resolve_target(keyword)
        # resolve_target's contract is that the target it accepts is the target it names:
        # the host it was handed must come back unchanged. Pinning "127.0.0.1" asserted the
        # CI disposable DSN's spelling instead, so it failed the first time this test ran
        # against a server addressed as "localhost" (run 44968069626).
        resolved_params = conninfo_to_dict(resolved)
        self.assertEqual(resolved_params["host"], params["host"])
        self.assertEqual(resolved_params["port"], params["port"])
        self.assertEqual(resolved_params["dbname"], params["dbname"])
        conn = self.fresh()
        PGM.apply(conn)
        cli = self._cli("--inspect", "--dsn", keyword)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertIn("READY", cli.stdout)

    def test_T47_remote_and_unverified_targets_are_refused(self):
        # PG-R6-T12
        from pg_migrate import resolve_target

        for target in (
            "host=db.example.com dbname=production",
            "postgresql://someone@203.0.113.9:5432/villa",
            "service=villa dbname=production",
            "host=/var/run/postgresql dbname=villa",
        ):
            with self.subTest(target=target):
                with self.assertRaises(SystemExit) as ctx:
                    resolve_target(target)
                self.assertEqual(ctx.exception.code, 2)

    def test_T48_pending_migration_is_not_called_corrupt(self):
        # PG-R6-T13
        conn = self.fresh()
        state = PGM.database_state(conn, PGM.discover())
        self.assertEqual(state["status"], PGM.PENDING)
        self.assertEqual(state["conflicts"], [])
        self.assertTrue(
            any(row["migration"].startswith("002") for row in state["steps"]),
            state["steps"],
        )

    def test_T51_missing_ck_bookings_stay_is_rejected(self):
        # BLOCKER-1 of review pass #6: the same class as ck_holds_stay. Without it a zero-night
        # booking falls outside the exclusion range and confirms nothing.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ck_bookings_stay")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("ck_bookings_stay", ctx.exception.message)

    def test_T52_changed_ck_bookings_stay_is_rejected(self):
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT ck_bookings_stay")
            cur.execute("ALTER TABLE bookings ADD CONSTRAINT ck_bookings_stay CHECK (check_in <= check_out)")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("ck_bookings_stay", ctx.exception.message)

    def test_T53_dropped_not_null_makes_the_guard_unsound(self):
        # HIGH-2: an EXCLUDE treats NULL as non-conflicting, so dropping NOT NULL on the scoped
        # columns disables the guard while every constraint definition still reads as approved.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds ALTER COLUMN status DROP NOT NULL")
        conn.commit()
        missing = PGM.verify_guards(conn)
        self.assertIn("not-null:holds.status", missing)
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")

    def test_T54_password_is_never_echoed(self):
        # HIGH-1: mask_dsn only knew the URL form, so `password=...` went to logs verbatim.
        self.assertEqual(
            PGM.mask_dsn("postgresql://villa:s3cret@127.0.0.1:55432/villa"),
            "postgresql://villa:***@127.0.0.1:55432/villa",
        )
        masked = PGM.mask_dsn("host=127.0.0.1 port=55432 dbname=villa user=villa password=s3cret")
        self.assertNotIn("s3cret", masked)
        self.assertIn("password=***", masked)

    def test_T55_malformed_fixture_is_refused_not_crashed(self):
        # LOW-7: a JSON file that is merely valid must not surface as AttributeError.
        import tempfile

        conn = self.fresh(guarded=True)
        payload = json.loads(PGM.APPROVED_FIXTURE.read_text())
        payload["objects"][0]["definition"] = 42
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "approved.json"
            fake.write_text(json.dumps(payload))
            original = PGM.APPROVED_FIXTURE
            PGM.APPROVED_FIXTURE = fake
            try:
                with self.assertRaises(PGM.MigrationRefused) as ctx:
                    PGM.verify_guards(conn)
                self.assertEqual(ctx.exception.code, "APPROVED_FILE_UNREADABLE")
            finally:
                PGM.APPROVED_FIXTURE = original

    def test_T56_target_without_an_explicit_port_is_refused(self):
        # MEDIUM-5: defaulting to 5432 is how a statement reaches the wrong cluster.
        from pg_migrate import resolve_target

        with self.assertRaises(SystemExit) as ctx:
            resolve_target("host=127.0.0.1 dbname=villa")
        self.assertEqual(ctx.exception.code, 2)

    def test_T57_empty_database_is_unknown_not_ready(self):
        # LOW-9: "no baseline" must not be classified as anything optimistic.
        with disposable() as other:
            conn = connect(other)
            try:
                state = PGM.database_state(conn, PGM.discover())
            finally:
                conn.close()
        self.assertEqual(state["status"], PGM.UNKNOWN)

    def test_T58_no_success_row_when_the_guard_fails_inside_the_transaction(self):
        # MEDIUM-2: pre-flight can pass and the guard still fail inside the loop's transaction.
        # The ledger row and the DDL share that transaction, so neither survives.
        conn = self.fresh()
        real = PGM.verify_guards
        calls = {"n": 0}

        def flaky(current: Any, credited: Any = None) -> list[str]:
            calls["n"] += 1
            # On a database with no guard yet the pre-flight has nothing to verify, so the first
            # call is the in-loop gate — the one that shares a transaction with the DDL.
            return ["check:holds.ck_holds_stay"]

        PGM.verify_guards = flaky
        try:
            with self.assertRaises(PGM.MigrationRefused) as ctx:
                PGM.apply(conn)
        finally:
            PGM.verify_guards = real
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("still not the approved one", ctx.exception.message)
        self.assertEqual(calls["n"], 1, "the in-loop gate must run before the ledger row is written")
        recorded = one(
            conn,
            "SELECT count(*) FROM public.pg_schema_migrations WHERE migration_id LIKE '002%%'"
            " AND state IN ('ALREADY_APPLIED', 'BASELINE_RECONCILED')",
        )
        self.assertEqual(recorded, 0)
        self.assertEqual(
            one(
                conn,
                "SELECT state FROM public.pg_schema_migrations WHERE migration_id LIKE '002%%'",
            ),
            PGM.FAILED,
        )

    def test_T63_a_trigger_on_holds_is_outside_the_approved_schema(self):
        # HIGH-1 of review pass #8: the approved files put no trigger on holds/bookings, so a
        # trigger there is drift. Presence-only verification cannot see it, and a BEFORE INSERT
        # trigger can rewrite unit_id or drop the row while every constraint still reads right.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION villa_r63() RETURNS trigger AS $$ BEGIN RETURN NULL; END;"
                " $$ LANGUAGE plpgsql"
            )
            cur.execute(
                "CREATE TRIGGER trg_r63 BEFORE INSERT ON holds"
                " FOR EACH ROW EXECUTE FUNCTION villa_r63()"
            )
        conn.commit()
        missing = PGM.verify_guards(conn)
        self.assertIn("unexpected:trigger:holds.trg_r63", missing)
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")

    def test_T64_an_extra_constraint_on_bookings_is_outside_the_approved_schema(self):
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bookings ADD CONSTRAINT ck_r64 CHECK (true)")
        conn.commit()
        self.assertIn(
            "unexpected:check:bookings.ck_r64", PGM.verify_guards(conn)
        )

    def test_T59_dropped_status_enum_is_rejected(self):
        # BLOCKER-2 of review pass #7: the EXCLUDE predicate compares status with the closed
        # set. Drop the enum CHECK and every row still "conflicts" as far as the constraint is
        # concerned, yet nothing keeps the status inside the set the predicate was written for.
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE holds DROP CONSTRAINT holds_status_check")
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("holds_status_check", ctx.exception.message)

    def test_T60_widened_status_enum_is_rejected(self):
        conn = self.fresh(guarded=True)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE bookings DROP CONSTRAINT bookings_status_check")
            cur.execute(
                "ALTER TABLE bookings ADD CONSTRAINT bookings_status_check"
                " CHECK (status <> '')"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as ctx:
            PGM.apply(conn)
        self.assertEqual(ctx.exception.code, "MIGRATION_INCOMPLETE")
        self.assertIn("bookings_status_check", ctx.exception.message)

    def test_T61_derived_inventory_covers_every_guard_object(self):
        # The class, not the instance: every validated CHECK/EXCLUDE on holds/bookings that the
        # approved SQL creates must be in guard_objects(). Nothing is hand-listed any more.
        conn = self.fresh(guarded=True)
        reference = reference_definitions(conn)
        verified = {(obj.kind, obj.table, obj.name) for obj in PGM.guard_objects()}
        for item in reference["objects"]:
            self.assertIn(
                (item["kind"], item["table"], item["name"]),
                verified,
                f"{item['table']}.{item['name']} is part of the approved guard but unverified",
            )

    def test_T62_target_description_never_carries_the_password(self):
        # HIGH-1 of review pass #7: the CLI must not echo anything it did not parse itself.
        import pg_migrate

        params = conninfo_to_dict("postgresql://villa:s3cret@127.0.0.1:55432/villa")
        described = pg_migrate.describe_target(params)
        self.assertEqual(described, "villa@127.0.0.1:55432/villa")
        self.assertNotIn("s3cret", described)

    def test_T50_definitions_captured_on_another_major_version_are_refused(self):
        # A fixture from a different major version is not "close enough": deparse changes.
        import tempfile

        conn = self.fresh(guarded=True)
        payload = json.loads(PGM.APPROVED_FIXTURE.read_text())
        payload["server_version_num"] = "150004"
        payload["server_version"] = "15.4"
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "approved.json"
            fake.write_text(json.dumps(payload))
            original = PGM.APPROVED_FIXTURE
            PGM.APPROVED_FIXTURE = fake
            try:
                with self.assertRaises(PGM.MigrationRefused) as ctx:
                    PGM.verify_guards(conn)
                self.assertEqual(ctx.exception.code, "APPROVED_DEFINITION_VERSION_MISMATCH")
            finally:
                PGM.APPROVED_FIXTURE = original

    def test_T49_ledger_is_consistent_after_a_failure(self):
        # PG-R6-T14
        conn = self.fresh()
        PGM.apply(conn)
        broken = PGM.Migration(
            migration_id="999_r6_broken",
            source=Path("999_r6_broken.sql"),
            sha256="b" * 64,
            classification=OBJECT,
            objects=(),
            ddl=("CREATE TABLE r6_should_not_exist (id TEXT)", "CREATE TABLE r6_should_not_exist (id TEXT)"),
            data=(),
        )
        with self.assertRaises(PGM.MigrationRefused):
            PGM.apply(conn, migrations=tuple(PGM.discover()) + (broken,))
        self.assertEqual(
            one(conn, "SELECT state FROM public.pg_schema_migrations WHERE migration_id=%s",
                (broken.migration_id,)),
            PGM.FAILED,
        )
        self.assertEqual(one(conn, "SELECT count(*) FROM public.pg_schema_migrations"
                                   " WHERE migration_id=%s AND state IN ('ALREADY_APPLIED',"
                                   " 'BASELINE_RECONCILED')", (broken.migration_id,)), 0)
        self.assertIsNone(one(conn, "SELECT to_regclass('public.r6_should_not_exist')"))


class TestFunctionProvenance(MigrationCase):
    """A guard function is guard material: per-migration credit has to cover its body.

    ``_provenance()`` used to map only the fixture's constraint and trigger entries, so
    ``verify_guards(conn, "021")`` — the gate ``apply()`` runs inside the migration transaction —
    never opened a single function. A body could be replaced, the run would still write a
    success row, and only the post-loop full inventory would complain, after the commit.
    """

    LOCK = "villa_lock_occupied_nights"
    ISO = "villa_iso_date"

    def signature(self, conn: Any, name: str) -> tuple[str, str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid)"
                " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE p.proname = %s AND n.nspname = 'public'",
                (name,),
            )
            args, result = cur.fetchone()
        return str(args), str(result)

    def live_definition(self, conn: Any, name: str) -> str:
        return str(
            one(
                conn,
                "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace WHERE p.proname = %s AND n.nspname = 'public'",
                (name,),
            )
        )

    def replace_body(self, conn: Any, name: str, body: str) -> None:
        """Same name, same signature, different implementation — a body-only mutation."""
        args, result = self.signature(conn, name)
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE OR REPLACE FUNCTION {name}({args}) RETURNS {result}"
                f" LANGUAGE sql STABLE AS $mutation$ {body} $mutation$"
            )
        conn.commit()

    def approved_declaration(self, name: str) -> str:
        """The declaration for ``name`` copied verbatim out of the approved file."""
        for path in PG_ONLY_FILES:
            for statement in SL.split_statements(path.read_text(encoding="utf-8")):
                if f"FUNCTION {name}(" in statement:
                    return SL._strip_leading_comments(statement)
        raise AssertionError(f"{name} is not declared in the approved files")

    def test_T003B_01_correct_002_function_definitions_pass(self):
        conn = self.migrated()
        self.assertEqual(PGM.verify_guards(conn, "002"), [])
        self.assertEqual(
            self.live_definition(conn, self.ISO).strip(),
            PGM.approved_guards()["function"][self.ISO].strip(),
        )

    def test_T003B_02_correct_021_function_definitions_pass(self):
        conn = self.migrated()
        self.assertEqual(PGM.verify_guards(conn, "021"), [])
        credited = {name for name in PGM.approved_guards()["function"] if name != self.ISO}
        self.assertEqual(len(credited), 6)
        for name in sorted(credited):
            with self.subTest(function=name):
                self.assertEqual(
                    self.live_definition(conn, name).strip(),
                    PGM.approved_guards()["function"][name].strip(),
                )

    def test_T003B_03_mutation_proof_an_altered_body_is_refused_and_restoring_clears_it(self):
        # 1. the unmodified database passes
        conn = self.migrated()
        self.assertEqual(PGM.verify_guards(conn, "021"), [])
        # 2. same name, same signature, deliberately different body
        self.replace_body(conn, self.LOCK, "SELECT NULL::void")
        self.assertNotEqual(
            self.live_definition(conn, self.LOCK).strip(),
            PGM.approved_guards()["function"][self.LOCK].strip(),
        )
        # 3. the per-migration path now sees it — this is the check that used to be empty
        self.assertIn(f"function:{self.LOCK}", PGM.verify_guards(conn, "021"))
        self.assertIn(f"function:{self.LOCK}", PGM.verify_guards(conn))
        self.assertEqual(PGM.verify_guards(conn, "002"), [], "021's function is not 002's")
        step = [
            s for s in PGM.plan(conn, PGM.discover())
            if s.migration.migration_id.startswith("021")
        ][0]
        self.assertEqual(step.planned_state, PGM.CONFLICTED)
        self.assertIn(self.LOCK, step.reason)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pg_schema_migrations WHERE migration_id LIKE '021%'")
        conn.commit()
        step = [
            s for s in PGM.plan(conn, PGM.discover())
            if s.migration.migration_id.startswith("021")
        ][0]
        self.assertEqual(step.planned_state, PGM.CONFLICTED)
        self.assertIn("not the approved definition", step.reason)
        # 4. restoring the approved text returns verification to clean
        with conn.cursor() as cur:
            cur.execute(_pgsql.SQL(self.approved_declaration(self.LOCK)))
        conn.commit()
        self.assertEqual(PGM.verify_guards(conn, "021"), [])
        self.assertEqual(PGM.verify_guards(conn), [])

    def test_T003B_04_a_missing_guard_function_is_reported_by_its_own_credit(self):
        conn = self.fresh()  # twin applied, neither PostgreSQL-only file run
        missing = PGM.verify_guards(conn, "002")
        self.assertIn(f"function:{self.ISO}", missing)
        self.assertIn("check:holds.ck_holds_check_in_iso", missing)
        missing_021 = PGM.verify_guards(conn, "021")
        self.assertIn(f"function:{self.LOCK}", missing_021)
        self.assertNotIn(f"function:{self.ISO}", missing_021)

    def test_T003B_05_an_unexpected_overload_is_an_ambiguous_refusal(self):
        conn = self.migrated()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION villa_iso_date(value text, fmt text) RETURNS date"
                " LANGUAGE sql AS $overload$ SELECT NULL::date $overload$"
            )
        conn.commit()
        with self.assertRaises(PGM.MigrationRefused) as caught:
            PGM.verify_guards(conn, "002")
        self.assertEqual(caught.exception.code, "FUNCTION_AMBIGUOUS")

    def test_T003B_06_a_same_named_no_op_is_rejected(self):
        conn = self.migrated()
        self.replace_body(conn, self.ISO, "SELECT NULL::date")
        self.assertIn(f"function:{self.ISO}", PGM.verify_guards(conn, "002"))
        self.assertEqual(PGM.verify_guards(conn, "021"), [])

    def test_T003B_07_every_fixture_function_is_credited_to_the_file_that_declares_it(self):
        provenance = PGM._provenance()
        functions = PGM.approved_guards()["function"]
        self.assertEqual(len(functions), 7)
        for name in sorted(functions):
            with self.subTest(function=name):
                self.assertEqual(provenance[f"function:{name}"], self.declared_credit(name))
        self.assertEqual({provenance[f"function:{name}"] for name in functions}, {"002", "021"})
        # fail closed: a fixture function no approved file declares cannot be credited
        from unittest import mock

        payload = dict(PGM.approved_guards())
        payload["function"] = {**functions, "villa_orphan_guard": "CREATE FUNCTION ..."}
        with mock.patch.object(PGM, "approved_guards", return_value=payload):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM._function_credits()
        self.assertEqual(caught.exception.code, "APPROVED_FILE_UNPROVENANCED")

    def test_T003B_08_021_pending_while_002_is_correct_is_a_valid_intermediate_state(self):
        conn = self.fresh()
        run_script(conn, PG_ONLY_FILES[0])  # 002 only, the way an upgrade stops half-way
        steps = {s.migration.migration_id.split("_", 1)[0]: s.planned_state
                 for s in PGM.plan(conn, PGM.discover()) if s.migration.classification == PGM.POSTGRESQL_ONLY}
        self.assertEqual(steps["002"], PGM.BASELINE_RECONCILED)
        self.assertEqual(steps["021"], PGM.PENDING)
        self.assertEqual(PGM.verify_guards(conn, "002"), [])
        self.assertEqual(len(PGM.verify_guards(conn, "021")), 10)
        report = PGM.apply(conn)
        self.assertEqual((report.failed, report.missing_guards), ([], []))
        self.assertEqual(PGM.verify_guards(conn), [])
        self.assertEqual(PGM.database_state(conn, PGM.discover())["status"], PGM.READY)

    def test_T003B_09_a_drifted_002_function_is_refused_by_its_own_migration(self):
        conn = self.migrated()
        self.replace_body(conn, self.ISO, "SELECT '1970-01-01'::date")
        self.assertIn(f"function:{self.ISO}", PGM.verify_guards(conn, "002"))
        self.assertNotIn(f"function:{self.ISO}", PGM.verify_guards(conn, "021"))

    def test_T003B_10_the_full_inventory_still_passes_on_an_untouched_database(self):
        conn = self.migrated()
        self.assertEqual(PGM.verify_guards(conn), [])
        self.assertEqual(PGM._unexpected_objects(conn), [])
        state = PGM.database_state(conn, PGM.discover())
        self.assertEqual((state["status"], state["missing_guards"]), (PGM.READY, []))

    def test_T003B_11_a_wrong_function_never_yields_a_success_ledger_row(self):
        conn = self.migrated()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tgname, tgrelid::regclass::text FROM pg_trigger"
                " WHERE NOT tgisinternal AND tgname LIKE 'trg%occupancy%'"
            )
            for name, table in cur.fetchall():
                cur.execute(f'DROP TRIGGER "{name}" ON {table}')
            cur.execute(
                "DELETE FROM pg_schema_migrations WHERE migration_id LIKE '002%'"
                " OR migration_id LIKE '021%'"
            )
        conn.commit()
        self.replace_body(conn, self.ISO, "SELECT '1970-01-01'::date")
        with self.assertRaises(PGM.MigrationRefused) as caught:
            PGM.apply(conn)
        self.assertIn(caught.exception.code, ("CONFLICTED", "MIGRATION_INCOMPLETE"))
        self.assertEqual(
            one(
                conn,
                "SELECT count(*) FROM pg_schema_migrations WHERE state IN"
                " ('ALREADY_APPLIED','BASELINE_RECONCILED') AND (migration_id LIKE '002%'"
                " OR migration_id LIKE '021%')",
            ),
            0,
        )
        # and a database that is actually put back in order still migrates
        with conn.cursor() as cur:
            cur.execute(_pgsql.SQL(self.approved_declaration(self.ISO)))
        conn.commit()
        run_script(conn, PG_ONLY_FILES[1])  # the dropped 021 triggers, from the approved file
        conn.close()
        report = migrate(self.dsn)
        self.assertEqual((report.failed, report.missing_guards), ([], []))
        again = connect(self.dsn)
        self.addCleanup(again.close)
        self.assertEqual(PGM.verify_guards(again), [])

    def test_T003B_12_the_approved_path_is_unchanged_and_idempotent(self):
        conn = self.migrated()
        first = one(conn, "SELECT count(*) FROM pg_schema_migrations")
        report = PGM.apply(conn)
        self.assertEqual((report.applied, report.reconciled, report.failed), ([], [], []))
        self.assertEqual(one(conn, "SELECT count(*) FROM pg_schema_migrations"), first)
        self.assertEqual(PGM.verify_guards(conn, "002"), [])
        self.assertEqual(PGM.verify_guards(conn, "021"), [])
        self.assertEqual(
            one(conn, "SELECT count(*) FROM pg_schema_migrations WHERE"
                      " migration_id LIKE '021%' AND state='ALREADY_APPLIED'"),
            1,
        )


class TestFixtureCompleteness(MigrationCase):
    """The fixture and the approved SQL must name the same functions, in both directions.

    003B made the credit derivable, so a *fixture* function could no longer be skipped. The
    other direction stayed open: a function the SQL declares but the fixture omits was never in
    the guard inventory at all, so a missing, replaced or drifted body verified clean and the
    plan still said ALREADY_APPLIED. The capture query made that permanent for any function not
    named ``villa_*``, because it selected by name shape instead of by declaration.
    """

    def captured_payload(self) -> dict:
        return PGM.approved_guards()

    def test_T003C_01_the_fixture_and_the_declarations_name_the_same_functions(self):
        declared = PGM.declared_functions()
        fixture = PGM.approved_guards()["function"]
        self.assertEqual(set(declared), set(fixture))
        self.assertEqual(len(declared), 7)
        self.assertEqual(declared, PGM._function_credits())
        for name, credit in sorted(declared.items()):
            with self.subTest(function=name):
                self.assertEqual(credit, self.declared_credit(name))

    def test_T003C_02_a_declared_function_missing_from_the_fixture_is_refused(self):
        payload = dict(self.captured_payload())
        payload["function"] = {k: v for k, v in payload["function"].items() if k != "villa_iso_date"}
        with mock.patch.object(PGM, "approved_guards", return_value=payload):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM._function_credits()
        self.assertEqual(caught.exception.code, "APPROVED_FIXTURE_INCOMPLETE")
        self.assertIn("villa_iso_date", caught.exception.message)

    def test_T003C_03_a_declaration_the_fixture_cannot_see_is_refused_by_name(self):
        declared = dict(PGM.declared_functions())
        declared["villa_undeclared_helper"] = "021"
        with mock.patch.object(PGM, "declared_functions", return_value=declared):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM._function_credits()
        self.assertEqual(caught.exception.code, "APPROVED_FIXTURE_INCOMPLETE")
        self.assertIn("villa_undeclared_helper", caught.exception.message)

    def test_T003C_04_declaration_text_inside_comments_is_not_a_declaration(self):
        self.assertEqual(self.declared_in(self.SYNTHETIC_WITH_PROSE), {"villa_real": "021"})

    def test_T003C_05_declaration_text_inside_a_string_literal_is_not_a_declaration(self):
        sql = "SELECT 'CREATE FUNCTION villa_ghost() RETURNS int';\n"
        self.assertEqual(self.declared_in(sql), {})

    def test_T003C_06_declaration_text_inside_a_dollar_quoted_body_is_not_a_declaration(self):
        sql = (
            "CREATE FUNCTION villa_real(x int) RETURNS int LANGUAGE sql AS $$\n"
            "  -- CREATE FUNCTION villa_inner() RETURNS int\n"
            "  SELECT 'CREATE OR REPLACE FUNCTION villa_inner() RETURNS int';\n"
            "$$;\n"
        )
        self.assertEqual(self.declared_in(sql), {"villa_real": "021"})

    def test_T003C_07_the_same_function_declared_by_two_files_is_refused(self):
        directory = Path(self.tmpdir.name)
        (directory / "002_first.pg.sql").write_text(
            "CREATE FUNCTION villa_twice() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;", encoding="utf-8"
        )
        (directory / "021_second.pg.sql").write_text(
            "CREATE OR REPLACE FUNCTION villa_twice() RETURNS int LANGUAGE sql AS $$ SELECT 2 $$;",
            encoding="utf-8",
        )
        with mock.patch.object(PGM, "SCHEMA_DIR", directory), mock.patch.object(
            PGM, "PG_ONLY_FILES", ("002_first.pg.sql", "021_second.pg.sql")
        ):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM.declared_functions()
        self.assertEqual(caught.exception.code, "APPROVED_FILE_UNPROVENANCED")
        self.assertIn("villa_twice", caught.exception.message)

    def test_T003C_08_a_declared_then_dropped_function_is_not_required(self):
        sql = (
            "CREATE FUNCTION villa_legacy() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
            "DROP FUNCTION IF EXISTS villa_legacy();\n"
            "CREATE OR REPLACE FUNCTION public.villa_kept(value text) RETURNS text"
            " LANGUAGE sql AS $$ SELECT value $$;\n"
            "DROP FUNCTION villa_kept;\n"
            "CREATE FUNCTION villa_kept(value text) RETURNS text LANGUAGE sql AS $$ SELECT value $$;\n"
        )
        # villa_legacy: declared, then dropped, never recreated — not required.
        # villa_kept: dropped *and then* declared again — required, so position matters.
        self.assertEqual(self.declared_in(sql), {"villa_kept": "021"})

    def test_T003C_09_capture_asks_the_catalogue_by_declaration_not_by_name_shape(self):
        script = self.capture_script()
        cur = _RecordingCursor(
            rows=[("helper_not_villa", "CREATE FUNCTION helper_not_villa() RETURNS int")]
        )
        found = script._functions(cur, ["helper_not_villa", "villa_iso_date"])
        query, params = cur.calls[0]
        self.assertIn("proname = ANY(%s)", query)
        self.assertNotIn("LIKE", query)
        self.assertEqual(params, (["helper_not_villa", "villa_iso_date"],))
        self.assertEqual(found, {"helper_not_villa": "CREATE FUNCTION helper_not_villa() RETURNS int"})

    def test_T003C_10_an_incomplete_fixture_stops_verification_and_planning(self):
        conn = self.migrated()
        payload = dict(self.captured_payload())
        payload["function"] = {k: v for k, v in payload["function"].items() if k != "villa_iso_date"}
        with mock.patch.object(PGM, "approved_guards", return_value=payload):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM.verify_guards(conn)
            self.assertEqual(caught.exception.code, "APPROVED_FIXTURE_INCOMPLETE")
            with self.assertRaises(PGM.MigrationRefused) as planned:
                PGM.plan(conn, PGM.discover())
            self.assertEqual(planned.exception.code, "APPROVED_FIXTURE_INCOMPLETE")

    def test_T003C_11_the_approved_fixture_still_verifies_clean(self):
        conn = self.migrated()
        self.assertEqual(PGM.verify_guards(conn), [])
        self.assertEqual(PGM.database_state(conn, PGM.discover())["status"], PGM.READY)

    def test_T003C_12_a_fixture_entry_no_file_declares_is_still_refused(self):
        payload = dict(self.captured_payload())
        payload["function"] = {**payload["function"], "villa_phantom": "CREATE FUNCTION ..."}
        with mock.patch.object(PGM, "approved_guards", return_value=payload):
            with self.assertRaises(PGM.MigrationRefused) as caught:
                PGM._function_credits()
        self.assertEqual(caught.exception.code, "APPROVED_FILE_UNPROVENANCED")
        self.assertIn("villa_phantom", caught.exception.message)

    def test_T003C_13_a_nested_block_comment_hides_its_declaration(self):
        sql = (
            "/* outer /* inner */ CREATE FUNCTION villa_ghost() RETURNS int; */\n"
            "CREATE FUNCTION villa_real() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
        )
        self.assertEqual(self.declared_in(sql), {"villa_real": "021"})

    def test_T003C_14_a_schema_qualifier_written_with_spaces_is_still_read(self):
        sql = "create function public . villa_spaced() returns int language sql as $$ select 1 $$;\n"
        self.assertEqual(self.declared_in(sql), {"villa_spaced": "021"})

    def test_T003C_15_a_quoted_identifier_is_refused_not_skipped(self):
        sql = 'CREATE FUNCTION "villa_quoted"() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n'
        with self.assertRaises(PGM.MigrationRefused) as caught:
            self.declared_in(sql)
        self.assertEqual(caught.exception.code, "APPROVED_DECLARATION_UNSUPPORTED")

    def test_T003C_16_an_unterminated_literal_is_refused_not_swallowed(self):
        for body in (
            "CREATE FUNCTION villa_x() RETURNS int LANGUAGE sql AS $$ SELECT 1;\n",
            "SELECT 'unterminated;\nCREATE FUNCTION villa_y() RETURNS int;\n",
            "/* unterminated\nCREATE FUNCTION villa_z() RETURNS int;\n",
        ):
            with self.subTest(body=body.split()[0]):
                with self.assertRaises(PGM.MigrationRefused) as caught:
                    self.declared_in(body)
                self.assertEqual(caught.exception.code, "APPROVED_SQL_UNPARSEABLE")

    def test_T003C_17_capture_refuses_overloads_it_cannot_describe(self):
        script = self.capture_script()
        cur = _RecordingCursor(
            rows=[
                ("villa_overloaded", "CREATE FUNCTION villa_overloaded(int) RETURNS int"),
                ("villa_overloaded", "CREATE FUNCTION villa_overloaded(text) RETURNS text"),
            ]
        )
        with self.assertRaises(SystemExit) as caught:
            script._functions(cur, ["villa_overloaded"])
        self.assertIn("villa_overloaded", str(caught.exception))

    def test_T003C_18_schema_built_inside_a_do_block_is_refused(self):
        sql = (
            "DO $body$ BEGIN EXECUTE 'CREATE FUNCTION public.helper() RETURNS int'; END $body$;\n"
        )
        with self.assertRaises(PGM.MigrationRefused) as caught:
            self.declared_in(sql)
        self.assertEqual(caught.exception.code, "APPROVED_DECLARATION_UNSUPPORTED")

    def test_T003C_19_a_do_block_that_only_asserts_still_parses(self):
        # The approved 021 file installs its guard with two DO blocks that only raise; a refusal
        # here would break the real migration, so the check must be narrow.
        sql = (
            "DO $$ DECLARE v integer; BEGIN SELECT count(*) INTO v FROM public.holds;"
            " IF v > 0 THEN RAISE EXCEPTION 'refusing to install over % rows', v; END IF; END $$;\n"
            "CREATE FUNCTION villa_real() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
        )
        self.assertEqual(self.declared_in(sql), {"villa_real": "021"})
        self.assertEqual(len(PGM.declared_functions()), 7)

    def test_T003C_20_schema_built_inside_a_function_body_is_refused(self):
        # The declared function itself is honest; the DDL it would run is not attributable.
        sql = (
            "CREATE FUNCTION villa_make_table() RETURNS void LANGUAGE plpgsql AS $fn$\n"
            "BEGIN\n"
            "    EXECUTE 'CREATE TABLE public.hidden (id integer)';\n"
            "END\n"
            "$fn$;\n"
            "DO $body$ BEGIN PERFORM villa_make_table(); END $body$;\n"
        )
        with self.assertRaises(PGM.MigrationRefused) as caught:
            self.declared_in(sql)
        self.assertEqual(caught.exception.code, "APPROVED_DECLARATION_UNSUPPORTED")

    SYNTHETIC_WITH_PROSE = (
        "-- CREATE FUNCTION villa_ghost() RETURNS int\n"
        "/* CREATE OR REPLACE FUNCTION villa_block_ghost() RETURNS int */\n"
        "CREATE FUNCTION villa_real() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
        "COMMENT ON FUNCTION villa_real() IS 'mentions CREATE FUNCTION villa_in_comment()';\n"
    )

    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def declared_in(self, sql: str) -> dict[str, str]:
        """Run the real parser over a synthetic file, via the module's own file loop."""
        directory = Path(self.tmpdir.name)
        (directory / "021_synthetic.pg.sql").write_text(sql, encoding="utf-8")
        with mock.patch.object(PGM, "SCHEMA_DIR", directory), mock.patch.object(
            PGM, "PG_ONLY_FILES", ("021_synthetic.pg.sql",)
        ):
            return PGM.declared_functions()

    @staticmethod
    def capture_script() -> Any:
        import importlib.util

        path = REPO_ROOT / "scripts" / "capture_guard_definitions.py"
        spec = importlib.util.spec_from_file_location("capture_guard_definitions", path)
        if spec is None or spec.loader is None:  # pragma: no cover - the file is in the repo
            raise AssertionError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class _RecordingCursor:
    """Enough of a cursor to see what the capture script asks PostgreSQL for."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = ()) -> None:
        self.calls.append((query, params))

    def fetchall(self) -> list[tuple[str, str]]:
        return self.rows


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

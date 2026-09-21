"""Schema tests: twin parity, drift detection, and (optionally) a live PostgreSQL.

The two schema files are the contract this whole system is built on, so they are
tested three ways:

1. **SQLite really applies.** The development/tests dialect must build a clean
   database, be idempotent, and enforce its own append-only triggers.
2. **The twin cannot drift.** ``001_initial.pg.sql`` is generated from the SQLite
   source; this file fails if the committed twin stops matching the generator, and
   then compares the two files *structurally* (tables, columns, nullability,
   keys, checks, enums, indexes, triggers).
3. **PostgreSQL really accepts it.** With ``VILLA_PG_DSN`` set, both PostgreSQL
   files are applied to a real server and the resulting catalogue is compared with
   the SQLite model. Without a server the test is skipped — never silently passed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import unittest
from pathlib import Path

import schema_loader as SL

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "generate_pg_twin.py"
PG_ONLY_002 = REPO_ROOT / "src" / "schema" / "002_pg_range_exclusions.pg.sql"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_pg_twin", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestSqliteSchemaApplies(unittest.TestCase):
    """The development dialect must build a complete, self-guarding database."""

    @classmethod
    def setUpClass(cls):
        cls.conn = SL.connect_sqlite(":memory:")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_applied_version_is_recorded(self):
        self.assertEqual(SL.applied_version(self.conn), "001_initial")

    def test_every_table_from_the_model_exists(self):
        model = SL.parse_schema_model("sqlite")
        existing = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertEqual(set(model.tables) - existing, set())

    def test_every_index_from_the_model_exists(self):
        model = SL.parse_schema_model("sqlite")
        existing = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        self.assertEqual(set(model.indexes) - existing, set())

    def test_foreign_keys_are_enabled_on_connect(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
                " base_capacity, max_guests, status, created_at, updated_at)"
                " VALUES ('unit_x', 'prop_missing', 'x', 'room', 1, 1, 1, 2, 'ACTIVE', 't', 't')"
            )

    def test_audit_events_reject_update_and_delete(self):
        self.conn.execute(
            "INSERT INTO audit_events (audit_event_id, seq, occurred_at, actor, actor_role, action,"
            " entity_type, entity_id, before_state, after_state, reason, policy_version_id,"
            " evidence_ref, tool, trace_id, prev_hash, hash)"
            " VALUES ('audit_1', 1, 't', 'a', 'r', 'x', 'y', 'z', NULL, NULL, NULL, NULL, NULL,"
            " NULL, NULL, ?, ?)",
            ("0" * 64, "1" * 64),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE audit_events SET action = 'tampered'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit_events")

    def test_second_connection_sees_an_initialised_database(self):
        """Applying the schema twice must not raise (idempotent bootstrap)."""
        path = REPO_ROOT / "tests" / "tmp_schema_idempotent.db"
        try:
            first = SL.connect_sqlite(str(path))
            first.close()
            second = SL.connect_sqlite(str(path))
            self.assertEqual(SL.applied_version(second), "001_initial")
            second.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()

    def test_append_only_tables_are_covered_by_triggers_in_both_directions(self):
        model = SL.parse_schema_model("sqlite")
        by_table: dict[str, set[str]] = {}
        for trigger in model.triggers.values():
            by_table.setdefault(trigger.table, set()).update(trigger.events)
        append_only = (
            "audit_events", "booking_state_events", "policy_versions", "rate_cards",
            "case_messages", "case_events", "intake_proposals", "match_runs",
            "match_candidates", "approval_decisions",
        )
        for table in append_only:
            with self.subTest(table=table):
                self.assertEqual(by_table.get(table), {"UPDATE", "DELETE"})


class TestTwinDrift(unittest.TestCase):
    """``001_initial.pg.sql`` is generated — it must match the generator output."""

    def test_committed_twin_matches_the_generator(self):
        generator = load_generator()
        sqlite_sql = (REPO_ROOT / "src" / "schema" / "001_initial.sql").read_text(encoding="utf-8")
        expected = generator.generate(sqlite_sql)
        actual = (REPO_ROOT / "src" / "schema" / "001_initial.pg.sql").read_text(encoding="utf-8")
        self.assertEqual(
            actual,
            expected,
            "src/schema/001_initial.pg.sql is stale — run "
            "python3 scripts/generate_pg_twin.py --write",
        )

    def test_generator_is_deterministic(self):
        generator = load_generator()
        sqlite_sql = (REPO_ROOT / "src" / "schema" / "001_initial.sql").read_text(encoding="utf-8")
        self.assertEqual(generator.generate(sqlite_sql), generator.generate(sqlite_sql))

    def test_pg_twin_keeps_every_table_and_trigger_name(self):
        sqlite_model = SL.parse_schema_model("sqlite")
        pg_model = SL.parse_schema_model("pg")
        self.assertEqual(set(sqlite_model.tables), set(pg_model.tables))
        self.assertEqual(set(sqlite_model.triggers), set(pg_model.triggers))
        self.assertEqual(set(sqlite_model.indexes), set(pg_model.indexes))

    def test_pg_only_hardening_is_not_leaked_into_the_twin(self):
        """002 is PostgreSQL-only on purpose; the twin must stay SQLite-comparable."""
        twin = (REPO_ROOT / "src" / "schema" / "001_initial.pg.sql").read_text(encoding="utf-8")
        self.assertNotIn("EXCLUDE USING", twin)


class TestStructuralParity(unittest.TestCase):
    """Compare the two dialects field by field."""

    @classmethod
    def setUpClass(cls):
        cls.sqlite = SL.parse_schema_model("sqlite")
        cls.pg = SL.parse_schema_model("pg")

    def test_columns_match_in_name_order_and_nullability(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                other = self.pg.tables[name]
                self.assertEqual(
                    [(c.name, c.nullable) for c in table.columns],
                    [(c.name, c.nullable) for c in other.columns],
                )

    def test_column_types_match_by_family(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                other = self.pg.tables[name]
                self.assertEqual(
                    [(c.name, c.type_family) for c in table.columns],
                    [(c.name, c.type_family) for c in other.columns],
                )

    def test_primary_keys_match(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                self.assertEqual(table.primary_key, self.pg.tables[name].primary_key)

    def test_unique_constraints_match(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                self.assertEqual(table.uniques, self.pg.tables[name].uniques)

    def test_foreign_keys_match_including_delete_rules(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                self.assertEqual(
                    [
                        (fk.columns, fk.ref_table, fk.ref_columns, fk.on_delete)
                        for fk in table.foreign_keys
                    ],
                    [
                        (fk.columns, fk.ref_table, fk.ref_columns, fk.on_delete)
                        for fk in self.pg.tables[name].foreign_keys
                    ],
                )

    def test_check_constraints_match_by_name_and_expression(self):
        for name, table in self.sqlite.tables.items():
            with self.subTest(table=name):
                self.assertEqual(
                    [(c[0], c[1]) for c in table.checks],
                    [(c[0], c[1]) for c in self.pg.tables[name].checks],
                )

    def test_enum_value_sets_match(self):
        self.assertEqual(
            {key: set(values) for key, values in self.sqlite.enums.items()},
            {key: set(values) for key, values in self.pg.enums.items()},
        )

    def test_indexes_match_including_partial_predicates(self):
        for name, index in self.sqlite.indexes.items():
            with self.subTest(index=name):
                other = self.pg.indexes[name]
                self.assertEqual(
                    (index.table, index.columns, index.unique, index.predicate),
                    (other.table, other.columns, other.unique, other.predicate),
                )

    def test_triggers_match_table_event_and_timing(self):
        for name, trigger in self.sqlite.triggers.items():
            with self.subTest(trigger=name):
                other = self.pg.triggers[name]
                self.assertEqual(
                    (trigger.table, trigger.events, trigger.timing),
                    (other.table, other.events, other.timing),
                )

    def test_schema_meta_insert_is_identical(self):
        self.assertEqual(self.sqlite.inserts, self.pg.inserts)


class TestPostgresOnlyHardening(unittest.TestCase):
    """The PG-only file must actually carry the range guarantees it claims."""

    @classmethod
    def setUpClass(cls):
        cls.text = PG_ONLY_002.read_text(encoding="utf-8")

    def test_uses_btree_gist_for_range_exclusion(self):
        self.assertIn("CREATE EXTENSION IF NOT EXISTS btree_gist", self.text)
        self.assertIn("EXCLUDE USING gist", self.text)

    def test_declares_exclusion_constraints_for_holds_and_bookings(self):
        self.assertIn("ex_holds_no_overlap", self.text)
        self.assertIn("ex_bookings_no_overlap", self.text)

    def test_exclusions_are_partial_to_active_rows_only(self):
        self.assertRegex(self.text, r"WHERE \(status = 'ACTIVE'\)")
        self.assertRegex(self.text, r"WHERE \(status IN \('CONFIRMED'")

    def test_booking_status_list_matches_the_kernel_constant(self):
        import reservation_kernel as RK

        match = re.search(r"WHERE \(status IN \(([^)]*)\)\)", self.text)
        self.assertIsNotNone(match)
        listed = {value.strip().strip("'") for value in match.group(1).split(",")}
        self.assertEqual(listed, set(RK.OCCUPYING_BOOKING_STATES))

    def test_date_columns_are_pinned_to_iso_before_the_immutable_cast(self):
        """The IMMUTABLE promise is only sound with an unambiguous date format."""
        self.assertIn("IMMUTABLE", self.text)
        for column in ("check_in", "check_out"):
            with self.subTest(column=column):
                self.assertIn(f"ck_holds_{column}_iso", self.text)
                self.assertIn(f"ck_bookings_{column}_iso", self.text)


@unittest.skipUnless(os.environ.get("VILLA_PG_DSN"), "set VILLA_PG_DSN to run the live PostgreSQL parity test")
class TestLivePostgres(unittest.TestCase):
    """Apply both PostgreSQL files to a real server and compare the catalogue.

    ``VILLA_PG_DSN`` must point to a **disposable** database: ``setUpClass`` drops
    and recreates ``public`` so every run starts from an empty server, which is the
    only way to prove the DDL applies from scratch. Never point this at a database
    that holds real data.
    """

    @classmethod
    def _apply(cls, path: Path) -> None:
        """Execute a schema file statement by statement on the live server."""
        statements = [s for s in SL.split_statements(path.read_text(encoding="utf-8")) if s.strip()]
        with cls.conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    @classmethod
    def setUpClass(cls):
        import psycopg2  # noqa: F401  (import error here is a hard failure, by design)

        cls.dsn = os.environ["VILLA_PG_DSN"]
        cls.conn = psycopg2.connect(cls.dsn)
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")
        cls._apply(PG_ONLY_002.parent / "001_initial.pg.sql")
        cls._apply(PG_ONLY_002)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_both_files_apply_and_match_the_sqlite_model(self):
        model = SL.parse_schema_model("sqlite")
        with self.conn.cursor() as cur:
            for table in model.tables:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = %s ORDER BY ordinal_position",
                    (table,),
                )
                columns = [row[0] for row in cur.fetchall()]
                with self.subTest(table=table):
                    self.assertEqual(columns, [c.name for c in model.tables[table].columns])
            for constraint in ("ex_holds_no_overlap", "ex_bookings_no_overlap"):
                cur.execute(
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = %s", (constraint,)
                )
                self.assertEqual(cur.fetchone()[0], 1, constraint)

    def test_range_exclusion_really_blocks_an_overlap(self):
        """End-to-end proof: two overlapping ACTIVE holds must be rejected by PG."""
        import psycopg2

        seed = [
            "INSERT INTO properties (property_id, name, property_type, region, province,"
            " timezone, status, contract_pack_status, contract_pack_evaluated_at,"
            " created_at, updated_at) VALUES ('prop_pg_probe', 'Probe', 'villa', 'Probe',"
            " 'Bali', 'Asia/Makassar', 'SELLABLE', 'VERIFIED', NULL,"
            " '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00')",
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES ('unit_pg_probe', 'prop_pg_probe', 'Probe unit', 'entire_villa', 1, 1,"
            " 2, 4, 'ACTIVE', '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00')",
            "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level,"
            " source_type, source_ref, captured_at, valid_until, result, occupancy_ranges,"
            " can_quote, can_hold, can_confirm, notes, created_at)"
            " VALUES ('evid_pg_probe', 'unit_pg_probe', 'A', 'manager_written', 'probe',"
            " '2026-09-21T00:00:00+00:00', '2026-09-22T00:00:00+00:00', 'AVAILABLE', '[]',"
            " 1, 1, 1, NULL, '2026-09-21T00:00:00+00:00')",
        ]
        policy_insert = (
            "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
            " effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
            " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
            " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by,"
            " created_at) VALUES ('policy_probe', 'prop_pg_probe', 'property', 1,"
            " '2026-01-01', '2026-12-31', 'IDR', '30', '5', '10', '15', 48, 720, 2,"
            " '{}', %s, 'probe', 'user:probe', '2026-09-21T00:00:00+00:00')"
        )
        first_hold = (
            "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
            " status, guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,"
            " created_at, expires_at, closed_at, close_reason, extended_count)"
            " VALUES ('hold_pg_1', 'unit_pg_probe', 'prop_pg_probe', '2026-10-01',"
            " '2026-10-03', 2, 'ACTIVE', 'guest_probe', NULL, 'policy_probe',"
            " 'evid_pg_probe', 'idem_pg_0001', '2026-09-21T00:00:00+00:00',"
            " '2026-09-22T00:00:00+00:00', NULL, NULL, 0)"
        )
        overlapping_hold = (
            "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
            " status, guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key,"
            " created_at, expires_at, closed_at, close_reason, extended_count)"
            " VALUES ('hold_pg_2', 'unit_pg_probe', 'prop_pg_probe', '2026-10-02',"
            " '2026-10-04', 2, 'ACTIVE', 'guest_probe2', NULL, 'policy_probe',"
            " 'evid_pg_probe', 'idem_pg_0002', '2026-09-21T00:00:00+00:00',"
            " '2026-09-22T00:00:00+00:00', NULL, NULL, 0)"
        )

        # The probe runs inside ONE transaction that is rolled back. Several of
        # these tables are append-only on purpose (``villa_reject_mutation`` refuses
        # UPDATE/DELETE), so cleanup must never rely on deleting rows: a live
        # server would — correctly — reject the tidy-up and leak the probe data.
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                for statement in seed:
                    cur.execute(statement)
                cur.execute(policy_insert, ("0" * 64,))
                cur.execute(first_hold)
                # A back-to-back stay must still be allowed: [start, end) is half-open.
                cur.execute(
                    "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out,"
                    " nights, status, guest_ref, quote_ref, policy_version_id, evidence_id,"
                    " idempotency_key, created_at, expires_at, closed_at, close_reason,"
                    " extended_count) VALUES ('hold_pg_3', 'unit_pg_probe', 'prop_pg_probe',"
                    " '2026-10-03', '2026-10-05', 2, 'ACTIVE', 'guest_probe3', NULL,"
                    " 'policy_probe', 'evid_pg_probe', 'idem_pg_0003',"
                    " '2026-09-21T00:00:00+00:00', '2026-09-22T00:00:00+00:00', NULL, NULL, 0)"
                )
                with self.assertRaises(psycopg2.errors.ExclusionViolation):
                    cur.execute(overlapping_hold)
            finally:
                cur.execute("ROLLBACK")


    # --------------------------------------------------------------- ADR-0002 --
    @staticmethod
    def _seed_review_probe(cur) -> None:
        """Seed an isolated property/unit/policy for the ADR-0002 probe."""
        cur.execute(
            "INSERT INTO properties (property_id, name, property_type, region, province,"
            " timezone, status, contract_pack_status, contract_pack_evaluated_at,"
            " created_at, updated_at) VALUES ('prop_pg_adr2', 'Probe ADR-0002', 'villa',"
            " 'Probe', 'Bali', 'Asia/Makassar', 'SELLABLE', 'VERIFIED', NULL,"
            " '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00')"
        )
        cur.execute(
            "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
            " base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES ('unit_pg_adr2', 'prop_pg_adr2', 'Probe unit', 'entire_villa', 1, 1, 2, 4,"
            " 'ACTIVE', '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00')"
        )
        cur.execute(
            "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
            " effective_from, effective_to, currency, deposit_pct, service_fee_pct,"
            " tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,"
            " max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)"
            " VALUES ('policy_pg_adr2', 'prop_pg_adr2', 'property', 1, '2026-01-01', '2027-12-31',"
            " 'IDR', '30', '5', '10', '15', 48, 720, 2, '{}', '" + "0" * 64 + "', 'probe',"
            " 'user:probe', '2026-09-21T00:00:00+00:00')"
        )

    @staticmethod
    def _booking_probe(tag: str, check_in: str, check_out: str, status: str) -> str:
        """One booking on the ADR-0002 probe unit, built from string literals."""
        return (
            "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
            " status, guest_ref, adults, children, infants, currency, policy_version_id,"
            " policy_fingerprint, total_minor, payment_status, idempotency_key, created_at,"
            " updated_at) VALUES ("
            f"'{tag}', 'prop_pg_adr2', 'unit_pg_adr2', '{check_in}', '{check_out}', 2, '{status}',"
            " 'guest_probe', 2, 0, 0, 'IDR', 'policy_pg_adr2', '" + "0" * 64 + "', 1000000,"
            f" 'UNPAID', 'idem_{tag}_pg_0001', '2026-09-21T00:00:00+00:00',"
            " '2026-09-21T00:00:00+00:00')"
        )

    def test_booking_exclusion_holds_the_dates_during_review_and_dispute(self):
        """ADR-0002 Opsi A end-to-end: PostgreSQL itself refuses the re-sale.

        The kernel check and the SQLite partial index are proven separately. This is
        the third layer — ``ex_bookings_no_overlap`` on a live server — and it is the
        one that has to hold when a row reaches the table by any other route.
        """
        import psycopg2

        cases = (
            ("CANCELLATION_REQUESTED", "2026-11-01", "2026-11-03", "2026-11-02", "2026-11-04"),
            ("CANCELLATION_REVIEW", "2026-12-01", "2026-12-03", "2026-12-02", "2026-12-04"),
            ("DISPUTED", "2027-01-01", "2027-01-03", "2027-01-02", "2027-01-04"),
        )
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                self._seed_review_probe(cur)
                for index, (state, held_in, held_out, resale_in, resale_out) in enumerate(cases):
                    cur.execute("SAVEPOINT adr2")
                    cur.execute(
                        self._booking_probe(f"bk_pg_probe_{index}", held_in, held_out, state)
                    )
                    with self.assertRaises(psycopg2.errors.ExclusionViolation):
                        cur.execute(
                            self._booking_probe(
                                f"bk_pg_resale_{index}", resale_in, resale_out, "CONFIRMED"
                            )
                        )
                    cur.execute("ROLLBACK TO SAVEPOINT adr2")
                # The widening must not turn [start, end) into a closed range: a stay
                # beginning on the release date is a different stay, not an overlap.
                cur.execute(self._booking_probe("bk_pg_back", "2027-02-01", "2027-02-03", "CONFIRMED"))
                cur.execute(self._booking_probe("bk_pg_back2", "2027-02-03", "2027-02-05", "CONFIRMED"))
                # And the decision that releases the dates really does release them.
                cur.execute(self._booking_probe("bk_pg_gone", "2027-03-01", "2027-03-03", "CANCELLED"))
                cur.execute(self._booking_probe("bk_pg_new", "2027-03-01", "2027-03-03", "CONFIRMED"))
                cur.execute("SELECT COUNT(*) FROM bookings WHERE property_id = 'prop_pg_adr2'")
                stored = cur.fetchone()
                self.assertEqual(stored[0] if stored else None, 4)
            finally:
                cur.execute("ROLLBACK")

    def test_phase2_append_only_triggers_exist_and_reject_an_update(self):
        """The same six guards exist on the real server and really fire there.

        The SQLite run proves the DDL; this proves the *generated* PostgreSQL twin
        carries the guards too — a silent conversion bug would otherwise only show
        up in production.
        """
        import psycopg2

        logs = (
            "case_messages", "case_events", "intake_proposals",
            "match_runs", "match_candidates", "approval_decisions",
        )
        with self.conn.cursor() as cur:
            for table in logs:
                with self.subTest(table=table):
                    cur.execute(
                        "SELECT tg.tgname FROM pg_trigger tg"
                        " JOIN pg_class c ON c.oid = tg.tgrelid"
                        " WHERE c.relname = %s AND NOT tg.tgisinternal ORDER BY tg.tgname",
                        (table,),
                    )
                    names = [row[0] for row in cur.fetchall()]
                    self.assertEqual(
                        names, ["trg_%s_no_delete" % table, "trg_%s_no_update" % table]
                    )
            cur.execute("BEGIN")
            try:
                cur.execute(
                    "INSERT INTO cases (case_id, channel, guest_ref, status, priority, sla_policy,"
                    " first_response_due_at, quote_due_at, idempotency_key, created_at, updated_at)"
                    " VALUES ('case_pg_guard', 'website', 'guest_pg', 'NEW', 'NORMAL', 'web_v1',"
                    " '2026-09-21T06:00:00+00:00', '2026-09-21T12:00:00+00:00', 'idem_pg_guard',"
                    " '2026-09-21T04:00:00+00:00', '2026-09-21T04:00:00+00:00')"
                )
                cur.execute(
                    "INSERT INTO case_events (case_event_id, case_id, seq, kind, actor, actor_role,"
                    " command_id, occurred_at) VALUES ('ce_pg_guard', 'case_pg_guard', 1, 'CREATED',"
                    " 'agent_1', 'agent', 'cmd_pg_guard_1', '2026-09-21T04:00:00+00:00')"
                )
                # One failed statement aborts the whole transaction, so each probe needs
                # its own savepoint: otherwise the second probe fails for the wrong reason.
                cur.execute("SAVEPOINT before_update")
                with self.assertRaises(psycopg2.Error) as caught:
                    cur.execute("UPDATE case_events SET reason = 'ditulis ulang'")
                self.assertIn("append-only", str(caught.exception))
                cur.execute("ROLLBACK TO SAVEPOINT before_update")
                cur.execute("SAVEPOINT before_delete")
                with self.assertRaises(psycopg2.Error) as caught:
                    cur.execute("DELETE FROM case_events")
                self.assertIn("append-only", str(caught.exception))
                cur.execute("ROLLBACK TO SAVEPOINT before_delete")
            finally:
                cur.execute("ROLLBACK")


class TestPhase2FailClosedGuards(unittest.TestCase):
    """Phase 2 rows: every guard the database can enforce must really reject.

    These are the invariants that keep the AI layer from becoming a source of
    truth. They are asserted against the real DDL, not against a helper: if the
    constraint is dropped or weakened, the test fails.
    """

    NOW = "2026-09-21T04:00:00+00:00"
    FUTURE = "2026-09-21T06:00:00+00:00"
    HASH = "a" * 64

    def setUp(self):
        self.conn = SL.connect_sqlite(":memory:")
        self.addCleanup(self.conn.close)
        self._seed()

    # -- helpers ---------------------------------------------------------
    def _insert(self, table: str, **cols):
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values())
        )
        return cur

    def _rejects(self, table: str, **cols):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(table, **cols)

    def _seed(self):
        self._insert(
            "properties",
            property_id="prop_p2", name="Probe Villa", property_type="villa",
            region="Ubud", province="Bali", timezone="Asia/Makassar", status="SELLABLE",
            contract_pack_status="VERIFIED", created_at=self.NOW, updated_at=self.NOW,
        )
        self._insert(
            "units",
            unit_id="unit_p2", property_id="prop_p2", name="Probe Unit",
            unit_type="entire_villa", bedrooms=2, bathrooms=2, base_capacity=2,
            max_guests=4, status="ACTIVE", created_at=self.NOW, updated_at=self.NOW,
        )
        self._insert(
            "cases",
            case_id="case_p2", channel="website", guest_ref="guest_p2", status="NEW",
            priority="NORMAL", sla_policy="web_v1", first_response_due_at=self.FUTURE,
            quote_due_at="2026-09-21T12:00:00+00:00", idempotency_key="idem_case_p2_x",
            created_at=self.NOW, updated_at=self.NOW,
        )
        self._insert(
            "case_messages",
            case_message_id="msg_p2", case_id="case_p2", direction="INBOUND",
            channel="website", sender_ref="guest_p2", received_at=self.NOW,
            content_class="P2", body_redacted="Halo, saya mau tanya villa 2 kamar.",
            body_sha256=self.HASH, created_at=self.NOW,
        )
        self._insert(
            "intake_proposals",
            proposal_id="prop_p2_prop", case_id="case_p2", source_message_id="msg_p2",
            extractor="rule/1", method="rule", produced_at=self.NOW, status="PROPOSED",
            payload="{}", fingerprint=self.HASH, created_at=self.NOW,
        )

    # -- PII / classification -------------------------------------------
    def test_inbound_p3_and_p4_content_is_never_stored(self):
        for tier in ("P3", "P4"):
            with self.subTest(tier=tier):
                self._rejects(
                    "case_messages", case_message_id=f"msg_{tier}", case_id="case_p2",
                    direction="INBOUND", channel="email", sender_ref="guest_p2",
                    received_at=self.NOW, content_class=tier, body_redacted="no. KTP 1234",
                    body_sha256=self.HASH, created_at=self.NOW,
                )
                self._insert(
                    "case_messages", case_message_id=f"msg_{tier}_ok", case_id="case_p2",
                    direction="INBOUND", channel="email", sender_ref="guest_p2",
                    received_at=self.NOW, content_class=tier, body_redacted=None,
                    body_sha256=self.HASH, created_at=self.NOW,
                )

    def test_p2_content_must_arrive_already_redacted(self):
        self._rejects(
            "case_messages", case_message_id="msg_p2_raw", case_id="case_p2",
            direction="INBOUND", channel="email", sender_ref="guest_p2",
            received_at=self.NOW, content_class="P2", body_redacted=None,
            body_sha256=self.HASH, created_at=self.NOW,
        )

    # -- case lifecycle --------------------------------------------------
    def test_a_closed_case_needs_a_timestamp_and_a_reason(self):
        self._rejects(
            "cases", case_id="case_closed_bad", channel="website", guest_ref="guest_p2",
            status="CLOSED_WON", priority="NORMAL", sla_policy="web_v1",
            first_response_due_at=self.FUTURE, quote_due_at="2026-09-21T12:00:00+00:00",
            idempotency_key="idem_closed_bad", created_at=self.NOW, updated_at=self.NOW,
        )

    def test_case_sla_window_must_be_ordered(self):
        self._rejects(
            "cases", case_id="case_sla_bad", channel="whatsapp_business", guest_ref="guest_p2",
            status="NEW", priority="HIGH", sla_policy="wa_v1",
            first_response_due_at="2026-09-21T03:00:00+00:00", quote_due_at="2026-09-21T12:00:00+00:00",
            idempotency_key="idem_sla_bad", created_at=self.NOW, updated_at=self.NOW,
        )

    def test_a_case_cannot_be_created_twice_from_the_same_command(self):
        self._rejects(
            "cases", case_id="case_p2_dup", channel="website", guest_ref="guest_p2",
            status="NEW", priority="NORMAL", sla_policy="web_v1",
            first_response_due_at=self.FUTURE, quote_due_at="2026-09-21T12:00:00+00:00",
            idempotency_key="idem_case_p2_x", created_at=self.NOW, updated_at=self.NOW,
        )

    # -- intake review ---------------------------------------------------
    def test_a_reviewed_intake_field_must_name_its_reviewer(self):
        self._rejects(
            "intake_fields", intake_field_id="fld_bad", proposal_id="prop_p2_prop",
            case_id="case_p2", field_name="check_in", field_value="2026-10-01",
            value_type="date", confidence=90, source_message_id="msg_p2",
            status="ACCEPTED", value_sha256=self.HASH, created_at=self.NOW,
        )
        self._insert(
            "intake_fields", intake_field_id="fld_ok", proposal_id="prop_p2_prop",
            case_id="case_p2", field_name="check_in", field_value="2026-10-01",
            value_type="date", confidence=90, source_message_id="msg_p2",
            status="PROPOSED", value_sha256=self.HASH, created_at=self.NOW,
        )

    def test_a_rejected_intake_field_must_carry_a_reason(self):
        self._rejects(
            "intake_fields", intake_field_id="fld_rej", proposal_id="prop_p2_prop",
            case_id="case_p2", field_name="check_out", field_value=None,
            value_type="date", confidence=10, source_message_id="msg_p2",
            status="REJECTED", value_sha256=self.HASH, created_at=self.NOW,
            reviewed_by="agent_1", reviewed_at=self.NOW,
        )

    def test_one_field_per_proposal_only(self):
        self._insert(
            "intake_fields", intake_field_id="fld_a", proposal_id="prop_p2_prop",
            case_id="case_p2", field_name="pax", field_value="4", value_type="integer",
            confidence=80, source_message_id="msg_p2", status="PROPOSED",
            value_sha256=self.HASH, created_at=self.NOW,
        )
        self._rejects(
            "intake_fields", intake_field_id="fld_b", proposal_id="prop_p2_prop",
            case_id="case_p2", field_name="pax", field_value="6", value_type="integer",
            confidence=80, source_message_id="msg_p2", status="PROPOSED",
            value_sha256=self.HASH, created_at=self.NOW,
        )

    # -- matching --------------------------------------------------------
    def test_an_ineligible_candidate_must_say_why(self):
        self._insert(
            "match_runs", match_run_id="run_p2", case_id="case_p2", engine_version="m1",
            intake_snapshot="{}", intake_hash=self.HASH, criteria="{}",
            considered_count=1, eligible_count=0, created_by="agent_1", created_at=self.NOW,
        )
        self._rejects(
            "match_candidates", candidate_id="cand_bad", match_run_id="run_p2",
            unit_id="unit_p2", rank=1, eligible=0, score_centi=0, reasons="[]",
            created_at=self.NOW,
        )
        self._insert(
            "match_candidates", candidate_id="cand_ok", match_run_id="run_p2",
            unit_id="unit_p2", rank=1, eligible=0, score_centi=0, reasons="[]",
            deny_reasons='[{"code":"MIN_STAY"}]', created_at=self.NOW,
        )

    def test_matching_cannot_claim_more_eligible_than_considered(self):
        self._rejects(
            "match_runs", match_run_id="run_bad", case_id="case_p2", engine_version="m1",
            intake_snapshot="{}", intake_hash=self.HASH, criteria="{}",
            considered_count=1, eligible_count=2, created_by="agent_1", created_at=self.NOW,
        )

    # -- approvals -------------------------------------------------------
    def _approval(self, approval_id: str, **over):
        cols = dict(
            approval_id=approval_id, subject_type="refund", subject_id="bk_1",
            action="refund_deposit", risk_level="HIGH", requested_by="agent_1",
            requested_role="agent", requested_at=self.NOW,
            reason="Tamu membatalkan 20 hari sebelum check-in.", payload="{}",
            payload_sha256=self.HASH, money_minor=500000, currency="IDR",
            required_role="manager", required_approvals=2, status="PENDING",
            expires_at=self.FUTURE, idempotency_key=f"idem_{approval_id}",
            created_at=self.NOW,
        )
        cols.update(over)
        return cols

    def test_money_moving_approvals_need_two_approvers(self):
        self._rejects("approval_requests", **self._approval("app_bad", required_approvals=1))
        self._insert("approval_requests", **self._approval("app_ok"))

    def test_a_non_money_approval_may_need_one_approver(self):
        self._insert(
            "approval_requests",
            **self._approval(
                "app_discount", subject_type="discount", money_minor=None, currency=None,
                required_approvals=1, risk_level="LOW",
            ),
        )

    def test_money_amount_and_currency_travel_together(self):
        self._rejects("approval_requests", **self._approval("app_pair", currency=None))

    def test_the_same_person_cannot_approve_twice(self):
        self._insert("approval_requests", **self._approval("app_dual"))
        for index in (1, 2):
            self._insert(
                "approval_decisions", decision_id=f"dec_{index}", approval_id="app_dual",
                approver="manager_1", approver_role="manager", decision="APPROVE",
                note="Setuju, sesuai policy pembatalan.", decided_at=self.NOW,
                request_hash=self.HASH, signature_hash=self.HASH,
            ) if index == 1 else None
        self._rejects(
            "approval_decisions", decision_id="dec_dup", approval_id="app_dual",
            approver="manager_1", approver_role="manager", decision="REJECT",
            note="Berubah pikiran.", decided_at=self.NOW,
            request_hash=self.HASH, signature_hash=self.HASH,
        )

    # -- message drafts --------------------------------------------------
    def _draft(self, draft_id: str, **over):
        cols = dict(
            draft_id=draft_id, case_id="case_p2", channel="website", template="quote_v1",
            locale="id-ID", recipient_ref="guest_p2", body_text="Harga total Rp1.500.000.",
            body_sha256=self.HASH, grounding="GROUNDED", gate_decision="{}", status="DRAFT",
            created_by="copilot_v1", created_at=self.NOW,
        )
        cols.update(over)
        return cols

    def test_an_ungrounded_draft_can_never_be_approved_or_sent(self):
        self._rejects(
            "message_drafts", **self._draft(
                "dr_bad", grounding="UNGROUNDED", status="APPROVED",
                approved_by="agent_1", approved_at=self.NOW,
            )
        )

    def test_an_approved_draft_records_who_approved_it(self):
        self._rejects("message_drafts", **self._draft("dr_bad2", status="APPROVED"))

    def test_a_sent_draft_records_the_human_and_the_channel_receipt(self):
        self._rejects(
            "message_drafts", **self._draft(
                "dr_bad3", status="SENT", approved_by="agent_1", approved_at=self.NOW,
                sent_by="agent_1", sent_at=self.NOW,
            )
        )

    # -- append-only logs ------------------------------------------------
    def test_phase2_logs_reject_update_and_delete(self):
        """All six Phase-2 history tables refuse to be rewritten, in both directions."""
        self._insert(
            "case_events", case_event_id="ce_1", case_id="case_p2", seq=1, kind="CREATED",
            actor="agent_1", actor_role="agent", command_id="cmd_create_case_1",
            occurred_at=self.NOW,
        )
        self._insert(
            "match_runs", match_run_id="run_p2", case_id="case_p2", engine_version="m1",
            intake_snapshot="{}", intake_hash=self.HASH, criteria="{}",
            considered_count=0, eligible_count=0, created_by="agent_1", created_at=self.NOW,
        )
        self._insert(
            "match_candidates", candidate_id="cand_p2", match_run_id="run_p2",
            unit_id="unit_p2", rank=1, eligible=1, score_centi=100, reasons="[]",
            created_at=self.NOW,
        )
        self._insert("approval_requests", **self._approval("app_p2"))
        self._insert(
            "approval_decisions", decision_id="dec_p2", approval_id="app_p2",
            approver="manager_1", approver_role="manager", decision="APPROVE",
            note="Setuju, sesuai policy pembatalan.", decided_at=self.NOW,
            request_hash=self.HASH, signature_hash=self.HASH,
        )
        probes = (
            ("case_messages", "case_message_id", "msg_p2", "created_at", self.FUTURE),
            ("case_events", "case_event_id", "ce_1", "reason", "ditulis ulang"),
            ("intake_proposals", "proposal_id", "prop_p2_prop", "payload", '{"x":1}'),
            ("match_runs", "match_run_id", "run_p2", "criteria", '{"x":1}'),
            ("match_candidates", "candidate_id", "cand_p2", "reasons", '[{"code":"X"}]'),
            ("approval_decisions", "decision_id", "dec_p2", "note", "diubah belakangan"),
        )
        for table, key, value, column, replacement in probes:
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.conn.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (replacement, value)
                    )
                self.assertIn("append-only", str(caught.exception))
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (value,))
                self.assertIn("append-only", str(caught.exception))

    def test_sla_breach_must_be_resolved_with_a_note_or_left_open(self):
        self._rejects(
            "sla_breaches", breach_id="br_bad", subject_type="case", subject_id="case_p2",
            case_id="case_p2", kind="QUOTE", due_at="2026-09-21T03:00:00+00:00",
            detected_at="2026-09-21T04:00:00+00:00", breach_minutes=60, status="RESOLVED",
            created_at=self.NOW,
        )

    def test_a_breach_cannot_be_recorded_before_it_is_due(self):
        self._rejects(
            "sla_breaches", breach_id="br_early", subject_type="case", subject_id="case_p2",
            case_id="case_p2", kind="QUOTE", due_at="2026-09-21T05:00:00+00:00",
            detected_at="2026-09-21T04:00:00+00:00", breach_minutes=0, status="OPEN",
            created_at=self.NOW,
        )

    def test_the_same_breach_is_recorded_once(self):
        self._insert(
            "sla_breaches", breach_id="br_once", subject_type="case", subject_id="case_p2",
            case_id="case_p2", kind="FIRST_RESPONSE", due_at="2026-09-21T03:00:00+00:00",
            detected_at="2026-09-21T04:00:00+00:00", breach_minutes=60, status="OPEN",
            created_at=self.NOW,
        )
        self._rejects(
            "sla_breaches", breach_id="br_twice", subject_type="case", subject_id="case_p2",
            case_id="case_p2", kind="FIRST_RESPONSE", due_at="2026-09-21T03:00:00+00:00",
            detected_at="2026-09-21T04:00:00+00:00", breach_minutes=90, status="OPEN",
            created_at=self.NOW,
        )

if __name__ == "__main__":  # pragma: no cover
    unittest.main()

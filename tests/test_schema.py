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
        for table in ("audit_events", "booking_state_events", "policy_versions", "rate_cards"):
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
    """Apply both PostgreSQL files to a real server and compare the catalogue."""

    @classmethod
    def setUpClass(cls):
        import psycopg2  # noqa: F401  (import error here is a hard failure, by design)

        cls.dsn = os.environ["VILLA_PG_DSN"]
        cls.conn = psycopg2.connect(cls.dsn)
        cls.conn.autocommit = True

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _apply(self, path: Path) -> None:
        sql = path.read_text(encoding="utf-8")
        statements = [s for s in SL.split_statements(sql) if s.strip()]
        with self.conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    def test_both_files_apply_and_match_the_sqlite_model(self):
        self._apply(PG_ONLY_002.parent / "001_initial.pg.sql")
        self._apply(PG_ONLY_002)
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
            "DELETE FROM holds",
            "DELETE FROM availability_evidence",
            "DELETE FROM policy_versions WHERE policy_version_id = 'policy_probe'",
            "DELETE FROM units WHERE unit_id = 'unit_pg_probe'",
            "DELETE FROM properties WHERE property_id = 'prop_pg_probe'",
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

        with self.conn.cursor() as cur:
            for statement in seed:
                cur.execute(statement)
            cur.execute(policy_insert, ("0" * 64,))
            cur.execute(first_hold)
            with self.assertRaises(psycopg2.errors.ExclusionViolation):
                cur.execute(overlapping_hold)
            # A back-to-back stay must still be allowed: [start, end) is half-open.
            cur.execute(
                "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights,"
                " status, guest_ref, quote_ref, policy_version_id, evidence_id,"
                " idempotency_key, created_at, expires_at, closed_at, close_reason,"
                " extended_count) VALUES ('hold_pg_3', 'unit_pg_probe', 'prop_pg_probe',"
                " '2026-10-03', '2026-10-05', 2, 'ACTIVE', 'guest_probe3', NULL,"
                " 'policy_probe', 'evid_pg_probe', 'idem_pg_0003',"
                " '2026-09-21T00:00:00+00:00', '2026-09-22T00:00:00+00:00', NULL, NULL, 0)"
            )
            cur.execute("DELETE FROM holds")
            cur.execute("DELETE FROM policy_versions WHERE policy_version_id = 'policy_probe'")
            cur.execute("DELETE FROM availability_evidence")
            cur.execute("DELETE FROM units WHERE unit_id = 'unit_pg_probe'")
            cur.execute("DELETE FROM properties WHERE property_id = 'prop_pg_probe'")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

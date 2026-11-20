"""Tests for the SQLite migration runner.

The migration exists because rows added to the initial schema never reach a database that
was already built — ``apply_schema`` refuses to re-apply the initial schema. These tests
build exactly that situation (a database missing the rows) and check that the runner fixes
it, that a second run is a no-op, and — the one that matters most — that recording a
migration does not make ``apply_schema`` refuse to open the database afterwards.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import reservation_kernel as RK  # noqa: E402
import schema_loader as SL  # noqa: E402

FLOOR_MINOR = "commercial.margin_floor_minor"
DENY_KEY = "security.ai.denied_actions"


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.pop("VILLA_DB", None)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "scripts/apply_migrations.py", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=environment,
    )


def _old_database(path: Path, *, skip_statements_mentioning: tuple[str, ...] = ()) -> None:
    """A database as it looked before the migration's rows reached the schema.

    ``policy_defs`` is append-only (a trigger rejects DELETE), so an old database cannot be
    faked by deleting rows from a new one. The schema *text* is pruned instead — which is
    what such a database was actually built from. The same holds for the added ``guests``
    column and ``properties`` geo columns: an old database has neither their ``ALTER TABLE``
    migrations (016, 017) nor the ``CREATE TABLE`` lines, so the test prunes both from the
    schema text.
    """
    kept = [
        line
        for line in SL.read_schema("sqlite").splitlines()
        if FLOOR_MINOR not in line and "polrev_baseline_13" not in line
        and "preferred_currency" not in line
        # migration 016 itself carries the phrase in its header comment
        and not line.startswith("-- Migration 016")
        # migration 017's columns (ADR-0068) — an old database predates all four
        and "geo_precision" not in line
        and "geo_source" not in line
        and not line.strip().startswith(("latitude ", "longitude "))
        and not line.startswith("-- Migration 017")
    ]
    statements = SL.split_statements("\n".join(kept))
    if skip_statements_mentioning:
        # Whole statements, not lines: a later migration's table spans many lines and
        # pruning its header alone would leave orphan columns behind.
        statements = [
            statement
            for statement in statements
            if not any(marker in statement for marker in skip_statements_mentioning)
        ]
    conn = sqlite3.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _count(path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _has_key(path: Path) -> int:
    return _count(path, "SELECT COUNT(*) FROM policy_defs WHERE policy_key = ?", (FLOOR_MINOR,))


def _has_deny_v2(path: Path) -> int:
    return _count(
        path,
        "SELECT COUNT(*) FROM policy_revisions WHERE policy_key = ? AND version = 2",
        (DENY_KEY,),
    )


class TestMigrationRunner:
    def test_dry_run_reports_the_pending_migration_and_writes_nothing(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)
        assert _has_key(db) == 0

        result = _run("--dry-run", env={"VILLA_DB": str(db)})

        assert result.returncode == 0, result.stderr
        assert "would apply 003_policy_catalogue_and_deny_registry" in result.stdout
        assert "nothing was written" in result.stdout
        assert _has_key(db) == 0, "dry run must not touch the database"

    def test_applies_the_missing_catalogue_row(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode == 0, result.stderr
        assert _has_key(db) == 1

    def test_applies_the_deny_registry_amendment(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)
        assert _has_deny_v2(db) == 0

        _run(env={"VILLA_DB": str(db)})

        assert _has_deny_v2(db) == 1

    def test_second_run_changes_nothing(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)
        _run(env={"VILLA_DB": str(db)})
        defs_after_first = _count(db, "SELECT COUNT(*) FROM policy_defs")

        second = _run(env={"VILLA_DB": str(db)})

        assert second.returncode == 0, second.stderr
        assert "nothing to do" in second.stdout
        assert _count(db, "SELECT COUNT(*) FROM policy_defs") == defs_after_first

    def test_refuses_when_the_database_is_not_named(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)

        result = _run()  # no VILLA_DB in the environment

        assert result.returncode == 2
        assert "VILLA_DB" in result.stderr
        assert _has_key(db) == 0

    def test_recording_a_migration_does_not_make_apply_schema_refuse(self, tmp_path):
        """The trap this design avoids.

        ``applied_version()`` reads the newest ``schema_meta`` row and ``apply_schema``
        raises when it is not the build's ``SCHEMA_VERSION``. Recording migrations in
        ``schema_meta`` would therefore lock every migrated database. The bookkeeping lives
        in its own table so that cannot happen.
        """
        db = tmp_path / "old.db"
        _old_database(db)

        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        conn = SL.connect_sqlite(str(db))
        try:
            assert SL.applied_version(conn) == SL.SCHEMA_VERSION
            assert SL.apply_schema(conn) == SL.SCHEMA_VERSION  # must not raise
        finally:
            conn.close()

    def test_the_database_is_still_usable_by_the_kernel(self, tmp_path):
        db = tmp_path / "old.db"
        _old_database(db)
        _run(env={"VILLA_DB": str(db)})

        kernel = RK.ReservationKernel.open(str(db), agency_id="agency_default")
        kernel.close()


def _build_from_schema(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        for statement in SL.split_statements(SL.read_schema("sqlite")):
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _proof_objects(path: Path) -> list[tuple[str, str, str]]:
    """The table, its indexes and its triggers, as SQLite stored them."""
    conn = sqlite3.connect(str(path))
    try:
        return [
            (kind, name, " ".join((sql or "").split()))
            for kind, name, sql in conn.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE tbl_name = 'supplier_block_proofs' AND sql IS NOT NULL"
                " ORDER BY type, name"
            )
        ]
    finally:
        conn.close()


class TestSupplierBlockProofMigration:
    """019 must leave a migrated database identical to one built from the schema.

    ADR-0073's guarantees are structural: the unique index on evidence_id, the partial
    unique index on a live booking, and the trigger that refuses every UPDATE except
    revocation. If the migration produced a table without them, a migrated database would
    accept writes the fresh one refuses — and nothing else in the suite would notice.
    """

    def test_a_migrated_database_matches_a_fresh_one(self, tmp_path):
        migrated = tmp_path / "migrated.db"
        _old_database(migrated, skip_statements_mentioning=("supplier_block_proofs",))
        assert _proof_objects(migrated) == []

        result = _run(env={"VILLA_DB": str(migrated)})
        assert result.returncode == 0, result.stderr

        fresh = tmp_path / "fresh.db"
        _build_from_schema(fresh)

        assert _proof_objects(migrated) == _proof_objects(fresh)
        kinds = {kind for kind, _, _ in _proof_objects(migrated)}
        assert {"table", "index", "trigger"} <= kinds

    def test_the_migrated_table_refuses_a_repointed_proof(self, tmp_path):
        db = tmp_path / "migrated.db"
        _old_database(db, skip_statements_mentioning=("supplier_block_proofs",))
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO supplier_block_proofs (proof_id, booking_id, unit_id, check_in,"
                " check_out, supplier_id, relationship_id, evidence_id, verification_method,"
                " verified_by, verified_at, expires_at, created_at)"
                " VALUES ('p1', 'b1', 'u1', '2026-10-01', '2026-10-03', 's1', 'r1', 'e1',"
                " 'SUPPLIER_EMAIL', 'user:ops-1', '2026-09-01T00:00:00+00:00',"
                " '2026-12-31T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError) as ctx:
                conn.execute("UPDATE supplier_block_proofs SET booking_id = 'b2' WHERE proof_id = 'p1'")
            assert "immutable" in str(ctx.value)
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM supplier_block_proofs WHERE proof_id = 'p1'")
        finally:
            conn.close()


_MIGRATION_020_OBJECTS = (
    "supplier_contact",  # supplier_contacts, supplier_contact_events and their triggers
    "evidence_supplier_attributions",
    "ux_suppliers_agency_supplier",
    "ux_vsr_agency_supplier_rel",
    "ux_evidence_agency_unit",
    "ux_bookings_agency_booking",
    # G1: the parent keys and the event-kind/stay pins the attribution now relies on
    "uq_sce_event_contact_kind",
    "uq_bk_agency_stay",
    "ux_evidence_callback_contact",
)


def _objects_named(path: Path, markers: tuple[str, ...]) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            (kind, name, " ".join((sql or "").split()))
            for kind, name, sql in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            )
            if any(marker in name for marker in markers)
        ]
    finally:
        conn.close()


class TestSupplierContactsMigration:
    """020 (ADR-0074, PROPOSED): the migrated database must equal a fresh one, and must not
    touch a row that already exists. Applied here to disposable files only."""

    def test_a_migrated_database_matches_a_fresh_one(self, tmp_path):
        migrated = tmp_path / "migrated.db"
        _old_database(migrated, skip_statements_mentioning=_MIGRATION_020_OBJECTS)
        assert _objects_named(migrated, _MIGRATION_020_OBJECTS) == []

        result = _run(env={"VILLA_DB": str(migrated)})
        assert result.returncode == 0, result.stderr

        fresh = tmp_path / "fresh.db"
        _build_from_schema(fresh)
        got = _objects_named(migrated, _MIGRATION_020_OBJECTS)
        assert got == _objects_named(fresh, _MIGRATION_020_OBJECTS)
        assert {kind for kind, _, _ in got} >= {"table", "index", "trigger"}

    def test_migrating_twice_changes_nothing(self, tmp_path):
        db = tmp_path / "twice.db"
        _old_database(db, skip_statements_mentioning=_MIGRATION_020_OBJECTS)
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        once = _objects_named(db, _MIGRATION_020_OBJECTS)
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        assert _objects_named(db, _MIGRATION_020_OBJECTS) == once

    def test_legacy_evidence_is_left_exactly_as_it_was_and_unattributed(self, tmp_path):
        db = tmp_path / "legacy.db"
        _old_database(db, skip_statements_mentioning=_MIGRATION_020_OBJECTS)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
                " evidence_level, source_type, source_ref, captured_at, valid_until, result,"
                " occupancy_ranges, can_quote, can_hold, can_confirm, notes, created_at)"
                " VALUES ('legacy_1', 'agency_default', 'u1', 'B', 'manager_written', 'letter',"
                " '2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00', 'AVAILABLE', '[]',"
                " 1, 1, 1, NULL, '2026-09-01T00:00:00+00:00')"
            )
            conn.commit()
            before = conn.execute("SELECT * FROM availability_evidence").fetchall()
        finally:
            conn.close()

        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT * FROM availability_evidence").fetchall() == before
            assert conn.execute("SELECT COUNT(*) FROM evidence_supplier_attributions").fetchone()[0] == 0
        finally:
            conn.close()

    def test_the_migrated_database_enforces_the_attribution_pins(self, tmp_path):
        """G1: after 020 the pins must exist in the migrated file, not only in the source."""
        db = tmp_path / "pins.db"
        _old_database(db, skip_statements_mentioning=_MIGRATION_020_OBJECTS)
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            columns = {row[1]: row for row in conn.execute(
                "PRAGMA table_info(evidence_supplier_attributions)"
            )}
            assert columns["contact_event_kind"][3] == 1, "contact_event_kind must be NOT NULL"
            assert "VERIFIED" in (columns["contact_event_kind"][4] or ""), (
                "contact_event_kind must be CHECKed to VERIFIED, got "
                f"{columns['contact_event_kind'][4]!r}"
            )

            foreign_keys = list(conn.execute(
                "PRAGMA foreign_key_list(evidence_supplier_attributions)"
            ))
            targets = {row[2] for row in foreign_keys}
            assert {"supplier_contact_events", "bookings"} <= targets, targets
            by_parent: dict[str, list[dict]] = {}
            for row in foreign_keys:
                by_parent.setdefault(row[2], []).append(dict(zip(
                    ("id", "seq", "table", "from", "to", "on_update", "on_delete", "match"),
                    row,
                )))
            event_from = sorted(fk["from"] for fk in by_parent["supplier_contact_events"])
            assert event_from == ["contact_event_id", "contact_event_kind", "contact_id"], event_from
            stay_pairs = {(fk["from"], fk["to"]) for fk in by_parent["bookings"]}
            assert {
                ("agency_id", "agency_id"),
                ("booking_id", "booking_id"),
                ("unit_id", "unit_id"),
                ("block_start", "check_in"),
                ("block_end", "check_out"),
            } <= stay_pairs, stay_pairs

            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )}
            assert {"uq_sce_event_contact_kind", "uq_bk_agency_stay"} <= indexes
            assert list(conn.execute("PRAGMA foreign_key_check")) == []

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO evidence_supplier_attributions (evidence_id) VALUES ('bare')"
                )
        finally:
            conn.close()

"""Tests for the SQLite migration runner.

The migration exists because rows added to the initial schema never reach a database that
was already built — ``apply_schema`` refuses to re-apply the initial schema. These tests
build exactly that situation (a database missing the rows) and check that the runner fixes
it, that a second run is a no-op, and — the one that matters most — that recording a
migration does not make ``apply_schema`` refuse to open the database afterwards.
"""

from __future__ import annotations

import hashlib
import json
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


# --- G1-R: a database that predates 020 must upgrade, not refuse ------------------------
#
# The pre-G1 attribution table below is copied verbatim from
# `git show cced242:src/schema/001_initial.sql` — the commit before the G1 constraints.
# The fixture is built by *downgrading* a database made from today's schema to exactly
# that shape, and `test_the_fixture_really_predates_g1` proves the downgrade landed: no
# derived column, no contact_event_kind, and neither of the two new foreign keys.

_PRE_G1_ATTRIBUTIONS = """
CREATE TABLE evidence_supplier_attributions (
    evidence_id           TEXT    NOT NULL,
    agency_id             TEXT    NOT NULL,
    unit_id               TEXT    NOT NULL,
    booking_id            TEXT    NOT NULL,
    supplier_id           TEXT    NOT NULL,
    relationship_id       TEXT    NOT NULL,
    contact_id            TEXT    NOT NULL,
    contact_event_id      TEXT    NOT NULL,
    source_kind           TEXT    NOT NULL CHECK (source_kind IN ('CALLBACK_VERIFIED')),
    block_start           TEXT    NOT NULL,
    block_end             TEXT    NOT NULL,
    supplier_confirmed_at TEXT    NOT NULL,
    callback_at           TEXT    NOT NULL,
    callback_by           TEXT    NOT NULL CHECK (length(callback_by) >= 2),
    callback_role         TEXT    NOT NULL CHECK (callback_role IN ('reservation_manager')),
    recorded_at           TEXT    NOT NULL,
    CONSTRAINT pk_evidence_supplier_attributions PRIMARY KEY (evidence_id),
    CONSTRAINT fk_esa_evidence FOREIGN KEY (agency_id, evidence_id, unit_id)
        REFERENCES availability_evidence (agency_id, evidence_id, unit_id),
    CONSTRAINT fk_esa_booking FOREIGN KEY (agency_id, booking_id)
        REFERENCES bookings (agency_id, booking_id),
    CONSTRAINT fk_esa_supplier FOREIGN KEY (agency_id, supplier_id)
        REFERENCES suppliers (agency_id, supplier_id),
    CONSTRAINT fk_esa_relationship FOREIGN KEY (agency_id, supplier_id, relationship_id)
        REFERENCES villa_supplier_relationships (agency_id, supplier_id, villa_supplier_relationship_id),
    CONSTRAINT fk_esa_contact FOREIGN KEY (contact_id, agency_id, supplier_id)
        REFERENCES supplier_contacts (contact_id, agency_id, supplier_id),
    CONSTRAINT fk_esa_contact_event FOREIGN KEY (contact_event_id, contact_id)
        REFERENCES supplier_contact_events (event_id, contact_id),
    CONSTRAINT ck_esa_block_window CHECK (block_start < block_end),
    CONSTRAINT ck_esa_callback_after_confirmation CHECK (callback_at >= supplier_confirmed_at)
);
CREATE INDEX ix_esa_booking ON evidence_supplier_attributions (agency_id, booking_id);
CREATE TRIGGER trg_evidence_supplier_attributions_no_update
BEFORE UPDATE ON evidence_supplier_attributions
BEGIN
    SELECT RAISE(ABORT, 'evidence_supplier_attributions is append-only (UPDATE rejected)');
END;
CREATE TRIGGER trg_evidence_supplier_attributions_no_delete
BEFORE DELETE ON evidence_supplier_attributions
BEGIN
    SELECT RAISE(ABORT, 'evidence_supplier_attributions is append-only (DELETE rejected)');
END;
"""


def _mark_legacy_ledger(conn: sqlite3.Connection) -> None:
    """Record every migration except 020 — the ledger a pre-G1 database would have."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY,"
        " applied_at TEXT, description TEXT)"
    )
    versions = [
        p.stem for p in sorted(SL.SCHEMA_DIR.glob("*.sql"))
        if p.name != SL.DIALECT_FILES["sqlite"]
        and not p.name.endswith(".pg.sql")
        and p.name != "020_supplier_contacts.sql"
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO schema_migrations VALUES (?, '2026-11-01T09:00:00+00:00', 'legacy')",
        [(version,) for version in versions],
    )


def _pre_g1_database(path: Path) -> None:
    """Today's schema, downgraded in place to the pre-G1 shape of the two objects.

    The ledger records every migration except 020 — that is what a database from before
    G1 looks like: everything applied, and 020 still pending. Without the ledger the
    runner would try to re-apply the earlier migrations whose rows are already in the
    schema, which is a different (and unrealistic) situation.
    """
    _build_from_schema(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE evidence_supplier_attributions")
        conn.execute("DROP INDEX ux_evidence_callback_contact")
        conn.execute("ALTER TABLE availability_evidence DROP COLUMN callback_verified_contact")
        conn.executescript(_PRE_G1_ATTRIBUTIONS)
        _mark_legacy_ledger(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_g1r(conn: sqlite3.Connection) -> None:
    """Synthetic rows that exercise every object the upgrade touches. No real data."""
    stamp = "2026-11-01T09:00:00+00:00"
    conn.executescript(
        f"""
        INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)
        VALUES ('sup_g1r', 'agency_default', 'G1R Supplier', 'ACTIVE', '{stamp}', '{stamp}');
        INSERT INTO properties (property_id, agency_id, name, property_type, region, province,
            timezone, geo_precision, status, contract_pack_status, created_at, updated_at)
        VALUES ('prop_g1r', 'agency_default', 'G1R Villa', 'villa', 'Ubud', 'Bali',
            'Asia/Makassar', 'VERIFIED', 'SELLABLE', 'VERIFIED', '{stamp}', '{stamp}');
        INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms, bathrooms,
            base_capacity, max_guests, status, created_at, updated_at)
        VALUES ('unit_g1r', 'agency_default', 'prop_g1r', 'G1R Villa Whole', 'entire_villa',
            3, 3, 2, 6, 'ACTIVE', '{stamp}', '{stamp}');
        INSERT INTO villa_supplier_relationships (villa_supplier_relationship_id, agency_id,
            supplier_id, property_id, unit_id, status, created_at, updated_at)
        VALUES ('vsr_g1r', 'agency_default', 'sup_g1r', 'prop_g1r', 'unit_g1r', 'ACTIVE',
            '{stamp}', '{stamp}');
        INSERT INTO policy_versions (policy_version_id, agency_id, property_id, unit_scope,
            version, effective_from, effective_to, currency, deposit_pct, service_fee_pct,
            tax_pbjt_pct, max_discount_pct, quote_valid_hours, hold_ttl_minutes,
            max_hold_extensions, payload, fingerprint, source_doc_ref, approved_by, created_at)
        VALUES ('pv_g1r', 'agency_default', 'prop_g1r', 'unit', 1, '2026-01-01', '2027-01-01',
            'IDR', '0.20', '0.10', '0.10', '0.05', 24, 30, 1, '{{}}', '{'a' * 64}',
            'doc-g1r', 'owner-g1r', '{stamp}');
        INSERT INTO availability_evidence (evidence_id, agency_id, unit_id, evidence_level,
            source_type, source_ref, captured_at, valid_until, result, occupancy_ranges,
            can_quote, can_hold, can_confirm, created_at)
        VALUES ('ev_g1r_cb', 'agency_default', 'unit_g1r', 'B', 'manual_ops',
            'callback_verified:ctc_g1r', '2026-11-20T02:00:00+00:00',
            '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1, '{stamp}'),
        ('ev_g1r_note', 'agency_default', 'unit_g1r', 'B', 'manual_ops', 'letter-from-owner',
            '2026-11-20T02:10:00+00:00', '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]',
            1, 1, 1, '{stamp}'),
        ('ev_case_ok', 'agency_default', 'unit_g1r', 'B', 'manual_ops',
            'callback_verified:ctc_g1r', '2026-11-20T03:00:00+00:00',
            '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1, '{stamp}'),
        ('ev_case_wc', 'agency_default', 'unit_g1r', 'B', 'manual_ops',
            'callback_verified:ctc_g1r', '2026-11-20T03:00:00+00:00',
            '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1, '{stamp}'),
        ('ev_case_ws', 'agency_default', 'unit_g1r', 'B', 'manual_ops',
            'callback_verified:ctc_g1r', '2026-11-20T03:00:00+00:00',
            '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1, '{stamp}'),
        ('ev_case_wu', 'agency_default', 'unit_g1r', 'B', 'manual_ops',
            'callback_verified:ctc_g1r', '2026-11-20T03:00:00+00:00',
            '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1, '{stamp}');
        INSERT INTO bookings (booking_id, agency_id, villa_supplier_relationship_id, property_id,
            unit_id, check_in, check_out, nights, status, guest_ref, adults, children, infants,
            currency, policy_version_id, policy_fingerprint, total_minor, amount_paid_minor,
            payment_status, idempotency_key, created_at, updated_at)
        VALUES ('bk_g1r', 'agency_default', 'vsr_g1r', 'prop_g1r', 'unit_g1r', '2026-12-01',
            '2026-12-03', 2, 'PAID_IN_FULL', 'guest-g1r', 2, 0, 0, 'IDR', 'pv_g1r',
            '{'b' * 64}', 4000000, 4000000, 'CLEARED_FULL', 'idem-g1r-001', '{stamp}', '{stamp}');
        INSERT INTO supplier_contacts (contact_id, agency_id, supplier_id, channel,
            contact_value, registration_basis, created_by, created_role, created_at)
        VALUES ('ctc_g1r', 'agency_default', 'sup_g1r', 'PHONE', '+62 361 000 0002',
            'SIGNED_AGREEMENT', 'owner-g1r', 'owner', '{stamp}');
        INSERT INTO supplier_contact_events (event_id, agency_id, supplier_id, contact_id, seq,
            kind, actor, actor_role, contact_created_by, reason, occurred_at)
        VALUES ('evt_g1r', 'agency_default', 'sup_g1r', 'ctc_g1r', 1, 'VERIFIED', 'gm-g1r',
            'gm', 'owner-g1r', NULL, '{stamp}');
        INSERT INTO evidence_supplier_attributions (evidence_id, agency_id, unit_id, booking_id,
            supplier_id, relationship_id, contact_id, contact_event_id, source_kind, block_start,
            block_end, supplier_confirmed_at, callback_at, callback_by, callback_role, recorded_at)
        VALUES ('ev_g1r_cb', 'agency_default', 'unit_g1r', 'bk_g1r', 'sup_g1r', 'vsr_g1r',
            'ctc_g1r', 'evt_g1r', 'CALLBACK_VERIFIED', '2026-12-01', '2026-12-03',
            '2026-11-20T02:00:00+00:00', '2026-11-20T02:05:00+00:00', 'rm-g1r',
            'reservation_manager', '2026-11-20T02:06:00+00:00');
        """
    )


def _dump(path: Path) -> str:
    """A digest of every row and every object text — proof that nothing moved."""
    conn = sqlite3.connect(str(path))
    try:
        payload = []
        for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            " ORDER BY type, name"
        ):
            payload.append([kind, name, sql])
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ):
            rows = [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            payload.append([table, rows])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    finally:
        conn.close()


def _table_digest(path: Path, table: str, *, drop: tuple[str, ...] = ()) -> str:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")]
        rows = [
            {name: row[name] for name in columns if name not in drop}
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    finally:
        conn.close()


def _outcome(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> str:
    try:
        conn.execute(sql, args)
        return "ACCEPTED"
    except sqlite3.Error as error:
        return f"{type(error).__name__}: {error}"


def _attribution_sql(**overrides) -> tuple[str, tuple]:
    values = {
        "evidence": "ev_g1r_cb", "contact": "ctc_g1r", "event": "evt_g1r",
        "kind": "VERIFIED", "start": "2026-12-01", "end": "2026-12-03",
        "unit": "unit_g1r", "booking": "bk_g1r",
    }
    values.update(overrides)
    sql = (
        "INSERT INTO evidence_supplier_attributions (evidence_id, agency_id, unit_id,"
        " booking_id, supplier_id, relationship_id, contact_id, contact_event_id,"
        " contact_event_kind, source_kind, block_start, block_end, supplier_confirmed_at,"
        " callback_at, callback_by, callback_role, recorded_at) VALUES"
        " ('{evidence}', 'agency_default', '{unit}', '{booking}', 'sup_g1r', 'vsr_g1r',"
        " '{contact}', '{event}', '{kind}', 'CALLBACK_VERIFIED', '{start}', '{end}',"
        " '2026-11-20T02:00:00+00:00', '2026-11-20T02:05:00+00:00', 'rm-g1r',"
        " 'reservation_manager', '2026-11-21T00:00:00+00:00')"
    ).format(**values)
    return sql, ()


class TestG1RUpgradePath:
    """A pre-G1 database must upgrade in place, without losing a row or inventing one."""

    def test_the_fixture_really_predates_g1(self, tmp_path):
        db = tmp_path / "pre_g1.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            evidence_columns = {row[1] for row in conn.execute(
                "PRAGMA table_xinfo(availability_evidence)")}
            attribution_columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(evidence_supplier_attributions)")}
            parents = {(row[2], row[3], row[4]) for row in conn.execute(
                "PRAGMA foreign_key_list(evidence_supplier_attributions)")}
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
        finally:
            conn.close()
        assert "callback_verified_contact" not in evidence_columns
        assert "contact_event_kind" not in attribution_columns
        assert ("availability_evidence", "contact_id", "callback_verified_contact") not in parents
        assert ("bookings", "block_start", "check_in") not in parents
        assert "ux_evidence_callback_contact" not in indexes

    def test_T01_a_genuine_legacy_database_upgrades(self, tmp_path):
        db = tmp_path / "upgrade.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
        finally:
            conn.close()

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(str(db))
        try:
            generated = {row[1]: row for row in conn.execute(
                "PRAGMA table_xinfo(availability_evidence)")}
            assert generated["callback_verified_contact"][6] == 2, "expected a VIRTUAL column"
            derived = dict(conn.execute(
                "SELECT evidence_id, callback_verified_contact FROM availability_evidence"))
            assert derived["ev_g1r_cb"] == "ctc_g1r"
            assert derived["ev_g1r_note"] is None
            assert all(
                value == ("ctc_g1r" if key.startswith(("ev_case_", "ev_g1r_cb")) else None)
                for key, value in derived.items()
            ), derived
            carried = conn.execute(
                "SELECT contact_event_kind, callback_by, block_start, block_end"
                "  FROM evidence_supplier_attributions").fetchone()
            assert carried == ("VERIFIED", "rm-g1r", "2026-12-01", "2026-12-03")
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert list(conn.execute("PRAGMA foreign_key_check")) == []
        finally:
            conn.close()

    def test_T02_a_second_run_is_a_no_op(self, tmp_path):
        db = tmp_path / "twice.db"
        _pre_g1_database(db)
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        after_first = _dump(db)

        second = _run(env={"VILLA_DB": str(db)})

        assert second.returncode == 0, second.stderr
        assert "nothing to do" in second.stdout
        assert _dump(db) == after_first

    def test_T03_a_second_run_performs_no_unintended_writes(self, tmp_path):
        db = tmp_path / "writes.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
        finally:
            conn.close()
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        settled = _dump(db)
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        assert _dump(db) == settled

    def test_T04_an_unknown_damaged_state_fails_closed(self, tmp_path):
        db = tmp_path / "unknown.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
            # The evidence table is the anchor both G1 objects point at. Without it there
            # is no honest way to decide what the remaining shape means.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE availability_evidence")
            conn.commit()
        finally:
            conn.close()
        before = _dump(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "unknown or partially-migrated" in result.stderr
        assert _dump(db) == before, "a refused migration must not write"

    def test_T04b_a_contradictory_half_state_fails_closed(self, tmp_path):
        db = tmp_path / "contradictory.db"
        _build_from_schema(db)
        conn = sqlite3.connect(str(db))
        try:
            # An attribution table that already carries the new constraint, while the
            # column it must point at is gone: nothing can honestly be carried forward.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP INDEX ux_evidence_callback_contact")
            conn.execute("ALTER TABLE availability_evidence DROP COLUMN callback_verified_contact")
            _mark_legacy_ledger(conn)
            conn.commit()
        finally:
            conn.close()
        before = _dump(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "unknown or partially-migrated" in result.stderr
        assert _dump(db) == before

    def test_T04c_a_ledger_that_claims_020_is_done_fails_closed(self, tmp_path):
        db = tmp_path / "lying_ledger.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            # Nothing is pending any more, so this can only be caught by comparing the
            # ledger with the structure — which is the point of the guard.
            conn.execute(
                "INSERT INTO schema_migrations VALUES"
                " ('020_supplier_contacts', '2026-11-01T09:00:00+00:00', 'pretend')"
            )
            conn.commit()
        finally:
            conn.close()
        before = _dump(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "unknown or partially-migrated" in result.stderr
        assert _dump(db) == before

    def test_T05_an_unreadable_ledger_fails_closed(self, tmp_path):
        db = tmp_path / "bad_ledger.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("DROP TABLE schema_migrations")
            conn.execute("CREATE TABLE schema_migrations (applied TEXT)")
            conn.commit()
        finally:
            conn.close()

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "cannot read the migration ledger" in result.stderr

    def test_T06_historical_rows_survive_unchanged(self, tmp_path):
        db = tmp_path / "rows.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
        finally:
            conn.close()
        tables = ("suppliers", "villa_supplier_relationships", "bookings",
                  "supplier_block_proofs", "supplier_contacts", "supplier_contact_events")
        before = {table: _table_digest(db, table) for table in tables}
        attribution_before = _table_digest(
            db, "evidence_supplier_attributions", drop=("contact_event_kind",))
        evidence_before = _table_digest(
            db, "availability_evidence", drop=("callback_verified_contact",))
        chain_before = [tuple(row) for row in sqlite3.connect(str(db)).execute(
            "SELECT audit_event_id, prev_hash, hash FROM audit_events ORDER BY seq")]

        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        assert {table: _table_digest(db, table) for table in tables} == before
        assert _table_digest(
            db, "evidence_supplier_attributions", drop=("contact_event_kind",)) == attribution_before
        assert _table_digest(
            db, "availability_evidence", drop=("callback_verified_contact",)) == evidence_before
        conn = sqlite3.connect(str(db))
        try:
            assert [tuple(row) for row in conn.execute(
                "SELECT audit_event_id, prev_hash, hash FROM audit_events ORDER BY seq")] == chain_before
        finally:
            conn.close()

    def test_T07_foreign_key_integrity_holds_after_the_upgrade(self, tmp_path):
        db = tmp_path / "fk.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
        finally:
            conn.close()
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            assert list(conn.execute("PRAGMA foreign_key_check")) == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_T08_triggers_and_indexes_remain_present_and_functional(self, tmp_path):
        db = tmp_path / "objects.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            conn.commit()
        finally:
            conn.close()
        assert _run(env={"VILLA_DB": str(db)}).returncode == 0

        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            triggers = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND tbl_name = 'evidence_supplier_attributions'")}
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            assert {"trg_evidence_supplier_attributions_no_update",
                    "trg_evidence_supplier_attributions_no_delete"} <= triggers
            assert {"ux_evidence_callback_contact", "uq_sce_event_contact_kind",
                    "uq_bk_agency_stay"} <= indexes
            assert "append-only" in _outcome(
                conn, "UPDATE evidence_supplier_attributions SET callback_by = 'x1'")
            assert "append-only" in _outcome(
                conn, "DELETE FROM evidence_supplier_attributions")
            # the one-VERIFIED-contact index still refuses a second live verification
            assert "UNIQUE" in _outcome(
                conn,
                "INSERT INTO supplier_contact_events (event_id, agency_id, supplier_id,"
                " contact_id, seq, kind, actor, actor_role, contact_created_by, reason,"
                " occurred_at) VALUES ('evt_g1r_2', 'agency_default', 'sup_g1r', 'ctc_g1r',"
                " 2, 'VERIFIED', 'gm-g1r', 'gm', 'owner-g1r', NULL,"
                " '2026-11-02T00:00:00+00:00')",
            )
        finally:
            conn.close()

    def test_T09_T13_the_migrated_database_behaves_like_a_fresh_one(self, tmp_path):
        migrated = tmp_path / "migrated.db"
        fresh = tmp_path / "fresh.db"
        _pre_g1_database(migrated)
        _build_from_schema(fresh)
        for path in (migrated, fresh):
            conn = sqlite3.connect(str(path))
            try:
                _seed_g1r(conn)
                conn.commit()
            finally:
                conn.close()
        assert _run(env={"VILLA_DB": str(migrated)}).returncode == 0

        def matrix(path: Path) -> dict[str, str]:
            conn = sqlite3.connect(str(path))
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                outcomes = {
                    "valid": _outcome(conn, *_attribution_sql(evidence="ev_case_ok")),
                    "wrong_contact": _outcome(conn, *_attribution_sql(
                        evidence="ev_case_wc", contact="ctc_absent")),
                    "wrong_event_kind": _outcome(conn, *_attribution_sql(
                        evidence="ev_case_wc", kind="SUSPENDED")),
                    "wrong_stay": _outcome(conn, *_attribution_sql(
                        evidence="ev_case_ws", start="2026-12-02")),
                    "wrong_unit": _outcome(conn, *_attribution_sql(
                        evidence="ev_case_wu", unit="unit_absent")),
                    "non_callback_capture": _outcome(
                        conn, *_attribution_sql(evidence="ev_g1r_note")),
                    "duplicate": _outcome(conn, *_attribution_sql(evidence="ev_g1r_cb")),
                    "update": _outcome(
                        conn, "UPDATE evidence_supplier_attributions SET callback_by = 'x1'"),
                    "delete": _outcome(conn, "DELETE FROM evidence_supplier_attributions"),
                }
            finally:
                conn.close()
            return outcomes

        migrated_outcomes = matrix(migrated)
        assert migrated_outcomes == matrix(fresh), migrated_outcomes
        assert "FOREIGN KEY" in migrated_outcomes["wrong_contact"]
        assert "CHECK" in migrated_outcomes["wrong_event_kind"]
        assert "FOREIGN KEY" in migrated_outcomes["wrong_stay"]
        assert "FOREIGN KEY" in migrated_outcomes["wrong_unit"]
        assert "append-only" in migrated_outcomes["update"]
        assert migrated_outcomes["valid"] == "ACCEPTED"
        assert "UNIQUE" in migrated_outcomes["duplicate"]

        # The rebuild drops the attribution table, so every object 020 puts back must
        # actually be back — compared by name against a database that never needed it.
        def objects(path: Path) -> set[str]:
            conn = sqlite3.connect(str(path))
            try:
                return {
                    f"{kind}:{name}" for kind, name in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
                    if name != "schema_migrations"
                }
            finally:
                conn.close()

        assert objects(migrated) == objects(fresh), (
            objects(fresh) - objects(migrated), objects(migrated) - objects(fresh))

    def test_T12b_an_attribution_outside_the_booking_stay_is_refused(self, tmp_path):
        db = tmp_path / "wrong_stay.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            # Same contact and same capture, but a window the booking never held: carrying
            # this row would assert a stay nobody booked.
            conn.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
                " evidence_level, source_type, source_ref, captured_at, valid_until, result,"
                " occupancy_ranges, can_quote, can_hold, can_confirm, created_at) VALUES"
                " ('ev_g1r_wrongstay', 'agency_default', 'unit_g1r', 'B', 'manual_ops',"
                " 'callback_verified:ctc_g1r', '2026-11-20T02:00:00+00:00',"
                " '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1,"
                " '2026-11-01T09:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO evidence_supplier_attributions (evidence_id, agency_id, unit_id,"
                " booking_id, supplier_id, relationship_id, contact_id, contact_event_id,"
                " source_kind, block_start, block_end, supplier_confirmed_at, callback_at,"
                " callback_by, callback_role, recorded_at) VALUES ('ev_g1r_wrongstay',"
                " 'agency_default', 'unit_g1r', 'bk_g1r', 'sup_g1r', 'vsr_g1r', 'ctc_g1r',"
                " 'evt_g1r', 'CALLBACK_VERIFIED', '2026-12-02', '2026-12-04',"
                " '2026-11-20T02:00:00+00:00', '2026-11-20T02:05:00+00:00', 'rm-g1r',"
                " 'reservation_manager', '2026-11-20T02:06:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        before = _dump(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "inventing authority" in result.stderr
        assert _dump(db) == before

    def test_T12_a_mid_migration_failure_rolls_back(self, tmp_path):
        db = tmp_path / "rollback.db"
        _pre_g1_database(db)
        conn = sqlite3.connect(str(db))
        try:
            _seed_g1r(conn)
            # An attribution whose capture names somebody else: carrying it would mean
            # inventing a contact that the evidence never verified.
            conn.execute(
                "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
                " evidence_level, source_type, source_ref, captured_at, valid_until, result,"
                " occupancy_ranges, can_quote, can_hold, can_confirm, created_at) VALUES"
                " ('ev_g1r_other', 'agency_default', 'unit_g1r', 'B', 'manual_ops',"
                " 'callback_verified:ctc_someone_else', '2026-11-20T02:00:00+00:00',"
                " '2026-12-05T00:00:00+00:00', 'AVAILABLE', '[]', 1, 1, 1,"
                " '2026-11-01T09:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO evidence_supplier_attributions (evidence_id, agency_id, unit_id,"
                " booking_id, supplier_id, relationship_id, contact_id, contact_event_id,"
                " source_kind, block_start, block_end, supplier_confirmed_at, callback_at,"
                " callback_by, callback_role, recorded_at) VALUES ('ev_g1r_other',"
                " 'agency_default', 'unit_g1r', 'bk_g1r', 'sup_g1r', 'vsr_g1r', 'ctc_g1r',"
                " 'evt_g1r', 'CALLBACK_VERIFIED', '2026-12-01', '2026-12-03',"
                " '2026-11-20T02:00:00+00:00', '2026-11-20T02:05:00+00:00', 'rm-g1r',"
                " 'reservation_manager', '2026-11-20T02:06:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        before = _dump(db)

        result = _run(env={"VILLA_DB": str(db)})

        assert result.returncode != 0
        assert "inventing authority" in result.stderr
        assert _dump(db) == before, "a refused upgrade must leave the database untouched"
        conn = sqlite3.connect(str(db))
        try:
            assert not [row for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'evidence_supplier_attributions__%'")]
            assert "callback_verified_contact" not in {
                row[1] for row in conn.execute("PRAGMA table_xinfo(availability_evidence)")}
            assert "020_supplier_contacts" not in {
                row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        finally:
            conn.close()


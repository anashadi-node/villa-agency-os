-- Migration 019: supplier_block_proofs (ADR-0073).
--
-- 001_initial.sql creates this table for a new database; this file exists for databases
-- that already exist, because schema_loader.apply_schema refuses to re-apply the initial
-- schema. The DDL below is copied verbatim from the initial schema so the two can never
-- drift: a migrated database and a fresh one must be structurally identical, and
-- tests/test_schema.py compares them.
--
-- Additive only. Nothing is dropped, renamed or backfilled, and no existing row is touched:
-- a database without proofs keeps working and simply cannot confirm a booking until a proof
-- is recorded. That is the intended fail-closed direction — a legacy booking that was
-- confirmed under the old unit-level rule keeps its status and its audit trail, because
-- rewriting historical facts is not a migration's job.

CREATE TABLE IF NOT EXISTS supplier_block_proofs (
    proof_id            TEXT    NOT NULL,
    agency_id           TEXT    NOT NULL DEFAULT 'agency_default',
    booking_id          TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    check_in            TEXT    NOT NULL,
    check_out           TEXT    NOT NULL,
    supplier_id         TEXT    NOT NULL,
    relationship_id     TEXT    NOT NULL,
    evidence_id         TEXT    NOT NULL,
    evidence_object_id  TEXT,
    verification_method TEXT    NOT NULL CHECK (verification_method IN (
                            'SUPPLIER_PORTAL','SUPPLIER_API','SUPPLIER_EMAIL',
                            'MANAGER_CALL','MANAGER_WRITTEN')),
    verified_by         TEXT    NOT NULL CHECK (length(verified_by) >= 3),
    verified_at         TEXT    NOT NULL,
    expires_at          TEXT    NOT NULL,
    notes               TEXT,
    revoked_at          TEXT,
    revoked_by          TEXT,
    revoke_reason       TEXT,
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_supplier_block_proofs PRIMARY KEY (proof_id),
    CONSTRAINT fk_sbp_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_sbp_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT fk_sbp_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_sbp_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_sbp_relationship FOREIGN KEY (relationship_id)
        REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT fk_sbp_evidence FOREIGN KEY (evidence_id)
        REFERENCES availability_evidence (evidence_id),
    CONSTRAINT fk_sbp_evidence_object FOREIGN KEY (evidence_object_id)
        REFERENCES evidence_objects (evidence_id),
    CONSTRAINT ck_sbp_stay_window CHECK (check_in < check_out),
    CONSTRAINT ck_sbp_expiry_after_verification CHECK (expires_at > verified_at),
    CONSTRAINT ck_sbp_revocation_complete CHECK (
        revoked_at IS NULL OR (revoked_by IS NOT NULL AND length(revoke_reason) >= 3)),
    CONSTRAINT uq_sbp_evidence UNIQUE (evidence_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_sbp_active_booking ON supplier_block_proofs (booking_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_sbp_booking ON supplier_block_proofs (agency_id, booking_id);

CREATE INDEX IF NOT EXISTS ix_sbp_unit_window ON supplier_block_proofs (agency_id, unit_id, check_in, check_out);

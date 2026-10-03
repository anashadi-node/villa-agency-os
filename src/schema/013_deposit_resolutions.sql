-- Migration 013: §30's security-deposit resolution fact (ADR-0055).
--
-- 001_initial.sql creates this table when the database file is *created*, so a fresh
-- environment and a migrated one hold the same shape. A database built before today
-- cannot gain the table by being reopened: schema_loader.apply_schema refuses to
-- re-apply the initial schema ("write a migration instead of re-applying the initial
-- schema"). Every statement here is idempotent — running twice changes nothing.
--
-- What this table owns. §30 refuses to close a booking that still holds a security
-- deposit until the deposit's resolution is a recorded fact: returned, forfeited or
-- applied, decided by a person, carried by evidence. Before this table the gate could
-- only fail closed by name ("no module records the deposit's resolution"); the row is
-- that owner. Append-only triggers make a resolution something to prove, not to edit;
-- `idempotency_key` makes a retry of the same decision the same fact.

CREATE TABLE IF NOT EXISTS deposit_resolutions (
    deposit_resolution_id TEXT NOT NULL,
    agency_id             TEXT NOT NULL DEFAULT 'agency_default',
    booking_id            TEXT NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('RETURNED','FORFEITED','APPLIED')),
    amount_minor          INTEGER NOT NULL CHECK (amount_minor > 0),
    currency              TEXT NOT NULL CHECK (length(currency) = 3),
    decided_by            TEXT NOT NULL,
    actor_role            TEXT NOT NULL,
    evidence_ref          TEXT NOT NULL CHECK (length(evidence_ref) >= 3),
    note                  TEXT,
    idempotency_key       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    CONSTRAINT pk_deposit_resolutions PRIMARY KEY (deposit_resolution_id),
    CONSTRAINT uq_deposit_resolutions_idem UNIQUE (agency_id, idempotency_key),
    CONSTRAINT fk_deposit_resolution_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_deposit_resolution_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id)
);

CREATE INDEX IF NOT EXISTS ix_deposit_resolutions_booking
    ON deposit_resolutions (agency_id, booking_id);

CREATE TRIGGER IF NOT EXISTS trg_deposit_resolutions_no_update
BEFORE UPDATE ON deposit_resolutions
BEGIN
    SELECT RAISE(ABORT, 'deposit_resolutions is append-only (UPDATE rejected)');
END;

CREATE TRIGGER IF NOT EXISTS trg_deposit_resolutions_no_delete
BEFORE DELETE ON deposit_resolutions
BEGIN
    SELECT RAISE(ABORT, 'deposit_resolutions is append-only (DELETE rejected)');
END;
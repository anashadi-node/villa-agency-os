-- Migration 014: §30's supplier settlement fact (ADR-0056).
--
-- 001_initial.sql creates this table when the database file is *created*, so a fresh
-- environment and a migrated one hold the same shape. A database built before today
-- cannot gain the table by being reopened: schema_loader.apply_schema refuses to
-- re-apply the initial schema ("write a migration instead of re-applying the initial
-- schema"). Every statement here is idempotent — running twice changes nothing.
--
-- What this table owns. §30 refuses to close a booking whose agency<->supplier money is
-- unreconciled: `DUE` declares what the supplier is owed (with the contract/SOP as the
-- evidence), `PAID` records the parts that settled it. Append-only, human-decided,
-- evidence-carried — the same contract ADR-0055 set for the deposit's resolution.

-- §30's supplier settlement fact (ADR-0056): what the agency owes — and has paid — the
-- supplier for one booking, decided by a person and carried by evidence. Append-only like
-- every §30 fact; `action` carries both halves of the reconciliation: `DUE` declares what
-- the supplier is owed (the contract's consequence, named once), `PAID` records the parts
-- that settled it. The gate re-checks the arithmetic the writer enforces.
CREATE TABLE IF NOT EXISTS supplier_settlements (
    supplier_settlement_id TEXT NOT NULL,
    agency_id              TEXT NOT NULL DEFAULT 'agency_default',
    booking_id             TEXT NOT NULL,
    route_id               TEXT NOT NULL,
    action                 TEXT NOT NULL CHECK (action IN ('DUE','PAID')),
    amount_minor           INTEGER NOT NULL CHECK (amount_minor > 0),
    currency               TEXT NOT NULL CHECK (length(currency) = 3),
    decided_by             TEXT NOT NULL,
    actor_role             TEXT NOT NULL,
    evidence_ref           TEXT NOT NULL CHECK (length(evidence_ref) >= 3),
    note                   TEXT,
    idempotency_key        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    CONSTRAINT pk_supplier_settlements PRIMARY KEY (supplier_settlement_id),
    CONSTRAINT uq_supplier_settlements_idem UNIQUE (agency_id, idempotency_key),
    CONSTRAINT fk_supplier_settlement_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_supplier_settlement_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id)
);
CREATE INDEX IF NOT EXISTS ix_supplier_settlements_booking ON supplier_settlements (agency_id, booking_id);
CREATE TRIGGER IF NOT EXISTS trg_supplier_settlements_no_update
BEFORE UPDATE ON supplier_settlements
BEGIN
    SELECT RAISE(ABORT, 'supplier_settlements is append-only (UPDATE rejected)');
END;
CREATE TRIGGER IF NOT EXISTS trg_supplier_settlements_no_delete
BEFORE DELETE ON supplier_settlements
BEGIN
    SELECT RAISE(ABORT, 'supplier_settlements is append-only (DELETE rejected)');
END;


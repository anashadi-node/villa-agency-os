-- Migration 020: supplier contacts and evidence attribution (ADR-0074, PROPOSED).
--
-- 001_initial.sql carries the same DDL for a new database; this file is for databases that
-- already exist. Copied verbatim apart from IF NOT EXISTS so tests/test_migrations.py can
-- compare a migrated database with a fresh one. NOT applied to any operational database by
-- E5: applying it is a separate, explicit approval.
--
-- ADR-0074 (PROPOSED), E5 data contract: who a supplier is on the phone, and which
-- booking-specific block assertion a signed-in human attested.
--
-- Additive on purpose. availability_evidence is NOT altered: SQLite cannot add a composite
-- foreign key with ALTER TABLE, and nullable composite keys would let an incomplete
-- reference through (MATCH SIMPLE skips the check when any column is NULL). Attribution
-- therefore lives in its own table whose every reference column is NOT NULL. An evidence
-- row with no attribution row is "unattributed": legacy evidence stays readable and
-- bit-for-bit unchanged, and it can never authorise a NEW confirmation once enforcement is on.
--
-- Parent-key indexes so child rows can reference (agency, id) pairs. They are redundant
-- with the single-column primary keys on purpose: they let a composite foreign key make a
-- cross-tenant link structurally impossible.
CREATE UNIQUE INDEX IF NOT EXISTS ux_suppliers_agency_supplier ON suppliers (agency_id, supplier_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vsr_agency_supplier_rel
    ON villa_supplier_relationships (agency_id, supplier_id, villa_supplier_relationship_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_agency_unit ON availability_evidence (agency_id, evidence_id, unit_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_agency_booking ON bookings (agency_id, booking_id);

-- A registered way to reach a supplier. The number is stored at registration and never
-- edited: the callback goes to THIS number, not to one quoted in an inbound message.
-- `registration_basis` is a closed list, so a free-text note cannot establish authority.
CREATE TABLE IF NOT EXISTS supplier_contacts (
    contact_id         TEXT    NOT NULL,
    agency_id          TEXT    NOT NULL,
    supplier_id        TEXT    NOT NULL,
    channel            TEXT    NOT NULL CHECK (channel IN ('PHONE')),
    contact_value      TEXT    NOT NULL CHECK (length(contact_value) >= 6),
    registration_basis TEXT    NOT NULL CHECK (registration_basis IN ('SIGNED_AGREEMENT','OWNER_ATTESTED')),
    created_by         TEXT    NOT NULL CHECK (length(created_by) >= 2),
    created_role       TEXT    NOT NULL CHECK (created_role IN ('owner','gm')),
    created_at         TEXT    NOT NULL,
    revoked_at         TEXT,
    revoked_by         TEXT,
    revoke_reason      TEXT,
    CONSTRAINT pk_supplier_contacts PRIMARY KEY (contact_id),
    CONSTRAINT fk_sc_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_sc_supplier FOREIGN KEY (agency_id, supplier_id)
        REFERENCES suppliers (agency_id, supplier_id),
    CONSTRAINT ck_sc_revocation_complete CHECK (
        revoked_at IS NULL OR (revoked_by IS NOT NULL AND length(revoke_reason) >= 3)),
    CONSTRAINT uq_sc_scope UNIQUE (contact_id, agency_id, supplier_id),
    CONSTRAINT uq_sc_maker UNIQUE (contact_id, created_by)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sc_live_value ON supplier_contacts (agency_id, supplier_id, channel, contact_value)
    WHERE revoked_at IS NULL;

-- twin:revoke-only
CREATE TRIGGER IF NOT EXISTS trg_supplier_contacts_revoke_only
BEFORE UPDATE ON supplier_contacts
BEGIN
    SELECT RAISE(ABORT, 'supplier_contacts is immutable except revocation')
    WHERE NOT (
        OLD.revoked_at IS NULL
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoked_by IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.contact_id IS OLD.contact_id
        AND NEW.agency_id IS OLD.agency_id
        AND NEW.supplier_id IS OLD.supplier_id
        AND NEW.channel IS OLD.channel
        AND NEW.contact_value IS OLD.contact_value
        AND NEW.registration_basis IS OLD.registration_basis
        AND NEW.created_by IS OLD.created_by
        AND NEW.created_role IS OLD.created_role
        AND NEW.created_at IS OLD.created_at
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_supplier_contacts_no_delete
BEFORE DELETE ON supplier_contacts
BEGIN
    SELECT RAISE(ABORT, 'supplier_contacts is append-only (DELETE rejected)');
END;

-- Verification history, append-only. The contact's current standing is its latest event
-- (highest seq). Maker/checker separation is structural: every event repeats the contact's
-- creator through a composite foreign key, so the CHECK below can compare actor and maker
-- without a trigger. Two checkers racing to verify the same contact collide on
-- uq_sce_seq / ux_sce_one_verified instead of both succeeding.
CREATE TABLE IF NOT EXISTS supplier_contact_events (
    event_id           TEXT    NOT NULL,
    agency_id          TEXT    NOT NULL,
    supplier_id        TEXT    NOT NULL,
    contact_id         TEXT    NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 1),
    kind               TEXT    NOT NULL CHECK (kind IN ('VERIFIED','SUSPENDED','REINSTATED')),
    actor              TEXT    NOT NULL CHECK (length(actor) >= 2),
    actor_role         TEXT    NOT NULL CHECK (actor_role IN ('owner','gm')),
    contact_created_by TEXT    NOT NULL,
    reason             TEXT,
    occurred_at        TEXT    NOT NULL,
    CONSTRAINT pk_supplier_contact_events PRIMARY KEY (event_id),
    CONSTRAINT fk_sce_contact FOREIGN KEY (contact_id, agency_id, supplier_id)
        REFERENCES supplier_contacts (contact_id, agency_id, supplier_id),
    CONSTRAINT fk_sce_maker FOREIGN KEY (contact_id, contact_created_by)
        REFERENCES supplier_contacts (contact_id, created_by),
    CONSTRAINT ck_sce_checker_is_not_maker CHECK (kind = 'SUSPENDED' OR actor <> contact_created_by),
    CONSTRAINT ck_sce_reason CHECK (kind = 'VERIFIED' OR (reason IS NOT NULL AND length(reason) >= 3)),
    CONSTRAINT uq_sce_seq UNIQUE (contact_id, seq),
    CONSTRAINT uq_sce_event_contact UNIQUE (event_id, contact_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sce_one_verified ON supplier_contact_events (contact_id) WHERE kind = 'VERIFIED';

CREATE TRIGGER IF NOT EXISTS trg_supplier_contact_events_no_update
BEFORE UPDATE ON supplier_contact_events
BEGIN
    SELECT RAISE(ABORT, 'supplier_contact_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER IF NOT EXISTS trg_supplier_contact_events_no_delete
BEFORE DELETE ON supplier_contact_events
BEGIN
    SELECT RAISE(ABORT, 'supplier_contact_events is append-only (DELETE rejected)');
END;

-- One booking-specific supplier assertion per evidence row, written once. Every reference
-- is NOT NULL and tenant-scoped, so the row cannot name another agency's supplier, contact,
-- relationship, evidence or booking. The callback is a human attestation, not
-- cryptographic authentication: `callback_by` is the signed-in reservation_manager who
-- phoned the registered number, and the kernel verified that identity, role binding and
-- step-up before this row existed.
CREATE TABLE IF NOT EXISTS evidence_supplier_attributions (
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
CREATE INDEX IF NOT EXISTS ix_esa_booking ON evidence_supplier_attributions (agency_id, booking_id);

CREATE TRIGGER IF NOT EXISTS trg_evidence_supplier_attributions_no_update
BEFORE UPDATE ON evidence_supplier_attributions
BEGIN
    SELECT RAISE(ABORT, 'evidence_supplier_attributions is append-only (UPDATE rejected)');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_supplier_attributions_no_delete
BEFORE DELETE ON evidence_supplier_attributions
BEGIN
    SELECT RAISE(ABORT, 'evidence_supplier_attributions is append-only (DELETE rejected)');
END;

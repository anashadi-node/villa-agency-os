-- Villa Agency OS — initial schema (PostgreSQL, production dialect).
-- GENERATED from src/schema/001_initial.sql by scripts/generate_pg_twin.py.
-- Do not edit by hand: edit the SQLite source, then run
--     python3 scripts/generate_pg_twin.py --write
--
-- Semantic twin of 001_initial.sql. The complete list of dialect deltas:
--   * INTEGER -> BIGINT. Both map to the "int" family in the parity model;
--     PostgreSQL production uses 64-bit integers for money and counters.
--   * Append-only guards: each SQLite RAISE(ABORT) trigger becomes one trigger
--     with the SAME name, table and event, calling the shared plpgsql function
--     villa_reject_mutation(). The function is created once, before the first
--     trigger.
--   * "PRAGMA foreign_keys = ON" is SQLite-only (PostgreSQL enforces foreign
--     keys natively).
-- Everything else — tables, column order, nullability, defaults, primary keys,
-- unique constraints, foreign keys, CHECK constraint names and expressions,
-- enum value sets, indexes and partial-index predicates — is byte-identical
-- text, and tests/test_schema.py fails when it is not.;

-- --------------------------------------------------------------------------
-- Meta
-- --------------------------------------------------------------------------
CREATE TABLE schema_meta (
    schema_version  TEXT    NOT NULL,
    applied_at      TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    CONSTRAINT pk_schema_meta PRIMARY KEY (schema_version)
);

-- --------------------------------------------------------------------------
-- Inventory: properties & units
-- --------------------------------------------------------------------------
CREATE TABLE properties (
    property_id   TEXT    NOT NULL,
    name          TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    property_type TEXT    NOT NULL CHECK (property_type IN ('villa','resort','guesthouse','apartment','homestay')),
    region        TEXT    NOT NULL CHECK (length(region) >= 2),
    province      TEXT    NOT NULL CHECK (province IN ('Bali','DI Yogyakarta')),
    timezone      TEXT    NOT NULL CHECK (length(timezone) >= 3),
    status        TEXT    NOT NULL CHECK (status IN ('SELLABLE','SELLABLE_WITH_REVIEW','NOT_SELLABLE','SUSPENDED')),
    contract_pack_status TEXT NOT NULL CHECK (contract_pack_status IN ('DRAFT','VERIFIED','ACTIVATED','EXPIRED')),
    contract_pack_evaluated_at TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    CONSTRAINT pk_properties PRIMARY KEY (property_id)
);

CREATE INDEX ix_properties_status ON properties (status);

CREATE TABLE units (
    unit_id      TEXT    NOT NULL,
    property_id  TEXT    NOT NULL,
    name         TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    unit_type    TEXT    NOT NULL CHECK (unit_type IN ('entire_villa','suite','room','bungalow','compound_unit')),
    bedrooms     BIGINT NOT NULL CHECK (bedrooms >= 0),
    bathrooms    BIGINT NOT NULL CHECK (bathrooms >= 0),
    base_capacity BIGINT NOT NULL CHECK (base_capacity >= 1),
    max_guests   BIGINT NOT NULL CHECK (max_guests >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE','MAINTENANCE','SUSPENDED')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_units PRIMARY KEY (unit_id),
    CONSTRAINT fk_units_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT ck_units_capacity CHECK (max_guests >= base_capacity)
);

CREATE INDEX ix_units_property ON units (property_id, status);

-- --------------------------------------------------------------------------
-- Policy versions (append-only business rules)
-- --------------------------------------------------------------------------
CREATE TABLE policy_versions (
    policy_version_id   TEXT    NOT NULL,
    property_id         TEXT    NOT NULL,
    unit_scope          TEXT    NOT NULL,
    version             BIGINT NOT NULL CHECK (version >= 1),
    effective_from      TEXT    NOT NULL,
    effective_to        TEXT    NOT NULL,
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    deposit_pct         TEXT    NOT NULL,
    service_fee_pct     TEXT    NOT NULL,
    tax_pbjt_pct        TEXT    NOT NULL,
    max_discount_pct    TEXT    NOT NULL,
    quote_valid_hours   BIGINT NOT NULL CHECK (quote_valid_hours BETWEEN 1 AND 168),
    hold_ttl_minutes    BIGINT NOT NULL CHECK (hold_ttl_minutes BETWEEN 15 AND 1440),
    max_hold_extensions BIGINT NOT NULL CHECK (max_hold_extensions BETWEEN 0 AND 3),
    payload             TEXT    NOT NULL,
    fingerprint         TEXT    NOT NULL CHECK (length(fingerprint) = 64),
    source_doc_ref      TEXT    NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by         TEXT    NOT NULL CHECK (length(approved_by) >= 1),
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_policy_versions PRIMARY KEY (policy_version_id),
    CONSTRAINT fk_policy_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT uq_policy_version UNIQUE (property_id, unit_scope, version),
    CONSTRAINT ck_policy_dates CHECK (effective_from <= effective_to),
    CONSTRAINT ck_policy_deposit CHECK (CAST(deposit_pct AS REAL) >= 0 AND CAST(deposit_pct AS REAL) <= 100),
    CONSTRAINT ck_policy_service_fee CHECK (CAST(service_fee_pct AS REAL) >= 0 AND CAST(service_fee_pct AS REAL) <= 25),
    CONSTRAINT ck_policy_tax_pbjt CHECK (CAST(tax_pbjt_pct AS REAL) >= 0 AND CAST(tax_pbjt_pct AS REAL) <= 10),
    CONSTRAINT ck_policy_max_discount CHECK (CAST(max_discount_pct AS REAL) >= 0 AND CAST(max_discount_pct AS REAL) <= 50),
    CONSTRAINT ck_policy_scope CHECK (unit_scope IN ('property','unit'))
);

CREATE INDEX ix_policy_lookup ON policy_versions (property_id, effective_from, effective_to);

-- --------------------------------------------------------------------------
-- Rate cards & seasons
-- --------------------------------------------------------------------------
CREATE TABLE rate_cards (
    rate_card_id        TEXT    NOT NULL,
    property_id         TEXT    NOT NULL,
    unit_scope          TEXT    NOT NULL,
    name                TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    version             BIGINT NOT NULL CHECK (version >= 1),
    effective_from      TEXT    NOT NULL,
    effective_to        TEXT    NOT NULL,
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    weekly_discount_pct TEXT    NOT NULL,
    monthly_discount_pct TEXT   NOT NULL,
    source_doc_ref      TEXT    NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by         TEXT    NOT NULL CHECK (length(approved_by) >= 1),
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_rate_cards PRIMARY KEY (rate_card_id),
    CONSTRAINT fk_rate_cards_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT uq_rate_card_version UNIQUE (property_id, unit_scope, version),
    CONSTRAINT ck_rate_card_dates CHECK (effective_from <= effective_to),
    CONSTRAINT ck_rate_card_weekly CHECK (CAST(weekly_discount_pct AS REAL) >= 0 AND CAST(weekly_discount_pct AS REAL) <= 50),
    CONSTRAINT ck_rate_card_monthly CHECK (CAST(monthly_discount_pct AS REAL) >= 0 AND CAST(monthly_discount_pct AS REAL) <= 60)
);

CREATE INDEX ix_rate_cards_lookup ON rate_cards (property_id, effective_from, effective_to);

CREATE TABLE rate_seasons (
    season_id         TEXT    NOT NULL,
    rate_card_id      TEXT    NOT NULL,
    name              TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    start_date        TEXT    NOT NULL,
    end_date          TEXT    NOT NULL,
    nightly_rate_minor BIGINT NOT NULL CHECK (nightly_rate_minor >= 0),
    min_stay_nights   BIGINT NOT NULL CHECK (min_stay_nights >= 1),
    priority          BIGINT NOT NULL CHECK (priority >= 0),
    created_at        TEXT    NOT NULL,
    CONSTRAINT pk_rate_seasons PRIMARY KEY (season_id),
    CONSTRAINT fk_rate_seasons_card FOREIGN KEY (rate_card_id) REFERENCES rate_cards (rate_card_id) ON DELETE CASCADE,
    CONSTRAINT ck_season_dates CHECK (start_date <= end_date)
);

CREATE INDEX ix_rate_seasons_card ON rate_seasons (rate_card_id, start_date);

-- --------------------------------------------------------------------------
-- Availability evidence (the only thing that may authorise a hold/confirmation)
-- --------------------------------------------------------------------------
CREATE TABLE availability_evidence (
    evidence_id    TEXT    NOT NULL,
    unit_id        TEXT    NOT NULL,
    evidence_level TEXT    NOT NULL CHECK (evidence_level IN ('A','B','C','D')),
    source_type    TEXT    NOT NULL CHECK (source_type IN ('property_api','booking_engine','manager_written','ical','memory','manual_ops')),
    source_ref     TEXT    NOT NULL CHECK (length(source_ref) >= 1),
    captured_at    TEXT    NOT NULL,
    valid_until    TEXT    NOT NULL,
    result         TEXT    NOT NULL CHECK (result IN ('AVAILABLE','OCCUPIED','UNKNOWN')),
    occupancy_ranges TEXT  NOT NULL DEFAULT '[]',
    can_quote      BIGINT NOT NULL CHECK (can_quote IN (0,1)),
    can_hold       BIGINT NOT NULL CHECK (can_hold IN (0,1)),
    can_confirm    BIGINT NOT NULL CHECK (can_confirm IN (0,1)),
    notes          TEXT,
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_availability_evidence PRIMARY KEY (evidence_id),
    CONSTRAINT fk_evidence_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT ck_evidence_window CHECK (captured_at < valid_until),
    CONSTRAINT ck_evidence_confirm_requires_ab CHECK (can_confirm = 0 OR evidence_level IN ('A','B'))
);

CREATE INDEX ix_evidence_unit ON availability_evidence (unit_id, captured_at);

-- --------------------------------------------------------------------------
-- Quotes
-- --------------------------------------------------------------------------
CREATE TABLE quotes (
    quote_id            TEXT    NOT NULL,
    property_id         TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    check_in            TEXT    NOT NULL,
    check_out           TEXT    NOT NULL,
    nights              BIGINT NOT NULL CHECK (nights >= 1),
    adults              BIGINT NOT NULL CHECK (adults >= 1),
    children            BIGINT NOT NULL CHECK (children >= 0),
    infants             BIGINT NOT NULL CHECK (infants >= 0),
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    policy_version_id   TEXT    NOT NULL,
    policy_fingerprint  TEXT    NOT NULL CHECK (length(policy_fingerprint) = 64),
    rate_card_id        TEXT    NOT NULL,
    accommodation_minor BIGINT NOT NULL CHECK (accommodation_minor >= 0),
    extra_guest_minor   BIGINT NOT NULL CHECK (extra_guest_minor >= 0),
    cleaning_minor      BIGINT NOT NULL CHECK (cleaning_minor >= 0),
    extras_minor        BIGINT NOT NULL CHECK (extras_minor >= 0),
    discount_minor      BIGINT NOT NULL CHECK (discount_minor >= 0),
    subtotal_minor      BIGINT NOT NULL CHECK (subtotal_minor >= 0),
    service_fee_minor   BIGINT NOT NULL CHECK (service_fee_minor >= 0),
    tax_minor           BIGINT NOT NULL CHECK (tax_minor >= 0),
    total_minor         BIGINT NOT NULL CHECK (total_minor >= 0),
    security_deposit_minor BIGINT NOT NULL CHECK (security_deposit_minor >= 0),
    lines               TEXT    NOT NULL,
    fingerprint         TEXT    NOT NULL CHECK (length(fingerprint) = 64),
    status              TEXT    NOT NULL CHECK (status IN ('DRAFT','SENT','ACCEPTED','EXPIRED','VOID')),
    warnings            TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL,
    expires_at          TEXT    NOT NULL,
    CONSTRAINT pk_quotes PRIMARY KEY (quote_id),
    CONSTRAINT fk_quotes_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_quotes_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_quotes_policy FOREIGN KEY (policy_version_id) REFERENCES policy_versions (policy_version_id),
    CONSTRAINT fk_quotes_rate_card FOREIGN KEY (rate_card_id) REFERENCES rate_cards (rate_card_id),
    CONSTRAINT ck_quote_stay CHECK (check_in < check_out),
    CONSTRAINT ck_quote_expiry CHECK (created_at < expires_at)
);

CREATE INDEX ix_quotes_unit_window ON quotes (unit_id, check_in, check_out);

-- --------------------------------------------------------------------------
-- Holds (short-lived inventory lock)
-- --------------------------------------------------------------------------
CREATE TABLE holds (
    hold_id            TEXT    NOT NULL,
    unit_id            TEXT    NOT NULL,
    property_id        TEXT    NOT NULL,
    check_in           TEXT    NOT NULL,
    check_out          TEXT    NOT NULL,
    nights             BIGINT NOT NULL CHECK (nights >= 1),
    status             TEXT    NOT NULL CHECK (status IN ('ACTIVE','RELEASED','CONVERTED','EXPIRED')),
    guest_ref          TEXT    NOT NULL CHECK (length(guest_ref) >= 3),
    quote_ref          TEXT,
    policy_version_id  TEXT    NOT NULL,
    evidence_id        TEXT    NOT NULL,
    idempotency_key    TEXT    NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at         TEXT    NOT NULL,
    expires_at         TEXT    NOT NULL,
    closed_at          TEXT,
    close_reason       TEXT,
    extended_count     BIGINT NOT NULL DEFAULT 0 CHECK (extended_count >= 0),
    CONSTRAINT pk_holds PRIMARY KEY (hold_id),
    CONSTRAINT fk_holds_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_holds_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_holds_policy FOREIGN KEY (policy_version_id) REFERENCES policy_versions (policy_version_id),
    CONSTRAINT fk_holds_evidence FOREIGN KEY (evidence_id) REFERENCES availability_evidence (evidence_id),
    CONSTRAINT uq_holds_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_holds_stay CHECK (check_in < check_out),
    CONSTRAINT ck_holds_expiry CHECK (created_at < expires_at),
    CONSTRAINT ck_holds_closed CHECK ((status = 'ACTIVE') OR (closed_at IS NOT NULL))
);

-- Defence in depth: two ACTIVE holds may never share a unit + arrival date.
-- The full overlap test (partial days) is done inside the write transaction;
-- this index makes the most common collision impossible even if that fails.
CREATE UNIQUE INDEX uq_holds_active_slot ON holds (unit_id, check_in) WHERE status = 'ACTIVE';

-- --------------------------------------------------------------------------
-- Bookings
-- --------------------------------------------------------------------------
CREATE TABLE bookings (
    booking_id          TEXT    NOT NULL,
    property_id         TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    check_in            TEXT    NOT NULL,
    check_out           TEXT    NOT NULL,
    nights              BIGINT NOT NULL CHECK (nights >= 1),
    status              TEXT    NOT NULL CHECK (status IN (
                            'INQUIRY','QUALIFIED','AVAILABILITY_PENDING','AVAILABILITY_CONFIRMED',
                            'AVAILABILITY_UNAVAILABLE','QUOTE_DRAFTED','QUOTE_SENT','QUOTE_EXPIRED',
                            'HOLD_REQUESTED','HOLD_ACTIVE','HOLD_EXPIRED','PAYMENT_PENDING',
                            'PAYMENT_PARTIAL','PAYMENT_UNMATCHED','PAYMENT_FAILED','DEPOSIT_PAID',
                            'PAID_IN_FULL','CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL',
                            'IN_STAY','COMPLETED','CANCELLATION_REQUESTED','CANCELLATION_REVIEW',
                            'CANCELLED','REFUND_PENDING','REFUNDED','NO_SHOW','RELOCATED','DISPUTED',
                            'CLOSED')),
    guest_ref           TEXT    NOT NULL CHECK (length(guest_ref) >= 3),
    adults              BIGINT NOT NULL CHECK (adults >= 1),
    children            BIGINT NOT NULL CHECK (children >= 0),
    infants             BIGINT NOT NULL CHECK (infants >= 0),
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    policy_version_id   TEXT    NOT NULL,
    policy_fingerprint  TEXT    NOT NULL CHECK (length(policy_fingerprint) = 64),
    quote_ref           TEXT,
    hold_ref            TEXT,
    total_minor         BIGINT NOT NULL CHECK (total_minor >= 0),
    amount_paid_minor   BIGINT NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
    security_deposit_minor BIGINT NOT NULL DEFAULT 0 CHECK (security_deposit_minor >= 0),
    payment_status      TEXT    NOT NULL CHECK (payment_status IN ('UNPAID','DEPOSIT_CLEARED','CLEARED_FULL','PARTIALLY_REFUNDED','REFUNDED','FAILED','UNMATCHED')),
    idempotency_key     TEXT    NOT NULL CHECK (length(idempotency_key) >= 8),
    version_lock        BIGINT NOT NULL DEFAULT 1 CHECK (version_lock >= 1),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    confirmed_at        TEXT,
    cancelled_at        TEXT,
    CONSTRAINT pk_bookings PRIMARY KEY (booking_id),
    CONSTRAINT fk_bookings_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_bookings_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_bookings_policy FOREIGN KEY (policy_version_id) REFERENCES policy_versions (policy_version_id),
    CONSTRAINT uq_bookings_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_bookings_stay CHECK (check_in < check_out),
    CONSTRAINT ck_booking_paid_le_total CHECK (amount_paid_minor <= total_minor)
);

CREATE INDEX ix_bookings_unit_window ON bookings (unit_id, check_in, check_out);

CREATE INDEX ix_bookings_status ON bookings (status);

-- Occupying statuses only: a cancelled/closed booking must not block a re-sale.
CREATE UNIQUE INDEX uq_bookings_active_slot ON bookings (unit_id, check_in)
    WHERE status IN ('CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY');

-- --------------------------------------------------------------------------
-- Append-only logs
-- --------------------------------------------------------------------------
CREATE TABLE booking_state_events (
    state_event_id TEXT    NOT NULL,
    booking_id     TEXT    NOT NULL,
    seq            BIGINT NOT NULL CHECK (seq >= 1),
    from_state     TEXT    NOT NULL,
    to_state       TEXT    NOT NULL,
    command        TEXT    NOT NULL,
    actor          TEXT    NOT NULL,
    actor_role     TEXT    NOT NULL,
    command_id     TEXT    NOT NULL CHECK (length(command_id) >= 8),
    evidence_ref   TEXT,
    approval_ref   TEXT,
    reason         TEXT,
    occurred_at    TEXT    NOT NULL,
    CONSTRAINT pk_booking_state_events PRIMARY KEY (state_event_id),
    CONSTRAINT fk_bse_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT uq_bse_command UNIQUE (booking_id, command_id),
    CONSTRAINT uq_bse_seq UNIQUE (booking_id, seq)
);

CREATE INDEX ix_bse_booking ON booking_state_events (booking_id, seq);

CREATE TABLE audit_events (
    audit_event_id  TEXT    NOT NULL,
    seq             BIGINT NOT NULL CHECK (seq >= 1),
    occurred_at     TEXT    NOT NULL,
    actor           TEXT    NOT NULL CHECK (length(actor) >= 1),
    actor_role      TEXT    NOT NULL CHECK (length(actor_role) >= 1),
    action          TEXT    NOT NULL CHECK (length(action) >= 1),
    entity_type     TEXT    NOT NULL CHECK (length(entity_type) >= 1),
    entity_id       TEXT    NOT NULL CHECK (length(entity_id) >= 1),
    before_state    TEXT,
    after_state     TEXT,
    reason          TEXT,
    policy_version_id TEXT,
    evidence_ref    TEXT,
    tool            TEXT,
    trace_id        TEXT,
    prev_hash       TEXT    NOT NULL CHECK (length(prev_hash) = 64),
    hash            TEXT    NOT NULL CHECK (length(hash) = 64),
    CONSTRAINT pk_audit_events PRIMARY KEY (audit_event_id),
    CONSTRAINT uq_audit_seq UNIQUE (seq)
);

CREATE INDEX ix_audit_entity ON audit_events (entity_type, entity_id, seq);

-- Idempotency ledger: the primary key is the key alone, so keys are globally
-- unique across operations — a caller that reuses one key for a different
-- operation gets a conflict instead of a silent double-write.
CREATE TABLE idempotency_keys (
    idempotency_key TEXT    NOT NULL,
    operation       TEXT    NOT NULL,
    request_hash    TEXT    NOT NULL CHECK (length(request_hash) = 64),
    result_ref      TEXT,
    result_hash     TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT pk_idempotency_keys PRIMARY KEY (idempotency_key)
);

CREATE FUNCTION villa_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

-- --------------------------------------------------------------------------
-- Immutability guards: the database itself refuses to rewrite history.
-- --------------------------------------------------------------------------
CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_audit_events_no_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_booking_state_events_no_update
    BEFORE UPDATE ON booking_state_events
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_booking_state_events_no_delete
    BEFORE DELETE ON booking_state_events
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_policy_versions_no_update
    BEFORE UPDATE ON policy_versions
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_policy_versions_no_delete
    BEFORE DELETE ON policy_versions
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_rate_cards_no_update
    BEFORE UPDATE ON rate_cards
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

CREATE TRIGGER trg_rate_cards_no_delete
    BEFORE DELETE ON rate_cards
    FOR EACH ROW EXECUTE FUNCTION villa_reject_mutation();

INSERT INTO schema_meta (schema_version, applied_at, description)
VALUES ('001_initial', '2026-09-21T00:00:00+00:00', 'Villa Agency OS initial schema: inventory, policy, rates, evidence, quotes, holds, bookings, audit');

-- Villa Agency OS — initial schema (SQLite, development & test dialect)
-- Semantic twin of 001_initial.pg.sql. Keep both files in lock step; the parity
-- test (tests/test_schema.py) fails when tables/columns/constraints drift.
--
-- Conventions (identical in both dialects):
--   * ids          TEXT   opaque, prefixed ("prop_ab12", "hold_cd34")
--   * timestamps   TEXT   ISO-8601 UTC, second precision, e.g. 2026-09-21T04:05:06+00:00
--   * dates        TEXT   ISO-8601 calendar date, e.g. 2026-09-21
--   * money        INTEGER minor units + a separate ISO-4217 currency column
--                  (IDR has 0 minor units, USD 2 — never a float, never NUMERIC)
--   * percentages  TEXT   decimal string ("10", "7.5") — compared with CHECK
--   * JSON payloads TEXT  canonical JSON (sorted keys, no whitespace)
--   * booleans     INTEGER 0/1 with CHECK (col IN (0,1))
--
-- Oddities are deliberate: no floats, no nullable money, no cascading deletes on
-- financial records, and audit tables reject UPDATE/DELETE at the database level.

PRAGMA foreign_keys = ON;

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
-- Tenant scope (Master Spec Wave 1-A)
-- --------------------------------------------------------------------------
CREATE TABLE agencies (
    agency_id    TEXT    NOT NULL,
    name         TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_agencies PRIMARY KEY (agency_id)
);

-- --------------------------------------------------------------------------
-- Inventory: properties & units
-- --------------------------------------------------------------------------
CREATE TABLE properties (
    property_id   TEXT    NOT NULL,
    agency_id     TEXT    NOT NULL DEFAULT 'agency_default',
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
    CONSTRAINT pk_properties PRIMARY KEY (property_id),
    CONSTRAINT fk_properties_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);
CREATE INDEX ix_properties_status ON properties (status);

CREATE TABLE units (
    unit_id      TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    property_id  TEXT    NOT NULL,
    name         TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    unit_type    TEXT    NOT NULL CHECK (unit_type IN ('entire_villa','suite','room','bungalow','compound_unit')),
    bedrooms     INTEGER NOT NULL CHECK (bedrooms >= 0),
    bathrooms    INTEGER NOT NULL CHECK (bathrooms >= 0),
    base_capacity INTEGER NOT NULL CHECK (base_capacity >= 1),
    max_guests   INTEGER NOT NULL CHECK (max_guests >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE','MAINTENANCE','SUSPENDED')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_units PRIMARY KEY (unit_id),
    CONSTRAINT fk_units_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_units_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT ck_units_capacity CHECK (max_guests >= base_capacity)
);
CREATE INDEX ix_units_property ON units (property_id, status);

-- --------------------------------------------------------------------------
-- Supplier route skeleton (Master Spec Wave 1-A)
-- --------------------------------------------------------------------------
CREATE TABLE suppliers (
    supplier_id  TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    name         TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_suppliers PRIMARY KEY (supplier_id),
    CONSTRAINT fk_suppliers_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);
CREATE INDEX ix_suppliers_agency ON suppliers (agency_id, status);

CREATE TABLE villa_supplier_relationships (
    villa_supplier_relationship_id TEXT NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    supplier_id  TEXT    NOT NULL,
    property_id  TEXT,
    unit_id      TEXT,
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_villa_supplier_relationships PRIMARY KEY (villa_supplier_relationship_id),
    CONSTRAINT fk_vsr_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_vsr_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_vsr_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_vsr_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id)
);
CREATE INDEX ix_vsr_lookup ON villa_supplier_relationships (agency_id, property_id, unit_id, status);

CREATE TABLE contract_versions (
    contract_version_id TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    villa_supplier_relationship_id TEXT NOT NULL,
    version      INTEGER NOT NULL CHECK (version >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('DRAFT','PENDING_VERIFICATION','VERIFIED','APPROVED','ACTIVE','SUPERSEDED','EXPIRED')),
    effective_from TEXT NOT NULL,
    effective_to   TEXT NOT NULL,
    source_doc_ref TEXT NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by TEXT,
    created_at   TEXT    NOT NULL,
    CONSTRAINT pk_contract_versions PRIMARY KEY (contract_version_id),
    CONSTRAINT fk_contract_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_contract_route FOREIGN KEY (villa_supplier_relationship_id) REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT uq_contract_version UNIQUE (villa_supplier_relationship_id, version),
    CONSTRAINT ck_contract_dates CHECK (effective_from <= effective_to)
);

CREATE TABLE rate_versions (
    rate_version_id TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    villa_supplier_relationship_id TEXT NOT NULL,
    contract_version_id TEXT,
    version      INTEGER NOT NULL CHECK (version >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('DRAFT','PENDING_VERIFICATION','VERIFIED','APPROVED','ACTIVE','SUPERSEDED','EXPIRED')),
    currency     TEXT    NOT NULL CHECK (length(currency) = 3),
    effective_from TEXT NOT NULL,
    effective_to   TEXT NOT NULL,
    source_doc_ref TEXT NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by TEXT,
    created_at   TEXT    NOT NULL,
    CONSTRAINT pk_rate_versions PRIMARY KEY (rate_version_id),
    CONSTRAINT fk_rate_version_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_rate_version_route FOREIGN KEY (villa_supplier_relationship_id) REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT fk_rate_version_contract FOREIGN KEY (contract_version_id) REFERENCES contract_versions (contract_version_id),
    CONSTRAINT uq_rate_version UNIQUE (villa_supplier_relationship_id, version),
    CONSTRAINT ck_rate_version_dates CHECK (effective_from <= effective_to)
);

-- --------------------------------------------------------------------------
-- Policy versions (append-only business rules)
-- --------------------------------------------------------------------------
CREATE TABLE policy_versions (
    policy_version_id   TEXT    NOT NULL,
    agency_id           TEXT    NOT NULL DEFAULT 'agency_default',
    property_id         TEXT    NOT NULL,
    unit_scope          TEXT    NOT NULL,
    version             INTEGER NOT NULL CHECK (version >= 1),
    effective_from      TEXT    NOT NULL,
    effective_to        TEXT    NOT NULL,
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    deposit_pct         TEXT    NOT NULL,
    service_fee_pct     TEXT    NOT NULL,
    tax_pbjt_pct        TEXT    NOT NULL,
    max_discount_pct    TEXT    NOT NULL,
    quote_valid_hours   INTEGER NOT NULL CHECK (quote_valid_hours BETWEEN 1 AND 168),
    hold_ttl_minutes    INTEGER NOT NULL CHECK (hold_ttl_minutes BETWEEN 15 AND 1440),
    max_hold_extensions INTEGER NOT NULL CHECK (max_hold_extensions BETWEEN 0 AND 3),
    payload             TEXT    NOT NULL,
    fingerprint         TEXT    NOT NULL CHECK (length(fingerprint) = 64),
    source_doc_ref      TEXT    NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by         TEXT    NOT NULL CHECK (length(approved_by) >= 1),
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_policy_versions PRIMARY KEY (policy_version_id),
    CONSTRAINT fk_policy_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
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
    agency_id           TEXT    NOT NULL DEFAULT 'agency_default',
    property_id         TEXT    NOT NULL,
    unit_scope          TEXT    NOT NULL,
    name                TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    version             INTEGER NOT NULL CHECK (version >= 1),
    effective_from      TEXT    NOT NULL,
    effective_to        TEXT    NOT NULL,
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    weekly_discount_pct TEXT    NOT NULL,
    monthly_discount_pct TEXT   NOT NULL,
    source_doc_ref      TEXT    NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by         TEXT    NOT NULL CHECK (length(approved_by) >= 1),
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_rate_cards PRIMARY KEY (rate_card_id),
    CONSTRAINT fk_rate_cards_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
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
    nightly_rate_minor INTEGER NOT NULL CHECK (nightly_rate_minor >= 0),
    min_stay_nights   INTEGER NOT NULL CHECK (min_stay_nights >= 1),
    priority          INTEGER NOT NULL CHECK (priority >= 0),
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
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    unit_id        TEXT    NOT NULL,
    evidence_level TEXT    NOT NULL CHECK (evidence_level IN ('A','B','C','D')),
    source_type    TEXT    NOT NULL CHECK (source_type IN ('property_api','booking_engine','manager_written','ical','memory','manual_ops')),
    source_ref     TEXT    NOT NULL CHECK (length(source_ref) >= 1),
    captured_at    TEXT    NOT NULL,
    valid_until    TEXT    NOT NULL,
    result         TEXT    NOT NULL CHECK (result IN ('AVAILABLE','OCCUPIED','UNKNOWN')),
    occupancy_ranges TEXT  NOT NULL DEFAULT '[]',
    can_quote      INTEGER NOT NULL CHECK (can_quote IN (0,1)),
    can_hold       INTEGER NOT NULL CHECK (can_hold IN (0,1)),
    can_confirm    INTEGER NOT NULL CHECK (can_confirm IN (0,1)),
    notes          TEXT,
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_availability_evidence PRIMARY KEY (evidence_id),
    CONSTRAINT fk_evidence_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
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
    agency_id           TEXT    NOT NULL DEFAULT 'agency_default',
    property_id         TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    check_in            TEXT    NOT NULL,
    check_out           TEXT    NOT NULL,
    nights              INTEGER NOT NULL CHECK (nights >= 1),
    adults              INTEGER NOT NULL CHECK (adults >= 1),
    children            INTEGER NOT NULL CHECK (children >= 0),
    infants             INTEGER NOT NULL CHECK (infants >= 0),
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    policy_version_id   TEXT    NOT NULL,
    policy_fingerprint  TEXT    NOT NULL CHECK (length(policy_fingerprint) = 64),
    rate_card_id        TEXT    NOT NULL,
    accommodation_minor INTEGER NOT NULL CHECK (accommodation_minor >= 0),
    extra_guest_minor   INTEGER NOT NULL CHECK (extra_guest_minor >= 0),
    cleaning_minor      INTEGER NOT NULL CHECK (cleaning_minor >= 0),
    extras_minor        INTEGER NOT NULL CHECK (extras_minor >= 0),
    discount_minor      INTEGER NOT NULL CHECK (discount_minor >= 0),
    subtotal_minor      INTEGER NOT NULL CHECK (subtotal_minor >= 0),
    service_fee_minor   INTEGER NOT NULL CHECK (service_fee_minor >= 0),
    tax_minor           INTEGER NOT NULL CHECK (tax_minor >= 0),
    total_minor         INTEGER NOT NULL CHECK (total_minor >= 0),
    security_deposit_minor INTEGER NOT NULL CHECK (security_deposit_minor >= 0),
    lines               TEXT    NOT NULL,
    fingerprint         TEXT    NOT NULL CHECK (length(fingerprint) = 64),
    status              TEXT    NOT NULL CHECK (status IN ('DRAFT','SENT','ACCEPTED','EXPIRED','VOID')),
    warnings            TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL,
    expires_at          TEXT    NOT NULL,
    CONSTRAINT pk_quotes PRIMARY KEY (quote_id),
    CONSTRAINT fk_quotes_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
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
    agency_id          TEXT    NOT NULL DEFAULT 'agency_default',
    unit_id            TEXT    NOT NULL,
    property_id        TEXT    NOT NULL,
    check_in           TEXT    NOT NULL,
    check_out          TEXT    NOT NULL,
    nights             INTEGER NOT NULL CHECK (nights >= 1),
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
    extended_count     INTEGER NOT NULL DEFAULT 0 CHECK (extended_count >= 0),
    CONSTRAINT pk_holds PRIMARY KEY (hold_id),
    CONSTRAINT fk_holds_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_holds_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_holds_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_holds_policy FOREIGN KEY (policy_version_id) REFERENCES policy_versions (policy_version_id),
    CONSTRAINT fk_holds_evidence FOREIGN KEY (evidence_id) REFERENCES availability_evidence (evidence_id),
    CONSTRAINT uq_holds_idempotency UNIQUE (agency_id, idempotency_key),
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
    agency_id           TEXT    NOT NULL DEFAULT 'agency_default',
    villa_supplier_relationship_id TEXT NOT NULL DEFAULT 'route_default',
    property_id         TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    check_in            TEXT    NOT NULL,
    check_out           TEXT    NOT NULL,
    nights              INTEGER NOT NULL CHECK (nights >= 1),
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
    adults              INTEGER NOT NULL CHECK (adults >= 1),
    children            INTEGER NOT NULL CHECK (children >= 0),
    infants             INTEGER NOT NULL CHECK (infants >= 0),
    currency            TEXT    NOT NULL CHECK (length(currency) = 3),
    policy_version_id   TEXT    NOT NULL,
    policy_fingerprint  TEXT    NOT NULL CHECK (length(policy_fingerprint) = 64),
    quote_ref           TEXT,
    hold_ref            TEXT,
    total_minor         INTEGER NOT NULL CHECK (total_minor >= 0),
    amount_paid_minor   INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
    security_deposit_minor INTEGER NOT NULL DEFAULT 0 CHECK (security_deposit_minor >= 0),
    payment_status      TEXT    NOT NULL CHECK (payment_status IN ('UNPAID','DEPOSIT_CLEARED','CLEARED_FULL','PARTIALLY_REFUNDED','REFUNDED','FAILED','UNMATCHED')),
    idempotency_key     TEXT    NOT NULL CHECK (length(idempotency_key) >= 8),
    version_lock        INTEGER NOT NULL DEFAULT 1 CHECK (version_lock >= 1),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    confirmed_at        TEXT,
    cancelled_at        TEXT,
    CONSTRAINT pk_bookings PRIMARY KEY (booking_id),
    CONSTRAINT fk_bookings_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_bookings_supplier_route FOREIGN KEY (villa_supplier_relationship_id) REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT fk_bookings_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_bookings_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_bookings_policy FOREIGN KEY (policy_version_id) REFERENCES policy_versions (policy_version_id),
    CONSTRAINT uq_bookings_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_bookings_stay CHECK (check_in < check_out),
    CONSTRAINT ck_booking_paid_le_total CHECK (amount_paid_minor <= total_minor)
);
CREATE INDEX ix_bookings_unit_window ON bookings (unit_id, check_in, check_out);
CREATE INDEX ix_bookings_status ON bookings (status);
-- Occupying statuses only: a cancelled/closed booking must not block a re-sale.
-- ADR-0002 (Opsi A, diputuskan 2026-09-21): pembatalan yang sedang ditinjau dan
-- sengketa saat tamu masih di dalam unit JUGA menahan slot. Tanggal terkunci sampai
-- pembatalan DIPUTUSKAN (-> CANCELLED) atau sengketa ditutup — fail-closed, bukan
-- berdasarkan janji untuk tidak menjual ulang.
CREATE UNIQUE INDEX uq_bookings_active_slot ON bookings (unit_id, check_in)
    WHERE status IN ('CONFIRMED','BALANCE_DUE','BALANCE_OVERDUE','PRE_ARRIVAL','IN_STAY','CANCELLATION_REQUESTED','CANCELLATION_REVIEW','DISPUTED');

-- --------------------------------------------------------------------------
-- Append-only logs
-- --------------------------------------------------------------------------
CREATE TABLE booking_state_events (
    state_event_id TEXT    NOT NULL,
    booking_id     TEXT    NOT NULL,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
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
    seq             INTEGER NOT NULL CHECK (seq >= 1),
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

-- Idempotency ledger. The key is unique *within an agency*, not across the whole
-- platform: two agencies are separate businesses and picking the same string
-- ("hold-2026-10-01") is not a conflict they caused. Within one agency the
-- guarantee is unchanged — reusing a key for a different operation or payload
-- raises IDEMPOTENCY_CONFLICT instead of silently double-writing.
CREATE TABLE idempotency_keys (
    idempotency_key TEXT    NOT NULL,
    agency_id       TEXT    NOT NULL DEFAULT 'agency_default',
    operation       TEXT    NOT NULL,
    request_hash    TEXT    NOT NULL CHECK (length(request_hash) = 64),
    result_ref      TEXT,
    result_hash     TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT pk_idempotency_keys PRIMARY KEY (agency_id, idempotency_key),
    CONSTRAINT fk_idempotency_keys_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);

-- --------------------------------------------------------------------------
-- Phase 2 — case management, structured intake, explainable matching,
-- approval queue, message drafts, SLA tracking.
--
-- Rules that the shape of these tables enforces (fail-closed by construction):
--   * an inbound message body is NEVER the raw text: only a redacted body plus a
--     sha256 of the original. P3/P4 content is not stored at all.
--   * an extracted intake value only becomes usable after a human review row is
--     recorded; the value itself stays immutable once written.
--   * a candidate can only be ineligible with an explicit deny reason.
--   * a draft that is not GROUNDED can never reach APPROVED/SENT.
--   * money-moving approvals (refund/payout/bank_change) need 2 approvers.
-- --------------------------------------------------------------------------
CREATE TABLE cases (
    case_id        TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    channel        TEXT    NOT NULL CHECK (channel IN ('website','email','whatsapp_business','dashboard','phone')),
    channel_ref    TEXT,
    guest_ref      TEXT    NOT NULL CHECK (length(guest_ref) >= 3),
    status         TEXT    NOT NULL CHECK (status IN (
                       'NEW','TRIAGED','MATCHING','AWAITING_EVIDENCE','QUOTED','HOLD_PENDING',
                       'BOOKED','ESCALATED','CLOSED_WON','CLOSED_LOST','CLOSED_DUPLICATE')),
    priority       TEXT    NOT NULL CHECK (priority IN ('LOW','NORMAL','HIGH','URGENT')),
    owner_actor    TEXT,
    booking_id     TEXT,
    sla_policy     TEXT    NOT NULL CHECK (length(sla_policy) >= 3),
    first_response_due_at TEXT NOT NULL,
    quote_due_at   TEXT    NOT NULL,
    first_responded_at    TEXT,
    quoted_at      TEXT,
    idempotency_key TEXT   NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    closed_at      TEXT,
    close_reason   TEXT,
    CONSTRAINT pk_cases PRIMARY KEY (case_id),
    CONSTRAINT fk_cases_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_cases_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT uq_cases_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_cases_sla_window CHECK (created_at < first_response_due_at AND first_response_due_at <= quote_due_at),
    CONSTRAINT ck_cases_closed CHECK (status NOT LIKE 'CLOSED_%' OR closed_at IS NOT NULL),
    CONSTRAINT ck_cases_close_reason CHECK (status NOT LIKE 'CLOSED_%' OR (close_reason IS NOT NULL AND length(close_reason) >= 3))
);
CREATE INDEX ix_cases_status ON cases (status, first_response_due_at);
CREATE INDEX ix_cases_owner ON cases (owner_actor, status);

CREATE TABLE case_messages (
    case_message_id TEXT  NOT NULL,
    case_id        TEXT    NOT NULL,
    direction      TEXT    NOT NULL CHECK (direction IN ('INBOUND','OUTBOUND')),
    channel        TEXT    NOT NULL CHECK (channel IN ('website','email','whatsapp_business','dashboard','phone')),
    external_ref   TEXT,
    sender_ref     TEXT    NOT NULL CHECK (length(sender_ref) >= 3),
    received_at    TEXT    NOT NULL,
    content_class  TEXT    NOT NULL CHECK (content_class IN ('P0','P1','P2','P3','P4')),
    body_redacted  TEXT,
    body_sha256    TEXT    NOT NULL CHECK (length(body_sha256) = 64),
    attachment_count INTEGER NOT NULL DEFAULT 0 CHECK (attachment_count >= 0),
    untrusted      INTEGER NOT NULL DEFAULT 1 CHECK (untrusted IN (0,1)),
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_case_messages PRIMARY KEY (case_message_id),
    CONSTRAINT fk_case_messages_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT ck_case_messages_p3p4_not_stored CHECK (content_class NOT IN ('P3','P4') OR body_redacted IS NULL),
    CONSTRAINT ck_case_messages_p2_redacted CHECK (content_class <> 'P2' OR body_redacted IS NOT NULL)
);
CREATE INDEX ix_case_messages_case ON case_messages (case_id, received_at);

CREATE TABLE intake_proposals (
    proposal_id    TEXT    NOT NULL,
    case_id        TEXT    NOT NULL,
    source_message_id TEXT NOT NULL,
    extractor      TEXT    NOT NULL CHECK (length(extractor) >= 3),
    method         TEXT    NOT NULL CHECK (method IN ('rule','llm_shadow','manual')),
    produced_at    TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('PROPOSED','ACCEPTED','REJECTED','PARTIAL','SUPERSEDED')),
    payload        TEXT    NOT NULL,
    fingerprint    TEXT    NOT NULL CHECK (length(fingerprint) = 64),
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_intake_proposals PRIMARY KEY (proposal_id),
    CONSTRAINT fk_intake_proposals_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT fk_intake_proposals_message FOREIGN KEY (source_message_id) REFERENCES case_messages (case_message_id)
);
CREATE INDEX ix_intake_proposals_case ON intake_proposals (case_id, produced_at);

CREATE TABLE intake_fields (
    intake_field_id TEXT  NOT NULL,
    proposal_id    TEXT   NOT NULL,
    case_id        TEXT   NOT NULL,
    field_name     TEXT   NOT NULL CHECK (length(field_name) BETWEEN 2 AND 64),
    field_value    TEXT,
    value_type     TEXT   NOT NULL CHECK (value_type IN ('date','integer','text','money','boolean','enum')),
    confidence     INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    source_message_id TEXT NOT NULL,
    source_span_start INTEGER,
    source_span_end   INTEGER,
    status         TEXT   NOT NULL CHECK (status IN ('PROPOSED','ACCEPTED','REJECTED')),
    value_sha256   TEXT   NOT NULL CHECK (length(value_sha256) = 64),
    created_at     TEXT   NOT NULL,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    rejection_reason TEXT,
    CONSTRAINT pk_intake_fields PRIMARY KEY (intake_field_id),
    CONSTRAINT fk_intake_fields_proposal FOREIGN KEY (proposal_id) REFERENCES intake_proposals (proposal_id),
    CONSTRAINT fk_intake_fields_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT fk_intake_fields_message FOREIGN KEY (source_message_id) REFERENCES case_messages (case_message_id),
    CONSTRAINT uq_intake_field_once UNIQUE (proposal_id, field_name),
    CONSTRAINT ck_intake_field_span CHECK (source_span_start IS NULL OR source_span_end IS NULL OR source_span_start <= source_span_end),
    CONSTRAINT ck_intake_field_reviewed CHECK (status = 'PROPOSED' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    CONSTRAINT ck_intake_field_rejection CHECK (status <> 'REJECTED' OR (rejection_reason IS NOT NULL AND length(rejection_reason) >= 3))
);
CREATE INDEX ix_intake_fields_case ON intake_fields (case_id, field_name, status);

CREATE TABLE match_runs (
    match_run_id   TEXT    NOT NULL,
    case_id        TEXT    NOT NULL,
    engine_version TEXT    NOT NULL CHECK (length(engine_version) >= 1),
    intake_snapshot TEXT   NOT NULL,
    intake_hash    TEXT    NOT NULL CHECK (length(intake_hash) = 64),
    criteria       TEXT    NOT NULL,
    considered_count INTEGER NOT NULL CHECK (considered_count >= 0),
    eligible_count INTEGER NOT NULL CHECK (eligible_count >= 0),
    created_by     TEXT    NOT NULL CHECK (length(created_by) >= 1),
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_match_runs PRIMARY KEY (match_run_id),
    CONSTRAINT fk_match_runs_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT ck_match_runs_counts CHECK (eligible_count <= considered_count)
);
CREATE INDEX ix_match_runs_case ON match_runs (case_id, created_at);

CREATE TABLE match_candidates (
    candidate_id   TEXT    NOT NULL,
    match_run_id   TEXT    NOT NULL,
    unit_id        TEXT    NOT NULL,
    rank           INTEGER NOT NULL CHECK (rank >= 1),
    eligible       INTEGER NOT NULL CHECK (eligible IN (0,1)),
    score_centi    INTEGER NOT NULL,
    reasons        TEXT    NOT NULL,
    deny_reasons   TEXT    NOT NULL DEFAULT '[]',
    evidence_refs  TEXT    NOT NULL DEFAULT '[]',
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_match_candidates PRIMARY KEY (candidate_id),
    CONSTRAINT fk_match_candidates_run FOREIGN KEY (match_run_id) REFERENCES match_runs (match_run_id),
    CONSTRAINT fk_match_candidates_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT uq_match_candidate_rank UNIQUE (match_run_id, rank),
    CONSTRAINT uq_match_candidate_unit UNIQUE (match_run_id, unit_id),
    CONSTRAINT ck_match_candidate_deny CHECK (eligible = 1 OR deny_reasons <> '[]')
);
CREATE INDEX ix_match_candidates_run ON match_candidates (match_run_id, rank);

CREATE TABLE approval_requests (
    approval_id    TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    subject_type   TEXT    NOT NULL CHECK (subject_type IN (
                       'booking','case','quote','hold','refund','payout','policy_exception',
                       'relocation','bank_change','discount','message_send','cancellation')),
    subject_id     TEXT    NOT NULL,
    action         TEXT    NOT NULL CHECK (length(action) >= 3),
    risk_level     TEXT    NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')),
    requested_by   TEXT    NOT NULL CHECK (length(requested_by) >= 1),
    requested_role TEXT    NOT NULL CHECK (length(requested_role) >= 1),
    requested_at   TEXT    NOT NULL,
    reason         TEXT    NOT NULL CHECK (length(reason) >= 10),
    evidence_refs  TEXT    NOT NULL DEFAULT '[]',
    payload        TEXT    NOT NULL,
    payload_sha256 TEXT    NOT NULL CHECK (length(payload_sha256) = 64),
    money_minor    INTEGER CHECK (money_minor IS NULL OR money_minor >= 0),
    currency       TEXT    CHECK (currency IS NULL OR length(currency) = 3),
    required_role  TEXT    NOT NULL CHECK (length(required_role) >= 1),
    required_approvals INTEGER NOT NULL CHECK (required_approvals BETWEEN 1 AND 3),
    status         TEXT    NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','WITHDRAWN')),
    expires_at     TEXT    NOT NULL,
    idempotency_key TEXT   NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at     TEXT    NOT NULL,
    closed_at      TEXT,
    CONSTRAINT pk_approval_requests PRIMARY KEY (approval_id),
    CONSTRAINT fk_approval_requests_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_approval_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_approval_money_pair CHECK ((money_minor IS NULL) = (currency IS NULL)),
    CONSTRAINT ck_approval_expiry CHECK (requested_at < expires_at),
    CONSTRAINT ck_approval_closed CHECK (status = 'PENDING' OR closed_at IS NOT NULL),
    CONSTRAINT ck_approval_dual_for_money CHECK (required_approvals >= 2 OR subject_type NOT IN ('refund','payout','bank_change'))
);
CREATE INDEX ix_approval_status ON approval_requests (agency_id, status, expires_at);
CREATE INDEX ix_approval_subject ON approval_requests (subject_type, subject_id);

CREATE TABLE approval_decisions (
    decision_id    TEXT    NOT NULL,
    approval_id    TEXT    NOT NULL,
    approver       TEXT    NOT NULL CHECK (length(approver) >= 1),
    approver_role  TEXT    NOT NULL CHECK (length(approver_role) >= 1),
    decision       TEXT    NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
    note           TEXT    NOT NULL CHECK (length(note) >= 3),
    decided_at     TEXT    NOT NULL,
    request_hash   TEXT    NOT NULL CHECK (length(request_hash) = 64),
    signature_hash TEXT    NOT NULL CHECK (length(signature_hash) = 64),
    CONSTRAINT pk_approval_decisions PRIMARY KEY (decision_id),
    CONSTRAINT uq_approval_decision_once UNIQUE (approval_id, approver),
    CONSTRAINT ck_approval_decision_self CHECK (approver <> '')
);
CREATE INDEX ix_approval_decisions_approval ON approval_decisions (approval_id, decided_at);

CREATE TABLE message_drafts (
    draft_id       TEXT    NOT NULL,
    case_id        TEXT    NOT NULL,
    booking_id     TEXT,
    channel        TEXT    NOT NULL CHECK (channel IN ('website','email','whatsapp_business','dashboard','phone')),
    template       TEXT    NOT NULL CHECK (length(template) >= 3),
    locale         TEXT    NOT NULL CHECK (length(locale) >= 5),
    recipient_ref  TEXT    NOT NULL CHECK (length(recipient_ref) >= 3),
    body_text      TEXT    NOT NULL CHECK (length(body_text) BETWEEN 1 AND 8000),
    body_sha256    TEXT    NOT NULL CHECK (length(body_sha256) = 64),
    claim_bindings TEXT    NOT NULL DEFAULT '[]',
    grounding      TEXT    NOT NULL CHECK (grounding IN ('GROUNDED','UNGROUNDED','REVIEW_REQUIRED')),
    gate_decision  TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','SENT','REJECTED','EXPIRED')),
    approval_id    TEXT,
    created_by     TEXT    NOT NULL CHECK (length(created_by) >= 1),
    created_at     TEXT    NOT NULL,
    approved_by    TEXT,
    approved_at    TEXT,
    sent_by        TEXT,
    sent_at        TEXT,
    external_ref   TEXT,
    CONSTRAINT pk_message_drafts PRIMARY KEY (draft_id),
    CONSTRAINT fk_drafts_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT fk_drafts_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT ck_drafts_grounded_before_approval CHECK (grounding = 'GROUNDED' OR status NOT IN ('APPROVED','SENT')),
    CONSTRAINT ck_drafts_approved_pair CHECK (status NOT IN ('APPROVED','SENT') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT ck_drafts_sent_pair CHECK (status <> 'SENT' OR (sent_by IS NOT NULL AND sent_at IS NOT NULL AND external_ref IS NOT NULL))
);
CREATE INDEX ix_drafts_case ON message_drafts (case_id, created_at);
CREATE INDEX ix_drafts_status ON message_drafts (status, created_at);

CREATE TABLE case_events (
    case_event_id  TEXT    NOT NULL,
    case_id        TEXT    NOT NULL,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'CREATED','MESSAGE_IN','MESSAGE_OUT','INTAKE_PROPOSED','INTAKE_REVIEWED','MATCH_RUN',
                       'QUOTE_LINKED','HOLD_LINKED','BOOKING_LINKED','STATUS_CHANGED','APPROVAL_REQUESTED',
                       'APPROVAL_DECIDED','DRAFT_CREATED','SLA_BREACH','ESCALATED','NOTE')),
    from_status    TEXT,
    to_status      TEXT,
    actor          TEXT    NOT NULL CHECK (length(actor) >= 1),
    actor_role     TEXT    NOT NULL CHECK (length(actor_role) >= 1),
    command_id     TEXT    NOT NULL CHECK (length(command_id) >= 8),
    reason         TEXT,
    refs           TEXT    NOT NULL DEFAULT '[]',
    occurred_at    TEXT    NOT NULL,
    CONSTRAINT pk_case_events PRIMARY KEY (case_event_id),
    CONSTRAINT fk_case_events_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT uq_case_events_seq UNIQUE (case_id, seq),
    CONSTRAINT uq_case_events_command UNIQUE (case_id, command_id)
);
CREATE INDEX ix_case_events_case ON case_events (case_id, seq);

CREATE TABLE sla_breaches (
    breach_id      TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    subject_type   TEXT    NOT NULL CHECK (subject_type IN (
                       'case','payment','complaint','approval','incident','ops_task','invoice')),
    subject_id     TEXT    NOT NULL,
    case_id        TEXT,
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'FIRST_RESPONSE','AVAILABILITY_ANSWER','QUOTE','COMPLAINT_FIRST_RESPONSE',
                       'COMPLAINT_RESOLUTION','UNMATCHED_PAYMENT','APPROVAL_QUEUE','OPS_TASK',
                       'REGULATOR_NOTICE','PAYMENT_DUNNING')),
    due_at         TEXT    NOT NULL,
    detected_at    TEXT    NOT NULL,
    breach_minutes INTEGER NOT NULL CHECK (breach_minutes >= 0),
    owner_actor    TEXT,
    status         TEXT    NOT NULL CHECK (status IN ('OPEN','RESOLVED','WAIVED')),
    resolution_note TEXT,
    resolved_at    TEXT,
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_sla_breaches PRIMARY KEY (breach_id),
    CONSTRAINT fk_sla_breaches_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_sla_breaches_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT uq_sla_breach_once UNIQUE (agency_id, subject_type, subject_id, kind),
    CONSTRAINT ck_sla_breach_detected CHECK (detected_at >= due_at),
    CONSTRAINT ck_sla_breach_resolution CHECK (status = 'OPEN' OR (resolved_at IS NOT NULL AND resolution_note IS NOT NULL AND length(resolution_note) >= 3))
);
CREATE INDEX ix_sla_breaches_open ON sla_breaches (agency_id, status, due_at);

-- --------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- Phase 3 — billing, pembayaran (hosted page), rekonsiliasi, dokumen, ops, insiden
--
-- Prinsip: uang selalu integer minor unit; setiap mutasi uang punya jejak
-- (ledger append-only dengan rantai hash); webhook yang tanda tangannya tidak
-- terverifikasi tidak boleh pernah "applied"; setiap kegagalan rekonsiliasi
-- punya pemilik dan tenggat, bukan hilang di log.
-- ---------------------------------------------------------------------------

CREATE TABLE invoices (
    invoice_id     TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    invoice_number TEXT,
    booking_id     TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL CHECK (length(policy_fingerprint) = 64),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    subtotal_minor INTEGER NOT NULL CHECK (subtotal_minor >= 0),
    tax_minor      INTEGER NOT NULL CHECK (tax_minor >= 0),
    service_minor  INTEGER NOT NULL CHECK (service_minor >= 0),
    total_minor    INTEGER NOT NULL CHECK (total_minor >= 0),
    amount_paid_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
    status         TEXT NOT NULL CHECK (status IN ('ISSUED','VOID')),
    payment_status TEXT NOT NULL CHECK (payment_status IN ('UNPAID','PARTIAL','PAID','OVERPAID','REFUNDED')),
    issued_at      TEXT NOT NULL,
    due_at         TEXT NOT NULL,
    voided_at      TEXT,
    void_reason    TEXT,
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_invoices PRIMARY KEY (invoice_id),
    CONSTRAINT fk_invoices_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_invoices_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT uq_invoices_number UNIQUE (invoice_number),
    CONSTRAINT ck_invoices_total CHECK (total_minor = subtotal_minor + tax_minor + service_minor),
    CONSTRAINT ck_invoices_due CHECK (due_at > issued_at),
    CONSTRAINT ck_invoices_number CHECK (status <> 'ISSUED' OR (invoice_number IS NOT NULL AND length(invoice_number) >= 8)),
    CONSTRAINT ck_invoices_void CHECK (status <> 'VOID' OR (voided_at IS NOT NULL AND void_reason IS NOT NULL AND length(void_reason) >= 3)),
    CONSTRAINT ck_invoices_void_lock CHECK (status <> 'VOID' OR amount_paid_minor = 0)
);

CREATE TABLE invoice_lines (
    line_id        TEXT NOT NULL,
    invoice_id     TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('NIGHTLY','SURCHARGE','CLEANING','DISCOUNT','TAX','SERVICE','OTHER')),
    description    TEXT NOT NULL CHECK (length(description) >= 3),
    quantity       INTEGER NOT NULL CHECK (quantity >= 0),
    unit_amount_minor INTEGER NOT NULL,
    amount_minor   INTEGER NOT NULL,
    rate_card_id   TEXT,
    nights         INTEGER CHECK (nights IS NULL OR nights >= 0),
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_invoice_lines PRIMARY KEY (line_id),
    CONSTRAINT fk_invoice_lines_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT fk_invoice_lines_rate_card FOREIGN KEY (rate_card_id) REFERENCES rate_cards (rate_card_id),
    CONSTRAINT ck_invoice_lines_amount CHECK (amount_minor = quantity * unit_amount_minor),
    CONSTRAINT ck_invoice_lines_discount CHECK (kind <> 'DISCOUNT' OR amount_minor <= 0)
);
CREATE INDEX ix_invoice_lines_invoice ON invoice_lines (invoice_id);

CREATE TABLE payments (
    payment_id     TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    invoice_id     TEXT NOT NULL,
    provider       TEXT NOT NULL CHECK (length(provider) >= 3),
    provider_intent_id TEXT NOT NULL CHECK (length(provider_intent_id) >= 6),
    method         TEXT NOT NULL CHECK (method IN ('HOSTED_PAGE','BANK_TRANSFER','CASH','OTHER')),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    amount_minor   INTEGER NOT NULL CHECK (amount_minor > 0),
    status         TEXT NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED','EXPIRED','REFUNDED')),
    created_at     TEXT NOT NULL,
    expires_at     TEXT,
    settled_at     TEXT,
    provider_status TEXT,
    CONSTRAINT pk_payments PRIMARY KEY (payment_id),
    CONSTRAINT fk_payments_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_payments_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT uq_payments_intent UNIQUE (provider, provider_intent_id),
    CONSTRAINT ck_payments_settled CHECK (status <> 'SUCCEEDED' OR settled_at IS NOT NULL),
    CONSTRAINT ck_payments_expiry CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE INDEX ix_payments_invoice ON payments (invoice_id, status);

CREATE TABLE payment_events (
    event_id       TEXT NOT NULL,
    provider       TEXT NOT NULL CHECK (length(provider) >= 3),
    provider_event_id TEXT NOT NULL CHECK (length(provider_event_id) >= 6),
    payment_id     TEXT,
    event_type     TEXT NOT NULL CHECK (length(event_type) >= 3),
    signature_verified INTEGER NOT NULL CHECK (signature_verified IN (0,1)),
    applied        INTEGER NOT NULL CHECK (applied IN (0,1)),
    rejection_reason TEXT,
    amount_minor   INTEGER,
    currency       TEXT,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_redacted TEXT,
    received_at    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_payment_events PRIMARY KEY (event_id),
    CONSTRAINT fk_payment_events_payment FOREIGN KEY (payment_id) REFERENCES payments (payment_id),
    CONSTRAINT uq_payment_events_provider UNIQUE (provider, provider_event_id),
    CONSTRAINT ck_payment_events_signature CHECK (applied = 0 OR signature_verified = 1),
    CONSTRAINT ck_payment_events_reject CHECK (applied = 1 OR (rejection_reason IS NOT NULL AND length(rejection_reason) >= 3)),
    CONSTRAINT ck_payment_events_currency CHECK ((amount_minor IS NULL) = (currency IS NULL))
);
CREATE INDEX ix_payment_events_payment ON payment_events (payment_id, event_type);

CREATE TABLE ledger_entries (
    entry_id       TEXT NOT NULL,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    account        TEXT NOT NULL CHECK (account IN ('CASH_IN','TAX_PAYABLE','SERVICE_FEE','GUEST_DEPOSIT','REFUND_OUT','OWNER_PAYABLE','PSP_FEE')),
    direction      TEXT NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
    amount_minor   INTEGER NOT NULL CHECK (amount_minor > 0),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    booking_id     TEXT,
    invoice_id     TEXT,
    payment_id     TEXT,
    occurred_at    TEXT NOT NULL,
    memo           TEXT NOT NULL CHECK (length(memo) >= 3),
    entry_hash     TEXT NOT NULL CHECK (length(entry_hash) = 64),
    prev_hash      TEXT NOT NULL CHECK (length(prev_hash) = 64),
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_ledger_entries PRIMARY KEY (entry_id),
    CONSTRAINT fk_ledger_entries_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT fk_ledger_entries_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT fk_ledger_entries_payment FOREIGN KEY (payment_id) REFERENCES payments (payment_id),
    CONSTRAINT uq_ledger_entries_seq UNIQUE (seq)
);
CREATE INDEX ix_ledger_entries_ref ON ledger_entries (invoice_id, account);

CREATE TABLE reconciliation_cases (
    recon_id       TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    kind           TEXT NOT NULL CHECK (kind IN ('UNMATCHED_PAYMENT','AMOUNT_MISMATCH','DUPLICATE_EVENT','REFUND_WITHOUT_PAYMENT','MISSING_SETTLEMENT','UNVERIFIED_WEBHOOK')),
    payment_event_id TEXT,
    payment_id     TEXT,
    invoice_id     TEXT,
    detected_at    TEXT NOT NULL,
    due_at         TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED','WAIVED')),
    owner_actor    TEXT,
    resolution_note TEXT,
    resolved_at    TEXT,
    details        TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_reconciliation_cases PRIMARY KEY (recon_id),
    CONSTRAINT fk_recon_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_recon_event FOREIGN KEY (payment_event_id) REFERENCES payment_events (event_id),
    CONSTRAINT fk_recon_payment FOREIGN KEY (payment_id) REFERENCES payments (payment_id),
    CONSTRAINT fk_recon_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT ck_recon_due CHECK (due_at > detected_at),
    CONSTRAINT ck_recon_resolved CHECK (status = 'OPEN' OR (resolved_at IS NOT NULL AND resolution_note IS NOT NULL AND length(resolution_note) >= 3))
);
CREATE INDEX ix_recon_open ON reconciliation_cases (agency_id, status, due_at);

CREATE TABLE documents (
    document_id    TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    document_key   TEXT NOT NULL CHECK (length(document_key) >= 6),
    kind           TEXT NOT NULL CHECK (kind IN ('CONFIRMATION','RECEIPT','INVOICE','CONTRACT','NOTICE','REPORT')),
    version        INTEGER NOT NULL CHECK (version >= 1),
    format         TEXT NOT NULL CHECK (format IN ('HTML','TEXT','MARKDOWN')),
    booking_id     TEXT,
    invoice_id     TEXT,
    case_id        TEXT,
    content_class  TEXT NOT NULL CHECK (content_class IN ('P0','P1','P2','P3','P4')),
    redaction_profile TEXT NOT NULL CHECK (length(redaction_profile) >= 3),
    storage_ref    TEXT,
    body_sha256    TEXT NOT NULL CHECK (length(body_sha256) = 64),
    status         TEXT NOT NULL CHECK (status IN ('GENERATED','SUPERSEDED','VOID')),
    generated_by   TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_documents PRIMARY KEY (document_id),
    CONSTRAINT fk_documents_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_documents_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT fk_documents_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT fk_documents_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT uq_documents_version UNIQUE (document_key, version),
    CONSTRAINT ck_documents_storage CHECK (status <> 'GENERATED' OR (storage_ref IS NOT NULL AND length(storage_ref) >= 3))
);
CREATE INDEX ix_documents_key ON documents (document_key, version);

CREATE TABLE ops_tasks (
    task_id        TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    kind           TEXT NOT NULL CHECK (kind IN ('CHECKIN_PREP','CHECKOUT_INSPECTION','CLEANING','MAINTENANCE','GUEST_FOLLOWUP','PAYMENT_CHASE','DOCUMENT_CHASE','VERIFICATION')),
    booking_id     TEXT,
    unit_id        TEXT,
    case_id        TEXT,
    due_at         TEXT NOT NULL,
    priority       TEXT NOT NULL CHECK (priority IN ('LOW','NORMAL','HIGH','URGENT')),
    assignee       TEXT,
    status         TEXT NOT NULL CHECK (status IN ('OPEN','IN_PROGRESS','BLOCKED','DONE','CANCELLED')),
    checklist      TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    completed_at   TEXT,
    blocked_reason TEXT,
    CONSTRAINT pk_ops_tasks PRIMARY KEY (task_id),
    CONSTRAINT fk_ops_tasks_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_ops_tasks_booking FOREIGN KEY (booking_id) REFERENCES bookings (booking_id),
    CONSTRAINT fk_ops_tasks_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_ops_tasks_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT uq_ops_tasks_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_ops_tasks_done CHECK (status NOT IN ('DONE','CANCELLED') OR completed_at IS NOT NULL),
    CONSTRAINT ck_ops_tasks_blocked CHECK (status <> 'BLOCKED' OR (blocked_reason IS NOT NULL AND length(blocked_reason) >= 3))
);
CREATE INDEX ix_ops_tasks_open ON ops_tasks (status, due_at);

-- Kewajiban notifikasi diwakili oleh TENGGAT (notice_due_at), bukan oleh timestamp
-- pengiriman: memaksa notified_at terisi saat insert berarti database memaksa tim
-- mencatat kebohongan (atau menyembunyikan kewajiban). Status "belum dikirim" harus
-- bisa disimpan supaya SLA 72 jam-nya bisa ditagih dan terlihat di dashboard.
CREATE TABLE incidents (
    incident_id    TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    severity       TEXT NOT NULL CHECK (severity IN ('SEV1','SEV2','SEV3','SEV4')),
    title          TEXT NOT NULL CHECK (length(title) >= 8),
    detected_at    TEXT NOT NULL,
    detected_by    TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (length(source) >= 3),
    playbook       TEXT NOT NULL CHECK (length(playbook) >= 3),
    affected_refs  TEXT NOT NULL DEFAULT '[]',
    personal_data_involved INTEGER NOT NULL DEFAULT 0 CHECK (personal_data_involved IN (0,1)),
    regulatory_notification_required INTEGER NOT NULL DEFAULT 0 CHECK (regulatory_notification_required IN (0,1)),
    notice_due_at  TEXT,
    notified_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('OPEN','MITIGATING','RESOLVED','POSTMORTEM_DONE')),
    summary        TEXT,
    resolved_at    TEXT,
    postmortem_ref TEXT,
    created_at     TEXT NOT NULL,
    CONSTRAINT pk_incidents PRIMARY KEY (incident_id),
    CONSTRAINT fk_incidents_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT ck_incidents_regulatory CHECK (regulatory_notification_required = 0 OR severity IN ('SEV1','SEV2')),
    CONSTRAINT ck_incidents_notice_deadline CHECK (
        regulatory_notification_required = 0
        OR (notice_due_at IS NOT NULL AND notice_due_at > detected_at)
    ),
    CONSTRAINT ck_incidents_resolved CHECK (status NOT IN ('RESOLVED','POSTMORTEM_DONE') OR (resolved_at IS NOT NULL AND summary IS NOT NULL AND length(summary) >= 10)),
    CONSTRAINT ck_incidents_postmortem CHECK (status <> 'POSTMORTEM_DONE' OR (postmortem_ref IS NOT NULL AND length(postmortem_ref) >= 3))
);
CREATE INDEX ix_incidents_open ON incidents (status, severity);

CREATE TABLE incident_steps (
    step_id        TEXT NOT NULL,
    incident_id    TEXT NOT NULL,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    action         TEXT NOT NULL CHECK (length(action) >= 3),
    actor          TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (length(outcome) >= 3),
    evidence_refs  TEXT NOT NULL DEFAULT '[]',
    occurred_at    TEXT NOT NULL,
    CONSTRAINT pk_incident_steps PRIMARY KEY (step_id),
    CONSTRAINT fk_incident_steps_incident FOREIGN KEY (incident_id) REFERENCES incidents (incident_id),
    CONSTRAINT uq_incident_steps_seq UNIQUE (incident_id, seq)
);

-- Immutability guards: the database itself refuses to rewrite history.
-- --------------------------------------------------------------------------
CREATE TRIGGER trg_audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_booking_state_events_no_update
BEFORE UPDATE ON booking_state_events
BEGIN
    SELECT RAISE(ABORT, 'booking_state_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_booking_state_events_no_delete
BEFORE DELETE ON booking_state_events
BEGIN
    SELECT RAISE(ABORT, 'booking_state_events is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_policy_versions_no_update
BEFORE UPDATE ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_policy_versions_no_delete
BEFORE DELETE ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_rate_cards_no_update
BEFORE UPDATE ON rate_cards
BEGIN
    SELECT RAISE(ABORT, 'rate_cards is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_rate_cards_no_delete
BEFORE DELETE ON rate_cards
BEGIN
    SELECT RAISE(ABORT, 'rate_cards is append-only (DELETE rejected)');
END;

-- Phase 2 append-only logs: the same guard as booking_state_events/audit_events.
CREATE TRIGGER trg_case_messages_no_update
BEFORE UPDATE ON case_messages
BEGIN
    SELECT RAISE(ABORT, 'case_messages is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_case_messages_no_delete
BEFORE DELETE ON case_messages
BEGIN
    SELECT RAISE(ABORT, 'case_messages is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_case_events_no_update
BEFORE UPDATE ON case_events
BEGIN
    SELECT RAISE(ABORT, 'case_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_case_events_no_delete
BEFORE DELETE ON case_events
BEGIN
    SELECT RAISE(ABORT, 'case_events is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_intake_proposals_no_update
BEFORE UPDATE ON intake_proposals
BEGIN
    SELECT RAISE(ABORT, 'intake_proposals is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_intake_proposals_no_delete
BEFORE DELETE ON intake_proposals
BEGIN
    SELECT RAISE(ABORT, 'intake_proposals is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_match_runs_no_update
BEFORE UPDATE ON match_runs
BEGIN
    SELECT RAISE(ABORT, 'match_runs is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_match_runs_no_delete
BEFORE DELETE ON match_runs
BEGIN
    SELECT RAISE(ABORT, 'match_runs is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_match_candidates_no_update
BEFORE UPDATE ON match_candidates
BEGIN
    SELECT RAISE(ABORT, 'match_candidates is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_match_candidates_no_delete
BEFORE DELETE ON match_candidates
BEGIN
    SELECT RAISE(ABORT, 'match_candidates is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_approval_decisions_no_update
BEFORE UPDATE ON approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'approval_decisions is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_approval_decisions_no_delete
BEFORE DELETE ON approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'approval_decisions is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_payment_events_no_update
BEFORE UPDATE ON payment_events
BEGIN
    SELECT RAISE(ABORT, 'payment_events: riwayat append-only tidak boleh diubah (no UPDATE)');
END;

CREATE TRIGGER trg_payment_events_no_delete
BEFORE DELETE ON payment_events
BEGIN
    SELECT RAISE(ABORT, 'payment_events: riwayat append-only tidak boleh dihapus (no DELETE)');
END;

CREATE TRIGGER trg_ledger_entries_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries: riwayat append-only tidak boleh diubah (no UPDATE)');
END;

CREATE TRIGGER trg_ledger_entries_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries: riwayat append-only tidak boleh dihapus (no DELETE)');
END;

CREATE TRIGGER trg_incident_steps_no_update
BEFORE UPDATE ON incident_steps
BEGIN
    SELECT RAISE(ABORT, 'incident_steps: riwayat append-only tidak boleh diubah (no UPDATE)');
END;

CREATE TRIGGER trg_incident_steps_no_delete
BEFORE DELETE ON incident_steps
BEGIN
    SELECT RAISE(ABORT, 'incident_steps: riwayat append-only tidak boleh dihapus (no DELETE)');
END;

INSERT INTO agencies (agency_id, name, status, created_at, updated_at)
VALUES ('agency_default', 'Default Agency', 'ACTIVE', '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00');

INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)
VALUES ('supplier_default', 'agency_default', 'Default Supplier', 'ACTIVE', '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00');

INSERT INTO villa_supplier_relationships (
    villa_supplier_relationship_id, agency_id, supplier_id, property_id, unit_id, status, created_at, updated_at
)
VALUES (
    'route_default', 'agency_default', 'supplier_default', NULL, NULL, 'ACTIVE',
    '2026-09-21T00:00:00+00:00', '2026-09-21T00:00:00+00:00'
);

INSERT INTO schema_meta (schema_version, applied_at, description)
VALUES ('001_initial', '2026-09-21T00:00:00+00:00', 'Villa Agency OS initial schema: inventory, policy, rates, evidence, quotes, holds, bookings, audit, cases, intake, matching, approvals, drafts, SLA, billing, payments, reconciliation, documents, ops, incidents');

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

-- Provenance is not optional (v1.1 §12): every commercial fact records where it came
-- from, how it was read, and who verified it. The pipeline is
--   DRAFT → PENDING_VERIFICATION → VERIFIED → APPROVED → ACTIVE → SUPERSEDED/EXPIRED
-- and AI_EXTRACTED → ACTIVE is forbidden: an extraction may only ever be a *draft*
-- source, because the CHECK below refuses ACTIVE without a named human verifier and
-- approver. That is the invariant enforced structurally, not by convention.
CREATE TABLE contract_versions (
    contract_version_id TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    villa_supplier_relationship_id TEXT NOT NULL,
    version      INTEGER NOT NULL CHECK (version >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('DRAFT','PENDING_VERIFICATION','VERIFIED','APPROVED','ACTIVE','SUPERSEDED','EXPIRED')),
    effective_from TEXT NOT NULL,
    effective_to   TEXT NOT NULL,
    source_doc_ref TEXT NOT NULL CHECK (length(source_doc_ref) >= 1),
    source_reference TEXT NOT NULL CHECK (length(source_reference) >= 1),
    source_evidence_id TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'HUMAN' CHECK (extraction_method IN ('HUMAN','AI_EXTRACTED')),
    verified_by TEXT,
    verified_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_contract_versions PRIMARY KEY (contract_version_id),
    CONSTRAINT fk_contract_evidence FOREIGN KEY (source_evidence_id) REFERENCES evidence_objects (evidence_id),
    CONSTRAINT fk_contract_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_contract_route FOREIGN KEY (villa_supplier_relationship_id) REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT uq_contract_version UNIQUE (villa_supplier_relationship_id, version),
    CONSTRAINT ck_contract_dates CHECK (effective_from <= effective_to),
    CONSTRAINT ck_contract_verified CHECK (
        status NOT IN ('VERIFIED','APPROVED','ACTIVE')
        OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)
    ),
    CONSTRAINT ck_contract_activation_is_human CHECK (
        status <> 'ACTIVE' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);

-- A rate is money, so it carries an amount and a minimum stay, and it must name the
-- contract it belongs to before it can be ACTIVE (v1.1 §2 VILLA TRUTH: a rate comes
-- from verified applicable contract data). Nightly amount is the WAVE 2 record;
-- season segments and occupancy rules arrive with Pricing & Quotation (WAVE 4).
CREATE TABLE rate_versions (
    rate_version_id TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    villa_supplier_relationship_id TEXT NOT NULL,
    contract_version_id TEXT,
    version      INTEGER NOT NULL CHECK (version >= 1),
    status       TEXT    NOT NULL CHECK (status IN ('DRAFT','PENDING_VERIFICATION','VERIFIED','APPROVED','ACTIVE','SUPERSEDED','EXPIRED')),
    currency     TEXT    NOT NULL CHECK (length(currency) = 3),
    nightly_minor INTEGER NOT NULL CHECK (nightly_minor >= 0),
    min_stay     INTEGER NOT NULL DEFAULT 1 CHECK (min_stay >= 1),
    effective_from TEXT NOT NULL,
    effective_to   TEXT NOT NULL,
    source_doc_ref TEXT NOT NULL CHECK (length(source_doc_ref) >= 1),
    source_reference TEXT NOT NULL CHECK (length(source_reference) >= 1),
    source_evidence_id TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'HUMAN' CHECK (extraction_method IN ('HUMAN','AI_EXTRACTED')),
    verified_by TEXT,
    verified_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CONSTRAINT pk_rate_versions PRIMARY KEY (rate_version_id),
    CONSTRAINT fk_rate_evidence FOREIGN KEY (source_evidence_id) REFERENCES evidence_objects (evidence_id),
    CONSTRAINT fk_rate_version_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_rate_version_route FOREIGN KEY (villa_supplier_relationship_id) REFERENCES villa_supplier_relationships (villa_supplier_relationship_id),
    CONSTRAINT fk_rate_version_contract FOREIGN KEY (contract_version_id) REFERENCES contract_versions (contract_version_id),
    CONSTRAINT uq_rate_version UNIQUE (villa_supplier_relationship_id, version),
    CONSTRAINT ck_rate_version_dates CHECK (effective_from <= effective_to),
    CONSTRAINT ck_rate_version_verified CHECK (
        status NOT IN ('VERIFIED','APPROVED','ACTIVE')
        OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)
    ),
    CONSTRAINT ck_rate_version_activation_is_human CHECK (
        status <> 'ACTIVE'
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND contract_version_id IS NOT NULL)
    )
);
CREATE INDEX ix_contract_window ON contract_versions (agency_id, villa_supplier_relationship_id, status, effective_from);
CREATE INDEX ix_rate_window ON rate_versions (agency_id, villa_supplier_relationship_id, status, effective_from);

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
    result         TEXT    NOT NULL CHECK (result IN ('AVAILABLE','NOT_AVAILABLE','AVAILABLE_BUT_RESTRICTED','UNKNOWN')),
    restrictions   TEXT,
    occupancy_ranges TEXT  NOT NULL DEFAULT '[]',
    can_quote      INTEGER NOT NULL CHECK (can_quote IN (0,1)),
    can_hold       INTEGER NOT NULL CHECK (can_hold IN (0,1)),
    can_confirm    INTEGER NOT NULL CHECK (can_confirm IN (0,1)),
    notes          TEXT,
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_availability_evidence PRIMARY KEY (evidence_id),
    CONSTRAINT ck_evidence_restriction_stated CHECK (result <> 'AVAILABLE_BUT_RESTRICTED' OR restrictions IS NOT NULL),
    CONSTRAINT fk_evidence_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_evidence_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT ck_evidence_window CHECK (captured_at < valid_until),
    CONSTRAINT ck_evidence_confirm_requires_ab CHECK (can_confirm = 0 OR evidence_level IN ('A','B'))
);
CREATE INDEX ix_evidence_unit ON availability_evidence (unit_id, captured_at);

-- --------------------------------------------------------------------------
-- Availability checks (§7): the request half of CHECK AVAILABILITY
-- --------------------------------------------------------------------------
-- §7 opens with CLIENT INQUIRY -> CHECK AVAILABILITY, and Appendix A lists
-- CHECK_REQUESTED and CHECKING among the availability states. availability_evidence
-- holds only the answers a source reports, so the request needs its own home; without
-- one, those two states exist in the vocabulary and nowhere in the system.
--
-- The state lives in an append-only event log and is derived from it. A mutable
-- status column would let the request row and its log disagree, and the log is the
-- one that has to win.
CREATE TABLE availability_checks (
    check_id       TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    unit_id        TEXT    NOT NULL,
    check_in       TEXT    NOT NULL,
    check_out      TEXT    NOT NULL,
    requested_by   TEXT    NOT NULL CHECK (length(requested_by) >= 3),
    requested_role TEXT    NOT NULL CHECK (length(requested_role) >= 1),
    requested_at   TEXT    NOT NULL,
    notes          TEXT,
    CONSTRAINT pk_availability_checks PRIMARY KEY (check_id),
    CONSTRAINT fk_availability_checks_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_availability_checks_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT ck_availability_checks_window CHECK (check_in < check_out),
    CONSTRAINT ck_availability_checks_notes CHECK (notes IS NULL OR length(notes) <= 500)
);
CREATE INDEX ix_availability_checks_unit ON availability_checks (agency_id, unit_id, check_in);

CREATE TABLE availability_check_events (
    check_event_id TEXT    NOT NULL,
    check_id       TEXT    NOT NULL,
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    state          TEXT    NOT NULL CHECK (state IN ('CHECK_REQUESTED','CHECKING','ANSWERED')),
    actor          TEXT    NOT NULL CHECK (length(actor) >= 3),
    actor_role     TEXT    NOT NULL CHECK (length(actor_role) >= 1),
    evidence_id    TEXT,
    note           TEXT,
    occurred_at    TEXT    NOT NULL,
    CONSTRAINT pk_availability_check_events PRIMARY KEY (check_event_id),
    CONSTRAINT fk_ace_check FOREIGN KEY (check_id) REFERENCES availability_checks (check_id),
    CONSTRAINT fk_ace_evidence FOREIGN KEY (evidence_id) REFERENCES availability_evidence (evidence_id),
    CONSTRAINT uq_ace_seq UNIQUE (check_id, seq),
    CONSTRAINT ck_ace_answer_has_evidence CHECK (state <> 'ANSWERED' OR evidence_id IS NOT NULL),
    CONSTRAINT ck_ace_open_has_no_evidence CHECK (state = 'ANSWERED' OR evidence_id IS NULL),
    CONSTRAINT ck_ace_note CHECK (note IS NULL OR length(note) <= 500)
);
CREATE INDEX ix_ace_check ON availability_check_events (check_id, seq);

CREATE TRIGGER trg_availability_checks_no_update
BEFORE UPDATE ON availability_checks
BEGIN
    SELECT RAISE(ABORT, 'availability_checks is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_availability_checks_no_delete
BEFORE DELETE ON availability_checks
BEGIN
    SELECT RAISE(ABORT, 'availability_checks is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_availability_check_events_no_update
BEFORE UPDATE ON availability_check_events
BEGIN
    SELECT RAISE(ABORT, 'availability_check_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_availability_check_events_no_delete
BEFORE DELETE ON availability_check_events
BEGIN
    SELECT RAISE(ABORT, 'availability_check_events is append-only (DELETE rejected)');
END;

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
-- Quotation versions, snapshots and acceptance (§11). §11 requires immutable
-- versioning (V1/V2/V3) and forbids overwriting a version that was sent or
-- accepted, so the chain is append-only and the priced content freezes at the
-- moment of sending. An acceptance records the exact snapshots the guest agreed
-- to — quotation_version_id, price_snapshot_id, terms_snapshot_id,
-- policy_snapshot_id, accepted_at — because a price that can be re-derived
-- later is not the same thing as a price that was accepted. The lineage of a
-- quotation is the stay it covers (unit + window), which is the identity the
-- ``quotes`` table already uses.
-- --------------------------------------------------------------------------
CREATE TABLE quotation_versions (
    quotation_version_id TEXT    NOT NULL,
    agency_id            TEXT    NOT NULL DEFAULT 'agency_default',
    property_id          TEXT    NOT NULL,
    unit_id              TEXT    NOT NULL,
    check_in             TEXT    NOT NULL,
    check_out            TEXT    NOT NULL,
    version              INTEGER NOT NULL CHECK (version >= 1),
    quote_id             TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('DRAFT','SENT','ACCEPTED','SUPERSEDED','VOID')),
    supersedes_id        TEXT,
    created_by           TEXT    NOT NULL CHECK (length(created_by) >= 3),
    created_at           TEXT    NOT NULL,
    sent_at              TEXT,
    CONSTRAINT pk_quotation_versions PRIMARY KEY (quotation_version_id),
    CONSTRAINT fk_quotation_versions_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_quotation_versions_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_quotation_versions_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_quotation_versions_quote FOREIGN KEY (quote_id) REFERENCES quotes (quote_id),
    CONSTRAINT fk_quotation_versions_supersedes FOREIGN KEY (supersedes_id) REFERENCES quotation_versions (quotation_version_id),
    CONSTRAINT uq_quotation_versions_version UNIQUE (agency_id, unit_id, check_in, check_out, version),
    CONSTRAINT ck_quotation_versions_stay CHECK (check_in < check_out),
    CONSTRAINT ck_quotation_versions_supersedes CHECK (supersedes_id IS NULL OR supersedes_id <> quotation_version_id),
    -- A draft has never been sent; a version that was sent carries the timestamp of that
    -- send. A void version is either case: it may be withdrawn before it is sent or after,
    -- and the withdrawal must not erase the fact that it was sent (the freeze trigger keeps
    -- ``sent_at`` immutable for exactly that reason).
    CONSTRAINT ck_quotation_versions_sent CHECK (
        (status = 'DRAFT' AND sent_at IS NULL)
        OR (status IN ('SENT','ACCEPTED','SUPERSEDED') AND sent_at IS NOT NULL)
        OR status = 'VOID'
    )
);
CREATE INDEX ix_quotation_versions_stay ON quotation_versions (agency_id, unit_id, check_in, check_out);

CREATE TABLE quotation_snapshots (
    snapshot_id          TEXT NOT NULL,
    agency_id            TEXT NOT NULL DEFAULT 'agency_default',
    quotation_version_id TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('PRICE','TERMS','POLICY')),
    source_ref           TEXT,
    payload              TEXT NOT NULL,
    fingerprint          TEXT NOT NULL CHECK (length(fingerprint) = 64),
    created_at           TEXT NOT NULL,
    CONSTRAINT pk_quotation_snapshots PRIMARY KEY (snapshot_id),
    CONSTRAINT fk_quotation_snapshots_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_quotation_snapshots_version FOREIGN KEY (quotation_version_id) REFERENCES quotation_versions (quotation_version_id),
    -- Exactly one snapshot of each kind per version: two price snapshots for one
    -- version would leave "what the guest agreed to" ambiguous.
    CONSTRAINT uq_quotation_snapshots_kind UNIQUE (quotation_version_id, kind)
);

CREATE TABLE quotation_acceptances (
    acceptance_id        TEXT NOT NULL,
    agency_id            TEXT NOT NULL DEFAULT 'agency_default',
    quotation_version_id TEXT NOT NULL,
    price_snapshot_id    TEXT NOT NULL,
    terms_snapshot_id    TEXT NOT NULL,
    policy_snapshot_id   TEXT NOT NULL,
    accepted_at          TEXT NOT NULL,
    recorded_by          TEXT NOT NULL CHECK (length(recorded_by) >= 3),
    created_at           TEXT NOT NULL,
    CONSTRAINT pk_quotation_acceptances PRIMARY KEY (acceptance_id),
    CONSTRAINT fk_quotation_acceptances_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_quotation_acceptances_version FOREIGN KEY (quotation_version_id) REFERENCES quotation_versions (quotation_version_id),
    CONSTRAINT fk_quotation_acceptances_price FOREIGN KEY (price_snapshot_id) REFERENCES quotation_snapshots (snapshot_id),
    CONSTRAINT fk_quotation_acceptances_terms FOREIGN KEY (terms_snapshot_id) REFERENCES quotation_snapshots (snapshot_id),
    CONSTRAINT fk_quotation_acceptances_policy FOREIGN KEY (policy_snapshot_id) REFERENCES quotation_snapshots (snapshot_id),
    CONSTRAINT uq_quotation_acceptances_version UNIQUE (quotation_version_id)
);

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
    status             TEXT    NOT NULL CHECK (status IN ('REQUESTED','AWAITING_SUPPLIER','CONFIRMED','REJECTED','EXTENSION_REQUESTED','RELEASE_REQUESTED','RELEASE_CONFIRMED','EXPIRED_PENDING_VERIFICATION','CONVERTED')),
    guest_ref          TEXT    NOT NULL CHECK (length(guest_ref) >= 3),
    quote_ref          TEXT,
    policy_version_id  TEXT    NOT NULL,
    evidence_id        TEXT    NOT NULL,
    idempotency_key    TEXT    NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at         TEXT    NOT NULL,
    expires_at         TEXT    NOT NULL,
    expires_timezone   TEXT    NOT NULL CHECK (length(expires_timezone) >= 3),
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
    CONSTRAINT ck_holds_closed CHECK ((status IN ('REQUESTED','AWAITING_SUPPLIER','CONFIRMED','EXTENSION_REQUESTED','RELEASE_REQUESTED')) OR (closed_at IS NOT NULL))
);
-- Defence in depth: two ACTIVE holds may never share a unit + arrival date.
-- The full overlap test (partial days) is done inside the write transaction;
-- this index makes the most common collision impossible even if that fails.
CREATE UNIQUE INDEX uq_holds_active_slot ON holds (unit_id, check_in) WHERE status IN ('REQUESTED','AWAITING_SUPPLIER','CONFIRMED','EXTENSION_REQUESTED','RELEASE_REQUESTED','EXPIRED_PENDING_VERIFICATION');

CREATE TABLE hold_extension_requests (
    extension_id       TEXT    NOT NULL,
    agency_id          TEXT    NOT NULL DEFAULT 'agency_default',
    hold_id            TEXT    NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 1),
    requested_minutes  INTEGER NOT NULL CHECK (requested_minutes > 0),
    requested_timezone TEXT    NOT NULL CHECK (length(requested_timezone) >= 3),
    requested_at       TEXT    NOT NULL,
    requested_by       TEXT,
    status             TEXT    NOT NULL CHECK (status IN ('REQUESTED','CONFIRMED','REJECTED')),
    decided_at         TEXT,
    decided_by         TEXT,
    decided_timezone   TEXT,
    reason             TEXT,
    CONSTRAINT pk_hold_extension_requests PRIMARY KEY (extension_id),
    CONSTRAINT fk_hold_ext_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_hold_ext_hold FOREIGN KEY (hold_id) REFERENCES holds (hold_id),
    CONSTRAINT ck_hold_ext_decided CHECK ((status = 'REQUESTED') OR (decided_at IS NOT NULL)),
    CONSTRAINT uq_hold_ext_sequence UNIQUE (hold_id, seq)
);

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
-- Guest Master & confirmed inquiry requirements (Master Spec v1.1 §5, §6, §50 WAVE 3)
--
-- Two distinctions this section exists to make, because both are places where a
-- platform quietly loses its source of truth:
--
--  * A **guest** is MASTER data with an identity (Guest Master), not the free-text
--    `guest_ref` the case spine was built around. The old column stays for legacy
--    rows; new rows link a real guest.
--  * **intake_fields** are *proposals* (PROPOSED/ACCEPTED/REJECTED, with confidence)
--    produced by extraction; **inquiry_requirements** is the confirmed trip
--    requirement that the inquiry is actually priced and matched against (§6:
--    "Current trip requirements → Inquiry"). A requirement may cite the accepted
--    intake field it came from, so the AI-extracted → verified step stays visible
--    instead of being assumed.
-- --------------------------------------------------------------------------
CREATE TABLE guests (
    guest_id     TEXT NOT NULL,
    agency_id    TEXT NOT NULL DEFAULT 'agency_default',
    full_name    TEXT NOT NULL CHECK (length(full_name) BETWEEN 2 AND 200),
    email        TEXT,
    phone        TEXT,
    locale       TEXT NOT NULL DEFAULT 'en-US' CHECK (length(locale) BETWEEN 2 AND 35),
    status       TEXT NOT NULL CHECK (status IN ('ACTIVE','ARCHIVED')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CONSTRAINT pk_guests PRIMARY KEY (guest_id),
    CONSTRAINT fk_guests_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    -- A guest nobody can reach is not a guest record; at least one channel is required.
    CONSTRAINT ck_guests_contactable CHECK (email IS NOT NULL OR phone IS NOT NULL)
);
CREATE UNIQUE INDEX uq_guest_email ON guests (agency_id, email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX uq_guest_phone ON guests (agency_id, phone) WHERE phone IS NOT NULL;


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
    reference      TEXT,
    guest_id       TEXT,
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
    CONSTRAINT fk_cases_guest FOREIGN KEY (guest_id) REFERENCES guests (guest_id),
    CONSTRAINT uq_cases_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_cases_sla_window CHECK (created_at < first_response_due_at AND first_response_due_at <= quote_due_at),
    CONSTRAINT ck_cases_closed CHECK (status NOT LIKE 'CLOSED_%' OR closed_at IS NOT NULL),
    CONSTRAINT ck_cases_close_reason CHECK (status NOT LIKE 'CLOSED_%' OR (close_reason IS NOT NULL AND length(close_reason) >= 3))
);
CREATE INDEX ix_cases_status ON cases (status, first_response_due_at);
-- Human-readable references (Appendix B) are per agency, and a legacy row may have
-- none: the index is partial so NULL keeps meaning "not yet allocated" while a real
-- reference can never be duplicated inside one agency.
CREATE UNIQUE INDEX uq_cases_reference ON cases (agency_id, reference) WHERE reference IS NOT NULL;
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

CREATE TABLE inquiry_requirements (
    requirement_id   TEXT NOT NULL,
    agency_id        TEXT NOT NULL DEFAULT 'agency_default',
    case_id          TEXT NOT NULL,
    field_name       TEXT NOT NULL CHECK (length(field_name) BETWEEN 2 AND 64),
    field_value      TEXT,
    value_type       TEXT NOT NULL CHECK (value_type IN ('date','integer','text','money','boolean','enum')),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','WITHDRAWN')),
    source           TEXT NOT NULL CHECK (source IN ('HUMAN_ENTRY','ACCEPTED_INTAKE')),
    source_intake_field_id TEXT,
    supersedes_requirement_id TEXT,
    recorded_by      TEXT NOT NULL CHECK (length(recorded_by) >= 1),
    recorded_at      TEXT NOT NULL,
    CONSTRAINT pk_inquiry_requirements PRIMARY KEY (requirement_id),
    CONSTRAINT fk_requirements_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_requirements_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT fk_requirements_intake FOREIGN KEY (source_intake_field_id) REFERENCES intake_fields (intake_field_id),
    CONSTRAINT fk_requirements_supersedes FOREIGN KEY (supersedes_requirement_id) REFERENCES inquiry_requirements (requirement_id),
    CONSTRAINT ck_requirements_provenance CHECK (
        source <> 'ACCEPTED_INTAKE' OR source_intake_field_id IS NOT NULL
    ),
    CONSTRAINT ck_requirements_not_self_superseding CHECK (
        supersedes_requirement_id IS NULL OR supersedes_requirement_id <> requirement_id
    ),
    CONSTRAINT ck_requirements_value_present CHECK (
        field_value IS NOT NULL AND length(field_value) >= 1
    )
);
-- One ACTIVE value per field: a guest who changes their mind gets a new version, never
-- two competing truths about the same trip.
CREATE UNIQUE INDEX uq_requirement_active_field
    ON inquiry_requirements (agency_id, case_id, field_name) WHERE status = 'ACTIVE';
CREATE INDEX ix_requirements_case ON inquiry_requirements (agency_id, case_id, status);


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
    CONSTRAINT fk_approval_decisions_request FOREIGN KEY (approval_id)
        REFERENCES approval_requests (approval_id),
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
    CONSTRAINT fk_drafts_approval FOREIGN KEY (approval_id)
        REFERENCES approval_requests (approval_id),
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
    CONSTRAINT uq_invoices_number UNIQUE (agency_id, invoice_number),
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
    CONSTRAINT uq_documents_version UNIQUE (agency_id, document_key, version),
    CONSTRAINT ck_documents_storage CHECK (status <> 'GENERATED' OR (storage_ref IS NOT NULL AND length(storage_ref) >= 3))
);
CREATE INDEX ix_documents_key ON documents (agency_id, document_key, version);

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

-- --------------------------------------------------------------------------
-- Option sets (§5: INQUIRY -> OPTION SETS -> VILLA; §34 Option Set Builder)
-- --------------------------------------------------------------------------
-- What an agency offers for one inquiry. Append-only like the rest of the ledger:
-- a change is a new version, and presenting the set is a row of its own, so
-- nothing a guest has already been shown can be edited afterwards.

CREATE TABLE option_sets (
    option_set_id  TEXT    NOT NULL,
    case_id        TEXT    NOT NULL,
    version        INTEGER NOT NULL CHECK (version >= 1),
    label          TEXT,
    created_by     TEXT    NOT NULL CHECK (length(created_by) >= 3),
    created_at     TEXT    NOT NULL,
    CONSTRAINT pk_option_sets PRIMARY KEY (option_set_id),
    CONSTRAINT fk_option_sets_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT uq_option_sets_version UNIQUE (case_id, version)
);

CREATE TABLE option_set_items (
    option_set_item_id  TEXT    NOT NULL,
    option_set_id       TEXT    NOT NULL,
    position            INTEGER NOT NULL CHECK (position >= 1),
    property_id         TEXT    NOT NULL,
    unit_id             TEXT,
    source_match_run_id TEXT,
    note                TEXT,
    created_at          TEXT    NOT NULL,
    CONSTRAINT pk_option_set_items PRIMARY KEY (option_set_item_id),
    CONSTRAINT fk_option_set_items_set FOREIGN KEY (option_set_id) REFERENCES option_sets (option_set_id),
    CONSTRAINT fk_option_set_items_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_option_set_items_unit FOREIGN KEY (unit_id) REFERENCES units (unit_id),
    CONSTRAINT fk_option_set_items_run FOREIGN KEY (source_match_run_id) REFERENCES match_runs (match_run_id),
    CONSTRAINT uq_option_set_items_position UNIQUE (option_set_id, position),
    CONSTRAINT uq_option_set_items_property UNIQUE (option_set_id, property_id),
    CONSTRAINT ck_option_set_items_note CHECK (note IS NULL OR length(note) <= 500)
);

CREATE TABLE option_set_presentations (
    presentation_id TEXT NOT NULL,
    option_set_id   TEXT NOT NULL,
    presented_by    TEXT NOT NULL CHECK (length(presented_by) >= 3),
    presented_at    TEXT NOT NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('website','email','whatsapp_business','dashboard','phone')),
    CONSTRAINT pk_option_set_presentations PRIMARY KEY (presentation_id),
    CONSTRAINT fk_option_set_presentations_set FOREIGN KEY (option_set_id) REFERENCES option_sets (option_set_id),
    CONSTRAINT uq_option_set_presentations_set UNIQUE (option_set_id)
);

-- §16 Policy Engine (WAVE 1-H): the governance policy vocabulary. §16 requires
-- policy to be a first-class component instead of numbers scattered across the
-- source, so a key is declared in policy_defs and its values live in
-- policy_revisions. Priced policy (deposit %, tax, quote validity) keeps its own
-- home in policy_versions; the two stores never hold the same value.
-- --------------------------------------------------------------------------
CREATE TABLE policy_defs (
    policy_key   TEXT NOT NULL,
    domain       TEXT NOT NULL,
    value_type   TEXT NOT NULL,
    risk_level   TEXT NOT NULL,
    owner_role   TEXT NOT NULL CHECK (length(owner_role) >= 1),
    spec_ref     TEXT NOT NULL CHECK (length(spec_ref) >= 1),
    description  TEXT NOT NULL CHECK (length(description) >= 1),
    created_at   TEXT NOT NULL,
    CONSTRAINT pk_policy_defs PRIMARY KEY (policy_key),
    CONSTRAINT ck_policy_defs_domain CHECK (domain IN ('SECURITY','FINANCE','COMMERCIAL','AVAILABILITY','OPERATIONS','RETENTION')),
    CONSTRAINT ck_policy_defs_value_type CHECK (value_type IN ('MINUTES','HOURS','DAYS','COUNT','PCT','BOOL','JSON','ROLE_SET','TEXT')),
    CONSTRAINT ck_policy_defs_risk CHECK (risk_level IN ('LOW','MEDIUM','HIGH'))
);

CREATE TABLE policy_revisions (
    policy_revision_id TEXT NOT NULL,
    agency_id          TEXT NOT NULL DEFAULT 'agency_default',
    policy_key         TEXT NOT NULL,
    version            INTEGER NOT NULL CHECK (version >= 1),
    value              TEXT NOT NULL CHECK (length(value) >= 1),
    effective_from     TEXT NOT NULL,
    effective_to       TEXT NOT NULL,
    source_doc_ref     TEXT NOT NULL CHECK (length(source_doc_ref) >= 1),
    approved_by        TEXT NOT NULL CHECK (length(approved_by) >= 1),
    fingerprint        TEXT NOT NULL CHECK (length(fingerprint) = 64),
    created_at         TEXT NOT NULL,
    CONSTRAINT pk_policy_revisions PRIMARY KEY (policy_revision_id),
    CONSTRAINT fk_policy_revisions_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_policy_revisions_def FOREIGN KEY (policy_key) REFERENCES policy_defs (policy_key),
    CONSTRAINT uq_policy_revisions_version UNIQUE (agency_id, policy_key, version),
    CONSTRAINT ck_policy_revisions_dates CHECK (effective_from <= effective_to)
);

-- §18/§56 AI service identity (WAVE 1-I): the machine principal, its kill switch and
-- the tool envelope. §18 gives the AI Service role "narrow safe tools" with an explicit
-- deny on financial authority, secrets, raw passport and privileged security; §56 fixes
-- the three risk tiers. The tool vocabulary is data and the deny registry is policy
-- (security.ai.denied_actions), so neither lives in the source.
-- --------------------------------------------------------------------------
CREATE TABLE ai_principals (
    principal_id TEXT NOT NULL,
    agency_id    TEXT NOT NULL DEFAULT 'agency_default',
    name         TEXT NOT NULL CHECK (length(name) >= 1),
    secret_ref   TEXT NOT NULL CHECK (length(secret_ref) >= 1),
    created_at   TEXT NOT NULL,
    CONSTRAINT pk_ai_principals PRIMARY KEY (principal_id),
    CONSTRAINT fk_ai_principals_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);

-- Status is derived from the newest event, never stored as a mutable column: an
-- append-only log keeps *when* and *who* for the §40 kill switch.
CREATE TABLE ai_principal_events (
    ai_principal_event_id TEXT NOT NULL,
    principal_id          TEXT NOT NULL,
    seq                   INTEGER NOT NULL CHECK (seq >= 1),
    kind                  TEXT NOT NULL,
    actor                 TEXT NOT NULL CHECK (length(actor) >= 1),
    reason                TEXT,
    occurred_at           TEXT NOT NULL,
    CONSTRAINT pk_ai_principal_events PRIMARY KEY (ai_principal_event_id),
    CONSTRAINT fk_ai_principal_events_principal FOREIGN KEY (principal_id)
        REFERENCES ai_principals (principal_id),
    CONSTRAINT uq_ai_principal_events_seq UNIQUE (principal_id, seq),
    CONSTRAINT ck_ai_principal_events_kind
        CHECK (kind IN ('REGISTERED', 'SUSPENDED', 'REINSTATED', 'ACTIVATED', 'DEGRADED', 'READ_ONLY', 'RETIRED'))
);

-- The §56 capability envelope. Platform-wide on purpose: a per-agency copy would let a
-- tenant believe the AI holds authority it does not have here.
CREATE TABLE ai_tools (
    tool        TEXT NOT NULL,
    tier        TEXT NOT NULL,
    policy_key  TEXT,
    spec_ref    TEXT NOT NULL CHECK (length(spec_ref) >= 1),
    description TEXT NOT NULL CHECK (length(description) >= 1),
    created_at  TEXT NOT NULL,
    CONSTRAINT pk_ai_tools PRIMARY KEY (tool),
    CONSTRAINT fk_ai_tools_policy FOREIGN KEY (policy_key) REFERENCES policy_defs (policy_key),
    CONSTRAINT ck_ai_tools_tier CHECK (tier IN ('LOW', 'MEDIUM', 'HIGH')),
    CONSTRAINT ck_ai_tools_medium_is_policy_controlled
        CHECK (tier <> 'MEDIUM' OR policy_key IS NOT NULL)
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

-- --------------------------------------------------------------------------
-- --------------------------------------------------------------------------
-- Evidence objects — the document attack surface (Master Spec v1.1 §27, §12, §19)
--
-- `documents` (above) holds documents this system *generates*. This section holds
-- files that arrive from outside — supplier contracts, payment proofs, screenshots,
-- bank references — the surface §53(8) names as an attack vector.
--
-- The pipeline is a database invariant, not a convention:
--   UPLOAD → QUARANTINE → TYPE/SIZE VALIDATION → MALWARE SCAN → CLASSIFICATION →
--   SAFE STORAGE → AUTHORIZED ACCESS
-- `ck_evidence_stored_requires_clean_scan` refuses STORED without a named scanner and
-- a CLEAN verdict, so "quarantine was skipped" is not a state any code path can write.
-- Files are never hard-deleted (§39): deletion is a status with an audit event.
-- --------------------------------------------------------------------------
CREATE TABLE evidence_objects (
    evidence_id      TEXT NOT NULL,
    agency_id        TEXT NOT NULL DEFAULT 'agency_default',
    kind             TEXT NOT NULL CHECK (kind IN (
                         'EMAIL','WHATSAPP','PDF','SCREENSHOT','PORTAL_REFERENCE',
                         'PHONE_CONFIRMATION_NOTE','BANK_REFERENCE','OTHER')),
    status           TEXT NOT NULL CHECK (status IN (
                         'QUARANTINED','VALIDATED','SCAN_PENDING','SCANNED','CLASSIFIED',
                         'STORED','REJECTED','SCAN_FAILED','DELETED')),
    classification   TEXT CHECK (classification IS NULL OR classification IN ('L1','L2','L3','L4')),
    access_policy    TEXT NOT NULL CHECK (access_policy IN ('AGENCY_SCOPED','NEED_TO_KNOW','RESTRICTED_VAULT')),
    original_filename TEXT NOT NULL CHECK (length(original_filename) >= 1),
    content_type     TEXT NOT NULL CHECK (length(content_type) >= 3),
    byte_size        INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256           TEXT NOT NULL CHECK (length(sha256) = 64),
    uploaded_by      TEXT NOT NULL CHECK (length(uploaded_by) >= 1),
    quarantine_ref   TEXT NOT NULL CHECK (length(quarantine_ref) >= 1),
    storage_backend  TEXT,
    storage_key      TEXT,
    scan_provider    TEXT,
    scan_verdict     TEXT CHECK (scan_verdict IS NULL OR scan_verdict IN ('CLEAN','INFECTED','UNKNOWN')),
    scanned_at       TEXT,
    rejection_reason TEXT,
    retention_until  TEXT,
    version          INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CONSTRAINT pk_evidence_objects PRIMARY KEY (evidence_id),
    CONSTRAINT fk_evidence_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT ck_evidence_classified CHECK (
        status NOT IN ('CLASSIFIED','STORED') OR classification IS NOT NULL
    ),
    CONSTRAINT ck_evidence_stored_requires_clean_scan CHECK (
        status <> 'STORED'
        OR (storage_key IS NOT NULL AND storage_backend IS NOT NULL
            AND scan_provider IS NOT NULL AND scan_verdict = 'CLEAN' AND scanned_at IS NOT NULL)
    ),
    CONSTRAINT ck_evidence_restricted_vault CHECK (
        classification IS NULL OR classification <> 'L4' OR access_policy = 'RESTRICTED_VAULT'
    ),
    CONSTRAINT ck_evidence_rejection CHECK (
        status <> 'REJECTED' OR (rejection_reason IS NOT NULL AND length(rejection_reason) >= 3)
    )
);
CREATE INDEX ix_evidence_scope ON evidence_objects (agency_id, status, classification);

-- Temporary authorized access (§27: "temporary signed/authorized URLs"; no permanent
-- public links for confidential/restricted files). The raw token is never stored —
-- only its digest — and a grant is always bounded in time and purpose.
CREATE TABLE evidence_access_grants (
    grant_id       TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    evidence_id    TEXT NOT NULL,
    granted_to     TEXT NOT NULL CHECK (length(granted_to) >= 1),
    purpose        TEXT NOT NULL CHECK (length(purpose) >= 3),
    issued_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    single_use     INTEGER NOT NULL DEFAULT 1 CHECK (single_use IN (0,1)),
    used_at        TEXT,
    revoked_at     TEXT,
    revoked_reason TEXT,
    token_sha256   TEXT NOT NULL CHECK (length(token_sha256) = 64),
    CONSTRAINT pk_evidence_access_grants PRIMARY KEY (grant_id),
    CONSTRAINT fk_grant_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_grant_evidence FOREIGN KEY (evidence_id) REFERENCES evidence_objects (evidence_id),
    CONSTRAINT uq_grant_token UNIQUE (token_sha256),
    CONSTRAINT ck_grant_window CHECK (issued_at < expires_at),
    CONSTRAINT ck_grant_single_use CHECK (single_use = 1 OR used_at IS NULL),
    CONSTRAINT ck_grant_revoked CHECK (
        revoked_at IS NULL OR (revoked_reason IS NOT NULL AND length(revoked_reason) >= 3)
    )
);
CREATE INDEX ix_grant_evidence ON evidence_access_grants (agency_id, evidence_id, expires_at);

-- Append-only trail: every step of the pipeline leaves a row nobody can rewrite.
-- `evidence_id` is nullable for exactly one reason: an access attempt against an
-- unknown or forged grant has no valid target, and a security signal that cannot be
-- written is a signal that does not exist. Only ACCESS_DENIED may omit the target.
CREATE TABLE evidence_events (
    event_id     TEXT NOT NULL,
    agency_id    TEXT NOT NULL DEFAULT 'agency_default',
    evidence_id  TEXT,
    kind         TEXT NOT NULL CHECK (kind IN (
                    'UPLOADED','VALIDATED','VALIDATION_REJECTED','SCAN_REQUESTED','SCAN_PASSED',
                    'SCAN_FAILED','CLASSIFIED','STORED','ACCESS_GRANTED','ACCESS_USED',
                    'ACCESS_DENIED','ACCESS_REVOKED','SOFT_DELETED')),
    actor        TEXT NOT NULL CHECK (length(actor) >= 1),
    actor_role   TEXT NOT NULL CHECK (length(actor_role) >= 1),
    detail       TEXT NOT NULL DEFAULT '{}',
    occurred_at  TEXT NOT NULL,
    CONSTRAINT pk_evidence_events PRIMARY KEY (event_id),
    CONSTRAINT fk_evidence_events_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_evidence_events_object FOREIGN KEY (evidence_id) REFERENCES evidence_objects (evidence_id),
    CONSTRAINT ck_evidence_event_target CHECK (
        evidence_id IS NOT NULL OR kind = 'ACCESS_DENIED'
    )
);
CREATE INDEX ix_evidence_events_object ON evidence_events (agency_id, evidence_id, occurred_at);

CREATE TRIGGER trg_evidence_events_no_update
BEFORE UPDATE ON evidence_events
BEGIN
    SELECT RAISE(ABORT, 'evidence_events: append-only history cannot be rewritten (no UPDATE)');
END;

CREATE TRIGGER trg_evidence_events_no_delete
BEFORE DELETE ON evidence_events
BEGIN
    SELECT RAISE(ABORT, 'evidence_events: append-only history cannot be deleted (no DELETE)');
END;

-- Identitas & akses (ADR-0005, diterima 2026-09-26 — MFA faktor: TOTP)
--
-- Prinsip yang membentuk bentuk tabel di bawah:
--   * Database TIDAK menyimpan shared secret TOTP. Yang disimpan hanya RUJUKAN
--     ke penyimpan rahasia (env/vault per identitas), sehingga bocornya tabel
--     auth tidak dengan sendirinya menghasilkan bypass faktor kedua.
--   * Sesi punya dua batas waktu sekaligus: idle dan absolut. Satu batas saja
--     bisa dilanggar oleh penyerang yang cukup sabar, atau oleh sesi yang
--     ditinggal terbuka.
--   * Setiap peristiwa auth dicatat append-only. "Siapa masuk kapan, dengan
--     faktor apa, dan siapa yang ditolak" adalah bukti, bukan log debug.
-- --------------------------------------------------------------------------
CREATE TABLE mfa_factors (
    factor_id      TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    identity       TEXT    NOT NULL CHECK (length(identity) >= 1),
    kind           TEXT    NOT NULL CHECK (kind IN ('TOTP')),
    secret_ref     TEXT    NOT NULL CHECK (length(secret_ref) >= 3),
    digits         INTEGER NOT NULL DEFAULT 6 CHECK (digits IN (6, 8)),
    step_seconds   INTEGER NOT NULL DEFAULT 30 CHECK (step_seconds BETWEEN 15 AND 120),
    algorithm      TEXT    NOT NULL DEFAULT 'sha1' CHECK (algorithm IN ('sha1','sha256','sha512')),
    status         TEXT    NOT NULL CHECK (status IN ('ACTIVE','DISABLED')),
    last_used_step INTEGER,
    confirmed_at   TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    CONSTRAINT pk_mfa_factors PRIMARY KEY (factor_id),
    CONSTRAINT fk_mfa_factors_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_mfa_factor_identity UNIQUE (agency_id, identity, kind),
    CONSTRAINT ck_mfa_factor_confirmed CHECK (status <> 'ACTIVE' OR confirmed_at IS NOT NULL)
);
CREATE INDEX ix_mfa_factors_identity ON mfa_factors (agency_id, identity, status);

CREATE TABLE auth_sessions (
    session_id       TEXT    NOT NULL,
    agency_id        TEXT    NOT NULL DEFAULT 'agency_default',
    identity         TEXT    NOT NULL CHECK (length(identity) >= 1),
    role             TEXT    NOT NULL CHECK (length(role) >= 1),
    token_sha256     TEXT    NOT NULL CHECK (length(token_sha256) = 64),
    issued_at        TEXT    NOT NULL,
    last_seen_at     TEXT    NOT NULL,
    idle_expires_at  TEXT    NOT NULL,
    expires_at       TEXT    NOT NULL,
    mfa_satisfied_at TEXT,
    step_up_until    TEXT,
    revoked_at       TEXT,
    revoked_reason   TEXT,
    CONSTRAINT pk_auth_sessions PRIMARY KEY (session_id),
    CONSTRAINT fk_auth_sessions_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_auth_session_token UNIQUE (token_sha256),
    CONSTRAINT ck_auth_session_window CHECK (issued_at < idle_expires_at AND issued_at < expires_at),
    CONSTRAINT ck_auth_session_revoked CHECK (
        revoked_at IS NULL OR (revoked_reason IS NOT NULL AND length(revoked_reason) >= 3)
    ),
    CONSTRAINT ck_auth_session_stepup CHECK (step_up_until IS NULL OR mfa_satisfied_at IS NOT NULL)
);
CREATE INDEX ix_auth_sessions_identity ON auth_sessions (agency_id, identity, expires_at);

CREATE TABLE auth_events (
    auth_event_id TEXT NOT NULL,
    agency_id     TEXT NOT NULL DEFAULT 'agency_default',
    identity      TEXT NOT NULL CHECK (length(identity) >= 1),
    kind          TEXT NOT NULL CHECK (kind IN (
                      'LOGIN','LOGOUT','SESSION_ROTATED','SESSION_REVOKED','SESSION_REFUSED',
                      'MFA_ENROLLED','MFA_CHALLENGE','MFA_SUCCEEDED','MFA_FAILED','MFA_REPLAY_REFUSED',
                      'STEP_UP_GRANTED','STEP_UP_REFUSED','ACCESS_DENIED')),
    session_id    TEXT,
    actor         TEXT NOT NULL CHECK (length(actor) >= 1),
    detail        TEXT NOT NULL DEFAULT '{}',
    occurred_at   TEXT NOT NULL,
    CONSTRAINT pk_auth_events PRIMARY KEY (auth_event_id),
    CONSTRAINT fk_auth_events_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_auth_events_session FOREIGN KEY (session_id) REFERENCES auth_sessions (session_id)
);
CREATE INDEX ix_auth_events_identity ON auth_events (agency_id, identity, occurred_at);

CREATE TRIGGER trg_auth_events_no_update
BEFORE UPDATE ON auth_events
BEGIN
    SELECT RAISE(ABORT, 'auth_events: riwayat append-only tidak boleh diubah (no UPDATE)');
END;

CREATE TRIGGER trg_auth_events_no_delete
BEFORE DELETE ON auth_events
BEGIN
    SELECT RAISE(ABORT, 'auth_events: riwayat append-only tidak boleh dihapus (no DELETE)');
END;

CREATE TRIGGER trg_policy_defs_no_update
BEFORE UPDATE ON policy_defs
BEGIN
    SELECT RAISE(ABORT, 'policy_defs is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_policy_defs_no_delete
BEFORE DELETE ON policy_defs
BEGIN
    SELECT RAISE(ABORT, 'policy_defs is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_policy_revisions_no_update
BEFORE UPDATE ON policy_revisions
BEGIN
    SELECT RAISE(ABORT, 'policy_revisions is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_policy_revisions_no_delete
BEFORE DELETE ON policy_revisions
BEGIN
    SELECT RAISE(ABORT, 'policy_revisions is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_ai_principals_no_update
BEFORE UPDATE ON ai_principals
BEGIN
    SELECT RAISE(ABORT, 'ai_principals is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_ai_principals_no_delete
BEFORE DELETE ON ai_principals
BEGIN
    SELECT RAISE(ABORT, 'ai_principals is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_ai_principal_events_no_update
BEFORE UPDATE ON ai_principal_events
BEGIN
    SELECT RAISE(ABORT, 'ai_principal_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_ai_principal_events_no_delete
BEFORE DELETE ON ai_principal_events
BEGIN
    SELECT RAISE(ABORT, 'ai_principal_events is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_ai_tools_no_update
BEFORE UPDATE ON ai_tools
BEGIN
    SELECT RAISE(ABORT, 'ai_tools is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_ai_tools_no_delete
BEFORE DELETE ON ai_tools
BEGIN
    SELECT RAISE(ABORT, 'ai_tools is append-only (DELETE rejected)');
END;

-- --------------------------------------------------------------------------- --
-- Outbox (ADR-0007) and the integration inbox.
--
-- Why these tables exist. ADR-0007: "Business state commit must not depend on
-- notification success." A letter that is written after the business row commits can be
-- lost by a crash in the gap; a letter written *with* the row commits with it or not at
-- all. So an event is inserted in the same transaction as the state it describes, and a
-- worker publishes it afterwards — an outage delays delivery, it never rewrites truth.
--
-- The letter is immutable, the envelope is not. Delivery state (status, attempts,
-- next_attempt_at, published_at, last_error) moves as the worker tries; the content —
-- type, aggregate, payload, correlation, agency, idempotency key — is nailed down by a
-- trigger, because a queue whose letters can be edited after the fact is not evidence.
-- Deleting is deliberately *not* forbidden: retention (D9) is an approved act with its
-- own flow, and a trigger here would silently make that rule impossible.
-- --------------------------------------------------------------------------- --

CREATE TABLE outbox_events (
    outbox_event_id  TEXT    NOT NULL,
    agency_id        TEXT    NOT NULL DEFAULT 'agency_default',
    event_type       TEXT    NOT NULL CHECK (length(event_type) >= 3),
    aggregate_type   TEXT    NOT NULL CHECK (length(aggregate_type) >= 3),
    aggregate_id     TEXT    NOT NULL CHECK (length(aggregate_id) >= 1),
    payload          TEXT    NOT NULL,
    payload_sha256   TEXT    NOT NULL CHECK (length(payload_sha256) = 64),
    actor            TEXT    NOT NULL CHECK (length(actor) >= 3),
    correlation_id   TEXT    NOT NULL CHECK (length(correlation_id) >= 3),
    causation_id     TEXT,
    status           TEXT    NOT NULL CHECK (status IN ('PENDING','PUBLISHED','FAILED','DEAD_LETTER')),
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    next_attempt_at  TEXT    NOT NULL,
    published_at     TEXT,
    last_error       TEXT,
    idempotency_key  TEXT    NOT NULL CHECK (length(idempotency_key) >= 8),
    created_at       TEXT    NOT NULL,
    CONSTRAINT pk_outbox_events PRIMARY KEY (outbox_event_id),
    CONSTRAINT fk_outbox_events_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_outbox_events_idempotency UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_outbox_events_published CHECK (status <> 'PUBLISHED' OR published_at IS NOT NULL),
    CONSTRAINT ck_outbox_events_dead CHECK (
        status <> 'DEAD_LETTER' OR (attempts >= max_attempts AND last_error IS NOT NULL)
    ),
    CONSTRAINT ck_outbox_events_self_caused CHECK (
        causation_id IS NULL OR causation_id <> outbox_event_id
    )
);
CREATE INDEX ix_outbox_events_due ON outbox_events (agency_id, status, next_attempt_at);

CREATE TRIGGER trg_outbox_events_content_is_immutable
BEFORE UPDATE ON outbox_events
WHEN OLD.agency_id       <> NEW.agency_id
  OR OLD.event_type      <> NEW.event_type
  OR OLD.aggregate_type   <> NEW.aggregate_type
  OR OLD.aggregate_id    <> NEW.aggregate_id
  OR OLD.payload         <> NEW.payload
  OR OLD.payload_sha256  <> NEW.payload_sha256
  OR OLD.actor           <> NEW.actor
  OR OLD.correlation_id  <> NEW.correlation_id
  OR OLD.idempotency_key <> NEW.idempotency_key
  OR (OLD.causation_id IS NOT NEW.causation_id)
  OR OLD.created_at      <> NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'an outbox event''s content is immutable; only its delivery state may change');
END;

CREATE TABLE outbox_deliveries (
    delivery_id      TEXT    NOT NULL,
    outbox_event_id  TEXT    NOT NULL,
    agency_id        TEXT    NOT NULL DEFAULT 'agency_default',
    attempt_no       INTEGER NOT NULL CHECK (attempt_no >= 1),
    outcome          TEXT    NOT NULL CHECK (outcome IN ('PUBLISHED','FAILED','REQUEUED')),
    error            TEXT,
    actor            TEXT,
    reason           TEXT,
    attempted_at     TEXT    NOT NULL,
    CONSTRAINT pk_outbox_deliveries PRIMARY KEY (delivery_id),
    CONSTRAINT fk_outbox_deliveries_event FOREIGN KEY (outbox_event_id)
        REFERENCES outbox_events (outbox_event_id),
    CONSTRAINT uq_outbox_deliveries_attempt UNIQUE (outbox_event_id, attempt_no),
    CONSTRAINT ck_outbox_deliveries_failed CHECK (outcome <> 'FAILED' OR error IS NOT NULL),
    CONSTRAINT ck_outbox_deliveries_requeued CHECK (
        outcome <> 'REQUEUED' OR (actor IS NOT NULL AND reason IS NOT NULL AND length(reason) >= 3)
    )
);

CREATE TABLE integration_inbox (
    inbox_id       TEXT NOT NULL,
    agency_id      TEXT NOT NULL DEFAULT 'agency_default',
    source         TEXT NOT NULL CHECK (length(source) >= 3),
    external_id    TEXT NOT NULL CHECK (length(external_id) >= 3),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    received_at    TEXT NOT NULL,
    processed_at   TEXT,
    CONSTRAINT pk_integration_inbox PRIMARY KEY (inbox_id),
    CONSTRAINT fk_integration_inbox_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_integration_inbox_source UNIQUE (agency_id, source, external_id)
);

-- --------------------------------------------------------------------------- --
-- Exception fingerprints: one row per distinct failure, so a loop that breaks the same way
-- a thousand times opens **one** incident and counts the rest.
--
-- Why a table and not a log line. Severity decides who is woken up and when (ADR-0039's
-- escalation, incidents.py's 72-hour regulator clock). A failure that arrives 400 times a
-- minute must not produce 400 incidents: the obligation is the *failure*, not the arrival.
-- The window (`dedupe_window_minutes`) and the count are stored, so "how often did this
-- happen, and was it one incident or thirty" is answerable afterwards — which is exactly what
-- a post-mortem asks and what an in-memory counter cannot answer.
--
-- The row is a *pointer*, never a replacement: the incident itself lives in `incidents`,
-- with its playbook and its append-only step history. This table only remembers that the
-- same failure was seen before, when, and how many times.
-- --------------------------------------------------------------------------- --

CREATE TABLE exception_fingerprints (
    fingerprint_id  TEXT    NOT NULL,
    agency_id       TEXT    NOT NULL DEFAULT 'agency_default',
    dedupe_key      TEXT    NOT NULL CHECK (length(dedupe_key) = 64),
    category        TEXT    NOT NULL CHECK (length(category) >= 3),
    source          TEXT    NOT NULL CHECK (length(source) >= 3),
    code            TEXT    NOT NULL CHECK (length(code) >= 3),
    severity        TEXT    NOT NULL CHECK (severity IN ('SEV1','SEV2','SEV3','SEV4')),
    occurrences     INTEGER NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
    incident_id     TEXT,
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    CONSTRAINT pk_exception_fingerprints PRIMARY KEY (fingerprint_id),
    CONSTRAINT fk_exception_fingerprints_agency FOREIGN KEY (agency_id)
        REFERENCES agencies (agency_id),
    CONSTRAINT fk_exception_fingerprints_incident FOREIGN KEY (incident_id)
        REFERENCES incidents (incident_id),
    CONSTRAINT uq_exception_fingerprints_key UNIQUE (agency_id, dedupe_key),
    CONSTRAINT ck_exception_fingerprints_window CHECK (first_seen_at <= last_seen_at),
    CONSTRAINT ck_exception_fingerprints_incident CHECK (incident_id IS NOT NULL)
);
CREATE INDEX ix_exception_fingerprints_open ON exception_fingerprints (agency_id, last_seen_at);

-- --------------------------------------------------------------------------- --
-- Agency settings: the values the **tenant** owns, not the platform (ADR-0037).
--
-- What belongs here: the payment provider a tenant uses, its WhatsApp number, where its
-- PBJT rate comes from, who is on call. What does *not*: anything that bounds the platform —
-- the deny registry, approval thresholds, the MFA role set. Those are owner policy values
-- (`policy_revisions`), and a tenant cannot set them for itself.
--
-- One row per key, holding the value in force. The *history* is the audit chain: every write
-- appends an event with `before`/`after`, so "who changed the WhatsApp number, when, and what
-- was it before" is answered by the chain rather than by a second copy of the data here.
--
-- The catalogue of legal keys is code (`src/settings.py`), not data: an unknown key is refused
-- rather than stored, because a settings table that accepts anything becomes a place where a
-- typo silently does nothing.
-- --------------------------------------------------------------------------- --

CREATE TABLE agency_settings (
    setting_id     TEXT    NOT NULL,
    agency_id      TEXT    NOT NULL DEFAULT 'agency_default',
    setting_key    TEXT    NOT NULL CHECK (length(setting_key) >= 3),
    value          TEXT    NOT NULL CHECK (length(value) >= 1),
    revision       INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_by     TEXT    NOT NULL CHECK (length(updated_by) >= 3),
    updated_role   TEXT    NOT NULL CHECK (length(updated_role) >= 2),
    updated_at     TEXT    NOT NULL,
    CONSTRAINT pk_agency_settings PRIMARY KEY (setting_id),
    CONSTRAINT fk_agency_settings_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT uq_agency_settings_key UNIQUE (agency_id, setting_key)
);
CREATE INDEX ix_agency_settings_agency ON agency_settings (agency_id, setting_key);

-- --------------------------------------------------------------------------- --
-- A tenant's own role set (ADR-0035 amendment: "peran dapat diedit", decision D3).
--
-- What a tenant may change: **who** holds a role, and **what that role is called** in its own
-- language. What a tenant may not change: the platform's authority ladder. Authority comes from
-- `based_on` — a canonical blueprint id in src/roles.py — so renaming "Finance & Accounting
-- Manager" to anything at all cannot move it one rank, and no tenant can create a role that
-- outranks its blueprint or that the owner deleted (the AI maker roles are not assignable).
--
-- Why holders are opaque references and not names: the platform enforces *that* a role has a
-- holder, never *who* the person is. Storing a staff name here would put personal data into a
-- table that gates, dashboards and the audit chain all read, for no enforcement benefit. A tenant
-- names its own people in its own records (`holder_ref` like `mr_ceo`).
-- --------------------------------------------------------------------------- --

CREATE TABLE agency_roles (
    role_id     TEXT    NOT NULL,
    agency_id   TEXT    NOT NULL DEFAULT 'agency_default',
    label       TEXT    NOT NULL CHECK (length(label) >= 3),
    based_on    TEXT    NOT NULL CHECK (length(based_on) >= 2),
    status      TEXT    NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
    revision    INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_by  TEXT    NOT NULL CHECK (length(updated_by) >= 3),
    updated_role TEXT   NOT NULL CHECK (length(updated_role) >= 2),
    updated_at  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    CONSTRAINT pk_agency_roles PRIMARY KEY (agency_id, role_id),
    CONSTRAINT fk_agency_roles_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT ck_agency_roles_id CHECK (role_id GLOB '[a-z]*' AND length(role_id) BETWEEN 2 AND 64)
);
CREATE INDEX ix_agency_roles_blueprint ON agency_roles (agency_id, based_on, status);

CREATE TABLE agency_role_holders (
    holder_id    TEXT    NOT NULL,
    agency_id    TEXT    NOT NULL DEFAULT 'agency_default',
    role_id      TEXT    NOT NULL,
    holder_ref   TEXT    NOT NULL CHECK (length(holder_ref) >= 2),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    note         TEXT,
    updated_by   TEXT    NOT NULL CHECK (length(updated_by) >= 3),
    updated_at   TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    CONSTRAINT pk_agency_role_holders PRIMARY KEY (holder_id),
    CONSTRAINT fk_agency_role_holders_role FOREIGN KEY (agency_id, role_id)
        REFERENCES agency_roles (agency_id, role_id),
    CONSTRAINT uq_agency_role_holders UNIQUE (agency_id, role_id, holder_ref)
);
CREATE INDEX ix_agency_role_holders_ref ON agency_role_holders (agency_id, holder_ref, status);

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

CREATE TRIGGER trg_option_sets_no_update
BEFORE UPDATE ON option_sets
BEGIN
    SELECT RAISE(ABORT, 'option_sets is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_option_sets_no_delete
BEFORE DELETE ON option_sets
BEGIN
    SELECT RAISE(ABORT, 'option_sets is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_option_set_items_no_update
BEFORE UPDATE ON option_set_items
BEGIN
    SELECT RAISE(ABORT, 'option_set_items is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_option_set_items_no_delete
BEFORE DELETE ON option_set_items
BEGIN
    SELECT RAISE(ABORT, 'option_set_items is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_option_set_presentations_no_update
BEFORE UPDATE ON option_set_presentations
BEGIN
    SELECT RAISE(ABORT, 'option_set_presentations is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_option_set_presentations_no_delete
BEFORE DELETE ON option_set_presentations
BEGIN
    SELECT RAISE(ABORT, 'option_set_presentations is append-only (DELETE rejected)');
END;

-- §11 immutability for quotations. A version that was sent or accepted is never
-- overwritten: its status may still move forward (SENT -> ACCEPTED/SUPERSEDED),
-- but the priced content — the quote it points at, the stay it covers, the
-- version number and who made it — is frozen from the moment it is sent. The
-- guarantee lives here so it does not depend on a module remembering to be
-- careful, exactly as the option-set versions above.
CREATE TRIGGER trg_quotation_versions_frozen_content
BEFORE UPDATE ON quotation_versions
WHEN OLD.status IN ('SENT','ACCEPTED')
    AND (
        NEW.quotation_version_id <> OLD.quotation_version_id
        OR NEW.agency_id <> OLD.agency_id
        OR NEW.property_id <> OLD.property_id
        OR NEW.unit_id <> OLD.unit_id
        OR NEW.check_in <> OLD.check_in
        OR NEW.check_out <> OLD.check_out
        OR NEW.version <> OLD.version
        OR NEW.quote_id <> OLD.quote_id
        OR NEW.supersedes_id IS NOT OLD.supersedes_id
        OR NEW.created_by <> OLD.created_by
        OR NEW.created_at <> OLD.created_at
        OR NEW.sent_at IS NOT OLD.sent_at
    )
BEGIN
    SELECT RAISE(ABORT, 'a sent or accepted quotation version is immutable (only status may move)');
END;

CREATE TRIGGER trg_quotation_versions_no_delete
BEFORE DELETE ON quotation_versions
BEGIN
    SELECT RAISE(ABORT, 'quotation_versions is append-only (DELETE rejected)');
END;

CREATE TRIGGER trg_quotation_snapshots_no_update
BEFORE UPDATE ON quotation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'quotation_snapshots is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_quotation_snapshots_no_delete
BEFORE DELETE ON quotation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'quotation_snapshots is append-only (DELETE rejected)');
END;

-- The three snapshots are written when the version is sent. Accepting the version
-- must not be able to add or replace one afterwards: the acceptance has to name the
-- record that was already complete, not assemble one after the guest agreed.
CREATE TRIGGER trg_quotation_snapshots_after_accept
BEFORE INSERT ON quotation_snapshots
WHEN (SELECT status FROM quotation_versions WHERE quotation_version_id = NEW.quotation_version_id) = 'ACCEPTED'
BEGIN
    SELECT RAISE(ABORT, 'an accepted quotation version cannot gain a new snapshot');
END;

CREATE TRIGGER trg_quotation_acceptances_no_update
BEFORE UPDATE ON quotation_acceptances
BEGIN
    SELECT RAISE(ABORT, 'quotation_acceptances is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_quotation_acceptances_no_delete
BEFORE DELETE ON quotation_acceptances
BEGIN
    SELECT RAISE(ABORT, 'quotation_acceptances is append-only (DELETE rejected)');
END;

-- --------------------------------------------------------------------------- --
-- §9/§13 AI Reservation Service (2026-10-02): the conversation ledger and the
-- human-handoff ladder. This is the first §9 component with a caller in the code.
--
-- Two tables, because a conversation holds two different kinds of fact:
--
--   * `ai_conversations` — the conversation's own identity: the tenant, the
--     inquiry it belongs to, the channel it arrived on, and **references** to the
--     records other modules own. It deliberately does not copy requirements
--     (intake), candidates (matching), prices (quotation) or payment state
--     (payments): each of those has exactly one owner, and a copy here would be a
--     second answer to the same question — the failure §15 names. The handoff
--     package resolves them by reference, at the moment it is built.
--   * `ai_conversation_events` — the §13 ladder. Append-only, with the current
--     rung *derived from the newest event* rather than stored as a mutable column,
--     for the same reason `ai_principal_events` works that way: the log is what
--     proves who moved the conversation, when, and why.
--
-- The guest is `guest_ref`: the opaque reference the agency already uses. No name,
-- no phone number, no identity document — §9 forbids raw identity documents in
-- model context, and the cheapest way to keep that promise is never to store one.
--
-- One conversation per (inquiry, channel). Two AI conversations answering the same
-- guest thread is the failure §13's closing paragraph is about, so the schema
-- refuses the second one instead of trusting a caller to check.
-- --------------------------------------------------------------------------- --
CREATE TABLE ai_conversations (
    conversation_id TEXT NOT NULL,
    agency_id       TEXT NOT NULL DEFAULT 'agency_default',
    principal_id    TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    guest_ref       TEXT NOT NULL CHECK (length(guest_ref) >= 3),
    channel         TEXT NOT NULL CHECK (channel IN ('website','email','whatsapp_business','dashboard','phone')),
    consent_state   TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (consent_state IN ('UNKNOWN','GRANTED','WITHDRAWN')),
    quote_ref       TEXT,
    hold_ref        TEXT,
    reservation_ref TEXT,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CONSTRAINT pk_ai_conversations PRIMARY KEY (conversation_id),
    CONSTRAINT fk_ai_conversations_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_ai_conversations_principal FOREIGN KEY (principal_id)
        REFERENCES ai_principals (principal_id),
    CONSTRAINT fk_ai_conversations_case FOREIGN KEY (case_id) REFERENCES cases (case_id),
    CONSTRAINT fk_ai_conversations_quote FOREIGN KEY (quote_ref) REFERENCES quotes (quote_id),
    CONSTRAINT fk_ai_conversations_hold FOREIGN KEY (hold_ref) REFERENCES holds (hold_id),
    CONSTRAINT fk_ai_conversations_booking FOREIGN KEY (reservation_ref) REFERENCES bookings (booking_id),
    CONSTRAINT uq_ai_conversations_thread UNIQUE (agency_id, case_id, channel)
);
CREATE INDEX ix_ai_conversations_case ON ai_conversations (agency_id, case_id);
CREATE INDEX ix_ai_conversations_principal ON ai_conversations (agency_id, principal_id);

-- The §13 ladder. `state` is the rung this event *arrived at*, so the history can
-- be read without replaying it; the rung a conversation stands on is the newest
-- event, never a column. The CHECKs pin the shape of the three failures that
-- matter: an escalation with no named reason is not a handoff, a handoff with no
-- package is a human being handed a stranger, and a rung that names no human is
-- not a human taking over.
CREATE TABLE ai_conversation_events (
    ai_conversation_event_id TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    seq                      INTEGER NOT NULL CHECK (seq >= 1),
    kind                     TEXT    NOT NULL CHECK (kind IN (
                                 'OPENED','ESCALATED','HANDOFF_REQUESTED','HUMAN_TAKEN',
                                 'WRITES_SUSPENDED','HANDLING_STARTED','RESOLVED',
                                 'RESUME_ALLOWED','RESUMED')),
    state                    TEXT    NOT NULL CHECK (state IN (
                                 'AI_RUNNING','ESCALATION_TRIGGERED','HANDOFF_PENDING',
                                 'HUMAN_ASSIGNED','AI_WRITE_SUSPENDED','HUMAN_HANDLING',
                                 'RESOLUTION','AI_RESUME_ALLOWED')),
    trigger_code             TEXT,
    handoff_blueprint        TEXT,
    human_ref                TEXT,
    package_fingerprint      TEXT CHECK (package_fingerprint IS NULL OR length(package_fingerprint) = 64),
    actor                    TEXT    NOT NULL CHECK (length(actor) >= 1),
    actor_role               TEXT    NOT NULL CHECK (length(actor_role) >= 1),
    reason                   TEXT,
    occurred_at              TEXT    NOT NULL,
    CONSTRAINT pk_ai_conversation_events PRIMARY KEY (ai_conversation_event_id),
    CONSTRAINT fk_ai_conversation_events_conversation FOREIGN KEY (conversation_id)
        REFERENCES ai_conversations (conversation_id),
    CONSTRAINT uq_ai_conversation_events_seq UNIQUE (conversation_id, seq),
    CONSTRAINT ck_ai_conversation_events_trigger CHECK (kind <> 'ESCALATED' OR trigger_code IS NOT NULL),
    CONSTRAINT ck_ai_conversation_events_handoff CHECK (
        kind <> 'HANDOFF_REQUESTED' OR (handoff_blueprint IS NOT NULL AND package_fingerprint IS NOT NULL)
    ),
    CONSTRAINT ck_ai_conversation_events_human CHECK (
        kind NOT IN ('HUMAN_TAKEN', 'RESUME_ALLOWED') OR human_ref IS NOT NULL
    )
);

CREATE TRIGGER trg_ai_conversation_events_no_update
BEFORE UPDATE ON ai_conversation_events
BEGIN
    SELECT RAISE(ABORT, 'ai_conversation_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER trg_ai_conversation_events_no_delete
BEFORE DELETE ON ai_conversation_events
BEGIN
    SELECT RAISE(ABORT, 'ai_conversation_events is append-only (DELETE rejected)');
END;

-- §16 governance policy catalogue (WAVE 1-H). The vocabulary is data: every
-- key the platform resolves must be declared here first, with the role that
-- owns it and the clause it comes from.
-- --------------------------------------------------------------------------
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.discount.authority_roles', 'COMMERCIAL', 'ROLE_SET', 'MEDIUM', 'owner', '§16', 'Roles allowed to grant a discount without owner approval.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.margin_floor_pct', 'COMMERCIAL', 'PCT', 'MEDIUM', 'owner', '§16', 'Lowest gross margin a booking may be sold at.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.margin_floor_minor', 'COMMERCIAL', 'COUNT', 'MEDIUM', 'owner', '§11', 'Absolute margin floor in minor units, the second half of the §11 guard: a discount may not leave less than this behind, whatever the percentage says.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.margin_floor_currency', 'COMMERCIAL', 'TEXT', 'MEDIUM', 'owner', '§11', 'ISO-4217 currency commercial.margin_floor_minor is denominated in. A booking in another currency has the floor converted through commercial.fx.rates before the §11 guard is applied.', '2026-09-27T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.fx.rates', 'COMMERCIAL', 'JSON', 'MEDIUM', 'owner', '§11', 'Published FX rates as {"BASE/QUOTE": "rate"}: one unit of BASE is worth this many QUOTE. Used to express the absolute margin floor in the currency of the booking. An unpublished pair refuses — the engine never assumes parity.', '2026-09-27T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('commercial.fx.max_age_hours', 'COMMERCIAL', 'HOURS', 'MEDIUM', 'owner', '§11', 'How old the effective revision of commercial.fx.rates may be before a conversion refuses. The rate is a day''s price, so the limit is applied in whole days and the tighter day wins (48 h refuses from the third day). An unpublished limit refuses every conversion.', '2026-09-28T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.approval.payment_thresholds_minor', 'FINANCE', 'JSON', 'HIGH', 'owner', '§16', 'Payment amounts in minor units that require approval at each level.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.approval.dual_control_min_approvals', 'FINANCE', 'COUNT', 'HIGH', 'owner', '§16', 'Approvals required for a subject on the dual-control list.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.approval.max_required_approvals', 'FINANCE', 'COUNT', 'HIGH', 'owner', '§16', 'Upper bound of required approvals the engine will accept.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.approval.expiry_minutes_by_risk', 'FINANCE', 'JSON', 'MEDIUM', 'owner', '§16', 'How long an approval request stays pending, per risk level.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.approval.dual_control_subjects', 'FINANCE', 'JSON', 'HIGH', 'owner', '§16', 'Subject types that always need two approvals.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('finance.booking.financial_gates', 'FINANCE', 'JSON', 'HIGH', 'owner', '§16', 'Financial conditions a booking must satisfy before confirmation.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('availability.hold.reminder_minutes', 'AVAILABILITY', 'JSON', 'LOW', 'owner', '§16', 'When to remind the team before a hold expires.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('availability.reconfirmation.freshness_hours', 'AVAILABILITY', 'HOURS', 'MEDIUM', 'owner', '§16', 'Age at which availability must be reconfirmed with the supplier.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('availability.evidence.freshness_seconds_by_level', 'AVAILABILITY', 'JSON', 'MEDIUM', 'owner', '§16', 'Minimum freshness of evidence for each evidence level.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('availability.evidence.max_freshness_seconds_by_level', 'AVAILABILITY', 'JSON', 'MEDIUM', 'owner', '§16', 'Maximum freshness an evidence level may claim.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('operations.incident.escalation_minutes_by_severity', 'OPERATIONS', 'JSON', 'HIGH', 'owner', '§16', 'Escalation deadline for each incident severity.', '2026-09-26T00:00:00+00:00');

-- §30's closure gate: the required-documents list is tenant data declared through one
-- policy key (ADR-0054). Declared is not granted: with no published revision the
-- documents check fails closed, exactly like every other unowned §30 fact.
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('operations.booking.required_document_kinds', 'OPERATIONS', 'JSON', 'MEDIUM', 'owner', '§30/ADR-0054', 'Document kinds (documents.DOCUMENT_KINDS vocabulary) a booking must have GENERATED before the §30 gate closes it; an empty list means the tenant requires none; unpublished stays fail-closed.', '2026-10-03T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('retention.records.retention_days_by_class', 'RETENTION', 'JSON', 'HIGH', 'owner', '§49', 'Retention period per data class. §49 leaves the periods OPEN.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.ai.denied_actions', 'SECURITY', 'JSON', 'HIGH', 'owner', '§18/§56/ADR-0009', 'Capabilities the AI service may never hold, checked before any allowlist.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.mfa.required_roles', 'SECURITY', 'ROLE_SET', 'HIGH', 'owner', '§18', 'Roles that must satisfy MFA before acting.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.session.absolute_timeout_hours', 'SECURITY', 'HOURS', 'HIGH', 'owner', '§18', 'Absolute session lifetime regardless of activity.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.session.idle_timeout_minutes', 'SECURITY', 'MINUTES', 'HIGH', 'owner', '§18', 'Idle time before a session is treated as expired.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.step_up.high_risk_subjects', 'SECURITY', 'JSON', 'HIGH', 'owner', '§18', 'Subject types that require a fresh step-up.', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.step_up.window_minutes', 'SECURITY', 'MINUTES', 'HIGH', 'owner', '§16/§18', 'How long a step-up unlock lasts before it must be repeated.', '2026-09-26T00:00:00+00:00');
-- §9 "AI uncertainty above configured threshold". The threshold is published, not hard-coded,
-- and it ships UNPUBLISHED on purpose: until the owner writes the number down, the AI
-- Reservation Service must not act on a judgement that came from language at all, because
-- "the model sounded sure" is not a value anybody decided. The platform-default bar lives in
-- docs/GO_LIVE_DECISIONS.md as an open owner decision, not in this file.
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.ai.escalation_min_confidence', 'SECURITY', 'PCT', 'HIGH', 'owner', '§9/§17', 'The lowest reported confidence at which the AI Reservation Service may act on a judgement that came from language rather than from a deterministic rule; below it, and until it is published, the conversation goes to a human.', '2026-10-02T00:00:00+00:00');

-- Baseline revisions: only values a document already fixes. The rest stay
-- unpublished on purpose — §49 retention and the commercial authority numbers
-- have no agreed value yet, and inventing one would be a fabricated policy.
-- --------------------------------------------------------------------------
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_01', 'agency_default', 'finance.approval.dual_control_min_approvals', 1, '2', '2026-01-01', '9999-12-31', 'src/approvals.py DUAL_CONTROL_MIN_APPROVALS migrated to policy (WAVE 1-H)', 'owner_anas', '6772a47637184b2a8de5a5fdf7fea657ab83e6729bdc29b5bc33df3cd43e48ec', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_02', 'agency_default', 'finance.approval.max_required_approvals', 1, '3', '2026-01-01', '9999-12-31', 'src/approvals.py MAX_REQUIRED_APPROVALS migrated to policy (WAVE 1-H)', 'owner_anas', 'fe7cf878381df9190702c7fd8be8cec62a7e47b761872bbd62773057d8c5d0e7', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_03', 'agency_default', 'finance.approval.expiry_minutes_by_risk', 1, '{"HIGH":240,"LOW":1440,"MEDIUM":720}', '2026-01-01', '9999-12-31', 'src/approvals.py EXPIRY_MINUTES migrated to policy (WAVE 1-H)', 'owner_anas', '8d27b13dbdc17469a1aa76847face0b6225afb97bb5ccd6714fd7068ccea7783', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_04', 'agency_default', 'finance.approval.dual_control_subjects', 1, '["refund","payout","bank_change"]', '2026-01-01', '9999-12-31', 'src/approvals.py DUAL_CONTROL_SUBJECTS migrated to policy (WAVE 1-H)', 'owner_anas', 'e4b1a6c769866b6beb904cd26a410ff5e0ed5686e26ccb5811f18907661c2e4b', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_05', 'agency_default', 'availability.evidence.freshness_seconds_by_level', 1, '{"A":900,"B":86400,"C":172800,"D":0}', '2026-01-01', '9999-12-31', 'src/gates.py LEVEL_FRESHNESS_SECONDS migrated to policy (WAVE 1-H)', 'owner_anas', 'db7e0609993baa75db8de888df022f0c55019513dad9c4bba57c2248a939bc1f', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_06', 'agency_default', 'availability.evidence.max_freshness_seconds_by_level', 1, '{"A":3600,"B":172800,"C":604800,"D":0}', '2026-01-01', '9999-12-31', 'src/gates.py LEVEL_MAX_FRESHNESS_SECONDS migrated to policy (WAVE 1-H)', 'owner_anas', 'aacfdb9b67ac8203465a80703fde1d92f3cf9712cca934736cbf31138fb464c9', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_07', 'agency_default', 'security.ai.denied_actions', 1, '["payment.approve","payment.execute","refund.approve","refund.execute","bank.change","bank.approve","discount.approve","margin_override.approve","contract.activate","rate.verify","rate.activate","permission.change","audit.modify","audit.delete","secret.read","passport.read_raw","blockchain.sign","crypto.transfer","withdrawal.approve","treasury.control","key.read_raw","seed_phrase.read"]', '2026-01-01', '9999-12-31', 'ADR-0009 deny registry (base list + 2026-09-26 amendment)', 'owner_anas', 'fde960e1f943507e4b9d9479aa06980c20ff17f290881ea9045f06719f5a4474', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_13', 'agency_default', 'security.ai.denied_actions', 2, '["payment.approve","payment.execute","refund.approve","refund.execute","bank.change","bank.approve","discount.approve","discount.approve_out_of_policy","margin_override.approve","contract.activate","rate.verify","rate.activate","permission.change","audit.modify","audit.delete","secret.read","passport.read_raw","blockchain.sign","crypto.transfer","withdrawal.approve","treasury.control","key.read_raw","seed_phrase.read"]', '2026-09-26', '9999-12-31', 'ADR-0009 deny registry (§21 amendment 2026-09-26: the registry must name the tool that exists)', 'owner_anas', '3d954399d919bdb7e8136d1024a385db806d17acbf739e79c9a76fd4a423778a', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_08', 'agency_default', 'security.mfa.required_roles', 1, '["owner","finance"]', '2026-01-01', '9999-12-31', 'src/identity.py MFA_REQUIRED_ROLES migrated to policy (WAVE 1-H)', 'owner_anas', 'ab8dcfcd6cd4fb1009aad9343c88721787e34422a266c0e54005da6b621551a3', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_09', 'agency_default', 'security.session.absolute_timeout_hours', 1, '12', '2026-01-01', '9999-12-31', 'ADR-0005 (identity, auth, session and RBAC)', 'owner_anas', '7f56d92c8d33ce6ba2bb92cab4a0619e7c8fa0c4c88b2802bcf645fedfb4817c', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_10', 'agency_default', 'security.session.idle_timeout_minutes', 1, '30', '2026-01-01', '9999-12-31', 'ADR-0005 (identity, auth, session and RBAC)', 'owner_anas', 'a3a743be40cd4c3f03bfd1e7714b82b38a65bf2529a13094072e5a21f4cbcd2b', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_11', 'agency_default', 'security.step_up.high_risk_subjects', 1, '["refund","payout","bank_change"]', '2026-01-01', '9999-12-31', 'src/identity.py HIGH_RISK_SUBJECT_TYPES migrated to policy (WAVE 1-H)', 'owner_anas', '6e15f3f9ca451993b55784b4211f732d8f5d866e787c780c88aea20c1abb12d9', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_12', 'agency_default', 'security.step_up.window_minutes', 1, '10', '2026-01-01', '9999-12-31', 'ADR-0005 (identity, auth, session and RBAC)', 'owner_anas', '87206f8ceb50e944f64122ec100de474d0fbd728ca42e51d3e4a022d027302f1', '2026-09-26T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_14', 'agency_default', 'security.mfa.required_roles', 2, '["owner","finance","gm"]', '2026-10-02', '9999-12-31', 'Keputusan owner 2026-10-02 - GM/CEO menyetujui uang keluar (ADR-0036 tier 2), maka GM wajib faktor kedua sebelum membuka sesi', 'owner_anas', '90e498edd0927913ec5b0b325759b96cc8dc7f0c59295a91a95b19923fa3398d', '2026-10-02T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_15', 'agency_default', 'security.mfa.required_roles', 3, '["owner","finance_manager","gm"]', '2026-10-02', '9999-12-31', 'Keputusan owner 2026-10-02 - pakai nama kanonik Finance Manager (bukan kata halaman ''finance'')', 'owner_anas', 'b308cfd0ad0d0bae60608c1ba8ca0c2414660e7dc212da10b274534ed2be4bdd', '2026-10-02T00:00:00+00:00');
INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at) VALUES ('polrev_baseline_16', 'agency_default', 'security.ai.denied_actions', 3, '["payment.approve","payment.execute","refund.approve","refund.execute","bank.change","bank.approve","discount.approve","discount.approve_out_of_policy","margin_override.approve","contract.activate","rate.verify","rate.activate","permission.change","audit.modify","audit.delete","secret.read","passport.read_raw","blockchain.sign","crypto.transfer","withdrawal.approve","treasury.control","key.read_raw","seed_phrase.read","policy.publish","security.controls.change","ai.allowance.change","deploy.production"]', '2026-10-02', '9999-12-31', 'Keputusan owner 2026-10-02 - penjaga keamanan (ADR-0039) tidak boleh mematikan pengamannya sendiri; prinsip ''Keamanan & Privacy pertama''', 'owner_anas', '52748dc1d7be043ddc79cff1e3c7e0d4184b2c391a45f66aa750fd4e4c0269e2', '2026-10-02T00:00:00+00:00');

-- §56 capability envelope: low-risk autonomous, medium-risk through a policy-controlled
-- tool, high-risk as human-authority-only. Registered is not granted: the deny
-- registry (security.ai.denied_actions) is checked first.
-- --------------------------------------------------------------------------
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at) VALUES ('security.ai.medium_tool_allowance', 'SECURITY', 'JSON', 'HIGH', 'owner', '§56/ADR-0009', 'Medium-risk tools the owner has explicitly allowed the AI service to hold; a tool absent from this list is not policy-controlled and is refused.', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('inquiry.intake', 'LOW', NULL, '§56', 'Inquiry intake', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('inquiry.clarify', 'LOW', NULL, '§56', 'Clarification questions', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('villa.search', 'LOW', NULL, '§56', 'Deterministic villa search', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('villa.answer_verified', 'LOW', NULL, '§56', 'Verified villa questions and answers', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('villa.suggest_alternatives', 'LOW', NULL, '§56', 'Suitable alternatives', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('followup.routine', 'LOW', NULL, '§56', 'Routine follow-up', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('booking.status', 'LOW', NULL, '§56', 'Booking-status information', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('arrival.inform_verified', 'LOW', NULL, '§56', 'Verified pre-arrival information', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('availability.request', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Availability requests', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('hold.request_tentative', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Tentative-hold requests', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('quotation.send', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Quotation sending', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('request.coordinate_special', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Special-request coordination', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('comm.client_selected', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Selected client communications', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('comm.supplier_selected', 'MEDIUM', 'security.ai.medium_tool_allowance', '§56', 'Selected supplier communications', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('payment.approve', 'HIGH', NULL, '§56', 'Financial approval', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('payment.execute', 'HIGH', NULL, '§56', 'Financial execution', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('bank.change', 'HIGH', NULL, '§56', 'Bank changes', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('refund.approve', 'HIGH', NULL, '§56', 'Refund approval', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('refund.execute', 'HIGH', NULL, '§56', 'Refund execution', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('discount.approve_out_of_policy', 'HIGH', NULL, '§56', 'Out-of-policy discounts', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('margin_override.approve', 'HIGH', NULL, '§56', 'Commercial overrides', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('contract.activate', 'HIGH', NULL, '§56', 'Contract activation', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('rate.verify', 'HIGH', NULL, '§56', 'Rate verification', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('rate.activate', 'HIGH', NULL, '§56', 'Rate activation', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('incident.compensate_serious', 'HIGH', NULL, '§56', 'Serious incident compensation', '2026-09-26T00:00:00+00:00');
INSERT INTO ai_tools (tool, tier, policy_key, spec_ref, description, created_at) VALUES ('legal.dispute', 'HIGH', NULL, '§56', 'Legal disputes', '2026-09-26T00:00:00+00:00');

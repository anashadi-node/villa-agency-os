-- Migration 006: the transactional outbox and the integration inbox (ADR-0007).
--
-- 001_initial.sql creates these tables when the database file is created, so this file
-- exists for databases that already exist: schema_loader.apply_schema refuses to re-apply
-- the initial schema ("write a migration instead of re-applying the initial schema").
-- Every statement is idempotent (IF NOT EXISTS), so re-running changes nothing.
--
-- Context: ADR-0007 (accepted 2026-09-28, paper-only until now) is what makes an agent
-- report survive a crash — the event row commits with the state it describes. §14 is why
-- the event is *typed* rather than free text. The table's content is immutable by trigger;
-- only delivery state (status/attempts/next_attempt_at/published_at/last_error) moves.

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

CREATE TABLE IF NOT EXISTS outbox_events (
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
CREATE INDEX IF NOT EXISTS ix_outbox_events_due ON outbox_events (agency_id, status, next_attempt_at);

CREATE TRIGGER IF NOT EXISTS trg_outbox_events_content_is_immutable
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

CREATE TABLE IF NOT EXISTS outbox_deliveries (
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

CREATE TABLE IF NOT EXISTS integration_inbox (
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

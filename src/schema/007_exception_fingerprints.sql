-- Migration 007: exception fingerprints (the exception engine, 2026-10-02).
--
-- 001_initial.sql creates this table for a new database; this file exists for databases that
-- already exist, because schema_loader.apply_schema refuses to re-apply the initial schema.
-- The statement is idempotent.
--
-- Context: a failure that repeats must be counted, not re-opened. `src/exception_engine.py`
-- routes a classified failure to an incident (incidents.py playbooks, SEV1..SEV4) and records
-- the fingerprint here, so the second arrival inside the window increments `occurrences`
-- instead of waking the GM again.

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

CREATE TABLE IF NOT EXISTS exception_fingerprints (
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
CREATE INDEX IF NOT EXISTS ix_exception_fingerprints_open ON exception_fingerprints (agency_id, last_seen_at);

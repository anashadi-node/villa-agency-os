-- --------------------------------------------------------------------------- --
-- Migration 011: the §17 kill-switch ladder, five statuses instead of three.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt with the wider vocabulary and
-- the rows are copied across unchanged. The append-only triggers are recreated because dropping a
-- table drops them, and an append-only log without its guards is not append-only.
--
-- Why widen the *kind* vocabulary rather than add a second "mode" table: §17's five statuses are
-- the principal's status, and `AiService._status()` already derives that from the newest event.
-- A second log would give one question two answers.
-- --------------------------------------------------------------------------- --
CREATE TABLE ai_principal_events_new (
    ai_principal_event_id TEXT NOT NULL,
    principal_id          TEXT NOT NULL,
    seq                   INTEGER NOT NULL CHECK (seq >= 1),
    kind                  TEXT NOT NULL,
    actor                 TEXT NOT NULL CHECK (length(actor) >= 1),
    reason                TEXT,
    occurred_at           TEXT NOT NULL,
    CONSTRAINT pk_ai_principal_events_new PRIMARY KEY (ai_principal_event_id),
    CONSTRAINT fk_ai_principal_events_principal_new FOREIGN KEY (principal_id)
        REFERENCES ai_principals (principal_id),
    CONSTRAINT uq_ai_principal_events_seq_new UNIQUE (principal_id, seq),
    CONSTRAINT ck_ai_principal_events_kind_new
        CHECK (kind IN ('REGISTERED', 'SUSPENDED', 'REINSTATED', 'ACTIVATED', 'DEGRADED', 'READ_ONLY', 'RETIRED'))
);

INSERT INTO ai_principal_events_new
    SELECT * FROM ai_principal_events;

DROP TABLE ai_principal_events;

ALTER TABLE ai_principal_events_new RENAME TO ai_principal_events;

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

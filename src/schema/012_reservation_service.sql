-- Migration 012: the §9/§13 AI Reservation Service — conversation ledger, handoff ladder,
-- and the §9 escalation-confidence key.
--
-- 001_initial.sql seeds the catalogue when the database file is *created*, so these tables
-- and this key are also added there. A database built before today cannot gain them by being
-- reopened: schema_loader.apply_schema refuses to re-apply the initial schema ("write a
-- migration instead of re-applying the initial schema"). Every statement here is idempotent —
-- running twice changes nothing.
--
-- Why a conversation is a stored row rather than worker memory. §5 of the AI Workforce
-- Architecture is explicit: a worker crash must not lose the reservation workflow, and §9's
-- "Conversation Isolation Minimum" names the context that may never be shared between
-- customers or tenants. A row in this table is that isolation boundary; a dictionary inside a
-- worker would be neither durable nor isolated from the next conversation the worker picks up.
--
-- Why the ladder is a log. The rung a conversation stands on is the newest event, never a
-- mutable column, because the question an incident review asks is not "what rung is it on" but
-- "who moved it, when, and why". That is the same reasoning `ai_principal_events` carries for
-- the §17 kill switch, and the same append-only triggers guard it.
--
-- The guest is `guest_ref` — an opaque reference. No name, no phone number, no identity
-- document reaches this table: §9 forbids raw identity documents in model context, and a
-- column that does not exist cannot leak into one.

CREATE TABLE IF NOT EXISTS ai_conversations (
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
CREATE INDEX IF NOT EXISTS ix_ai_conversations_case ON ai_conversations (agency_id, case_id);
CREATE INDEX IF NOT EXISTS ix_ai_conversations_principal ON ai_conversations (agency_id, principal_id);

CREATE TABLE IF NOT EXISTS ai_conversation_events (
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

CREATE TRIGGER IF NOT EXISTS trg_ai_conversation_events_no_update
BEFORE UPDATE ON ai_conversation_events
BEGIN
    SELECT RAISE(ABORT, 'ai_conversation_events is append-only (UPDATE rejected)');
END;

CREATE TRIGGER IF NOT EXISTS trg_ai_conversation_events_no_delete
BEFORE DELETE ON ai_conversation_events
BEGIN
    SELECT RAISE(ABORT, 'ai_conversation_events is append-only (DELETE rejected)');
END;

-- The §9 escalation-confidence key: declared here, deliberately left **unpublished**. A
-- migration declares vocabulary; it never decides a number (§4's rule, restated in migration
-- 004). Until the owner publishes a value the service refuses to act on any judgement that
-- came from language, which is the deny-first posture the rest of the AI layer already has.
INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at)
SELECT 'security.ai.escalation_min_confidence', 'SECURITY', 'PCT', 'HIGH', 'owner', '§9/§17', 'The lowest reported confidence at which the AI Reservation Service may act on a judgement that came from language rather than from a deterministic rule; below it, and until it is published, the conversation goes to a human.', '2026-10-02T00:00:00+00:00'
WHERE NOT EXISTS (SELECT 1 FROM policy_defs WHERE policy_key = 'security.ai.escalation_min_confidence');

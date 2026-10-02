-- Migration 008: agency settings, the values a tenant owns (ADR-0037, decision D17).
--
-- 001_initial.sql creates the table for a new database; this file exists for databases that
-- already exist, because schema_loader.apply_schema refuses to re-apply the initial schema.
-- The statements are idempotent.
--
-- Why a tenant table and not more policy keys: policy values are the owner's (they bound the
-- platform); a payment provider or a WhatsApp number is the *tenant's*, differs per agency, and
-- must never become a platform-wide default. `src/settings.py` holds the closed catalogue of
-- keys, and `readiness()` answers the question D17 asks: may *this* agency go live yet?
--
-- Corrected in place on 2026-10-02, minutes after it was first applied and before any
-- deployment: `updated_role` required 3+ characters, and the role id `gm` is two. The suite
-- caught it on the first GM write. A migration is edited in place only while it has run on
-- nobody's database but this one; once it is out, a wrong migration gets a new one beside it.

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

CREATE TABLE IF NOT EXISTS agency_settings (
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

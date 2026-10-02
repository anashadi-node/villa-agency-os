-- Migration 010: a tenant's own role set and its holders (decision D3, ADR-0035 amendment).
--
-- 001_initial.sql creates the tables for a new database; this file exists for databases that
-- already exist, because schema_loader.apply_schema refuses to re-apply the initial schema.
-- The statements are idempotent.
--
-- Authority comes from `based_on` (a canonical blueprint id in src/roles.py), never from the
-- label, so a tenant may rename a role in its own language without moving one rank.
-- `src/agency_roles.py` holds the closed rules: which blueprints are assignable, and why holder
-- references are opaque.
--
-- Corrected in place on 2026-10-02, minutes after it was first applied and before any deployment:
-- the length CHECKs required 3 characters, and the role vocabulary's shortest id is `gm` (two).
-- The suite caught it on the first seed. A migration is edited in place only while it has run on
-- nobody's database but this one; once it is out, a wrong migration gets a new one beside it.

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

CREATE TABLE IF NOT EXISTS agency_roles (
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
CREATE INDEX IF NOT EXISTS ix_agency_roles_blueprint ON agency_roles (agency_id, based_on, status);

CREATE TABLE IF NOT EXISTS agency_role_holders (
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
CREATE INDEX IF NOT EXISTS ix_agency_role_holders_ref ON agency_role_holders (agency_id, holder_ref, status);

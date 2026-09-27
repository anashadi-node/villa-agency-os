-- Migration 003: catalogue and deny-registry rows added after 001_initial was first applied.
--
-- 001_initial.sql seeds the catalogue and the baseline revisions when the database file is
-- created. A database created before these rows existed cannot gain them by being reopened:
-- schema_loader.apply_schema refuses to re-apply the initial schema ("write a migration
-- instead of re-applying the initial schema"). So each row has to be inserted here, once,
-- for every database that already exists. Both statements are idempotent — re-running them
-- changes nothing.
--
-- 1. commercial.margin_floor_minor — the absolute half of the §11 discount guard. A database
--    without it cannot express "a discount may not leave less than this behind, whatever the
--    percentage says", and the discount engine refuses every request with POLICY_UNKNOWN_KEY.
-- 2. security.ai.denied_actions version 2 — the §21 amendment: the deny registry must name
--    the tool that actually exists (discount.approve_out_of_policy), not a name no tool
--    carries. A database still on version 1 leaves that capability un-denied.
--    The fingerprint is copied verbatim from 001_initial.sql; the audit chain checks it.

INSERT INTO policy_defs (policy_key, domain, value_type, risk_level, owner_role, spec_ref, description, created_at)
SELECT 'commercial.margin_floor_minor', 'COMMERCIAL', 'COUNT', 'MEDIUM', 'owner', '§11', 'Absolute margin floor in minor units, the second half of the §11 guard: a discount may not leave less than this behind, whatever the percentage says.', '2026-09-26T00:00:00+00:00'
WHERE NOT EXISTS (SELECT 1 FROM policy_defs WHERE policy_key = 'commercial.margin_floor_minor');

INSERT INTO policy_revisions (policy_revision_id, agency_id, policy_key, version, value, effective_from, effective_to, source_doc_ref, approved_by, fingerprint, created_at)
SELECT 'polrev_baseline_13', 'agency_default', 'security.ai.denied_actions', 2, '["payment.approve","payment.execute","refund.approve","refund.execute","bank.change","bank.approve","discount.approve","discount.approve_out_of_policy","margin_override.approve","contract.activate","rate.verify","rate.activate","permission.change","audit.modify","audit.delete","secret.read","passport.read_raw","blockchain.sign","crypto.transfer","withdrawal.approve","treasury.control","key.read_raw","seed_phrase.read"]', '2026-09-26', '9999-12-31', 'ADR-0009 deny registry (§21 amendment 2026-09-26: the registry must name the tool that exists)', 'owner_anas', '3d954399d919bdb7e8136d1024a385db806d17acbf739e79c9a76fd4a423778a', '2026-09-26T00:00:00+00:00'
WHERE NOT EXISTS (
    SELECT 1 FROM policy_revisions
    WHERE agency_id = 'agency_default' AND policy_key = 'security.ai.denied_actions' AND version = 2
);

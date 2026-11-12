-- Migration 017: properties.latitude / longitude / geo_precision / geo_source (ADR-0061).
--
-- 001_initial.sql creates these columns for a new database; this file exists for databases
-- that already exist, because schema_loader.apply_schema refuses to re-apply the initial
-- schema.
--
-- Why four columns and not one JSON blob: the coordinate is the thing a map needs, the
-- precision is the thing that decides whether the map may show it at all, and the source is
-- what an auditor asks for when a guest says "your site put the pin at my neighbour's gate".
-- Keeping them apart means the public renderer reads `geo_precision` and can refuse a pin
-- whose provenance nobody recorded (see src/geo.py, `resolve_point`).
--
-- Defaults are safe by construction: every existing property becomes UNKNOWN with no
-- coordinate, so nothing that was not surveyed can appear on a map. A human (or
-- `scripts/import_villas.py`, with region-only data) raises it to AREA_APPROX; only a
-- written owner confirmation or a walked survey raises it to VERIFIED.
--
-- The CHECK constraints are self-contained on purpose: SQLite cannot add a table-level
-- constraint with ALTER TABLE, so the "VERIFIED implies coordinates" rule is enforced in
-- `villa_import.validate_geo_fields` and pinned by tests/test_geo.py instead.

ALTER TABLE properties ADD COLUMN latitude REAL
    CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90));

ALTER TABLE properties ADD COLUMN longitude REAL
    CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180));

ALTER TABLE properties ADD COLUMN geo_precision TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK (geo_precision IN ('UNKNOWN','AREA_APPROX','VERIFIED'));

ALTER TABLE properties ADD COLUMN geo_source TEXT
    CHECK (geo_source IS NULL OR length(geo_source) BETWEEN 2 AND 200);

# PostgreSQL migration 021 — historical upgrade runbook (v1 → v2)

**Status: `OPERATOR_REQUIRED — NOT_AUTOMATED`.**
This document describes a *manual, supervised* upgrade of an already-installed 021 from the
unit-scoped lock generation (v1) to the approved night-scoped generation (v2). Nothing in
`src/pg_migrations.py` performs this upgrade, and this runbook must not be read as an
instruction to make it do so.

Everything below was measured on disposable PostgreSQL 16 databases during PACK-004A and
PACK-004B. No production database was used, and no production readiness is implied.

## 1. Why an upgrade is not automatic

The ledger (`pg_schema_migrations`) stores the SHA-256 of the SQL source each migration row was
applied from. A database that ran 021 as v1 carries a fingerprint that no longer matches the
current file, so the runner classifies it `CONFLICTED` and refuses. That refusal is correct:
a checksum mismatch means *the source changed after it ran*, and silently replaying new DDL over
a live guard would be a schema change with no record and no rehearsal.

Therefore the only supported route is: detect, verify, plan a window, apply the v2 DDL under
supervision, then reconcile the ledger with evidence. Overwriting the stored checksum to obtain
a `READY` status is forbidden — it destroys the only proof of what was actually applied.

## 2. Fingerprints (complete values, from real evidence)

| Generation | SHA-256 of `src/schema/021_pg_cross_table_occupancy.pg.sql` |
|---|---|
| v1 — unit-scoped lock | `3f946cb895c87ffc7206a73de30fd4ed24bfbd46ffbf154f31766e9e0c4ef7b8` |
| v2 — night-scoped lock (approved) | `ec9af60372d956216594d5605d151fd65a12bcf232a00a40fed73fb753550b62` |

The same two values appear in the ledger's `source_sha256` column for the `021` row, which is
what makes the comparison meaningful. Historical source for v1 is commit `9b5d943`'s parent; v2
is `9b5d943`..`a134d5a0` on `feat/villa-fullstack-integration`.

## 3. Distinguishing characteristics

| Signal | v1 | v2 |
|---|---|---|
| `to_regprocedure('villa_lock_occupied_nights(text,text,text)')` | `NULL` — the helper does not exist | present |
| Lock key inside the installed guard body: `position('villa-occupancy-unit:' IN pg_get_functiondef('villa_hold_occupancy_guard()'::regprocedure)) > 0` | `true` | `false` |
| `position('villa-occupancy-night:' IN pg_get_functiondef('villa_lock_occupied_nights(text,text,text)'::regprocedure)) > 0` | n/a (no helper) | `true` |
| Advisory lock identity | one per `(unit_id)` | one per `(unit_id, night)` |
| Consequence | a back-to-back booking queues behind an unrelated stay on the same unit | back-to-back windows share no night and proceed |
| `SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%cross_table_occupancy%'` | 2 | 2 |

Both generations install the same two triggers; the difference lives in the helper and in the
body of the guard, so a trigger count alone cannot tell them apart.

## 4. Preflight — run and record before touching anything

```sql
-- (a) ledger state and fingerprint
SELECT migration_id, left(source_sha256, 16) AS sha16, state, applied_at
  FROM pg_schema_migrations WHERE migration_id LIKE '021%';

-- (b) generation detection (see §3 for the full queries)

-- (c) the invariant the guard exists for: must return 0 before and after
SELECT count(*) FROM holds h JOIN bookings b
  ON b.unit_id = h.unit_id
 AND daterange(villa_iso_date(h.check_in), villa_iso_date(h.check_out), '[)')
  && daterange(villa_iso_date(b.check_in), villa_iso_date(b.check_out), '[)')
 WHERE h.status = ANY (villa_occupying_states('hold'))
   AND b.status = ANY (villa_occupying_states('booking'));

-- (d) the same check inside each table (002's EXCLUDE should already make this 0)
```

Record: server version, `max_locks_per_transaction`, `max_connections`, `deadlock_timeout`,
`lock_timeout`, `statement_timeout`, the fingerprint from (a), and the counts from (c)/(d) —
in the change ticket, not in this repository.

## 5. Prerequisites

1. **A restore point that has been restored, not just taken.** A backup nobody has restored is a
   hope. Restore it into a disposable database, apply the v2 DDL there, and run §4(c) again.
2. **A maintenance window** sized for the DDL, not for the migration bookkeeping: creating a
   trigger takes `ACCESS EXCLUSIVE` on `holds` and `bookings`, so writers are blocked, not merely
   slowed. Short window, explicit start/stop, operator on the call.
3. **Owner + operator approval on record** for: the window, the exact SHA-256 being installed
   (§2), and the ledger reconciliation note (§8).
4. **No pending conflicting work**: no in-flight reservation batch, no long transaction on either
   table (`pg_stat_activity` clean of `idle in transaction` on the target database).

## 6. Refusal conditions — do not proceed if any is true

- Ledger state is `CONFLICTED`, or the stored `source_sha256` is neither of the two values in §2.
- §4(c) returns anything other than 0: the database is already double-booked, and 021 refuses to
  install over contradictions (verified in 004A: `MIGRATION_FAILED`, full rollback, zero triggers,
  data intact). Resolve the conflicting rows first, with a written decision, then restart.
- The restore point has not been restored successfully.
- The window cannot accommodate `ACCESS EXCLUSIVE` on both tables.
- Anybody proposes editing `pg_schema_migrations` to make the runner accept the state.

## 7. Apply (supervised, manual)

1. Announce the window; stop reservation writers.
2. Apply the approved v2 DDL from the file whose SHA-256 matches `ec9af603…` exactly.
3. Watch `pg_stat_activity` for blocked sessions; abort if a lock wait exceeds the window budget.
4. Do not run the migration runner against this database while the state is `CONFLICTED` in the
   hope that it "fixes itself". It will refuse, by design.

## 8. Ledger reconciliation (with evidence, never by hand-editing history)

The ledger is an audit record: it says what was applied and when. After a supervised manual
apply, either

- record the upgrade as a **new** ledger entry that names the manual operation, the operator, the
  window, and the v2 SHA-256; or
- rebuild the database from the approved baseline and let the runner apply v2 normally.

Never rewrite the existing `021` row's `source_sha256` to the new value: that would claim v2 was
applied at the original timestamp, which is false. Never delete the row.

## 9. Post-upgrade verification

```sql
-- generation is v2
SELECT to_regprocedure('villa_lock_occupied_nights(text,text,text)') IS NOT NULL;              -- t
SELECT position('villa-occupancy-night:' IN
       pg_get_functiondef('villa_lock_occupied_nights(text,text,text)'::regprocedure)) > 0;     -- t
-- triggers still exactly the approved pair
SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%cross_table_occupancy%';                   -- 2
-- invariant still holds
-- (re-run §4(c): 0)
```

Then, on a disposable copy, prove refusal behaviour has not weakened: an overlapping hold and
booking must still be refused with `23P01`, and back-to-back stays must still commit.

## 10. Recovery from a failed application

Measured behaviour (004A): a failed 021 install rolls back completely — zero triggers created,
ledger row `FAILED`, existing rows untouched. Recovery is: fix the data problem, re-run the
install. The restart path is idempotent (verified: second apply is a no-op), so a half-installed
guard is not a state you have to reason about.

If the DDL itself fails mid-way inside a manual `psql` session, the transaction boundary is yours
to guarantee: run the whole file in one transaction, or not at all.

## 11. Preserve the audit trail

- Keep the preflight output, the window record, the operator names and the applied SHA-256.
- Keep the old fingerprint in the ledger. It is the evidence that a v1 guard existed and that an
  upgrade happened; it is not clutter to be tidied away.
- Do not delete `pg_schema_migrations` rows, and do not "repair" checksums to obtain a green
  status. A green status obtained that way is a false statement about the database.

## 12. Out of scope

Automatic upgrade, down-migration, data backfills, and any change to 002 or its approved fixture
(`002_pg_range_exclusions.approved.json`). Those require their own pack and their own approval.

# Villa Agency OS — PACK-005C Recovery and Permission Runbook (Local Draft)

**Status:** prepared, not activated. This document does not authorize a live
permission change, a timer, a deployment, or a production migration. The Owner
must approve activation separately.

## 1. What exists after PACK-005C

| Component | Path | Purpose |
|---|---|---|
| Backup engine | `scripts/backup_sqlite.py` | SQLite Online Backup API, private output, SHA-256 manifest |
| Restore drill | `scripts/verify_backup.py` | disposable restore, integrity/FK, ledger and count reconciliation |
| Shared helpers | `scripts/backup_common.py` | read-only access, inventory, hashing, exclusive publication |
| Permission planner | `scripts/harden_db_permissions.py` | check-only by default; `--apply` only on a disposable/approved target |
| Backup-age monitor | `scripts/check_backup_age.py` | read-only RPO check; never deletes or creates a backup |
| Disabled scheduler drafts | `deploy/systemd/villa-backup.service`, `.timer` | deployment-ready but no `[Install]` section |
| Focused tests | `tests/test_pack_005c_operational_safeguards.py` | T005C-01…16 plus adversarial cases |

## 2. Backup

```bash
python3 scripts/backup_sqlite.py \
  --db /path/to/villa.db \
  --dest-dir /path/to/private-backups
```

- The source is opened with `mode=ro`; the Online Backup API captures committed
  WAL contents without writing to the source. The source must be a regular file
  owned by the current service account; a mismatch is refused.
- The destination is created as `0700` when absent; an existing destination must
  already be `0700` and owned by the current user, otherwise the engine refuses
  rather than silently changing a shared directory.
- The backup and manifest are `0600`; a per-database `flock` prevents a second
  concurrent job.
- The database is linked into place first, then the manifest. A crash before the
  manifest leaves a single unverified database file, never a false success; it is
  intentionally not deleted automatically.
- Existing file names are never overwritten. A controlled failure before
  publication leaves no published database or manifest.
- No cleanup/retention deletion is implemented. This is deliberate until the
  retention and recoverability policy is approved.

Exit codes: `0` success, `1` controlled failure, `2` command-line usage error.
The operator log contains paths, counts, and hashes — never row values, guest
data, credentials, or payment data.

## 3. Restore verification (mandatory drill)

```bash
python3 scripts/verify_backup.py \
  --backup /path/to/private-backups/<name>.db \
  --json
```

The verifier:

1. reads the manifest and rejects a missing/invalid one;
2. checks file size and SHA-256 before opening anything;
3. restores into a new `0700` temporary directory with SQLite's Online Backup API;
4. runs `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, schema inventory,
   migration-ledger comparison, and source/restore row-count reconciliation;
5. deletes the disposable copy on success and on failure;
6. never opens or replaces an operational database.

A corrupt, truncated, ledger-mismatched, or count-mismatched backup is a failure.
A file merely existing is not a restore proof. For the existing audit-chain and
migration checks, also run the repository's existing preflight path:

```bash
python3 scripts/preflight.py --db /path/to/villa.db \
  --backup /path/to/private-backups/<name>.db
```

## 4. Scheduler (draft only)

The units live in `deploy/systemd/` and have **no `[Install]` section**. They
cannot be enabled accidentally with `systemctl enable`; activation needs an
explicit Owner decision.

Configuration file expected by the service (not created by this pack):

```ini
# ~/.config/villa/backup.env  (mode 0600)
VILLA_BACKUP_DB=/absolute/path/to/villa.db
VILLA_BACKUP_DIR=/absolute/path/to/private-backups
```

The service uses `UMask=0077`, a read-only source path, a writable backup path,
and `journal` for failure visibility. Do **not** put whitespace in either path:
systemd splits `ExecStart` words. The exclusive `flock` makes overlapping timer
runs safe; a second run exits with `BACKUP_ALREADY_RUNNING`.

Backup-age monitoring is a read-only preflight/runbook check, not a deletion job.
Run it after each scheduled backup (or from the timer's failure handler, once
approved):

```bash
python3 scripts/check_backup_age.py \
  --backup-dir /path/to/private-backups \
  --max-age-hours 24 --json
```

Exit `0` means a valid manifest with a backup file is within the RPO; exit `1`
means stale/no usable manifest. It never creates, deletes, or moves a backup.
Alerting must be configured by the Owner/operations owner (D13) before the timer
is enabled.

Activation checklist (separate approval; none of these steps were run here):

1. Owner approves the timer and the retention/alerting policy.
2. Create `~/.config/villa/backup.env` as `0600`; create the destination `0700`.
3. Confirm the backup directory owner is the service user.
4. `systemctl --user daemon-reload`.
5. `systemctl --user start villa-backup.service` once; inspect journal and run
   `verify_backup.py`.
6. Only then `systemctl --user enable --now villa-backup.timer`.
7. Record the first verified backup and its manifest hash as evidence.

## 5. Permission hardening plan (C4)

Reported current state at pack start:

```text
data/                 0775
data/villa.db         0644
data/villa.db-wal     0644
data/villa.db-shm     0644
```

Target for a single-service-account deployment:

```text
database directory   0700   owner = service account
database and WAL/SHM 0600   owner = service account
backup directory     0700   owner = service account
backup + manifest    0600   owner = service account
```

Evaluate before activation:

- **Service user/group:** run the dashboard and backup as the same non-root
  service account that owns `villa.db`; do not grant a shared group access.
- **Directory traversal:** `0700` on the database directory is required so the
  service account is the only traverser; a broader parent is outside this scope.
- **WAL/SHM creation:** SQLite creates sidecars next to the database with the
  process umask. The service uses `UMask=0077`; re-check sidecar modes after the
  first restart.
- **Restart/reopen:** stop the service before a live permission change; after
  restart, verify the service can open the database and that the expected
  `-wal`/`-shm` files exist with `0600`.
- **Backup ownership:** the backup runs as the same account; a root-owned backup
  directory is a failure, not a convenience.
- **Rollback:** record the exact prior modes and owner first. If the service
  cannot reopen, revert the directory to its recorded prior mode and restore
  access for the owning account; do not broaden permissions to `0666`/`0777`.
  Investigate and retry only after the cause is understood.
- **No live change in PACK-005C:** use the planner/tests on a disposable
  directory only.

Disposable verification:

```bash
python3 scripts/harden_db_permissions.py --db /tmp/disposable/villa.db --json
python3 scripts/harden_db_permissions.py --db /tmp/disposable/villa.db --apply
```

The planner refuses a symlink, a non-regular database, or a path not owned by the
current user. `--apply` is never run on `data/villa.db` by this pack.

## 6. Operational evidence to retain

- backup path, timestamp, source path, source SHA-256, backup SHA-256, byte size;
- manifest JSON (contains schema names/counts only);
- restore-verification result with exit code and checks;
- the exact test command, test counts, duration, and exit code;
- for any future permission/timer change: Owner approval reference and the
  before/after mode inventory.

## 7. Known limits

- SQLite is the operational store in this pack; PostgreSQL cutover remains
  blocked by its own migration/runner evidence.
- No off-host upload is implemented; off-host copies need an approved destination
  and key/access policy.
- No retention deletion is implemented.
- `data/villa.db` was not modified, permissioned, or used as a test fixture.

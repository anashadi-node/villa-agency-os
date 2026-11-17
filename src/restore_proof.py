"""FR-918 restore proof: a backup counts only if it can be restored and then checked.

``scripts/preflight.py`` used to judge a backup by existence, size and age, so any 8 KiB
file passed. This module restores the file into a private temporary directory with the
SQLite Online Backup API and runs every check on the *restored copy*, opened read-only.

Rules that make the result trustworthy:

* The backup is read with ``mode=ro&immutable=1``: it is never written, locked or given a
  ``-wal``/``-shm`` neighbour. Its sha256 and its directory listing are compared before and
  after; any change is a FAIL.
* Nothing here calls ``connect_sqlite`` or ``apply_schema``. Those build the schema on an
  empty file, which would turn an empty SQLite file into a database that "passes".
* The restored copy is opened ``mode=ro``, so a check cannot create a table even by mistake.
* Only the temporary directory created by this module is deleted.
* An anchor that cannot be checked is ``NOT_VERIFIED``, never ``OK``. No anchor is invented.

The duration is measured on the file given. On a synthetic file it proves the mechanism, not
the production RTO (D10: RTO 4 h, RPO 24 h).
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anchors import AnchorError, verify_published_anchors
from audit import AUDIT_TABLE, AuditLog
from primitives import DomainError, IdFactory, SystemClock
from schema_loader import SCHEMA_VERSION, SchemaError, applied_version, missing_migrations

OK = "OK"
FAIL = "FAIL"
NOT_VERIFIED = "NOT_VERIFIED"

#: D10 (GO_LIVE_DECISIONS.md): RTO 4 hours. A budget the restore of THIS file must fit in.
RTO_BUDGET_SECONDS = 4 * 3600.0
REQUIRED_TABLES = ("schema_meta", "schema_migrations", AUDIT_TABLE)


@dataclass(frozen=True)
class RestoreProof:
    verdict: str  # OK | FAIL | NOT_VERIFIED
    detail: str
    started_at: str  # UTC, ISO-8601
    restore_seconds: Optional[float]  # sqlite3.backup() only; None if it never ran
    total_seconds: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_uri(path: Path, *, immutable: bool) -> str:
    return path.resolve().as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")


def _verify_restored(ro: sqlite3.Connection, anchor_dir: Optional[Path]) -> tuple[list[str], str, str]:
    """Run every check; return (failures, facts, anchor_state). Reads only."""
    failures: list[str] = []
    facts: list[str] = []

    integrity = [row[0] for row in ro.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        failures.append(f"integrity_check: {'; '.join(integrity)[:200]}")
    else:
        facts.append("integrity ok")
    dangling = ro.execute("PRAGMA foreign_key_check").fetchall()
    if dangling:
        failures.append(f"foreign_key_check: {len(dangling)} dangling row(s)")

    tables = {r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    absent = [name for name in REQUIRED_TABLES if name not in tables]
    if absent:
        # Not a Villa Agency database: the rest cannot be judged, and must not pass.
        failures.append(f"not a Villa Agency database, missing table(s): {', '.join(absent)}")
        return failures, "", "NOT_RUN"

    version = applied_version(ro)
    if version != SCHEMA_VERSION:
        failures.append(f"schema version {version!r}, code expects {SCHEMA_VERSION!r}")
    try:
        missing = missing_migrations(ro)
    except (SchemaError, sqlite3.Error) as exc:
        failures.append(f"migration ledger unreadable: {exc}")
    else:
        if missing is None:
            failures.append("no schema_migrations ledger")
        elif missing:
            failures.append(f"{len(missing)} migration(s) not recorded: {', '.join(missing[:10])}")
        else:
            facts.append("ledger complete")

    try:
        audit = AuditLog(ro, clock=SystemClock(), ids=IdFactory(True, "restore-proof"))
        facts.append(f"audit chain intact (seq {audit.verify().count})")
    except DomainError as exc:  # includes AUDIT_APPEND_ONLY_UNENFORCED and every chain break
        failures.append(f"audit chain {getattr(exc, 'code', 'BROKEN')}: {exc}")
        return failures, ", ".join(facts), "NOT_RUN"
    if anchor_dir is None:
        return failures, ", ".join(facts), "NOT_VERIFIED"
    if not anchor_dir.is_dir():
        failures.append(f"anchor directory missing: {anchor_dir}")
        return failures, ", ".join(facts), "FAIL"
    try:
        result = verify_published_anchors(conn=ro, audit=audit, directory=anchor_dir)
    except (AnchorError, DomainError) as exc:
        failures.append(f"anchor {getattr(exc, 'code', 'ANCHOR_ERROR')}: {exc}")
        return failures, ", ".join(facts), "FAIL"
    if not result.ok:
        failures.append(f"anchor issues: {'; '.join(result.issues)[:200]}")
        return failures, ", ".join(facts), "FAIL"
    facts.append(f"anchor {result.latest_file} verified")
    return failures, ", ".join(facts), OK


def prove_restore(
    backup: Path,
    *,
    anchor_dir: Optional[Path] = None,
    budget_seconds: float = RTO_BUDGET_SECONDS,
) -> RestoreProof:
    """Restore ``backup`` into a private temp dir and verify the copy. Never raises."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    clock_start = time.perf_counter()
    restore_seconds: Optional[float] = None

    def done(verdict: str, detail: str) -> RestoreProof:
        return RestoreProof(
            verdict, detail, started_at, restore_seconds, time.perf_counter() - clock_start
        )

    backup = Path(backup)
    if not backup.is_file():
        return done(FAIL, f"backup not found: {backup}")
    # immutable=1 makes SQLite ignore -wal and -journal files, so a backup that still has
    # either one pending would be read as a half-written snapshot. Only a finished, quiet
    # file (what the .backup command leaves) may be opened that way; anything else is refused.
    for suffix in ("-wal", "-journal"):
        pending = backup.with_name(backup.name + suffix)
        if pending.exists() and pending.stat().st_size > 0:
            return done(
                FAIL,
                f"backup has a pending {suffix} file; take backups with the .backup command",
            )

    sha_before = _sha256(backup)
    listing_before = sorted(p.name for p in backup.parent.iterdir())
    workdir = Path(tempfile.mkdtemp(prefix="villa-restore-proof-"))
    failures: list[str] = []
    facts = ""
    anchor_state = "NOT_RUN"
    try:
        restored = workdir / "restored.sqlite"
        source = sqlite3.connect(_readonly_uri(backup, immutable=True), uri=True)
        target = sqlite3.connect(restored)
        try:
            t0 = time.perf_counter()
            source.backup(target)
            restore_seconds = time.perf_counter() - t0
        finally:
            target.close()
            source.close()
        ro = sqlite3.connect(_readonly_uri(restored, immutable=False), uri=True)
        ro.row_factory = sqlite3.Row  # AuditLog reads rows by name; this does not write
        try:
            objects_before = ro.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            failures, facts, anchor_state = _verify_restored(ro, anchor_dir)
            if ro.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] != objects_before:
                failures.append("the checks changed the restored schema")
        finally:
            ro.close()
    except sqlite3.Error as exc:
        failures.append(f"cannot restore or read the backup: {exc}")
    except OSError as exc:
        failures.append(f"cannot restore the backup: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if _sha256(backup) != sha_before or sorted(p.name for p in backup.parent.iterdir()) != listing_before:
        failures.append("the backup or its directory changed during the proof")
    total = time.perf_counter() - clock_start
    if total > budget_seconds:
        failures.append(f"took {total:.2f}s, over the {budget_seconds:.0f}s budget")
    if failures:
        return done(FAIL, "; ".join(failures))
    timing = f"restored in {restore_seconds:.3f}s at {started_at} (measured on this file only)"
    if anchor_state == NOT_VERIFIED:
        return done(NOT_VERIFIED, f"{facts}; anchor not verified (no anchor directory); {timing}")
    return done(OK, f"{facts}; {timing}")

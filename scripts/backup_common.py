"""Small, shared building blocks for the operational SQLite backup tools.

The backup path is deliberately boring: standard library only, read-only access to
the source, private directories, exclusive file creation, and a manifest made of
counts and hashes — never row values. This module is imported by
``scripts/backup_sqlite.py`` and ``scripts/verify_backup.py`` so both sides compute
identity and inventory the same way.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


BACKUP_FORMAT = "villa-sqlite-backup-v1"
MANIFEST_SUFFIX = ".manifest.json"
CHUNK_SIZE = 1024 * 1024


class BackupError(Exception):
    """A controlled failure with an actionable, stable code.

    The code is what an operator greps for; the message is what an operator acts
    on. Neither contains row values, credentials, or other guest data.
    """

    def __init__(self, code: str, message: str, *, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.detail:
            value["detail"] = self.detail
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_value(value: os.stat_result) -> dict[str, int]:
    return {
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "inode": value.st_ino,
        "device": value.st_dev,
        "mode": stat.S_IMODE(value.st_mode),
    }


def readonly_uri(path: Path, *, immutable: bool = False) -> str:
    """A URI that cannot write to the file, even by accident.

    ``immutable`` is only for a completed backup with no sidecars. It must not be
    used for the live source: it would hide a WAL that has not been checkpointed.
    """
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"{path.resolve().as_uri()}?{query}"


def connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _ident(name: str) -> str:
    # Names come from sqlite_master, not from a request. Doubling quotes keeps a
    # strange but legal identifier from breaking the count query.
    return '"' + name.replace('"', '""') + '"'


def inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """Schema names, row counts, and the migration ledger — no row contents."""
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    indexes = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' ORDER BY name"
        )
    ]
    triggers = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for table in tables:
        row = conn.execute(f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()
        counts[table] = int(row[0]) if row is not None else 0

    ledger: list[str] = []
    if "schema_migrations" in tables:
        ledger = [
            str(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY rowid"
            )
        ]
    schema_version = None
    if "schema_meta" in tables:
        row = conn.execute(
            "SELECT schema_version FROM schema_meta ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        schema_version = str(row[0]) if row is not None else None
    return {
        "tables": tables,
        "indexes": indexes,
        "triggers": triggers,
        "counts": counts,
        "migration_ledger": ledger,
        "schema_version": schema_version,
    }


def source_identity(path: Path) -> dict[str, Any]:
    """The source's identity before and after a backup.

    The ``-shm`` file is shared memory: SQLite may change its bytes just by opening
    a WAL database read-only. It is recorded for context but is not part of the
    no-mutation comparison. The database and WAL are.
    """
    resolved = path.resolve(strict=True)
    info: dict[str, Any] = {"path": str(resolved), **_stat_value(resolved.stat())}
    info["sha256"] = sha256_file(resolved)
    info["sidecars"] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(resolved) + suffix)
        if sidecar.exists() and sidecar.is_file():
            sidecar_info: dict[str, Any] = {**_stat_value(sidecar.stat())}
            if suffix != "-shm":
                sidecar_info["sha256"] = sha256_file(sidecar)
            info["sidecars"][suffix] = sidecar_info
    return info


def identity_differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Fields that prove the source changed. Volatile ``-shm`` bytes are excluded.

    Opening a WAL database read-only can create an *empty* ``-wal``/``-shm`` pair
    even when no row changed. An empty WAL is not evidence of mutation; a non-empty
    WAL, a changed WAL, or a changed database file is.
    """
    differences: list[str] = []
    for key in ("sha256", "size", "inode", "device"):
        if before.get(key) != after.get(key):
            differences.append(f"database {key}")
    before_sidecars = before.get("sidecars", {})
    after_sidecars = after.get("sidecars", {})
    for suffix in ("-wal", "-journal"):
        old = before_sidecars.get(suffix)
        new = after_sidecars.get(suffix)
        old_size = int(old.get("size", 0)) if old else 0
        new_size = int(new.get("size", 0)) if new else 0
        if not old_size and not new_size:
            # A benign empty sidecar may appear or disappear on a read-only open.
            continue
        if old is None or new is None or old_size != new_size:
            differences.append(f"{suffix} changed")
        elif old.get("sha256") != new.get("sha256"):
            differences.append(f"{suffix} bytes changed")
    return differences


def ensure_private_dir(path: Path) -> Path:
    """Create the destination as 0700, or refuse to use an existing shared one."""
    path = path.expanduser()
    if path.is_symlink():
        raise BackupError("BACKUP_DIR_SYMLINK", f"backup directory is a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise BackupError("BACKUP_DIR_NOT_DIR", f"backup path is not a directory: {path}")
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise BackupError(
                "BACKUP_DIR_INSECURE",
                f"backup directory is not private ({oct(mode)}); expected 0700: {path}",
                detail="chmod 700 the destination after checking its owner, then retry",
            )
        if info.st_uid != os.geteuid():
            raise BackupError(
                "BACKUP_DIR_OWNER",
                f"backup directory is owned by uid {info.st_uid}, not the current user: {path}",
                detail="run the backup as the directory owner; do not broaden permissions",
            )
        return path
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Semgrep's file-mode rule expects 0644; this is a directory and 0700 is the
    # restrictive target (owner-only traversal), not a file readable by others.
    os.chmod(path, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    return path


def secure_file(path: Path) -> None:
    """Require a regular file and set it to 0600, without following a symlink."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BackupError("BACKUP_FILE_UNSAFE", f"not a regular file: {path}")
    os.chmod(path, 0o600)


def fsync_dir(path: Path) -> None:
    """Make a directory entry durable after a create/link/unlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new_bytes(path: Path, payload: bytes) -> None:
    """Exclusive create + fsync. An existing path is never truncated."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise BackupError("FILE_EXISTS", f"refusing to overwrite: {path}") from exc
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    write_new_bytes(path, payload + b"\n")


def publish_exclusive(source: Path, destination: Path) -> None:
    """Publish a completed temporary file without ever replacing a backup."""
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise BackupError("BACKUP_EXISTS", f"refusing to overwrite: {destination}") from exc
    fsync_dir(destination.parent)
    os.unlink(source)
    fsync_dir(destination.parent)


def lock_path(destination: Path, source: Path) -> Path:
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    return destination / f".backup-{digest}.lock"


@contextmanager
def exclusive_lock(path: Path, *, label: str = "backup") -> Iterator[None]:
    """One live job per configured database.

    ``flock`` is released by the kernel if the process dies, which is why it is
    preferred over a hand-written PID file. The lock file is intentionally kept;
    deleting it would create a race with a process that already opened it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError(
                "BACKUP_ALREADY_RUNNING",
                f"another {label} is already running for this database (lock: {path})",
                detail="wait for the active job to exit; do not start a second backup",
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def unique_name(source: Path, *, label: Optional[str] = None) -> str:
    stem = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in source.stem
    )[:48] or "database"
    if label:
        clean_label = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in label
        )[:40]
        stem = f"{stem}-{clean_label}"
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{stem}-{stamp}-{uuid.uuid4().hex[:8]}.db"

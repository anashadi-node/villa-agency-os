"""Publish the audit-chain head outside the database.

The chain in :mod:`audit` proves the *order* and *content* of every recorded
event, but it cannot prove that the newest rows still exist: deleting the tail
leaves a perfectly valid shorter chain. Closing that hole needs a number that
lives somewhere else — a *published anchor*: ``(count, head_hash)`` written to a
second system (a directory that can be mounted read-only or WORM) and recorded
back into the chain itself.

Two failures are therefore detected that the database alone cannot see:

* **Truncation** — the database has fewer events than the newest anchor, or the
  same number with a different head (history recomputed).
* **Anchor tampering** — an anchor file edited or removed after publication; the
  file's own hash is recorded in an audit event, and each anchor names its
  predecessor, so the anchor files form a small chain of their own.

Nothing here talks to the network: publishing means writing one JSON file to a
directory the caller controls, plus one audit event. An operator copies that
directory to a second system (or object storage with retention) on their own
schedule. No guest data is written: an anchor is two numbers and hashes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from audit import AuditLog, ChainAnchor
from primitives import (
    DomainError,
    canonical_json,
    require_ref,
    sha256_hex,
    to_iso,
)

__all__ = [
    "ANCHOR_SCHEMA",
    "ANCHOR_AUDIT_ACTION",
    "AnchorError",
    "AnchorDirectoryMissing",
    "AnchorNotFound",
    "AnchorPublishRefused",
    "AnchorFileTampered",
    "PublishedAnchor",
    "AnchorVerification",
    "publish_anchor",
    "latest_published",
    "verify_published_anchors",
]

ANCHOR_SCHEMA = "villa-chain-anchor/1"
ANCHOR_AUDIT_ACTION = "chain.anchor_published"
ANCHOR_ENTITY_TYPE = "audit_chain"
ANCHOR_FILE_PREFIX = "anchor-"
ANCHOR_FILE_SUFFIX = ".json"
HEX64 = 64


class AnchorError(DomainError):
    """Base class for anchor failures."""


class AnchorDirectoryMissing(AnchorError):
    """The publication directory does not exist (fail closed, never create it)."""


class AnchorNotFound(AnchorError):
    """No anchor has been published yet."""


class AnchorPublishRefused(AnchorError):
    """Publishing would record a head that moves backwards or sideways."""


class AnchorFileTampered(AnchorError):
    """Published anchor files no longer form the chain they claim to."""


@dataclass(frozen=True)
class PublishedAnchor:
    """One anchor file: what it says and where it lives."""

    file_name: str
    path: str
    count: int
    head_hash: str
    published_at: str
    file_sha256: str
    previous_file: Optional[str] = None
    previous_sha256: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "count": self.count,
            "head_hash": self.head_hash,
            "published_at": self.published_at,
            "file_sha256": self.file_sha256,
            "previous_file": self.previous_file,
            "previous_sha256": self.previous_sha256,
        }


@dataclass(frozen=True)
class AnchorVerification:
    """Result of re-checking the published anchors against the live chain."""

    latest_file: str
    count: int
    head_hash: str
    files_checked: int
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "latest_file": self.latest_file,
            "count": self.count,
            "head_hash": self.head_hash,
            "files_checked": self.files_checked,
            "issues": list(self.issues),
            "ok": self.ok,
        }


def _anchor_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise AnchorDirectoryMissing(
            "anchor directory does not exist; publish refused",
            code="ANCHOR_DIRECTORY_MISSING",
            path=str(directory),
        )
    files = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith(ANCHOR_FILE_PREFIX)
        and path.name.endswith(ANCHOR_FILE_SUFFIX)
    ]
    # Names are timestamped, so lexical order is chronological order; the count is
    # part of the name so two publications inside the same second stay ordered.
    return sorted(files, key=lambda path: path.name)


def _parse_anchor(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise AnchorFileTampered(
            "anchor file is not valid JSON",
            code="ANCHOR_FILE_INVALID",
            file=path.name,
        ) from exc
    if not isinstance(raw, dict):
        raise AnchorFileTampered(
            "anchor file must contain a JSON object",
            code="ANCHOR_FILE_INVALID",
            file=path.name,
        )
    if raw.get("schema") != ANCHOR_SCHEMA:
        raise AnchorFileTampered(
            "anchor file declares an unknown schema",
            code="ANCHOR_FILE_INVALID",
            file=path.name,
            schema=raw.get("schema"),
        )
    count = raw.get("count")
    head = raw.get("head_hash")
    if not isinstance(count, int) or count < 0:
        raise AnchorFileTampered(
            "anchor count must be a non-negative integer",
            code="ANCHOR_FILE_INVALID",
            file=path.name,
        )
    if not isinstance(head, str) or len(head) != HEX64:
        raise AnchorFileTampered(
            "anchor head_hash must be 64 hex characters",
            code="ANCHOR_FILE_INVALID",
            file=path.name,
        )
    return raw


def _file_sha256(path: Path) -> str:
    """Hash the file exactly as published (text, utf-8)."""

    return sha256_hex(path.read_text(encoding="utf-8"))


def latest_published(directory: str | os.PathLike[str]) -> Optional[PublishedAnchor]:
    """Return the newest published anchor, or ``None`` when none exists."""

    base = Path(directory)
    files = _anchor_files(base)
    if not files:
        return None
    path = files[-1]
    raw = _parse_anchor(path)
    return PublishedAnchor(
        file_name=path.name,
        path=str(path),
        count=int(raw["count"]),
        head_hash=str(raw["head_hash"]),
        published_at=str(raw.get("published_at", "")),
        file_sha256=_file_sha256(path),
        previous_file=raw.get("previous_file"),
        previous_sha256=raw.get("previous_sha256"),
    )


def _check_anchor_chain(files: list[Path]) -> None:
    """Fail closed unless the anchor files link to each other and never shrink."""

    if not files:
        return
    first = _parse_anchor(files[0])
    if first.get("previous_file") is not None:
        raise AnchorFileTampered(
            "the oldest anchor file names a predecessor that is no longer present",
            code="ANCHOR_CHAIN_BROKEN",
            file=files[0].name,
            found_previous=first.get("previous_file"),
        )

    previous: Optional[dict[str, Any]] = None
    previous_path: Optional[Path] = None
    for path in files:
        raw = _parse_anchor(path)
        if previous is not None and previous_path is not None:
            if raw.get("previous_file") != previous_path.name:
                raise AnchorFileTampered(
                    "anchor file does not name its predecessor: a file was inserted or removed",
                    code="ANCHOR_CHAIN_BROKEN",
                    file=path.name,
                    expected_previous=previous_path.name,
                    found_previous=raw.get("previous_file"),
                )
            if raw.get("previous_sha256") != _file_sha256(previous_path):
                raise AnchorFileTampered(
                    "predecessor anchor file was modified after publication",
                    code="ANCHOR_CHAIN_BROKEN",
                    file=path.name,
                    previous=previous_path.name,
                )
            if int(raw["count"]) < int(previous["count"]):
                raise AnchorFileTampered(
                    "anchor count went backwards",
                    code="ANCHOR_CHAIN_BROKEN",
                    file=path.name,
                    previous_count=int(previous["count"]),
                    count=int(raw["count"]),
                )
        previous, previous_path = raw, path


def publish_anchor(
    *,
    conn: Any,
    audit: AuditLog,
    clock: Any,
    directory: str | os.PathLike[str],
    actor: str,
    actor_role: str,
    label: Optional[str] = None,
) -> PublishedAnchor:
    """Write the current chain head to ``directory`` and record it in the chain.

    Refuses to publish when the chain does not verify, when the head would move
    backwards (rows were deleted), or when the head would move sideways at the
    same length (rows were rewritten) — in all three cases nothing is written.
    """

    actor = require_ref(actor, "actor")
    actor_role = require_ref(actor_role, "actor_role")
    base = Path(directory)
    files = _anchor_files(base)
    _check_anchor_chain(files)
    previous = latest_published(base)

    # Fail closed *before* writing: an anchor published over a broken chain would
    # bless whatever the database currently claims.
    audit.verify()
    head = audit.anchor()

    if previous is not None:
        # Extending a tampered series would launder the tampering: the new file
        # would record the *edited* hash of its predecessor as the truth.
        recorded = _recorded_anchor_hashes(audit)
        expected = recorded.get(previous.file_name)
        if expected is None:
            raise AnchorFileTampered(
                "previous anchor file is not recorded in the chain",
                code="ANCHOR_NOT_RECORDED",
                file=previous.file_name,
            )
        if expected != previous.file_sha256:
            raise AnchorFileTampered(
                "previous anchor file changed after publication",
                code="ANCHOR_FILE_CHANGED",
                file=previous.file_name,
            )

    if previous is not None:
        if head.count < previous.count:
            raise AnchorPublishRefused(
                "chain is shorter than the published anchor: refusing to bless it",
                code="ANCHOR_ROLLBACK",
                observed_count=head.count,
                anchor_count=previous.count,
            )
        if head.count == previous.count and head.head_hash != previous.head_hash:
            raise AnchorPublishRefused(
                "chain head changed at the published length: history was rewritten",
                code="ANCHOR_REWRITTEN",
                count=head.count,
            )

    published_at = to_iso(clock.now())
    stamp = published_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    file_name = f"{ANCHOR_FILE_PREFIX}{stamp}-{head.count:08d}{ANCHOR_FILE_SUFFIX}"
    body: dict[str, Any] = {
        "schema": ANCHOR_SCHEMA,
        "published_at": published_at,
        "count": head.count,
        "head_hash": head.head_hash,
        "previous_file": previous.file_name if previous else None,
        "previous_sha256": previous.file_sha256 if previous else None,
        "label": label,
    }
    text = canonical_json(body) + "\n"
    final_path = base / file_name
    tmp_path = base / f".{file_name}.tmp"
    tmp_path.write_text(text, encoding="utf-8")
    if final_path.exists():
        # Two publishers raced for the same head: refusing is the only safe
        # answer, because the second file would look like a second anchor.
        tmp_path.unlink()
        raise AnchorPublishRefused(
            "an anchor file for this head already exists: another publisher won "
            "the race (or the clock and count repeated)",
            code="ANCHOR_FILE_EXISTS",
            file=file_name,
            count=head.count,
        )
    os.replace(tmp_path, final_path)
    os.chmod(final_path, 0o644)
    file_sha = sha256_hex(text)

    audit.append(
        action=ANCHOR_AUDIT_ACTION,
        entity_type=ANCHOR_ENTITY_TYPE,
        entity_id=file_name,
        actor=actor,
        actor_role=actor_role,
        after={
            "count": head.count,
            "head_hash": head.head_hash,
            "file_sha256": file_sha,
            "file": file_name,
        },
        reason=label,
        evidence_ref=file_name,
    )

    return PublishedAnchor(
        file_name=file_name,
        path=str(final_path),
        count=head.count,
        head_hash=head.head_hash,
        published_at=published_at,
        file_sha256=file_sha,
        previous_file=previous.file_name if previous else None,
        previous_sha256=previous.file_sha256 if previous else None,
    )


def _recorded_anchor_hashes(audit: AuditLog) -> dict[str, str]:
    """Map ``file_name -> recorded sha256`` from the chain's own anchor events."""

    recorded: dict[str, str] = {}
    for event in audit.iter_events(entity_type=ANCHOR_ENTITY_TYPE):
        if event.action != ANCHOR_AUDIT_ACTION:
            continue
        payload: Any = event.after_state
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:  # pragma: no cover - defensive
                continue
        if not isinstance(payload, dict):
            continue
        file_name = payload.get("file")
        file_sha = payload.get("file_sha256")
        if isinstance(file_name, str) and isinstance(file_sha, str):
            recorded[file_name] = file_sha
    return recorded


def verify_published_anchors(
    *,
    conn: Any,
    audit: AuditLog,
    directory: str | os.PathLike[str],
    strict: bool = True,
) -> AnchorVerification:
    """Re-check the live chain against every published anchor.

    Raises the same exceptions as :func:`audit.verify_chain` when the database
    was truncated or rewritten, and :class:`AnchorFileTampered` when the anchor
    files themselves were edited or removed. With ``strict=False`` the problem is
    reported in ``issues`` instead of raised (for dashboards).
    """

    base = Path(directory)
    files = _anchor_files(base)
    if not files:
        raise AnchorNotFound(
            "no published anchor found: the chain cannot be checked for truncation",
            code="ANCHOR_NOT_FOUND",
            path=str(base),
        )

    issues: list[str] = []
    try:
        _check_anchor_chain(files)
    except AnchorFileTampered as exc:
        if strict:
            raise
        issues.append(f"{exc.code}: {exc}")

    latest_path = files[-1]
    raw = _parse_anchor(latest_path)
    anchor = ChainAnchor(count=int(raw["count"]), head_hash=str(raw["head_hash"]))

    try:
        # Raises when the live chain has fewer events than the anchor, or the same
        # number with a different head.
        audit.verify(anchor=anchor)
    except DomainError as exc:
        if strict:
            raise
        issues.append(f"{getattr(exc, 'code', 'AUDIT_ANCHOR_MISMATCH')}: {exc}")

    recorded = _recorded_anchor_hashes(audit)
    expected = recorded.get(latest_path.name)
    actual = _file_sha256(latest_path)
    if expected is None:
        message = (
            f"ANCHOR_NOT_RECORDED: {latest_path.name} has no matching "
            "chain.anchor_published event"
        )
        if strict:
            raise AnchorFileTampered(
                "anchor file is not recorded in the audit chain",
                code="ANCHOR_NOT_RECORDED",
                file=latest_path.name,
            )
        issues.append(message)
    elif expected != actual:
        if strict:
            raise AnchorFileTampered(
                "anchor file content changed after publication",
                code="ANCHOR_FILE_CHANGED",
                file=latest_path.name,
                recorded=expected,
                actual=actual,
            )
        issues.append(f"ANCHOR_FILE_CHANGED: {latest_path.name}")

    return AnchorVerification(
        latest_file=latest_path.name,
        count=anchor.count,
        head_hash=anchor.head_hash,
        files_checked=len(files),
        issues=tuple(issues),
    )

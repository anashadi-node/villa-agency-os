"""Evidence objects: the quarantine pipeline for files that arrive from outside.

Master Spec v1.1 §27 (document & evidence architecture), §12 (a commercial fact must
name the document it came from), §19 (data classification), §39 (no hard delete of
history), §53(8) (the document surface is an attack vector).

The pipeline is fixed and may not be shortened:

    UPLOAD → QUARANTINE → TYPE/SIZE VALIDATION → MALWARE SCAN → CLASSIFICATION
          → SAFE STORAGE → AUTHORIZED ACCESS

Three things are enforced in more than one layer, on purpose:

1. **Nothing reaches safe storage without a clean scan and a classification.** The
   database refuses the row (``ck_evidence_stored_requires_clean_scan``) and this
   service refuses the call, so "quarantine was skipped" is not a state the system can
   represent — not even for an imported legacy row.
2. **No scanner and no token key means no service.** A deployment without a malware
   scanner cannot promote files, and a deployment without a signing key cannot issue
   access grants. Both fail closed with a named error instead of degrading quietly.
   That is deliberate: a document pipeline that "works" without scanning is worse than
   one that is visibly unavailable.
3. **Access is always temporary and purpose-bound.** A grant is a row with an expiry,
   a purpose and (by default) a single use; the raw token is never stored, only its
   digest. L4 files must use the ``RESTRICTED_VAULT`` policy.

Not implemented here, and not pretended: encryption at rest and retention periods.
§49 leaves the exact retention periods OPEN, and field/object encryption needs a key
management decision, so ``retention_until`` exists as a column with no invented
default and the storage backend is a seam rather than an encryption claim.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    UTC,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    to_iso,
)

__all__ = [
    "ACCESS_POLICIES",
    "ALLOWED_CONTENT_TYPES",
    "CLASSIFICATIONS",
    "DEFAULT_MAX_BYTES",
    "EVIDENCE_KINDS",
    "PIPELINE",
    "AccessDenied",
    "AccessTokenSigner",
    "EvidenceError",
    "EvidenceNotFound",
    "EvidenceService",
    "InfectedFile",
    "InvalidEvidenceTransition",
    "LocalStorageBackend",
    "MalwareScanner",
    "NoScanner",
    "RejectedFile",
    "ScanResult",
    "ScannerUnavailable",
    "StorageUnavailable",
    "ValidationFailed",
]

EVIDENCE_KINDS: tuple[str, ...] = (
    "EMAIL", "WHATSAPP", "PDF", "SCREENSHOT", "PORTAL_REFERENCE",
    "PHONE_CONFIRMATION_NOTE", "BANK_REFERENCE", "OTHER",
)
CLASSIFICATIONS: tuple[str, ...] = ("L1", "L2", "L3", "L4")
ACCESS_POLICIES: tuple[str, ...] = ("AGENCY_SCOPED", "NEED_TO_KNOW", "RESTRICTED_VAULT")

#: The pipeline, in order. A step may only be entered from the step before it.
PIPELINE: tuple[str, ...] = (
    "QUARANTINED", "VALIDATED", "SCAN_PENDING", "SCANNED", "CLASSIFIED", "STORED",
)
NEXT_STEP: Mapping[str, str] = {
    "QUARANTINED": "VALIDATED",
    "VALIDATED": "SCAN_PENDING",
    "SCAN_PENDING": "SCANNED",
    "SCANNED": "CLASSIFIED",
    "CLASSIFIED": "STORED",
}

#: Content types the platform is willing to look at. §27 requires type validation and
#: the exact allowlist is an operational decision, so it is a constructor argument with
#: this documented default rather than a hidden constant.
ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "text/plain",
    "text/csv",
    "message/rfc822",
)
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

#: Magic bytes for the types where a mismatch is a real attack (a .pdf that is not a
#: PDF is how a polyglot file walks past an extension check).
MAGIC_BYTES: Mapping[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


class EvidenceError(DomainError):
    """Base class for deliberate refusals from this module."""


class EvidenceNotFound(EvidenceError):
    code = "RESOURCE_NOT_FOUND"


class ValidationFailed(EvidenceError):
    code = "VALIDATION_ERROR"


class RejectedFile(EvidenceError):
    code = "FILE_REJECTED"


class ScannerUnavailable(EvidenceError):
    code = "SOURCE_UNAVAILABLE"


class InfectedFile(EvidenceError):
    code = "FILE_INFECTED"


class StorageUnavailable(EvidenceError):
    code = "SOURCE_UNAVAILABLE"


class InvalidEvidenceTransition(EvidenceError):
    code = "STATE_TRANSITION_NOT_ALLOWED"


class AccessDenied(EvidenceError):
    code = "FORBIDDEN"


@dataclass(frozen=True)
class ScanResult:
    verdict: str  # CLEAN | INFECTED | UNKNOWN
    provider: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in ("CLEAN", "INFECTED", "UNKNOWN"):
            raise ValidationFailed(f"unknown scan verdict: {self.verdict!r}",
                                   code="VALIDATION_ERROR", verdict=self.verdict)


class MalwareScanner(Protocol):
    """A malware scanner. §27 requires the step; this is the seam for the engine."""

    name: str

    def scan(self, *, content: bytes, content_type: str, filename: str) -> ScanResult:  # pragma: no cover
        ...


class NoScanner:
    """The default: no scanner configured, so no file may be promoted.

    A deployment that has not wired a scanner is not silently trusted — every attempt
    to scan is refused with ``SOURCE_UNAVAILABLE`` and the file stays in quarantine.
    """

    name = ""

    def scan(self, *, content: bytes, content_type: str, filename: str) -> ScanResult:
        raise ScannerUnavailable(
            "no malware scanner is configured: files stay in quarantine",
            code="SOURCE_UNAVAILABLE",
            filename=filename,
        )


class StorageBackend(Protocol):
    """Where the bytes live. Quarantine and safe storage are separate roots."""

    name: str

    def put(self, *, key: str, content: bytes) -> str:  # pragma: no cover
        ...

    def get(self, key: str) -> bytes:  # pragma: no cover
        ...


class LocalStorageBackend:
    """Filesystem storage with the two properties this platform can actually promise.

    ``mode`` defaults to owner-only (0600) so a stored evidence file is not readable by
    other local accounts, and every key is confined to the root: a key that escapes it
    is refused rather than resolved. Encryption at rest is a deployment concern and is
    *not* claimed here.
    """

    #: A storage key is path-like ("evidence/<agency>/<id>"), not an opaque reference
    #: token, so `primitives.require_ref` is the wrong rule for it: the real control is
    #: confinement to the storage root plus an explicit refusal of traversal.
    MAX_KEY_LENGTH = 200

    def __init__(self, root: Any, *, name: str = "local", mode: int = 0o600) -> None:
        self.root = Path(root)
        self.name = name
        self.mode = mode

    def _target(self, key: Any) -> Path:
        if not isinstance(key, str) or not key.strip():
            raise StorageUnavailable("storage key is required", code="SOURCE_UNAVAILABLE")
        if "\x00" in key or len(key) > self.MAX_KEY_LENGTH:
            raise StorageUnavailable("storage key is not usable", code="SOURCE_UNAVAILABLE",
                                     storage_key=key[:40])
        if key.startswith("/") or key.startswith("\\"):
            raise StorageUnavailable("storage key must be relative to the storage root",
                                     code="SOURCE_UNAVAILABLE", storage_key=key)
        parts = [part for part in key.replace("\\", "/").split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise StorageUnavailable("storage key must not traverse upwards",
                                     code="SOURCE_UNAVAILABLE", storage_key=key)
        root = self.root.resolve()
        target = (root / Path(*parts)).resolve()
        if target != root and root not in target.parents:
            raise StorageUnavailable("storage key escapes the storage root",
                                     code="SOURCE_UNAVAILABLE", storage_key=key)
        return target

    def put(self, *, key: str, content: bytes) -> str:
        target = self._target(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.mode)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return self.name

    def get(self, key: str) -> bytes:
        return self._target(key).read_bytes()


class AccessTokenSigner:
    """HMAC-SHA256 signer for temporary access tokens.

    The key comes from the environment (§40: secrets never live in source). With no key
    the signer refuses to operate, so a misconfigured deployment cannot hand out
    unsigned links — it fails closed instead.
    """

    ENV_VAR = "VILLA_EVIDENCE_TOKEN_KEY"
    MIN_KEY_BYTES = 32

    def __init__(self, key: Optional[str] = None) -> None:
        raw = key if key is not None else os.environ.get(self.ENV_VAR)
        if raw is None or not str(raw).strip():
            raise StorageUnavailable(
                "no evidence token key configured: access grants are unavailable",
                code="SOURCE_UNAVAILABLE",
                env_var=self.ENV_VAR,
            )
        self._key = str(raw).encode("utf-8")
        if len(self._key) < self.MIN_KEY_BYTES:
            raise StorageUnavailable(
                f"evidence token key must be at least {self.MIN_KEY_BYTES} bytes",
                code="SOURCE_UNAVAILABLE",
                length=len(self._key),
            )

    @staticmethod
    def _payload(agency_id: str, grant_id: str, evidence_id: str, purpose: str, expires_at: str) -> bytes:
        return canonical_json({
            "agency_id": agency_id, "grant_id": grant_id, "evidence_id": evidence_id,
            "purpose": purpose, "expires_at": expires_at,
        }).encode("utf-8")

    def issue(self, *, agency_id: str, grant_id: str, evidence_id: str,
              purpose: str, expires_at: str) -> str:
        body = self._payload(agency_id, grant_id, evidence_id, purpose, expires_at)
        signature = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        return f"{grant_id}.{signature}"

    def verify(self, token: str, *, agency_id: str, grant_id: str, evidence_id: str,
               purpose: str, expires_at: str) -> None:
        expected = self.issue(agency_id=agency_id, grant_id=grant_id, evidence_id=evidence_id,
                              purpose=purpose, expires_at=expires_at)
        if not hmac.compare_digest(expected, token):
            raise AccessDenied("access token signature does not verify", code="FORBIDDEN",
                               grant_id=grant_id)


class EvidenceService:
    """The single writer of ``evidence_objects``/``evidence_access_grants``/``evidence_events``."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = DEFAULT_AGENCY_ID,
        quarantine: Optional[StorageBackend] = None,
        storage: Optional[StorageBackend] = None,
        scanner: Optional[MalwareScanner] = None,
        signer: Optional[AccessTokenSigner] = None,
        allowed_content_types: Sequence[str] = ALLOWED_CONTENT_TYPES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        grant_ttl_seconds: int = 900,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self.quarantine = quarantine
        self.storage = storage
        self.scanner = scanner if scanner is not None else NoScanner()
        self.signer = signer
        self.allowed_content_types = tuple(allowed_content_types)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValidationFailed("max_bytes must be a positive integer",
                                   code="VALIDATION_ERROR", max_bytes=max_bytes)
        self.max_bytes = max_bytes
        if not isinstance(grant_ttl_seconds, int) or grant_ttl_seconds <= 0:
            raise ValidationFailed("grant_ttl_seconds must be positive",
                                   code="VALIDATION_ERROR", grant_ttl_seconds=grant_ttl_seconds)
        self.grant_ttl_seconds = grant_ttl_seconds

    # -- plumbing ---------------------------------------------------------- #
    def _stamp(self) -> str:
        return to_iso(self.clock.now().astimezone(UTC))

    def _row(self, evidence_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM evidence_objects WHERE evidence_id = ? AND agency_id = ?",
            (require_ref(evidence_id, "evidence_id"), self.agency_id),
        ).fetchone()
        if row is None:
            raise EvidenceNotFound(f"evidence object not found: {evidence_id}",
                                   code="RESOURCE_NOT_FOUND", evidence_id=evidence_id)
        return row

    def _event(self, evidence_id: Optional[str], kind: str, *, actor: str, actor_role: str,
               detail: Mapping[str, Any] | None = None) -> None:
        """Append one trail row. ``evidence_id`` is None only for a targetless denial."""
        self.conn.execute(
            "INSERT INTO evidence_events (event_id, agency_id, evidence_id, kind, actor,"
            " actor_role, detail, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.ids.new("evt"), self.agency_id, evidence_id, kind,
             require_text(actor, "actor", max_len=64), require_text(actor_role, "actor_role", max_len=64),
             canonical_json(dict(detail or {})), self._stamp()),
        )

    def _require_step(self, row: sqlite3.Row, expected_from: str, target: str) -> None:
        current = str(row["status"])
        if current != expected_from:
            raise InvalidEvidenceTransition(
                f"evidence is {current}; {target} may only be entered from {expected_from}",
                code="STATE_TRANSITION_NOT_ALLOWED",
                evidence_id=str(row["evidence_id"]),
                status=current,
                expected=expected_from,
                target=target,
            )

    # -- 1. upload → quarantine -------------------------------------------- #
    def upload(
        self,
        *,
        content: bytes,
        filename: Any,
        content_type: Any,
        kind: str,
        uploaded_by: Any,
        actor_role: Any = "reservation_agent",
        access_policy: str = "AGENCY_SCOPED",
    ) -> str:
        """Receive bytes into quarantine. Nothing is validated or trusted yet.

        The digest is computed here, from the bytes actually received: a caller-supplied
        hash is a claim, and the stored provenance must be a fact.
        """
        if not isinstance(content, (bytes, bytearray)):
            raise ValidationFailed("content must be bytes", code="VALIDATION_ERROR")
        raw = bytes(content)
        name = require_text(filename, "original_filename", max_len=255)
        media = require_text(content_type, "content_type", max_len=128).lower()
        kind_value = kind if kind in EVIDENCE_KINDS else None
        if kind_value is None:
            raise ValidationFailed(f"unknown evidence kind: {kind!r}",
                                   code="VALIDATION_ERROR", kind=kind, allowed=list(EVIDENCE_KINDS))
        if access_policy not in ACCESS_POLICIES:
            raise ValidationFailed(f"unknown access policy: {access_policy!r}",
                                   code="VALIDATION_ERROR", access_policy=access_policy)
        if self.quarantine is None:
            raise StorageUnavailable(
                "no quarantine storage configured: uploads are refused",
                code="SOURCE_UNAVAILABLE", filename=name,
            )
        evidence_id = self.ids.new("evid")
        quarantine_ref = f"quarantine/{evidence_id}"
        self.quarantine.put(key=quarantine_ref, content=raw)
        stamp = self._stamp()
        self.conn.execute(
            "INSERT INTO evidence_objects (evidence_id, agency_id, kind, status, access_policy,"
            " original_filename, content_type, byte_size, sha256, uploaded_by, quarantine_ref,"
            " version, created_at, updated_at)"
            " VALUES (?, ?, ?, 'QUARANTINED', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (evidence_id, self.agency_id, kind_value, access_policy, name, media, len(raw),
             hashlib.sha256(raw).hexdigest(), require_ref(uploaded_by, "uploaded_by", max_len=64),
             quarantine_ref, stamp, stamp),
        )
        self._event(evidence_id, "UPLOADED", actor=uploaded_by, actor_role=actor_role,
                    detail={"filename": name, "byte_size": len(raw), "content_type": media})
        return evidence_id

    # -- 2. type / size validation ---------------------------------------- #
    def validate(self, evidence_id: Any, *, actor: Any = "system:evidence",
                 actor_role: Any = "system_service") -> None:
        """Type, size and filename checks. A failure parks the file as REJECTED."""
        row = self._row(evidence_id)
        self._require_step(row, "QUARANTINED", "VALIDATED")
        reason = self._validation_reason(row)
        if reason is not None:
            self.conn.execute(
                "UPDATE evidence_objects SET status = 'REJECTED', rejection_reason = ?,"
                " updated_at = ? WHERE evidence_id = ? AND agency_id = ?",
                (reason, self._stamp(), str(row["evidence_id"]), self.agency_id),
            )
            self._event(str(row["evidence_id"]), "VALIDATION_REJECTED", actor=actor,
                        actor_role=actor_role, detail={"reason": reason})
            raise RejectedFile(f"file rejected: {reason}", code="FILE_REJECTED",
                               evidence_id=str(row["evidence_id"]), reason=reason)
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'VALIDATED', updated_at = ?"
            " WHERE evidence_id = ? AND agency_id = ?",
            (self._stamp(), str(row["evidence_id"]), self.agency_id),
        )
        self._event(str(row["evidence_id"]), "VALIDATED", actor=actor, actor_role=actor_role)

    def _validation_reason(self, row: sqlite3.Row) -> Optional[str]:
        name = str(row["original_filename"])
        if name != Path(name).name or "\x00" in name:
            return "filename contains a path or control character"
        if str(row["content_type"]) not in self.allowed_content_types:
            return f"content type is not accepted: {row['content_type']}"
        if int(row["byte_size"]) > self.max_bytes:
            return f"file exceeds the {self.max_bytes} byte limit"
        if int(row["byte_size"]) == 0:
            return "file is empty"
        expected = MAGIC_BYTES.get(str(row["content_type"]))
        if expected is not None:
            content = self._quarantined(row)
            if not any(content.startswith(prefix) for prefix in expected):
                return f"content does not match the declared type {row['content_type']}"
        return None

    def _quarantined(self, row: sqlite3.Row) -> bytes:
        if self.quarantine is None:
            raise StorageUnavailable("no quarantine storage configured", code="SOURCE_UNAVAILABLE")
        return self.quarantine.get(str(row["quarantine_ref"]))

    # -- 3. malware scan --------------------------------------------------- #
    def scan(self, evidence_id: Any, *, actor: Any = "system:evidence",
             actor_role: Any = "system_service") -> ScanResult:
        """Run the configured scanner. INFECTED parks the file; UNKNOWN is not CLEAN."""
        row = self._row(evidence_id)
        self._require_step(row, "VALIDATED", "SCANNED")
        evidence_key = str(row["evidence_id"])
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'SCAN_PENDING', updated_at = ?"
            " WHERE evidence_id = ? AND agency_id = ?",
            (self._stamp(), evidence_key, self.agency_id),
        )
        self._event(evidence_key, "SCAN_REQUESTED", actor=actor, actor_role=actor_role,
                    detail={"provider": getattr(self.scanner, "name", "")})
        result = self.scanner.scan(
            content=self._quarantined(row), content_type=str(row["content_type"]),
            filename=str(row["original_filename"]),
        )
        stamp = self._stamp()
        if result.verdict != "CLEAN":
            self.conn.execute(
                "UPDATE evidence_objects SET status = 'SCAN_FAILED', scan_provider = ?,"
                " scan_verdict = ?, scanned_at = ?, updated_at = ?"
                " WHERE evidence_id = ? AND agency_id = ?",
                (result.provider, result.verdict, stamp, stamp, evidence_key, self.agency_id),
            )
            self._event(evidence_key, "SCAN_FAILED", actor=actor, actor_role=actor_role,
                        detail={"provider": result.provider, "verdict": result.verdict,
                                "detail": result.detail})
            raise InfectedFile(
                f"scanner returned {result.verdict}: the file is not promoted",
                code="FILE_INFECTED", evidence_id=evidence_key, verdict=result.verdict,
            )
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'SCANNED', scan_provider = ?, scan_verdict = ?,"
            " scanned_at = ?, updated_at = ? WHERE evidence_id = ? AND agency_id = ?",
            (result.provider, result.verdict, stamp, stamp, evidence_key, self.agency_id),
        )
        self._event(evidence_key, "SCAN_PASSED", actor=actor, actor_role=actor_role,
                    detail={"provider": result.provider})
        return result

    # -- 4. classification ------------------------------------------------- #
    def classify(self, evidence_id: Any, *, classification: str, access_policy: Optional[str] = None,
                 actor: Any = "system:evidence", actor_role: Any = "system_service") -> None:
        """Assign the data classification (§19). L4 must use the restricted vault policy."""
        row = self._row(evidence_id)
        self._require_step(row, "SCANNED", "CLASSIFIED")
        if classification not in CLASSIFICATIONS:
            raise ValidationFailed(f"unknown classification: {classification!r}",
                                   code="VALIDATION_ERROR", classification=classification,
                                   allowed=list(CLASSIFICATIONS))
        policy = access_policy or ("RESTRICTED_VAULT" if classification == "L4" else "AGENCY_SCOPED")
        if policy not in ACCESS_POLICIES:
            raise ValidationFailed(f"unknown access policy: {policy!r}",
                                   code="VALIDATION_ERROR", access_policy=policy)
        if classification == "L4" and policy != "RESTRICTED_VAULT":
            raise ValidationFailed(
                "L4 evidence must use the RESTRICTED_VAULT access policy",
                code="VALIDATION_ERROR", classification=classification, access_policy=policy,
            )
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'CLASSIFIED', classification = ?,"
            " access_policy = ?, updated_at = ? WHERE evidence_id = ? AND agency_id = ?",
            (classification, policy, self._stamp(), str(row["evidence_id"]), self.agency_id),
        )
        self._event(str(row["evidence_id"]), "CLASSIFIED", actor=actor, actor_role=actor_role,
                    detail={"classification": classification, "access_policy": policy})

    # -- 5. safe storage --------------------------------------------------- #
    def store(self, evidence_id: Any, *, actor: Any = "system:evidence",
              actor_role: Any = "system_service") -> str:
        """Promote a scanned, classified file out of quarantine into safe storage."""
        row = self._row(evidence_id)
        self._require_step(row, "CLASSIFIED", "STORED")
        if self.storage is None:
            raise StorageUnavailable("no safe storage configured: promotion is refused",
                                     code="SOURCE_UNAVAILABLE", evidence_id=str(row["evidence_id"]))
        evidence_key = str(row["evidence_id"])
        storage_key = f"evidence/{self.agency_id}/{evidence_key}"
        backend = self.storage.put(key=storage_key, content=self._quarantined(row))
        stamp = self._stamp()
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'STORED', storage_backend = ?, storage_key = ?,"
            " updated_at = ? WHERE evidence_id = ? AND agency_id = ?",
            (backend, storage_key, stamp, evidence_key, self.agency_id),
        )
        self._event(evidence_key, "STORED", actor=actor, actor_role=actor_role,
                    detail={"storage_backend": backend, "storage_key": storage_key})
        return storage_key

    # -- 6. temporary authorized access ------------------------------------ #
    def grant_access(self, evidence_id: Any, *, granted_to: Any, purpose: Any,
                     ttl_seconds: Optional[int] = None, single_use: bool = True,
                     actor: Any = "system:evidence", actor_role: Any = "system_service") -> str:
        """Issue a temporary, purpose-bound access token. Shown once, never stored."""
        row = self._row(evidence_id)
        if str(row["status"]) != "STORED":
            raise AccessDenied("only stored evidence may be shared",
                               code="FORBIDDEN", evidence_id=str(row["evidence_id"]),
                               status=str(row["status"]))
        if self.signer is None:
            raise StorageUnavailable(
                "no access token signer configured: grants are refused",
                code="SOURCE_UNAVAILABLE", evidence_id=str(row["evidence_id"]),
            )
        who = require_ref(granted_to, "granted_to", max_len=64)
        why = require_text(purpose, "purpose", max_len=200)
        ttl = self.grant_ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise ValidationFailed("ttl_seconds must be a positive integer",
                                   code="VALIDATION_ERROR", ttl_seconds=ttl)
        from datetime import timedelta

        issued = self.clock.now().astimezone(UTC)
        expires = issued + timedelta(seconds=ttl)
        grant_id = self.ids.new("grant")
        evidence_key = str(row["evidence_id"])
        token = self.signer.issue(agency_id=self.agency_id, grant_id=grant_id,
                                  evidence_id=evidence_key, purpose=why,
                                  expires_at=to_iso(expires))
        self.conn.execute(
            "INSERT INTO evidence_access_grants (grant_id, agency_id, evidence_id, granted_to,"
            " purpose, issued_at, expires_at, single_use, token_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (grant_id, self.agency_id, evidence_key, who, why, to_iso(issued), to_iso(expires),
             1 if single_use else 0, hashlib.sha256(token.encode("utf-8")).hexdigest()),
        )
        self._event(evidence_key, "ACCESS_GRANTED", actor=actor, actor_role=actor_role,
                    detail={"grant_id": grant_id, "granted_to": who, "purpose": why,
                            "expires_at": to_iso(expires), "single_use": bool(single_use)})
        return token

    def authorize(self, token: Any, *, at: Any = None, actor: Any = "system:evidence",
                  actor_role: Any = "system_service") -> str:
        """Resolve an access token to its evidence id, or refuse. Never logs the token."""
        raw = require_text(token, "token", max_len=512)
        grant_id = raw.split(".", 1)[0]
        row = self.conn.execute(
            "SELECT * FROM evidence_access_grants WHERE grant_id = ? AND agency_id = ?",
            (require_ref(grant_id, "grant_id"), self.agency_id),
        ).fetchone()
        moment = (self.clock.now() if at is None else parse_iso(at)).astimezone(UTC)
        if row is None:
            self._event(None, "ACCESS_DENIED", actor=actor, actor_role=actor_role,
                        detail={"reason": "unknown grant", "grant_id": grant_id})
            raise AccessDenied("access grant not found", code="FORBIDDEN", grant_id=grant_id)
        if row["revoked_at"] is not None:
            self._deny(row, actor, actor_role, "grant was revoked", moment)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(row["token_sha256"]):
            self._deny(row, actor, actor_role, "token digest does not match", moment)
        if parse_iso(str(row["expires_at"])) <= moment:
            self._deny(row, actor, actor_role, "grant expired", moment)
        if int(row["single_use"]) == 1 and row["used_at"] is not None:
            self._deny(row, actor, actor_role, "grant already used", moment)
        if self.signer is not None:
            try:
                self.signer.verify(raw, agency_id=self.agency_id, grant_id=str(row["grant_id"]),
                                   evidence_id=str(row["evidence_id"]), purpose=str(row["purpose"]),
                                   expires_at=str(row["expires_at"]))
            except AccessDenied:
                self._deny(row, actor, actor_role, "signature does not verify", moment)
        if int(row["single_use"]) == 1:
            self.conn.execute(
                "UPDATE evidence_access_grants SET used_at = ? WHERE grant_id = ? AND agency_id = ?",
                (to_iso(moment), str(row["grant_id"]), self.agency_id),
            )
        self._event(str(row["evidence_id"]), "ACCESS_USED", actor=actor, actor_role=actor_role,
                    detail={"grant_id": str(row["grant_id"]), "purpose": str(row["purpose"])})
        return str(row["evidence_id"])

    def _deny(self, row: Mapping[str, Any], actor: str, actor_role: str, reason: str,
              moment: Any) -> None:
        self._event(str(row["evidence_id"]), "ACCESS_DENIED", actor=actor, actor_role=actor_role,
                    detail={"grant_id": str(row["grant_id"]), "reason": reason})
        raise AccessDenied(f"access denied: {reason}", code="FORBIDDEN",
                           grant_id=str(row["grant_id"]), reason=reason,
                           evidence_id=str(row["evidence_id"]))

    def revoke_access(self, grant_id: Any, *, reason: Any, actor: Any = "system:evidence",
                      actor_role: Any = "system_service") -> None:
        why = require_text(reason, "reason", max_len=200)
        cursor = self.conn.execute(
            "UPDATE evidence_access_grants SET revoked_at = ?, revoked_reason = ?"
            " WHERE grant_id = ? AND agency_id = ? AND revoked_at IS NULL",
            (self._stamp(), why, require_ref(grant_id, "grant_id"), self.agency_id),
        )
        if cursor.rowcount != 1:
            raise AccessDenied("grant not found or already revoked", code="FORBIDDEN",
                               grant_id=grant_id)
        row = self.conn.execute(
            "SELECT evidence_id FROM evidence_access_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        self._event(str(row["evidence_id"]), "ACCESS_REVOKED", actor=actor, actor_role=actor_role,
                    detail={"grant_id": str(grant_id), "reason": why})

    # -- deletion is a status, never a DELETE (§39) ------------------------ #
    def soft_delete(self, evidence_id: Any, *, reason: Any, actor: Any, actor_role: Any) -> None:
        """Mark an evidence object deleted. The row and its history stay."""
        row = self._row(evidence_id)
        why = require_text(reason, "reason", max_len=200)
        self.conn.execute(
            "UPDATE evidence_objects SET status = 'DELETED', rejection_reason = ?, updated_at = ?"
            " WHERE evidence_id = ? AND agency_id = ?",
            (why, self._stamp(), str(row["evidence_id"]), self.agency_id),
        )
        self._event(str(row["evidence_id"]), "SOFT_DELETED", actor=actor, actor_role=actor_role,
                    detail={"reason": why})

    # -- reads ------------------------------------------------------------- #
    def get(self, evidence_id: Any) -> Mapping[str, Any]:
        return dict(self._row(evidence_id))

    def events(self, evidence_id: Any) -> tuple[Mapping[str, Any], ...]:
        self._row(evidence_id)
        rows = self.conn.execute(
            "SELECT * FROM evidence_events WHERE evidence_id = ? AND agency_id = ?"
            " ORDER BY occurred_at, event_id",
            (require_ref(evidence_id, "evidence_id"), self.agency_id),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def denials(self) -> tuple[Mapping[str, Any], ...]:
        """Every refused access attempt, including ones with no valid target.

        A denial against an unknown or forged grant is the signal an operator actually
        needs; it is recorded with ``evidence_id IS NULL`` and is readable here.
        """
        rows = self.conn.execute(
            "SELECT * FROM evidence_events WHERE agency_id = ? AND kind = 'ACCESS_DENIED'"
            " ORDER BY occurred_at, event_id",
            (self.agency_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def open_grants(self, evidence_id: Any) -> tuple[Mapping[str, Any], ...]:
        self._row(evidence_id)
        rows = self.conn.execute(
            "SELECT * FROM evidence_access_grants WHERE evidence_id = ? AND agency_id = ?"
            " AND revoked_at IS NULL AND used_at IS NULL ORDER BY expires_at",
            (require_ref(evidence_id, "evidence_id"), self.agency_id),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def stored_evidence(self, evidence_id: Any) -> Mapping[str, Any]:
        """The evidence row, but only when it is safely stored — the provenance gate.

        A commercial fact may cite only evidence that completed the pipeline; anything
        still in quarantine is not yet a source anybody may rely on (§12).
        """
        row = self._row(evidence_id)
        if str(row["status"]) != "STORED":
            raise AccessDenied(
                "evidence has not completed the pipeline and cannot be cited as a source",
                code="VERIFICATION_REQUIRED",
                evidence_id=str(row["evidence_id"]), status=str(row["status"]),
            )
        return dict(row)

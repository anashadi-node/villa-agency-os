"""Kanal masuk/keluar: from channel traffic to cases, and from an approved draft to
the wire.

The connector layer is the only place where the outside world meets the kernel, so
it is written as a *border control* rather than a convenience wrapper:

* **Everything inbound is data, never an instruction.** A message body is stored
  through :meth:`cases.CaseService.record_inbound`/:meth:`~cases.CaseService.open_case`
  as an untrusted row (``untrusted = 1``); this module has no code path that reads
  a body and changes a status, a booking or a price as a result. Instruction-like
  text is *reported* (:func:`scan_untrusted`) and never obeyed.
* **Idempotent by external reference.** Every inbound message carries a stable,
  pseudonymised ``external_ref`` (``channel + external_ref`` is the identity of the
  message). Ingesting the same message twice leaves **one** ``case_messages`` row;
  the same reference with different content is refused as
  ``EXTERNAL_REF_CONFLICT`` instead of being silently dropped.
* **Classification before storage.** A message body defaults to ``P3`` — the class
  whose body is *never stored* (only its SHA-256 is kept, per
  ``ck_case_messages_p3p4_not_stored``). A caller may declare a *looser* class, and
  then only if :func:`gates.gate_outbound` allows it for the ``store`` destination;
  a ``P4`` body is refused outright and a ``P0``/``P1`` body that actually carries
  direct identifiers is refused as ``MISCLASSIFIED_CONTENT`` (it is ``P2`` content).
  The class may never be lowered silently — there is no code path here that turns a
  stricter declaration into a looser one.
* **Headers with PII are stored redacted** (:func:`primitives.redact`) and secrets
  live only in an argument or an environment variable — never in the database, never
  in an error message, never in a log line.
* **Outbound only leaves after a human said yes.** :class:`OutboundQueue` refuses
  any draft that is not ``APPROVED`` *and* ``GROUNDED`` *and* backed by an
  ``APPROVED`` ``message_send`` approval, writes one deterministic file per draft,
  and records the transport's ``external_ref`` only after the transport succeeded.
  A transport that raises leaves the draft exactly as it was: an unsent draft.

Transport contract
------------------
A transport is anything with ``channel`` and ``fetch() -> tuple[InboundMessage, ...]``
(:class:`Transport`). Three implementations ship here, one per inbound channel:

``EmailFileConnector``      scans a directory of ``.eml``/``.txt`` files, one file =
                            one message, headers ``From``/``Subject``/``Date``.
``WebsiteFormConnector``    parses one website form payload
                            (``name``, ``email``, ``message``, optional ``dates``).
``WhatsAppWebhookConnector`` verifies an HMAC-SHA256 signature over the **raw**
                            request body (:func:`hmac.compare_digest`) before it
                            looks at the payload, and refuses a missing/empty/
                            wrong signature.

``src/messages.py`` does not exist yet: the outbound seam is the
``on_approved_draft(artifact)`` hook (:func:`default_approved_hook`), called after a
successful delivery. This module never inserts into ``case_messages`` by hand —
case and message rows are written by :class:`cases.CaseService`, and an outbound
draft is a ``message_drafts`` row that some other component owns.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Protocol, Sequence

import cases as C
import gates as G
import intake as I
from primitives import (
    UTC,
    DomainError,
    FrozenClock,
    IdFactory,
    SystemClock,
    canonical_json,
    parse_iso,
    redact,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    # errors
    "ConnectorError",
    "CODE_CONNECTOR_ERROR",
    "CODE_INVALID_MESSAGE",
    "CODE_INVALID_PAYLOAD",
    "CODE_INVALID_CONTENT_CLASS",
    "CODE_EXTERNAL_REF_CONFLICT",
    "CODE_SECRET_MISSING",
    "CODE_SIGNATURE_MISSING",
    "CODE_SIGNATURE_INVALID",
    "CODE_UNSAFE_FILE",
    "CODE_MESSAGE_TOO_LARGE",
    "CODE_REJECTED_FILES",
    "CODE_DRAFT_NOT_FOUND",
    "CODE_DRAFT_NOT_APPROVED",
    "CODE_DRAFT_NOT_GROUNDED",
    "CODE_DRAFT_BODY_TAMPERED",
    "CODE_APPROVAL_NOT_APPROVED",
    "CODE_DRAFT_ALREADY_SENT",
    "CODE_SENT_ARTIFACT_MISSING",
    "CODE_TRANSPORT_FAILED",
    "CODE_UNKNOWN_DESTINATION",
    "CODE_DB_MISSING",
    # vocabulary
    "MODULE_VERSION",
    "DEFAULT_INBOUND_CONTENT_CLASS",
    "STORABLE_CONTENT_CLASSES",
    "SIGNATURE_HEADER",
    "WHATSAPP_SECRET_ENV",
    "DESTINATION_BY_CHANNEL",
    "INJECTION_MARKERS",
    "PLACEHOLDER_SECRETS",
    "MAX_MESSAGE_BYTES",
    "MAX_PREVIEW_CHARS",
    "DEFAULT_OUTBOX_ACTOR",
    "IDEMPOTENCY_PREFIX",
    "EXTERNAL_REF_PREFIX",
    # records
    "InboundMessage",
    "ScanResult",
    "IntakeResult",
    "Draft",
    "DeliveryResult",
    "OutboundArtifact",
    # transports
    "Transport",
    "EmailFileConnector",
    "WebsiteFormConnector",
    "WhatsAppWebhookConnector",
    "OutboundTransport",
    "FileOutboundTransport",
    # helpers
    "scan_untrusted",
    "load_env_secret",
    "sign_webhook_body",
    "verify_webhook_signature",
    "resolve_inbound_content_class",
    "identifier_kinds",
    "parse_message_file",
    "parse_email_bytes",
    "parse_website_payload",
    "parse_whatsapp_payload",
    "propose_candidates",
    "propose_intake",
    "intake_message",
    "find_message_by_external_ref",
    "read_draft",
    "default_approved_hook",
    "OutboundQueue",
    "main",
]

#: Bumped whenever the wire format of an outbound artifact changes.
MODULE_VERSION = "connectors.v1"

#: The class a body inherits when the caller declares nothing. ``P3`` is the
#: "content is not stored" tier (``ck_case_messages_p3p4_not_stored``), so the
#: default is *not* to keep guest text.
DEFAULT_INBOUND_CONTENT_CLASS = "P3"

#: Classes whose body may be stored (``P2`` stored redacted, ``P0``/``P1`` raw).
STORABLE_CONTENT_CLASSES: tuple[str, ...] = ("P0", "P1", "P2")

#: Header carrying the HMAC-SHA256 signature of a webhook request (Meta spelling).
SIGNATURE_HEADER = "x-hub-signature-256"

#: Environment variable holding the webhook signing secret (.env.template).
WHATSAPP_SECRET_ENV = "WEBHOOK_SIGNING_SECRET"

#: Which outbound destination a channel's traffic is classified against. The
#: ``message_drafts.channel`` vocabulary and the ``gates`` destinations differ;
#: this is the single place that maps one to the other.
DESTINATION_BY_CHANNEL: Mapping[str, str] = {
    "email": G.DESTINATION_EMAIL,
    "website": G.DESTINATION_CHAT,
    "whatsapp_business": G.DESTINATION_CHAT,
    "dashboard": G.DESTINATION_CHAT,
    "phone": G.DESTINATION_CHAT,
}

#: Instruction-like shapes found in untrusted text. Matching one of these changes
#: **nothing**: the names are reported so a human can see that the message tried
#: to talk to the system, and the message is stored as data either way.
INJECTION_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_prefix", re.compile(r"(?im)^[ \t>]*(system|assistant|developer|tool)\s*:")),
    (
        "instruction_override",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|lupakan|abaikan)\b[^.\n]{0,40}"
            r"\b(previous|above|earlier|prior|instruksi|perintah)\b"
        ),
    ),
    (
        "status_mutation",
        re.compile(
            r"(?i)\b(ubah|rubah|ganti|set|update|change|approve|setujui)\b[^.\n]{0,30}"
            r"\b(status|booking|pesanan|harga|price|invoice|draft)\b"
        ),
    ),
    (
        "tool_invocation",
        re.compile(r"(?i)\b(call|invoke|execute|run|jalankan)\b[^.\n]{0,20}\b(tool|function|sql|shell|command|ssh)\b"),
    ),
    (
        "exfiltration",
        re.compile(
            r"(?i)\b(send|kirim|forward|cc|bcc|email)\b[^.\n]{0,40}"
            r"\b(credential|password|token|secret|otp|kartu|card)\b"
        ),
    ),
)

#: Values that carry no secret at all, however much they look like one. Compared
#: case-insensitively against the stripped value.
PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "changeme",
        "change_me",
        "change-me",
        "redacted",
        "placeholder",
        "example",
        "dummy",
        "sample",
        "secret",
        "value",
        "none",
        "null",
        "nil",
        "todo",
        "tbd",
        "test",
        "testing",
        "your_key_here",
        "your-key-here",
        "your_token_here",
        "xxx",
        "xxxx",
        "xxxxxx",
        "xxxxxxx",
        "xxxxxxxx",
        "...",
    }
)

#: A single message file may not exceed this size (1 MiB). Anything larger is a
#: data error, not mail.
MAX_MESSAGE_BYTES = 1024 * 1024

#: How much of a redacted body the *plan* (dry run) may print.
MAX_PREVIEW_CHARS = 120

#: Who the connector claims to be when it records a delivery.
DEFAULT_OUTBOX_ACTOR = "ops_connector"

#: Deterministic idempotency-key prefix for an ingested message.
IDEMPOTENCY_PREFIX = "ingest"

#: Deterministic external-ref prefix for a delivered draft.
EXTERNAL_REF_PREFIX = "out"

#: Sender pseudonym prefix (never the guest's address or number).
GUEST_REF_PREFIX = "guest"

#: The extractor name recorded on an intake proposal this module triggers.
EXTRACTOR_NAME = "connectors.inbound.v1"


# --------------------------------------------------------------------------- #
# Errors — one class, explicit UPPERCASE codes (never parse the message)
# --------------------------------------------------------------------------- #
CODE_CONNECTOR_ERROR = "CONNECTOR_ERROR"
CODE_INVALID_MESSAGE = "INVALID_MESSAGE"
CODE_INVALID_PAYLOAD = "INVALID_PAYLOAD"
CODE_INVALID_CONTENT_CLASS = "INVALID_CONTENT_CLASS"
CODE_EXTERNAL_REF_CONFLICT = "EXTERNAL_REF_CONFLICT"
CODE_SECRET_MISSING = "SECRET_MISSING"
CODE_SIGNATURE_MISSING = "SIGNATURE_MISSING"
CODE_SIGNATURE_INVALID = "SIGNATURE_INVALID"
CODE_UNSAFE_FILE = "UNSAFE_FILE"
CODE_MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"
CODE_REJECTED_FILES = "REJECTED_FILES"
CODE_DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
CODE_DRAFT_NOT_APPROVED = "DRAFT_NOT_APPROVED"
CODE_DRAFT_NOT_GROUNDED = "DRAFT_NOT_GROUNDED"
CODE_DRAFT_BODY_TAMPERED = "DRAFT_BODY_TAMPERED"
CODE_APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
CODE_DRAFT_ALREADY_SENT = "DRAFT_ALREADY_SENT"
CODE_SENT_ARTIFACT_MISSING = "SENT_ARTIFACT_MISSING"
CODE_TRANSPORT_FAILED = "TRANSPORT_FAILED"
CODE_UNKNOWN_DESTINATION = "UNKNOWN_DESTINATION"
CODE_DB_MISSING = "DB_MISSING"


class ConnectorError(DomainError):
    """A deliberate connector failure. ``code`` is the contract; text is for humans.

    ``details`` never carries a secret and never carries a raw body: identifiers,
    counts, field names and redacted values only.
    """

    code = CODE_CONNECTOR_ERROR


def _fail(code: str, message: str, **details: Any) -> ConnectorError:
    return ConnectorError(message, code=code, **details)


# --------------------------------------------------------------------------- #
# Untrusted text: reported, never obeyed
# --------------------------------------------------------------------------- #
def scan_untrusted(text: Any) -> tuple[str, ...]:
    """Names of the instruction-like shapes ``text`` contains (sorted, unique).

    This is a *report*, not a filter and not an interpreter: the return value
    feeds the audit/plan output so a human can see that a message tried to issue
    instructions. Nothing in this module branches on the result.
    """
    if not isinstance(text, str) or not text:
        return ()
    found = {name for name, pattern in INJECTION_MARKERS if pattern.search(text)}
    return tuple(sorted(found))


def identifier_kinds(value: Any) -> tuple[str, ...]:
    """Direct-identifier kinds (``email``, ``phone``, ``nik``, ``card``) in ``value``.

    Uses the repository's own detector (:data:`gates.IDENTIFIER_PATTERNS`) so the
    connector and the classification gate cannot disagree about what a direct
    identifier is.
    """
    if value is None or isinstance(value, bool):
        return ()
    text = value if isinstance(value, str) else canonical_json(value)
    found = {name for name, pattern in G.IDENTIFIER_PATTERNS if pattern.search(text)}
    return tuple(sorted(found))


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def load_env_secret(
    name: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum_length: int = 8,
    required: bool = True,
) -> Optional[str]:
    """Return the secret held in environment variable ``name``, or refuse.

    Refusals (``SECRET_MISSING``): the variable is absent, blank/whitespace-only,
    shorter than ``minimum_length``, or one of the :data:`PLACEHOLDER_SECRETS`
    shapes (``changeme``, ``<...>``, ``{{...}}``, an ``xxx`` mask, a name that
    embeds ``placeholder``/``your_key_here``). A secret is never echoed back in the
    error, never logged, and never stored in the database — only *used*.

    ``env`` defaults to :data:`os.environ`; tests pass an explicit mapping so the
    result never depends on the machine running the suite. With
    ``required=False`` a missing/placeholder value returns ``None`` instead of
    raising (for optional integrations) — it never returns the placeholder.
    """
    if not isinstance(name, str) or not name.strip():
        raise _fail(CODE_SECRET_MISSING, "nama variabel secret wajib diisi", field="name")
    variable = name.strip()
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(variable)
    if raw is None or not str(raw).strip():
        if not required:
            return None
        raise _fail(
            CODE_SECRET_MISSING,
            f"secret {variable} tidak ada atau kosong (set lewat env, jangan hardcode)",
            secret_name=variable,
            reason="missing_or_empty",
        )
    value = str(raw).strip()
    lowered = value.lower()
    masked = set(value) <= {"x", "X", "*", "-", "_", ".", " "}
    templated = (value.startswith("<") and value.endswith(">")) or (
        value.startswith("{{") and value.endswith("}}")
    )
    looks_placeholder = (
        lowered in PLACEHOLDER_SECRETS
        or masked
        or templated
        or "placeholder" in lowered
        or "your_key_here" in lowered
        or "your-key-here" in lowered
    )
    if looks_placeholder:
        if not required:
            return None
        raise _fail(
            CODE_SECRET_MISSING,
            f"secret {variable} masih placeholder, bukan nilai nyata",
            secret_name=variable,
            reason="placeholder",
        )
    if len(value) < minimum_length:
        if not required:
            return None
        raise _fail(
            CODE_SECRET_MISSING,
            f"secret {variable} terlalu pendek (minimal {minimum_length} karakter)",
            secret_name=variable,
            reason="too_short",
            length=len(value),
        )
    return value


# --------------------------------------------------------------------------- #
# Webhook signature (HMAC-SHA256 over the raw body)
# --------------------------------------------------------------------------- #
def _as_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    raise _fail(
        CODE_INVALID_PAYLOAD,
        f"body webhook harus bytes/str, dapat {type(body).__name__}",
        got=type(body).__name__,
    )


def sign_webhook_body(secret: str, body: Any, *, algorithm: str = "sha256") -> str:
    """``sha256=<hex>`` for ``body`` — the value a sender must put in the header."""
    if not secret or not str(secret).strip():
        raise _fail(CODE_SECRET_MISSING, "secret tanda tangan kosong", reason="missing_or_empty")
    digest = hmac.new(str(secret).encode("utf-8"), _as_bytes(body), hashlib.sha256).hexdigest()
    return f"{algorithm}={digest}"


def verify_webhook_signature(
    secret: Optional[str],
    body: Any,
    signature: Optional[str],
    *,
    algorithm: str = "sha256",
    header: str = SIGNATURE_HEADER,
) -> None:
    """Raise unless ``signature`` is the HMAC-SHA256 of the **raw** ``body``.

    Order of refusals: no secret -> ``SECRET_MISSING``; no/blank signature ->
    ``SIGNATURE_MISSING``; wrong algorithm prefix, non-hex or mismatching digest ->
    ``SIGNATURE_INVALID``. The comparison is :func:`hmac.compare_digest`, so a
    near-miss signature cannot be found by timing. The secret and the provided
    signature never appear in the error details.
    """
    if secret is None or not str(secret).strip():
        raise _fail(CODE_SECRET_MISSING, "secret webhook tidak diberikan", reason="missing_or_empty")
    if signature is None or not str(signature).strip():
        raise _fail(
            CODE_SIGNATURE_MISSING,
            f"header {header} tidak ada/kosong: webhook tanpa tanda tangan ditolak",
            header=header,
        )
    raw = str(signature).strip()
    if "=" in raw:
        prefix, _, digest_part = raw.partition("=")
        if prefix.strip().lower() != algorithm.lower():
            raise _fail(
                CODE_SIGNATURE_INVALID,
                f"algoritma tanda tangan {prefix.strip()!r} tidak didukung",
                header=header,
                algorithm=prefix.strip(),
            )
    else:
        digest_part = raw
    candidate = digest_part.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", candidate):
        raise _fail(
            CODE_SIGNATURE_INVALID,
            f"tanda tangan di {header} bukan hex sha256 yang valid",
            header=header,
            length=len(candidate),
        )
    expected = hmac.new(str(secret).encode("utf-8"), _as_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, candidate):
        raise _fail(
            CODE_SIGNATURE_INVALID,
            f"tanda tangan HMAC-SHA256 di {header} tidak cocok dengan body mentah",
            header=header,
        )


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def _require_content_class(value: Any) -> str:
    if not isinstance(value, str) or value not in C.CONTENT_CLASSES:
        raise _fail(
            CODE_INVALID_CONTENT_CLASS,
            "content_class tidak dikenal: " + str(value),
            content_class=str(value),
            allowed=list(C.CONTENT_CLASSES),
        )
    return value


def resolve_inbound_content_class(body: str, declared: Optional[str] = None) -> str:
    """The class a body is stored under: the declaration, or ``P3``.

    The declared class may be *looser* than the default (``P0``/``P1``/``P2``) but
    only if :func:`gates.gate_outbound` allows it for the ``store`` destination.
    Fail closed:

    * an unknown class -> ``INVALID_CONTENT_CLASS``;
    * ``P4`` -> ``P4_PROHIBITED`` (never stored, never sent);
    * ``P0``/``P1`` that actually carries an identifier -> ``MISCLASSIFIED_CONTENT``
      (content with a direct identifier is ``P2`` at minimum — storing it raw is the
      leak the classification exists to prevent).
    """
    cls = DEFAULT_INBOUND_CONTENT_CLASS if declared is None else _require_content_class(declared)
    decision = G.gate_outbound(
        [G.ClassifiedField(name="body", classification=cls, value=body)],
        destination=G.DESTINATION_STORE,
    )
    if not decision.allowed:
        raise _fail(
            decision.code,
            f"body ditolak untuk disimpan sebagai {cls}: {decision.message}",
            content_class=cls,
            destination=G.DESTINATION_STORE,
        )
    if cls in ("P0", "P1"):
        kinds = identifier_kinds(body)
        if kinds:
            raise _fail(
                G.CODE_MISCLASSIFIED_CONTENT,
                f"body dideklarasikan {cls} tetapi membawa identifier {list(kinds)}: "
                "deklarasikan P2 supaya disimpan teredaksi, atau jangan simpan isinya",
                content_class=cls,
                identifiers=list(kinds),
            )
    return cls


def _as_iso(value: Any, field_name: str) -> str:
    if isinstance(value, datetime):
        return to_iso(value)
    try:
        return to_iso(parse_iso(str(value)))
    except DomainError as exc:
        raise _fail(
            CODE_INVALID_MESSAGE,
            f"{field_name} bukan timestamp ISO-8601: {value!r}",
            field=field_name,
        ) from exc


@dataclass(frozen=True)
class InboundMessage:
    """One inbound message, already normalised into the kernel's vocabulary.

    ``body`` is the raw text **as received** — it is data. It is hashed exactly as
    :mod:`cases` hashes it (``sha256`` of the received text), so the stored
    ``body_sha256`` can be checked against it, and it is *not* echoed by
    :meth:`plan`.
    """

    channel: str
    external_ref: str
    body: str
    sender_ref: Optional[str] = None
    subject: Optional[str] = None
    received_at: Optional[str] = None
    content_class: str = DEFAULT_INBOUND_CONTENT_CLASS
    attachment_count: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)
    source: str = ""
    declared_fields: tuple[I.ProposedField, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.channel, str) or self.channel not in C.CASE_CHANNELS:
            raise _fail(
                CODE_INVALID_MESSAGE,
                "channel tidak dikenal: " + str(self.channel),
                channel=str(self.channel),
                allowed=list(C.CASE_CHANNELS),
            )
        object.__setattr__(self, "external_ref", require_ref(self.external_ref, "external_ref"))
        if self.sender_ref is not None:
            object.__setattr__(self, "sender_ref", require_ref(self.sender_ref, "sender_ref"))
        _require_content_class(self.content_class)
        if not isinstance(self.body, str) or not self.body.strip():
            raise _fail(CODE_INVALID_MESSAGE, "body pesan kosong", channel=self.channel)
        try:
            require_text(self.body, "body", max_len=C.MAX_BODY_CHARS)
        except DomainError as exc:
            raise _fail(CODE_INVALID_MESSAGE, f"body pesan tidak valid: {exc.message}") from exc
        if self.received_at is not None:
            object.__setattr__(self, "received_at", _as_iso(self.received_at, "received_at"))
        if isinstance(self.attachment_count, bool) or not isinstance(self.attachment_count, int):
            raise _fail(CODE_INVALID_MESSAGE, "attachment_count harus integer")
        if self.attachment_count < 0:
            raise _fail(CODE_INVALID_MESSAGE, "attachment_count tidak boleh negatif")
        if self.subject is not None:
            if not isinstance(self.subject, str):
                raise _fail(CODE_INVALID_MESSAGE, "subject harus teks")
            # A subject is a header: it may carry PII, so the stored copy is redacted.
            object.__setattr__(self, "subject", redact(self.subject))
        object.__setattr__(self, "headers", {str(k): redact(v) for k, v in dict(self.headers).items()})
        object.__setattr__(self, "declared_fields", tuple(self.declared_fields))

    # -- derived ----------------------------------------------------------- #
    @property
    def body_sha256(self) -> str:
        """Digest of the body exactly as :mod:`cases` computes it."""
        return sha256_hex(self.body)

    @property
    def guest_ref(self) -> str:
        """Stable, PII-free guest reference (threads from one sender share a case)."""
        seed = self.sender_ref if self.sender_ref else f"{self.channel}:{self.external_ref}"
        return f"{GUEST_REF_PREFIX}_{sha256_hex(self.channel, seed)[:16]}"

    @property
    def idempotency_key(self) -> str:
        """Deterministic key: the same ``(channel, external_ref)`` never writes twice."""
        return f"{IDEMPOTENCY_PREFIX}_{sha256_hex(self.channel, self.external_ref)[:24]}"

    @property
    def injection_flags(self) -> tuple[str, ...]:
        """Instruction-like shapes seen in the body — reported, never obeyed."""
        return scan_untrusted(self.body)

    @property
    def body_preview(self) -> str:
        """Redacted, truncated preview for a plan/dry run (never the raw body)."""
        return redact(self.body)[:MAX_PREVIEW_CHARS]

    def plan(self) -> dict[str, Any]:
        """A data-only description of this message. Contains no raw body/headers."""
        return {
            "channel": self.channel,
            "external_ref": self.external_ref,
            "idempotency_key": self.idempotency_key,
            "sender_ref": self.sender_ref,
            "guest_ref": self.guest_ref,
            "subject_redacted": self.subject,
            "received_at": self.received_at,
            "content_class": self.content_class,
            "attachment_count": self.attachment_count,
            "body_sha256": self.body_sha256,
            "body_preview_redacted": self.body_preview,
            "body_truncated": len(redact(self.body)) > MAX_PREVIEW_CHARS,
            "source": self.source,
            "injection_flags": list(self.injection_flags),
            "declared_fields": [item.name for item in self.declared_fields],
        }


@dataclass(frozen=True)
class ScanResult:
    """Outcome for one candidate file: a message, or a refusal with a code."""

    source: str
    message: Optional[InboundMessage] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.message is not None


@dataclass(frozen=True)
class IntakeResult:
    """What one ingest did (or deliberately did not do)."""

    case_id: str
    case_message_id: str
    case_status: str
    channel: str
    external_ref: str
    content_class: str
    duplicate: bool
    created_case: bool
    body_stored: bool
    proposal_id: Optional[str]
    proposed_fields: tuple[str, ...]
    injection_flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_message_id": self.case_message_id,
            "case_status": self.case_status,
            "channel": self.channel,
            "external_ref": self.external_ref,
            "content_class": self.content_class,
            "duplicate": self.duplicate,
            "created_case": self.created_case,
            "body_stored": self.body_stored,
            "proposal_id": self.proposal_id,
            "proposed_fields": list(self.proposed_fields),
            "injection_flags": list(self.injection_flags),
        }


@dataclass(frozen=True)
class Draft:
    """Read-only projection of a ``message_drafts`` row."""

    draft_id: str
    case_id: str
    channel: str
    template: str
    locale: str
    recipient_ref: str
    body_text: str
    body_sha256: str
    claim_bindings: tuple[str, ...]
    grounding: str
    gate_decision: str
    status: str
    created_by: str
    created_at: str
    booking_id: Optional[str] = None
    approval_id: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    sent_by: Optional[str] = None
    sent_at: Optional[str] = None
    external_ref: Optional[str] = None

    @property
    def is_approved(self) -> bool:
        return self.status == "APPROVED"

    @property
    def is_grounded(self) -> bool:
        return self.grounding == "GROUNDED"

    @property
    def is_sent(self) -> bool:
        return self.status == "SENT"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Draft":
        try:
            bindings = json.loads(row["claim_bindings"] or "[]")
        except (ValueError, TypeError):
            raise _fail(
                CODE_DRAFT_BODY_TAMPERED,
                "claim_bindings bukan JSON",
                draft_id=str(row["draft_id"]),
            )
        if not isinstance(bindings, list) or not all(isinstance(item, str) for item in bindings):
            raise _fail(
                CODE_DRAFT_BODY_TAMPERED,
                "claim_bindings harus daftar teks",
                draft_id=str(row["draft_id"]),
            )
        return cls(
            draft_id=str(row["draft_id"]),
            case_id=str(row["case_id"]),
            channel=str(row["channel"]),
            template=str(row["template"]),
            locale=str(row["locale"]),
            recipient_ref=str(row["recipient_ref"]),
            body_text=str(row["body_text"]),
            body_sha256=str(row["body_sha256"]),
            claim_bindings=tuple(bindings),
            grounding=str(row["grounding"]),
            gate_decision=str(row["gate_decision"]),
            status=str(row["status"]),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            booking_id=None if row["booking_id"] is None else str(row["booking_id"]),
            approval_id=None if row["approval_id"] is None else str(row["approval_id"]),
            approved_by=None if row["approved_by"] is None else str(row["approved_by"]),
            approved_at=None if row["approved_at"] is None else str(row["approved_at"]),
            sent_by=None if row["sent_by"] is None else str(row["sent_by"]),
            sent_at=None if row["sent_at"] is None else str(row["sent_at"]),
            external_ref=None if row["external_ref"] is None else str(row["external_ref"]),
        )


@dataclass(frozen=True)
class OutboundArtifact:
    """The bytes that left, described without re-printing guest content."""

    draft_id: str
    case_id: str
    channel: str
    recipient_ref: str
    external_ref: str
    path: str
    payload_sha256: str
    file_created: bool


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one outbound delivery attempt."""

    draft_id: str
    case_id: str
    channel: str
    recipient_ref: str
    external_ref: str
    path: str
    already_sent: bool
    payload_sha256: str
    hook_called: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "case_id": self.case_id,
            "channel": self.channel,
            "recipient_ref": self.recipient_ref,
            "external_ref": self.external_ref,
            "path": self.path,
            "already_sent": self.already_sent,
            "payload_sha256": self.payload_sha256,
            "hook_called": self.hook_called,
        }


# --------------------------------------------------------------------------- #
# Transport contract
# --------------------------------------------------------------------------- #
class Transport(Protocol):
    """An inbound channel: one ``channel`` name and a pull of messages.

    Structural, not inherited: a test double with these two members is a transport.
    """

    channel: str

    def fetch(self) -> tuple[InboundMessage, ...]:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------- #
# Inbound channel: email files
# --------------------------------------------------------------------------- #
def _split_header_body(raw: bytes) -> tuple[dict[str, str], bytes]:
    """Minimal fallback parse used only to report *which* header is missing."""
    head, _, _body = raw.partition(b"\n\n")
    headers: dict[str, str] = {}
    for line in head.splitlines():
        if b":" not in line:
            break
        name, _, value = line.partition(b":")
        key = name.decode("latin-1").strip().lower()
        if key and key not in headers:
            headers[key] = value.decode("latin-1").strip()
    return headers, _body


def parse_email_bytes(data: bytes, *, source: str = "") -> InboundMessage:
    """Parse one message file into an :class:`InboundMessage`.

    Contract: ``From`` and ``Subject`` must be present and non-empty, ``Date`` may
    be absent (the case's clock is then authoritative) but must parse if present,
    the body must be non-empty, and there must be exactly one message per file (a
    multipart file contributes its text parts, joined, plus an attachment count).
    Any violation is an ``INVALID_MESSAGE`` refusal — nothing is ever stored from a
    file that does not parse.
    """
    if not isinstance(data, bytes) or not data:
        raise _fail(CODE_INVALID_MESSAGE, "berkas pesan kosong", source=source)
    if len(data) > MAX_MESSAGE_BYTES:
        raise _fail(
            CODE_MESSAGE_TOO_LARGE,
            f"berkas pesan melebihi batas {MAX_MESSAGE_BYTES} byte",
            source=source,
            size=len(data),
        )
    message = BytesParser(policy=policy.default).parsebytes(data)

    fallback_headers, _ = _split_header_body(data)
    from_raw = str(message.get("From", "") or "").strip()
    if not from_raw:
        reason = "missing_from"
        if b"\n" not in data and b":" not in data:
            reason = "not_a_message_file"
        raise _fail(
            CODE_INVALID_MESSAGE,
            "header From wajib ada dan tidak boleh kosong",
            source=source,
            field="From",
            reason=reason,
        )
    subject_raw = str(message.get("Subject", "") or "").strip()
    if not subject_raw:
        raise _fail(
            CODE_INVALID_MESSAGE,
            "header Subject wajib ada dan tidak boleh kosong",
            source=source,
            field="Subject",
            reason="missing_subject",
        )

    received_at: Optional[str] = None
    date_raw = str(message.get("Date", "") or "").strip() or fallback_headers.get("date", "")
    if date_raw:
        try:
            parsed = parsedate_to_datetime(date_raw)
        except (TypeError, ValueError) as exc:
            raise _fail(
                CODE_INVALID_MESSAGE,
                f"header Date tidak bisa dibaca: {date_raw!r}",
                source=source,
                field="Date",
            ) from exc
        if parsed is None:
            raise _fail(
                CODE_INVALID_MESSAGE,
                f"header Date tidak bisa dibaca: {date_raw!r}",
                source=source,
                field="Date",
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        received_at = to_iso(parsed)

    text_parts: list[str] = []
    attachment_count = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            attachment_count += 1
            continue
        if part.get_content_maintype() != "text":
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError, UnicodeDecodeError):
            raw_payload = part.get_payload(decode=True)
            if isinstance(raw_payload, bytes):
                content = raw_payload.decode("utf-8", errors="replace")
            else:
                content = None
        if isinstance(content, str) and content.strip():
            text_parts.append(content)
    body = "\n".join(text_parts).strip()
    if not body:
        raise _fail(
            CODE_INVALID_MESSAGE,
            "body pesan kosong atau tidak bisa didekode",
            source=source,
            field="body",
            reason="missing_body",
        )

    address, raw_subject = _address_of(from_raw), subject_raw
    message_id = str(message.get("Message-ID", "") or "").strip()
    external_ref = _external_ref_for(
        "email",
        message_id if message_id else f"{from_raw}\x1f{received_at or ''}\x1f{sha256_hex(body)}",
    )
    headers = {
        "from_redacted": redact(from_raw),
        "subject_redacted": redact(raw_subject),
    }
    if message.get("To"):
        headers["to_redacted"] = redact(str(message.get("To")))
    if date_raw:
        headers["date"] = date_raw
    if message_id:
        headers["message_id_ref"] = external_ref
    return InboundMessage(
        channel="email",
        external_ref=external_ref,
        body=body,
        sender_ref=f"{GUEST_REF_PREFIX}_{sha256_hex('email', address)[:16]}",
        subject=raw_subject,
        received_at=received_at,
        attachment_count=attachment_count,
        headers=headers,
        source=source,
    )


def _external_ref_for(channel: str, native_id: str) -> str:
    """Pseudonymised, deterministic external reference.

    The native identifier (Message-ID, form id, provider message id) is hashed so
    the ``case_messages.external_ref`` column never carries an address, a phone
    number or a provider id, while staying stable enough to deduplicate replays.
    """
    return f"{channel}_{sha256_hex(channel, native_id)[:32]}"


def _address_of(from_header: str) -> str:
    """The address part of a ``From`` header, or the whole header if there is none."""
    match = re.search(r"<([^<>@\s]+@[^<>\s]+)>", from_header)
    if match:
        return match.group(1).strip().lower()
    if "@" in from_header:
        return from_header.strip().strip("<>").lower()
    return from_header.strip()


def parse_message_file(path: Path) -> InboundMessage:
    """Read and parse one message file (``.eml``/``.txt``)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _fail(
            CODE_INVALID_MESSAGE,
            f"berkas pesan tidak bisa dibaca: {path.name}",
            source=path.name,
        ) from exc
    return parse_email_bytes(data, source=path.name)


class EmailFileConnector:
    """Inbound ``email`` channel backed by a directory of ``.eml``/``.txt`` files.

    One file is one message (never two), files are visited in sorted name order so
    two runs over the same directory produce the same rows in the same order, and a
    file that does not parse is *refused* — it yields a :class:`ScanResult` with an
    error code and contributes no message. Hidden files, temporary suffixes,
    directories and symlinks are skipped (``UNSAFE_FILE`` for a symlink: a symlink
    out of the inbox is not mail).
    """

    channel = "email"

    def __init__(
        self,
        root: Any,
        *,
        suffixes: Sequence[str] = (".eml", ".txt"),
        max_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.root = Path(root)
        self.suffixes = tuple(str(s).lower() for s in suffixes)
        self.max_bytes = int(max_bytes)

    # -- discovery --------------------------------------------------------- #
    def candidates(self) -> tuple[Path, ...]:
        """Every file this connector would look at, in deterministic order."""
        if not self.root.exists():
            raise _fail(
                CODE_INVALID_MESSAGE,
                f"direktori inbox tidak ada: {self.root}",
                root=str(self.root),
            )
        if not self.root.is_dir():
            raise _fail(
                CODE_INVALID_MESSAGE,
                f"bukan direktori: {self.root}",
                root=str(self.root),
            )
        found: list[Path] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            name = entry.name
            if name.startswith("."):
                continue
            if name.endswith("~") or name.endswith(".tmp") or name.endswith(".swp"):
                continue
            if entry.is_dir() or entry.is_symlink():
                continue
            if entry.suffix.lower() not in self.suffixes:
                continue
            found.append(entry)
        return tuple(found)

    def scan(self) -> tuple[ScanResult, ...]:
        """Per-file outcome; a broken file never raises, it reports."""
        results: list[ScanResult] = []
        for path in self.candidates():
            if path.is_symlink():  # pragma: no cover - filtered in candidates()
                results.append(
                    ScanResult(path.name, None, CODE_UNSAFE_FILE, "symlink tidak diproses")
                )
                continue
            try:
                size = path.stat().st_size
            except OSError:
                results.append(
                    ScanResult(path.name, None, CODE_INVALID_MESSAGE, "stat gagal")
                )
                continue
            if size > self.max_bytes:
                results.append(
                    ScanResult(
                        path.name,
                        None,
                        CODE_MESSAGE_TOO_LARGE,
                        f"berkas {size} byte melebihi batas {self.max_bytes} byte",
                    )
                )
                continue
            try:
                message = parse_message_file(path)
            except ConnectorError as exc:
                results.append(
                    ScanResult(path.name, None, exc.code, exc.message)
                )
                continue
            results.append(ScanResult(path.name, message))
        return tuple(results)

    def fetch(self) -> tuple[InboundMessage, ...]:
        """Strict pull: every candidate must parse, or ``REJECTED_FILES``.

        The CLI uses :meth:`scan` so one junk file cannot stop the inbox; a caller
        that wants all-or-nothing gets that here.
        """
        results = self.scan()
        rejected = [r for r in results if not r.ok]
        if rejected:
            raise _fail(
                CODE_REJECTED_FILES,
                f"{len(rejected)} berkas ditolak: "
                + ", ".join(f"{r.source}({r.error_code})" for r in rejected),
                rejected=[{"source": r.source, "code": r.error_code} for r in rejected],
                accepted=len(results) - len(rejected),
            )
        return tuple(r.message for r in results if r.message is not None)


# --------------------------------------------------------------------------- #
# Inbound channel: website form
# --------------------------------------------------------------------------- #
FORM_REQUIRED_FIELDS: tuple[str, ...] = ("name", "email", "message")


def parse_website_payload(
    payload: Any,
    *,
    source: str = "website_form",
    content_class: Optional[str] = None,
) -> InboundMessage:
    """Parse one website form payload.

    Required: ``name``, ``email``, ``message`` (non-empty strings). Optional:
    ``dates`` (``{"check_in": ISO, "check_out": ISO}`` — declared as intake
    *candidates*, never as accepted facts), ``phone``, ``reference``. Missing or
    malformed input is ``INVALID_PAYLOAD`` and stores nothing.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail(
                CODE_INVALID_PAYLOAD,
                "payload form bukan UTF-8",
                source=source,
            ) from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise _fail(
                CODE_INVALID_PAYLOAD,
                "payload form bukan JSON yang valid",
                source=source,
            ) from exc
    if not isinstance(payload, Mapping):
        raise _fail(
            CODE_INVALID_PAYLOAD,
            f"payload form harus objek JSON, dapat {type(payload).__name__}",
            source=source,
            got=type(payload).__name__,
        )
    values: dict[str, str] = {}
    for name in FORM_REQUIRED_FIELDS:
        raw = payload.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise _fail(
                CODE_INVALID_PAYLOAD,
                f"field form wajib '{name}' kosong atau hilang",
                source=source,
                field=name,
            )
        if not isinstance(raw, str):
            raise _fail(
                CODE_INVALID_PAYLOAD,
                f"field form '{name}' harus teks",
                source=source,
                field=name,
                got=type(raw).__name__,
            )
        values[name] = raw.strip()

    declared = _declared_stay_fields(payload.get("dates"), source=source)
    body_parts = [values["message"]]
    if payload.get("phone") is not None and isinstance(payload["phone"], str):
        body_parts.append(f"Telepon: {payload['phone'].strip()}")
    body = "\n".join(part for part in body_parts if part.strip())

    native_id = payload.get("reference")
    if not isinstance(native_id, str) or not native_id.strip():
        # No provider id: the content itself is the identity, so a re-delivery of
        # the same submission is recognised as the same message.
        native_id = f"{values['email']}\x1f{sha256_hex(values['message'])}"
    headers = {
        "name_redacted": redact(values["name"]),
        "email_redacted": redact(values["email"]),
        "dates": canonical_json(payload.get("dates")) if payload.get("dates") is not None else None,
    }
    return InboundMessage(
        channel="website",
        external_ref=_external_ref_for("website", native_id),
        body=body,
        sender_ref=f"{GUEST_REF_PREFIX}_{sha256_hex('website', values['email'].lower())[:16]}",
        subject=f"Form website: {values['name']}",
        received_at=None,
        content_class=content_class or DEFAULT_INBOUND_CONTENT_CLASS,
        headers={key: value for key, value in headers.items() if value},
        source=source,
        declared_fields=declared,
    )


def _declared_stay_fields(dates: Any, *, source: str) -> tuple[I.ProposedField, ...]:
    """Turn a form's structured ``dates`` into PROPOSED intake candidates.

    Structured input still only ever becomes a *proposal* — a human accepts it (or
    not) through :class:`intake.IntakeService`. A date that is not ISO-8601 is an
    ``INVALID_PAYLOAD`` refusal: guessing a stay window is worse than refusing.
    """
    if dates is None:
        return ()
    if isinstance(dates, str):
        # Free text: kept as data in the headers, never turned into facts.
        return ()
    if not isinstance(dates, Mapping):
        raise _fail(
            CODE_INVALID_PAYLOAD,
            "field 'dates' harus objek {check_in, check_out} atau teks",
            source=source,
            field="dates",
        )
    out: list[I.ProposedField] = []
    for key in ("check_in", "check_out"):
        raw = dates.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise _fail(
                CODE_INVALID_PAYLOAD,
                f"dates.{key} harus teks ISO-8601",
                source=source,
                field=f"dates.{key}",
            )
        try:
            parsed = date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise _fail(
                CODE_INVALID_PAYLOAD,
                f"dates.{key} bukan tanggal ISO-8601: {raw!r}",
                source=source,
                field=f"dates.{key}",
            ) from exc
        out.append(I.ProposedField(name=key, value=parsed, confidence=95))
    return tuple(out)


class WebsiteFormConnector:
    """Inbound ``website`` channel: one JSON form payload in, one message out."""

    channel = "website"

    def __init__(
        self,
        payload: Any,
        *,
        source: str = "website_form",
        content_class: Optional[str] = None,
    ) -> None:
        self.payload = payload
        self.source = source
        self.content_class = content_class

    def fetch(self) -> tuple[InboundMessage, ...]:
        return (
            parse_website_payload(
                self.payload, source=self.source, content_class=self.content_class
            ),
        )


# --------------------------------------------------------------------------- #
# Inbound channel: WhatsApp webhook
# --------------------------------------------------------------------------- #
def _whatsapp_messages(payload: Mapping[str, Any], *, source: str) -> Iterator[dict[str, Any]]:
    """Yield the flat ``{from,id,timestamp,text}`` records of a webhook payload.

    Two shapes are accepted: the Cloud-API envelope
    (``entry[].changes[].value.messages[]``) and a flat record. Anything else is
    ``INVALID_PAYLOAD``.
    """
    if "messages" in payload or "text" in payload and "from" in payload:
        yield dict(payload)
        return
    entries = payload.get("entry")
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        found = False
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            changes = entry.get("changes")
            if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
                continue
            for change in changes:
                if not isinstance(change, Mapping):
                    continue
                value = change.get("value")
                if not isinstance(value, Mapping):
                    continue
                messages = value.get("messages")
                if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
                    continue
                for record in messages:
                    if isinstance(record, Mapping):
                        found = True
                        yield dict(record)
        if found:
            return
    raise _fail(
        CODE_INVALID_PAYLOAD,
        "payload webhook tidak memuat pesan WhatsApp",
        source=source,
    )


def parse_whatsapp_payload(
    payload: Any,
    *,
    source: str = "whatsapp_webhook",
    content_class: Optional[str] = None,
) -> tuple[InboundMessage, ...]:
    """Turn a **verified** WhatsApp webhook payload into messages.

    Signature verification happens before this function is called: unverified bytes
    are never parsed.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail(CODE_INVALID_PAYLOAD, "payload webhook bukan UTF-8", source=source) from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise _fail(CODE_INVALID_PAYLOAD, "payload webhook bukan JSON", source=source) from exc
    if not isinstance(payload, Mapping):
        raise _fail(
            CODE_INVALID_PAYLOAD,
            f"payload webhook harus objek JSON, dapat {type(payload).__name__}",
            source=source,
        )
    out: list[InboundMessage] = []
    for record in _whatsapp_messages(payload, source=source):
        sender = record.get("from")
        if not isinstance(sender, str) or not sender.strip():
            raise _fail(CODE_INVALID_PAYLOAD, "pesan webhook tanpa 'from'", source=source, field="from")
        text = record.get("text")
        if isinstance(text, Mapping):
            text = text.get("body")
        if not isinstance(text, str) or not text.strip():
            raise _fail(
                CODE_INVALID_PAYLOAD,
                "pesan webhook tanpa teks (media/status tidak didukung)",
                source=source,
                field="text",
            )
        message_id = record.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise _fail(CODE_INVALID_PAYLOAD, "pesan webhook tanpa 'id'", source=source, field="id")
        received_at: Optional[str] = None
        stamp = record.get("timestamp")
        if stamp is not None:
            try:
                seconds = int(str(stamp).strip())
            except (TypeError, ValueError) as exc:
                raise _fail(
                    CODE_INVALID_PAYLOAD,
                    f"timestamp webhook tidak valid: {stamp!r}",
                    source=source,
                    field="timestamp",
                ) from exc
            received_at = to_iso(datetime.fromtimestamp(seconds, tz=UTC))
        out.append(
            InboundMessage(
                channel="whatsapp_business",
                external_ref=_external_ref_for("whatsapp_business", message_id.strip()),
                body=text.strip(),
                sender_ref=f"{GUEST_REF_PREFIX}_{sha256_hex('whatsapp', sender.strip())[:16]}",
                subject="Pesan WhatsApp",
                received_at=received_at,
                content_class=content_class or DEFAULT_INBOUND_CONTENT_CLASS,
                headers={"from_redacted": redact(sender.strip())},
                source=source,
            )
        )
    if not out:
        raise _fail(CODE_INVALID_PAYLOAD, "payload webhook tidak memuat pesan", source=source)
    return tuple(out)


class WhatsAppWebhookConnector:
    """Inbound ``whatsapp_business`` channel with HMAC-SHA256 webhook verification.

    The signature is checked over the **raw body bytes** before any JSON is parsed,
    with :func:`hmac.compare_digest`. The secret comes from the constructor or from
    an environment variable (``WEBHOOK_SIGNING_SECRET`` by default) — never from the
    database, never from the payload. A missing, blank or wrong signature refuses
    the whole request (``SIGNATURE_MISSING`` / ``SIGNATURE_INVALID``).
    """

    channel = "whatsapp_business"

    def __init__(
        self,
        body: Any,
        *,
        headers: Mapping[str, str],
        secret: Optional[str] = None,
        env_secret: Optional[str] = WHATSAPP_SECRET_ENV,
        env: Optional[Mapping[str, str]] = None,
        signature_header: str = SIGNATURE_HEADER,
        content_class: Optional[str] = None,
        source: str = "whatsapp_webhook",
    ) -> None:
        self.body = _as_bytes(body)
        self.headers = {str(k).lower(): v for k, v in dict(headers).items()}
        self.signature_header = signature_header.lower()
        self.content_class = content_class
        self.source = source
        self.env_secret = env_secret
        self.env = env
        self._secret = secret if secret is not None else self._secret_from_env()
        if not self._secret or not str(self._secret).strip():
            raise _fail(
                CODE_SECRET_MISSING,
                "secret webhook WhatsApp wajib (argumen atau env)",
                secret_name=env_secret,
                reason="missing_or_empty",
            )

    # Never print the secret, whatever happens to this object.
    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"WhatsAppWebhookConnector(source={self.source!r}, "
            f"secret=<redacted>, bytes={len(self.body)})"
        )

    def _secret_from_env(self) -> Optional[str]:
        if self.env_secret is None:
            return None
        return load_env_secret(self.env_secret, env=self.env, required=False)

    @property
    def secret(self) -> str:
        """The signing secret. Kept internal so it never reaches a repr/log."""
        return str(self._secret)

    def signature(self) -> Optional[str]:
        """The signature the request carried, if any."""
        value = self.headers.get(self.signature_header)
        return None if value is None else str(value)

    def verify(self) -> None:
        """Raise ``SIGNATURE_MISSING``/``SIGNATURE_INVALID`` unless the HMAC matches."""
        verify_webhook_signature(
            self.secret,
            self.body,
            self.signature(),
            header=self.signature_header,
        )

    def fetch(self) -> tuple[InboundMessage, ...]:
        """Verify first, then parse. An unverified body is never parsed."""
        self.verify()
        return parse_whatsapp_payload(
            self.body, source=self.source, content_class=self.content_class
        )


# --------------------------------------------------------------------------- #
# Baca sisi DB in-bound: idempotency + intake proposals
# --------------------------------------------------------------------------- #
def find_message_by_external_ref(
    conn: sqlite3.Connection,
    channel: str,
    external_ref: str,
    *,
    agency_id: str = "agency_default",
) -> Optional[Mapping[str, Any]]:
    """The stored inbound message for ``(channel, external_ref)``, if any."""
    agency = require_ref(agency_id, "agency_id")
    return conn.execute(
        "SELECT m.case_message_id, m.case_id, m.channel, m.external_ref, m.body_sha256,"
        " m.content_class, m.received_at"
        " FROM case_messages m JOIN cases c ON c.case_id = m.case_id"
        " WHERE m.channel = ? AND m.external_ref = ? AND m.direction = 'INBOUND'"
        " AND c.agency_id = ? ORDER BY m.created_at, m.case_message_id LIMIT 1",
        (channel, external_ref, agency),
    ).fetchone()


def find_open_case(
    conn: sqlite3.Connection,
    channel: str,
    guest_ref: str,
    *,
    agency_id: str = "agency_default",
) -> Optional[str]:
    """The open case a new message from this guest belongs to (oldest first)."""
    agency = require_ref(agency_id, "agency_id")
    row = conn.execute(
        "SELECT case_id FROM cases WHERE channel = ? AND guest_ref = ?"
        " AND agency_id = ? AND status NOT LIKE 'CLOSED_%' ORDER BY created_at, case_id LIMIT 1",
        (channel, guest_ref, agency),
    ).fetchone()
    return None if row is None else str(row["case_id"])


def propose_candidates(message: InboundMessage, *, now: Any) -> tuple[I.ProposedField, ...]:
    """Fields this message *suggests* — pure, writes nothing.

    Rule-based extraction over the body plus whatever the channel declared
    structurally (a website form's ``dates``). Both are proposals; neither is a
    fact until a human reviews it. ``now`` (a ``date``/``datetime``) is an argument
    because relative phrases ("besok", "akhir bulan") may not be resolved against a
    hidden wall clock.
    """
    extracted: list[I.ProposedField] = list(I.parse_rule_based(message.body, now=now))
    extracted.extend(message.declared_fields)
    seen: dict[str, I.ProposedField] = {}
    for item in extracted:
        current = seen.get(item.name)
        if current is None or item.confidence > current.confidence:
            seen[item.name] = item
    return tuple(seen[name] for name in I.FIELD_SPECS if name in seen)


def propose_intake(
    intake_service: I.IntakeService,
    *,
    case_id: str,
    message: InboundMessage,
    source_message_id: str,
    now: Any,
    candidates: Optional[tuple[I.ProposedField, ...]] = None,
) -> Optional[str]:
    """Persist this message's candidates as ``PROPOSED`` fields, or nothing.

    Returns the proposal id, or ``None`` when the message suggested no field at all
    (an empty proposal carries no provenance and :mod:`intake` refuses to write it).
    Every row is written ``PROPOSED``; this module has no code path that calls
    ``IntakeService.review`` and none that writes ``ACCEPTED``.
    """
    if candidates is None:
        candidates = propose_candidates(message, now=now)
    if not candidates:
        return None
    produced_at = to_iso(now) if isinstance(now, datetime) else now
    proposal_id, _rows = intake_service.propose(
        case_id,
        source_message_id=source_message_id,
        fields=candidates,
        extractor=EXTRACTOR_NAME,
        method="rule",
        produced_at=produced_at,
    )
    return proposal_id


def _wrap_case_error(exc: DomainError) -> ConnectorError:
    return ConnectorError(exc.message, code=exc.code, **dict(exc.details))


def intake_message(
    conn: sqlite3.Connection,
    *,
    message: InboundMessage,
    service: C.CaseService,
    intake: Optional[I.IntakeService] = None,
    clock: Any = None,
    ids: Optional[IdFactory] = None,
    content_class: Optional[str] = None,
    guest_ref: Optional[str] = None,
    priority: str = C.DEFAULT_PRIORITY,
    sla_policy: Optional[str] = None,
    agency_id: Optional[str] = None,
) -> IntakeResult:
    """Store one inbound message as a case (or append it to the guest's case).

    The write path is :class:`cases.CaseService` — ``open_case`` for the first
    message of a thread, ``record_inbound`` for the rest. This function never
    inserts into ``cases``/``case_messages``/``case_events`` by hand, never touches
    ``cases.status``, and never turns message text into an action.

    Idempotency: ``(channel, external_ref)`` identifies the message. A replay
    returns ``duplicate=True`` with the *existing* row and writes nothing; the same
    reference carrying *different* content is ``EXTERNAL_REF_CONFLICT`` (silently
    dropping a changed message would be worse than refusing it).

    ``intake`` (optional) records the message's extracted/declared candidates as
    ``PROPOSED`` fields. They are never auto-accepted.
    """
    if not isinstance(message, InboundMessage):
        raise _fail(
            CODE_INVALID_MESSAGE,
            f"message harus InboundMessage, dapat {type(message).__name__}",
        )
    if message.channel not in C.CASE_CHANNELS:  # pragma: no cover - guarded in __post_init__
        raise _fail(CODE_INVALID_MESSAGE, "channel tidak dikenal: " + str(message.channel))
    clock = clock if clock is not None else getattr(service, "clock", None) or SystemClock()
    declared = content_class if content_class is not None else message.content_class
    resolved_class = resolve_inbound_content_class(message.body, declared)

    agency_scope = agency_id or getattr(service, "agency_id", None) or "agency_default"
    existing = find_message_by_external_ref(
        conn, message.channel, message.external_ref, agency_id=agency_scope
    )
    if existing is not None:
        if str(existing["body_sha256"]) != message.body_sha256:
            raise _fail(
                CODE_EXTERNAL_REF_CONFLICT,
                "external_ref yang sama datang dengan isi berbeda: pesan ditolak, "
                "tidak ada baris yang ditulis",
                channel=message.channel,
                external_ref=message.external_ref,
                case_message_id=str(existing["case_message_id"]),
            )
        return _existing_result(conn, service, message, existing)

    guest = guest_ref if guest_ref is not None else message.guest_ref
    case_id = find_open_case(conn, message.channel, guest, agency_id=agency_scope)
    common = {
        "content_class": resolved_class,
        "body": message.body,
        "idempotency_key": message.idempotency_key,
        "external_ref": message.external_ref,
        "received_at": message.received_at,
        "attachments": message.attachment_count,
    }
    try:
        if case_id is None:
            case, stored = service.open_case(
                channel=message.channel,
                guest_ref=guest,
                priority=priority,
                sla_policy=sla_policy,
                sender_ref=message.sender_ref,
                **common,
            )
            created_case = True
        else:
            stored = service.record_inbound(case_id, channel=message.channel, **common)
            case = service.get(case_id)
            created_case = False
    except C.DuplicateCase:
        # A racing replay: the case exists, so report the duplicate instead of
        # opening a second one.
        raced = find_message_by_external_ref(
            conn, message.channel, message.external_ref, agency_id=agency_scope
        )
        if raced is not None:
            return _existing_result(conn, service, message, raced)
        raise
    except C.CaseError as exc:
        raise _wrap_case_error(exc) from exc

    proposal_id: Optional[str] = None
    proposed: tuple[str, ...] = ()
    if intake is not None:
        now = clock.now().astimezone(UTC)
        candidates = propose_candidates(message, now=now)
        if candidates:
            try:
                proposal_id = propose_intake(
                    intake,
                    case_id=case.case_id,
                    message=message,
                    source_message_id=stored.case_message_id,
                    now=now,
                    candidates=candidates,
                )
            except DomainError as exc:
                raise _wrap_case_error(exc) from exc
            proposed = tuple(item.name for item in candidates)
    return IntakeResult(
        case_id=case.case_id,
        case_message_id=stored.case_message_id,
        case_status=case.status,
        channel=message.channel,
        external_ref=message.external_ref,
        content_class=resolved_class,
        duplicate=False,
        created_case=created_case,
        body_stored=stored.body_stored,
        proposal_id=proposal_id,
        proposed_fields=proposed,
        injection_flags=message.injection_flags,
    )


def _existing_result(
    conn: sqlite3.Connection,
    service: C.CaseService,
    message: InboundMessage,
    row: Mapping[str, Any],
) -> IntakeResult:
    """A no-write result describing the row a replay already created."""
    case = service.get(str(row["case_id"]))
    return IntakeResult(
        case_id=case.case_id,
        case_message_id=str(row["case_message_id"]),
        case_status=case.status,
        channel=str(row["channel"]),
        external_ref=str(row["external_ref"]),
        content_class=str(row["content_class"]),
        duplicate=True,
        created_case=False,
        body_stored=str(row["content_class"]) in STORABLE_CONTENT_CLASSES,
        proposal_id=None,
        proposed_fields=(),
        injection_flags=message.injection_flags,
    )


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #
def read_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    agency_id: str = "agency_default",
) -> Draft:
    """One tenant-scoped ``message_drafts`` row, or ``DRAFT_NOT_FOUND``."""
    ref = require_ref(draft_id, "draft_id")
    agency = require_ref(agency_id, "agency_id")
    row = conn.execute(
        "SELECT d.* FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
        " WHERE d.draft_id = ? AND c.agency_id = ?",
        (ref, agency),
    ).fetchone()
    if row is None:
        raise _fail(CODE_DRAFT_NOT_FOUND, f"draft tidak ditemukan: {ref}", draft_id=ref)
    return Draft.from_row(row)


def default_approved_hook(artifact: OutboundArtifact) -> None:
    """The outbound seam. Does nothing today.

    ``src/messages.py`` (which owns draft creation and would own follow-up work)
    does not exist yet; when it does, it takes this hook. It is called **after** the
    transport succeeded and after the draft was marked ``SENT``, so its failure can
    never be mistaken for "nothing was sent".
    """


class OutboundTransport(Protocol):
    """Sends one already-approved payload; returns the transport receipt."""

    def send(self, payload: Mapping[str, Any]) -> str:  # pragma: no cover - protocol
        ...


class FileOutboundTransport:
    """Writes one deterministic JSON file per draft, atomically (tmp + ``os.replace``).

    Deterministic: the file name is derived from ``(channel, draft_id)`` and the
    document from the draft's content, so the same draft yields byte-identical
    output and the same ``external_ref``. The write is atomic, so a crashed process
    leaves either no file or a complete one — never a half-written artifact that
    looks delivered.
    """

    def __init__(self, root: Any, *, suffix: str = ".out.json", mode: int = 0o600) -> None:
        self.root = Path(root)
        self.suffix = suffix
        self.mode = mode

    def file_name(self, channel: str, draft_id: str) -> str:
        return f"{require_ref(channel, 'channel')}-{require_ref(draft_id, 'draft_id')}{self.suffix}"

    def path_for(self, channel: str, draft_id: str) -> Path:
        return Path(self.root) / self.file_name(channel, draft_id)

    def send(self, payload: Mapping[str, Any]) -> str:
        """Write ``payload`` (plus its ``external_ref``) and return that reference."""
        channel = require_ref(str(payload["channel"]), "channel")
        draft_id = require_ref(str(payload["draft_id"]), "draft_id")
        body_sha256 = str(payload.get("body_sha256", ""))
        receipt = f"{EXTERNAL_REF_PREFIX}_{sha256_hex(channel, draft_id, body_sha256)[:32]}"
        document = dict(payload)
        document["external_ref"] = receipt
        document["artifact_version"] = MODULE_VERSION
        data = canonical_json(document).encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(channel, draft_id)
        if target.exists() and target.read_bytes() == data:
            return receipt
        tmp = target.with_name(target.name + ".tmp")
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.mode)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - the tmp file is already gone
                pass
            raise
        os.replace(tmp, target)
        return receipt


#: Reason a draft was refused, so the failure is greppable without the message text.
REFUSAL_NOT_APPROVED = "status_bukan_APPROVED"
REFUSAL_NOT_GROUNDED = "grounding_bukan_GROUNDED"
REFUSAL_NO_APPROVER = "approved_by/approved_at kosong"
REFUSAL_APPROVAL_NOT_APPROVED = "approval bukan APPROVED"


class OutboundQueue:
    """The only path from an approved draft to the wire.

    Every delivery is checked, in order, against all of:

    1. ``status == 'APPROVED'`` — else ``DRAFT_NOT_APPROVED`` (covers ``DRAFT``,
       ``PENDING_APPROVAL``, ``REJECTED``, ``EXPIRED``);
    2. ``grounding == 'GROUNDED'`` — else ``DRAFT_NOT_GROUNDED``;
    3. ``approved_by`` **and** ``approved_at`` recorded — else ``DRAFT_NOT_APPROVED``;
    4. an ``APPROVED`` ``message_send`` approval from
       :class:`approvals.ApprovalService` for this draft — else
       ``APPROVAL_NOT_APPROVED`` (a status column is a claim; the decision log is
       the evidence);
    5. ``body_sha256`` matching the stored ``body_text`` — else
       ``DRAFT_BODY_TAMPERED``;
    6. the classification gate for the channel's outbound destination — else the
       gate's own code (an unclassified body is never sent).

    Only then does the transport run, and only after it *returned* is the draft
    marked ``SENT`` with the receipt as ``external_ref``. A transport that raises
    leaves the draft untouched: no ``SENT``, no ``external_ref``, no partial file.
    """

    def __init__(
        self,
        root: Any,
        *,
        conn: sqlite3.Connection,
        transport: Optional[OutboundTransport] = None,
        on_approved_draft: Optional[Any] = None,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        approvals: Optional[Any] = None,
        actor: str = DEFAULT_OUTBOX_ACTOR,
        content_class: str = "P1",
        actor_role: Optional[str] = None,
        justification: Optional[str] = None,
        agency_id: str = "agency_default",
    ) -> None:
        self.transport = (
            FileOutboundTransport(root) if transport is None else transport
        )
        self.root = Path(root)
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.on_approved_draft = (
            default_approved_hook if on_approved_draft is None else on_approved_draft
        )
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.actor = require_ref(actor, "actor")
        self.content_class = _require_content_class(content_class)
        self.actor_role = actor_role
        self.justification = justification
        self.agency_id = require_ref(agency_id, "agency_id")
        self.approvals = approvals

    # -- helpers ----------------------------------------------------------- #
    def _approval_service(self) -> Any:
        if self.approvals is None:
            from approvals import ApprovalService  # local import: optional coupling

            self.approvals = ApprovalService(self.conn, self.clock, self.ids)
        return self.approvals

    def _require_human_approval(self, draft: Draft) -> None:
        """Prove a human approved *this* draft, or refuse.

        The ``status`` column alone is not evidence (any code path could set it), so
        the decision log in ``approval_requests``/``approval_decisions`` is consulted
        through :class:`approvals.ApprovalService`, which derives the status from the
        recorded signatures.
        """
        service = self._approval_service()
        if draft.approval_id:
            approval = service.get(draft.approval_id)
            if (
                approval.status != "APPROVED"
                or approval.subject_type != "message_send"
                or approval.subject_id != draft.draft_id
            ):
                raise _fail(
                    CODE_APPROVAL_NOT_APPROVED,
                    f"approval {draft.approval_id} tidak APPROVED untuk draft ini",
                    draft_id=draft.draft_id,
                    approval_id=draft.approval_id,
                    approval_status=approval.status,
                    reason=REFUSAL_APPROVAL_NOT_APPROVED,
                )
            return
        for approval in service.for_subject("message_send", draft.draft_id):
            if approval.status == "APPROVED":
                return
        raise _fail(
            CODE_APPROVAL_NOT_APPROVED,
            "draft tidak punya approval message_send yang APPROVED",
            draft_id=draft.draft_id,
            reason=REFUSAL_APPROVAL_NOT_APPROVED,
        )

    def _require_sendable(self, draft: Draft) -> None:
        if draft.status == "SENT":
            raise _fail(
                CODE_DRAFT_ALREADY_SENT,
                "draft sudah ditandai SENT (lihat deliver() untuk replay)",
                draft_id=draft.draft_id,
            )
        if not draft.is_approved:
            raise _fail(
                CODE_DRAFT_NOT_APPROVED,
                f"draft berstatus {draft.status}: hanya draft APPROVED yang boleh dikirim",
                draft_id=draft.draft_id,
                status=draft.status,
                reason=REFUSAL_NOT_APPROVED,
            )
        if not draft.is_grounded:
            raise _fail(
                CODE_DRAFT_NOT_GROUNDED,
                f"draft grounding={draft.grounding}: hanya GROUNDED yang boleh dikirim",
                draft_id=draft.draft_id,
                grounding=draft.grounding,
                reason=REFUSAL_NOT_GROUNDED,
            )
        if not draft.approved_by or not draft.approved_at:
            raise _fail(
                CODE_DRAFT_NOT_APPROVED,
                "draft APPROVED tanpa approved_by/approved_at: bukan persetujuan manusia",
                draft_id=draft.draft_id,
                reason=REFUSAL_NO_APPROVER,
            )
        if sha256_hex(draft.body_text) != draft.body_sha256:
            raise _fail(
                CODE_DRAFT_BODY_TAMPERED,
                "body_sha256 draft tidak cocok dengan body_text: draft diubah setelah approval",
                draft_id=draft.draft_id,
            )
        self._require_human_approval(draft)

    def _destination(self, channel: str) -> str:
        destination = DESTINATION_BY_CHANNEL.get(channel)
        if destination is None:
            raise _fail(
                CODE_UNKNOWN_DESTINATION,
                f"channel {channel!r} tidak punya destination klasifikasi",
                channel=channel,
            )
        return destination

    def _payload(self, draft: Draft, *, at: str) -> dict[str, Any]:
        return {
            "channel": draft.channel,
            "draft_id": draft.draft_id,
            "case_id": draft.case_id,
            "booking_id": draft.booking_id,
            "recipient_ref": draft.recipient_ref,
            "template": draft.template,
            "locale": draft.locale,
            "body_text": draft.body_text,
            "body_sha256": draft.body_sha256,
            "claim_bindings": list(draft.claim_bindings),
            "grounding": draft.grounding,
            "gate_decision": draft.gate_decision,
            "approval_id": draft.approval_id,
            "approved_by": draft.approved_by,
            "approved_at": draft.approved_at,
            "sent_by": self.actor,
            "sent_at": at,
            "content_class": self.content_class,
        }

    # -- API --------------------------------------------------------------- #
    def pending(self, *, limit: Optional[int] = None) -> tuple[Draft, ...]:
        """Drafts that are approved *and* grounded and not yet sent, oldest first."""
        rows = self.conn.execute(
            "SELECT d.* FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE d.status = 'APPROVED' AND d.grounding = 'GROUNDED' AND c.agency_id = ?"
            " ORDER BY d.created_at, d.draft_id",
            (self.agency_id,),
        ).fetchall()
        drafts = tuple(Draft.from_row(row) for row in rows)
        return drafts if limit is None else drafts[: int(limit)]

    def plan(self, draft_id: str) -> dict[str, Any]:
        """What a delivery would do — reads only, writes nothing."""
        draft = read_draft(self.conn, draft_id, agency_id=self.agency_id)
        entry: dict[str, Any] = {
            "draft_id": draft.draft_id,
            "case_id": draft.case_id,
            "channel": draft.channel,
            "status": draft.status,
            "grounding": draft.grounding,
            "recipient_ref": draft.recipient_ref,
            "body_sha256": draft.body_sha256,
            "target": str(self._transport_path(draft)),
            "content_class": self.content_class,
            "destination": DESTINATION_BY_CHANNEL.get(draft.channel),
        }
        try:
            self._require_sendable(draft)
            decision = G.gate_outbound(
                [G.ClassifiedField(name="body_text", classification=self.content_class, value=draft.body_text)],
                destination=self._destination(draft.channel),
                actor_role=self.actor_role,
                justification=self.justification,
            )
            entry["gate"] = decision.code
            entry["gate_allowed"] = decision.allowed
            entry["would_send"] = bool(decision.allowed)
        except DomainError as exc:
            entry["gate"] = None
            entry["gate_allowed"] = False
            entry["would_send"] = False
            entry["refusal_code"] = exc.code
            entry["refusal_reason"] = exc.message
        return entry

    def _transport_path(self, draft: Draft) -> Path:
        transport = self.transport
        if isinstance(transport, FileOutboundTransport):
            return transport.path_for(draft.channel, draft.draft_id)
        return Path(self.root) / FileOutboundTransport(Path(self.root)).file_name(
            draft.channel, draft.draft_id
        )

    def deliver(self, draft_id: str, *, at: Any = None) -> DeliveryResult:
        """Send one approved draft exactly once.

        Replay of an already-delivered draft is a no-op returning
        ``already_sent=True`` — the file is not rewritten and the recorded
        ``external_ref`` is not changed.
        """
        draft = read_draft(self.conn, draft_id, agency_id=self.agency_id)
        target = self._transport_path(draft)
        if draft.is_sent:
            if not target.exists():
                raise _fail(
                    CODE_SENT_ARTIFACT_MISSING,
                    "draft ditandai SENT tetapi berkas kirimnya tidak ada",
                    draft_id=draft.draft_id,
                    path=str(target),
                )
            return DeliveryResult(
                draft_id=draft.draft_id,
                case_id=draft.case_id,
                channel=draft.channel,
                recipient_ref=draft.recipient_ref,
                external_ref=draft.external_ref or "",
                path=str(target),
                already_sent=True,
                payload_sha256="",
                hook_called=False,
            )

        self._require_sendable(draft)
        destination = self._destination(draft.channel)
        decision = G.gate_outbound(
            [G.ClassifiedField(name="body_text", classification=self.content_class, value=draft.body_text)],
            destination=destination,
            actor_role=self.actor_role,
            justification=self.justification,
        )
        if not decision.allowed:
            raise _fail(
                decision.code,
                f"payload outbound ditolak gate klasifikasi: {decision.message}",
                draft_id=draft.draft_id,
                destination=destination,
                content_class=self.content_class,
            )

        moment = to_iso(
            (parse_iso(at) if isinstance(at, str) else at if at is not None else self.clock.now()).astimezone(UTC)
        )
        payload = self._payload(draft, at=moment)
        payload_sha256 = sha256_hex(canonical_json(payload))
        existed_before = target.exists()
        try:
            receipt = self.transport.send(payload)
        except Exception as exc:  # noqa: BLE001 - any transport failure is the same refusal
            raise _fail(
                CODE_TRANSPORT_FAILED,
                f"transport gagal, draft TIDAK ditandai terkirim: {type(exc).__name__}",
                draft_id=draft.draft_id,
                channel=draft.channel,
                transport=type(self.transport).__name__,
            ) from exc
        if not isinstance(receipt, str) or not receipt.strip():
            raise _fail(
                CODE_TRANSPORT_FAILED,
                "transport tidak mengembalikan external_ref: draft TIDAK ditandai terkirim",
                draft_id=draft.draft_id,
                transport=type(self.transport).__name__,
            )
        try:
            receipt = require_ref(receipt.strip(), "external_ref")
        except DomainError as exc:
            raise _fail(
                CODE_TRANSPORT_FAILED,
                "external_ref dari transport bukan reference token yang sah: "
                "draft TIDAK ditandai terkirim",
                draft_id=draft.draft_id,
                transport=type(self.transport).__name__,
                reason="invalid_receipt",
            ) from exc

        # The draft is marked SENT only now, after the transport returned a receipt.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE message_drafts SET status = 'SENT', sent_by = ?, sent_at = ?,"
                " external_ref = ? WHERE draft_id = ? AND status = 'APPROVED'"
                " AND EXISTS (SELECT 1 FROM cases c WHERE c.case_id = message_drafts.case_id AND c.agency_id = ?)",
                (self.actor, moment, receipt, draft.draft_id, self.agency_id),
            )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        row = self.conn.execute(
            "SELECT d.status, d.external_ref FROM message_drafts d JOIN cases c ON c.case_id = d.case_id"
            " WHERE d.draft_id = ? AND c.agency_id = ?",
            (draft.draft_id, self.agency_id),
        ).fetchone()
        if row is None or str(row["status"]) != "SENT":  # pragma: no cover - defensive
            raise _fail(
                CODE_TRANSPORT_FAILED,
                "draft tidak bisa ditandai SENT setelah pengiriman",
                draft_id=draft.draft_id,
            )

        artifact = OutboundArtifact(
            draft_id=draft.draft_id,
            case_id=draft.case_id,
            channel=draft.channel,
            recipient_ref=draft.recipient_ref,
            external_ref=receipt,
            path=str(target),
            payload_sha256=payload_sha256,
            file_created=not existed_before,
        )
        # The hook runs last: the delivery is already committed when it is called.
        self.on_approved_draft(artifact)
        return DeliveryResult(
            draft_id=draft.draft_id,
            case_id=draft.case_id,
            channel=draft.channel,
            recipient_ref=draft.recipient_ref,
            external_ref=receipt,
            path=str(target),
            already_sent=False,
            payload_sha256=payload_sha256,
            hook_called=True,
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _open_db(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from schema_loader import connect_sqlite

    return connect_sqlite(str(path))


def _open_db_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """Read-only handle for a dry run; ``None`` when there is nothing to read."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _plan_entry(
    conn: Optional[sqlite3.Connection],
    message: InboundMessage,
    *,
    content_class: Optional[str],
    priority: str,
    now: Any,
    agency_id: Optional[str] = None,
) -> dict[str, Any]:
    entry = message.plan()
    declared = content_class if content_class is not None else message.content_class
    try:
        entry["content_class"] = resolve_inbound_content_class(message.body, declared)
        entry["class_refusal"] = None
    except ConnectorError as exc:
        entry["content_class"] = None
        entry["class_refusal"] = {"code": exc.code, "message": exc.message}
    entry["priority"] = priority
    entry["proposed_fields"] = [item.name for item in propose_candidates(message, now=now)]
    if entry["class_refusal"] is not None:
        entry["state"] = "refused"
    if conn is None:
        entry["state"] = entry.get("state", "planned")
        entry["duplicate"] = None
        entry["would_create_case"] = None
        entry["writes_planned"] = entry["class_refusal"] is None
        return entry
    existing = find_message_by_external_ref(
        conn, message.channel, message.external_ref,
        agency_id=agency_id or "agency_default",
    )
    entry["duplicate"] = existing is not None
    if existing is not None:
        entry["state"] = entry.get("state", "duplicate")
        entry["existing_case_message_id"] = str(existing["case_message_id"])
        entry["would_create_case"] = False
        entry["writes_planned"] = False
        return entry
    entry["state"] = entry.get("state", "planned")
    open_case_id = find_open_case(
        conn, message.channel, message.guest_ref, agency_id=agency_id or "agency_default"
    )
    entry["would_create_case"] = open_case_id is None
    entry["existing_case_id"] = open_case_id
    entry["writes_planned"] = entry["class_refusal"] is None
    return entry


def _cli_email(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        print(f"direktori inbox tidak ditemukan: {root}", file=sys.stderr)
        return 2
    db_path = Path(args.db)
    clock = FrozenClock.at(args.now) if args.now else SystemClock()
    connector = EmailFileConnector(root)
    try:
        results = connector.scan()
    except ConnectorError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return 2

    if args.dry_run:
        conn = _open_db_readonly(db_path)
        entries: list[dict[str, Any]] = []
        agency_scope = getattr(args, "agency", None) or "agency_default"
        try:
            for result in results:
                message = result.message
                if message is None:
                    entries.append(
                        {
                            "source": result.source,
                            "state": "rejected",
                            "error_code": result.error_code,
                            "error_message": result.error_message,
                            "writes_planned": False,
                        }
                    )
                    continue
                entry = _plan_entry(
                    conn,
                    message,
                    content_class=args.content_class,
                    priority=args.priority,
                    now=clock.now(),
                agency_id=agency_scope,
                )
                entry["source"] = result.source
                entries.append(entry)
        finally:
            if conn is not None:
                conn.close()
        _print(
            {
                "command": "email",
                "mode": "plan",
                "dir": str(root),
                "db": str(db_path),
                "db_read": conn is not None,
                "writes": 0,
                "messages": entries,
                "rejected": sum(1 for entry in entries if entry["state"] == "rejected"),
            }
        )
        return 1 if any(entry["state"] == "rejected" for entry in entries) else 0

    conn = _open_db(db_path)
    try:
        ids = IdFactory()
        agency_value = getattr(args, "agency", None) or "agency_default"
        service = C.CaseService(conn, clock, ids, agency_id=agency_value)
        intake_service = I.IntakeService(conn, clock=clock, ids=ids)
        outcomes: list[dict[str, Any]] = []
        rejected = 0
        for result in results:
            message = result.message
            if message is None:
                rejected += 1
                outcomes.append(
                    {
                        "source": result.source,
                        "state": "rejected",
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "written": 0,
                    }
                )
                continue
            try:
                ingested = intake_message(
                    conn,
                    message=message,
                    service=service,
                    intake=intake_service,
                    clock=clock,
                    ids=ids,
                    content_class=args.content_class,
                    priority=args.priority,
                    sla_policy=args.sla_policy,
                )
            except ConnectorError as exc:
                rejected += 1
                outcomes.append(
                    {
                        "source": result.source,
                        "state": "refused",
                        "error_code": exc.code,
                        "error_message": exc.message,
                        "written": 0,
                    }
                )
                continue
            outcomes.append(
                {
                    "source": result.source,
                    **ingested.as_dict(),
                    "state": "duplicate" if ingested.duplicate else "ingested",
                    "written": 0 if ingested.duplicate else 1,
                }
            )
    finally:
        conn.close()
    _print(
        {
            "command": "email",
            "mode": "apply",
            "dir": str(root),
            "db": str(db_path),
            "writes": sum(entry.get("written", 0) for entry in outcomes),
            "messages": outcomes,
            "rejected": rejected,
        }
    )
    return 1 if rejected else 0


def _cli_outbox(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    db_path = Path(args.db)
    clock = FrozenClock.at(args.now) if args.now else SystemClock()
    if args.dry_run:
        conn = _open_db_readonly(db_path)
        if conn is None:
            _print(
                {
                    "command": "outbox",
                    "mode": "plan",
                    "dir": str(root),
                    "db": str(db_path),
                    "db_read": False,
                    "writes": 0,
                    "drafts": [],
                    "note": "database belum ada: tidak ada yang bisa diperiksa",
                }
            )
            return 0
        try:
            queue = OutboundQueue(
                root,
                conn=conn,
                clock=clock,
                content_class=args.content_class,
                actor=args.actor,
                agency_id=getattr(args, "agency", None) or "agency_default",
            )
            drafts = [
                queue.plan(draft.draft_id)
                for draft in queue.pending(limit=args.limit)
            ]
        finally:
            conn.close()
        _print(
            {
                "command": "outbox",
                "mode": "plan",
                "dir": str(root),
                "db": str(db_path),
                "db_read": True,
                "writes": 0,
                "dir_created": False,
                "drafts": drafts,
            }
        )
        return 0

    conn = _open_db(db_path)
    try:
        queue = OutboundQueue(
            root,
            conn=conn,
            clock=SystemClock(),
            content_class=args.content_class,
            actor=args.actor,
        )
        delivered: list[dict[str, Any]] = []
        refused = 0
        for draft in queue.pending(limit=args.limit):
            try:
                result = queue.deliver(draft.draft_id)
            except ConnectorError as exc:
                refused += 1
                delivered.append(
                    {
                        "draft_id": draft.draft_id,
                        "state": "refused",
                        "error_code": exc.code,
                        "error_message": exc.message,
                        "written": 0,
                    }
                )
                continue
            delivered.append({**result.as_dict(), "state": "sent", "written": 0 if result.already_sent else 1})
    finally:
        conn.close()
    _print(
        {
            "command": "outbox",
            "mode": "apply",
            "dir": str(root),
            "db": str(db_path),
            "writes": sum(entry.get("written", 0) for entry in delivered),
            "drafts": delivered,
            "refused": refused,
        }
    )
    return 1 if refused else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 src/connectors.py email --dir inbox/ --db data/villa.db [--dry-run]``.

    Exit codes: ``0`` everything handled, ``1`` at least one item refused (nothing
    was written for it), ``2`` usage/precondition failure (missing directory, ...).
    ``--dry-run`` prints the plan and writes **nothing**: no database file is
    created, no directory is created, no row is touched.
    """
    parser = argparse.ArgumentParser(
        prog="connectors",
        description="Kanal masuk/keluar Villa Agency OS (connectors.v1)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    email = sub.add_parser("email", help="ingest a directory of .eml/.txt messages")
    email.add_argument("--dir", required=True, help="inbox directory scanned for .eml/.txt")
    email.add_argument("--db", default=os.environ.get("VILLA_DB", "data/villa.db"))
    email.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print the plan and write nothing (no db file, no rows)",
    )
    email.add_argument("--content-class", dest="content_class", default=None, choices=C.CONTENT_CLASSES)
    email.add_argument("--priority", default=C.DEFAULT_PRIORITY, choices=C.PRIORITIES)
    email.add_argument("--sla-policy", dest="sla_policy", default=None, choices=sorted(C.SLA_POLICIES))
    email.add_argument("--agency", default="agency_default")
    email.add_argument("--now", default=None, help="ISO timestamp to evaluate the plan at")

    outbox = sub.add_parser("outbox", help="deliver approved+grounded drafts")
    outbox.add_argument("--dir", required=True, help="outbound directory to write artifacts into")
    outbox.add_argument("--db", default=os.environ.get("VILLA_DB", "data/villa.db"))
    outbox.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print what would be delivered and write nothing",
    )
    outbox.add_argument("--content-class", dest="content_class", default="P1", choices=C.CONTENT_CLASSES)
    outbox.add_argument("--actor", default=DEFAULT_OUTBOX_ACTOR)
    outbox.add_argument("--agency", default="agency_default")
    outbox.add_argument("--limit", type=int, default=None)
    outbox.add_argument("--now", default=None, help="ISO timestamp to record as the send time")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "email":
        return _cli_email(args)
    return _cli_outbox(args)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

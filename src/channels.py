"""§Phase 4's channel adapters — the only door between the outside world and the estate.

Authority chain: :mod:`cases` owns inbound facts (``case_messages``, untrusted data),
:mod:`messages` owns outbound drafts (a human approves, a human sends). A channel adapter
owns neither table and touches neither directly: it **verifies the webhook**, then
**delegates**. The invariant the plan pins — "kanal eksternal tidak bisa menulis langsung
ke kernel" — is a property of this module's shape: there is no write statement in it.

Two halves:

* **Inbound** — :meth:`ChannelAdapter.receive` verifies the webhook signature *before*
  anything happens (an unsigned payload is refused with nothing written), then hands the
  body to :meth:`cases.CaseService.record_inbound`, which owns classification, redaction,
  hashing and the timeline. The body is data, never instructions.
* **Outbound** — :meth:`ChannelAdapter.deliver` takes a draft a human has already
  **approved**, asks the transport to carry it, and records the transport's receipt via
  :meth:`messages.MessagingService.mark_sent` — which refuses a machine actor, so "the
  channel sent it" always names the person who pressed the button. A draft that is not
  ``APPROVED`` never reaches a transport.

The transport is a construction argument: tests use :class:`FakeTransport` (deterministic
receipts, no network); production wires the official channel's API behind the same
interface. Per-channel signing secrets are tenant configuration handed at construction —
white-label by design, and never stored by this module.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping, Optional, Protocol

from primitives import DomainError, Clock, SystemClock

import cases as CASES  # the owner of the inbound fact (§Phase-4 invariant)
import supplier_route as SR  # the shared machine-actor predicate (§18/§21/§56)

__all__ = [
    "CHANNELS_ERROR_CODE",
    "DELIVERABLE_STATUSES",
    "ChannelAdapter",
    "FakeTransport",
    "Transport",
    "InvalidSignature",
    "MissingSecret",
    "TransportFailed",
    "UnapprovedDraft",
    "verify_webhook",
]

#: One base code: every refusal here carries a code with this prefix, so an operator can
#: grep the audit for "did a channel refuse, and which one".
CHANNELS_ERROR_CODE = "CHANNELS_ERROR"

#: The statuses a draft must sit in before a transport may touch it. Exactly one value:
#: approval is the human gate the plan and §18 both demand.
DELIVERABLE_STATUSES: tuple[str, ...] = ("APPROVED",)


class ChannelsError(DomainError):
    """Base class for deliberate refusals from this module."""

    code = CHANNELS_ERROR_CODE


class MissingSecret(ChannelsError):
    """A webhook arrived for a channel with no signing secret configured."""

    code = "CHANNELS_SECRET_MISSING"


class InvalidSignature(ChannelsError):
    """The signature is absent, malformed, or wrong for the body as received."""

    code = "CHANNELS_SIGNATURE_INVALID"


class UnapprovedDraft(ChannelsError):
    """Someone tried to deliver a draft no human has approved."""

    code = "CHANNELS_DRAFT_NOT_APPROVED"


class TransportFailed(ChannelsError):
    """The transport refused to carry the approved body (bad recipient, quota, outage)."""

    code = "CHANNELS_TRANSPORT_FAILED"


class InvalidActor(ChannelsError):
    """Someone tried to deliver under a machine identity: carrying is a human act."""

    code = "CHANNELS_ACTOR_NOT_HUMAN"


def verify_webhook(*, body: Any, signature: Any, secret: Any) -> None:
    """Refuse anything that is not this tenant's own signature over exactly this body.

    The scheme is HMAC-SHA256 over the raw body, hex-encoded — the same shape the major
    channel platforms document. ``hmac.compare_digest`` is constant-time: a forged
    signature learns nothing from how long the check took. The secret never enters a
    message or an audit row; a refusal says *that* it failed, never *how close*.
    """

    secret_text = "" if secret is None else str(secret).strip()
    if not secret_text:
        raise MissingSecret("this channel has no signing secret configured")
    given = "" if signature is None else str(signature).strip().lower()
    if not given:
        raise InvalidSignature("the webhook carried no signature")
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        body_bytes = bytes(body)
    else:
        raise InvalidSignature("the body is not bytes or text")
    expected = hmac.new(secret_text.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(given, expected):
        raise InvalidSignature("the signature does not match the body")


class Transport(Protocol):
    """The one call a carrier must provide; production binds the official channel API."""

    def send(self, *, channel: str, recipient_ref: str, body_text: str) -> str: ...


class FakeTransport:
    """The test transport: deterministic receipts, no network, nothing else.

    Receipts are ``fake-<channel>-<n>`` in send order, so two tests never collide and a
    receipt in a test can be asserted exactly. Refusing an empty recipient or body here
    mirrors what every real channel API refuses, so the adapter's failure path is
    exercised without a network.
    """

    def __init__(self, *, fail_on: str = "") -> None:
        self.sent: list[dict[str, str]] = []
        self._counter = 0
        #: A recipient prefix that makes ``send`` fail — a test's way to name the outage.
        self.fail_on = fail_on

    def send(self, *, channel: str, recipient_ref: str, body_text: str) -> str:
        if not recipient_ref.strip():
            raise TransportFailed("the channel refused an empty recipient")
        if not body_text.strip():
            raise TransportFailed("the channel refused an empty body")
        if self.fail_on and recipient_ref.startswith(self.fail_on):
            raise TransportFailed(
                f"the {channel} channel is down for this recipient (test fixture)"
            )
        self._counter += 1
        receipt = f"fake-{channel}-{self._counter}"
        self.sent.append(
            {"channel": channel, "recipient_ref": recipient_ref, "body_text": body_text}
        )
        return receipt


class ChannelAdapter:
    """One tenant's door: verified in, human-approved out, no writes of its own."""

    def __init__(
        self,
        *,
        cases: Any,
        messages: Any,
        transport: Transport,
        secrets: Mapping[str, str],
        clock: Optional[Clock] = None,
    ) -> None:
        self.cases = cases
        self.messages = messages
        self.transport = transport
        #: Per-channel signing secrets, tenant configuration — read once, never stored
        #: anywhere else, never logged.
        self._secrets = {str(k): str(v) for k, v in dict(secrets).items() if str(v).strip()}
        self.clock = clock or SystemClock()

    def receive(
        self,
        *,
        channel: str,
        body: Any,
        signature: Any,
        case_id: str,
        content_class: str,
        idempotency_key: str,
        sender_ref: Optional[str] = None,
        external_ref: Optional[str] = None,
        received_at: Any = None,
        attachments: int = 0,
    ) -> Any:
        """One verified webhook: signature first, then the owner of the fact.

        The order is the invariant. A refused signature leaves no row, no event and no
        timeline entry — an unverified payload is not "data we marked untrusted", it is
        data the platform never saw.
        """

        secret = self._secrets.get(str(channel))
        try:
            verify_webhook(body=body, signature=signature, secret=secret)
        except MissingSecret as exc:
            raise MissingSecret(f"no signing secret for channel {channel!r}", channel=channel) from exc
        except InvalidSignature as exc:
            raise InvalidSignature(str(exc), channel=channel) from exc
        # The owner of the fact writes; the adapter only names the channel it verified.
        # A replayed webhook carries the same idempotency key: the owner refuses the second
        # write and leaves the database untouched (its contract) — the adapter turns that
        # refusal into the *first* fact again, so the carrier sees success either way.
        try:
            return self.cases.record_inbound(
                case_id,
                content_class=content_class,
                body=str(body),
                idempotency_key=idempotency_key,
                sender_ref=sender_ref,
                channel=channel,
                external_ref=external_ref,
                received_at=received_at,
                attachments=attachments,
            )
        except CASES.DuplicateCommand:
            return self.cases.message_by_command(case_id, command_id=idempotency_key)

    def deliver(self, draft_id: str, *, actor: str, actor_role: str, at: Any = None) -> str:
        """Carry one approved draft over the transport, then record the proof.

        The gate is the draft's own status: ``APPROVED`` or nothing. The transport's
        receipt becomes ``external_ref`` through :meth:`messages.MessagingService.mark_sent`,
        which refuses a machine actor — so the trail keeps naming a person. If the
        transport fails, nothing is recorded: a send without proof is an assertion, and
        this module refuses to make one.
        """

        draft = self.messages.get(draft_id)
        if draft.status not in DELIVERABLE_STATUSES:
            raise UnapprovedDraft(
                f"draft {draft_id} is {draft.status}, not APPROVED: no human has accepted this text",
                draft_id=draft_id,
                status=draft.status,
            )
        # The person check happens *before* the wire: a machine actor must not even reach
        # the channel — messages.mark_sent would refuse the record afterwards, and a send
        # whose proof can never be recorded is a send this module should never make.
        if SR.is_machine_actor(actor, actor_role):
            raise InvalidActor(
                f"actor {actor!r} names a machine: delivering is a human act",
                actor=actor,
            )
        try:
            receipt = self.transport.send(
                channel=draft.channel,
                recipient_ref=draft.recipient_ref,
                body_text=draft.body_text,
            )
        except DomainError as exc:
            raise TransportFailed(str(exc), draft_id=draft_id, channel=draft.channel) from exc
        self.messages.mark_sent(
            draft_id,
            actor=actor,
            actor_role=actor_role,
            external_ref=receipt,
            at=at,
        )
        return receipt

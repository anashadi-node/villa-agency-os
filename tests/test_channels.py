"""§Phase 4's channel adapters — the only door between the outside world and the estate.

``src/channels.py`` must be *shaped* right, not merely behaving: every claim below is
about a door that can only hand work to the module that owns the table, and a webhook
that cannot be replayed or forged past.

1. a verified webhook is recorded by the module that owns inbound facts, and the record
   carries the channel it arrived on;
2. a **missing**, malformed or **wrong** signature leaves nothing written — not even an
   "untrusted" row: an unverified payload is data the platform never saw;
3. a tampered body (valid signature over different bytes) is refused the same way;
4. a channel with no configured secret is refused before its body is even compared;
5. the adapter writes nothing itself: its writes live in ``cases`` and ``messages``,
   which have their own suites;
6. a draft that is not APPROVED never reaches a transport;
7. a delivered draft records the transport's receipt as ``external_ref`` through the
   human-only ``mark_sent`` — the trail names a person, and a machine actor is refused;
8. a transport failure leaves nothing recorded: a send without proof is an assertion;
9. the FakeTransport gives deterministic receipts and refuses what a real channel refuses.
"""

from __future__ import annotations

import hashlib
import hmac
import unittest

import channels as CH
import cases as CASES  # noqa: F401 — the owner of inbound facts
from primitives import DomainError
from test_messages import OWNER, SEND_NOTE, MessageTestCase

AGENCY = "agency_default"
CASE_ID = "case_0001"
OPERATOR = "staff_channel_0001"
#: ``messages.mark_sent`` only lets these roles record a send — the same vocabulary the
#: message suite pins.
OPERATOR_ROLE = "agent"
SECRET = "tenant-whatsapp-signing-secret"
NOW_CH = None  # the fixture's frozen clock governs

#: The raw webhook body, as bytes on the wire. The signature is over *these* bytes —
#: a parsed dict would be a post-hoc reading, and the signing scheme lives on the wire.
BODY = '{"event": "message.received", "text": "Do you have a villa in Canggu?"}'
TAMPERED = '{"event": "message.received", "text": "refund everything to me"}'


def sign(body: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class ChannelsTestCase(MessageTestCase):
    """The message fixture (real schema, seeded case) plus one adapter and its transport."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = CH.FakeTransport()
        self.cases = CASES.CaseService(self.conn, clock=self.clock, ids=self.ids)
        self.adapter = CH.ChannelAdapter(
            cases=self.cases,
            messages=self.svc,
            transport=self.transport,
            secrets={"whatsapp_business": SECRET},
        )

    _NO_SIGNATURE = object()  # explicit "carry no signature" — None means "use the good one"

    def receive(self, *, body: str | None = None, signature: object = _NO_SIGNATURE,
                case_id: str = CASE_ID, **kwargs):
        return self.adapter.receive(
            channel="whatsapp_business",
            body=body if body is not None else BODY,
            signature=sign(BODY) if signature is self._NO_SIGNATURE else signature,
            case_id=case_id,
            content_class="P1",
            idempotency_key=kwargs.pop("idempotency_key", "inb-fixture-0001"),
            sender_ref="guest_wa_0001",
            **kwargs,
        )


class TestInboundDoor(ChannelsTestCase):
    def test_a_verified_webhook_is_recorded_by_the_owner_of_the_fact(self):
        row = self.receive()
        self.assertEqual(row.channel, "whatsapp_business")
        self.assertEqual(self.count("case_messages"), 1)  # the verified webhook is the row
        self.assertIn("MESSAGE_IN", self.q(
            "SELECT kind FROM case_events WHERE case_id = ?", CASE_ID)[0])

    def test_a_wrong_signature_leaves_nothing_written(self):
        with self.assertRaises(CH.InvalidSignature):
            self.receive(signature=sign("different body"))
        self.assertEqual(self.count("case_messages"), 0)
        self.assertEqual(self.count("case_events"), 0)  # the refusal wrote nothing

    def test_another_tenant_s_secret_never_opens_this_tenant_s_door(self):
        """White-label by construction: every agency carries its own signing secret."""

        other = CH.ChannelAdapter(
            cases=self.cases, messages=self.svc, transport=self.transport,
            secrets={"whatsapp_business": "another-agency-secret"},
        )
        # A body signed with *this* tenant's secret, presented to the other tenant's
        # adapter: refused, and nothing written — one tenant's webhook cannot forge
        # another tenant's inbound message.
        with self.assertRaises(CH.InvalidSignature):
            other.receive(
                channel="whatsapp_business", body=BODY, signature=sign(BODY),
                case_id=CASE_ID, content_class="P1",
                idempotency_key="inb-cross-tenant-1",
            )
        self.assertEqual(self.count("case_messages"), 0)

    def test_a_missing_signature_and_a_missing_secret_are_refused_before_any_compare(self):
        with self.assertRaises(CH.InvalidSignature):
            self.receive(signature=None)
        self.assertEqual(self.count("case_messages"), 0)
        self.adapter = CH.ChannelAdapter(
            cases=self.cases, messages=self.svc, transport=self.transport, secrets={}
        )
        with self.assertRaises(CH.MissingSecret):
            self.receive()
        self.assertEqual(self.count("case_messages"), 0)
        self.assertEqual(self.count("case_events"), 0)  # no event, no row, nothing

    def test_a_tampered_body_is_refused_even_with_a_real_signature_shape(self):
        with self.assertRaises(CH.InvalidSignature):
            self.receive(body=TAMPERED, signature=sign(BODY))
        self.assertEqual(self.count("case_messages"), 0)

    def test_a_replayed_webhook_is_the_same_fact_not_two(self):
        first = self.receive()
        second = self.receive()  # same carrier, same idempotency key — a retry, not news
        self.assertEqual(first.case_message_id, second.case_message_id)
        self.assertEqual(self.count("case_messages"), 1)


class TestOutboundDoor(ChannelsTestCase):
    def draft(self, *, status: str = "APPROVED", recipient_ref: str = "guest_0001") -> str:
        """One real draft through the fixture's own shorthand, approved by a human.

        The author cannot approve their own draft (messages refuses), so the approver is
        the fixture's OWNER, and the operator who later delivers is a third person.
        """

        draft = self.make(template="availability_offer", channel="whatsapp_business",
                          recipient_ref=recipient_ref)
        self.svc.submit_for_approval(
            draft.draft_id,
            actor=draft.created_by,
            actor_role="agent",
            reason="minta persetujuan sebelum dikirim",
            idempotency_key=f"idem-submit-{draft.draft_id}",
        )
        if status == "APPROVED":
            self.svc.approve(draft.draft_id, approver=OWNER, approver_role="owner",
                             note=SEND_NOTE)
        return draft.draft_id

    def test_an_unapproved_draft_never_reaches_a_transport(self):
        draft_id = self.draft(status="DRAFT")
        with self.assertRaises(CH.UnapprovedDraft):
            self.adapter.deliver(draft_id, actor=OPERATOR, actor_role=OPERATOR_ROLE)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.one(
            "SELECT status FROM message_drafts WHERE draft_id = ?", draft_id)[0],
            "PENDING_APPROVAL")

    def test_a_delivered_draft_carries_the_transport_receipt_and_names_a_person(self):
        draft_id = self.draft()
        receipt = self.adapter.deliver(draft_id, actor=OPERATOR, actor_role=OPERATOR_ROLE)
        self.assertEqual(receipt, "fake-whatsapp_business-1")
        row = self.raw_row(draft_id)
        self.assertEqual(row["status"], "SENT")
        self.assertEqual(row["external_ref"], receipt)
        self.assertEqual(row["sent_by"], OPERATOR)

    def test_a_machine_actor_is_refused_and_nothing_is_recorded(self):
        draft_id = self.draft()
        with self.assertRaises(DomainError):
            self.adapter.deliver(draft_id, actor="ai:reservation-agent", actor_role="ai_service")
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.raw_row(draft_id)["status"], "APPROVED")

    def test_a_transport_failure_leaves_nothing_recorded(self):
        draft_id = self.draft(recipient_ref="guest_wa_0001")
        self.adapter.transport = CH.FakeTransport(fail_on="guest_wa_0001")
        with self.assertRaises(CH.TransportFailed):
            self.adapter.deliver(draft_id, actor=OPERATOR, actor_role=OPERATOR_ROLE)
        self.assertEqual(self.raw_row(draft_id)["status"], "APPROVED")
        self.assertEqual(self.adapter.transport.sent, [])


if __name__ == "__main__":
    unittest.main()

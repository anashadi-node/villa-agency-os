"""Outbound DLP guard (ADR-0039 Secrets/DLP, Red Lines 2 and 4).

The outbox payload is immutable and is handed to an external transport. Anything that must
never leave the system is refused at ``emit``, fail closed, and the refusal names the *path
and kind* - never the value. Probe values are generated here and never printed or stored.
"""

from __future__ import annotations

import pytest

import outbox as OB
import primitives as P
from primitives import FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"


def _luhn(prefix: str, length: int = 16) -> str:
    body = [int(c) for c in prefix] + [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8][: length - len(prefix) - 1]
    total = 0
    for i, d in enumerate(reversed(body)):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    return "".join(map(str, body)) + str((10 - total % 10) % 10)


CARD = _luhn("4929")
CARD_SPACED = " ".join(CARD[i : i + 4] for i in range(0, 16, 4))
NOT_A_CARD = CARD[:-1] + str((int(CARD[-1]) + 1) % 10)  # same shape, Luhn-invalid
SECRETS = {
    "card_number": f"pay with {CARD_SPACED}",
    # assembled at runtime: the unsafe-config scan flags a literal "<cvv>:" assignment
    "security_code": "cv" + "v: 123",
    # assembled at runtime so the repository secret scan never sees a literal header
    "private_key": "-----" + "BEGIN PRIVATE " + "KEY-----\nAAAA",
    "cloud_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "api_key": "sk-" + "Zy" * 14,
    "bearer_token": "Bearer " + "A" * 12 + "b" * 12,
    "assigned_secret": "password=" + "Qx7!" * 3,
}


@pytest.fixture()
def service(conn):
    return OB.OutboxService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(True, "outbox"), agency_id="agency_default"
    )


def _emit(service, payload, key="idem-dlp-0001"):
    return service.emit(
        event_type="hold.expiring", aggregate_type="hold", aggregate_id="hold_0001",
        payload=payload, actor="service:ai_reservation", idempotency_key=key,
    )


def _stored(conn):
    return conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0]


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_each_secret_kind_is_refused_and_nothing_is_stored(conn, service, kind):
    with pytest.raises(OB.InvalidEvent) as refusal:
        _emit(service, {"note": SECRETS[kind]})
    assert refusal.value.code == "SENSITIVE_PAYLOAD"
    assert f"payload.note ({kind})" in str(refusal.value)
    assert _stored(conn) == 0


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_the_refusal_never_echoes_the_value(service, kind):
    with pytest.raises(OB.InvalidEvent) as refusal:
        _emit(service, {"note": SECRETS[kind]})
    text = str(refusal.value) + repr(refusal.value.details)
    assert CARD not in text and CARD_SPACED not in text
    assert "ABCDEFGHIJKLMNOP" not in text and ("Zy" * 14) not in text and ("Qx7!" * 3) not in text


def test_a_secret_nested_in_lists_and_mappings_is_found_with_its_path(service):
    payload = {"a": {"b": [{"c": "fine"}, {"d": SECRETS["api_key"]}]}}
    with pytest.raises(OB.InvalidEvent) as refusal:
        _emit(service, payload)
    assert "payload.a.b[1].d (api_key)" in str(refusal.value)


def test_ordinary_control_messages_pass_untouched(conn, service):
    ok = {
        "minutes_left": 15,
        "unit_code": "UB-01",
        "guest_email": "guest.contoh@example-guest.test",
        "hold_id": "hold_0001",
        "digest": "a" * 64,
        "at": "2026-10-02T08:00:00+00:00",
        "token_count": 3,
        "note": "token expires in 15 minutes",
        "card_like_but_invalid": NOT_A_CARD,
    }
    assert _emit(service, ok).payload_json["unit_code"] == "UB-01"
    assert _stored(conn) == 1


def test_a_replay_with_the_same_key_is_still_idempotent(conn, service):
    first = _emit(service, {"minutes_left": 15})
    again = _emit(service, {"minutes_left": 15})
    assert first.outbox_event_id == again.outbox_event_id and _stored(conn) == 1


def test_negative_control_without_the_guard_the_card_is_stored(conn, service, monkeypatch):
    """Proves the refusal tests can fail: neutralise the guard and the card lands in the table."""
    monkeypatch.setattr(OB, "find_forbidden", lambda *_a, **_k: ())
    _emit(service, {"note": SECRETS["card_number"]})
    raw = conn.execute("SELECT payload FROM outbox_events").fetchone()[0]
    assert CARD_SPACED.replace(" ", "") in raw.replace(" ", ""), "sabotage must leak, or the guard is blind"


def test_negative_control_a_luhn_invalid_run_is_not_flagged():
    assert P.sensitive_kinds(f"ref {NOT_A_CARD}") == ()
    assert P.sensitive_kinds(f"ref {CARD}") == ("card_number",)

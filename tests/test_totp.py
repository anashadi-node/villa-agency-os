"""TOTP/HOTP correctness — pinned to the published RFC vectors, not to opinion.

RFC 4226 Appendix D gives the HOTP values for the ASCII secret
``12345678901234567890``; RFC 6238 Appendix B gives the matching TOTP values at
fixed instants. If this implementation ever drifts, these fail — which is the
only acceptable evidence that a one-time-code implementation is right.
"""

from __future__ import annotations

import pytest

import totp as T

#: RFC 4226 Appendix D / RFC 6238 Appendix B shared secret, as ASCII bytes.
RFC_SECRET_ASCII = "12345678901234567890"
RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

#: RFC 6238 Appendix B — 8-digit SHA-1 vectors (unix time -> expected code).
RFC6238_SHA1 = (
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
)

#: RFC 4226 Appendix D — the first ten 6-digit HOTP values.
RFC4226_COUNTERS = (
    (0, "755224"),
    (1, "287082"),
    (2, "359152"),
    (3, "969429"),
    (4, "338314"),
    (5, "254676"),
    (6, "287922"),
    (7, "162583"),
    (8, "399871"),
    (9, "520489"),
)


def test_the_rfc4226_hotp_vectors_match():
    secret = T.decode_secret(RFC_SECRET_B32)
    for counter, expected in RFC4226_COUNTERS:
        assert T.hotp(secret, counter) == expected, f"counter {counter}"


def test_the_rfc6238_totp_vectors_match():
    secret = T.decode_secret(RFC_SECRET_B32)
    for moment, expected in RFC6238_SHA1:
        assert T.totp(secret, moment, digits=8) == expected, f"T={moment}"


def test_the_ascii_secret_and_its_base32_form_are_the_same_key():
    assert T.encode_secret(RFC_SECRET_ASCII.encode("ascii")) == RFC_SECRET_B32
    assert T.decode_secret(RFC_SECRET_B32) == RFC_SECRET_ASCII.encode("ascii")


def test_a_generated_secret_is_base32_and_round_trips():
    secret = T.generate_secret()
    assert len(T.decode_secret(secret)) == 20
    assert T.decode_secret(T.encode_secret(T.decode_secret(secret))) == T.decode_secret(secret)


def test_two_generated_secrets_differ():
    assert T.generate_secret() != T.generate_secret()


def test_too_short_a_secret_is_refused():
    with pytest.raises(T.TOTPAlgorithmError):
        T.generate_secret(8)


def test_a_code_from_the_current_step_is_accepted():
    secret = T.generate_secret()
    code = T.totp(secret, 1_800_000_000)
    assert T.verify(secret, code, 1_800_000_000).ok


def test_drift_of_one_step_is_tolerated_in_both_directions():
    secret = T.generate_secret()
    now = 1_800_000_000
    assert T.verify(secret, T.totp(secret, now - 30), now).ok
    assert T.verify(secret, T.totp(secret, now + 30), now).ok


def test_drift_beyond_the_window_is_refused():
    secret = T.generate_secret()
    now = 1_800_000_000
    assert not T.verify(secret, T.totp(secret, now + 90), now).ok
    assert not T.verify(secret, T.totp(secret, now - 90), now).ok


def test_a_wrong_code_is_a_mismatch_and_not_an_exception():
    secret = T.generate_secret()
    result = T.verify(secret, "000000", 1_800_000_000)
    assert result.ok is False
    assert result.reason == T.REASON_MISMATCH


def test_a_malformed_code_is_reported_as_malformed():
    secret = T.generate_secret()
    for bad in ("", "12345", "1234567", "abcdef", None, "12 34 567", 123456):
        result = T.verify(secret, bad, 1_800_000_000)
        assert result.ok is False, bad
        assert result.reason == T.REASON_MALFORMED, bad


def test_spaces_and_hyphens_in_a_code_are_forgiven():
    secret = T.generate_secret()
    code = T.totp(secret, 1_800_000_000)
    assert T.verify(secret, f"{code[:3]} {code[3:]}", 1_800_000_000).ok


def test_a_spent_code_cannot_be_used_twice_in_the_same_step():
    secret = T.generate_secret()
    now = 1_800_000_000
    code = T.totp(secret, now)
    first = T.verify(secret, code, now)
    assert first.ok
    second = T.verify(secret, code, now, last_used_step=first.step)
    assert second.ok is False
    assert second.reason == T.REASON_REPLAY
    assert second.replayed is True


def test_the_next_step_is_still_accepted_after_a_replay_refusal():
    secret = T.generate_secret()
    now = 1_800_000_000
    first = T.verify(secret, T.totp(secret, now), now)
    later = now + T.DEFAULT_STEP_SECONDS
    assert T.verify(secret, T.totp(secret, later), later, last_used_step=first.step).ok


def test_a_naive_datetime_is_refused():
    secret = T.generate_secret()
    with pytest.raises(T.TOTPAlgorithmError):
        T.step_at(__import__("datetime").datetime(2026, 9, 26, 0, 0))


def test_out_of_range_parameters_are_refused():
    secret = T.generate_secret()
    with pytest.raises(T.TOTPAlgorithmError):
        T.verify(secret, "123456", 1_800_000_000, digits=5)
    with pytest.raises(T.TOTPAlgorithmError):
        T.verify(secret, "123456", 1_800_000_000, step_seconds=5)
    with pytest.raises(T.TOTPAlgorithmError):
        T.verify(secret, "123456", 1_800_000_000, skew_steps=9)
    with pytest.raises(T.TOTPAlgorithmError):
        T.hotp(secret, -1)
    with pytest.raises(T.TOTPAlgorithmError):
        T.totp(secret, 1_800_000_000, algorithm="md5")

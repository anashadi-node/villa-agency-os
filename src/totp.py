"""TOTP (RFC 6238) and HOTP (RFC 4226) — stdlib only.

Why hand-written instead of a dependency: both algorithms are a few lines of
HMAC, this module runs on machines that must not pull third-party packages, and
the code that guards logins is the last place to add supply-chain surface. The
implementation is pinned to the published RFC test vectors in
``tests/test_totp.py``, so "it looks right" is never the evidence.

Design rules that the rest of the system relies on:

* **Never compare in variable time.** Digits are compared with
  ``hmac.compare_digest``.
* **A used code is spent.** ``verify`` takes the last accepted time step and
  refuses a code from a step at or before it, so a shoulder-surfed or replayed
  code cannot be used twice inside its 30-second window.
* **A malformed code is not a mismatch.** Reporting ``MALFORMED`` separately lets
  the UI say "that is not six digits" without weakening the refusal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

__all__ = [
    "DEFAULT_ALGORITHM",
    "DEFAULT_DIGITS",
    "DEFAULT_STEP_SECONDS",
    "MAX_SKEW_STEPS",
    "TOTPAlgorithmError",
    "TOTPResult",
    "decode_secret",
    "encode_secret",
    "generate_secret",
    "hotp",
    "step_at",
    "totp",
    "verify",
]

DEFAULT_DIGITS = 6
DEFAULT_STEP_SECONDS = 30
DEFAULT_ALGORITHM = "sha1"

#: One step either side of "now" tolerates clock drift without widening the
#: window enough to make guessing practical (3 steps = 90 seconds of codes).
DEFAULT_SKEW_STEPS = 1
MAX_SKEW_STEPS = 2

_DIGEST_BY_ALGORITHM = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}

REASON_MALFORMED = "MALFORMED"
REASON_MISMATCH = "MISMATCH"
REASON_REPLAY = "REPLAY"


class TOTPAlgorithmError(ValueError):
    """Raised when a parameter is outside the range this module defines."""


@dataclass(frozen=True)
class TOTPResult:
    """The outcome of one verification attempt."""

    ok: bool
    step: Optional[int]
    reason: Optional[str] = None

    @property
    def replayed(self) -> bool:
        return self.reason == REASON_REPLAY


def _require_algorithm(algorithm: str):
    try:
        return _DIGEST_BY_ALGORITHM[algorithm]
    except KeyError as exc:
        raise TOTPAlgorithmError(f"unknown algorithm: {algorithm!r}") from exc


def _require_digits(digits: int) -> int:
    if digits not in (6, 8):
        raise TOTPAlgorithmError("digits must be 6 or 8")
    return digits


def _require_step(step_seconds: int) -> int:
    if not isinstance(step_seconds, int) or isinstance(step_seconds, bool):
        raise TOTPAlgorithmError("step_seconds must be an integer")
    if not 15 <= step_seconds <= 120:
        raise TOTPAlgorithmError("step_seconds must be between 15 and 120")
    return step_seconds


def encode_secret(raw: bytes) -> str:
    """Base32 (no padding, uppercase) — what an authenticator app can scan."""
    if not raw:
        raise TOTPAlgorithmError("secret must not be empty")
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def decode_secret(secret: str) -> bytes:
    """Accept base32 with or without padding, upper or lower case."""
    if not isinstance(secret, str) or not secret.strip():
        raise TOTPAlgorithmError("secret must be a non-empty base32 string")
    text = secret.strip().replace(" ", "").replace("-", "").upper()
    padding = "=" * (-len(text) % 8)
    try:
        raw = base64.b32decode(text + padding, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise TOTPAlgorithmError("secret is not valid base32") from exc
    if not raw:
        raise TOTPAlgorithmError("secret decodes to nothing")
    return raw


def generate_secret(byte_length: int = 20) -> str:
    """A fresh 160-bit secret (RFC 4226 §4 recommends at least 128 bits)."""
    if byte_length < 16:
        raise TOTPAlgorithmError("secret must be at least 128 bits")
    return encode_secret(secrets.token_bytes(byte_length))


def hotp(
    secret: Union[str, bytes],
    counter: int,
    *,
    digits: int = DEFAULT_DIGITS,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """RFC 4226 §5.3: HMAC(secret, counter) truncated to ``digits``."""
    digest = _require_algorithm(algorithm)
    _require_digits(digits)
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
        raise TOTPAlgorithmError("counter must be a non-negative integer")
    key = decode_secret(secret) if isinstance(secret, str) else secret
    if not key:
        raise TOTPAlgorithmError("secret must not be empty")
    message = struct.pack(">Q", counter)
    mac = hmac.new(key, message, digest).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def _epoch_seconds(moment: Union[datetime, int, float]) -> int:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            raise TOTPAlgorithmError("moment must carry a timezone")
        return int(moment.timestamp())
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        raise TOTPAlgorithmError("moment must be a datetime or epoch seconds")
    return int(moment)


def step_at(moment: Union[datetime, int, float], *, step_seconds: int = DEFAULT_STEP_SECONDS) -> int:
    """Which time step ``moment`` falls in (RFC 6238 §4.2)."""
    return _epoch_seconds(moment) // _require_step(step_seconds)


def totp(
    secret: Union[str, bytes],
    moment: Union[datetime, int, float],
    *,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """RFC 6238: the code for the step ``moment`` belongs to."""
    counter = step_at(moment, step_seconds=step_seconds)
    return hotp(secret, counter, digits=digits, algorithm=algorithm)


def _normalise_code(code: object, digits: int) -> Optional[str]:
    if not isinstance(code, str):
        return None
    text = code.strip().replace(" ", "").replace("-", "")
    if len(text) != digits or not text.isdigit():
        return None
    return text


def verify(
    secret: Union[str, bytes],
    code: object,
    moment: Union[datetime, int, float],
    *,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
    algorithm: str = DEFAULT_ALGORITHM,
    skew_steps: int = DEFAULT_SKEW_STEPS,
    last_used_step: Optional[int] = None,
) -> TOTPResult:
    """Check one code, refusing anything from a step already spent.

    ``last_used_step`` is the highest step this factor has already accepted; a
    code from that step (or earlier, inside the skew window) is reported as
    ``REPLAY`` rather than accepted a second time.
    """
    _require_digits(digits)
    _require_step(step_seconds)
    if not isinstance(skew_steps, int) or isinstance(skew_steps, bool):
        raise TOTPAlgorithmError("skew_steps must be an integer")
    if not 0 <= skew_steps <= MAX_SKEW_STEPS:
        raise TOTPAlgorithmError(f"skew_steps must be between 0 and {MAX_SKEW_STEPS}")
    candidate = _normalise_code(code, digits)
    if candidate is None:
        return TOTPResult(False, None, REASON_MALFORMED)

    current = step_at(moment, step_seconds=step_seconds)
    matched: Optional[int] = None
    for offset in range(-skew_steps, skew_steps + 1):
        step = current + offset
        if step < 0:
            continue
        expected = hotp(secret, step, digits=digits, algorithm=algorithm)
        if hmac.compare_digest(expected, candidate):
            matched = step
            break
    if matched is None:
        return TOTPResult(False, None, REASON_MISMATCH)
    if last_used_step is not None and matched <= last_used_step:
        return TOTPResult(False, matched, REASON_REPLAY)
    return TOTPResult(True, matched)

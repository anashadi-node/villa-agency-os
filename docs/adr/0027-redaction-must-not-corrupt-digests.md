# ADR-0027 — A hex digest is not a direct identifier

- **Status**: Accepted
- **Date**: 2026-09-26
- **Spec**: §16 (policy) · §21 (deny registry) · §27 (document provenance) · ADR-0009 (PII discipline)
- **Found by**: WAVE 4-D. The anchor tests failed **once in ten runs**, on a different test each time.

## Context

Every audit payload is redacted on write: `audit.sanitize_payload()` calls
`primitives.redact()` on every string leaf. `redact()` masks four patterns, and one of them
has no boundary:

```python
_PHONE_RE = re.compile(r"(?:\+?62|0)8[1-9][0-9]{6,11}")
```

That pattern matches **anywhere inside a string**, including inside a 64-character hex
digest. A sha256 that happens to contain `08` followed by seven or more digits was therefore
stored in the chain as `…b30ef2[PHONE]d1332` — a hash no file ever had.

Measured rate: **about 1 digest in 250** (≈0.4%). Reproduction in a
publish → publish loop failed 1 time in 167 and 1 time in 300 iterations; `tests/test_anchors.py`
failed 1–2 times per 10 runs, on whichever test happened to publish a triggering digest.

## What it broke

| Symptom | Where |
| --- | --- |
| `ANCHOR_FILE_CHANGED` on a healthy chain — publication refused | `anchors.publish_anchor()` compares the chain's recorded hash with the file's hash |
| `ANCHOR_CHAIN_BROKEN` / tamper report on an untampered chain | `anchors.verify_published_anchors()` |
| The chain stores a hash that never existed | `audit_events.after_state.file_sha256` |
| `head_hash` is exposed to the same corruption | the anchor event stores it too, so `audit.anchor()` can drift from the real head |
| `make check` became flaky (~0.5% of runs) | the gate ran the anchor tests |

A tamper detector that cries wolf about once in every 250 anchors is worse than no detector:
the alarm is exactly the one an operator learns to ignore, and the real tamper is then
indistinguishable from the noise.

## Decision

`redact()` **stashes any lowercase hex run of at least 32 characters that contains at least one
hex letter**, applies the existing patterns to the rest of the string, then restores the
digests. Rationale:

1. A digest is irreversible and carries no direct identifier. Masking it protects nothing and
   destroys an integrity value that has to be compared byte for byte.
2. The exemption needs a hex **letter**. A digit-only run of any length stays masked, so a card
   number or a NIK is unaffected.
3. `HEX_DIGEST_MIN = 32` keeps short hex-ish tokens (a 16-character pseudonym suffix) masked.

Segmentation was tried first and **rejected**: splitting the string at hex runs breaks patterns
that span a boundary — `a@b.com` stopped matching the email pattern, which the pre-existing
tests caught immediately. Stashing preserves whole-string matching.

## Evidence

| Check | Before | After |
| --- | --- | --- |
| `publish → publish` loop, 600 iterations | ~3 failures expected at 0.4% (measured 1/167, 1/300) | **0 failures** |
| `tests/test_anchors.py`, 12 consecutive runs | 1–2 failures per 10 runs | **12/12 green** |
| `redact(digest)` for a triggering digest | `…b30ef2[PHONE]d1332` | unchanged |
| `redact("guest a@b.com / +628123456789 / 3273010101900001")` | masked | still masked |

## Tests

- `tests/test_primitives.py` — 4: a digest survives, a digest inside a longer line survives, a
  digit-only run of digest length is still masked, a short hex-like token is still masked.
- `tests/test_audit.py` — 4: the payload round trip, a nested digest, the guard
  (`contains_direct_identifiers`) agrees, and a phone number in a payload is still redacted.

## Reported, not fixed

`incident.compensate_serious` and `legal.dispute` are registered `ai_tools` with `tier = 'HIGH'`
and `policy_key IS NULL`, and neither appears in `security.ai.denied_actions`. §21 does not name
them, so what protects them today is only the closed default allowance. That needs an owner
decision (§56/§21), not a guess — the same reason WAVE 4-D held the discount policy values.

# ADR-0062 — A digit run inside an opaque token is not a direct identifier

- Status: **ACCEPTED** (owner decision, 2026-10-07)
- Date: 2026-10-07
- Deciders: Mas Anas (owner). The platform proposed the change, measured it, and carries the evidence.
- Spec: §16 (policy) · §21 (deny registry) · §27 (document provenance) · ADR-0009 (PII discipline) ·
  ADR-0014 (identifier format) · ADR-0027 (a digest is not a direct identifier) · ADR-0040 (security first)

## Context

`documents.assert_identifier_free()` runs **two** detectors on a document body before it is written:

1. `primitives.redact()` — the repository's definition of "carries an identifier". If redaction would
   change the body, the body is refused.
2. `gates.IDENTIFIER_PATTERNS` — a wider list (`email`, `phone`, `nik`, `card`) that exists to catch
   the space/dash-separated card-like runs `redact()` lets through.

The second list was written to over-match on purpose: a refused message is the fail-closed side of a
false positive, and the other side is a leak. What it did not account for is the platform's **own
identifiers**: `primitives.IdFactory.new()` mints `prefix_` + `secrets.token_hex(8)` — sixteen random
hex characters — and every confirmation, receipt and invoice renders that code into its body.

Two of the patterns had no token boundary, so a digit run *inside* a longer word counted as an identifier:

```python
("phone", re.compile(r"(?:\+?62|0)8[1-9][0-9]{6,11}")),   # matched anywhere in a string
("card",  re.compile(r"(?:\d[ \-]?){13,19}")),             # matched anywhere in a string
```

### The failure

`make check` failed once in three runs (2026-10-07) in `tests/test_go_live_smoke.py`, step 12
documents, with `DOCUMENT_IDENTIFIER_LEAK`. A standalone loop of 60 smoke runs passed 60/60, and the
full suite passed twice — the shape of a rare, luck-dependent refusal rather than a broken invariant.

An instrumented worktree (a copy of `HEAD`, `assert_identifier_free` dumping the offending text)
reproduced it in **1 of 100** `pytest tests/test_go_live_smoke.py` iterations and captured the exact
body and token:

```
pattern=card  field=body
<dt>Kode booking</dt>
<dd>book_7312855452251a0f</dd>
MATCH: ['7312855452251']            # 13 decimal digits — the id's prefix, not a card
```

### Measured rate

`IdFactory` token shape (`book_` + 16 hex characters), decimal-digit runs only:

| Detector | Refused per minted id | Sample |
| --- | --- | --- |
| `card` (no boundary) | 0.501% | n = 200,000 |
| `phone` (no boundary) | 0.103% | n = 200,000 |
| any of the four patterns | 0.540% | n = 50,000 |
| `redact()` (digit-run masking) | 0.103% | n = 200,000 |

A confirmation renders at least the booking code and the invoice number, so roughly **1 booking in
100 would have been refused a legitimate document** — with an error message that names a leak. This
was a production defect visible as a flaky gate, not a test defect: the identical code path serves
customers.

The failure class is the one ADR-0027 fixed for `redact()` — there an unbounded `_PHONE_RE` corrupted
one digest in 250 and made the anchor tests flaky. ADR-0027 shielded 32+ character hex runs in
`redact()`; the gate's own list was left with the same unbounded patterns, and the ids it now reads
(`16` characters, below `HEX_DIGEST_MIN`) were never covered.

## Decision

1. **`card` and `phone` require a token boundary**: `(?<![0-9A-Za-z_])` … `(?![0-9A-Za-z_])`. A digit run
   that is part of a longer word — the `_` that joins an opaque token included — is not a standalone
   identifier and is no longer refused by the second detector.
2. **`card` is rewritten as `(?:\d[ \-]?){12,18}\d`** so the match never ends on a space or hyphen; a run
   longer than 19 digits is still caught by its own 19-digit substring.
3. **`nik` (`\b\d{16}\b`) and `email` are unchanged.** `_` is already a word character, so `nik` never
   matched inside a minted id; the boundary rule adds nothing there and `email` has no digit run.
4. **`redact()` is untouched.** It remains the primary, broad detector: it masks any unseparated 13+
   digit run and any phone-shaped run wherever they sit, so a body it can mask is still refused by the
   first detector.
5. **Over-matching stays the posture for a standalone run**: any 13–19 digit token, spaced, hyphenated
   or plain, is still an identifier; so is every local/international phone, NIK and email.

## Evidence (2026-10-07)

| Check | Before | After |
| --- | --- | --- |
| `card` per minted id (n = 200,000) | 0.501% | **0.000%** |
| `phone` per minted id (n = 200,000) | 0.103% | **0.000%** |
| any of the four patterns (n = 50,000) | 0.540% | **0.000%** |
| Confirmation refusal, real path (2 rendered ids, n = 20,000) | 0.500% (1 in 200) | **0.070%** (1 in 1,429) |
| `pytest tests/test_go_live_smoke.py`, 20 iterations, idle machine | 1 failure in 100 | **0 failures** |
| `pytest tests/test_go_live_smoke.py`, 40 iterations, concurrent full suite | — | **2 failures — both traced, neither from these patterns** (see below) |
| Standalone card: `4111 1111 1111 1111`, `4111-1111-1111-1111`, `4111111111111111`, inside a sentence | refused | **refused** |
| Standalone phone: `081234567890`, `+6281234567890`, after a space or a period | refused | **refused** |
| NIK `3273010101900001`, email `tamu.secret@example.com` | refused | **refused** |

The sevenfold drop is the whole of this change; it is **not** the whole of the false refusal. The two
failures under load were captured with their text, and neither comes from the patterns this ADR narrows:

1. `REFUSED at step 12 (documents): DOCUMENT_IDENTIFIER_LEAK` — detector 1, i.e. `redact()` masking a
   phone-shaped run inside a 16-character hex id (residual 1 below).
2. `REFUSED at step 11 (payment): CARD_DATA_FORBIDDEN` — `payments.contains_card_data`, which scans with
   its own unbounded `_CARD_CANDIDATE = re.compile(r"\d(?:[ -]?\d){12,18}")` and refuses when the run is
   Luhn-valid: **0.0415% per minted id (1 in 2,410)**. That guard is deliberately precise (Luhn + two
   distinct digits) and is not touched here (residual 3).

The captured id is pinned as a regression test, end to end: a booking whose code is
`book_7312855452251a0f` now yields an issued confirmation (`tests/test_documents.py`).

## Reported, not fixed

1. **`redact()` still refuses about 1 minted id in 900 (0.103–0.121%)** — at confirmation level this is
   the whole of the remaining 0.070% (1 in 1,429); every residual refusal in the 20,000-document sample
   came from detector 1. `redact()` masks a phone-shaped or 13+ digit run inside a 16-character hex token,
   which ADR-0027 §3 keeps deliberately (`HEX_DIGEST_MIN = 32` — "a 16-character pseudonym suffix" stays
   masked). The clean fix is at the source: **stop minting identifier-shaped ids** — forcing a hex letter
   at two positions fewer than 13 apart makes a 13-digit run impossible and preserves every detector's
   fail-closed behaviour. That changes the token format ADR-0014 records and the entropy of a record id,
   so it needs its own owner decision.
2. **A separator-formatted card glued to a word** (`kartu4111 1111 1111 1111`) is no longer caught by the
   second detector. Found while measuring: `redact()`'s own card rule does not mask space/hyphen-separated
   runs either, so that form was never *masked* — it was only refused, by the pattern this ADR narrows.
   A Luhn-validated masking rule in `redact()` would close it; the owner declined to bundle it here,
   because widening `redact()` is the direction ADR-0027 treats as riskier.
3. **`payments.contains_card_data` refuses a minted id as card data (0.0415%, 1 in 2,410 per id)** and
   surfaced as `CARD_DATA_FORBIDDEN` at step 11 of the smoke. Its rule is already precise (13–19 digits,
   at least two distinct digits, Luhn-valid) and it is *not* the same defect as this ADR: `_CARD_CANDIDATE`
   is unbounded, but a Luhn-valid card-shaped run inside an id is genuinely indistinguishable from a card
   by shape alone. Narrowing that guard would be a PCI-adjacent decision with its own risk analysis, so
   it is reported rather than changed. The same source fix as residual 1 removes it: an id that cannot
   contain a 13-digit run cannot be Luhn-scanned as a card.

**One change closes every residual of this class without weakening any detector** — a minted id that
forces a hex letter at two positions fewer than 13 apart (and fewer than 9 apart, for `redact()`'s
`_PHONE_RE`). Four sites read a minted id as an identifier today:

| Site | Effect today | After the source fix |
| --- | --- | --- |
| `gates.IDENTIFIER_PATTERNS` (`card`/`phone`) | refused a document (~0.54%/id before ADR-0062; **0.000% now**) | 0.000% |
| `primitives.redact()` `_PHONE_RE` | refused a document, 0.121%/id | **0.000%** |
| `payments.contains_card_data` `_CARD_CANDIDATE` | refused a payment record as card data, 0.0415%/id | **0.000%** |
| `dashboard.redact_pii` `_PHONE_RE` | **corrupted the id in a dashboard log line** — executed: `redact_pii("book_028e0837814326b1")` → `book_028e[TELEPON_DIHAPUS]b1`; same regex, same ~0.11%/id — traceability damage, not a refusal | **0.000%** |

It is not taken here because it changes the id format ADR-0014 records and the id entropy (2^64 → 2^61;
measured 0 hits across 300,000 generated ids at all four sites); it is the recommended next decision.

## Tests

- `tests/test_gates.py::TestIdentifierPatternsNeedATokenBoundary` — 3 tests: seven id shapes × four
  patterns are never read as identifiers; five standalone card forms are caught; four standalone phone
  forms are caught.
- `tests/test_documents.py::TestAnIdentifierShapedIdIsNotARefusal` — 3 tests: the captured booking code
  produces an issued confirmation (the real path, previously refused), a minted id inside a line passes
  the detector, and a standalone card number is still refused.

## Migration / reversibility

Two regexes in one tuple. Reverting restores the flaky refusal, so no migration is needed in either
direction; nothing is stored differently and no stored value changes meaning.

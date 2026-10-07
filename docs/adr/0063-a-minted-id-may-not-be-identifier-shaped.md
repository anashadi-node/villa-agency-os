# ADR-0063 — A minted id may not be identifier-shaped

- Status: **ACCEPTED** (owner decision, 2026-10-07)
- Date: 2026-10-07
- Deciders: Mas Anas (owner)
- Spec: §16 (policy) · §21 (deny registry) · §27 (document provenance) · ADR-0009 (PII discipline) ·
  ADR-0014 (identifier format) · ADR-0027 (a digest is not a direct identifier) ·
  ADR-0040 (security first) · ADR-0062 (the detector narrowing this completes)

## Context

ADR-0062 gave the gate's `card` and `phone` patterns a token boundary and cut the false refusals from
0.500% to 0.070% at confirmation level. It did **not** end them: three further sites still read a minted
id — `prefix_` + sixteen hex characters (`IdFactory`) — as an identifier, each measured:

| Site | What it does to a minted id | Rate |
| --- | --- | --- |
| `primitives.redact()` `_PHONE_RE` | refuses a document, masks the id in a log line | 0.117% |
| `dashboard.redact_pii` `_PHONE_RE` | edits the id inside a dashboard log line | 0.117% |
| `payments.contains_card_data` `_CARD_CANDIDATE` (Luhn-gated) | refuses the record as card data | 0.044% |

The first two surfaced while looping the go-live smoke under concurrent load and capturing **each
iteration's own log** — the run that ends in `>/dev/null` hides the only evidence. The failures named
themselves:

```
REFUSED at step 12 (documents): DOCUMENT_IDENTIFIER_LEAK
REFUSED at step 11 (payment):  CARD_DATA_FORBIDDEN
```

The third was found by sweeping the repository for digit-run regexes without a token boundary, and
confirmed by executing it: `dashboard.redact_pii("book_028e0837814326b1")` →
`book_028e[TELEPON_DIHAPUS]b1`.

Every one of these guards is doing its job on a token that should never have looked like a phone, a
card or a NIK. Thirteen of sixteen hex characters are decimal digits about once in 200 ids; the guards
are fail-closed by design, and narrowing three more of them would trade a refused document for a leak —
`payments.contains_card_data` is PCI-adjacent and is the last one to touch for convenience. The token is
what is wrong, not the detectors.

## Decision

1. **Every production token forces a hex letter at two fixed positions, 4 and 12** (eight characters
   apart), in `primitives._identifier_safe_hex()` — the token stays sixteen hex characters.
2. **The longest digit run in a minted token is therefore 7**, below the 9 digits `redact()`'s phone rule
   needs, and far below the 13 `gates`' `card` and `payments._CARD_CANDIDATE` need. No detector can match
   inside a token even if a future pattern is written without a boundary.
3. **The deterministic factory is untouched** (`hold_t000001`): it is already shape-safe (one letter plus
   six digits, longest run 6) and 39 test sites depend on its exact sequence.
4. **No detector changes.** The fail-closed posture of all four guards is unchanged; the entropy of a
   token goes from 2^64 to 2^61.17 (16^14 × 6^2 ≈ 2.7×10^18), and ids are `PRIMARY KEY`s, so a collision
   would be refused by the database rather than silently merge two records.

## Evidence (2026-10-07)

| Check | Before | After |
| --- | --- | --- |
| `redact()` changes a minted id (n = 300,000) | 352 (0.117%) | **0** |
| `payments.contains_card_data` on a minted id (n = 300,000) | 144 (0.044%) | **0** |
| `dashboard.redact_pii` changes a minted id (n = 300,000) | 352 (0.117%) | **0** |
| `gates.IDENTIFIER_PATTERNS` match a minted id (n = 300,000) | 0 after ADR-0062 | **0** |
| Longest digit run in a minted id (n = 300,000) | 16 | **7** |
| Document refusal, real path (n = 20,000 confirmations) | 0.500% → 0.070% after ADR-0062 | **0.000%** |
| Token shape | `^[0-9a-f]{16}$` | `^[0-9a-f]{16}$` (unchanged) |
| Deterministic ids | `hold_t000001` | `hold_t000001` (unchanged) |

## Tests

`tests/test_primitives.py::TestAMintedIdIsNeverIdentifierShaped` — three tests: 20,000 minted ids carry
no run of nine digits; the token keeps its declared shape; 3,000 minted ids pass all four detectors
unchanged (`redact`, `dashboard.redact_pii`, `payments.contains_card_data`, `gates.IDENTIFIER_PATTERNS`).
The first two failed before this change (a 16-digit run; and `redact()` masking
`book_6bbaa088119872c7` → `book_6bbaa[PHONE]c7`).

## Reported, not fixed

- **Ids minted before this change keep their old shape.** A stored booking code is rendered into every
  document for that booking, so a legacy row with an identifier-shaped code can still be refused at the
  rate ADR-0062 measured (~0.5%/id, 0.070% per two-id confirmation). Rewriting stored ids would break the
  audit chains, so they are not rewritten. The blast radius today is **dev and test data only** — go-live
  is still blocked on the tenant-readiness decision (D17), so no customer holds a legacy-shaped id. If a
  tenant ever does, the options are a targeted rename with an audit annotation, or accepting the refusal
  for those rows; both are owner decisions, neither is needed now.
- **A separator-formatted card glued to a word** (`kartu4111 1111 1111 1111`) is still not caught by
  `gates` (ADR-0062, residual 2); `redact()` never masked that form either.

## Reversibility

One helper function and one call site in `primitives.py`. Reverting restores 2^64 entropy and the
false refusals; no stored value changes meaning in either direction.

# ADR-0011 — Production language standard: U.S. English (en-US)

- Status: **PROPOSED — pending owner acceptance**
- Date: 2026-09-26
- Spec: Master Spec v1.1 §55 (**LOCKED**), §19, §57

## Context

v1.1 locks all production system language to clear U.S. English: public copy, internal UI,
client communications, invoices, quotations, confirmations, validation/help text, default AI
replies, code/API/database identifiers, tests, and user-facing documentation. U.S. spelling,
not British.

The existing system does not meet this. Measured on 2026-09-26: **306 Indonesian string
literals across 10 modules** (`dashboard.py` 81, `connectors.py` 55, `incidents.py` 54,
`cases.py` 37, `intake.py` 32, `matching.py` 16, `identity.py` 14, `messages.py` 9) plus **11
templates**. Canonical identifiers (tables, columns, error codes) are already English, so the
gap is the human-readable layer.

The requirement is not optional, so this ADR does not decide *whether*; it decides *how*, and
what happens to the Indonesian text in the meantime.

## Decision

1. **New production text is written in en-US from now on.** No new Indonesian in `src/` or
   `templates/`.
2. **Machine-readable contracts stay English and stable.** `DomainError.code` values
   (`MFA_REQUIRED`, `STEP_UP_REQUIRED`, …) are the API contract; the human-readable `message`
   becomes en-US, and no caller may parse it.
3. **A ratchet guard, not a big bang.** `tests/test_language_standard.py` scans production
   surfaces for Indonesian text. Files that still contain legacy Indonesian are listed in an
   explicit `LEGACY_PENDING_TRANSLATION` set with their current count; the test fails if a
   file *grows* or if a new file appears. The set can only shrink.
4. **Migration order** follows blast radius: `dashboard.py` + `templates/` (what a buyer sees
   first), then `connectors.py`/`messages.py` (what a guest reads), then the rest.
5. **Indonesian remains allowed** in `docs/` governance notes, where v1.1 itself is written in
   Indonesian, and in commit/report prose to the owner.

## Alternatives considered

- **Translate everything in one change** — rejected: a 300-string change touching 10 modules
  cannot be reviewed for meaning, and a wrong translation in an invoice or a refusal message is
  a business defect, not cosmetics.
- **Leave text and only fix new code** — rejected: the locked requirement would remain unmet
  for the surfaces that matter most (client-facing output).
- **Locale files with runtime switching** — deferred: v1.1 says architecture stays
  localization-ready but other production languages are deferred. Adding a full i18n layer now
  is scope the spec did not ask for; the ratchet guard leaves that door open.

## Security & operational impact

- Refusals must stay unambiguous after translation: an error's `code` is unchanged, so tests
  and integrations keep working while message text changes.
- No secret or PII handling changes.
- Cost: review effort per module; the guard prevents regression.

## Migration / reversibility

Reversible per file. The guard set makes progress visible and prevents backsliding.

## Tests required

- Guard test (ratchet) over `templates/*.html` and string literals in `src/`.
- Spot checks that user-visible finance/approval strings are en-US before WAVE 6.

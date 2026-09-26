# ADR-0020 — Hold lifecycle: one vocabulary, one writer, and an expiry that is not a release

- **Status**: ACCEPTED (recorded with WAVE 4-B, 2026-09-26)
- **Spec references**: §7 (Reservation & Hold Lifecycle), §11 (Pricing, Quote & Discount),
  Appendix A (status vocabulary), Appendix B (id prefixes)
- **Supersedes**: nothing. **Amends**: ADR-0019 (same vocabulary discipline, different table).

## Context

§7 states three rules for holds:

1. *"Hold expiry dan extension selalu menyimpan timezone."*
2. *"EXTENSION_REQUESTED tidak memperpanjang expiry sampai supplier mengonfirmasi."*
3. *"Expired supplier hold masuk `EXPIRED_PENDING_VERIFICATION`, bukan otomatis RELEASED
   kecuali authoritative integration menjamin status."*

The brownfield kernel broke two of the three:

| Rule | Brownfield behaviour | Evidence |
|------|---------------------|----------|
| 2 | `extend_hold()` moved `expires_at` immediately | `UPDATE holds SET expires_at = ...` in the old `extend_hold` |
| 3 | `expire_hold()` / the sweep wrote `EXPIRED` as a fact, which then freed the slot | `HOLD_EXPIRED = "EXPIRED"`, `_close_hold(status=HOLD_EXPIRED)` |
| 1 | `expires_at` stored an instant with no zone | no timezone column on `holds` |

It also spoke a vocabulary of its own — `ACTIVE` / `RELEASED` / `EXPIRED` / `CONVERTED` —
where Appendix A names `REQUESTED`, `AWAITING_SUPPLIER`, `CONFIRMED`, `REJECTED`,
`EXTENSION_REQUESTED`, `RELEASE_REQUESTED`, `RELEASE_CONFIRMED`,
`EXPIRED_PENDING_VERIFICATION`.

The occupying predicate was restated in six places (`holds.status` CHECK,
`uq_holds_active_slot`, `ck_holds_closed`, the PG exclusion constraint, the PG twin, and
the kernel/observability/dashboard/scheduler queries), each carrying its own copy of
`'ACTIVE'` — the same trap ADR-0002 recorded for bookings.

## Decision

1. **One home for the vocabulary: `src/hold_states.py`.** Nine states a `holds` row may
   hold. `NOT_REQUESTED` is deliberately absent: Appendix A names it as the absence of a
   hold request — a row that does not exist, not a state one can be in. `CONVERTED` is
   carried as a documented extension (the hold became a booking; §7 does not name it).
2. **Two subsets, not one flag.** `OCCUPYING_HOLD_STATES` (blocks the slot),
   `LIVE_HOLD_STATES` (in force, so its TTL can run out), `CLOSED_HOLD_STATES`
   (`closed_at` must be set). `EXPIRED_PENDING_VERIFICATION` is occupying **and** closed:
   the hold is over, the slot is not yet free.
3. **`holds.expires_timezone` is NOT NULL**, derived from `properties.timezone` and
   validated with `zoneinfo` (stdlib). A name that is not a zone stores a timezone in
   appearance only, so `resolve_timezone` refuses it (`HOLD_INVALID_TIMEZONE`).
4. **One writer: `_transition_hold()`.** It validates the edge against `hold_states`,
   sets `closed_at` exactly for the closed states, and audits the before/after pair in the
   same transaction. `_close_hold` is deleted; every state change goes through it.
5. **The extension is two steps.** `request_extension()` writes a
   `hold_extension_requests` row and moves the hold to `EXTENSION_REQUESTED` — the expiry
   does not move (§7 rule 2). `confirm_extension()` moves the expiry by the requested
   minutes and increments `extended_count`; `reject_extension()` leaves the expiry exactly
   where it was. The old `extend_hold()` is **removed**: it had no callers outside tests,
   and it was the violation.
6. **An expiry is not a release.** The sweep and `expire_hold()` write
   `EXPIRED_PENDING_VERIFICATION`, which **keeps occupying the slot**. Only
   `verify_expired_hold(evidence_id=...)` closes it, and only with evidence that is
   level A (`property_api` / `booking_engine` — §7's *authoritative integration*), about
   the same unit, carrying `can_confirm`, and still fresh for its level.
7. **The authority rule lives in one place.** `_authoritative_evidence()` is called by
   `_transition_hold()` for *any* transition out of `EXPIRED_PENDING_VERIFICATION`, so
   `release_hold()` cannot reach `RELEASE_CONFIRMED` without it either. A caller that
   passes no verification evidence is refused
   (`HOLD_VERIFICATION_REQUIRES_AUTHORITATIVE_EVIDENCE`), not silently allowed.
8. **The restatements are pinned, not trusted.** `tests/test_wave4b_hold_lifecycle.py`
   parses the SQLite CHECK, the partial index predicate, the PG twin and the PG exclusion
   constraint and compares each to `hold_states`; `observability` re-exports the same
   tuple and no longer hardcodes `'ACTIVE'`.

## Consequences

- **Schema**: +1 table (`hold_extension_requests`, tenant-scoped, `seq`-ordered), +1 column
  (`holds.expires_timezone`). No data migration: there is no live database (`data/` empty).
- **Behaviour change (intended)**: a swept hold no longer frees its slot. This is the
  fail-closed reading of rule 3 — the alternative is selling dates the supplier may still
  consider held. `test_swept_hold_keeps_the_slot_until_it_is_verified` and
  `test_an_unverified_expiry_still_occupies_its_slot` pin it.
- **Behaviour change (intended)**: `release_hold()` on an unverified expiry is refused.
- **Reachable states today**: `CONFIRMED`, `EXTENSION_REQUESTED`, `RELEASE_CONFIRMED`,
  `CONVERTED`, `EXPIRED_PENDING_VERIFICATION`. `REQUESTED`, `AWAITING_SUPPLIER` and
  `RELEASE_REQUESTED` become reachable with the supplier round-trip (WAVE 4-B-2); they are
  in the vocabulary and the CHECK because the schema must accept what §7 names.
- **Dashboard**: the inventory calendar marks every occupying hold as `HOLD` — including
  the ones pending verification — because showing them as free would contradict what the
  kernel does with the next request for the same dates. The legend was corrected to match.
- **`holds_active`** (observability) counts `LIVE_HOLD_STATES`; `holds_expired_but_open`
  counts every occupying hold past its TTL, whether the sweep has reached it or not.

## Open items (recorded, not fixed here)

1. **Self-manufactured evidence.** When `request_hold(evidence_id=None)` is called, the
   kernel records its own row with `source_type="manual_ops"` and `evidence_level="B"` —
   a pair `LEVEL_SOURCE_TYPES` does not accept (B is for manager-written sources). The
   availability gate would refuse that row, but `request_hold` does not ask it. Closing
   this means requiring explicit evidence or recording it honestly as level D (which
   cannot authorise a hold) — a change that touches every caller, so it belongs to the
   evidence-provenance slice, not to this one.
2. **A pending extension whose hold expired** stays `REQUESTED`; confirming it is refused
   (the hold is no longer `EXTENSION_REQUESTED`), so nothing is granted by accident, but
   the row is not tidied.
3. **`verify_expired_hold` is not yet exposed** through the dashboard or a CLI — a human
   operator currently has no button for it (the kernel API is the only door).

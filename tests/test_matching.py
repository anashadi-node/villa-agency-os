"""Tests for :mod:`matching` — explainable, deterministic unit matching.

The suite is organised around the three promises the module makes:

1. **eligibility first, score second** — a refused candidate always carries a
   coded ``deny_reason`` (persisted, checked in the database afterwards) and
   never outranks an eligible one;
2. **the kernel is the only availability authority** — blocking ids recorded in
   ``evidence_refs`` are ids that really exist in ``bookings``/``holds``;
3. **determinism** — identical facts + identical database ⇒ byte-identical
   decision payload, with a deterministic tie-break on ascending ``unit_id``.

Nothing here uses the wall clock, ``random`` or an LLM: ``now``/``clock``/``ids``
are injected everywhere.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
from datetime import date

import pytest

import intake as I
import matching as M
import reservation_kernel as K
from primitives import DomainError, FrozenClock, IdFactory, canonical_json, parse_iso_date

NOW = "2026-09-21T08:00:00Z"
CHECK_IN = "2026-10-01"
CHECK_OUT = "2026-10-04"
NIGHTS = 3
NIGHTLY = 1_400_000

# --------------------------------------------------------------------------- #
# Seeding helpers (direct inserts: this module owns no table of its own, so the
# tests describe the *database state* a match run has to explain)
# --------------------------------------------------------------------------- #
def _fp(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def policy_payload(**over: object) -> dict:
    """A payload the policy engine would accept, plus room for extra keys."""
    data: dict[str, object] = {
        "extra_guest_fee_minor": 150_000,
        "cleaning_fee_minor": 250_000,
        "security_deposit_minor": 1_000_000,
        "min_stay_nights": 2,
        "cancellation_ladder": [{"days_before": 30, "refund_pct": "100"}],
        "notes": "seeded policy",
    }
    data.update(over)
    return data


def seed_property(
    conn,
    property_id: str = "prop_1",
    *,
    name: str = "Villa Anantara",
    property_type: str = "villa",
    region: str = "Ubud",
    province: str = "Bali",
    timezone: str = "Asia/Makassar",
    status: str = "SELLABLE",
    contract_pack_status: str = "ACTIVATED",
) -> str:
    conn.execute(
        "INSERT INTO properties (property_id, name, property_type, region, province, timezone,"
        " status, contract_pack_status, contract_pack_evaluated_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            property_id,
            name,
            property_type,
            region,
            province,
            timezone,
            status,
            contract_pack_status,
            NOW,
            NOW,
            NOW,
        ),
    )
    return property_id


def seed_unit(
    conn,
    unit_id: str = "unit_1",
    *,
    property_id: str = "prop_1",
    name: str | None = None,
    unit_type: str = "entire_villa",
    bedrooms: int = 2,
    bathrooms: int = 1,
    base_capacity: int = 2,
    max_guests: int = 4,
    status: str = "ACTIVE",
) -> str:
    conn.execute(
        "INSERT INTO units (unit_id, property_id, name, unit_type, bedrooms, bathrooms,"
        " base_capacity, max_guests, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unit_id,
            property_id,
            name or f"Unit {unit_id}",
            unit_type,
            bedrooms,
            bathrooms,
            base_capacity,
            max_guests,
            status,
            NOW,
            NOW,
        ),
    )
    return unit_id


def seed_policy(
    conn,
    policy_version_id: str = "pol_1",
    *,
    property_id: str = "prop_1",
    unit_scope: str = "property",
    version: int = 1,
    effective_from: str = "2026-01-01",
    effective_to: str = "2026-12-31",
    currency: str = "IDR",
    min_stay_nights: int = 2,
    payload: dict | None = None,
) -> str:
    body = payload if payload is not None else policy_payload(min_stay_nights=min_stay_nights)
    conn.execute(
        "INSERT INTO policy_versions (policy_version_id, property_id, unit_scope, version,"
        " effective_from, effective_to, currency, deposit_pct, service_fee_pct, tax_pbjt_pct,"
        " max_discount_pct, quote_valid_hours, hold_ttl_minutes, max_hold_extensions, payload,"
        " fingerprint, source_doc_ref, approved_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '30', '10', '5', '10', 24, 30, 1, ?, ?, ?, ?, ?)",
        (
            policy_version_id,
            property_id,
            unit_scope,
            version,
            effective_from,
            effective_to,
            currency,
            canonical_json(body),
            _fp(f"policy:{policy_version_id}"),
            f"doc://policy/{policy_version_id}",
            "actor:owner",
            NOW,
        ),
    )
    return policy_version_id


def seed_rate_card(
    conn,
    rate_card_id: str = "rc_1",
    *,
    property_id: str = "prop_1",
    unit_scope: str = "property",
    version: int = 1,
    name: str = "Standard 2026",
    effective_from: str = "2026-01-01",
    effective_to: str = "2026-12-31",
    currency: str = "IDR",
    seasons: tuple[tuple[str, str, str, int, int, int], ...] | None = None,
) -> str:
    conn.execute(
        "INSERT INTO rate_cards (rate_card_id, property_id, unit_scope, name, version,"
        " effective_from, effective_to, currency, weekly_discount_pct, monthly_discount_pct,"
        " source_doc_ref, approved_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '10', '15', ?, 'actor:owner', ?)",
        (
            rate_card_id,
            property_id,
            unit_scope,
            name,
            version,
            effective_from,
            effective_to,
            currency,
            f"doc://rate/{rate_card_id}",
            NOW,
        ),
    )
    rows = seasons or (("season_1", "2026-01-01", "2026-12-31", NIGHTLY, 2, 0),)
    for season_id, start_date, end_date, nightly, min_stay, priority in rows:
        conn.execute(
            "INSERT INTO rate_seasons (season_id, rate_card_id, name, start_date, end_date,"
            " nightly_rate_minor, min_stay_nights, priority, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                season_id,
                rate_card_id,
                f"Season {season_id}",
                start_date,
                end_date,
                nightly,
                min_stay,
                priority,
                NOW,
            ),
        )
    return rate_card_id


def seed_case(conn, case_id: str = "case_1", *, status: str = "MATCHING") -> str:
    conn.execute(
        "INSERT INTO cases (case_id, channel, channel_ref, guest_ref, status, priority, owner_actor,"
        " booking_id, sla_policy, first_response_due_at, quote_due_at, first_responded_at,"
        " quoted_at, idempotency_key, created_at, updated_at, closed_at, close_reason)"
        " VALUES (?, 'website', NULL, 'guest_001', ?, 'NORMAL', NULL, NULL, 'default',"
        " '2026-09-21T09:00:00Z', '2026-09-21T12:00:00Z', NULL, NULL, ?, ?, ?, NULL, NULL)",
        (case_id, status, f"idem-{case_id}", NOW, NOW),
    )
    return case_id


def seed_booking(
    conn,
    booking_id: str = "book_1",
    *,
    unit_id: str = "unit_1",
    property_id: str = "prop_1",
    check_in: str = CHECK_IN,
    check_out: str = CHECK_OUT,
    status: str = "CONFIRMED",
    policy_version_id: str = "pol_1",
    guest_ref: str = "guest_002",
    adults: int = 2,
) -> str:
    nights = (parse_iso_date(check_out) - parse_iso_date(check_in)).days
    conn.execute(
        "INSERT INTO bookings (booking_id, property_id, unit_id, check_in, check_out, nights,"
        " status, guest_ref, adults, children, infants, currency, policy_version_id,"
        " policy_fingerprint, quote_ref, hold_ref, total_minor, amount_paid_minor,"
        " security_deposit_minor, payment_status, idempotency_key, version_lock, created_at,"
        " updated_at, confirmed_at, cancelled_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'IDR', ?, ?, NULL, NULL, 4000000, 0, 0,"
        " 'UNPAID', ?, 1, ?, ?, ?, NULL)",
        (
            booking_id,
            property_id,
            unit_id,
            check_in,
            check_out,
            nights,
            status,
            guest_ref,
            adults,
            policy_version_id,
            _fp(f"policy:{policy_version_id}"),
            f"idem-{booking_id}",
            NOW,
            NOW,
            NOW,
        ),
    )
    return booking_id


def seed_evidence(
    conn,
    evidence_id: str = "ev_1",
    *,
    unit_id: str = "unit_1",
    evidence_level: str = "A",
    source_type: str = "property_api",
    result: str = "AVAILABLE",
    captured_at: str = "2026-09-21T07:00:00Z",
    valid_until: str = "2026-10-01T00:00:00Z",
    can_quote: int = 1,
    can_hold: int = 1,
    can_confirm: int = 1,
) -> str:
    conn.execute(
        "INSERT INTO availability_evidence (evidence_id, unit_id, evidence_level, source_type,"
        " source_ref, captured_at, valid_until, result, occupancy_ranges, can_quote, can_hold,"
        " can_confirm, notes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, NULL, ?)",
        (
            evidence_id,
            unit_id,
            evidence_level,
            source_type,
            f"src://{source_type}/{unit_id}",
            captured_at,
            valid_until,
            result,
            can_quote,
            can_hold,
            can_confirm,
            NOW,
        ),
    )
    return evidence_id


def seed_hold(
    conn,
    hold_id: str = "hold_1",
    *,
    unit_id: str = "unit_1",
    property_id: str = "prop_1",
    check_in: str = CHECK_IN,
    check_out: str = CHECK_OUT,
    status: str = "CONFIRMED",
    policy_version_id: str = "pol_1",
    evidence_id: str = "ev_1",
    guest_ref: str = "guest_003",
) -> str:
    if conn.execute(
        "SELECT 1 FROM availability_evidence WHERE evidence_id = ?", (evidence_id,)
    ).fetchone() is None:
        seed_evidence(conn, evidence_id, unit_id=unit_id)
    nights = (parse_iso_date(check_out) - parse_iso_date(check_in)).days
    conn.execute(
        "INSERT INTO holds (hold_id, unit_id, property_id, check_in, check_out, nights, status,"
        " guest_ref, quote_ref, policy_version_id, evidence_id, idempotency_key, created_at,"
        " expires_at, expires_timezone, closed_at, close_reason, extended_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
        (
            hold_id,
            unit_id,
            property_id,
            check_in,
            check_out,
            nights,
            status,
            guest_ref,
            policy_version_id,
            evidence_id,
            f"idem-{hold_id}",
            NOW,
            "2026-09-22T08:00:00Z",
            "Asia/Makassar",
            None if status == "CONFIRMED" else "2026-09-21T09:00:00Z",
        ),
    )
    return hold_id


def kernel_for(conn, *, seed: str = "k") -> K.ReservationKernel:
    return K.ReservationKernel(conn, clock=FrozenClock.at(NOW), ids=IdFactory(seed=seed))


def base_facts(**over: object) -> dict:
    facts: dict[str, object] = {"check_in": CHECK_IN, "check_out": CHECK_OUT, "adults": 2}
    facts.update(over)
    return facts


def seed_base(
    conn,
    *,
    with_rate_card: bool = True,
    with_policy: bool = True,
    min_stay_nights: int = 2,
    policy: dict | None = None,
) -> K.ReservationKernel:
    """The smallest believable world: one sellable property, one active unit."""
    seed_property(conn)
    seed_unit(conn)
    if with_policy:
        seed_policy(conn, min_stay_nights=min_stay_nights, payload=policy)
    if with_rate_card:
        seed_rate_card(conn)
    seed_case(conn)
    return kernel_for(conn)


def run_match(conn, *, facts: dict | None = None, kernel=None, ids=None, now=NOW, case_id="case_1"):
    return M.match(
        conn,
        case_id=case_id,
        facts=base_facts() if facts is None else facts,
        kernel=kernel if kernel is not None else kernel_for(conn),
        now=now,
        clock=None,
        ids=ids if ids is not None else IdFactory(seed="m"),
    )


def run_rows(conn) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM match_runs ORDER BY rowid").fetchall()]


def candidate_rows(conn, match_run_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM match_candidates WHERE match_run_id = ? ORDER BY rank",
            (match_run_id,),
        ).fetchall()
    ]


def stored_candidate(conn, match_run_id: str, unit_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM match_candidates WHERE match_run_id = ? AND unit_id = ?",
        (match_run_id, unit_id),
    ).fetchone()
    assert row is not None, f"no persisted candidate for {unit_id}"
    return dict(row)


def reason_codes(candidate: M.MatchCandidate) -> set[str]:
    return {str(reason["code"]) for reason in candidate.reasons}


def note_codes(candidate: M.MatchCandidate) -> set[str]:
    return {str(note["code"]) for note in candidate.notes}


class _SpyKernel:
    """Records every occupancy question, delegates the answer."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    def occupancy(self, unit_id: str, *, start: str, end: str):
        self.calls.append({"unit_id": unit_id, "start": start, "end": end})
        return self.inner.occupancy(unit_id, start=start, end=end)


class _StubKernel:
    """Answers from a fixed script — proves the verdict really is the kernel's."""

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.calls: list[str] = []

    def occupancy(self, unit_id: str, *, start: str, end: str):
        self.calls.append(unit_id)
        return tuple(self.script.get(unit_id, ()))


# --------------------------------------------------------------------------- #
# 1. Fail-closed inputs
# --------------------------------------------------------------------------- #
class TestFailClosedCriteria:
    def test_missing_check_in_raises_missing_facts(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            run_match(conn, facts={"check_out": CHECK_OUT, "adults": 2})
        assert excinfo.value.code == "MISSING_FACTS"
        assert "check_in" in excinfo.value.details["missing"]
        assert run_rows(conn) == []

    def test_missing_check_out_raises_missing_facts(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            run_match(conn, facts={"check_in": CHECK_IN, "adults": 2})
        assert "check_out" in excinfo.value.details["missing"]
        assert conn.execute("SELECT COUNT(*) FROM match_candidates").fetchone()[0] == 0

    def test_missing_adults_raises_missing_facts(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            run_match(conn, facts={"check_in": CHECK_IN, "check_out": CHECK_OUT})
        assert "adults" in excinfo.value.details["missing"]

    def test_missing_required_facts_are_reported_together(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            run_match(conn, facts={})
        assert tuple(excinfo.value.details["missing"]) == I.REQUIRED_FIELDS

    def test_explicit_none_fact_counts_as_missing(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            run_match(conn, facts={"check_in": None, "check_out": CHECK_OUT, "adults": 2})
        assert "check_in" in excinfo.value.details["missing"]

    def test_facts_none_is_loaded_from_intake_and_fails_closed(self, conn):
        seed_base(conn)
        with pytest.raises(M.MissingFacts) as excinfo:
            M.match(
                conn,
                case_id="case_1",
                facts=None,
                kernel=kernel_for(conn),
                now=NOW,
                ids=IdFactory(seed="m"),
            )
        # nothing was ever ACCEPTED for this case, so the required facts are absent
        assert excinfo.value.code == "MISSING_FACTS"
        assert run_rows(conn) == []

    def test_inverted_dates_raise_invalid_criteria(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria) as excinfo:
            run_match(conn, facts=base_facts(check_in=CHECK_OUT, check_out=CHECK_IN))
        assert excinfo.value.code == "INVALID_CRITERIA"
        assert conn.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == 0

    def test_equal_dates_raise_invalid_criteria(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria):
            run_match(conn, facts=base_facts(check_in=CHECK_IN, check_out=CHECK_IN))

    def test_zero_adults_raise_invalid_criteria(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria) as excinfo:
            run_match(conn, facts=base_facts(adults=0))
        assert excinfo.value.code == "INVALID_CRITERIA"
        assert excinfo.value.details.get("adults") == 0
        assert run_rows(conn) == []

    def test_unparseable_date_raises_invalid_criteria(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria):
            run_match(conn, facts=base_facts(check_in="besok"))

    def test_float_budget_is_refused_not_rounded(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria):
            run_match(conn, facts=base_facts(budget_minor=1_500_000.5))
        assert run_rows(conn) == []

    def test_negative_budget_is_refused(self, conn):
        seed_base(conn)
        with pytest.raises(M.InvalidCriteria):
            run_match(conn, facts=base_facts(budget_minor=-1))

    def test_empty_inventory_raises_no_inventory(self, conn):
        seed_case(conn)  # a case, no units at all
        with pytest.raises(M.NoInventory) as excinfo:
            run_match(conn)
        assert excinfo.value.code == "NO_INVENTORY"
        assert run_rows(conn) == []

    def test_empty_inventory_writes_no_candidate_rows(self, conn):
        seed_case(conn)
        with pytest.raises(M.NoInventory):
            run_match(conn)
        assert conn.execute("SELECT COUNT(*) FROM match_candidates").fetchone()[0] == 0

    def test_missing_kernel_is_refused(self, conn):
        seed_base(conn)
        with pytest.raises(DomainError) as excinfo:
            M.match(
                conn,
                case_id="case_1",
                facts=base_facts(),
                kernel=None,
                now=NOW,
                ids=IdFactory(seed="m"),
            )
        assert excinfo.value.code == "MATCH_KERNEL_REQUIRED"
        assert run_rows(conn) == []

    def test_errors_are_domain_errors_with_stable_codes(self):
        for cls, code in (
            (M.MissingFacts, "MISSING_FACTS"),
            (M.InvalidCriteria, "INVALID_CRITERIA"),
            (M.NoInventory, "NO_INVENTORY"),
            (M.MatchRunNotFound, "MATCH_RUN_NOT_FOUND"),
        ):
            assert issubclass(cls, DomainError)
            assert cls.code == code
            assert cls("x").code == code

    def test_signature_is_keyword_only_after_conn(self):
        params = list(inspect.signature(M.match).parameters.values())
        assert params[0].name == "conn"
        assert [p.name for p in params[1:]] == [
            "case_id",
            "facts",
            "kernel",
            "now",
            "clock",
            "ids",
            "agency_id",
        ]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])

    def test_clock_can_supply_now(self, conn):
        seed_base(conn)
        result = M.match(
            conn,
            case_id="case_1",
            facts=base_facts(),
            kernel=kernel_for(conn),
            now=None,
            clock=FrozenClock.at(NOW),
            ids=IdFactory(seed="m"),
        )
        assert result.created_at.startswith("2026-09-21T08:00:00")


# --------------------------------------------------------------------------- #
# 2. The ledger: one run row + one candidate row per unit, append-only
# --------------------------------------------------------------------------- #
class TestLedgerWrites:
    def test_result_exposes_the_documented_fields(self, conn):
        seed_base(conn)
        result = run_match(conn)
        assert isinstance(result.match_run_id, str) and result.match_run_id.startswith("match_run_")
        assert len(result.intake_hash) == 64
        assert result.considered_count == 1
        assert result.eligible_count == 1
        assert result.engine_version == M.ENGINE_VERSION
        candidate = result.candidates[0]
        for field in (
            "unit_id",
            "property_id",
            "rank",
            "eligible",
            "score_centi",
            "reasons",
            "deny_reasons",
            "evidence_refs",
        ):
            assert field in candidate.to_dict()

    def test_exactly_one_run_row_per_call(self, conn):
        seed_base(conn)
        result = run_match(conn)
        rows = run_rows(conn)
        assert len(rows) == 1
        assert rows[0]["match_run_id"] == result.match_run_id

    def test_one_candidate_row_per_considered_unit(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        seed_unit(conn, "unit_3")
        result = run_match(conn)
        assert len(candidate_rows(conn, result.match_run_id)) == 3

    def test_considered_count_equals_number_of_candidates(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        seed_unit(conn, "unit_3")
        result = run_match(conn)
        rows = candidate_rows(conn, result.match_run_id)
        assert result.considered_count == len(result.candidates) == len(rows) == 3

    def test_eligible_count_matches_eligible_candidates(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn)
        assert result.eligible_count == 1
        assert sum(1 for c in result.candidates if c.eligible) == 1

    def test_run_row_carries_counts_and_criteria(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(budget_minor=5_000_000, currency="IDR"))
        row = run_rows(conn)[0]
        assert row["case_id"] == "case_1"
        assert row["engine_version"] == M.ENGINE_VERSION
        assert row["considered_count"] == result.considered_count
        assert row["eligible_count"] == result.eligible_count
        assert row["intake_hash"] == result.intake_hash
        criteria = json.loads(row["criteria"])
        assert criteria["check_in"] == CHECK_IN
        assert criteria["check_out"] == CHECK_OUT
        assert criteria["nights"] == NIGHTS
        assert criteria["provenance"]["availability"] == "reservation_kernel.occupancy"

    def test_run_row_snapshot_is_the_facts_json(self, conn):
        seed_base(conn)
        run_match(conn, facts=base_facts(children=1, area_preference="Ubud"))
        snapshot = json.loads(run_rows(conn)[0]["intake_snapshot"])
        assert snapshot["check_in"] == CHECK_IN
        assert snapshot["children"] == 1
        assert snapshot["area_preference"] == "Ubud"

    def test_intake_hash_is_sha256_hex_and_stable(self, conn):
        seed_base(conn)
        first = run_match(conn)
        second = run_match(conn)
        assert first.intake_hash == second.intake_hash
        int(first.intake_hash, 16)
        assert len(first.intake_hash) == 64

    def test_intake_hash_changes_with_the_facts(self, conn):
        seed_base(conn)
        first = run_match(conn)
        second = run_match(conn, facts=base_facts(children=2))
        assert first.intake_hash != second.intake_hash

    def test_ranks_are_unique_and_contiguous(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        seed_unit(conn, "unit_3", status="MAINTENANCE")
        result = run_match(conn)
        rows = candidate_rows(conn, result.match_run_id)
        assert [row["rank"] for row in rows] == [1, 2, 3]
        assert [c.rank for c in result.candidates] == [1, 2, 3]

    def test_each_unit_appears_once_per_run(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        result = run_match(conn)
        rows = candidate_rows(conn, result.match_run_id)
        units = [row["unit_id"] for row in rows]
        assert len(units) == len(set(units))
        assert set(units) == {"unit_1", "unit_2"}

    def test_persisted_rows_match_the_returned_candidates(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn)
        for candidate in result.candidates:
            row = stored_candidate(conn, result.match_run_id, candidate.unit_id)
            assert row["rank"] == candidate.rank
            assert bool(row["eligible"]) is candidate.eligible
            assert row["score_centi"] == candidate.score_centi
            assert json.loads(row["deny_reasons"]) == list(candidate.deny_reasons)
            assert json.loads(row["evidence_refs"]) == list(candidate.evidence_refs)
            assert json.loads(row["reasons"]) == [dict(r) for r in candidate.reasons]

    def test_denied_rows_pass_the_db_deny_check(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        run_match(conn)
        for row in conn.execute("SELECT * FROM match_candidates").fetchall():
            if row["eligible"] == 0:
                assert json.loads(row["deny_reasons"])

    def test_second_run_appends_and_keeps_the_first(self, conn):
        seed_base(conn)
        first = run_match(conn)
        before = candidate_rows(conn, first.match_run_id)
        second = run_match(conn)
        assert first.match_run_id != second.match_run_id
        assert candidate_rows(conn, first.match_run_id) == before
        assert len(run_rows(conn)) == 2

    def test_run_ids_are_unique(self, conn):
        seed_base(conn)
        ids = [run_match(conn).match_run_id for _ in range(3)]
        assert len(set(ids)) == 3

    def test_candidate_ids_are_unique_across_runs(self, conn):
        seed_base(conn)
        first = run_match(conn)
        second = run_match(conn)
        rows = [row["candidate_id"] for row in conn.execute("SELECT * FROM match_candidates")]
        assert len(rows) == 2 == len(set(rows))
        assert first.match_run_id != second.match_run_id

    def test_matching_never_updates_the_ledger(self, conn):
        seed_base(conn)
        first = run_match(conn)
        snapshot = {row["candidate_id"]: row for row in candidate_rows(conn, first.match_run_id)}
        run_match(conn)
        after = {row["candidate_id"]: row for row in candidate_rows(conn, first.match_run_id)}
        assert snapshot == after


# --------------------------------------------------------------------------- #
# 3. Eligibility first, score second
# --------------------------------------------------------------------------- #
class TestEligibilityBeforeScore:
    def test_eligible_candidate_has_no_deny_reason(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert candidate.eligible is True
        assert candidate.deny_reasons == ()

    def test_denied_candidate_always_carries_a_known_deny_code(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn)
        for candidate in result.candidates:
            if not candidate.eligible:
                assert candidate.deny_reasons
                assert set(candidate.deny_reasons) <= set(M.DENY_REASON_CODES)

    def test_denied_candidates_rank_below_every_eligible_one(self, conn):
        seed_base(conn)
        seed_unit(conn, "aaa_maintenance", status="MAINTENANCE")
        seed_unit(conn, "zzz_also_fine")
        result = run_match(conn)
        eligible_ranks = [c.rank for c in result.candidates if c.eligible]
        denied_ranks = [c.rank for c in result.candidates if not c.eligible]
        assert eligible_ranks and denied_ranks
        assert max(eligible_ranks) < min(denied_ranks)
        assert result.candidates[0].eligible is True

    def test_denied_candidate_is_not_scored(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn)
        denied = [c for c in result.candidates if not c.eligible]
        assert denied
        for candidate in denied:
            assert candidate.score_centi == 0
            assert candidate.reasons == ()

    def test_score_is_the_sum_of_the_reason_weights(self, conn):
        seed_base(conn)
        result = run_match(
            conn,
            facts=base_facts(
                area_preference="Ubud", budget_minor=5_000_000, currency="IDR", bedrooms=2
            ),
        )
        candidate = result.candidates[0]
        assert candidate.score_centi == sum(int(r["weight"]) for r in candidate.reasons)
        assert candidate.score_centi > 0

    def test_score_is_an_integer_never_a_float(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(budget_minor=5_000_000, currency="IDR"))
        for candidate in result.candidates:
            assert isinstance(candidate.score_centi, int)
            assert not isinstance(candidate.score_centi, bool)
            assert int(candidate.score_centi) == candidate.score_centi
        payload = canonical_json(result.to_dict())
        assert ".0" not in payload.split('"score_centi":')[1][:6]

    def test_scores_are_integer_in_the_database(self, conn):
        seed_base(conn)
        result = run_match(conn)
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert isinstance(row["score_centi"], int)
        assert row["score_centi"] == result.candidates[0].score_centi

    def test_reason_weights_are_positive_integers(self):
        for code, weight in M.REASON_WEIGHTS.items():
            assert code in M.POSITIVE_REASON_CODES
            assert isinstance(weight, int) and not isinstance(weight, bool)
            assert weight > 0

    def test_reason_codes_are_from_the_closed_set(self, conn):
        seed_base(conn)
        result = run_match(
            conn,
            facts=base_facts(
                area_preference="Ubud", budget_minor=5_000_000, currency="IDR", bedrooms=2
            ),
        )
        for candidate in result.candidates:
            assert reason_codes(candidate) <= set(M.POSITIVE_REASON_CODES)

    def test_every_scored_candidate_has_a_reason(self, conn):
        seed_base(conn)
        result = run_match(conn)
        for candidate in result.candidates:
            if candidate.eligible:
                assert candidate.reasons
                assert candidate.score_centi == sum(int(r["weight"]) for r in candidate.reasons)
            else:
                assert candidate.score_centi == 0

    def test_case_is_recorded_even_when_every_unit_is_refused(self, conn):
        seed_base(conn)
        conn.execute("UPDATE units SET status = 'MAINTENANCE' WHERE unit_id = 'unit_1'")
        result = run_match(conn)
        assert result.eligible_count == 0
        assert result.considered_count == 1
        assert run_rows(conn)[0]["eligible_count"] == 0
        assert result.candidates[0].deny_reasons == ("MAINTENANCE_BLOCK",)


# --------------------------------------------------------------------------- #
# 4. Capacity
# --------------------------------------------------------------------------- #
class TestCapacity:
    def test_capacity_exceeded_is_a_refusal(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(adults=5))
        candidate = result.candidates[0]
        assert candidate.eligible is False
        assert "CAPACITY_EXCEEDED" in candidate.deny_reasons
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert row["eligible"] == 0
        assert "CAPACITY_EXCEEDED" in json.loads(row["deny_reasons"])

    def test_capacity_refusal_leaves_inventory_untouched(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(adults=9))
        unit = conn.execute("SELECT * FROM units WHERE unit_id = 'unit_1'").fetchone()
        assert unit["max_guests"] == 4
        assert unit["status"] == "ACTIVE"
        assert candidate_rows(conn, result.match_run_id)[0]["eligible"] == 0

    def test_capacity_fit_reason_is_recorded(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(adults=3)).candidates[0]
        assert candidate.eligible is True
        assert "CAPACITY_FIT" in reason_codes(candidate)
        detail = next(r["detail"] for r in candidate.reasons if r["code"] == "CAPACITY_FIT")
        assert detail["guests"] == 3
        assert detail["max_guests"] == 4

    def test_children_count_towards_capacity(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(adults=2, children=3)).candidates[0]
        assert "CAPACITY_EXCEEDED" in candidate.deny_reasons

    def test_infants_do_not_count_towards_capacity(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_tiny", max_guests=2, base_capacity=1)
        candidate = next(
            c
            for c in run_match(conn, facts=base_facts(adults=2, infants=2)).candidates
            if c.unit_id == "unit_tiny"
        )
        assert candidate.eligible is True
        assert "CAPACITY_FIT" in reason_codes(candidate)

    def test_exact_capacity_is_a_fit(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(adults=4)).candidates[0]
        assert candidate.eligible is True

    def test_capacity_and_service_denials_coexist(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_broken", max_guests=1, base_capacity=1, status="MAINTENANCE")
        candidate = next(
            c for c in run_match(conn, facts=base_facts(adults=3)).candidates if c.unit_id == "unit_broken"
        )
        assert set(candidate.deny_reasons) == {"CAPACITY_EXCEEDED", "MAINTENANCE_BLOCK"}
        assert list(candidate.deny_reasons) == ["CAPACITY_EXCEEDED", "MAINTENANCE_BLOCK"]


# --------------------------------------------------------------------------- #
# 5. Date availability — decided by reservation_kernel, evidenced by real ids
# --------------------------------------------------------------------------- #
class TestDateAvailability:
    def test_booking_blocks_the_dates_and_is_evidenced(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_real", check_in="2026-10-02", check_out="2026-10-05")
        result = run_match(conn)
        candidate = result.candidates[0]
        assert candidate.eligible is False
        assert "DATES_UNAVAILABLE" in candidate.deny_reasons
        assert "book_real" in candidate.evidence_refs
        row = conn.execute("SELECT * FROM bookings WHERE booking_id = 'book_real'").fetchone()
        assert row is not None
        assert row["status"] == "CONFIRMED"
        assert json.loads(stored_candidate(conn, result.match_run_id, "unit_1")["evidence_refs"]) == [
            "book_real"
        ]

    def test_hold_blocks_the_dates_and_is_evidenced(self, conn):
        seed_base(conn)
        seed_hold(conn, "hold_real")
        result = run_match(conn)
        candidate = result.candidates[0]
        assert "DATES_UNAVAILABLE" in candidate.deny_reasons
        assert candidate.evidence_refs == ("hold_real",)
        row = conn.execute("SELECT * FROM holds WHERE hold_id = 'hold_real'").fetchone()
        assert row["status"] == "CONFIRMED"

    def test_evidence_ids_must_exist_in_the_database(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_real", check_in="2026-10-02", check_out="2026-10-05")
        seed_hold(conn, "hold_other", check_in="2026-10-03", check_out="2026-10-06")
        result = run_match(conn)
        known = {
            row[0]
            for row in conn.execute(
                "SELECT booking_id FROM bookings UNION SELECT hold_id FROM holds"
                " UNION SELECT evidence_id FROM availability_evidence"
            )
        }
        for candidate in result.candidates:
            for ref in candidate.evidence_refs:
                assert ref in known

    def test_free_dates_get_the_available_reason(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert "AVAILABLE" in reason_codes(candidate)
        assert "DATES_UNAVAILABLE" not in candidate.deny_reasons

    def test_adjacent_booking_does_not_block(self, conn):
        seed_base(conn)
        # ends the morning the guest arrives: [start, end) windows do not overlap
        seed_booking(conn, "book_before", check_in="2026-09-28", check_out=CHECK_IN)
        candidate = run_match(conn).candidates[0]
        assert candidate.eligible is True

    def test_cancelled_booking_does_not_block(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_cancelled", status="CANCELLED", check_in="2026-10-02", check_out="2026-10-05")
        result = run_match(conn)
        assert result.candidates[0].eligible is True
        assert result.candidates[0].evidence_refs == ()

    def test_released_hold_does_not_block(self, conn):
        seed_base(conn)
        seed_hold(conn, "hold_released", status="RELEASE_CONFIRMED")
        result = run_match(conn)
        assert result.candidates[0].eligible is True
        assert result.candidates[0].evidence_refs == ()

    def test_occupancy_is_asked_once_per_unit_with_the_stay_window(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        seed_unit(conn, "unit_3", status="MAINTENANCE")
        spy = _SpyKernel(kernel_for(conn))
        result = run_match(conn, kernel=spy)
        assert len(spy.calls) == result.considered_count == 3
        assert [call["unit_id"] for call in spy.calls] == ["unit_1", "unit_2", "unit_3"]
        for call in spy.calls:
            assert (call["start"], call["end"]) == (CHECK_IN, CHECK_OUT)

    def test_the_kernel_verdict_overrides_our_own_view_of_the_database(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_real", check_in="2026-10-02", check_out="2026-10-05")
        permissive = _StubKernel()  # kernel insists the dates are free
        result = run_match(conn, kernel=permissive)
        assert result.candidates[0].eligible is True
        assert conn.execute("SELECT COUNT(*) FROM bookings WHERE status = 'CONFIRMED'").fetchone()[0] == 1
        assert permissive.calls == ["unit_1"]

    def test_a_kernel_that_blocks_wins_even_without_a_row(self, conn):
        seed_base(conn)
        blocking = _StubKernel(
            {
                "unit_1": (
                    {
                        "kind": "booking",
                        "id": "book_ghost",
                        "status": "CONFIRMED",
                        "check_in": CHECK_IN,
                        "check_out": CHECK_OUT,
                    },
                )
            }
        )
        result = run_match(conn, kernel=blocking)
        candidate = result.candidates[0]
        assert candidate.deny_reasons == ("DATES_UNAVAILABLE",)
        assert candidate.evidence_refs == ("book_ghost",)

    def test_source_never_queries_holds_or_bookings_to_decide_availability(self):
        source = pathlib.Path(M.__file__).read_text(encoding="utf-8")
        evaluator = source.split("def _evaluate_unit")[1].split("\ndef ")[0]
        assert "FROM bookings" not in evaluator
        assert "FROM holds" not in evaluator
        assert "kernel.occupancy(" in evaluator
        # the only reads of holds/bookings left are the evidence *report* in explain
        for fragment in ("FROM bookings", "FROM holds"):
            assert source.index(fragment) > source.index("def _resolve_evidence_ref")

    def test_every_occupying_booking_state_blocks_and_the_rest_do_not(self, conn):
        seed_base(conn)
        seed_property(conn, "prop_2", name="Villa Dua")
        seed_policy(conn, "pol_2", property_id="prop_2")
        occupying = tuple(K.OCCUPYING_BOOKING_STATES)
        assert occupying, "the kernel must export its occupying states"
        for index, status in enumerate(occupying):
            seed_unit(conn, f"unit_hold_{index}", property_id="prop_2")
            seed_booking(
                conn,
                f"book_{index}",
                unit_id=f"unit_hold_{index}",
                property_id="prop_2",
                policy_version_id="pol_2",
                status=status,
                check_in="2026-10-02",
                check_out="2026-10-05",
            )
        seed_unit(conn, "unit_free_a", property_id="prop_2")
        seed_booking(
            conn,
            "book_free_a",
            unit_id="unit_free_a",
            property_id="prop_2",
            status="CANCELLED",
            check_in="2026-10-02",
            check_out="2026-10-05",
        )
        seed_unit(conn, "unit_free_b", property_id="prop_2")
        seed_booking(
            conn,
            "book_free_b",
            unit_id="unit_free_b",
            property_id="prop_2",
            status="COMPLETED",
            check_in="2026-10-02",
            check_out="2026-10-05",
        )
        result = run_match(conn)
        by_unit = {c.unit_id: c for c in result.candidates}
        for index, status in enumerate(occupying):
            candidate = by_unit[f"unit_hold_{index}"]
            assert candidate.deny_reasons == ("DATES_UNAVAILABLE",), status
            assert candidate.evidence_refs == (f"book_{index}",)
        for unit_id in ("unit_free_a", "unit_free_b"):
            assert by_unit[unit_id].eligible is True
            assert by_unit[unit_id].evidence_refs == ()


# --------------------------------------------------------------------------- #
# 6. Area
# --------------------------------------------------------------------------- #
class TestArea:
    def test_area_mismatch_is_a_refusal(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(area_preference="Seminyak"))
        candidate = result.candidates[0]
        assert candidate.eligible is False
        assert "AREA_MISMATCH" in candidate.deny_reasons
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert "AREA_MISMATCH" in json.loads(row["deny_reasons"])
        assert conn.execute("SELECT region FROM properties").fetchone()[0] == "Ubud"

    def test_area_match_is_case_insensitive(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(area_preference="  ubud ")).candidates[0]
        assert candidate.eligible is True
        assert "AREA_MATCH" in reason_codes(candidate)

    def test_area_match_reason_carries_both_sides(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(area_preference="Ubud")).candidates[0]
        detail = next(r["detail"] for r in candidate.reasons if r["code"] == "AREA_MATCH")
        assert detail == {"area_preference": "Ubud", "region": "Ubud"}

    def test_missing_area_preference_is_unknown_not_scored(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert candidate.eligible is True
        assert "AREA_MATCH" not in reason_codes(candidate)
        assert "UNKNOWN_AREA_BASIS" in candidate.unknowns
        assert candidate.basis["area"] == "UNKNOWN"

    def test_area_only_filters_the_units_that_do_not_match(self, conn):
        seed_base(conn)
        seed_property(conn, "prop_2", region="Seminyak", name="Villa Seminyak")
        seed_unit(conn, "unit_beach", property_id="prop_2")
        result = run_match(conn, facts=base_facts(area_preference="Ubud"))
        by_unit = {c.unit_id: c for c in result.candidates}
        assert by_unit["unit_1"].eligible is True
        assert by_unit["unit_beach"].eligible is False
        assert "AREA_MISMATCH" in by_unit["unit_beach"].deny_reasons


# --------------------------------------------------------------------------- #
# 7. Budget (a missing basis must never become an invented match)
# --------------------------------------------------------------------------- #
class TestBudget:
    def test_budget_within_is_reasoned_with_the_basis(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(budget_minor=5_000_000, currency="IDR"))
        candidate = result.candidates[0]
        assert "BUDGET_WITHIN" in reason_codes(candidate)
        detail = next(r["detail"] for r in candidate.reasons if r["code"] == "BUDGET_WITHIN")
        assert detail["basis_minor"] == NIGHTLY * NIGHTS
        assert detail["budget_minor"] == 5_000_000
        assert candidate.basis["accommodation_minor"] == NIGHTLY * NIGHTS

    def test_exact_budget_is_within(self, conn):
        seed_base(conn)
        candidate = run_match(
            conn, facts=base_facts(budget_minor=NIGHTLY * NIGHTS, currency="IDR")
        ).candidates[0]
        assert "BUDGET_WITHIN" in reason_codes(candidate)

    def test_budget_exceeded_is_not_a_refusal_but_loses_the_points(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        facts = base_facts(budget_minor=1_000_000, currency="IDR")
        result = run_match(conn, facts=facts)
        for candidate in result.candidates:
            assert candidate.eligible is True
            assert "BUDGET_WITHIN" not in reason_codes(candidate)
            assert candidate.basis["budget"] == "EXCEEDED"
            assert "BUDGET_EXCEEDED" in note_codes(candidate)
        po = run_match(conn, facts=base_facts(budget_minor=9_000_000, currency="IDR"))
        assert po.candidates[0].score_centi > result.candidates[0].score_centi

    def test_budget_without_a_rate_basis_is_unknown(self, conn):
        seed_base(conn, with_rate_card=False)
        result = run_match(conn, facts=base_facts(budget_minor=9_000_000, currency="IDR"))
        candidate = result.candidates[0]
        assert candidate.eligible is True
        assert "BUDGET_WITHIN" not in reason_codes(candidate)
        assert "UNKNOWN_BUDGET_BASIS" in candidate.unknowns
        assert candidate.basis["budget"] == "UNKNOWN"
        stored = json.loads(stored_candidate(conn, result.match_run_id, "unit_1")["reasons"])
        assert all(reason["code"] != "BUDGET_WITHIN" for reason in stored)

    def test_unknown_budget_basis_is_marked_in_serialised_output(self, conn):
        seed_base(conn, with_rate_card=False)
        result = run_match(conn, facts=base_facts(budget_minor=9_000_000, currency="IDR"))
        payload = canonical_json(result.to_dict())
        assert "UNKNOWN_BUDGET_BASIS" in payload
        assert "UNKNOWN" in payload
        assert "BUDGET_WITHIN" not in payload

    def test_no_budget_fact_is_unknown_not_assumed(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert "BUDGET_WITHIN" not in reason_codes(candidate)
        assert "UNKNOWN_BUDGET_CRITERIA" in candidate.unknowns
        assert candidate.basis["budget"] == "UNKNOWN"

    def test_budget_currency_mismatch_is_unknown(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(budget_minor=9_000_000, currency="USD"))
        candidate = result.candidates[0]
        assert "BUDGET_WITHIN" not in reason_codes(candidate)
        assert "UNKNOWN_BUDGET_CURRENCY" in candidate.unknowns
        assert "BUDGET_CURRENCY_UNCOMPARABLE" in note_codes(candidate)

    def test_budget_basis_uses_the_winning_season_priority(self, conn):
        seed_base(conn, with_rate_card=False)
        seed_rate_card(
            conn,
            seasons=(
                ("season_low", "2026-01-01", "2026-12-31", 1_000_000, 1, 0),
                ("season_high", "2026-10-01", "2026-10-31", 2_000_000, 1, 5),
            ),
        )
        candidate = run_match(conn, facts=base_facts(budget_minor=5_000_000, currency="IDR")).candidates[0]
        assert candidate.basis["accommodation_minor"] == 2_000_000 * NIGHTS
        assert "BUDGET_WITHIN" not in reason_codes(candidate)

    def test_ambiguous_rate_card_degrades_to_unknown(self, conn):
        seed_base(conn, with_rate_card=False)
        seed_rate_card(
            conn,
            seasons=(
                ("season_a", "2026-01-01", "2026-12-31", 1_000_000, 1, 0),
                ("season_b", "2026-01-01", "2026-12-31", 2_000_000, 1, 0),
            ),
        )
        result = run_match(conn, facts=base_facts(budget_minor=9_000_000, currency="IDR"))
        candidate = result.candidates[0]
        assert candidate.eligible is True
        assert candidate.basis["budget"] == "UNKNOWN"
        assert "RATE_AMBIGUOUS" in note_codes(candidate)
        assert "BUDGET_WITHIN" not in reason_codes(candidate)

    def test_budget_is_compared_in_minor_units_only(self, conn):
        seed_base(conn)
        candidate = run_match(
            conn, facts=base_facts(budget_minor=NIGHTLY * NIGHTS - 1, currency="IDR")
        ).candidates[0]
        assert "BUDGET_WITHIN" not in reason_codes(candidate)  # one centi short


# --------------------------------------------------------------------------- #
# 8. Unit status / property sellability
# --------------------------------------------------------------------------- #
class TestServiceability:
    def test_maintenance_unit_is_refused(self, conn):
        seed_base(conn)
        conn.execute("UPDATE units SET status = 'MAINTENANCE'")
        result = run_match(conn)
        candidate = result.candidates[0]
        assert candidate.deny_reasons == ("MAINTENANCE_BLOCK",)
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert row["eligible"] == 0
        assert row["score_centi"] == 0
        assert json.loads(row["deny_reasons"]) == ["MAINTENANCE_BLOCK"]
        assert conn.execute("SELECT status FROM units").fetchone()[0] == "MAINTENANCE"

    def test_suspended_unit_is_out_of_service(self, conn):
        seed_base(conn)
        conn.execute("UPDATE units SET status = 'SUSPENDED'")
        result = run_match(conn)
        assert result.candidates[0].deny_reasons == ("OUT_OF_SERVICE",)

    def test_not_sellable_property_is_refused(self, conn):
        seed_base(conn)
        conn.execute("UPDATE properties SET status = 'NOT_SELLABLE'")
        result = run_match(conn)
        candidate = result.candidates[0]
        assert candidate.deny_reasons == ("PROPERTY_NOT_SELLABLE",)
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert json.loads(row["deny_reasons"]) == ["PROPERTY_NOT_SELLABLE"]
        assert conn.execute("SELECT status FROM properties").fetchone()[0] == "NOT_SELLABLE"

    def test_suspended_property_is_refused(self, conn):
        seed_base(conn)
        conn.execute("UPDATE properties SET status = 'SUSPENDED'")
        assert run_match(conn).candidates[0].deny_reasons == ("PROPERTY_NOT_SELLABLE",)

    def test_sellable_with_review_stays_eligible_but_flags_review(self, conn):
        seed_base(conn)
        conn.execute("UPDATE properties SET status = 'SELLABLE_WITH_REVIEW'")
        result = run_match(conn)
        candidate = result.candidates[0]
        assert candidate.eligible is True
        assert candidate.review_required is True
        assert "PROPERTY_SELLABLE_WITH_REVIEW" in note_codes(candidate)

    def test_unactivated_contract_pack_flags_review_without_inventing_a_denial(self, conn):
        seed_base(conn)
        conn.execute("UPDATE properties SET contract_pack_status = 'DRAFT'")
        candidate = run_match(conn).candidates[0]
        assert candidate.eligible is True
        assert candidate.review_required is True
        assert "CONTRACT_PACK_NOT_ACTIVATED" in note_codes(candidate)

    def test_one_refused_unit_does_not_hide_the_others(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="SUSPENDED")
        seed_unit(conn, "unit_3")
        result = run_match(conn)
        by_unit = {c.unit_id: c for c in result.candidates}
        assert by_unit["unit_1"].eligible is True
        assert by_unit["unit_2"].deny_reasons == ("OUT_OF_SERVICE",)
        assert by_unit["unit_3"].eligible is True
        assert result.eligible_count == 2


# --------------------------------------------------------------------------- #
# 9. Minimum stay
# --------------------------------------------------------------------------- #
class TestMinStay:
    def test_policy_min_stay_below_the_stay_is_a_fit(self, conn):
        seed_base(conn, min_stay_nights=2)
        candidate = run_match(conn).candidates[0]
        assert "MIN_STAY_FIT" in reason_codes(candidate)
        assert candidate.basis["min_stay_required"] == 2

    def test_policy_min_stay_above_the_stay_is_a_refusal(self, conn):
        seed_base(conn, min_stay_nights=5)
        result = run_match(conn)
        candidate = result.candidates[0]
        assert "MIN_STAY_NOT_MET" in candidate.deny_reasons
        row = stored_candidate(conn, result.match_run_id, "unit_1")
        assert "MIN_STAY_NOT_MET" in json.loads(row["deny_reasons"])
        assert row["score_centi"] == 0

    def test_rate_card_season_min_stay_applies(self, conn):
        seed_base(conn, with_rate_card=False, min_stay_nights=1)
        seed_rate_card(conn, seasons=(("season_peak", "2026-01-01", "2026-12-31", NIGHTLY, 7, 0),))
        result = run_match(conn)
        candidate = result.candidates[0]
        assert "MIN_STAY_NOT_MET" in candidate.deny_reasons
        assert candidate.basis["min_stay_required"] == 7
        assert "rate_card:rc_1" in candidate.basis["min_stay_sources"]

    def test_the_stricter_of_policy_and_rate_card_wins(self, conn):
        seed_base(conn, with_rate_card=False, min_stay_nights=4)
        seed_rate_card(conn, seasons=(("season_short", "2026-01-01", "2026-12-31", NIGHTLY, 2, 0),))
        candidate = run_match(conn).candidates[0]
        assert candidate.basis["min_stay_required"] == 4
        assert "MIN_STAY_NOT_MET" in candidate.deny_reasons

    def test_missing_min_stay_basis_is_unknown_and_not_judged(self, conn):
        seed_base(conn, with_rate_card=False, with_policy=False)
        result = run_match(conn)
        candidate = result.candidates[0]
        assert candidate.eligible is True
        assert "MIN_STAY_FIT" not in reason_codes(candidate)
        assert "UNKNOWN_MIN_STAY_BASIS" in candidate.unknowns
        assert candidate.basis["min_stay"] == "UNKNOWN"

    def test_min_stay_refusal_keeps_the_unit_and_rates_intact(self, conn):
        seed_base(conn, min_stay_nights=5)
        result = run_match(conn)
        assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rate_seasons").fetchone()[0] == 1
        assert candidate_rows(conn, result.match_run_id)[0]["eligible"] == 0


# --------------------------------------------------------------------------- #
# 10. Amenities and bedrooms (no basis ⇒ UNKNOWN, never an invented reason)
# --------------------------------------------------------------------------- #
class TestAmenitiesAndBedrooms:
    def test_amenity_match_when_the_policy_declares_them(self, conn):
        seed_base(conn, policy=policy_payload(amenities=["pool", "wifi", "ac"]))
        candidate = run_match(conn, facts=base_facts(amenities="wifi, pool")).candidates[0]
        assert "AMENITY_MATCH" in reason_codes(candidate)
        assert candidate.basis["amenities"] == "MATCH"

    def test_partial_amenity_request_is_not_a_match(self, conn):
        seed_base(conn, policy=policy_payload(amenities=["pool"]))
        candidate = run_match(conn, facts=base_facts(amenities=["pool", "sauna"])).candidates[0]
        assert "AMENITY_MATCH" not in reason_codes(candidate)
        assert candidate.basis["amenities"] == "PARTIAL"
        assert note_codes(candidate) == {"AMENITY_MISSING"}

    def test_requested_amenities_without_a_declared_list_are_unknown(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(amenities=["pool"])).candidates[0]
        assert "AMENITY_MATCH" not in reason_codes(candidate)
        assert "UNKNOWN_AMENITY_BASIS" in candidate.unknowns
        assert candidate.basis["amenities"] == "UNKNOWN"

    def test_no_amenity_request_produces_no_amenity_reason(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert "AMENITY_MATCH" not in reason_codes(candidate)
        assert candidate.basis["amenities"] == "NOT_REQUESTED"
        assert "UNKNOWN_AMENITY_BASIS" not in candidate.unknowns

    def test_bedroom_fit_is_reasoned_when_requested(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(bedrooms=2)).candidates[0]
        assert "BEDROOM_FIT" in reason_codes(candidate)
        assert candidate.basis["bedrooms"] == "FIT"

    def test_bedroom_shortfall_is_noted_but_not_a_closed_set_denial(self, conn):
        seed_base(conn)
        candidate = run_match(conn, facts=base_facts(bedrooms=4)).candidates[0]
        assert candidate.eligible is True
        assert "BEDROOM_FIT" not in reason_codes(candidate)
        assert "BEDROOM_SHORTFALL" in note_codes(candidate)

    def test_missing_bedroom_request_is_unknown(self, conn):
        seed_base(conn)
        candidate = run_match(conn).candidates[0]
        assert "UNKNOWN_BEDROOM_BASIS" in candidate.unknowns
        assert candidate.basis["bedrooms"] == "UNKNOWN"


# --------------------------------------------------------------------------- #
# 11. Determinism and tie-breaking
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_two_runs_serialise_byte_for_byte(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2")
        seed_booking(conn, "book_1", unit_id="unit_2", check_in="2026-10-02", check_out="2026-10-05")
        facts = base_facts(area_preference="Ubud", budget_minor=5_000_000, currency="IDR")
        first = run_match(conn, facts=facts)
        second = run_match(conn, facts=facts)
        assert canonical_json(first.to_dict()) == canonical_json(second.to_dict())
        assert first.canonical() == second.canonical()

    def test_runs_differ_only_in_ledger_identity(self, conn):
        seed_base(conn)
        first = run_match(conn)
        second = run_match(conn)
        assert first.match_run_id != second.match_run_id
        assert first.created_at == second.created_at == "2026-09-21T08:00:00+00:00"
        assert first.to_dict() == second.to_dict()

    def test_persisted_payloads_are_identical_too(self, conn):
        seed_base(conn)
        first = run_match(conn)
        second = run_match(conn)
        rows_a = candidate_rows(conn, first.match_run_id)
        rows_b = candidate_rows(conn, second.match_run_id)
        for row in (rows_a + rows_b):
            row.pop("candidate_id")
            row.pop("match_run_id")
        assert [json.dumps(r, sort_keys=True) for r in rows_a] == [
            json.dumps(r, sort_keys=True) for r in rows_b
        ]

    def test_tie_break_is_ascending_unit_id(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_aaa")
        seed_unit(conn, "unit_zzz")
        result = run_match(conn)
        assert [c.unit_id for c in result.candidates] == ["unit_1", "unit_aaa", "unit_zzz"]
        assert [c.score_centi for c in result.candidates] == [result.candidates[0].score_centi] * 3

    def test_tie_break_survives_second_run(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_bbb")
        seed_unit(conn, "unit_ccc")
        first = [c.unit_id for c in run_match(conn).candidates]
        second = [c.unit_id for c in run_match(conn).candidates]
        assert first == second == ["unit_1", "unit_bbb", "unit_ccc"]

    def test_equal_scores_keep_a_total_order(self, conn):
        seed_base(conn)
        for unit_id in ("unit_d", "unit_a", "unit_c", "unit_b"):
            seed_unit(conn, unit_id)
        result = run_match(conn)
        keys = [(-c.score_centi, c.unit_id) for c in result.candidates]
        assert keys == sorted(keys)

    def test_higher_score_outranks_lower_score(self, conn):
        seed_base(conn)
        seed_property(conn, "prop_cheap", region="Ubud")
        seed_unit(conn, "unit_cheap", property_id="prop_cheap")
        seed_policy(conn, "pol_cheap", property_id="prop_cheap", min_stay_nights=2)
        # prop_cheap has no rate card at all: no BUDGET_WITHIN basis, lower score
        facts = base_facts(area_preference="Ubud", budget_minor=5_000_000, currency="IDR", bedrooms=2)
        result = run_match(conn, facts=facts)
        ranked = {c.unit_id: c for c in result.candidates}
        assert ranked["unit_1"].score_centi > ranked["unit_cheap"].score_centi
        assert ranked["unit_1"].rank < ranked["unit_cheap"].rank
        assert result.candidates[0].unit_id == "unit_1"
        scores = [c.score_centi for c in result.candidates if c.eligible]
        assert scores == sorted(scores, reverse=True)

    def test_ranking_is_not_affected_by_insertion_order(self, conn):
        from schema_loader import connect_sqlite

        seed_base(conn)
        seed_unit(conn, "unit_2", bedrooms=1)
        facts = base_facts(bedrooms=2)
        first = [(c.unit_id, c.rank, c.score_centi) for c in run_match(conn, facts=facts).candidates]

        other = connect_sqlite(":memory:")  # same inventory, added in the opposite order
        try:
            seed_property(other)
            seed_unit(other, "unit_2", bedrooms=1)
            seed_unit(other, "unit_1")
            seed_policy(other)
            seed_rate_card(other)
            seed_case(other)
            second = [
                (c.unit_id, c.rank, c.score_centi)
                for c in run_match(other, facts=facts, kernel=kernel_for(other)).candidates
            ]
        finally:
            other.close()
        assert first == second

    def test_ids_factory_injection_does_not_change_the_decision(self, conn):
        seed_base(conn)
        one = run_match(conn, ids=IdFactory(seed="alpha"))
        two = run_match(conn, ids=IdFactory(seed="omega"))
        assert one.to_dict() == two.to_dict()
        assert one.match_run_id != two.match_run_id


# --------------------------------------------------------------------------- #
# 12. explain() — ordered reasons for a human agent, resolvable evidence
# --------------------------------------------------------------------------- #
class TestExplain:
    def test_explain_is_a_string_report(self, conn):
        seed_base(conn)
        result = run_match(conn)
        text = M.explain(conn, result.match_run_id)
        assert isinstance(text, str)
        assert "match_run" in text
        assert "case_1" in text
        assert text.match_run_id == result.match_run_id

    def test_explain_lists_candidates_in_rank_order(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn)
        text = M.explain(conn, result.match_run_id)
        positions = [text.index(f"#{rank} ") for rank in (1, 2)]
        assert positions == sorted(positions)
        assert "unit_1" in text
        assert "unit_2" in text

    def test_explain_shows_reasons_and_denials(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        text = M.explain(conn, run_match(conn).match_run_id)
        assert "CAPACITY_FIT" in text
        assert "AVAILABLE" in text
        assert "MAINTENANCE_BLOCK" in text
        assert "ELIGIBLE" in text
        assert "DENIED" in text

    def test_explain_names_the_kernel_as_the_availability_source(self, conn):
        seed_base(conn)
        text = M.explain(conn, run_match(conn).match_run_id)
        assert "reservation_kernel.occupancy" in text

    def test_explain_mentions_every_evidence_id(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_real", check_in="2026-10-02", check_out="2026-10-05")
        result = run_match(conn)
        text = M.explain(conn, result.match_run_id)
        assert "book_real" in text
        assert "CONFIRMED" in text

    def test_explain_resolves_every_evidence_ref_against_the_database(self, conn):
        seed_base(conn)
        seed_booking(conn, "book_real", check_in="2026-10-02", check_out="2026-10-05")
        seed_hold(conn, "hold_real", unit_id="unit_1", check_in="2026-10-03", check_out="2026-10-07")
        result = run_match(conn)
        explained = M.explain(conn, result.match_run_id)
        refs = [entry["ref"] for entry in explained.evidence_refs]
        assert refs == ["book_real", "hold_real"]
        for entry in explained.evidence_refs:
            assert entry["kind"] in {"booking", "hold", "availability_evidence"}
            assert entry["status"]

    def test_explain_fails_closed_on_a_dangling_evidence_ref(self, conn):
        seed_base(conn)
        blocking = _StubKernel(
            {
                "unit_1": (
                    {
                        "kind": "booking",
                        "id": "book_ghost",
                        "status": "CONFIRMED",
                        "check_in": CHECK_IN,
                        "check_out": CHECK_OUT,
                    },
                )
            }
        )
        result = run_match(conn, kernel=blocking)
        with pytest.raises(DomainError) as excinfo:
            M.explain(conn, result.match_run_id)
        assert excinfo.value.code == "MATCH_EVIDENCE_MISSING"
        assert "book_ghost" in str(excinfo.value.details.get("evidence_refs", "book_ghost"))

    def test_explain_unknown_run_raises(self, conn):
        seed_base(conn)
        with pytest.raises(M.MatchRunNotFound) as excinfo:
            M.explain(conn, "match_run_does_not_exist")
        assert excinfo.value.code == "MATCH_RUN_NOT_FOUND"

    def test_explain_as_dict_mirrors_the_report(self, conn):
        seed_base(conn)
        result = run_match(conn)
        explained = M.explain(conn, result.match_run_id)
        payload = explained.as_dict()
        assert payload["match_run_id"] == result.match_run_id
        assert payload["case_id"] == "case_1"
        assert payload["considered_count"] == result.considered_count
        assert [c["rank"] for c in payload["candidates"]] == [1]

    def test_explain_accepts_the_arguments_in_either_order(self, conn):
        seed_base(conn)
        result = run_match(conn)
        assert M.explain(conn, result.match_run_id) == M.explain(result.match_run_id, conn)

    def test_explain_of_an_older_run_still_works_after_a_new_run(self, conn):
        seed_base(conn)
        first = run_match(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        run_match(conn)
        text = M.explain(conn, first.match_run_id)
        assert "unit_2" not in text
        assert text.considered_count == 1


# --------------------------------------------------------------------------- #
# 13. Cross-cutting invariants
# --------------------------------------------------------------------------- #
class TestInvariants:
    def test_every_deny_code_has_at_least_one_reachable_case(self, conn):
        seen: set[str] = set()
        seed_base(conn)
        seed_property(conn, "prop_strict", region="Ubud")
        seed_unit(conn, "unit_strict", property_id="prop_strict")
        seed_policy(conn, "pol_strict", property_id="prop_strict", min_stay_nights=9)

        conn.execute("UPDATE properties SET status = 'NOT_SELLABLE'")
        seen |= set(run_match(conn).candidate_for("unit_1").deny_reasons)
        conn.execute("UPDATE properties SET status = 'SELLABLE'")

        conn.execute("UPDATE units SET status = 'SUSPENDED' WHERE unit_id = 'unit_1'")
        seen |= set(run_match(conn).candidate_for("unit_1").deny_reasons)
        conn.execute("UPDATE units SET status = 'MAINTENANCE' WHERE unit_id = 'unit_1'")
        seen |= set(run_match(conn).candidate_for("unit_1").deny_reasons)
        conn.execute("UPDATE units SET status = 'ACTIVE' WHERE unit_id = 'unit_1'")

        seen |= set(run_match(conn, facts=base_facts(adults=9)).candidate_for("unit_1").deny_reasons)
        seen |= set(
            run_match(conn, facts=base_facts(area_preference="Seminyak"))
            .candidate_for("unit_1")
            .deny_reasons
        )
        seen |= set(run_match(conn).candidate_for("unit_strict").deny_reasons)

        seed_booking(conn, "book_seen", check_in="2026-10-02", check_out="2026-10-05")
        seen |= set(run_match(conn).candidate_for("unit_1").deny_reasons)

        assert seen == set(M.DENY_REASON_CODES)

    def test_deny_reasons_are_sorted_in_the_canonical_order(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_bad", max_guests=1, base_capacity=1, status="MAINTENANCE")
        seed_booking(conn, "book_x", unit_id="unit_bad", check_in="2026-10-02", check_out="2026-10-05")
        candidate = next(
            c for c in run_match(conn, facts=base_facts(adults=4)).candidates if c.unit_id == "unit_bad"
        )
        assert list(candidate.deny_reasons) == sorted(
            candidate.deny_reasons, key=M.DENY_REASON_CODES.index
        )
        assert set(candidate.deny_reasons) == {
            "CAPACITY_EXCEEDED",
            "MAINTENANCE_BLOCK",
            "DATES_UNAVAILABLE",
        }

    def test_module_constants_are_the_documented_ones(self):
        assert M.DENY_REASON_CODES == (
            "DATES_UNAVAILABLE",
            "CAPACITY_EXCEEDED",
            "MIN_STAY_NOT_MET",
            "OUT_OF_SERVICE",
            "PROPERTY_NOT_SELLABLE",
            "AREA_MISMATCH",
            "MAINTENANCE_BLOCK",
        )
        assert M.POSITIVE_REASON_CODES == (
            "CAPACITY_FIT",
            "BEDROOM_FIT",
            "AREA_MATCH",
            "BUDGET_WITHIN",
            "AMENITY_MATCH",
            "AVAILABLE",
            "MIN_STAY_FIT",
        )
        assert set(M.REASON_WEIGHTS) == set(M.POSITIVE_REASON_CODES)
        assert M.UNKNOWN == "UNKNOWN"

    def test_date_objects_and_iso_strings_are_equivalent(self, conn):
        seed_base(conn)
        iso = run_match(conn, facts=base_facts())
        typed = run_match(
            conn,
            facts={
                "check_in": date(2026, 10, 1),
                "check_out": date(2026, 10, 4),
                "adults": 2,
            },
        )
        assert iso.to_dict() == typed.to_dict()

    def test_extra_fact_keys_do_not_leak_into_reasons(self, conn):
        seed_base(conn)
        result = run_match(conn, facts=base_facts(purpose="honeymoon", special_requests="late check-in"))
        candidate = result.candidates[0]
        assert reason_codes(candidate) <= set(M.POSITIVE_REASON_CODES)
        assert candidate.eligible is True

    def test_matching_never_writes_to_other_tables(self, conn):
        seed_base(conn)
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("units", "properties", "policy_versions", "rate_cards", "bookings", "holds", "cases")
        }
        run_match(conn)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("units", "properties", "policy_versions", "rate_cards", "bookings", "holds", "cases")
        }
        assert before == after

    def test_explain_helps_an_agent_pick_the_top_unit(self, conn):
        seed_base(conn)
        seed_unit(conn, "unit_2", status="MAINTENANCE")
        result = run_match(conn, facts=base_facts(budget_minor=5_000_000, currency="IDR"))
        text = M.explain(conn, result.match_run_id)
        top = result.candidates[0]
        assert f"#{top.rank} [ELIGIBLE" in text
        assert top.unit_id in text
        assert f"score={top.score_centi} centi" in text

"""WAVE 4-A tests: the canonical availability vocabulary and the restricted state.

§11 is explicit: "Min-stay/occupancy restrictions menghasilkan explicit restricted
state, bukan NOT_AVAILABLE". Appendix A fixes the canonical vocabulary as
UNKNOWN / CHECK_REQUESTED / CHECKING / AVAILABLE / NOT_AVAILABLE /
AVAILABLE_BUT_RESTRICTED / RECONFIRMATION_REQUIRED.

The brownfield table said ``result IN ('AVAILABLE','OCCUPIED','UNKNOWN')``: no
restricted state, no reconfirmation, and a value (``OCCUPIED``) that Appendix A does
not contain. These tests pin the alignment.

The state is *derived* from evidence, never stored: one fact, one home. Stale evidence
can only ever produce RECONFIRMATION_REQUIRED, never AVAILABLE — an old "yes" is not a
current "yes".
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from availability import (
    AVAILABILITY_STATES,
    REQUEST_STATES,
    RESTRICTION_KEYS,
    SOURCE_RESULTS,
    AvailabilityService,
    derive_state,
)
from primitives import DomainError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "src" / "schema" / "001_initial.sql"
NOW = "2026-09-26T09:00:00+00:00"


def evidence(
    *,
    result: str = "AVAILABLE",
    level: str = "A",
    captured_at: str | None = NOW,
    valid_until: str | None = None,
    restrictions: object = None,
    evidence_id: str = "EV-1",
    **over: object,
) -> dict[str, object]:
    """One evidence row, shaped like the table (plus whatever the test overrides)."""
    row: dict[str, object] = {
        "evidence_id": evidence_id,
        "unit_id": "U-1",
        "evidence_level": level,
        "source_type": "property_api",
        "captured_at": captured_at,
        "valid_until": valid_until,
        "result": result,
        "restrictions": restrictions,
        "can_quote": True,
        "can_hold": True,
        "can_confirm": True,
    }
    row.update(over)
    return row


def ddl_block(table: str) -> str:
    text = SCHEMA.read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE {table}\s*\((.*?)\n\);", text, re.S)
    assert match is not None, f"no DDL for {table}"
    return match.group(1)


def ddl_enum(block: str, column: str) -> tuple[str, ...]:
    match = re.search(rf"(?<!\w){column}\s+IN\s*\(([^)]*)\)", block)
    assert match is not None, f"no CHECK ... IN (...) for {column!r}"
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


# -- the vocabulary --------------------------------------------------------- #


def test_the_canonical_vocabulary_is_appendix_a_verbatim():
    assert AVAILABILITY_STATES == (
        "UNKNOWN",
        "CHECK_REQUESTED",
        "CHECKING",
        "AVAILABLE",
        "NOT_AVAILABLE",
        "AVAILABLE_BUT_RESTRICTED",
        "RECONFIRMATION_REQUIRED",
    )


def test_the_check_states_are_states_not_source_results():
    """A supplier reports a result; only the platform asks for a check."""
    assert REQUEST_STATES == ("CHECK_REQUESTED", "CHECKING")
    assert not set(REQUEST_STATES) & set(SOURCE_RESULTS)


def test_the_source_vocabulary_is_the_canonical_subset():
    assert SOURCE_RESULTS == ("AVAILABLE", "NOT_AVAILABLE", "AVAILABLE_BUT_RESTRICTED", "UNKNOWN")


def test_the_schema_pins_the_canonical_source_results():
    assert ddl_enum(ddl_block("availability_evidence"), "result") == SOURCE_RESULTS


def test_the_restricted_result_must_state_its_restriction():
    """A restricted state without a reason is just a worse NOT_AVAILABLE."""
    block = ddl_block("availability_evidence")
    assert "ck_evidence_restriction_stated" in block
    assert re.search(
        r"ck_evidence_restriction_stated\s+CHECK\s*\(\s*result\s*<>\s*'AVAILABLE_BUT_RESTRICTED'"
        r"\s+OR\s+restrictions\s+IS\s+NOT\s+NULL\s*\)",
        block,
    )


def test_the_restriction_keys_are_the_two_the_spec_names():
    assert RESTRICTION_KEYS == ("min_stay_nights", "max_occupancy")


def test_no_source_file_still_speaks_of_occupied():
    """``OCCUPIED`` was never a canonical value — not in the DDL, not in code."""
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.sql")) + sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"['\"]OCCUPIED['\"]", text):
            offenders.append(path.name)
    assert offenders == []


# -- derivation ------------------------------------------------------------- #


def test_available_evidence_derives_available():
    state = derive_state(evidence(), at=NOW)
    assert state.state == "AVAILABLE"
    assert state.reason_code == "OK"


def test_not_available_evidence_derives_not_available():
    state = derive_state(evidence(result="NOT_AVAILABLE"), at=NOW)
    assert state.state == "NOT_AVAILABLE"
    assert state.reason_code == "UNIT_OCCUPIED"


def test_restricted_evidence_derives_restricted_never_not_available():
    """§11, verbatim: restricted is its own state, not a flavour of unavailable."""
    state = derive_state(
        evidence(result="AVAILABLE_BUT_RESTRICTED", restrictions={"min_stay_nights": 3}),
        at=NOW,
    )
    assert state.state == "AVAILABLE_BUT_RESTRICTED"
    assert state.state != "NOT_AVAILABLE"
    assert state.reason_code == "AVAILABILITY_RESTRICTED"
    assert state.restrictions == {"min_stay_nights": 3}


def test_unknown_result_derives_unknown():
    assert derive_state(evidence(result="UNKNOWN"), at=NOW).state == "UNKNOWN"


def test_missing_result_derives_unknown():
    assert derive_state(evidence(result=""), at=NOW).state == "UNKNOWN"


def test_stale_evidence_derives_reconfirmation_required():
    """Level A is fresh for 900s; an hour later the answer is "ask again"."""
    state = derive_state(evidence(level="A", captured_at="2026-09-26T08:00:00+00:00"), at=NOW)
    assert state.state == "RECONFIRMATION_REQUIRED"
    assert state.reason_code == "EVIDENCE_STALE"


def test_expired_evidence_derives_reconfirmation_required():
    state = derive_state(evidence(valid_until="2026-09-26T08:30:00+00:00"), at=NOW)
    assert state.state == "RECONFIRMATION_REQUIRED"
    assert state.reason_code == "EVIDENCE_EXPIRED"


def test_staleness_is_checked_before_the_result():
    """An old "yes" is not a current "yes" — freshness beats a favourable result."""
    state = derive_state(
        evidence(result="AVAILABLE", captured_at="2026-09-26T00:00:00+00:00"), at=NOW
    )
    assert state.state == "RECONFIRMATION_REQUIRED"


def test_a_stale_restricted_evidence_is_reconfirmed_not_restricted():
    state = derive_state(
        evidence(
            result="AVAILABLE_BUT_RESTRICTED",
            restrictions={"max_occupancy": 4},
            captured_at="2026-09-25T00:00:00+00:00",
        ),
        at=NOW,
    )
    assert state.state == "RECONFIRMATION_REQUIRED"


def test_the_derived_state_carries_its_evidence_provenance():
    state = derive_state(evidence(evidence_id="EV-9", level="B"), at=NOW)
    assert state.evidence_id == "EV-9"
    assert state.evidence_level == "B"


def test_the_state_is_derived_not_stored():
    """One fact, one home: a stored state column would drift from the evidence."""
    assert "state" not in [c.lower() for c in re.findall(r"^\s{4}(\w+)\s", ddl_block("availability_evidence"), re.M)]


# -- the service reads the row --------------------------------------------- #


def _unit(conn):
    """A property + unit, so evidence has something to point at."""
    from reservation_kernel import ReservationKernel

    kernel = ReservationKernel(conn)
    kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
    kernel.upsert_unit(unit_id="unit_1", property_id="prop_1", name="Villa unit_1")
    return kernel


def test_the_service_derives_the_state_of_a_stored_evidence_row(conn):
    from primitives import FrozenClock

    kernel = _unit(conn)
    evidence_id = kernel.record_evidence(
        unit_id="unit_1",
        source_type="property_api",
        evidence_level="A",
        result="AVAILABLE",
        captured_at=NOW,
    )
    svc = AvailabilityService(conn, clock=FrozenClock.at(NOW))
    assert svc.state_of(evidence_id).state == "AVAILABLE"


def test_the_service_refuses_an_unknown_evidence_id(conn):
    svc = AvailabilityService(conn)
    with pytest.raises(DomainError) as exc:
        svc.state_of("EV-does-not-exist")
    assert exc.value.code == "EVIDENCE_NOT_FOUND"


def test_the_service_does_not_invent_a_state_without_a_result(conn):
    from primitives import FrozenClock

    kernel = _unit(conn)
    evidence_id = kernel.record_evidence(
        unit_id="unit_1",
        source_type="property_api",
        evidence_level="A",
        result="UNKNOWN",
        captured_at=NOW,
    )
    svc = AvailabilityService(conn, clock=FrozenClock.at(NOW))
    assert svc.state_of(evidence_id).state == "UNKNOWN"


def test_undated_evidence_derives_unknown_not_available():
    """Evidence with no timestamp cannot support a claim in either direction."""
    state = derive_state(evidence(captured_at=None), at=NOW)
    assert state.state == "UNKNOWN"
    assert state.reason_code == "EVIDENCE_TIMESTAMP_INVALID"


# -- the writer refuses the non-canonical ---------------------------------- #


def test_the_writer_refuses_a_legacy_occupied_result(conn):
    from reservation_kernel import ReservationKernel

    kernel = ReservationKernel(conn)
    with pytest.raises(DomainError) as exc:
        kernel.record_evidence(
            unit_id="U-1", source_type="property_api", evidence_level="A", result="OCCUPIED"
        )
    assert exc.value.code == "EVIDENCE_INVALID_RESULT"
    assert "NOT_AVAILABLE" in str(exc.value)


def test_the_writer_refuses_a_restricted_result_without_a_restriction(conn):
    from reservation_kernel import ReservationKernel

    kernel = ReservationKernel(conn)
    with pytest.raises(DomainError) as exc:
        kernel.record_evidence(
            unit_id="U-1",
            source_type="property_api",
            evidence_level="A",
            result="AVAILABLE_BUT_RESTRICTED",
        )
    assert exc.value.code == "EVIDENCE_RESTRICTION_REQUIRED"


def test_the_writer_refuses_an_unknown_restriction_key(conn):
    from reservation_kernel import ReservationKernel

    kernel = ReservationKernel(conn)
    with pytest.raises(DomainError) as exc:
        kernel.record_evidence(
            unit_id="U-1",
            source_type="property_api",
            evidence_level="A",
            result="AVAILABLE_BUT_RESTRICTED",
            restrictions={"min_stay_nights": 3, "vibe": "nice"},
        )
    assert exc.value.code == "EVIDENCE_INVALID_RESTRICTIONS"


def test_the_writer_refuses_a_negative_restriction(conn):
    from reservation_kernel import ReservationKernel

    kernel = ReservationKernel(conn)
    with pytest.raises(DomainError) as exc:
        kernel.record_evidence(
            unit_id="U-1",
            source_type="property_api",
            evidence_level="A",
            result="AVAILABLE_BUT_RESTRICTED",
            restrictions={"min_stay_nights": 0},
        )
    assert exc.value.code == "EVIDENCE_INVALID_RESTRICTIONS"


# -- the gate refuses, but says why ---------------------------------------- #


def test_a_restricted_evidence_denies_a_hold_and_names_the_restriction():
    from gates import GATE_AVAILABILITY, gate_availability

    decision = gate_availability(
        evidence(result="AVAILABLE_BUT_RESTRICTED", restrictions={"min_stay_nights": 3}),
        now=NOW,
        purpose="hold",
    )
    assert decision.denied
    assert decision.code == "AVAILABILITY_RESTRICTED"
    assert decision.gate == GATE_AVAILABILITY
    assert decision.details.get("restrictions") == {"min_stay_nights": 3}


def test_a_restricted_denial_is_not_an_unavailable_denial():
    """The two must not collapse: §11 keeps them distinct all the way to the UI."""
    from gates import gate_availability

    restricted = gate_availability(
        evidence(result="AVAILABLE_BUT_RESTRICTED", restrictions={"max_occupancy": 2}),
        now=NOW,
        purpose="quote",
    )
    unavailable = gate_availability(evidence(result="NOT_AVAILABLE"), now=NOW, purpose="quote")
    assert restricted.code != unavailable.code
    assert unavailable.code == "UNIT_OCCUPIED"

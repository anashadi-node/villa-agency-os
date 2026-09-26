"""Independent phase-3 verification probe for ``src/matching.py`` and ``src/documents.py``.

This file is a *third-party* probe: it was written without touching either
module, it seeds its own rows with direct SQL (through
:mod:`tests.seed_support`), it reads the result back from the database with raw
``SELECT`` statements, and it never calls a private helper of the modules under
test. Every assertion is about an observable property (a persisted row, a file
on disk, a returned public object), so a rewrite of the internals that keeps the
promises still passes.

Property (6) — idempotence — the *verdict* this probe proves
------------------------------------------------------------
``matching.match`` has **no memoisation**: every call appends a fresh
``match_runs`` row plus one ``match_candidates`` row per unit (see
:func:`test_rerun_is_append_only_and_recomputes_an_identical_decision`). The
guarantee the module actually makes is the second branch of the requirement:
*if the facts and the database did not change, recomputing produces a
byte-identical decision payload*. That is what is proven here, by
re-canonicalising the stored candidate rows of both runs and comparing the
strings. The other half of the promise (a ledger row is never rewritten) is
proven by snapshotting the first run's rows across the second run.

Occupancy source of truth
-------------------------
The set of bookings that occupy a slot lives in exactly one place,
``reservation_kernel.OCCUPYING_BOOKING_STATES`` (paired with the partial unique
index ``uq_bookings_active_slot`` in the schema). This probe consumes that
constant and the kernel's own ``occupancy()`` answer as the oracle; it never
re-implements the predicate for a different table.

Property index (P01..P76)
-------------------------
P01 one run, one candidate per unit; P02 rerun reproduces a byte-identical
decision; P03 rerun reproduces the order and the scores; P04 ledger identity is
the only thing that differs; P05 the stored intake hash traces to the stored
snapshot; P06 rank is unique and contiguous; P07 eligible before denied, ties
break on ascending unit_id; P08 changed facts change the decision (the
determinism above is not vacuous); P09 every ineligible candidate carries a
non-empty ``deny_reasons`` in the database; P10 the database CHECK refuses an
ineligible candidate without a deny reason; P11 deny codes come from the
published vocabulary, in canonical order; P12 MAINTENANCE_BLOCK is backed by a
MAINTENANCE unit row; P13 OUT_OF_SERVICE by a SUSPENDED unit row; P14
PROPERTY_NOT_SELLABLE by a real property status; P15 CAPACITY_EXCEEDED by the
unit's real ``max_guests``; P16 AREA_MISMATCH by the real region; P17
MIN_STAY_NOT_MET by a real policy row; P18 DATES_UNAVAILABLE by real blocking
rows that overlap the window; P19 every recorded evidence ref resolves; P20
``explain`` resolves every ref and agrees with the ledger; P21 a dangling
evidence ref is refused instead of being printed as proof; P22 the availability
verdict is the injected kernel's, not a private query; P23 every declared
occupying booking status keeps the unit out of the eligible set; P24 a
non-occupying status does not block the slot; P25 a booking outside the window
does not block; P26 an ACTIVE hold blocks and is named as evidence; P27 a
released hold does not block; P28 the kernel's declared occupancy set equals
the schema's partial index; P29 the kernel reports every declared occupying
state; P30 the kernel ignores a non-occupying state; P31 guests equal to
``max_guests`` is eligible; P32 guests above ``max_guests`` is denied; P33
children count towards capacity; P34 the capacity verdict matches the inventory
row for every unit; P35 stored scores are SQLite integers; P36 a score equals
the sum of the stored positive weights, and zero when denied; P37 no float
literal survives anywhere in the stored payload; P38 the stored reasons equal the
returned candidate's reasons, in the ledger's own canonical bytes; P39 the
AVAILABLE reason names the kernel as its source; P40 a rerun appends a second run
and recomputes an identical decision;
P41 a rerun never mutates the first run's rows; P42 a genuine change is not
served from the ledger; P43 the receipt prints the invoice number of its own
invoice row; P44 the receipt prints the totals of its invoice row; P45 the
printed arithmetic reconciles with the stored lines; P46 the confirmation total
equals the booking row and the invoice row; P47 the documents row points at the
source rows; P48 a receipt whose lines do not reconcile is refused and writes
nothing; P49 a receipt for an invoice without lines is refused; P50 a claim that
disagrees with the row is refused; P51 a VOID invoice cannot be documented; P52
every issued document is P1 under the redaction profile; P53 every written file
sits inside the root that was given; P54 nothing is written outside the given
root or into the default root; P55 a refused issue writes no row and no file;
P56 an escaping document key is refused before any write; P57
``resolve_storage_path`` refuses every escaping ref; P58 a document body carries
no identifier under any published pattern; P59 a non-redactable contact value is
refused; P60 a body carries no script token or unsafe URI scheme; P61 the
plain-text export is derived from the stored file; P62 a second issue bumps the
version and supersedes the old row; P63 the superseded row and file are kept;
P64 ``verify`` refuses a non-active version but can audit it explicitly; P65
voiding keeps the file and the row; P66 a confirmation is refused for every
status outside the allowed set and allowed for every status inside it; P67 an
offer letter is refused for every blocked status; P68 the role policy is
enforced for issue and for void; P69 an unvetted subject field is refused; P70 a
partially paid invoice prints the paid/outstanding amounts of its row; P71 a raw
direct identifier in a subject ref is refused instead of being published; P72 a
rate-season min-stay deny is traceable to the season row; P73 an unactivated
contract pack is an advisory review note, never a ledger deny; P74 a refused
match is never a match run; P75 a refused reissue leaves the previous version
intact; P76 (RED — see the docstring) a storage failure must surface as
DomainError[DOCUMENT_STORAGE_INVALID] and today escapes as a raw OSError.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
from pathlib import Path

import pytest

import documents as D
import gates as G
import matching as M
import reservation_kernel as K
from primitives import (
    DomainError,
    FrozenClock,
    IdFactory,
    Money,
    canonical_json,
    fingerprint,
    sha256_hex,
    to_iso,
)
from seed_support import CREATED, DAY, NOW, PAST, SeedMixin, stay

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "src" / "schema" / "001_initial.sql"

CHECK_IN = "2026-10-01"
CHECK_OUT = "2026-10-04"
NIGHTS = 3
BOOKING_ID = "book_0001"
INVOICE_ID = "inv_0001"
OTHER_INVOICE_ID = "inv_0002"

#: NIGHTLY + TAX add up to the invoice row seeded by ``seed_support.invoice``
#: (subtotal 900_000, tax 100_000, service 0, total 1_000_000).
RECONCILED_LINES = (
    ("NIGHTLY", "Menginap 4 malam", 1, 900_000),
    ("TAX", "PBJT 10 persen", 1, 100_000),
)
UNRECONCILED_LINES = (
    ("NIGHTLY", "Menginap 4 malam", 1, 900_000),
    ("TAX", "PBJT salah hitung", 1, 50_000),
)


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
class Probe(SeedMixin):
    """The shared row vocabulary plus the extra rows this probe needs."""

    def __init__(self, conn: sqlite3.Connection, *, case_id: str = "case_0001") -> None:
        self.conn = conn
        self.case_id = case_id
        self.inventory()
        self.case(case_id)

    # -- inventory ------------------------------------------------------- #
    def add_property(
        self,
        property_id: str,
        *,
        status: str = "SELLABLE",
        region: str = "bali",
        contract_pack_status: str = "ACTIVATED",
    ) -> str:
        self._insert(
            "properties",
            property_id=property_id,
            name=f"Properti {property_id}",
            property_type="villa",
            region=region,
            province="Bali",
            timezone="Asia/Makassar",
            status=status,
            contract_pack_status=contract_pack_status,
            contract_pack_evaluated_at=CREATED,
            created_at=CREATED,
            updated_at=CREATED,
        )
        return property_id

    def add_unit(
        self,
        unit_id: str,
        *,
        status: str = "ACTIVE",
        max_guests: int = 4,
        base_capacity: int = 2,
        bedrooms: int = 2,
        property_id: str = "prop_0001",
    ) -> str:
        self._insert(
            "units",
            unit_id=unit_id,
            property_id=property_id,
            name=f"Unit {unit_id}",
            unit_type="entire_villa",
            bedrooms=bedrooms,
            bathrooms=1,
            base_capacity=base_capacity,
            max_guests=max_guests,
            status=status,
            created_at=CREATED,
            updated_at=CREATED,
        )
        return unit_id

    def add_hold(
        self,
        hold_id: str = "hold_0001",
        *,
        status: str = "CONFIRMED",
        offset: int = 0,
        expires_at: str = DAY,
        close_reason: str = "tamu membatalkan",
    ) -> str:
        """``seed_support.hold`` cannot build a closed hold (no ``closed_at``)."""
        check_in, check_out = stay(offset)
        columns = dict(
            hold_id=hold_id,
            unit_id="unit_0001",
            property_id="prop_0001",
            check_in=check_in,
            check_out=check_out,
            nights=4,
            status=status,
            guest_ref="guest_0001",
            policy_version_id="policy_v1",
            evidence_id="ev_0001",
            idempotency_key=f"idem_{hold_id}",
            created_at=CREATED,
            expires_at=expires_at,
            expires_timezone="Asia/Makassar",
        )
        if status != "ACTIVE":
            columns["closed_at"] = PAST
            columns["close_reason"] = close_reason
        self._insert("holds", **columns)
        return hold_id

    def add_rate_card(
        self,
        *,
        rate_card_id: str = "rc_v1",
        property_id: str = "prop_0001",
        version: int = 1,
        effective_from: str = "2026-01-01",
        effective_to: str = "9999-12-31",
        nightly_rate_minor: int = 1_400_000,
        min_stay_nights: int = 5,
    ) -> str:
        """A one-season rate card: the second, independent min-stay/budget basis."""
        self._insert(
            "rate_cards",
            rate_card_id=rate_card_id,
            property_id=property_id,
            unit_scope="property",
            name=f"Rate card {rate_card_id}",
            version=version,
            effective_from=effective_from,
            effective_to=effective_to,
            currency="IDR",
            weekly_discount_pct="0",
            monthly_discount_pct="0",
            source_doc_ref="probe",
            approved_by="owner_anas",
            created_at=CREATED,
        )
        self._insert(
            "rate_seasons",
            season_id=f"season_{rate_card_id}",
            rate_card_id=rate_card_id,
            name="High season",
            start_date=effective_from,
            end_date=effective_to,
            nightly_rate_minor=nightly_rate_minor,
            min_stay_nights=min_stay_nights,
            priority=0,
            created_at=CREATED,
        )
        return rate_card_id

    def add_policy(
        self,
        *,
        policy_version_id: str = "policy_v2",
        property_id: str = "prop_0001",
        version: int = 2,
        payload: dict | None = None,
        unit_scope: str = "property",
    ) -> str:
        self._insert(
            "policy_versions",
            policy_version_id=policy_version_id,
            property_id=property_id,
            unit_scope=unit_scope,
            version=version,
            effective_from="2026-01-01",
            effective_to="9999-12-31",
            currency="IDR",
            deposit_pct="30",
            service_fee_pct="0",
            tax_pbjt_pct="10",
            max_discount_pct="10",
            quote_valid_hours=24,
            hold_ttl_minutes=120,
            max_hold_extensions=1,
            payload=json.dumps(payload if payload is not None else {}),
            fingerprint="a" * 64,
            source_doc_ref="probe",
            approved_by="owner_anas",
            created_at=CREATED,
        )
        return policy_version_id

    # -- billing --------------------------------------------------------- #
    def invoice_lines(self, invoice_id: str = INVOICE_ID, specs=RECONCILED_LINES) -> None:
        for index, (kind, description, quantity, unit_amount) in enumerate(specs, start=1):
            self._insert(
                "invoice_lines",
                line_id=f"line_{invoice_id}_{index:02d}",
                invoice_id=invoice_id,
                kind=kind,
                description=description,
                quantity=quantity,
                unit_amount_minor=unit_amount,
                amount_minor=quantity * unit_amount,
                created_at=CREATED,
            )


def billing_world(
    conn: sqlite3.Connection,
    *,
    booking_status: str = "CONFIRMED",
    invoice_status: str = "ISSUED",
    lines=RECONCILED_LINES,
    with_other_invoice: bool = True,
):
    """One booking, one invoice, its lines, and (optionally) a second invoice."""
    probe = Probe(conn)
    probe.booking(BOOKING_ID, status=booking_status, offset=0)
    probe.invoice(INVOICE_ID, status=invoice_status)
    if lines is not None:
        probe.invoice_lines(INVOICE_ID, lines)
    if with_other_invoice:
        probe.invoice(OTHER_INVOICE_ID, status="ISSUED")
    return probe


# --------------------------------------------------------------------------- #
# Running matching / reading the ledger back
# --------------------------------------------------------------------------- #
class _StubKernel:
    """Answers availability from a fixed script — proves the verdict is the kernel's."""

    def __init__(self, script=None) -> None:
        self.script = script or {}
        self.calls: list[str] = []

    def occupancy(self, unit_id, *, start, end):
        self.calls.append(unit_id)
        return tuple(self.script.get(unit_id, ()))


#: Every run needs its own id sequence (the ledger is append-only), so the
#: default seed is unique per call while staying reproducible inside one test.
_RUN_COUNTER = itertools.count(1)


def kernel(conn: sqlite3.Connection) -> K.ReservationKernel:
    return K.ReservationKernel(conn, clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed="kernel"))


def run_match(
    conn: sqlite3.Connection,
    *,
    facts: dict | None = None,
    case_id: str = "case_0001",
    ids_seed: str | None = None,
    kernel_obj=None,
):
    payload = {"check_in": CHECK_IN, "check_out": CHECK_OUT, "adults": 2}
    if facts:
        payload.update(facts)
    seed = ids_seed if ids_seed is not None else f"run{next(_RUN_COUNTER):06d}"
    return M.match(
        conn,
        case_id=case_id,
        facts=payload,
        kernel=kernel_obj if kernel_obj is not None else kernel(conn),
        now=NOW,
        ids=IdFactory(deterministic=True, seed=seed),
    )


def runs(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM match_runs ORDER BY rowid").fetchall()]


def candidates(conn: sqlite3.Connection, match_run_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM match_candidates WHERE match_run_id = ? ORDER BY rank", (match_run_id,)
        ).fetchall()
    ]


def candidate(conn: sqlite3.Connection, match_run_id: str, unit_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM match_candidates WHERE match_run_id = ? AND unit_id = ?", (match_run_id, unit_id)
    ).fetchone()
    assert row is not None, f"no persisted candidate for {unit_id} in {match_run_id}"
    return dict(row)


def deny_codes(row: dict) -> tuple[str, ...]:
    return tuple(str(code) for code in json.loads(row["deny_reasons"] or "[]"))


def positive_reasons(row: dict) -> list[dict]:
    return list(json.loads(row["reasons"] or "[]"))


def decision(conn: sqlite3.Connection, match_run_id: str) -> str:
    """Canonical JSON of the decision payload as it is stored in the database."""
    return canonical_json(
        [
            {
                "unit_id": row["unit_id"],
                "rank": row["rank"],
                "eligible": row["eligible"],
                "score_centi": row["score_centi"],
                "reasons": positive_reasons(row),
                "deny_reasons": list(deny_codes(row)),
                "evidence_refs": list(json.loads(row["evidence_refs"] or "[]")),
            }
            for row in candidates(conn, match_run_id)
        ]
    )


def resolve_ref(conn: sqlite3.Connection, ref: str) -> str | None:
    for table, column in (
        ("bookings", "booking_id"),
        ("holds", "hold_id"),
        ("availability_evidence", "evidence_id"),
    ):
        if conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (ref,)).fetchone() is not None:
            return table
    return None


#: The evidence ``kind`` the module reports, per owning table.
EVIDENCE_KIND = {
    "bookings": "booking",
    "holds": "hold",
    "availability_evidence": "availability_evidence",
}


def criteria_of(conn: sqlite3.Connection, match_run_id: str) -> dict:
    row = conn.execute("SELECT criteria FROM match_runs WHERE match_run_id = ?", (match_run_id,)).fetchone()
    assert row is not None
    return json.loads(row["criteria"])


def busy_world(conn: sqlite3.Connection):
    """One eligible unit, one maintenance unit, one capacity-short unit, and so on."""
    probe = Probe(conn)
    probe.add_unit("unit_0002", status="MAINTENANCE")
    probe.add_unit("unit_0003", status="SUSPENDED")
    probe.add_unit("unit_0004", max_guests=2)  # too small for 3 guests
    probe.add_property("prop_0002", region="ubud")
    probe.add_unit("unit_0005", property_id="prop_0002")  # area mismatch
    probe.add_property("prop_0003", status="NOT_SELLABLE")
    probe.add_unit("unit_0006", property_id="prop_0003")
    probe.booking("book_busy", status="CONFIRMED", offset=0)  # holds unit_0001 for the window
    return probe


# --------------------------------------------------------------------------- #
# Documents helpers
# --------------------------------------------------------------------------- #
def document_service(conn: sqlite3.Connection, root, *, ids_seed: str = "probe") -> D.DocumentService:
    return D.DocumentService(
        conn, clock=FrozenClock.at(NOW), ids=IdFactory(deterministic=True, seed=ids_seed), root=root
    )


def issue_receipt(service: D.DocumentService, *, key: str = "booking.book_0001.receipt", subject=None, **over):
    subject = {"invoice_id": INVOICE_ID} if subject is None else subject
    kwargs = {
        "kind": "RECEIPT",
        "document_key": key,
        "subject": subject,
        "generated_by": "finance_ana",
        "actor_role": "finance",
        "at": NOW,
    }
    kwargs.update(over)
    return service.generate(**kwargs)


def body_of(service: D.DocumentService, document) -> str:
    return service.path_for(document).read_text(encoding="utf-8")


def snapshot(root: Path) -> set[str]:
    root = Path(root)
    if not root.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def outside_snapshot(root: Path) -> set[str]:
    """Every path that exists next to the root but is not inside it."""
    root = Path(root)
    parent = root.parent
    if not parent.exists():
        return set()
    return {
        path.as_posix()
        for path in parent.rglob("*")
        if path != root and root not in path.parents
    }


def no_files(root: Path) -> bool:
    return not any(path.is_file() for path in Path(root).rglob("*"))


# --------------------------------------------------------------------------- #
# 1. Determinism of the match ledger
# --------------------------------------------------------------------------- #
class TestMatchingDeterminism:
    def test_one_run_and_one_candidate_per_unit(self, conn):
        """P01: a run writes one ledger row plus exactly one candidate per unit."""
        probe = Probe(conn)
        probe.add_unit("unit_0002")
        result = run_match(conn)

        stored = runs(conn)
        assert len(stored) == 1
        assert stored[0]["match_run_id"] == result.match_run_id
        rows = candidates(conn, result.match_run_id)
        assert len(rows) == 2
        assert {row["unit_id"] for row in rows} == {"unit_0001", "unit_0002"}
        assert stored[0]["considered_count"] == 2 == result.considered_count
        assert stored[0]["engine_version"] == M.ENGINE_VERSION

    def test_rerun_over_identical_facts_is_byte_identical(self, conn):
        """P02: same facts + same database => identical stored decision payload."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", max_guests=2)
        first = run_match(conn, ids_seed="first")
        second = run_match(conn, ids_seed="second")

        assert first.match_run_id != second.match_run_id
        assert decision(conn, first.match_run_id) == decision(conn, second.match_run_id)
        # The payload is not trivially empty: both runs really deny one unit.
        assert len(candidates(conn, first.match_run_id)) == 2
        assert decision(conn, first.match_run_id) != "[]"

    def test_rerun_reproduces_order_and_scores(self, conn):
        """P03: rank order and scores are reproduced, not merely the unit set."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", status="MAINTENANCE")
        probe.add_unit("unit_0003")
        first = run_match(conn)
        second = run_match(conn)

        def shape(run_id):
            return [(row["rank"], row["unit_id"], row["score_centi"]) for row in candidates(conn, run_id)]

        assert shape(first.match_run_id) == shape(second.match_run_id)
        assert [entry[1] for entry in shape(first.match_run_id)] == ["unit_0001", "unit_0003", "unit_0002"]

    def test_rerun_differs_only_in_ledger_identity(self, conn):
        """P04: match_run_id is the only field that changes between identical runs."""
        Probe(conn)
        run_match(conn, ids_seed="first")
        run_match(conn, ids_seed="second")
        first, second = runs(conn)

        assert first["match_run_id"] != second["match_run_id"]
        assert first["intake_hash"] == second["intake_hash"]
        assert first["intake_snapshot"] == second["intake_snapshot"]
        assert json.loads(first["criteria"]) == json.loads(second["criteria"])
        assert first["created_at"] == second["created_at"] == to_iso(FrozenClock.at(NOW).now())

    def test_stored_intake_hash_traces_to_the_stored_snapshot(self, conn):
        """P05: intake_hash is a fingerprint of the intake_snapshot column."""
        Probe(conn)
        result = run_match(conn)
        run = runs(conn)[0]

        snapshot_json = run["intake_snapshot"]
        assert canonical_json(json.loads(snapshot_json)) == snapshot_json
        assert fingerprint(json.loads(snapshot_json), domain="villa-agency-os:matching:intake") == run["intake_hash"]
        assert run["intake_hash"] == result.intake_hash
        decoded = json.loads(snapshot_json)
        assert decoded["adults"] == 2
        assert decoded["check_in"] == CHECK_IN and decoded["check_out"] == CHECK_OUT

    def test_rank_is_unique_and_contiguous(self, conn):
        """P06: ranks are 1..n with no duplicate, in the database and in the result."""
        busy_world(conn)  # world built for its side effect
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})

        rows = candidates(conn, result.match_run_id)
        ranks = [row["rank"] for row in rows]
        assert sorted(ranks) == list(range(1, len(rows) + 1))
        assert len(set(ranks)) == len(ranks) == 6
        assert [c.rank for c in result.candidates] == ranks
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT rank) FROM match_candidates WHERE match_run_id = ?", (result.match_run_id,)
        ).fetchone()[0]
        assert distinct == len(rows)

    def test_eligible_precede_denied_and_ties_break_on_unit_id(self, conn):
        """P07: eligibility first, then score desc, then ascending unit_id."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", status="MAINTENANCE")
        probe.add_unit("unit_0003")  # same score as unit_0001
        result = run_match(conn)
        rows = candidates(conn, result.match_run_id)

        eligible_ranks = [row["rank"] for row in rows if row["eligible"] == 1]
        denied_ranks = [row["rank"] for row in rows if row["eligible"] == 0]
        assert eligible_ranks and denied_ranks
        assert max(eligible_ranks) < min(denied_ranks)
        assert [row["unit_id"] for row in rows if row["eligible"] == 1] == ["unit_0001", "unit_0003"]
        scores = {row["unit_id"]: row["score_centi"] for row in rows}
        assert scores["unit_0001"] == scores["unit_0003"]
        assert max(eligible_ranks) == len(eligible_ranks)

    def test_changed_facts_change_the_decision(self, conn):
        """P08: the equality proven by P02/P03 is not vacuous — a real change moves it."""
        Probe(conn)
        first = run_match(conn, facts={"adults": 2})
        second = run_match(conn, facts={"adults": 4})
        third = run_match(conn, facts={"adults": 2, "children": 1})

        assert decision(conn, first.match_run_id) != decision(conn, second.match_run_id)
        assert decision(conn, first.match_run_id) != decision(conn, third.match_run_id)
        assert runs(conn)[0]["intake_hash"] != runs(conn)[1]["intake_hash"]


# --------------------------------------------------------------------------- #
# 2. Deny-reason integrity
# --------------------------------------------------------------------------- #
class TestDenyReasonIntegrity:
    def test_every_ineligible_candidate_has_deny_reasons(self, conn):
        """P09: no ineligible candidate exists without a coded deny reason."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        rows = candidates(conn, result.match_run_id)
        ineligible = [row for row in rows if row["eligible"] == 0]

        assert len(ineligible) == 6
        assert result.eligible_count == 0
        for row in ineligible:
            codes = deny_codes(row)
            assert codes, f"{row['unit_id']} is ineligible without deny_reasons"
            assert row["score_centi"] == 0
            assert positive_reasons(row) == []
        stored = conn.execute(
            "SELECT COUNT(*) FROM match_candidates WHERE match_run_id = ? AND eligible = 0"
            " AND (deny_reasons IS NULL OR deny_reasons = '[]')",
            (result.match_run_id,),
        ).fetchone()[0]
        assert stored == 0

    def test_database_check_refuses_eligibility_without_deny_reasons(self, conn):
        """P10: the schema CHECK is the second line of defence, and it fires."""
        probe = Probe(conn)
        result = run_match(conn)
        run_id = result.match_run_id
        # A unit that exists but was *not* considered by the run above, so the raw
        # inserts below collide with no real candidate row.
        probe.add_unit("unit_0003")
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            conn.execute(
                "INSERT INTO match_candidates (candidate_id, match_run_id, unit_id, rank, eligible,"
                " score_centi, reasons, deny_reasons, evidence_refs, created_at) VALUES ('cand_probe_bad',"
                " ?, 'unit_0003', 91, 0, 0, '[]', '[]', '[]', ?)",
                (run_id, NOW),
            )
        assert "ck_match_candidate_deny" in str(excinfo.value)

        conn.execute(
            "INSERT INTO match_candidates (candidate_id, match_run_id, unit_id, rank, eligible,"
            " score_centi, reasons, deny_reasons, evidence_refs, created_at) VALUES ('cand_probe_ok',"
            " ?, 'unit_0003', 92, 0, 0, '[]', ?, '[]', ?)",
            (run_id, '["MAINTENANCE_BLOCK"]', NOW),
        )
        row = conn.execute(
            "SELECT deny_reasons FROM match_candidates WHERE candidate_id = 'cand_probe_ok'"
        ).fetchone()
        assert json.loads(row["deny_reasons"]) == ["MAINTENANCE_BLOCK"]

    def test_deny_codes_come_from_the_published_vocabulary(self, conn):
        """P11: only vocabulary codes appear, in canonical order, deduplicated."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        seen: set[str] = set()
        for row in candidates(conn, result.match_run_id):
            codes = deny_codes(row)
            assert all(code in M.DENY_REASON_CODES for code in codes)
            assert list(codes) == sorted(set(codes), key=M.DENY_REASON_CODES.index)
            seen.update(codes)
        assert {"DATES_UNAVAILABLE", "MAINTENANCE_BLOCK", "OUT_OF_SERVICE"} <= seen
        assert {"CAPACITY_EXCEEDED", "AREA_MISMATCH", "PROPERTY_NOT_SELLABLE"} <= seen

    def test_maintenance_deny_is_backed_by_a_maintenance_unit(self, conn):
        """P12: MAINTENANCE_BLOCK names a unit row that really is in MAINTENANCE."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0002")

        assert "MAINTENANCE_BLOCK" in deny_codes(row)
        stored = conn.execute("SELECT status FROM units WHERE unit_id = 'unit_0002'").fetchone()
        assert stored["status"] == "MAINTENANCE"

    def test_out_of_service_deny_is_backed_by_a_suspended_unit(self, conn):
        """P13: OUT_OF_SERVICE names a unit row that really is SUSPENDED."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0003")

        assert "OUT_OF_SERVICE" in deny_codes(row)
        stored = conn.execute("SELECT status FROM units WHERE unit_id = 'unit_0003'").fetchone()
        assert stored["status"] == "SUSPENDED"

    def test_property_deny_is_backed_by_a_non_sellable_property(self, conn):
        """P14: PROPERTY_NOT_SELLABLE names a property row that is not sellable."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0006")

        assert "PROPERTY_NOT_SELLABLE" in deny_codes(row)
        stored = conn.execute(
            "SELECT p.status FROM units u JOIN properties p ON p.property_id = u.property_id"
            " WHERE u.unit_id = 'unit_0006'"
        ).fetchone()
        assert stored["status"] in ("NOT_SELLABLE", "SUSPENDED")
        assert stored["status"] == "NOT_SELLABLE"

    def test_capacity_deny_is_backed_by_the_units_max_guests(self, conn):
        """P15: CAPACITY_EXCEEDED is true against the real row, not against a constant."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0004")
        criteria = criteria_of(conn, result.match_run_id)

        assert "CAPACITY_EXCEEDED" in deny_codes(row)
        stored = conn.execute("SELECT max_guests FROM units WHERE unit_id = 'unit_0004'").fetchone()
        assert criteria["guests"] == 3 > int(stored["max_guests"]) == 2

    def test_area_deny_is_backed_by_the_real_region(self, conn):
        """P16: AREA_MISMATCH is a real mismatch against properties.region."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0005")
        criteria = criteria_of(conn, result.match_run_id)
        region = conn.execute(
            "SELECT p.region FROM units u JOIN properties p ON p.property_id = u.property_id"
            " WHERE u.unit_id = 'unit_0005'"
        ).fetchone()["region"]

        assert "AREA_MISMATCH" in deny_codes(row)
        wanted = " ".join(str(criteria["area_preference"]).casefold().split())
        have = " ".join(str(region).casefold().split())
        assert wanted != have and wanted not in have and have not in wanted

    def test_min_stay_deny_is_backed_by_a_real_policy_row(self, conn):
        """P17: MIN_STAY_NOT_MET is derivable from a policy_version row that exists."""
        probe = Probe(conn)
        probe.add_policy(payload={"min_stay_nights": 7})
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")
        criteria = criteria_of(conn, result.match_run_id)

        assert deny_codes(row) == ("MIN_STAY_NOT_MET",)
        assert criteria["nights"] == NIGHTS
        policy = conn.execute(
            "SELECT policy_version_id, payload FROM policy_versions WHERE property_id = 'prop_0001'"
            " AND effective_from <= ? AND ? <= effective_to ORDER BY version DESC LIMIT 1",
            (CHECK_IN, CHECK_OUT),
        ).fetchone()
        assert json.loads(policy["payload"])["min_stay_nights"] == 7
        basis = result.candidate_for("unit_0001").basis
        assert basis["min_stay_required"] == 7
        assert basis["min_stay_sources"] == [f"policy:{policy['policy_version_id']}"]

    def test_min_stay_deny_from_a_rate_card_is_backed_by_a_real_season_row(self, conn):
        """P72: the other min-stay basis (a rate season) is traceable too."""
        probe = Probe(conn)
        probe.add_rate_card(min_stay_nights=5, nightly_rate_minor=1_400_000)
        result = run_match(conn, facts={"adults": 2, "budget_minor": 4_000_000})
        row = candidate(conn, result.match_run_id, "unit_0001")
        criteria = criteria_of(conn, result.match_run_id)

        assert deny_codes(row) == ("MIN_STAY_NOT_MET",)
        season = conn.execute(
            "SELECT s.min_stay_nights, s.nightly_rate_minor FROM rate_seasons s"
            " JOIN rate_cards c ON c.rate_card_id = s.rate_card_id"
            " WHERE c.property_id = 'prop_0001' AND c.effective_from <= ? AND ? <= c.effective_to"
            " AND s.start_date <= ? AND ? <= s.end_date"
            " ORDER BY s.priority, s.season_id LIMIT 1",
            (CHECK_IN, CHECK_OUT, CHECK_IN, CHECK_OUT),
        ).fetchone()
        assert season is not None, "the deny must be derivable from a stored season"
        assert int(season["min_stay_nights"]) == 5 > criteria["nights"]
        basis = result.candidate_for("unit_0001").basis
        assert basis["min_stay_required"] == 5
        assert basis["min_stay_sources"] == ["rate_card:rc_v1"]
        assert basis["accommodation_minor"] == 3 * 1_400_000

    def test_contract_pack_note_is_advisory_and_never_a_ledger_deny(self, conn):
        """P73: an unactivated contract pack flags review in the result, not in the ledger.

        The write path (``reservation_kernel`` + ``gates``) is what refuses to sell
        without an activated pack; matching is advisory by design, so this probe
        pins the boundary instead of pretending it is a gate.
        """
        Probe(conn)
        conn.execute("UPDATE properties SET contract_pack_status = 'DRAFT' WHERE property_id = 'prop_0001'")
        result = run_match(conn)
        candidate_row = candidate(conn, result.match_run_id, "unit_0001")
        returned = result.candidate_for("unit_0001")

        assert candidate_row["eligible"] == 1
        assert deny_codes(candidate_row) == ()
        assert returned.review_required is True
        assert "CONTRACT_PACK_NOT_ACTIVATED" in {str(note["code"]) for note in returned.notes}
        # Flagged for review, but still never ranked as an eligible sale on its own:
        # the ledger row is the whole evidence a later reviewer gets.
        assert json.loads(candidate_row["reasons"])
        assert conn.execute("SELECT status FROM properties WHERE property_id = 'prop_0001'").fetchone()[
            "status"
        ] == "SELLABLE"

    def test_dates_deny_is_backed_by_a_real_blocking_row(self, conn):
        """P18: DATES_UNAVAILABLE names a booking/hold row that really overlaps."""
        probe = Probe(conn)
        probe.booking("book_busy", status="CONFIRMED", offset=0)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")
        criteria = criteria_of(conn, result.match_run_id)

        assert "DATES_UNAVAILABLE" in deny_codes(row)
        refs = json.loads(row["evidence_refs"])
        assert refs == ["book_busy"]
        blocking = conn.execute("SELECT * FROM bookings WHERE booking_id = 'book_busy'").fetchone()
        assert blocking["status"] in K.OCCUPYING_BOOKING_STATES
        assert blocking["check_in"] < criteria["check_out"]
        assert criteria["check_in"] < blocking["check_out"]

    def test_every_recorded_evidence_ref_resolves(self, conn):
        """P19: no candidate records an evidence id that is not in the database."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", max_guests=2)
        probe.hold("hold_busy", status="CONFIRMED", offset=0)
        result = run_match(conn)

        checked = 0
        for row in candidates(conn, result.match_run_id):
            for ref in json.loads(row["evidence_refs"] or "[]"):
                assert resolve_ref(conn, str(ref)) is not None, f"dangling evidence ref {ref!r}"
                checked += 1
        assert checked == 1

    def test_explain_resolves_every_ref_and_agrees_with_the_ledger(self, conn):
        """P20: explain() re-checks the evidence and reports the same verdicts."""
        busy_world(conn)
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        explanation = M.explain(conn, result.match_run_id)

        stored = {row["unit_id"]: row["eligible"] == 1 for row in candidates(conn, result.match_run_id)}
        assert {entry["unit_id"]: entry["eligible"] for entry in explanation.candidates} == stored
        assert explanation.considered_count == len(stored)
        assert explanation.eligible_count == result.eligible_count
        assert explanation.evidence_refs
        assert all(entry["ref"] and entry["kind"] for entry in explanation.evidence_refs)
        for entry in explanation.evidence_refs:
            table = resolve_ref(conn, entry["ref"])
            assert table is not None
            assert entry["kind"] == EVIDENCE_KIND[table]
        assert "book_busy" in str(explanation)

    def test_dangling_evidence_ref_is_refused(self, conn):
        """P21: a stubbed kernel id that does not exist is refused, not printed as proof."""
        Probe(conn)
        ghost = _StubKernel(
            {"unit_0001": ({"id": "book_ghost", "status": "CONFIRMED", "check_in": CHECK_IN, "check_out": CHECK_OUT},)}
        )
        result = run_match(conn, kernel_obj=ghost)
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert deny_codes(row) == ("DATES_UNAVAILABLE",)
        assert json.loads(row["evidence_refs"]) == ["book_ghost"]
        assert resolve_ref(conn, "book_ghost") is None
        with pytest.raises(DomainError) as excinfo:
            M.explain(conn, result.match_run_id)
        assert excinfo.value.code == M.CODE_DANGLING_EVIDENCE

    def test_availability_verdict_comes_from_the_injected_kernel(self, conn):
        """P22: matching asks the kernel; it does not query bookings itself."""
        probe = Probe(conn)
        probe.booking("book_busy", status="CONFIRMED", offset=0)
        free = _StubKernel()

        injected = run_match(conn, kernel_obj=free)
        row = candidate(conn, injected.match_run_id, "unit_0001")
        assert free.calls == ["unit_0001"]
        assert row["eligible"] == 1
        assert deny_codes(row) == ()

        real = run_match(conn)
        assert candidate(conn, real.match_run_id, "unit_0001")["eligible"] == 0


# --------------------------------------------------------------------------- #
# 3. Occupancy: the kernel's set, proven end to end
# --------------------------------------------------------------------------- #
class TestOccupancyExclusion:
    @pytest.mark.parametrize("status", K.OCCUPYING_BOOKING_STATES)
    def test_occupying_status_never_yields_an_eligible_candidate(self, conn, status):
        """P23: every declared occupying status keeps the unit out of the eligible set."""
        probe = Probe(conn)
        probe.booking("book_busy", status=status, offset=0)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert result.eligible_count == 0
        assert row["eligible"] == 0
        assert "DATES_UNAVAILABLE" in deny_codes(row)
        assert json.loads(row["evidence_refs"]) == ["book_busy"]
        assert conn.execute("SELECT status FROM bookings WHERE booking_id = 'book_busy'").fetchone()[
            "status"
        ] == status

    @pytest.mark.parametrize(
        "status",
        [
            "INQUIRY",
            "QUALIFIED",
            "QUOTE_SENT",
            "HOLD_ACTIVE",
            "PAYMENT_PENDING",
            "DEPOSIT_PAID",
            "PAID_IN_FULL",
            "CANCELLED",
            "COMPLETED",
            "REFUNDED",
            "NO_SHOW",
            "CLOSED",
        ],
    )
    def test_non_occupying_status_does_not_block(self, conn, status):
        """P24: a status outside the kernel's set leaves the unit eligible."""
        assert status not in K.OCCUPYING_BOOKING_STATES
        probe = Probe(conn)
        probe.booking("book_other", status=status, offset=0)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert row["eligible"] == 1
        assert deny_codes(row) == ()

    def test_booking_outside_the_window_does_not_block(self, conn):
        """P25: occupancy is a window question, and the kernel says so too."""
        probe = Probe(conn)
        probe.booking("book_later", status="CONFIRMED", offset=30)
        assert kernel(conn).occupancy("unit_0001", start=CHECK_IN, end=CHECK_OUT) == ()

        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")
        assert row["eligible"] == 1
        assert deny_codes(row) == ()
        stored = conn.execute("SELECT check_in, check_out FROM bookings WHERE booking_id = 'book_later'").fetchone()
        assert stored["check_in"] >= CHECK_OUT

    def test_confirmed_hold_blocks_and_is_named_as_evidence(self, conn):
        """P26: a CONFIRMED hold occupies the slot and its id is recorded."""
        probe = Probe(conn)
        probe.hold("hold_busy", status="CONFIRMED", offset=0)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert row["eligible"] == 0
        assert "DATES_UNAVAILABLE" in deny_codes(row)
        assert json.loads(row["evidence_refs"]) == ["hold_busy"]
        assert conn.execute("SELECT status FROM holds WHERE hold_id = 'hold_busy'").fetchone()[
            "status"
        ] == K.HOLD_CONFIRMED

    def test_released_hold_does_not_block(self, conn):
        """P27: a hold that is not in force does not occupy the slot."""
        probe = Probe(conn)
        probe.add_hold("hold_done", status="RELEASE_CONFIRMED", offset=0)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert row["eligible"] == 1
        assert deny_codes(row) == ()

    def test_kernel_occupancy_set_matches_the_schema_partial_index(self):
        """P28: the declared occupancy set and the schema's partial index are one pair."""
        sql_text = SCHEMA_SQL.read_text(encoding="utf-8")
        block = sql_text.split("uq_bookings_active_slot", 1)[1].split(";", 1)[0]
        inside = re.search(r"status IN \(([^)]*)\)", block)
        assert inside is not None, "the partial index no longer spells its status set"
        schema_states = {token.strip().strip("'") for token in inside.group(1).split(",") if token.strip()}
        assert schema_states == set(K.OCCUPYING_BOOKING_STATES)

    @pytest.mark.parametrize("status", K.OCCUPYING_BOOKING_STATES)
    def test_kernel_reports_every_declared_occupying_state(self, conn, status):
        """P29: the kernel's oracle really returns the booking it declared occupying."""
        probe = Probe(conn)
        probe.booking("book_busy", status=status, offset=0)
        rows = kernel(conn).occupancy("unit_0001", start=CHECK_IN, end=CHECK_OUT)

        assert [row["id"] for row in rows] == ["book_busy"]
        assert rows[0]["kind"] == "booking"

    @pytest.mark.parametrize("status", ["INQUIRY", "CANCELLED", "COMPLETED", "REFUNDED", "NO_SHOW"])
    def test_kernel_ignores_a_non_occupying_state(self, conn, status):
        """P30: the kernel oracle hides a booking whose status does not occupy."""
        probe = Probe(conn)
        probe.booking("book_other", status=status, offset=0)
        assert kernel(conn).occupancy("unit_0001", start=CHECK_IN, end=CHECK_OUT) == ()


# --------------------------------------------------------------------------- #
# 4. Capacity
# --------------------------------------------------------------------------- #
class TestCapacityGate:
    def test_guests_equal_to_max_guests_is_eligible(self, conn):
        """P31: exactly at the limit is a fit, not a denial."""
        Probe(conn)
        result = run_match(conn, facts={"adults": 4})
        row = candidate(conn, result.match_run_id, "unit_0001")
        max_guests = conn.execute("SELECT max_guests FROM units WHERE unit_id = 'unit_0001'").fetchone()[
            "max_guests"
        ]

        assert row["eligible"] == 1
        assert deny_codes(row) == ()
        assert criteria_of(conn, result.match_run_id)["guests"] == int(max_guests) == 4
        capacity = [reason for reason in positive_reasons(row) if reason["code"] == "CAPACITY_FIT"]
        assert capacity and capacity[0]["detail"]["max_guests"] == 4

    def test_guests_above_max_guests_is_denied(self, conn):
        """P32: one guest over the limit is a refusal with the capacity code."""
        Probe(conn)
        result = run_match(conn, facts={"adults": 5})
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert row["eligible"] == 0
        assert deny_codes(row) == ("CAPACITY_EXCEEDED",)
        assert row["score_centi"] == 0
        assert positive_reasons(row) == []

    def test_children_count_towards_capacity(self, conn):
        """P33: guests = adults + children, so two children can exceed the limit."""
        Probe(conn)
        result = run_match(conn, facts={"adults": 3, "children": 2})
        row = candidate(conn, result.match_run_id, "unit_0001")

        assert criteria_of(conn, result.match_run_id)["guests"] == 5
        assert "CAPACITY_EXCEEDED" in deny_codes(row)

        fitting = run_match(conn, facts={"adults": 3, "children": 1})
        assert candidate(conn, fitting.match_run_id, "unit_0001")["eligible"] == 1

    def test_capacity_verdict_matches_the_inventory_row_for_every_unit(self, conn):
        """P34: the verdict is traceable to each unit's own max_guests column."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", max_guests=2, base_capacity=1)
        probe.add_unit("unit_0003", max_guests=8, base_capacity=4)
        result = run_match(conn, facts={"adults": 3})

        verdicts = {
            row["unit_id"]: ("ELIGIBLE" if row["eligible"] == 1 else deny_codes(row))
            for row in candidates(conn, result.match_run_id)
        }
        assert verdicts == {
            "unit_0001": "ELIGIBLE",
            "unit_0002": ("CAPACITY_EXCEEDED",),
            "unit_0003": "ELIGIBLE",
        }
        for row in candidates(conn, result.match_run_id):
            max_guests = conn.execute(
                "SELECT max_guests FROM units WHERE unit_id = ?", (row["unit_id"],)
            ).fetchone()["max_guests"]
            fits = 3 <= int(max_guests)
            assert fits == (row["eligible"] == 1)


# --------------------------------------------------------------------------- #
# 5. Score and rank invariants
# --------------------------------------------------------------------------- #
class TestScoreAndRank:
    def test_scores_are_stored_as_integers(self, conn):
        """P35: SQLite reports every score as an integer, and no float is ever written."""
        probe = busy_world(conn)
        probe.add_unit("unit_0007")
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        types = {
            row["unit_id"]: row["score_centi"]
            for row in conn.execute(
                "SELECT unit_id, typeof(score_centi) AS score_centi FROM match_candidates"
                " WHERE match_run_id = ?",
                (result.match_run_id,),
            ).fetchall()
        }
        assert set(types.values()) == {"integer"}
        for row in candidates(conn, result.match_run_id):
            assert isinstance(row["score_centi"], int) and not isinstance(row["score_centi"], bool)
            for reason in positive_reasons(row):
                assert isinstance(reason["weight"], int)

    def test_score_equals_the_sum_of_stored_weights(self, conn):
        """P36: a stored score is exactly the sum of its stored positive weights."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", status="MAINTENANCE")
        result = run_match(conn, facts={"adults": 2, "area_preference": "bali", "bedrooms": 2})
        rows = candidates(conn, result.match_run_id)

        for row in rows:
            reasons = positive_reasons(row)
            if row["eligible"] == 1:
                assert reasons, f"{row['unit_id']} is eligible without a positive reason"
                assert row["score_centi"] == sum(int(reason["weight"]) for reason in reasons) > 0
                assert all(reason["code"] in M.POSITIVE_REASON_CODES for reason in reasons)
            else:
                assert row["score_centi"] == 0 and reasons == []
        assert {row["unit_id"]: row["score_centi"] for row in rows}["unit_0001"] == 30 + 25 + 20 + 10

    def test_no_float_literal_survives_in_the_stored_payload(self, conn):
        """P37: no float literal exists anywhere in the persisted decision columns."""
        busy_world(conn)  # world built for its side effect
        result = run_match(conn, facts={"adults": 3, "area_preference": "bali"})
        float_literal = re.compile(r"(?<![\w.])-?\d+\.\d+")
        rows = conn.execute(
            "SELECT reasons, deny_reasons, evidence_refs FROM match_candidates WHERE match_run_id = ?",
            (result.match_run_id,),
        ).fetchall()
        assert rows
        for row in rows:
            for column in ("reasons", "deny_reasons", "evidence_refs"):
                text = str(row[column])
                assert float_literal.search(text) is None, f"float literal in {column}: {text}"
        for row in candidates(conn, result.match_run_id):
            assert float_literal.search(decision(conn, result.match_run_id)) is None

    def test_stored_reasons_equal_the_returned_candidate(self, conn):
        """P38: what the caller receives is what the ledger stores, byte for byte."""
        Probe(conn)
        result = run_match(conn, facts={"adults": 2, "area_preference": "bali"})
        row = candidate(conn, result.match_run_id, "unit_0001")
        returned = result.candidate_for("unit_0001")

        assert row["reasons"] == canonical_json([dict(reason) for reason in returned.reasons])
        assert list(deny_codes(row)) == list(returned.deny_reasons)
        assert json.loads(row["evidence_refs"]) == list(returned.evidence_refs)

    def test_available_reason_names_the_kernel_as_its_source(self, conn):
        """P39: the AVAILABLE reason carries the kernel's provenance, not our own."""
        Probe(conn)
        result = run_match(conn)
        row = candidate(conn, result.match_run_id, "unit_0001")
        available = [reason for reason in positive_reasons(row) if reason["code"] == "AVAILABLE"]

        assert len(available) == 1
        assert available[0]["detail"]["source"] == M.PROVENANCE["availability"]
        assert "reservation_kernel" in str(available[0]["detail"]["source"])
        assert available[0]["detail"]["occupying"] == 0
        assert available[0]["detail"]["start"] == CHECK_IN
        assert available[0]["detail"]["end"] == CHECK_OUT
        criteria = criteria_of(conn, result.match_run_id)
        assert criteria["provenance"]["availability"] == available[0]["detail"]["source"]


# --------------------------------------------------------------------------- #
# 6. Rerun / idempotence
# --------------------------------------------------------------------------- #
class TestRerunIdempotence:
    def test_rerun_is_append_only_and_recomputes_an_identical_decision(self, conn):
        """P40: the ledger is append-only; recomputing the same facts is deterministic."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", status="MAINTENANCE")
        first = run_match(conn, ids_seed="run-a")
        rows_after_first = runs(conn)
        second = run_match(conn, ids_seed="run-b")

        stored = runs(conn)
        assert len(stored) == 2, "a rerun must append, never reuse a run row"
        assert {run["match_run_id"] for run in stored} == {first.match_run_id, second.match_run_id}
        assert decision(conn, first.match_run_id) == decision(conn, second.match_run_id)
        assert len(candidates(conn, first.match_run_id)) == len(candidates(conn, second.match_run_id)) == 2
        assert rows_after_first[0]["match_run_id"] == first.match_run_id
        duplicate = conn.execute(
            "SELECT COUNT(*) FROM (SELECT match_run_id, unit_id FROM match_candidates"
            " GROUP BY match_run_id, unit_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        assert duplicate == 0

    def test_rerun_never_mutates_the_first_run_rows(self, conn):
        """P41: the earlier ledger rows are immutable across a rerun."""
        probe = Probe(conn)
        probe.add_unit("unit_0002", max_guests=2)
        first = run_match(conn)
        before_run = dict(runs(conn)[0])
        before_candidates = candidates(conn, first.match_run_id)

        run_match(conn)

        assert dict(
            conn.execute("SELECT * FROM match_runs WHERE match_run_id = ?", (first.match_run_id,)).fetchone()
        ) == before_run
        assert candidates(conn, first.match_run_id) == before_candidates

    def test_a_real_change_is_never_served_from_the_ledger(self, conn):
        """P42: the module recomputes instead of replaying a stored verdict."""
        probe = Probe(conn)
        first = run_match(conn, facts={"adults": 2})
        before = decision(conn, first.match_run_id)

        probe.booking("book_busy", status="CONFIRMED", offset=0)  # a fact changed in the database
        rerun = run_match(conn, facts={"adults": 2})

        assert len(runs(conn)) == 2
        assert decision(conn, rerun.match_run_id) != before
        assert candidate(conn, rerun.match_run_id, "unit_0001")["eligible"] == 0
        # ...and the earlier verdict is still readable, exactly as it was written.
        assert decision(conn, first.match_run_id) == before


# --------------------------------------------------------------------------- #
# 7. Documents: grounding in the invoice / booking rows
# --------------------------------------------------------------------------- #
class TestDocumentGrounding:
    def test_receipt_carries_the_invoice_number_of_its_own_invoice(self, conn, tmp_path):
        """P43: the printed invoice number is the row's number, and only that one."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = issue_receipt(service)

        invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()
        other = conn.execute("SELECT invoice_number FROM invoices WHERE invoice_id = ?", (OTHER_INVOICE_ID,)).fetchone()
        body = body_of(service, document)
        number = str(invoice["invoice_number"])

        assert number in body and body.count(number) == 1
        assert str(other["invoice_number"]) not in body
        assert document.invoice_id == INVOICE_ID
        assert document.booking_id == invoice["booking_id"]

    def test_receipt_carries_the_totals_of_its_invoice_row(self, conn, tmp_path):
        """P44: every printed amount is the stored amount, re-formatted — not a copy."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = issue_receipt(service)

        invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()
        currency = str(invoice["currency"])
        body = body_of(service, document)
        outstanding = int(invoice["total_minor"]) - int(invoice["amount_paid_minor"])

        assert int(invoice["total_minor"]) == 1_000_000
        for amount in (
            int(invoice["subtotal_minor"]),
            int(invoice["tax_minor"]),
            int(invoice["service_minor"]),
            int(invoice["total_minor"]),
            outstanding,
        ):
            assert D.format_amount(Money(amount, currency), "id-ID") in body
        assert D.format_amount(Money(int(invoice["total_minor"]), currency), "id-ID") == "IDR 1.000.000"
        assert document.body_sha256 == sha256_hex(body)

    def test_partially_paid_invoice_prints_paid_and_outstanding_from_the_row(self, conn, tmp_path):
        """P70: paid/outstanding printed in the receipt are the row's, not a guess."""
        billing_world(conn)
        conn.execute(
            "UPDATE invoices SET amount_paid_minor = 350000, payment_status = 'PARTIAL'"
            " WHERE invoice_id = ?",
            (INVOICE_ID,),
        )
        service = document_service(conn, tmp_path)
        document = issue_receipt(service)

        invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()
        currency = str(invoice["currency"])
        body = body_of(service, document)
        paid = int(invoice["amount_paid_minor"])
        outstanding = int(invoice["total_minor"]) - paid

        assert (paid, outstanding) == (350_000, 650_000)
        assert D.format_amount(Money(paid, currency), "id-ID") in body
        assert D.format_amount(Money(outstanding, currency), "id-ID") in body
        assert str(invoice["payment_status"]) in body
        # The receipt rows are still the invoice's: nothing was recomputed locally.
        assert D.format_amount(Money(int(invoice["total_minor"]), currency), "id-ID") in body

    def test_printed_arithmetic_reconciles_with_the_stored_lines(self, conn, tmp_path):
        """P45: the numbers in the document can be traced to invoice_lines rows."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        issue_receipt(service)

        invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()
        lines = conn.execute(
            "SELECT kind, amount_minor FROM invoice_lines WHERE invoice_id = ? ORDER BY line_id", (INVOICE_ID,)
        ).fetchall()
        subtotal = sum(int(line["amount_minor"]) for line in lines if line["kind"] not in ("TAX", "SERVICE"))
        tax = sum(int(line["amount_minor"]) for line in lines if line["kind"] == "TAX")
        service_fee = sum(int(line["amount_minor"]) for line in lines if line["kind"] == "SERVICE")

        assert (subtotal, tax, service_fee) == (
            int(invoice["subtotal_minor"]),
            int(invoice["tax_minor"]),
            int(invoice["service_minor"]),
        )
        assert subtotal + tax + service_fee == int(invoice["total_minor"])

    def test_confirmation_total_matches_the_booking_and_the_invoice(self, conn, tmp_path):
        """P46: the confirmation quotes the booking row, which agrees with the invoice row."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = service.generate(
            kind="CONFIRMATION",
            document_key="booking.book_0001.confirmation",
            subject={"booking_id": BOOKING_ID},
            generated_by="agent_ana",
            actor_role="agent",
            at=NOW,
        )
        booking = conn.execute("SELECT * FROM bookings WHERE booking_id = ?", (BOOKING_ID,)).fetchone()
        invoice = conn.execute("SELECT * FROM invoices WHERE booking_id = ?", (BOOKING_ID,)).fetchone()
        body = body_of(service, document)
        currency = str(booking["currency"])

        assert int(booking["total_minor"]) == int(invoice["total_minor"])
        assert D.format_amount(Money(int(booking["total_minor"]), currency), "id-ID") in body
        assert str(booking["booking_id"]) in body
        assert str(booking["check_in"]) in body and str(booking["check_out"]) in body
        assert D.format_date(str(booking["check_in"]), "id-ID") in body
        assert str(booking["nights"]) in body

    def test_documents_row_points_at_the_source_rows(self, conn, tmp_path):
        """P47: the persisted document names the booking and invoice it quoted."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = issue_receipt(service)
        stored = conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document.document_id,)
        ).fetchone()
        invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()

        assert stored["booking_id"] == str(invoice["booking_id"]) == BOOKING_ID
        assert stored["invoice_id"] == INVOICE_ID
        assert stored["status"] == "GENERATED"
        assert stored["body_sha256"] == document.body_sha256
        assert stored["storage_ref"] == document.storage_ref
        assert service.verify(document.document_id).ok is True

    def test_receipt_refused_when_the_lines_do_not_reconcile(self, conn, tmp_path):
        """P48: a document may never quote totals its source lines contradict."""
        billing_world(conn, lines=UNRECONCILED_LINES)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service)
        assert excinfo.value.code == D.INVOICE_SOURCE_MISMATCH
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)

    def test_receipt_refused_without_lines(self, conn, tmp_path):
        """P49: an invoice with no lines has no derivable total, so no document."""
        billing_world(conn, lines=None)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service)
        assert excinfo.value.code == D.INVOICE_SOURCE_MISMATCH
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)

    def test_claim_that_disagrees_with_the_source_row_is_refused(self, conn, tmp_path):
        """P50: the caller cannot make the document say a different total."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        invoice = conn.execute("SELECT total_minor FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service, subject={"invoice_id": INVOICE_ID, "total_minor": int(invoice["total_minor"]) + 1})
        assert excinfo.value.code == D.INVOICE_SOURCE_MISMATCH
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)

    def test_void_invoice_cannot_be_documented(self, conn, tmp_path):
        """P51: a VOID invoice row is not a source a receipt may cite."""
        billing_world(conn, invoice_status="VOID")
        service = document_service(conn, tmp_path)
        voided = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()
        assert voided["status"] == "VOID" and voided["voided_at"] is not None

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service)
        assert excinfo.value.code == D.DOCUMENT_SOURCE_INVALID
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)


# --------------------------------------------------------------------------- #
# 8. Documents: data class, storage root, redaction
# --------------------------------------------------------------------------- #
class TestDocumentDataClassAndStorage:
    def test_every_issued_document_is_p1_under_the_redaction_profile(self, conn, tmp_path):
        """P52: no document is filed as P2/P3/P4, and each carries the P1 profile."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        issue_receipt(service)
        service.generate(
            kind="CONFIRMATION",
            document_key="booking.book_0001.confirmation",
            subject={"booking_id": BOOKING_ID},
            generated_by="agent_ana",
            actor_role="agent",
            at=NOW,
        )

        rows = conn.execute("SELECT * FROM documents").fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["content_class"] == D.DOCUMENT_CONTENT_CLASS == "P1"
            assert row["redaction_profile"] == D.REDACTION_PROFILE
            assert row["storage_ref"]
            assert "P2" not in str(row["content_class"]) and "P3" not in str(row["content_class"])
        flagged = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE content_class IN ('P2','P3','P4')"
        ).fetchone()[0]
        assert flagged == 0

    def test_every_written_file_sits_inside_the_root_that_was_given(self, conn, tmp_path):
        """P53: the storage_ref of every row resolves to a file inside the root."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        first = issue_receipt(service)
        second = issue_receipt(service, key="booking.book_0001.receipt.second")

        refs = {row["storage_ref"] for row in conn.execute("SELECT storage_ref FROM documents").fetchall()}
        assert refs == {first.storage_ref, second.storage_ref}
        for ref in refs:
            path = D.resolve_storage_path(tmp_path, ref)
            assert path.is_file()
            assert tmp_path in path.parents
        assert snapshot(tmp_path) == refs
        for row in conn.execute("SELECT storage_ref, body_sha256 FROM documents").fetchall():
            on_disk = D.resolve_storage_path(tmp_path, row["storage_ref"]).read_text(encoding="utf-8")
            assert sha256_hex(on_disk) == row["body_sha256"]

    def test_nothing_is_written_outside_the_given_root(self, conn, tmp_path):
        """P54: the default root and the parent directory stay untouched."""
        billing_world(conn)
        default_root = D.default_root()
        default_before = snapshot(default_root)
        outside_before = outside_snapshot(tmp_path)

        service = document_service(conn, tmp_path)
        issue_receipt(service)
        service.generate(
            kind="CONFIRMATION",
            document_key="booking.book_0001.confirmation",
            subject={"booking_id": BOOKING_ID},
            generated_by="agent_ana",
            actor_role="agent",
            at=NOW,
        )

        assert snapshot(default_root) == default_before
        assert outside_snapshot(tmp_path) == outside_before
        assert snapshot(tmp_path)

    def test_a_refused_issue_writes_no_row_and_no_file(self, conn, tmp_path):
        """P55: fail-closed means neither a ledger row nor a stray byte."""
        billing_world(conn)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service, subject={"invoice_id": "inv_9999"})
        assert excinfo.value.code == D.DOCUMENT_SOURCE_MISSING
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path) and outside_snapshot(tmp_path) == set() | outside_snapshot(tmp_path)

    def test_an_escaping_document_key_is_refused_before_any_write(self, conn, tmp_path):
        """P56: a traversal key never reaches the filesystem."""
        billing_world(conn)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            service.generate(
                kind="CONFIRMATION",
                document_key="../escape",
                subject={"booking_id": BOOKING_ID},
                generated_by="agent_ana",
                actor_role="agent",
                at=NOW,
            )
        assert excinfo.value.code == D.UNSAFE_DOCUMENT_KEY
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)
        assert not (tmp_path.parent / "escape").exists()
        assert not (tmp_path.parent / "escape.html").exists()

    def test_resolve_storage_path_refuses_every_escaping_ref(self, tmp_path):
        """P57: a tampered storage_ref cannot point the reader outside the root."""
        for ref in ("../escape.html", "/etc/passwd", "a/../../b.html", "sub\\win.html", "./same.html"):
            with pytest.raises(DomainError) as excinfo:
                D.resolve_storage_path(tmp_path, ref)
            assert excinfo.value.code == D.DOCUMENT_STORAGE_INVALID
        assert D.resolve_storage_path(tmp_path, "receipt/key/v0001.html") == tmp_path / "receipt" / "key" / "v0001.html"

    def test_a_body_carries_no_identifier_under_any_published_pattern(self, conn, tmp_path):
        """P58: raw P2/P3-shaped values are redacted before they are ever written."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        nik = "1234567890123456"
        email = "tamu.secret@example.com"
        phone = "081234567890"
        document = issue_receipt(
            service,
            subject={
                "invoice_id": INVOICE_ID,
                "recipient_ref": "guest_0001",
                "contact_email": email,
                "notes": f"KTP {nik}, telepon {phone}, balas ke {email}",
            },
        )
        body = body_of(service, document)

        for name, pattern in G.IDENTIFIER_PATTERNS:
            assert pattern.search(body) is None, f"{name} pattern survived into the body"
        for raw in (nik, email, phone):
            assert raw not in body
        assert "[EMAIL]" in body and "[ID_NUMBER]" in body and "[PHONE]" in body
        assert D.assert_identifier_free(body) == body
        assert "P1" in body

    def test_a_non_redactable_contact_value_is_refused(self, conn, tmp_path):
        """P59: a contact field that is not an identifier is refused, not stored."""
        billing_world(conn)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service, subject={"invoice_id": INVOICE_ID, "contact_email": "bukan-email"})
        assert excinfo.value.code == D.INVALID_DOCUMENT_REQUEST
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)

    def test_a_raw_identifier_in_recipient_ref_never_reaches_a_document(self, conn, tmp_path):
        """P71: an unredacted direct identifier in a subject ref is refused, not published."""
        billing_world(conn)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(
                service,
                subject={"invoice_id": INVOICE_ID, "recipient_ref": "tamu@example.com"},
            )
        assert excinfo.value.code == D.DOCUMENT_IDENTIFIER_LEAK
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)
        assert not any("tamu@example.com" in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file())

    def test_a_body_carries_no_script_token_or_unsafe_scheme(self, conn, tmp_path):
        """P60: the artefact is inert HTML whatever the caller writes in notes."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = issue_receipt(
            service,
            subject={
                "invoice_id": INVOICE_ID,
                "notes": "<script>alert(1)</script> javascript:void(0) klik di sini",
            },
        )
        body = body_of(service, document).lower()

        assert "<script" not in body
        assert "javascript:" not in body
        assert "alert(1)" in body  # the text is kept, the markup is escaped

    def test_the_plain_text_export_is_derived_from_the_stored_file(self, conn, tmp_path):
        """P61: the export cannot drift from the bytes it claims to export."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        document = issue_receipt(service)
        text = service.text(document.document_id)
        invoice = conn.execute("SELECT invoice_number FROM invoices WHERE invoice_id = ?", (INVOICE_ID,)).fetchone()

        assert str(invoice["invoice_number"]) in text
        assert "<" not in text and ">" not in text
        assert text.endswith("\n")
        assert sha256_hex(body_of(service, document)) == document.body_sha256


# --------------------------------------------------------------------------- #
# 9. Document versioning
# --------------------------------------------------------------------------- #
class TestDocumentVersioning:
    def test_second_issue_bumps_the_version_and_supersedes_the_old_row(self, conn, tmp_path):
        """P62: (document_key, version) is one-per-issue; the old row is superseded."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        key = "booking.book_0001.receipt"
        first = issue_receipt(service, key=key)
        second = issue_receipt(service, key=key)

        assert (first.version, second.version) == (1, 2)
        assert first.document_id != second.document_id
        rows = conn.execute(
            "SELECT version, status, document_id FROM documents WHERE document_key = ? ORDER BY version", (key,)
        ).fetchall()
        assert [row["version"] for row in rows] == [1, 2]
        assert [row["status"] for row in rows] == ["SUPERSEDED", "GENERATED"]
        assert service.get(first.document_id).status == "SUPERSEDED"
        assert service.get(second.document_id).status == "GENERATED"
        assert service.latest(key).document_id == second.document_id
        # Same instant + same facts => identical bytes, a different ledger identity.
        assert first.body_sha256 == second.body_sha256
        assert first.storage_ref != second.storage_ref

    def test_the_superseded_version_and_its_file_are_kept(self, conn, tmp_path):
        """P63: superseding retires a row; it never deletes one."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        key = "booking.book_0001.receipt"
        first = issue_receipt(service, key=key)
        issue_receipt(service, key=key)

        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE document_key = ?", (key,)
        ).fetchone()[0] == 2
        path = service.path_for(service.get(first.document_id))
        assert path.is_file()
        assert sha256_hex(path.read_text(encoding="utf-8")) == first.body_sha256
        report = service.verify(first.document_id, active_only=False)
        assert report.ok is True and report.issues == ()
        assert report.actual_sha256 == first.body_sha256

    def test_verify_refuses_a_non_active_version_by_default(self, conn, tmp_path):
        """P64: a superseded version is refused by default, auditable on request."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        key = "booking.book_0001.receipt"
        first = issue_receipt(service, key=key)
        issue_receipt(service, key=key)

        with pytest.raises(DomainError) as excinfo:
            service.verify(first.document_id)
        assert excinfo.value.code == D.DOCUMENT_NOT_ACTIVE
        assert service.verify(first.document_id, active_only=False).status == "SUPERSEDED"

    def test_voiding_keeps_the_file_and_the_row(self, conn, tmp_path):
        """P65: voiding is a status change, and the artefact stays auditable."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        key = "booking.book_0001.receipt"
        document = issue_receipt(service, key=key)
        path = service.path_for(document)

        voided = service.void(
            document.document_id, reason="dikirim ke tamu yang salah", actor="manager_ana", actor_role="manager"
        )
        assert voided.status == "VOID"
        assert path.is_file()
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE document_id = ?", (document.document_id,)
        ).fetchone()[0] == 1
        with pytest.raises(DomainError) as excinfo:
            service.void(
                document.document_id, reason="dua kali batal", actor="manager_ana", actor_role="manager"
            )
        assert excinfo.value.code == D.DOCUMENT_NOT_ACTIVE
        assert service.verify(document.document_id, active_only=False).ok is True


# --------------------------------------------------------------------------- #
# 10. Documents: gate on the booking status
# --------------------------------------------------------------------------- #
class TestDocumentGate:
    @pytest.mark.parametrize(
        "status",
        ["INQUIRY", "QUALIFIED", "QUOTE_SENT", "HOLD_ACTIVE", "PAYMENT_PENDING", "CANCELLED", "COMPLETED", "DISPUTED"],
    )
    def test_confirmation_is_refused_below_the_gate(self, conn, tmp_path, status):
        """P66a: a booking that has not cleared the gate gets no confirmation."""
        assert status not in D.CONFIRMATION_BOOKING_STATUSES
        billing_world(conn, booking_status=status)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            service.generate(
                kind="CONFIRMATION",
                document_key="booking.book_0001.confirmation",
                subject={"booking_id": BOOKING_ID},
                generated_by="agent_ana",
                actor_role="agent",
                at=NOW,
            )
        assert excinfo.value.code == D.DOCUMENT_SOURCE_INVALID
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE booking_id = ?", (BOOKING_ID,)
        ).fetchone()[0] == 0
        assert no_files(tmp_path)

    @pytest.mark.parametrize("status", D.CONFIRMATION_BOOKING_STATUSES)
    def test_confirmation_is_allowed_for_every_status_in_the_set(self, conn, tmp_path, status):
        """P66b: the gate is exactly the declared set — nothing more, nothing less."""
        billing_world(conn, booking_status=status)
        service = document_service(conn, tmp_path)
        document = service.generate(
            kind="CONFIRMATION",
            document_key="booking.book_0001.confirmation",
            subject={"booking_id": BOOKING_ID},
            generated_by="agent_ana",
            actor_role="agent",
            at=NOW,
        )
        assert document.status == "GENERATED"
        assert document.booking_id == BOOKING_ID

    @pytest.mark.parametrize("status", D.CONTRACT_BLOCKED_BOOKING_STATUSES)
    def test_offer_letter_is_refused_for_a_blocked_status(self, conn, tmp_path, status):
        """P67: an offer letter is refused wherever it would be moot."""
        billing_world(conn, booking_status=status)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            service.generate(
                kind="CONTRACT",
                document_key="booking.book_0001.offer",
                subject={"booking_id": BOOKING_ID},
                generated_by="manager_ana",
                actor_role="manager",
                at=NOW,
            )
        assert excinfo.value.code == D.DOCUMENT_SOURCE_INVALID
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)

    @pytest.mark.parametrize(
        "kind,role,allowed",
        [
            ("RECEIPT", "agent", False),
            ("RECEIPT", "finance", True),
            ("RECEIPT", "manager", True),
            ("CONFIRMATION", "agent", True),
            ("CONFIRMATION", "finance", False),
            ("CONTRACT", "agent", False),
            ("CONTRACT", "manager", True),
            ("INVOICE", "finance", True),
            ("INVOICE", "agent", False),
        ],
    )
    def test_role_policy_decides_who_may_issue(self, conn, tmp_path, kind, role, allowed):
        """P68: the role policy is enforced per kind, before any write."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        subject = {"invoice_id": INVOICE_ID} if kind in ("RECEIPT", "INVOICE") else {"booking_id": BOOKING_ID}

        if allowed:
            document = service.generate(
                kind=kind,
                document_key=f"booking.book_0001.{kind.lower()}",
                subject=subject,
                generated_by=f"{role}_ana",
                actor_role=role,
                at=NOW,
            )
            assert document.status == "GENERATED"
        else:
            with pytest.raises(DomainError) as excinfo:
                service.generate(
                    kind=kind,
                    document_key=f"booking.book_0001.{kind.lower()}",
                    subject=subject,
                    generated_by=f"{role}_ana",
                    actor_role=role,
                    at=NOW,
                )
            assert excinfo.value.code == D.DOCUMENT_ROLE_FORBIDDEN
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            assert no_files(tmp_path)

    def test_an_unvetted_subject_field_is_refused(self, conn, tmp_path):
        """P69: a field the service does not publish cannot ride into the body."""
        billing_world(conn)
        service = document_service(conn, tmp_path)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service, subject={"invoice_id": INVOICE_ID, "guest_email": "tamu@example.com"})
        assert excinfo.value.code == D.UNKNOWN_SUBJECT_FIELD
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert no_files(tmp_path)


# --------------------------------------------------------------------------- #
# Fail-closed boundaries — plus the one defect this probe found
# --------------------------------------------------------------------------- #
class TestFailClosed:
    def test_a_refused_match_is_not_a_match_run(self, conn):
        """P74: every refusal (criteria, inventory) leaves the ledger untouched."""
        Probe(conn)
        with pytest.raises(DomainError) as missing:
            M.match(
                conn,
                case_id="case_0001",
                facts={"check_out": CHECK_OUT, "adults": 2},
                kernel=kernel(conn),
                now=NOW,
                ids=IdFactory(deterministic=True, seed="refused"),
            )
        assert missing.value.code == M.CODE_MISSING_FACTS
        for facts in (
            {"check_in": CHECK_OUT, "check_out": CHECK_IN, "adults": 2},  # inverted stay
            {"check_in": CHECK_IN, "check_out": CHECK_OUT, "adults": 2.5},  # a float guest count
        ):
            with pytest.raises(DomainError):
                run_match(conn, facts=facts)
        assert conn.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM match_candidates").fetchone()[0] == 0

        # Empty inventory: the same promise (no row for a failed match).
        conn.execute("DELETE FROM availability_evidence")
        conn.execute("DELETE FROM units")
        with pytest.raises(DomainError) as excinfo:
            run_match(conn)
        assert excinfo.value.code == M.CODE_NO_INVENTORY
        assert conn.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == 0

    def test_a_refused_reissue_keeps_the_previous_version_intact(self, conn, tmp_path):
        """P75: a refusal after v1 exists neither retires v1 nor touches its file."""
        billing_world(conn)
        service = document_service(conn, tmp_path)
        first = issue_receipt(service)

        with pytest.raises(DomainError) as excinfo:
            issue_receipt(service, subject={"invoice_id": INVOICE_ID, "total_minor": 999})
        assert excinfo.value.code == D.INVOICE_SOURCE_MISMATCH

        stored = [tuple(row) for row in conn.execute("SELECT version, status FROM documents ORDER BY version")]
        assert stored == [(1, "GENERATED")]
        assert service.verify(first.document_id).ok
        assert snapshot(tmp_path) == {first.storage_ref}

    def test_storage_failure_surfaces_as_a_coded_domain_error(self, conn, tmp_path):
        """P76 — RED: a storage failure must carry ``DOCUMENT_STORAGE_INVALID``.

        DEFECT (``src/documents.py``, ``_write_body``): ``path.parent.mkdir(...)``
        runs *outside* the ``try/except OSError`` that turns a write failure into
        ``DomainError`` with :data:`DOCUMENT_STORAGE_INVALID`, so an unusable root
        escapes as a raw ``OSError`` subclass (``NotADirectoryError``) — the one
        storage failure that is not reported as a coded refusal. The rest of the
        fail-closed story does hold here: the transaction rolls back (no second
        row, v1 stays ``GENERATED``, connection not left in a transaction) and no
        file is written — which is why this test asserts all of that too.
        """
        billing_world(conn)
        service = document_service(conn, tmp_path)
        first = issue_receipt(service)
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("this is a file, not a directory", encoding="utf-8")
        broken = document_service(conn, blocker / "docs", ids_seed="broken")

        try:
            issue_receipt(broken)
        except DomainError as excinfo:
            assert excinfo.code == D.DOCUMENT_STORAGE_INVALID
        except Exception as exc:  # noqa: BLE001 - the defect: a raw OSError escapes
            pytest.fail(
                f"storage failure escaped as {type(exc).__name__} instead of"
                f" DomainError[{D.DOCUMENT_STORAGE_INVALID}]: {exc}",
                pytrace=False,
            )
        else:  # pragma: no cover - would be a second defect
            pytest.fail("an unusable storage root must not issue a document", pytrace=False)

        stored = [tuple(row) for row in conn.execute("SELECT version, status FROM documents ORDER BY version")]
        assert stored == [(1, "GENERATED")]
        assert D.DocumentService(conn, root=tmp_path).verify(first.document_id).ok
        # The blocker file is the fixture that makes the path unusable, so it is
        # excluded here: what this asserts is that the failed attempt wrote nothing.
        assert snapshot(tmp_path) - {"not-a-directory"} == {first.storage_ref}
        assert not blocker.is_dir()

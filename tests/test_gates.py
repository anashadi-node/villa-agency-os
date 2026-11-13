"""Rigorous tests for ``src/gates.py`` — the fail-closed policy gates.

Expectations are derived from the authoritative sources, not from the module's
own constants, so that policy drift fails here:

* ``src/schema/001_initial.sql`` — the DDL is the schema of record: the enum
  vocabularies of the gate inputs (property status, contract pack status, unit
  status, availability evidence level/source/result), the ``can_confirm = 0 OR
  evidence_level IN ('A','B')`` constraint, the ``captured_at < valid_until``
  constraint and the "money is INTEGER minor units + ISO-4217 currency" rule.
* ``docs/PROPERTY_CONTRACT_PACK.md`` — the 20 contract-pack items, which of
  them block a sale (Gate #0) and their re-check cadence in days.
* ``docs/POLICY_MATRIX.md`` §6 — the availability freshness table (per-level
  default window, the guardrail ceiling, and ``confirm_requires_level``).
* ``docs/DATA_CLASSIFICATION_AND_ACCESS.md`` — the P0..P4 classification
  matrix (never store P4, never send P3 to an LLM/log, redact P2 before an
  LLM, export P3 only for Admin/Finance with a recorded reason).

House rules asserted here as well: decisions are deterministic for the same
input object, reason codes are exact strings, money never becomes a float, and
anything unknown/missing/malformed DENIES (it never silently allows).
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gates import (  # noqa: F401 -- names re-used by the checks below
    CLASSIFICATIONS,
    CODE_OK,
    CONTRACT_PACK_STATUSES,
    DESTINATIONS,
    GATE_AVAILABILITY,
    GATE_CLASSIFICATION,
    GATE_CONTRACT_PACK,
    GATE_UNIT,
    GATES,
    ITEM_STATUSES,
    LEVEL_FRESHNESS_SECONDS,
    LEVEL_MAX_FRESHNESS_SECONDS,
    LEVEL_SOURCE_TYPES,
    LOGGING_DESTINATIONS,
    P2_AUTH_ROLES,
    P3_AUTH_ROLES,
    PROPERTY_STATUSES,
    PURPOSE_LEVELS,
    PURPOSES,
    SOURCE_TYPES,
    UNIT_STATUSES,
    AvailabilityEvidence,
    ClassifiedField,
    ContractPack,
    ContractPackItem,
    DomainError,
    GateDecision,
    GateDenied,
    apply_redaction,
    gate_availability,
    gate_contract_pack,
    gate_outbound,
    gate_unit,
    require_allow,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "src" / "schema" / "001_initial.sql"
CONTRACT_DOC = REPO_ROOT / "docs" / "PROPERTY_CONTRACT_PACK.md"
POLICY_DOC = REPO_ROOT / "docs" / "POLICY_MATRIX.md"
CLASSIFICATION_DOC = REPO_ROOT / "docs" / "DATA_CLASSIFICATION_AND_ACCESS.md"

# ---------------------------------------------------------------------------
# Derived-from-documentation helpers
# ---------------------------------------------------------------------------

_CODE_BLOCKING = "\u2714"  # the "blocks the sale" marker in the item table
_CODE_NON_BLOCKING = "\u2716"


def _read(path: Path) -> str:
    if not path.exists():  # pragma: no cover - guard rail for the derivation
        raise AssertionError(f"authoritative source missing: {path}")
    return path.read_text(encoding="utf-8")


def _sql_block(text: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table}\b(.*?);", text, re.S)
    if match is None:  # pragma: no cover - guard rail for the derivation
        raise AssertionError(f"table {table} not found in the schema of record")
    return match.group(1)


def _ddl_enum(block: str, column: str) -> tuple[str, ...]:
    """The values of a ``CHECK (<column> IN (...))`` constraint, in DDL order."""
    match = re.search(rf"(?<!\w){column}\s+IN\s*\(([^)]*)\)", block)
    if match is None:  # pragma: no cover - guard rail for the derivation
        raise AssertionError(f"no CHECK ... IN (...) constraint for {column!r}")
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _doc_contract_items() -> dict[str, tuple[bool, bool, int | None]]:
    """``{item_id: (blocks, conditional, cadence_days)}`` from the item table."""
    items: dict[str, tuple[bool, bool, int | None]] = {}
    for line in _read(CONTRACT_DOC).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"C\d{2}", cells[0]):
            continue
        marker, cadence_cell = cells[2], cells[3]
        conditional = _CODE_BLOCKING in marker and "jika" in marker.lower()
        blocks = _CODE_BLOCKING in marker and not conditional
        days = re.search(r"(\d+)\s*hari", cadence_cell)
        items[cells[0]] = (blocks, conditional, int(days.group(1)) if days else None)
    return items


def _doc_freshness() -> dict[str, tuple[int, int]]:
    """``{level: (default_seconds, guardrail_seconds)}`` from POLICY_MATRIX §6."""
    text = _read(POLICY_DOC)
    rows: dict[str, tuple[int, int]] = {}
    pattern = re.compile(
        r"`availability_freshness_([ABCD])_seconds`\s*\|\s*(\d+)[^|]*\|\s*\u2264\s*(\d+)"
    )
    for match in pattern.finditer(text):
        rows[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    if not rows:  # pragma: no cover - guard rail for the derivation
        raise AssertionError("the freshness table disappeared from POLICY_MATRIX.md")
    return rows


def _doc_confirm_levels() -> tuple[str, ...]:
    """``confirm_requires_level`` as a tuple of levels, from POLICY_MATRIX §6."""
    match = re.search(r"`confirm_requires_level`\s*\|\s*`?([A-D](?:_or_[A-D])*)", _read(POLICY_DOC))
    if match is None:  # pragma: no cover - guard rail for the derivation
        raise AssertionError("confirm_requires_level disappeared from POLICY_MATRIX.md")
    return tuple(match.group(1).split("_or_"))


def _doc_p3_roles() -> tuple[str, ...]:
    """The roles the classification doc names as P3 export authorities."""
    text = _read(CLASSIFICATION_DOC).lower()
    found = tuple(role for role in ("admin", "finance", "manager", "gm") if role in text and "p3" in text)
    return found


# ---------------------------------------------------------------------------
# Fixtures shared by every gate test
# ---------------------------------------------------------------------------

ALL_ITEMS_VALID = {item_id: "VALID" for item_id in _doc_contract_items()}


# Sentinel for "caller did not pass this argument" (so an explicit None is preserved).
_UNSET = object()


class GateTestCase(unittest.TestCase):
    """Shared builders: every fixture below is a *row* shape, as the gates take it."""

    NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    _UNSET = _UNSET

    # -- time ---------------------------------------------------------------
    @property
    def now(self) -> str:
        return self.NOW.isoformat()

    def at(self, seconds: int = 0) -> str:
        return (self.NOW + timedelta(seconds=seconds)).isoformat()

    # -- Gate #0 fixtures ---------------------------------------------------
    def item(
        self,
        item_id: str = "C01",
        status: str = "VALID",
        blocking: object = True,
        next_review_at: object = None,
    ) -> dict[str, object]:
        return {
            "item_id": item_id,
            "status": status,
            "blocking": blocking,
            "next_review_at": next_review_at,
        }

    def pack(
        self,
        items: object = None,
        property_status: str = "SELLABLE",
        pack_status: str = "ACTIVATED",
        property_id: str = "P-1",
        **over: object,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "property_id": property_id,
            "contract_pack_status": pack_status,
            "property_status": property_status,
            "items": [self.item()] if items is None else items,
        }
        row.update(over)
        return row

    # -- availability fixtures ---------------------------------------------
    def evidence(
        self,
        level: str = "A",
        source: str = "property_api",
        captured_at: object = _UNSET,
        result: str = "AVAILABLE",
        **over: object,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "evidence_id": "E-1",
            "unit_id": "U-1",
            "evidence_level": level,
            "source_type": source,
            "captured_at": self.now if captured_at is _UNSET else captured_at,
            "result": result,
            "can_quote": True,
            "can_hold": True,
            "can_confirm": True,
        }
        row.update(over)
        return row

    def classified(self, name: str = "field", classification: str = "P0", value: object = "Villa A", redacted: object = False) -> ClassifiedField:
        return ClassifiedField(name=name, classification=classification, value=value, redacted=redacted)

    # -- assertions ---------------------------------------------------------
    def assertAllowed(self, decision: GateDecision, code: str = CODE_OK) -> None:
        self.assertIsInstance(decision, GateDecision)
        self.assertTrue(decision.allowed, msg=f"expected ALLOW {code}, got {decision}")
        self.assertEqual(decision.code, code)
        self.assertFalse(decision.denied)

    def assertDenied(self, decision: GateDecision, code: str, gate: str | None = None) -> None:
        self.assertIsInstance(decision, GateDecision)
        self.assertFalse(decision.allowed, msg=f"expected DENY {code}, got {decision}")
        self.assertEqual(decision.code, code)
        self.assertTrue(decision.denied)
        self.assertNotEqual(decision.code, CODE_OK)
        if gate is not None:
            self.assertEqual(decision.gate, gate)


# ---------------------------------------------------------------------------
# The vocabulary of denial codes and the gate registry
# ---------------------------------------------------------------------------

class TestReasonCodeVocabulary(GateTestCase):
    """Every documented reason code is a distinct, published, machine-readable string."""

    # Every literal asserted anywhere in this file; a rename in the module must fail here.
    LITERALS = (
        "APPROVAL_REQUIRED",
        "AVAILABILITY_UNKNOWN",
        "CHANNEL_FORBIDDEN",
        "CONTRACT_PACK_DRAFT",
        "CONTRACT_PACK_EMPTY",
        "CONTRACT_PACK_EVALUATION_STALE",
        "CONTRACT_PACK_EXPIRED",
        "CONTRACT_PACK_ITEM_EXPIRED",
        "CONTRACT_PACK_ITEM_INVALID",
        "CONTRACT_PACK_ITEM_MISSING",
        "CONTRACT_PACK_MISSING",
        "CONTRACT_PACK_NOT_ACTIVATED",
        "CONTRACT_PACK_STATUS_UNKNOWN",
        "CONTENT_LOGGING_FORBIDDEN",
        "EVIDENCE_CANNOT_CONFIRM",
        "EVIDENCE_CANNOT_HOLD",
        "EVIDENCE_CANNOT_QUOTE",
        "EVIDENCE_EXPIRED",
        "EVIDENCE_FROM_FUTURE",
        "EVIDENCE_LEVEL_TOO_WEAK",
        "EVIDENCE_LEVEL_UNKNOWN",
        "EVIDENCE_MISSING",
        "EVIDENCE_SOURCE_LEVEL_MISMATCH",
        "EVIDENCE_SOURCE_UNKNOWN",
        "EVIDENCE_STALE",
        "EVIDENCE_TIMESTAMP_INVALID",
        "EVIDENCE_WINDOW_INVALID",
        "EXPORT_REQUIRES_JUSTIFICATION",
        "EXPORT_REQUIRES_ROLE",
        "FRESHNESS_WINDOW_TOO_LOOSE",
        "INVALID_FRESHNESS_WINDOW",
        "INVALID_NOW",
        "INVALID_PURPOSE",
        "MISCLASSIFIED_CONTENT",
        "P3_FORBIDDEN",
        "P4_PROHIBITED",
        "PII_REDACTION_REQUIRED",
        "PROPERTY_NOT_SELLABLE",
        "PROPERTY_STATUS_UNKNOWN",
        "PROPERTY_SUSPENDED",
        "REDACTION_NOT_APPLIED",
        "REVIEW_REQUIRED",
        "UNIT_IN_MAINTENANCE",
        "UNIT_OCCUPIED",
        "UNIT_STATUS_UNKNOWN",
        "UNIT_SUSPENDED",
        "UNKNOWN_CLASSIFICATION",
        "UNKNOWN_DESTINATION",
        "EMPTY_PAYLOAD",
    )

    def test_reason_codes_are_unique_non_empty_uppercase_strings(self):
        import gates

        codes = {name: value for name, value in vars(gates).items() if name.startswith("CODE_")}
        self.assertGreaterEqual(len(codes), 45, "the denial-code vocabulary shrank unexpectedly")
        for name, value in codes.items():
            with self.subTest(code=name):
                self.assertIsInstance(value, str)
                self.assertTrue(value)
                self.assertEqual(value, value.upper())
                self.assertNotIn(" ", value)
        self.assertEqual(len(set(codes.values())), len(codes), "reason codes must be distinct")

    def test_every_code_asserted_in_this_file_is_a_published_constant(self):
        import gates

        published = {name: value for name, value in vars(gates).items() if name.startswith("CODE_")}
        values = set(published.values())
        for literal in self.LITERALS:
            with self.subTest(code=literal):
                self.assertIn(literal, values)

    def test_ok_is_the_only_allow_code(self):
        self.assertEqual(CODE_OK, "OK")
        self.assertNotIn(CODE_OK, self.LITERALS)

    def test_gate_registry_holds_the_four_documented_gates(self):
        self.assertEqual(
            set(GATES),
            {GATE_CONTRACT_PACK, GATE_AVAILABILITY, GATE_CLASSIFICATION, GATE_UNIT},
        )
        self.assertEqual(
            {GATE_CONTRACT_PACK, GATE_AVAILABILITY, GATE_CLASSIFICATION, GATE_UNIT},
            {"contract_pack", "availability", "classification", "unit"},
        )
        for name, func in GATES.items():
            with self.subTest(gate=name):
                self.assertTrue(callable(func))
                self.assertTrue((func.__doc__ or "").strip(), "each gate documents its intent")

    def test_module_states_the_fail_closed_rule(self):
        import gates

        doc = (gates.__doc__ or "").lower()
        self.assertIn("fail closed", doc.replace("fails", "fail"))
        self.assertIn("gate", doc)

    def test_purposes_are_the_four_documented_stages(self):
        self.assertEqual(tuple(PURPOSES), ("show", "quote", "hold", "confirm"))
        self.assertEqual(set(PURPOSE_LEVELS), set(PURPOSES))


# ---------------------------------------------------------------------------
# Schema-of-record derivations (DDL) and the money/time invariants
# ---------------------------------------------------------------------------

class TestSchemaDerivedPolicy(GateTestCase):
    """The gate vocabularies and level rules must match the schema of record."""

    @classmethod
    def setUpClass(cls):
        cls.sql = _read(SCHEMA_SQL)

    def test_property_status_vocabulary_matches_the_ddl(self):
        block = _sql_block(self.sql, "properties")
        self.assertEqual(_ddl_enum(block, "status"), PROPERTY_STATUSES)

    def test_contract_pack_status_vocabulary_matches_the_ddl(self):
        block = _sql_block(self.sql, "properties")
        self.assertEqual(_ddl_enum(block, "contract_pack_status"), CONTRACT_PACK_STATUSES)

    def test_unit_status_vocabulary_matches_the_ddl(self):
        block = _sql_block(self.sql, "units")
        self.assertEqual(_ddl_enum(block, "status"), UNIT_STATUSES)

    def test_availability_evidence_vocabularies_match_the_ddl(self):
        block = _sql_block(self.sql, "availability_evidence")
        self.assertEqual(_ddl_enum(block, "evidence_level"), tuple(sorted(LEVEL_SOURCE_TYPES, reverse=False)))
        self.assertEqual(_ddl_enum(block, "source_type"), SOURCE_TYPES)
        self.assertEqual(
            _ddl_enum(block, "result"),
            ("AVAILABLE", "NOT_AVAILABLE", "AVAILABLE_BUT_RESTRICTED", "UNKNOWN"),
        )

    def test_every_source_type_belongs_to_exactly_one_level(self):
        block = _sql_block(self.sql, "availability_evidence")
        graders = _ddl_enum(block, "source_type")
        mapped = [source for sources in LEVEL_SOURCE_TYPES.values() for source in sources]
        self.assertEqual(sorted(mapped), sorted(graders))
        self.assertEqual(len(mapped), len(set(mapped)), "a source type cannot grade two levels")

    def test_confirm_is_restricted_to_levels_a_and_b_by_the_ddl_and_by_the_gate(self):
        block = _sql_block(self.sql, "availability_evidence")
        match = re.search(r"can_confirm\s*=\s*0\s+OR\s+evidence_level\s+IN\s*\(([^)]*)\)", block)
        self.assertIsNotNone(match, "the can_confirm level constraint disappeared from the DDL")
        ddl_levels = tuple(re.findall(r"'([^']*)'", match.group(1)))
        self.assertEqual(ddl_levels, _doc_confirm_levels())
        self.assertEqual(PURPOSE_LEVELS["confirm"], ddl_levels)
        evidence = self.evidence(level="C", source="ical")
        self.assertDenied(
            gate_availability(evidence, now=self.now, purpose="confirm"),
            "EVIDENCE_LEVEL_TOO_WEAK",
            GATE_AVAILABILITY,
        )

    def test_evidence_window_constraint_is_enforced_as_a_gate_denial(self):
        block = _sql_block(self.sql, "availability_evidence")
        self.assertIn("captured_at < valid_until", block)
        captured = self.at(-60)
        self.assertDenied(
            gate_availability(
                self.evidence(captured_at=captured, valid_until=captured),
                now=self.now,
                purpose="quote",
            ),
            "EVIDENCE_WINDOW_INVALID",
            GATE_AVAILABILITY,
        )

    def test_money_is_integer_minor_units_with_an_iso4217_currency_column(self):
        money, currencies = [], []
        for line in self.sql.splitlines():
            match = re.match(r"\s*(\w+)\s+(\w+)\s+(.*)$", line)
            if match is None:
                continue
            column, declared_type = match.group(1), match.group(2)
            if column.endswith("_minor"):
                money.append((column, declared_type))
            elif column == "currency":
                currencies.append(line)
        self.assertTrue(money, "the schema stopped declaring minor-unit money columns")
        for column, declared_type in money:
            with self.subTest(column=column):
                self.assertEqual(declared_type, "INTEGER", "money is integer minor units, never a float")
        self.assertTrue(currencies)
        for line in currencies:
            with self.subTest(currency=line.strip()):
                self.assertIn("length(currency) = 3", line, "currency must be an ISO-4217 code")

    def test_freshness_windows_match_the_policy_matrix(self):
        derived = _doc_freshness()
        for level, (default, guardrail) in derived.items():
            with self.subTest(level=level):
                self.assertEqual(LEVEL_FRESHNESS_SECONDS[level], default)
                self.assertEqual(LEVEL_MAX_FRESHNESS_SECONDS[level], guardrail)

    def test_level_capability_table_matches_the_policy_matrix(self):
        # §6: level C is indicative (show/quote/hold, never confirm); level D cannot be used.
        self.assertIn("C", PURPOSE_LEVELS["show"])
        self.assertIn("C", PURPOSE_LEVELS["quote"])
        self.assertIn("C", PURPOSE_LEVELS["hold"])
        self.assertNotIn("C", PURPOSE_LEVELS["confirm"])
        for purpose in PURPOSES:
            with self.subTest(purpose=purpose):
                self.assertNotIn("D", PURPOSE_LEVELS[purpose])
        self.assertEqual(LEVEL_FRESHNESS_SECONDS["D"], 0)
        self.assertEqual(LEVEL_MAX_FRESHNESS_SECONDS["D"], 0)
        self.assertEqual(LEVEL_SOURCE_TYPES["D"], ("memory", "manual_ops"))

    def test_classification_vocabulary_edge_and_the_doc_agree(self):
        self.assertEqual(tuple(CLASSIFICATIONS), ("P0", "P1", "P2", "P3", "P4"))
        for token in ("P0", "P1", "P2", "P3", "P4"):
            with self.subTest(classification=token):
                self.assertIn(token, _read(CLASSIFICATION_DOC))

    def test_decisions_never_carry_floats_and_stay_json_serialisable(self):
        decisions = [
            gate_contract_pack(self.pack(), now=self.now, purpose="quote"),
            gate_contract_pack(self.pack(property_status="SELLABLE_WITH_REVIEW"), now=self.now, purpose="quote"),
            gate_availability(self.evidence(), now=self.now, purpose="quote"),
            gate_outbound({"g": self.classified("g", "P2", "Budi")}, destination="store"),
            gate_unit(now=self.now, purpose="quote", unit_status="ACTIVE", pack=self.pack(), evidence=self.evidence()),
        ]
        for decision in decisions:
            with self.subTest(gate=decision.gate, code=decision.code):
                payload = decision.as_dict()
                json.dumps(payload)  # must not raise: decisions are plain data
                stack = [payload]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        stack.extend(node.values())
                    elif isinstance(node, (list, tuple)):
                        stack.extend(node)
                    else:
                        self.assertNotIsInstance(
                            node, float, "money/quantities are integers, never floats"
                        )


# ---------------------------------------------------------------------------
# Gate #0 — the contract pack, derived from docs/PROPERTY_CONTRACT_PACK.md
# ---------------------------------------------------------------------------

class TestContractPackDocDerivation(GateTestCase):
    """Gate #0 mirrors the item table and the re-check cadence in the contract pack doc."""

    @classmethod
    def setUpClass(cls):
        cls.items = _doc_contract_items()

    def blocking_ids(self) -> list[str]:
        return sorted(i for i, (blocks, _cond, _days) in self.items.items() if blocks)

    def non_blocking_ids(self) -> list[str]:
        return sorted(
            i for i, (blocks, cond, _days) in self.items.items() if not blocks and not cond
        )

    def conditional_ids(self) -> list[str]:
        return sorted(i for i, (_b, cond, _d) in self.items.items() if cond)

    def test_item_table_has_twenty_items_with_the_documented_blocking_split(self):
        self.assertEqual(len(self.items), 20)
        self.assertEqual(sorted(self.items), [f"C{n:02d}" for n in range(1, 21)])
        self.assertEqual(self.blocking_ids(), [f"C{n:02d}" for n in range(1, 12)])
        self.assertEqual(self.non_blocking_ids(), ["C12", "C13", "C15", "C16", "C17", "C18", "C19", "C20"])
        self.assertEqual(self.conditional_ids(), ["C14"])

    def test_a_blocking_item_that_is_missing_or_expired_denies_the_sale(self):
        for item_id in self.blocking_ids():
            with self.subTest(item=item_id):
                pack = self.pack(items=[self.item(item_id=item_id, status="MISSING")])
                decision = gate_contract_pack(pack, now=self.now, purpose="quote")
                self.assertDenied(decision, "CONTRACT_PACK_ITEM_MISSING", GATE_CONTRACT_PACK)
                self.assertEqual(decision.details["item_ids"], [item_id])

    def test_a_non_blocking_gap_is_a_review_flag_not_a_denial(self):
        for item_id in self.non_blocking_ids():
            with self.subTest(item=item_id):
                pack = self.pack(items=[self.item(item_id=item_id, status="MISSING", blocking=False)])
                decision = gate_contract_pack(pack, now=self.now, purpose="quote")
                self.assertAllowed(decision, "REVIEW_REQUIRED")
                self.assertTrue(decision.review_required)
                self.assertEqual(decision.details["review_items"], [item_id])

    def test_the_conditional_item_blocks_only_when_it_is_flagged_blocking(self):
        item_id = self.conditional_ids()[0]
        blocked = gate_contract_pack(
            self.pack(items=[self.item(item_id=item_id, status="MISSING", blocking=True)]),
            now=self.now,
            purpose="quote",
        )
        self.assertDenied(blocked, "CONTRACT_PACK_ITEM_MISSING", GATE_CONTRACT_PACK)
        reviewed = gate_contract_pack(
            self.pack(items=[self.item(item_id=item_id, status="MISSING", blocking=False)]),
            now=self.now,
            purpose="quote",
        )
        self.assertAllowed(reviewed, "REVIEW_REQUIRED")

    def test_documented_recheck_cadence_is_the_expiry_boundary(self):
        for item_id, (blocks, _cond, days) in sorted(self.items.items()):
            if days is None:
                continue
            with self.subTest(item=item_id, days=days):
                fresh = gate_contract_pack(
                    self.pack(
                        items=[
                            self.item(
                                item_id=item_id,
                                blocking=blocks,
                                next_review_at=self.at(days * 86400),
                            )
                        ]
                    ),
                    now=self.now,
                    purpose="quote",
                )
                self.assertAllowed(fresh, CODE_OK)
                stale = gate_contract_pack(
                    self.pack(
                        items=[
                            self.item(
                                item_id=item_id,
                                blocking=blocks,
                                next_review_at=self.at(days * 86400 + 1),
                            )
                        ]
                    ),
                    now=self.now,
                    purpose="quote",
                )
                self.assertAllowed(stale, CODE_OK)

    def test_documented_recheck_cadence_past_the_boundary_is_expired(self):
        # A review date that has already passed means the item's documented
        # re-check cadence lapsed.
        for item_id, (blocks, _cond, days) in sorted(self.items.items()):
            if days is None:
                continue
            past = self.pack(
                items=[
                    self.item(item_id=item_id, blocking=blocks, next_review_at=self.at(-1))
                ]
            )
            decision = gate_contract_pack(past, now=self.now, purpose="quote")
            with self.subTest(item=item_id, blocking=blocks, days=days):
                if blocks:
                    self.assertDenied(decision, "CONTRACT_PACK_ITEM_EXPIRED", GATE_CONTRACT_PACK)
                    self.assertEqual(decision.details["item_ids"], [item_id])
                else:
                    self.assertAllowed(decision, "REVIEW_REQUIRED")
                    self.assertEqual(decision.details["review_items"], [item_id])

    def test_items_without_a_review_date_never_expire(self):
        for item_id, (_blocks, _cond, days) in sorted(self.items.items()):
            if days is not None:
                continue
            with self.subTest(item=item_id):
                pack = self.pack(items=[self.item(item_id=item_id, next_review_at=None)])
                self.assertAllowed(gate_contract_pack(pack, now=self.now, purpose="quote"), CODE_OK)


# ---------------------------------------------------------------------------
# Gate #0 — allow/deny paths, boundaries and malformed input
# ---------------------------------------------------------------------------

class TestGateContractPack(GateTestCase):
    def test_a_complete_activated_pack_on_a_sellable_property_allows(self):
        decision = gate_contract_pack(self.pack(), now=self.now, purpose="quote")
        self.assertAllowed(decision, CODE_OK)
        self.assertEqual(decision.gate, GATE_CONTRACT_PACK)
        self.assertFalse(decision.review_required)
        self.assertEqual(decision.details["property_id"], "P-1")
        self.assertEqual(decision.details["property_status"], "SELLABLE")

    def test_all_twenty_items_valid_is_still_allowed(self):
        items = [self.item(item_id=item_id) for item_id in sorted(ALL_ITEMS_VALID)]
        self.assertAllowed(
            gate_contract_pack(self.pack(items=items), now=self.now, purpose="quote"), CODE_OK
        )

    def test_sellable_with_review_allows_but_flags_the_review(self):
        for purpose in ("show", "quote", "hold"):
            with self.subTest(purpose=purpose):
                decision = gate_contract_pack(
                    self.pack(property_status="SELLABLE_WITH_REVIEW"), now=self.now, purpose=purpose
                )
                self.assertAllowed(decision, "REVIEW_REQUIRED")
                self.assertTrue(decision.review_required)
        # A flagged pack may not be confirmed without a recorded approval.
        self.assertDenied(
            gate_contract_pack(
                self.pack(property_status="SELLABLE_WITH_REVIEW"), now=self.now, purpose="confirm"
            ),
            "APPROVAL_REQUIRED",
            GATE_CONTRACT_PACK,
        )

    def test_not_sellable_wins_over_green_items(self):
        items = [self.item(item_id=item_id) for item_id in sorted(ALL_ITEMS_VALID)]
        decision = gate_contract_pack(
            self.pack(items=items, property_status="NOT_SELLABLE"), now=self.now, purpose="quote"
        )
        self.assertDenied(decision, "PROPERTY_NOT_SELLABLE", GATE_CONTRACT_PACK)
        self.assertEqual(decision.details["property_status"], "NOT_SELLABLE")

    def test_suspended_property_denies(self):
        for value in ("SUSPENDED", "suspended", " Suspended "):
            with self.subTest(value=value):
                self.assertDenied(
                    gate_contract_pack(self.pack(property_status=value), now=self.now, purpose="quote"),
                    "PROPERTY_SUSPENDED",
                    GATE_CONTRACT_PACK,
                )

    def test_unknown_property_status_denies(self):
        for value in ("SELLABLE_LATER", "", None, 5, "SELL"):
            with self.subTest(value=value):
                self.assertDenied(
                    gate_contract_pack(self.pack(property_status=value), now=self.now, purpose="quote"),
                    "PROPERTY_STATUS_UNKNOWN",
                    GATE_CONTRACT_PACK,
                )

    def test_pack_status_gates_in_order(self):
        cases = {
            "DRAFT": "CONTRACT_PACK_DRAFT",
            "draft": "CONTRACT_PACK_DRAFT",
            "VERIFIED": "CONTRACT_PACK_NOT_ACTIVATED",
            "EXPIRED": "CONTRACT_PACK_EXPIRED",
            "ARCHIVED": "CONTRACT_PACK_STATUS_UNKNOWN",
            "": "CONTRACT_PACK_STATUS_UNKNOWN",
            None: "CONTRACT_PACK_STATUS_UNKNOWN",
            5: "CONTRACT_PACK_STATUS_UNKNOWN",
        }
        for pack_status, expected in cases.items():
            with self.subTest(pack_status=pack_status):
                decision = gate_contract_pack(
                    self.pack(pack_status=pack_status), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, expected, GATE_CONTRACT_PACK)

    def test_empty_pack_denies_because_completeness_cannot_be_claimed(self):
        for items in ([], (), None, "", b"", 0):
            with self.subTest(items=items):
                decision = gate_contract_pack(
                    self.pack(items=items) if items is not None else {
                        "property_id": "P-1",
                        "contract_pack_status": "ACTIVATED",
                        "property_status": "SELLABLE",
                    },
                    now=self.now,
                    purpose="quote",
                )
                self.assertDenied(decision, "CONTRACT_PACK_EMPTY", GATE_CONTRACT_PACK)

    def test_item_status_vocabulary_is_deny_or_review_only(self):
        # ITEM_STATUSES comes from the DDL; nothing here may ever allow a gap silently.
        expectations = {
            "VALID": CODE_OK,
            "MISSING": "CONTRACT_PACK_ITEM_MISSING",
            "EXPIRED": "CONTRACT_PACK_ITEM_EXPIRED",
            "INVALID": "CONTRACT_PACK_ITEM_INVALID",
            "PENDING": "CONTRACT_PACK_ITEM_INVALID",
        }
        self.assertEqual(set(expectations), set(ITEM_STATUSES))
        for status, expected in expectations.items():
            with self.subTest(status=status):
                decision = gate_contract_pack(
                    self.pack(items=[self.item(item_id="C01", status=status, blocking=True)]),
                    now=self.now,
                    purpose="quote",
                )
                if expected == CODE_OK:
                    self.assertAllowed(decision, CODE_OK)
                else:
                    self.assertDenied(decision, expected, GATE_CONTRACT_PACK)

    def test_blocking_item_status_denials_use_the_specific_code(self):
        cases = {
            "MISSING": "CONTRACT_PACK_ITEM_MISSING",
            "": "CONTRACT_PACK_ITEM_MISSING",
            None: "CONTRACT_PACK_ITEM_MISSING",
            "EXPIRED": "CONTRACT_PACK_ITEM_EXPIRED",
            "INVALID": "CONTRACT_PACK_ITEM_INVALID",
            "PENDING": "CONTRACT_PACK_ITEM_INVALID",
            "weird": "CONTRACT_PACK_ITEM_INVALID",
            5: "CONTRACT_PACK_ITEM_INVALID",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                decision = gate_contract_pack(
                    self.pack(items=[self.item(item_id="C01", status=status, blocking=True)]),
                    now=self.now,
                    purpose="quote",
                )
                self.assertDenied(decision, expected, GATE_CONTRACT_PACK)

    def test_item_expiry_boundary_is_inclusive(self):
        exact = gate_contract_pack(
            self.pack(items=[self.item(next_review_at=self.now)]), now=self.now, purpose="quote"
        )
        self.assertAllowed(exact, CODE_OK)
        one_second_past = gate_contract_pack(
            self.pack(items=[self.item(next_review_at=self.at(-1))]), now=self.now, purpose="quote"
        )
        self.assertDenied(one_second_past, "CONTRACT_PACK_ITEM_EXPIRED", GATE_CONTRACT_PACK)

    def test_a_plain_date_means_the_end_of_that_utc_day(self):
        pack = self.pack(items=[self.item(next_review_at="2026-09-21")])
        self.assertAllowed(
            gate_contract_pack(pack, now="2026-09-21T23:59:59+00:00", purpose="quote"), CODE_OK
        )
        self.assertDenied(
            gate_contract_pack(pack, now="2026-09-22T00:00:00+00:00", purpose="quote"),
            "CONTRACT_PACK_ITEM_EXPIRED",
            GATE_CONTRACT_PACK,
        )

    def test_unreadable_or_naive_item_dates_deny_instead_of_expiring_silently(self):
        for value in ("whenever", "2026-09-21T12:00:00", "", 0, datetime(2026, 9, 21, 12, 0), {}):
            with self.subTest(next_review_at=repr(value)):
                decision = gate_contract_pack(
                    self.pack(items=[self.item(next_review_at=value)]), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "CONTRACT_PACK_ITEM_INVALID", GATE_CONTRACT_PACK)

    def test_evaluation_age_boundary(self):
        exact = gate_contract_pack(
            self.pack(evaluated_at=self.at(-86400)), now=self.now, purpose="quote", max_pack_age_hours=24
        )
        self.assertAllowed(exact, CODE_OK)
        stale = gate_contract_pack(
            self.pack(evaluated_at=self.at(-86401)), now=self.now, purpose="quote", max_pack_age_hours=24
        )
        self.assertDenied(stale, "CONTRACT_PACK_EVALUATION_STALE", GATE_CONTRACT_PACK)
        self.assertEqual(stale.details["age_seconds"], 86401)
        self.assertEqual(stale.details["window_seconds"], 86400)

    def test_age_bound_of_zero_is_still_a_bound(self):
        self.assertAllowed(
            gate_contract_pack(
                self.pack(evaluated_at=self.now), now=self.now, purpose="quote", max_pack_age_hours=0
            ),
            CODE_OK,
        )
        self.assertDenied(
            gate_contract_pack(
                self.pack(evaluated_at=self.at(-1)), now=self.now, purpose="quote", max_pack_age_hours=0
            ),
            "CONTRACT_PACK_EVALUATION_STALE",
            GATE_CONTRACT_PACK,
        )

    def test_missing_unreadable_or_future_evaluation_time_still_denies_when_bounded(self):
        cases = {
            "absent": self.pack(),
            "garbage": self.pack(evaluated_at="whenever"),
            "naive string": self.pack(evaluated_at="2026-09-21T12:00:00"),
            "naive datetime": self.pack(evaluated_at=datetime(2026, 9, 21, 12, 0)),
            "future": self.pack(evaluated_at=self.at(1)),
            "int": self.pack(evaluated_at=0),
        }
        for label, pack in cases.items():
            with self.subTest(case=label):
                decision = gate_contract_pack(pack, now=self.now, purpose="quote", max_pack_age_hours=24)
                self.assertDenied(decision, "CONTRACT_PACK_EVALUATION_STALE", GATE_CONTRACT_PACK)

    def test_an_unbounded_pack_is_not_age_checked(self):
        for hours in (None,):
            with self.subTest(max_pack_age_hours=hours):
                self.assertAllowed(
                    gate_contract_pack(self.pack(), now=self.now, purpose="quote", max_pack_age_hours=hours),
                    CODE_OK,
                )

    def test_invalid_age_windows_are_refused(self):
        for value in (-1, -24, "nope", "", True, False, 1.5, float("nan"), float("inf"), [], {}):
            with self.subTest(max_pack_age_hours=repr(value)):
                decision = gate_contract_pack(
                    self.pack(evaluated_at=self.now), now=self.now, purpose="quote", max_pack_age_hours=value
                )
                self.assertDenied(decision, "INVALID_FRESHNESS_WINDOW", GATE_CONTRACT_PACK)

    def test_a_numeric_string_window_is_honoured(self):
        self.assertAllowed(
            gate_contract_pack(
                self.pack(evaluated_at=self.at(-3600)), now=self.now, purpose="quote", max_pack_age_hours="24"
            ),
            CODE_OK,
        )

    def test_confirm_on_a_flagged_pack_needs_a_recorded_approval(self):
        pack = self.pack(property_status="SELLABLE_WITH_REVIEW", items=[self.item()])
        self.assertDenied(
            gate_contract_pack(pack, now=self.now, purpose="confirm"),
            "APPROVAL_REQUIRED",
            GATE_CONTRACT_PACK,
        )
        self.assertAllowed(
            gate_contract_pack(pack, now=self.now, purpose="confirm", approval_ref="AP-1"),
            "REVIEW_REQUIRED",
        )

    def test_confirm_on_a_clean_pack_needs_no_approval(self):
        self.assertAllowed(
            gate_contract_pack(self.pack(), now=self.now, purpose="confirm"), CODE_OK
        )

    def test_purpose_is_normalised_but_unknown_purposes_deny(self):
        self.assertAllowed(
            gate_contract_pack(self.pack(), now=self.now, purpose=" Quote "), CODE_OK
        )
        for value in ("SELL", "showing", "", None, 0, [], {}):
            with self.subTest(purpose=repr(value)):
                self.assertDenied(
                    gate_contract_pack(self.pack(), now=self.now, purpose=value),
                    "INVALID_PURPOSE",
                    GATE_CONTRACT_PACK,
                )

    def test_now_must_be_a_timezone_aware_iso8601_clock(self):
        self.assertAllowed(
            gate_contract_pack(self.pack(), now=self.NOW, purpose="quote"), CODE_OK
        )
        for value in ("soon", "", None, 0, datetime(2026, 9, 21, 12, 0), "2026-09-21", {}):
            with self.subTest(now=repr(value)):
                self.assertDenied(
                    gate_contract_pack(self.pack(), now=value, purpose="quote"),
                    "INVALID_NOW",
                    GATE_CONTRACT_PACK,
                )

    def test_contract_pack_gate_is_deterministic_for_the_same_object(self):
        pack = self.pack(items=[self.item(), self.item(item_id="C02")])
        first = gate_contract_pack(pack, now=self.now, purpose="quote")
        second = gate_contract_pack(pack, now=self.now, purpose="quote")
        self.assertEqual(first, second)
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.as_dict(), second.as_dict())


# ---------------------------------------------------------------------------
# Availability gate
# ---------------------------------------------------------------------------

class TestGateAvailability(GateTestCase):
    def test_each_permitted_level_allows_with_the_documented_window(self):
        derived = _doc_freshness()
        for level, (default, _guardrail) in sorted(derived.items()):
            if level == "D":
                continue
            source = LEVEL_SOURCE_TYPES[level][0]
            with self.subTest(level=level, source=source):
                decision = gate_availability(
                    self.evidence(level=level, source=source), now=self.now, purpose="quote"
                )
                self.assertAllowed(decision, CODE_OK)
                self.assertEqual(decision.gate, GATE_AVAILABILITY)
                self.assertEqual(decision.details["window_seconds"], default)
                self.assertEqual(decision.details["age_seconds"], 0)
                if level == "C":
                    self.assertTrue(decision.details["indicative"])
                else:
                    self.assertNotIn("indicative", decision.details)

    def test_level_c_evidence_is_marked_indicative(self):
        decision = gate_availability(
            self.evidence(level="C", source="ical"), now=self.now, purpose="quote"
        )
        self.assertAllowed(decision, CODE_OK)
        self.assertTrue(decision.details["indicative"])

    def test_level_d_never_allows(self):
        for purpose in PURPOSES:
            with self.subTest(purpose=purpose):
                decision = gate_availability(
                    self.evidence(level="D", source="memory"), now=self.now, purpose=purpose
                )
                self.assertDenied(decision, "EVIDENCE_LEVEL_TOO_WEAK", GATE_AVAILABILITY)

    def test_level_c_cannot_confirm_although_it_may_be_shown(self):
        show = gate_availability(self.evidence(level="C", source="ical"), now=self.now, purpose="show")
        self.assertAllowed(show, CODE_OK)
        confirm = gate_availability(
            self.evidence(level="C", source="ical"), now=self.now, purpose="confirm"
        )
        self.assertDenied(confirm, "EVIDENCE_LEVEL_TOO_WEAK", GATE_AVAILABILITY)
        self.assertEqual(confirm.details["allowed_levels"], list(_doc_confirm_levels()))

    def test_unknown_levels_deny(self):
        for value in ("E", "", " E ", None, 5, [], {}):
            with self.subTest(level=repr(value)):
                decision = gate_availability(
                    self.evidence(level=value), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "EVIDENCE_LEVEL_UNKNOWN", GATE_AVAILABILITY)

    def test_unknown_sources_deny(self):
        for value in ("pigeon", "", None, 5, []):
            with self.subTest(source=repr(value)):
                decision = gate_availability(
                    self.evidence(source=value), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "EVIDENCE_SOURCE_UNKNOWN", GATE_AVAILABILITY)

    def test_a_source_may_not_grant_a_level_it_does_not_grade(self):
        regrades = [
            ("A", "memory"),
            ("A", "ical"),
            ("B", "ical"),
            ("B", "property_api"),
            ("C", "property_api"),
            ("C", "manager_written"),
        ]
        for level, source in regrades:
            with self.subTest(level=level, source=source):
                decision = gate_availability(
                    self.evidence(level=level, source=source), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "EVIDENCE_SOURCE_LEVEL_MISMATCH", GATE_AVAILABILITY)

    def test_source_type_is_normalised_but_not_trusted(self):
        self.assertAllowed(
            gate_availability(
                self.evidence(level="A", source="PROPERTY_API"), now=self.now, purpose="quote"
            ),
            CODE_OK,
        )

    def test_not_available_units_are_denied(self):
        decision = gate_availability(
            self.evidence(result="NOT_AVAILABLE"), now=self.now, purpose="quote"
        )
        self.assertDenied(decision, "UNIT_OCCUPIED", GATE_AVAILABILITY)

    def test_unknown_results_never_allow(self):
        for value in ("UNKNOWN", "MAYBE", "", None, 5, {}, "available-ish"):
            with self.subTest(result=repr(value)):
                decision = gate_availability(
                    self.evidence(result=value), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "AVAILABILITY_UNKNOWN", GATE_AVAILABILITY)

    def test_result_is_normalised(self):
        self.assertAllowed(
            gate_availability(self.evidence(result="available"), now=self.now, purpose="quote"),
            CODE_OK,
        )

    def test_freshness_boundary_is_inclusive_per_level(self):
        derived = _doc_freshness()
        for level, (default, _guardrail) in sorted(derived.items()):
            if level == "D":
                continue
            source = LEVEL_SOURCE_TYPES[level][0]
            with self.subTest(level=level, at="window"):
                self.assertAllowed(
                    gate_availability(
                        self.evidence(level=level, source=source, captured_at=self.at(-default)),
                        now=self.now,
                        purpose="quote",
                    ),
                    CODE_OK,
                )
            with self.subTest(level=level, at="window+1s"):
                stale = gate_availability(
                    self.evidence(level=level, source=source, captured_at=self.at(-default - 1)),
                    now=self.now,
                    purpose="quote",
                )
                self.assertDenied(stale, "EVIDENCE_STALE", GATE_AVAILABILITY)
                self.assertEqual(stale.details["age_seconds"], default + 1)

    def test_a_caller_supplied_window_may_tighten_but_never_loosen(self):
        tightened = gate_availability(
            self.evidence(captured_at=self.at(-1800)), now=self.now, purpose="quote", max_age_hours="0.5"
        )
        self.assertAllowed(tightened, CODE_OK)
        self.assertEqual(tightened.details["window_seconds"], 1800)
        just_past = gate_availability(
            self.evidence(captured_at=self.at(-1801)), now=self.now, purpose="quote", max_age_hours="0.5"
        )
        self.assertDenied(just_past, "EVIDENCE_STALE", GATE_AVAILABILITY)
        for level, hours in (("A", 2), ("B", 49), ("C", 169)):
            with self.subTest(level=level, max_age_hours=hours):
                decision = gate_availability(
                    self.evidence(level=level, source=LEVEL_SOURCE_TYPES[level][0]),
                    now=self.now,
                    purpose="quote",
                    max_age_hours=hours,
                )
                self.assertDenied(decision, "FRESHNESS_WINDOW_TOO_LOOSE", GATE_AVAILABILITY)

    def test_a_caller_supplied_window_equal_to_the_guardrail_is_allowed(self):
        derived = _doc_freshness()
        for level, (_default, guardrail) in sorted(derived.items()):
            if level == "D":
                continue
            source = LEVEL_SOURCE_TYPES[level][0]
            hours = guardrail // 3600
            with self.subTest(level=level, hours=hours):
                self.assertAllowed(
                    gate_availability(
                        self.evidence(level=level, source=source, captured_at=self.at(-guardrail)),
                        now=self.now,
                        purpose="quote",
                        max_age_hours=hours,
                    ),
                    CODE_OK,
                )

    def test_invalid_windows_are_refused(self):
        for value in (-1, "nah", "", True, 1.5, float("nan"), [], {}):
            with self.subTest(max_age_hours=repr(value)):
                decision = gate_availability(
                    self.evidence(), now=self.now, purpose="quote", max_age_hours=value
                )
                self.assertDenied(decision, "INVALID_FRESHNESS_WINDOW", GATE_AVAILABILITY)

    def test_valid_until_expires_at_its_own_instant(self):
        expired = gate_availability(
            self.evidence(captured_at=self.at(-60), valid_until=self.now),
            now=self.now,
            purpose="quote",
        )
        self.assertDenied(expired, "EVIDENCE_EXPIRED", GATE_AVAILABILITY)
        live = gate_availability(
            self.evidence(captured_at=self.at(-60), valid_until=self.at(1)),
            now=self.now,
            purpose="quote",
        )
        self.assertAllowed(live, CODE_OK)

    def test_captured_at_and_valid_until_must_be_readable_and_ordered(self):
        for value in ("whenever", "2026-09-22T12:00:00", "", 0, {}, []):
            with self.subTest(valid_until=repr(value)):
                decision = gate_availability(
                    self.evidence(valid_until=value), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "EVIDENCE_TIMESTAMP_INVALID", GATE_AVAILABILITY)
        for value in ("whenever", "2026-09-21", "", None, 0, datetime(2026, 9, 21, 12, 0), {}):
            with self.subTest(captured_at=repr(value)):
                decision = gate_availability(
                    self.evidence(captured_at=value), now=self.now, purpose="quote"
                )
                self.assertDenied(decision, "EVIDENCE_TIMESTAMP_INVALID", GATE_AVAILABILITY)

    def test_absent_valid_until_means_no_expiry_bound(self):
        decision = gate_availability(
            self.evidence(captured_at=self.at(-60), valid_until=None), now=self.now, purpose="quote"
        )
        self.assertAllowed(decision, CODE_OK)
        # The same evidence with a bound that has already passed is refused.
        self.assertDenied(
            gate_availability(
                self.evidence(captured_at=self.at(-60), valid_until=self.at(-30)),
                now=self.now,
                purpose="quote",
            ),
            "EVIDENCE_EXPIRED",
            GATE_AVAILABILITY,
        )

    def test_replayed_evidence_from_the_future_is_refused(self):
        decision = gate_availability(
            self.evidence(captured_at=self.at(1)), now=self.now, purpose="quote"
        )
        self.assertDenied(decision, "EVIDENCE_FROM_FUTURE", GATE_AVAILABILITY)
        self.assertEqual(decision.details["skew_seconds"], -1)

    def test_evidence_captured_exactly_at_now_is_fresh(self):
        decision = gate_availability(
            self.evidence(captured_at=self.now), now=self.now, purpose="quote"
        )
        self.assertAllowed(decision, CODE_OK)
        self.assertEqual(decision.details["age_seconds"], 0)

    def test_capability_flags_are_enforced_per_purpose(self):
        cases = {
            "quote": ("can_quote", "EVIDENCE_CANNOT_QUOTE"),
            "hold": ("can_hold", "EVIDENCE_CANNOT_HOLD"),
            "confirm": ("can_confirm", "EVIDENCE_CANNOT_CONFIRM"),
        }
        for purpose, (flag, expected) in cases.items():
            for value in (0, False, "0", None, "no"):
                with self.subTest(purpose=purpose, flag=value):
                    decision = gate_availability(
                        self.evidence(**{flag: value}), now=self.now, purpose=purpose
                    )
                    self.assertDenied(decision, expected, GATE_AVAILABILITY)

    def test_missing_capability_flags_deny_the_buyer_facing_steps(self):
        base = {"evidence_id": "E-1", "evidence_level": "A", "source_type": "property_api", "captured_at": self.now, "result": "AVAILABLE"}
        self.assertDenied(
            gate_availability(dict(base), now=self.now, purpose="confirm"),
            "EVIDENCE_CANNOT_CONFIRM",
            GATE_AVAILABILITY,
        )
        self.assertDenied(
            gate_availability(dict(base), now=self.now, purpose="hold"),
            "EVIDENCE_CANNOT_HOLD",
            GATE_AVAILABILITY,
        )

    def test_capability_flags_cannot_outrank_the_level(self):
        decision = gate_availability(
            self.evidence(level="C", source="ical", can_confirm=True),
            now=self.now,
            purpose="confirm",
        )
        self.assertDenied(decision, "EVIDENCE_LEVEL_TOO_WEAK", GATE_AVAILABILITY)

    def test_dataclass_evidence_is_accepted(self):
        evidence = AvailabilityEvidence(
            evidence_id="E-1",
            unit_id="U-1",
            evidence_level="A",
            source_type="property_api",
            captured_at=self.now,
            result="AVAILABLE",
            can_quote=True,
            can_hold=True,
        )
        self.assertAllowed(gate_availability(evidence, now=self.now, purpose="quote"), CODE_OK)

    def test_purpose_and_clock_are_validated_first(self):
        for purpose in ("SELL", "", None, 0, []):
            with self.subTest(purpose=repr(purpose)):
                self.assertDenied(
                    gate_availability(self.evidence(), now=self.now, purpose=purpose),
                    "INVALID_PURPOSE",
                    GATE_AVAILABILITY,
                )
        for now in ("soon", 0, None, datetime(2026, 9, 21, 12, 0)):
            with self.subTest(now=repr(now)):
                self.assertDenied(
                    gate_availability(self.evidence(), now=now, purpose="quote"),
                    "INVALID_NOW",
                    GATE_AVAILABILITY,
                )

    def test_purpose_is_normalised(self):
        self.assertAllowed(
            gate_availability(self.evidence(), now=self.now, purpose="SHOW"), CODE_OK
        )

    def test_availability_gate_is_deterministic_for_the_same_object(self):
        evidence = self.evidence()
        first = gate_availability(evidence, now=self.now, purpose="quote")
        second = gate_availability(evidence, now=self.now, purpose="quote")
        self.assertEqual(first, second)
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.as_dict(), second.as_dict())


# ---------------------------------------------------------------------------
# Outbound classification gate + redaction
# ---------------------------------------------------------------------------

class TestGateOutbound(GateTestCase):
    def test_metadata_reaches_every_destination(self):
        for destination in DESTINATIONS:
            with self.subTest(destination=destination):
                decision = gate_outbound(
                    {"property_name": self.classified("property_name", "P0", "Villa A")},
                    destination=destination,
                )
                self.assertAllowed(decision, CODE_OK)
                self.assertEqual(decision.gate, GATE_CLASSIFICATION)

    def test_unknown_destinations_deny(self):
        for destination in ("twitter", "", None, 5, [], {}, object()):
            with self.subTest(destination=repr(destination)):
                decision = gate_outbound(
                    {"property_name": self.classified("property_name", "P0", "Villa A")},
                    destination=destination,
                )
                self.assertDenied(decision, "UNKNOWN_DESTINATION", GATE_CLASSIFICATION)

    def test_destination_is_normalised(self):
        self.assertAllowed(
            gate_outbound(
                {"property_name": self.classified("property_name", "P0", "Villa A")},
                destination=" STORE ",
            ),
            CODE_OK,
        )

    def test_an_empty_payload_denies(self):
        for fields in (None, "", [], {}, (), b""):
            with self.subTest(fields=repr(fields)):
                decision = gate_outbound(fields, destination="store")
                self.assertDenied(decision, "EMPTY_PAYLOAD", GATE_CLASSIFICATION)

    def test_an_unclassified_value_never_leaves(self):
        for fields in ({"x": "plain"}, {"x": 5}, {"x": {"nested": "plain"}}, "plain", 0, 1.5, object()):
            with self.subTest(fields=repr(fields)):
                decision = gate_outbound(fields, destination="store")
                self.assertDenied(decision, "UNKNOWN_CLASSIFICATION", GATE_CLASSIFICATION)

    def test_p4_forbidden_fields_never_leave_by_any_channel(self):
        credentials = {
            "password": self.classified("password", "P4", "hunter2"),
            "api_token": self.classified("api_token", "P4", "tok_live_9f2"),
            "card_cvc": self.classified("card_cvc", "P4", "123"),
        }
        for destination in DESTINATIONS:
            for name in credentials:
                with self.subTest(destination=destination, field=name):
                    decision = gate_outbound({name: credentials[name]}, destination=destination)
                    self.assertDenied(decision, "P4_PROHIBITED", GATE_CLASSIFICATION)

    def test_p3_banking_data_is_never_stored_outside_and_never_shared(self):
        field = {"owner_account": self.classified("owner_account", "P3", "1234567890")}
        self.assertAllowed(gate_outbound(field, destination="store"), CODE_OK)
        for destination in ("llm", "log", "audit", "email", "chat"):
            with self.subTest(destination=destination):
                decision = gate_outbound(dict(field), destination=destination)
                self.assertDenied(decision, "P3_FORBIDDEN", GATE_CLASSIFICATION)
        refused = gate_outbound(dict(field), destination="export")
        self.assertDenied(refused, "EXPORT_REQUIRES_ROLE", GATE_CLASSIFICATION)
        refused_manager = gate_outbound(dict(field), destination="export", actor_role="manager")
        self.assertDenied(refused_manager, "EXPORT_REQUIRES_ROLE", GATE_CLASSIFICATION)
        for role in P3_AUTH_ROLES:
            with self.subTest(role=role):
                no_reason = gate_outbound(
                    dict(field), destination="export", actor_role=role
                )
                self.assertDenied(no_reason, "EXPORT_REQUIRES_JUSTIFICATION", GATE_CLASSIFICATION)
                with_reason = gate_outbound(
                    dict(field),
                    destination="export",
                    actor_role=role,
                    justification="owner payout reconciliation 2026-09",
                )
                self.assertAllowed(with_reason, CODE_OK)

    def test_export_justification_must_be_recorded_text(self):
        field = {"owner_account": self.classified("owner_account", "P3", "1234567890")}
        for justification in ("", "   ", None, 0, [], {}):
            with self.subTest(justification=repr(justification)):
                decision = gate_outbound(
                    dict(field),
                    destination="export",
                    actor_role="admin",
                    justification=justification,
                )
                self.assertDenied(decision, "EXPORT_REQUIRES_JUSTIFICATION", GATE_CLASSIFICATION)

    def test_roles_are_normalised(self):
        field = {"owner_account": self.classified("owner_account", "P3", "1234567890")}
        self.assertAllowed(
            gate_outbound(dict(field), destination="export", actor_role=" Admin ", justification="audit"),
            CODE_OK,
        )

    def test_guest_pii_is_only_redacted_for_an_llm(self):
        for destination in DESTINATIONS:
            with self.subTest(destination=destination):
                decision = gate_outbound(
                    {"guest_name": self.classified("guest_name", "P2", "Budi")},
                    destination=destination,
                )
                if destination == "store":
                    self.assertAllowed(decision, CODE_OK)
                elif destination == "llm":
                    self.assertDenied(decision, "PII_REDACTION_REQUIRED", GATE_CLASSIFICATION)
                elif destination in LOGGING_DESTINATIONS:
                    self.assertDenied(decision, "CONTENT_LOGGING_FORBIDDEN", GATE_CLASSIFICATION)
                elif destination == "chat":
                    self.assertDenied(decision, "CHANNEL_FORBIDDEN", GATE_CLASSIFICATION)
                else:
                    self.assertDenied(decision, "EXPORT_REQUIRES_ROLE", GATE_CLASSIFICATION)

    def test_authorised_roles_may_export_guest_pii(self):
        field = {"guest_phone": self.classified("guest_phone", "P2", "+6281234567890")}
        for destination in ("email", "export"):
            for role in P2_AUTH_ROLES:
                with self.subTest(destination=destination, role=role):
                    self.assertAllowed(
                        gate_outbound(dict(field), destination=destination, actor_role=role),
                        CODE_OK,
                    )
            for role in ("agent", "ops", "intern", "", None, 5):
                with self.subTest(destination=destination, role=repr(role)):
                    self.assertDenied(
                        gate_outbound(dict(field), destination=destination, actor_role=role),
                        "EXPORT_REQUIRES_ROLE",
                        GATE_CLASSIFICATION,
                    )

    def test_p3_authority_is_a_strict_subset_of_p2_authority(self):
        self.assertTrue(set(P3_AUTH_ROLES) <= set(P2_AUTH_ROLES))
        for role in P3_AUTH_ROLES:
            with self.subTest(role=role):
                self.assertIn(role, _doc_p3_roles())

    def test_classification_is_normalised_and_unknown_classes_deny(self):
        self.assertDenied(
            gate_outbound(
                {"owner_account": self.classified("owner_account", " p3 ", "1234567890")},
                destination="llm",
            ),
            "P3_FORBIDDEN",
            GATE_CLASSIFICATION,
        )
        for classification in ("P5", "PII", "", None, 5, "p2x"):
            with self.subTest(classification=repr(classification)):
                self.assertDenied(
                    gate_outbound(
                        {"x": self.classified("x", classification, "Villa A")}, destination="store"
                    ),
                    "UNKNOWN_CLASSIFICATION",
                    GATE_CLASSIFICATION,
                )

    def test_metadata_carrying_a_direct_identifier_is_misclassified(self):
        for classification in ("P0", "P1"):
            for fields in (
                {"note": self.classified("note", classification, "wiwiek@example.com")},
                {"note": self.classified("note", classification, "+6281234567890")},
                {"note": self.classified("note", classification, "3273010101900001")},
                {"note": self.classified("note", classification, "4111111111111111")},
            ):
                with self.subTest(classification=classification, value=fields["note"].value):
                    decision = gate_outbound(dict(fields), destination="llm")
                    self.assertDenied(decision, "MISCLASSIFIED_CONTENT", GATE_CLASSIFICATION)
        # Storing metadata is where it belongs, identifiers or not.
        self.assertAllowed(
            gate_outbound(
                {"note": self.classified("note", "P0", "wiwiek@example.com")}, destination="store"
            ),
            CODE_OK,
        )

    def test_the_most_severe_violation_names_the_decision(self):
        payload = {
            "password": self.classified("password", "P4", "hunter2"),
            "owner_account": self.classified("owner_account", "P3", "1234567890"),
            "guest_phone": self.classified("guest_phone", "P2", "+6281234567890"),
            "mystery": self.classified("mystery", "", "x"),
        }
        for destination in DESTINATIONS:
            with self.subTest(destination=destination):
                decision = gate_outbound(payload, destination=destination)
                self.assertDenied(decision, "P4_PROHIBITED", GATE_CLASSIFICATION)
                self.assertGreaterEqual(len(decision.details["violations"]), 2)

    def test_p3_outranks_the_p2_logging_rule(self):
        payload = {
            "owner_account": self.classified("owner_account", "P3", "1234567890"),
            "guest_phone": self.classified("guest_phone", "P2", "+6281234567890"),
        }
        decision = gate_outbound(payload, destination="log")
        self.assertDenied(decision, "P3_FORBIDDEN", GATE_CLASSIFICATION)
        self.assertIn("CONTENT_LOGGING_FORBIDDEN", decision.details["violations"])

    def test_a_single_classified_field_and_a_sequence_are_accepted(self):
        single = gate_outbound(self.classified("note", "P0", "Villa A"), destination="store")
        self.assertAllowed(single, CODE_OK)
        sequence = gate_outbound([self.classified("note", "P0", "Villa A")], destination="store")
        self.assertAllowed(sequence, CODE_OK)

    def test_redaction_is_required_even_for_an_innocent_p2_value_on_an_llm_path(self):
        decision = gate_outbound(
            {"preference": self.classified("preference", "P2", "late check-in")}, destination="llm"
        )
        self.assertDenied(decision, "PII_REDACTION_REQUIRED", GATE_CLASSIFICATION)
        self.assertEqual(decision.details["field"], "preference")

    def test_the_redacted_flag_is_never_trusted(self):
        for value in ("wiwiek@example.com", "+6281234567890", "4111111111111111", "4111 1111 1111 1111", "3273010101900001"):
            with self.subTest(value=value):
                decision = gate_outbound(
                    {"guest": self.classified("guest", "P2", value, redacted=True)}, destination="llm"
                )
                self.assertDenied(decision, "REDACTION_NOT_APPLIED", GATE_CLASSIFICATION)

    def test_redaction_results_pass_the_gate(self):
        raw = {
            "guest_email": self.classified("guest_email", "P2", "wiwiek@example.com"),
            "guest_phone": self.classified("guest_phone", "P2", "+6281234567890"),
        }
        redacted = {field.name: field for field in apply_redaction(raw)}
        decision = gate_outbound(redacted, destination="llm")
        self.assertAllowed(decision, CODE_OK)
        for name, field in redacted.items():
            with self.subTest(field=name):
                self.assertNotIn("wiwiek@example.com", str(field.value))
                self.assertNotIn("+6281234567890", str(field.value))

    def test_an_unredactable_value_never_leaves(self):
        # A card-shaped run that fails Luhn is not masked by ``redact`` (amounts and
        # references must survive it), so the gate's own pattern still has to stop it.
        decision = gate_outbound(
            {field.name: field for field in apply_redaction({"card": self.classified("card", "P2", "4111 1111 1111 1112")})},
            destination="llm",
        )
        self.assertDenied(decision, "REDACTION_NOT_APPLIED", GATE_CLASSIFICATION)

    def test_a_grouped_card_number_is_now_redacted_before_it_leaves(self):
        # Audit 2026-11-13 (K1): ``redact`` masks a Luhn-valid card typed with spaces.
        fields = apply_redaction({"card": self.classified("card", "P2", "4111 1111 1111 1111")})
        self.assertNotIn("4111", fields[0].value)

    def test_a_non_string_p2_value_cannot_be_redacted(self):
        decision = gate_outbound(
            {"doc": self.classified("doc", "P2", {"guest": "wiwiek@example.com"}, redacted=True)},
            destination="llm",
        )
        self.assertDenied(decision, "REDACTION_NOT_APPLIED", GATE_CLASSIFICATION)

    def test_outbound_gate_is_deterministic_for_the_same_object(self):
        payload = {"guest_phone": self.classified("guest_phone", "P2", "+6281234567890")}
        first = gate_outbound(payload, destination="llm")
        second = gate_outbound(payload, destination="llm")
        self.assertEqual(first, second)
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.as_dict(), second.as_dict())


class TestApplyRedaction(GateTestCase):
    def test_apply_redaction_returns_classified_fields_and_keeps_the_class(self):
        outcome = apply_redaction({"guest_email": self.classified("guest_email", "P2", "wiwiek@example.com")})
        self.assertIsInstance(outcome, tuple)
        field = outcome[0]
        self.assertIsInstance(field, ClassifiedField)
        self.assertEqual(field.name, "guest_email")
        self.assertEqual(field.classification, "P2")
        self.assertTrue(field.redacted)

    def test_direct_identifiers_are_masked_out_of_the_value(self):
        for value in ("wiwiek@example.com", "+6281234567890"):
            with self.subTest(value=value):
                field = apply_redaction({"guest": self.classified("guest", "P2", value)})[0]
                self.assertNotIn(value, str(field.value))
                self.assertIn("[", str(field.value))

    def test_non_p2_values_are_left_alone(self):
        payload = {
            "property_name": self.classified("property_name", "P0", "Villa A"),
            "internal_note": self.classified("internal_note", "P1", "ops note"),
            "owner_account": self.classified("owner_account", "P3", "1234567890"),
            "password": self.classified("password", "P4", "hunter2"),
        }
        for field in apply_redaction(payload):
            with self.subTest(field=field.name):
                self.assertEqual(field.value, payload[field.name].value)
                self.assertFalse(field.redacted)

    def test_redaction_never_weakens_the_declared_classification(self):
        field = apply_redaction({"guest_email": self.classified("guest_email", "P2", "wiwiek@example.com")})[0]
        self.assertEqual(field.classification, "P2")

    def test_an_empty_payload_yields_no_fields(self):
        self.assertEqual(apply_redaction({}), ())
        self.assertEqual(apply_redaction([]), ())

    def test_identifier_patterns_cover_the_four_direct_identifier_classes(self):
        from gates import IDENTIFIER_PATTERNS

        names = {name for name, _pattern in IDENTIFIER_PATTERNS}
        self.assertEqual(names, {"email", "phone", "nik", "card"})
        for value in ("wiwiek@example.com", "+6281234567890", "3273010101900001", "4111111111111111"):
            with self.subTest(value=value):
                self.assertDenied(
                    gate_outbound(
                        {"guest": self.classified("guest", "P2", value, redacted=True)}, destination="llm"
                    ),
                    "REDACTION_NOT_APPLIED",
                    GATE_CLASSIFICATION,
                )


# ---------------------------------------------------------------------------
# Composite unit gate
# ---------------------------------------------------------------------------

class TestGateUnit(GateTestCase):
    def unit(self, **over: object) -> GateDecision:
        kwargs: dict[str, object] = {
            "now": self.now,
            "purpose": "quote",
            "unit_status": "ACTIVE",
            "pack": self.pack(),
            "evidence": self.evidence(),
        }
        kwargs.update(over)
        return gate_unit(**kwargs)

    def test_a_complete_unit_allows_on_every_purpose(self):
        for purpose in PURPOSES:
            with self.subTest(purpose=purpose):
                decision = self.unit(purpose=purpose)
                self.assertAllowed(decision, CODE_OK)
                self.assertEqual(decision.gate, GATE_UNIT)
                self.assertEqual(decision.details["contract_pack_code"], CODE_OK)
                self.assertEqual(decision.details["evidence_code"], CODE_OK)

    def test_active_status_is_normalised(self):
        for value in ("ACTIVE", "active", " Active "):
            with self.subTest(unit_status=value):
                self.assertAllowed(self.unit(unit_status=value), CODE_OK)

    def test_non_sellable_unit_status_denies_before_anything_else(self):
        for value, expected in (("MAINTENANCE", "UNIT_IN_MAINTENANCE"), ("SUSPENDED", "UNIT_SUSPENDED")):
            with self.subTest(unit_status=value):
                decision = self.unit(unit_status=value, pack=None, evidence=None)
                self.assertDenied(decision, expected, GATE_UNIT)
                self.assertEqual(decision.details["unit_status"], value)

    def test_unknown_unit_statuses_deny(self):
        for value in ("RETIRED", "ACTIVE_NOW", "", None, 5, [], {}, object(), "nan"):
            with self.subTest(unit_status=repr(value)):
                self.assertDenied(self.unit(unit_status=value), "UNIT_STATUS_UNKNOWN", GATE_UNIT)

    def test_a_denied_pack_propagates_its_own_gate_and_code(self):
        cases = {
            "missing pack": (None, "CONTRACT_PACK_MISSING", GATE_CONTRACT_PACK),
            "not sellable": (self.pack(property_status="NOT_SELLABLE"), "PROPERTY_NOT_SELLABLE", GATE_CONTRACT_PACK),
            "suspended": (self.pack(property_status="SUSPENDED"), "PROPERTY_SUSPENDED", GATE_CONTRACT_PACK),
            "missing item": (
                self.pack(items=[self.item(item_id="C01", status="MISSING")]),
                "CONTRACT_PACK_ITEM_MISSING",
                GATE_CONTRACT_PACK,
            ),
        }
        for label, (pack, expected, gate) in cases.items():
            with self.subTest(case=label):
                self.assertDenied(self.unit(pack=pack), expected, gate)

    def test_a_denied_evidence_propagates_its_own_gate_and_code(self):
        cases = {
            "missing evidence": (None, "EVIDENCE_MISSING", GATE_AVAILABILITY),
            "too weak": (self.evidence(level="D", source="memory"), "EVIDENCE_LEVEL_TOO_WEAK", GATE_AVAILABILITY),
            "not available": (
                self.evidence(result="NOT_AVAILABLE"),
                "UNIT_OCCUPIED",
                GATE_AVAILABILITY,
            ),
            "stale": (self.evidence(captured_at=self.at(-901)), "EVIDENCE_STALE", GATE_AVAILABILITY),
        }
        for label, (evidence, expected, gate) in cases.items():
            with self.subTest(case=label):
                self.assertDenied(self.unit(evidence=evidence), expected, gate)

    def test_a_reviewable_pack_allows_with_the_review_flag(self):
        decision = self.unit(
            pack=self.pack(items=[self.item(item_id="C12", status="MISSING", blocking=False)])
        )
        self.assertAllowed(decision, "REVIEW_REQUIRED")
        self.assertTrue(decision.review_required)
        self.assertEqual(decision.details["contract_pack_code"], "REVIEW_REQUIRED")

    def test_confirming_a_flagged_unit_needs_a_recorded_approval(self):
        pack = self.pack(property_status="SELLABLE_WITH_REVIEW")
        self.assertDenied(
            self.unit(pack=pack, purpose="confirm"), "APPROVAL_REQUIRED", GATE_CONTRACT_PACK
        )
        self.assertAllowed(
            self.unit(pack=pack, purpose="confirm", approval_ref="AP-1"), "REVIEW_REQUIRED"
        )

    def test_freshness_bounds_are_forwarded_to_both_sub_gates(self):
        self.assertDenied(
            self.unit(evidence=self.evidence(captured_at=self.at(-1801)), max_age_hours="0.5"),
            "EVIDENCE_STALE",
            GATE_AVAILABILITY,
        )
        self.assertDenied(
            self.unit(pack=self.pack(evaluated_at=self.at(-1)), max_pack_age_hours=0),
            "CONTRACT_PACK_EVALUATION_STALE",
            GATE_CONTRACT_PACK,
        )
        self.assertDenied(
            self.unit(max_age_hours=2), "FRESHNESS_WINDOW_TOO_LOOSE", GATE_AVAILABILITY
        )

    def test_purpose_and_clock_are_validated_on_the_composite_gate(self):
        for purpose in ("SELL", "", None, 0):
            with self.subTest(purpose=repr(purpose)):
                self.assertDenied(self.unit(purpose=purpose), "INVALID_PURPOSE", GATE_UNIT)
        for now in ("soon", 0, None, datetime(2026, 9, 21, 12, 0)):
            with self.subTest(now=repr(now)):
                self.assertDenied(self.unit(now=now), "INVALID_NOW", GATE_UNIT)

    def test_unit_gate_is_deterministic_for_the_same_objects(self):
        pack, evidence = self.pack(), self.evidence()
        first = self.unit(pack=pack, evidence=evidence)
        second = self.unit(pack=pack, evidence=evidence)
        self.assertEqual(first, second)
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.as_dict(), second.as_dict())


# ---------------------------------------------------------------------------
# Fail-closed behaviour on garbage input
# ---------------------------------------------------------------------------

GARBAGE = (
    ("None", None),
    ("empty string", ""),
    ("zero", 0),
    ("empty list", []),
    ("empty dict", {}),
    ("empty tuple", ()),
    ("empty bytes", b""),
    ("plain object", object()),
    ("nan string", "nan"),
    ("float", 1.5),
    ("int", 5),
)

# Verified denial codes for each gate's primary argument.
PACK_GARBAGE_CODES = {
    "None": "CONTRACT_PACK_MISSING",
    "empty string": "CONTRACT_PACK_MISSING",
    "zero": "CONTRACT_PACK_MISSING",
    "empty list": "CONTRACT_PACK_MISSING",
    "empty tuple": "CONTRACT_PACK_MISSING",
    "empty bytes": "CONTRACT_PACK_MISSING",
    "plain object": "CONTRACT_PACK_MISSING",
    "nan string": "CONTRACT_PACK_MISSING",
    "float": "CONTRACT_PACK_MISSING",
    "int": "CONTRACT_PACK_MISSING",
    "empty dict": "PROPERTY_STATUS_UNKNOWN",
}

EVIDENCE_GARBAGE_CODES = {
    "None": "EVIDENCE_MISSING",
    "empty string": "EVIDENCE_MISSING",
    "zero": "EVIDENCE_MISSING",
    "empty list": "EVIDENCE_MISSING",
    "empty tuple": "EVIDENCE_MISSING",
    "empty bytes": "EVIDENCE_MISSING",
    "plain object": "EVIDENCE_MISSING",
    "nan string": "EVIDENCE_MISSING",
    "float": "EVIDENCE_MISSING",
    "int": "EVIDENCE_MISSING",
    "empty dict": "EVIDENCE_LEVEL_UNKNOWN",
}

FIELDS_GARBAGE_CODES = {
    "None": "EMPTY_PAYLOAD",
    "empty string": "EMPTY_PAYLOAD",
    "empty list": "EMPTY_PAYLOAD",
    "empty tuple": "EMPTY_PAYLOAD",
    "empty bytes": "EMPTY_PAYLOAD",
    "empty dict": "EMPTY_PAYLOAD",
    "zero": "UNKNOWN_CLASSIFICATION",
    "plain object": "UNKNOWN_CLASSIFICATION",
    "nan string": "UNKNOWN_CLASSIFICATION",
    "float": "UNKNOWN_CLASSIFICATION",
    "int": "UNKNOWN_CLASSIFICATION",
}

UNIT_STATUS_GARBAGE_CODES = {
    "None": "UNIT_STATUS_UNKNOWN",
    "empty string": "UNIT_STATUS_UNKNOWN",
    "zero": "UNIT_STATUS_UNKNOWN",
    "empty list": "UNIT_STATUS_UNKNOWN",
    "empty tuple": "UNIT_STATUS_UNKNOWN",
    "empty bytes": "UNIT_STATUS_UNKNOWN",
    "plain object": "UNIT_STATUS_UNKNOWN",
    "nan string": "UNIT_STATUS_UNKNOWN",
    "float": "UNIT_STATUS_UNKNOWN",
    "int": "UNIT_STATUS_UNKNOWN",
    "empty dict": "UNIT_STATUS_UNKNOWN",
}


class TestFailClosed(GateTestCase):
    """Unknown, missing or malformed input must DENY — never silently allow."""

    def assertDenial(self, decision: GateDecision, label: str, expected: str | None = None) -> None:
        self.assertFalse(decision.allowed, msg=f"{label} was allowed: {decision}")
        self.assertNotEqual(decision.code, CODE_OK, msg=f"{label} produced the OK code")
        self.assertTrue(decision.code, msg=f"{label} produced an empty reason code")
        self.assertIsInstance(decision.code, str)
        if expected is not None:
            self.assertEqual(decision.code, expected, msg=f"{label} used the wrong denial code")

    def test_gate_contract_pack_garbage_denies(self):
        for label, value in GARBAGE:
            with self.subTest(invalid=label):
                decision = gate_contract_pack(value, now=self.now, purpose="quote")
                self.assertDenial(decision, f"pack={label}", PACK_GARBAGE_CODES[label])

    def test_gate_availability_garbage_denies(self):
        for label, value in GARBAGE:
            with self.subTest(invalid=label):
                decision = gate_availability(value, now=self.now, purpose="quote")
                self.assertDenial(decision, f"evidence={label}", EVIDENCE_GARBAGE_CODES[label])

    def test_gate_outbound_garbage_denies(self):
        for label, value in GARBAGE:
            with self.subTest(invalid=label):
                decision = gate_outbound(value, destination="store")
                self.assertDenial(decision, f"fields={label}", FIELDS_GARBAGE_CODES[label])

    def test_gate_unit_garbage_unit_status_denies(self):
        for label, value in GARBAGE:
            with self.subTest(invalid=label):
                decision = gate_unit(
                    now=self.now, purpose="quote", unit_status=value, pack=self.pack(), evidence=self.evidence()
                )
                self.assertDenial(decision, f"unit_status={label}", UNIT_STATUS_GARBAGE_CODES[label])

    def test_every_gate_denies_a_wholly_empty_call(self):
        decisions = [
            ("contract_pack", gate_contract_pack(None, now=self.now, purpose="quote")),
            ("availability", gate_availability(None, now=self.now, purpose="quote")),
            ("outbound", gate_outbound(None, destination="store")),
            ("unit", gate_unit(now=self.now, purpose="quote", unit_status=None)),
        ]
        for label, decision in decisions:
            with self.subTest(gate=label):
                self.assertDenial(decision, label)

    def test_unknown_strings_are_never_coerced_into_an_allow(self):
        unknown_property = gate_contract_pack(
            self.pack(property_status="yes"), now=self.now, purpose="quote"
        )
        self.assertDenial(unknown_property, "property_status=yes", "PROPERTY_STATUS_UNKNOWN")
        unknown_pack = gate_contract_pack(
            self.pack(pack_status="activated?"), now=self.now, purpose="quote"
        )
        self.assertDenial(unknown_pack, "pack_status=activated?", "CONTRACT_PACK_STATUS_UNKNOWN")
        unknown_item = gate_contract_pack(
            self.pack(items=[self.item(status="ok")]), now=self.now, purpose="quote"
        )
        self.assertDenial(unknown_item, "item status=ok", "CONTRACT_PACK_ITEM_INVALID")
        unknown_level = gate_availability(
            self.evidence(level="trusted"), now=self.now, purpose="quote"
        )
        self.assertDenial(unknown_level, "level=trusted", "EVIDENCE_LEVEL_UNKNOWN")
        unknown_result = gate_availability(
            self.evidence(result="maybe"), now=self.now, purpose="quote"
        )
        self.assertDenial(unknown_result, "result=maybe", "AVAILABILITY_UNKNOWN")
        unknown_destination = gate_outbound(
            {"x": self.classified("x", "P0", "Villa A")}, destination="whatsapp"
        )
        self.assertDenial(unknown_destination, "destination=whatsapp", "UNKNOWN_DESTINATION")
        unknown_class = gate_outbound(
            {"x": self.classified("x", "public", "Villa A")}, destination="store"
        )
        self.assertDenial(unknown_class, "classification=public", "UNKNOWN_CLASSIFICATION")

    def test_a_malformed_item_container_denies_instead_of_raising(self):
        for items in (5, object(), 1.5, True):
            with self.subTest(items=repr(items)):
                decision = gate_contract_pack(
                    self.pack(items=items), now=self.now, purpose="quote"
                )
                self.assertDenial(decision, f"items={items!r}", "CONTRACT_PACK_EMPTY")

    def test_malformed_item_entries_deny_instead_of_raising(self):
        for items, expected in (
            ("abc", "CONTRACT_PACK_EMPTY"),
            ([None], "CONTRACT_PACK_EMPTY"),
            ([5], "CONTRACT_PACK_EMPTY"),
            ({"a": 1}, "CONTRACT_PACK_EMPTY"),
            # An entry with no usable item_id cannot satisfy a blocking item.
            ([{}], "CONTRACT_PACK_ITEM_MISSING"),
        ):
            with self.subTest(items=repr(items)):
                decision = gate_contract_pack(
                    self.pack(items=items), now=self.now, purpose="quote"
                )
                self.assertDenial(decision, f"items={items!r}", expected)

    def test_a_mapping_with_garbage_fields_denies_instead_of_raising(self):
        for pack in (
            {"items": [{"item_id": "C01", "status": "VALID", "blocking": True}]},
            {"items": [{"item_id": "C01", "status": "VALID", "blocking": True, "next_review_at": {"x": 1}}]},
        ):
            with self.subTest(pack=pack):
                decision = gate_contract_pack(pack, now=self.now, purpose="quote")
                self.assertDenial(decision, f"pack={pack}")
        for evidence in (
            {"evidence_level": "A", "source_type": "property_api", "captured_at": {"x": 1}},
            {"evidence_level": ["A"], "source_type": "property_api", "captured_at": self.now},
        ):
            with self.subTest(evidence=evidence):
                decision = gate_availability(evidence, now=self.now, purpose="quote")
                self.assertDenial(decision, f"evidence={evidence}")


# ---------------------------------------------------------------------------
# Decision object and require_allow
# ---------------------------------------------------------------------------

class TestDecisionObject(GateTestCase):
    def test_decision_requires_a_bool_verdict_a_code_and_mapping_details(self):
        with self.assertRaises(DomainError) as caught:
            GateDecision(allowed="yes", code="OK")  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "INVALID_FIELD")
        with self.assertRaises(DomainError):
            GateDecision(allowed=True, code="")
        with self.assertRaises(DomainError):
            GateDecision(allowed=True, code="OK", details=["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_decision_truthiness_follows_allowed(self):
        allowed = gate_contract_pack(self.pack(), now=self.now, purpose="quote")
        denied = gate_contract_pack(None, now=self.now, purpose="quote")
        self.assertTrue(bool(allowed))
        self.assertFalse(bool(denied))
        self.assertIn("ALLOW", str(allowed))
        self.assertIn("DENY", str(denied))

    def test_as_dict_is_a_copy_and_covers_every_field(self):
        decision = gate_contract_pack(self.pack(), now=self.now, purpose="quote")
        payload = decision.as_dict()
        self.assertEqual(
            sorted(payload), ["allowed", "code", "details", "gate", "message", "review_required"]
        )
        payload["details"]["property_id"] = "tampered"
        self.assertEqual(decision.details["property_id"], "P-1")

    def test_every_gate_stamps_its_own_name(self):
        self.assertEqual(
            gate_contract_pack(self.pack(), now=self.now, purpose="quote").gate, GATE_CONTRACT_PACK
        )
        self.assertEqual(
            gate_availability(self.evidence(), now=self.now, purpose="quote").gate, GATE_AVAILABILITY
        )
        self.assertEqual(
            gate_outbound({"x": self.classified("x", "P0", "Villa A")}, destination="store").gate,
            GATE_CLASSIFICATION,
        )
        self.assertEqual(
            gate_unit(
                now=self.now, purpose="quote", unit_status="ACTIVE", pack=self.pack(), evidence=self.evidence()
            ).gate,
            GATE_UNIT,
        )

    def test_require_allow_passes_an_allowed_decision_through(self):
        decision = gate_contract_pack(self.pack(), now=self.now, purpose="quote")
        self.assertIs(require_allow(decision), decision)

    def test_require_allow_raises_the_specific_denial_code(self):
        denials = [
            (gate_contract_pack(None, now=self.now, purpose="quote"), "CONTRACT_PACK_MISSING"),
            (
                gate_availability(
                    self.evidence(result="NOT_AVAILABLE"), now=self.now, purpose="quote"
                ),
                "UNIT_OCCUPIED",
            ),
            (
                gate_outbound(
                    {"password": self.classified("password", "P4", "hunter2")}, destination="store"
                ),
                "P4_PROHIBITED",
            ),
            (
                gate_unit(now=self.now, purpose="quote", unit_status="SUSPENDED"),
                "UNIT_SUSPENDED",
            ),
        ]
        for decision, expected in denials:
            with self.subTest(code=expected):
                with self.assertRaises(GateDenied) as caught:
                    require_allow(decision)
                self.assertEqual(caught.exception.code, expected)
                self.assertIsInstance(caught.exception, DomainError)
                self.assertIs(caught.exception.decision, decision)
                self.assertFalse(caught.exception.decision.allowed)
                self.assertTrue(caught.exception.message)

    def test_require_allow_rejects_anything_that_is_not_a_decision(self):
        for value in (None, "OK", 0, {}, [], True, object()):
            with self.subTest(value=repr(value)):
                with self.assertRaises(DomainError) as caught:
                    require_allow(value)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "INVALID_FIELD")

    def test_contract_pack_dataclass_coercion_matches_the_row_shape(self):
        row = self.pack(items=[self.item(status="VALID", blocking=True)])
        pack = ContractPack.from_row(row)
        self.assertIsInstance(pack, ContractPack)
        self.assertEqual(pack.property_id, "P-1")
        self.assertEqual(pack.property_status, "SELLABLE")
        self.assertEqual(len(pack.items), 1)
        self.assertIsInstance(pack.items[0], ContractPackItem)
        self.assertEqual(pack.items[0].item_id, "C01")
        self.assertTrue(pack.items[0].blocking)

    def test_contract_pack_item_defaults_to_blocking(self):
        self.assertTrue(ContractPackItem(item_id="C01", status="VALID").blocking)

    def test_evidence_dataclass_direct_construction_is_gated_the_same_way(self):
        evidence = AvailabilityEvidence(
            evidence_id="E-1",
            evidence_level="D",
            source_type="memory",
            captured_at=self.now,
            result="AVAILABLE",
        )
        self.assertDenied(
            gate_availability(evidence, now=self.now, purpose="quote"),
            "EVIDENCE_LEVEL_TOO_WEAK",
            GATE_AVAILABILITY,
        )

    def test_decisions_are_reproducible_across_fresh_equal_inputs(self):
        def build() -> tuple[dict[str, object], dict[str, object]]:
            return (
                {
                    "property_id": "P-1",
                    "contract_pack_status": "ACTIVATED",
                    "property_status": "SELLABLE",
                    "items": [{"item_id": "C01", "status": "VALID", "blocking": True, "next_review_at": None}],
                },
                {
                    "evidence_id": "E-1",
                    "evidence_level": "A",
                    "source_type": "property_api",
                    "captured_at": self.now,
                    "result": "AVAILABLE",
                    "can_quote": True,
                },
            )

        first_pack, first_evidence = build()
        second_pack, second_evidence = build()
        self.assertEqual(
            gate_unit(now=self.now, purpose="quote", unit_status="ACTIVE", pack=first_pack, evidence=first_evidence),
            gate_unit(now=self.now, purpose="quote", unit_status="ACTIVE", pack=second_pack, evidence=second_evidence),
        )


class TestIdentifierPatternsNeedATokenBoundary(unittest.TestCase):
    """ADR-0062: a digit run *inside* an opaque token is not a direct identifier.

    ``IdFactory`` mints ``prefix_`` + ``secrets.token_hex(8)``. About 0.50% of those
    16 hex characters begin with 13 decimal digits and about 0.10% carry a
    phone-shaped run, and the unanchored ``card``/``phone`` patterns read both as
    identifiers: the confirmation that carried the booking code was refused. The
    patterns now require the run to stand alone. ``redact()`` keeps its broad
    masking (ADR-0027), so nothing it can mask escapes the first detector.
    """

    #: Two ids captured from the failing run, plus siblings and the id shapes the
    #: factory can mint. Each was refused by ``card`` or ``phone`` before the fix.
    OPQ_IDS = (
        "book_7312855452251a0f",
        "book_3f5622888338672e",
        "book_8ad5b087340350a1",
        "book_028e0837814326b1",
        "inv_1234567890123456",
        "hold_0812345678901234",
        "unit_1",
    )

    def test_an_id_token_is_never_read_as_an_identifier(self):
        from gates import IDENTIFIER_PATTERNS

        for token in self.OPQ_IDS:
            for name, pattern in IDENTIFIER_PATTERNS:
                with self.subTest(token=token, pattern=name):
                    self.assertIsNone(
                        pattern.search(token), f"{name} read the id token {token!r}"
                    )

    def test_a_standalone_card_number_is_still_caught(self):
        from gates import IDENTIFIER_PATTERNS

        card = dict(IDENTIFIER_PATTERNS)["card"]
        for value in (
            "4111 1111 1111 1111",
            "4111-1111-1111-1111",
            "4111111111111111",
            "IDR 4111 1111 1111 1111 1111",
            "Nomor kartu: 4111 1111 1111 1111.",
        ):
            with self.subTest(value=value):
                self.assertIsNotNone(card.search(value), f"a standalone card was missed: {value!r}")

    def test_a_standalone_phone_number_is_still_caught(self):
        from gates import IDENTIFIER_PATTERNS

        phone = dict(IDENTIFIER_PATTERNS)["phone"]
        for value in ("081234567890", "+6281234567890", "wa 0812345678901", "hub.08123456789"):
            with self.subTest(value=value):
                self.assertIsNotNone(phone.search(value), f"a standalone phone was missed: {value!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

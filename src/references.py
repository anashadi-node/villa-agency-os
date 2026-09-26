"""Human-readable references (Master Spec v1.1 Appendix B).

`INQ-YYYY-NNNNN`, `QT-YYYY-NNNNN`, `RSV-YYYY-NNNNN`, `INV-YYYY-NNNNN`, `PR-YYYY-NNNNN`,
`INC-YYYY-NNNNN`, `APR-YYYY-NNNNN`.

Two rules the spec states outright and this module keeps:

* the internal primary key stays opaque and separate — **a reference is not an
  authorization boundary**, so nothing may look it up to decide access;
* numbering is per agency and per year, because each agency's incoming paperwork is
  its own (the same reference in two agencies is two different things).

Allocation is a read-then-insert, which is only safe because the column carries a unique
index: a losing writer gets `IntegrityError` and retries rather than handing out a
duplicate. The retry is bounded so a persistent conflict fails loudly.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from primitives import DomainError, require_ref

#: Canonical prefixes, exactly as locked in Appendix B.
REFERENCE_PREFIXES: Mapping[str, str] = {
    "INQUIRY": "INQ",
    "QUOTE": "QT",
    "RESERVATION": "RSV",
    "INVOICE": "INV",
    "PAYMENT_REQUEST": "PR",
    "INCIDENT": "INC",
    "APPROVAL": "APR",
}

#: Appendix B spells the counter as exactly five digits; past that the number simply
#: gets longer, because silently wrapping would collide with an existing reference.
COUNTER_WIDTH = 5
MAX_ATTEMPTS = 8


class ReferenceError(DomainError):
    """The reference could not be allocated."""


class UnknownReferenceKind(ReferenceError):
    """The requested kind has no canonical prefix."""


def _prefix(kind: str) -> str:
    if not isinstance(kind, str) or kind.upper() not in REFERENCE_PREFIXES:
        raise UnknownReferenceKind(
            f"no canonical reference prefix for kind {kind!r}",
            code="VALIDATION_ERROR", kind=str(kind),
            known=sorted(REFERENCE_PREFIXES),
        )
    return REFERENCE_PREFIXES[kind.upper()]


def format_reference(kind: str, year: int, counter: int) -> str:
    """`INQ-2026-00001` — the exact shape Appendix B locks."""
    if not isinstance(year, int) or year < 2000 or year > 9999:
        raise ReferenceError(f"year out of range: {year!r}", code="VALIDATION_ERROR")
    if not isinstance(counter, int) or counter < 1:
        raise ReferenceError(f"counter must be >= 1, got {counter!r}", code="VALIDATION_ERROR")
    return f"{_prefix(kind)}-{year:04d}-{counter:0{COUNTER_WIDTH}d}"


def parse_reference(reference: Any) -> tuple[str, int, int]:
    """Return `(prefix, year, counter)`. Rejects anything that is not Appendix B shape."""
    if not isinstance(reference, str) or len(reference) > 40:
        raise ReferenceError("reference is not a string", code="VALIDATION_ERROR")
    parts = reference.split("-")
    if len(parts) != 3:
        raise ReferenceError(f"reference must have three segments: {reference!r}",
                             code="VALIDATION_ERROR")
    prefix, year_text, counter_text = parts
    if prefix not in REFERENCE_PREFIXES.values():
        raise ReferenceError(f"unknown reference prefix: {prefix!r}", code="VALIDATION_ERROR")
    if not (year_text.isdigit() and len(year_text) == 4):
        raise ReferenceError(f"reference year must be four digits: {reference!r}",
                             code="VALIDATION_ERROR")
    if not (counter_text.isdigit() and len(counter_text) >= COUNTER_WIDTH):
        raise ReferenceError(f"reference counter must be at least five digits: {reference!r}",
                             code="VALIDATION_ERROR")
    return prefix, int(year_text), int(counter_text)


class ReferenceAllocator:
    """Allocates the next reference for one agency, one kind, one year at a time."""

    def __init__(self, conn: Any, *, agency_id: str, table: str = "cases",
                 column: str = "reference") -> None:
        self.conn = conn
        self.agency_id = require_ref(agency_id, "agency_id")
        self.table = table
        self.column = column

    def allocate(self, kind: str, *, year: int, on_allocate: Any = None) -> str:
        """Return the next free reference, optionally inserting it in the same breath.

        Probing is not reservation: another writer can take the number between the
        probe and your `INSERT`. So when `on_allocate` is supplied the whole
        probe-then-insert is retried on `IntegrityError`, and the unique index — not
        this method — is what actually makes duplicates impossible.
        """
        prefix = _prefix(kind)
        last_error: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            candidate = format_reference(kind, year, self._next_counter(prefix, year))
            if not self._is_free(candidate):
                continue
            if on_allocate is None:
                return candidate
            try:
                on_allocate(candidate)
            except sqlite3.IntegrityError as exc:  # pragma: no cover - needs a racing writer
                last_error = exc
                continue
            return candidate
        raise ReferenceError(
            "could not allocate a reference without colliding",
            code="CONCURRENT_MODIFICATION", prefix=prefix, year=year,
            attempts=MAX_ATTEMPTS, last_error=str(last_error),
        )

    def _next_counter(self, prefix: str, year: int) -> int:
        head = f"{prefix}-{year:04d}-"
        row = self.conn.execute(
            f"SELECT MAX(CAST(substr({self.column}, ?) AS INTEGER)) AS high"
            f" FROM {self.table} WHERE agency_id = ? AND {self.column} LIKE ?",
            (len(head) + 1, self.agency_id, f"{head}%"),
        ).fetchone()
        # An empty scope starts at 1; the counter is never 0 (Appendix B has no
        # `INQ-2026-00000`).
        return 1 if row is None or row["high"] is None else int(row["high"]) + 1

    def _is_free(self, reference: str) -> bool:
        """Probe one candidate against the unique index. Writes nothing."""
        row = self.conn.execute(
            f"SELECT 1 AS taken FROM {self.table} WHERE agency_id = ? AND {self.column} = ?",
            (self.agency_id, reference),
        ).fetchone()
        return row is None

    def is_taken(self, reference: str) -> bool:
        require_ref(reference, "reference", max_len=40)
        parse_reference(reference)
        return not self._is_free(reference)

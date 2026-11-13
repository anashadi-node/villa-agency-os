#!/usr/bin/env python3
"""Count stored rows that still carry P3/P4 values in free text — read-only.

Audit 2026-11-13, finding K1: before the redactor learned card numbers, security
codes, passport numbers and separated phone numbers, a guest could type them into
an inquiry and they were stored verbatim. The redactor now masks them on the way
in; this tool answers the remaining question — *is anything already stored?*

It never prints a value: only table, column and a row count. It never writes:
the journals it reads are append-only by trigger, so cleaning a row is an owner
decision (a documented migration with approval and audit), not something a
scanner may do. Exit codes: 0 = nothing found, 1 = rows found, 2 = usage problem.

    python3 scripts/scan_stored_pii.py --db data/villa.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from primitives import mask_sensitive_numbers  # noqa: E402

#: Free-text columns that hold what guests and owners typed or what was derived from it.
TARGETS: tuple[tuple[str, str], ...] = (
    ("case_messages", "body_redacted"),
    ("intake_fields", "field_value"),
    ("message_drafts", "body_text"),
    ("ai_conversation_events", "reason"),
    ("audit_events", "reason"),
    ("audit_events", "before_state"),
    ("audit_events", "after_state"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def scan(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    findings: list[tuple[str, str, int]] = []
    for table, column in TARGETS:
        if column not in _columns(conn, table):
            continue
        hits = 0
        for (value,) in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
            text = str(value)
            if mask_sensitive_numbers(text) != text:
                hits += 1
        findings.append((table, column, hits))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="SQLite database to scan (opened read-only)")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        print(f"no such database: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        findings = scan(conn)
    finally:
        conn.close()
    total = 0
    for table, column, hits in findings:
        total += hits
        print(f"{'FOUND' if hits else 'OK   '}  {table}.{column}: {hits} row(s)")
    print(f"STORED-PII-SCAN: {'FAIL' if total else 'PASS'} ({total} row(s) carry P3/P4 text)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())

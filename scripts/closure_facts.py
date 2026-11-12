#!/usr/bin/env python3
"""Record the §30 closure facts — the tenant staff's manual act, without a code change.

§30 refuses to close a booking until seven conditions hold. Two of them are *recorded
decisions*, not computed ones: what became of the security deposit (ADR-0055) and what the
agency owes — and has paid — the supplier (ADR-0056). Those facts are written by people, with
evidence, and this tool is how they get there:

    status --booking B        the §30 gate for one booking: every check, and what still blocks
    deposit --booking B       one deposit resolution (RETURNED / FORFEITED / APPLIED)
    settlement --booking B    one settlement row (DUE = what the supplier is owed, PAID = a part)

Three rules are deliberate (the same ones ``publish_policy_value.py`` carries):

* a write requires ``VILLA_DB`` to be named: a fact that lands silently in whatever
  ``data/villa.db`` sits in the working directory is a decision nobody made;
* the tool never defaults a fact — the store refuses an act with no evidence ref, no human
  actor, no idempotency key, and the tool refuses one without them anyway;
* exit codes: 0 = recorded/shown, 1 = the store refused the write (nothing changed),
  2 = usage or configuration problem.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import booking_closure as CLOSURE  # noqa: E402
import deposit_resolutions as DEPOSIT  # noqa: E402
import supplier_settlements as SETTLE  # noqa: E402
from primitives import DomainError, IdFactory, SystemClock  # noqa: E402

DEFAULT_DB = Path("data/villa.db")
DEFAULT_AGENCY = "agency_default"


def _db_path() -> Path:
    # An empty VILLA_DB must mean "unset": Path("") is ".", so a stray empty variable would
    # silently point every read at the working directory instead of the default database.
    return Path(os.environ.get("VILLA_DB", "").strip() or DEFAULT_DB)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _service(conn: sqlite3.Connection, agency_id: str, cls: Any) -> Any:
    return cls(
        conn,
        clock=SystemClock(),
        ids=IdFactory(),
        agency_id=agency_id,
    )


def _record(conn: sqlite3.Connection, agency_id: str, args: argparse.Namespace) -> int:
    cls = (
        DEPOSIT.DepositResolutionService
        if args.command == "deposit"
        else SETTLE.SupplierSettlementService
    )
    service = _service(conn, agency_id, cls)
    try:
        row = service.record(
            booking_id=args.booking,
            action=args.action,
            amount_minor=args.amount,
            currency=args.currency,
            decided_by=args.decided_by,
            actor_role=args.actor_role,
            evidence_ref=args.evidence_ref,
            idempotency_key=args.idempotency_key,
            note=args.note,
        )
    except DomainError as exc:
        print(f"REFUSED {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    label = "DEPOSIT_RESOLUTION" if args.command == "deposit" else "SUPPLIER_SETTLEMENT"
    print(
        f"RECORDED {label} {row.get('deposit_resolution_id') or row.get('supplier_settlement_id')}\n"
        f"  booking : {args.booking}\n"
        f"  action  : {row['action']} {row['amount_minor']} minor ({row['currency']})\n"
        f"  by      : {row['decided_by']} ({row['actor_role']})\n"
        f"  evidence: {row['evidence_ref']}"
    )
    return 0


def _status(conn: sqlite3.Connection, agency_id: str, args: argparse.Namespace) -> int:
    try:
        verdict = CLOSURE.evaluate(conn, booking_id=args.booking, agency_id=agency_id)
    except CLOSURE.DomainError as exc:
        print(f"REFUSED {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(f"§30 CLOSURE GATE {args.booking} — {'READY' if verdict.ready else 'BLOCKED'}")
    for check in verdict.checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name:<26} {check.detail}")
    if verdict.blockers:
        print(f"  blockers: {', '.join(verdict.blockers)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("status", "deposit", "settlement"))
    parser.add_argument("--booking", required=True, help="the booking the fact belongs to")
    parser.add_argument(
        "--action",
        help="deposit: RETURNED | FORFEITED | APPLIED — settlement: DUE | PAID",
    )
    parser.add_argument("--amount", type=int, help="positive integer, minor units")
    parser.add_argument("--currency", help="the booking's own currency (ISO-4217, 3 letters)")
    parser.add_argument("--decided-by", help="the person deciding this (required to write)")
    parser.add_argument("--actor-role", help="that person's role (required to write)")
    parser.add_argument(
        "--evidence-ref",
        help="where the proof lives — receipt, transfer ref, contract (required to write)",
    )
    parser.add_argument(
        "--idempotency-key",
        help="a key a retry repeats: same key = same fact, never a second one",
    )
    parser.add_argument("--note", default=None, help="optional note (no guest text, no P3/P4)")
    parser.add_argument("--agency", default=DEFAULT_AGENCY, help="agency scope")
    args = parser.parse_args(argv)

    # A write names its target before anything is looked up. The existence check used to
    # answer first on a checkout without data/villa.db, so the operator saw "database not
    # found" while the CLI had already fallen back to a default path a write must not guess.
    if args.command != "status" and not os.environ.get("VILLA_DB", "").strip():
        print(
            "VILLA_DB must be set explicitly for a write: a §30 fact that lands silently in "
            "whatever data/villa.db happens to sit in the working directory is a decision "
            "nobody made",
            file=sys.stderr,
        )
        return 2

    db_path = _db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    if args.command == "status":
        return _status(_connect(db_path), args.agency, args)

    missing = [
        flag
        for flag, given in (
            ("--action", args.action),
            ("--amount", args.amount),
            ("--currency", args.currency),
            ("--decided-by", args.decided_by),
            ("--actor-role", args.actor_role),
            ("--evidence-ref", args.evidence_ref),
            ("--idempotency-key", args.idempotency_key),
        )
        if given is None or (isinstance(given, str) and not given.strip())
    ]
    if missing:
        print(
            f"{', '.join(missing)} required for `{args.command}`: a fact with no evidence, no "
            "human actor and no idempotency key is not a recordable decision (the store "
            "refuses it anyway)",
            file=sys.stderr,
        )
        return 2
    if args.amount is not None and args.amount <= 0:
        print("--amount must be a positive integer in minor units", file=sys.stderr)
        return 2
    conn = _connect(db_path)
    try:
        return _record(conn, args.agency, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""Show Agent 3's reservation KPIs for one agency — the read an operator runs.

    python3 scripts/reservation_kpi.py                          # last 28 days, default DB
    python3 scripts/reservation_kpi.py --days 7 --agency ag_x   # any window, any tenant

The numbers are counts over recorded facts (charter §5): the funnel, the two SLA breach
counts, the double-booking detector, and the AI governance pair. No person data, no
message text — a KPI bundle contains integers and ratios, so reading it needs no gate.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import reservation_kpi as KPI  # noqa: E402
from primitives import DEFAULT_AGENCY_ID  # noqa: E402

DEFAULT_DB = Path("data/villa.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency", default=DEFAULT_AGENCY_ID, help="agency scope")
    parser.add_argument("--days", type=int, default=28, help="window length in days")
    parser.add_argument(
        "--db",
        default=None,
        help="database file (default: VILLA_DB, then data/villa.db)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db or os.environ.get("VILLA_DB", "").strip() or DEFAULT_DB)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    if args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        k = KPI.collect(conn, agency_id=args.agency, window_days=args.days)
    finally:
        conn.close()

    print(f"RESERVATION KPIs — {args.agency} — last {args.days} days")
    print("  funnel :")
    for stage, value in k.funnel.items():
        print(f"    {stage:<20} {value}")
    print("  rates  :")
    for name, value in k.rates.items():
        print(f"    {name:<20} {'n/a' if value is None else format(value, '.4f')}")
    print("  sla    :")
    for name, value in k.sla.items():
        print(f"    {name:<20} {value}")
    print("  governance:")
    for name, value in k.governance.items():
        print(f"    {name:<20} {'n/a' if value is None else format(value, '.4f') if isinstance(value, float) else value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

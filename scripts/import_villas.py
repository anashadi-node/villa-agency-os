#!/usr/bin/env python3
"""Import a tenant's villa list from one file — dry run unless ``--apply`` is typed.

Why a CLI and not a form
------------------------
A new agency arrives with a spreadsheet: twenty villas, forty units. This is how that becomes
inventory, and it is deliberately two-step:

    python3 scripts/import_villas.py --template > villa-list.csv      # the header to fill in
    python3 scripts/import_villas.py --file villa-list.csv            # plan only: writes nothing
    python3 scripts/import_villas.py --file villa-list.csv --apply    # writes, once

Three rules, as in the rest of the repository's operations tooling:

* ``--apply`` is required to write anything. A dry run is the default, so a mistyped path cannot
  become a partial import.
* ``--agency`` names the agency explicitly, and the file **cannot** override it: a column in a CSV
  must never write into another tenant's inventory.
* A refused row stops the whole import, and the report names the line and the field. Half a villa
  list is worse than none, because nobody can tell what landed.

Exit codes: 0 = planned clean (or applied), 1 = the file or a row was refused, 2 = usage problem.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import villa_import as VI  # noqa: E402
from primitives import DomainError, SystemClock  # noqa: E402
from reservation_kernel import ReservationKernel  # noqa: E402

DEFAULT_AGENCY = "agency_default"


def _usage_error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _kernel(db: str, agency: str) -> ReservationKernel:
    return ReservationKernel.open(db, clock=SystemClock(), agency_id=agency)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a villa/unit list from one CSV file.")
    parser.add_argument("--file", help="the CSV file to read")
    parser.add_argument("--template", action="store_true",
                        help="print a starter file with the right header, then exit")
    parser.add_argument("--agency", default=DEFAULT_AGENCY)
    parser.add_argument("--actor", help="who is doing this (a human reference)")
    parser.add_argument("--actor-role", default="owner",
                        choices=list(VI.IMPORT_ROLES))
    parser.add_argument("--source-ref", default=None,
                        help="what the file is (e.g. 'villa-list-okt-2026.csv'), kept in the audit trail")
    parser.add_argument("--apply", action="store_true", help="write the rows (default: plan only)")
    args = parser.parse_args(argv)

    if args.template:
        sys.stdout.write(VI.template_csv())
        return 0
    if not args.file:
        return _usage_error("--file is required (or --template to print a starter file)")
    path = Path(args.file)
    if not path.is_file():
        return _usage_error(f"no such file: {path}")

    if args.apply:
        if not os.environ.get("VILLA_DB"):
            return _usage_error("--apply needs VILLA_DB to be named explicitly")
        if not args.actor:
            return _usage_error("--apply needs --actor: an import nobody signed is not an audit trail")
    db = os.environ.get("VILLA_DB") or ":memory:"

    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return _usage_error(f"cannot read {path}: {exc}")

    kernel = _kernel(db, args.agency)
    try:
        try:
            plan = VI.plan_import(kernel, text)
        except VI.InvalidImportFile as exc:
            print(f"REFUSED: {exc}")
            for key, value in sorted(exc.details.items()):
                print(f"  {key}: {value}")
            return 1

        print(f"file        : {path} ({plan.fingerprint[:12]})")
        print(f"agency      : {args.agency}")
        print(f"plan        : {plan.summary}")
        if not plan.ok:
            print("refused rows:")
            for refusal in plan.refusals[:40]:
                print(f"  {refusal.as_text()}")
            if len(plan.refusals) > 40:
                print(f"  … and {len(plan.refusals) - 40} more")
            print("VERDICT: REFUSED — fix the rows above and plan again; nothing was written")
            return 1

        if not args.apply:
            print("VERDICT: PLAN ONLY — nothing was written. Re-run with --apply to import.")
            return 0

        result = VI.apply_import(
            kernel, plan, actor=args.actor, actor_role=args.actor_role,
            source_ref=args.source_ref or path.name,
        )
        print(f"result      : {result.summary}")
        print("VERDICT: APPLIED")
        return 0
    except DomainError as exc:
        print(f"REFUSED: [{exc.code}] {exc}")
        return 1
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())

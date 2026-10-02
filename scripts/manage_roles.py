#!/usr/bin/env python3
"""Manage one agency's roles from the command line — the tenant's own roster.

    python3 scripts/manage_roles.py list
    python3 scripts/manage_roles.py seed      --apply --actor "Mr. Owner" --actor-role owner
    python3 scripts/manage_roles.py add       --role finance_helper --label "Asisten Keuangan" \
                                             --based-on finance_manager --apply --actor ... 
    python3 scripts/manage_roles.py rename    --role gm --label "Direktur Utama" --apply --actor ...
    python3 scripts/manage_roles.py disable   --role commercial_manager --apply --actor ...
    python3 scripts/manage_roles.py appoint   --role gm --holder mr_ceo --apply --actor ...
    python3 scripts/manage_roles.py unappoint --role gm --holder mr_ceo --apply --actor ...

Three rules, as with the inventory importer:

* **Dry run unless ``--apply``.** ``list`` never writes; every mutation prints what it *would* do
  first, and only ``--apply`` writes.
* **The agency is named explicitly** (``--agency``), never inferred from anything else.
* **Authority is the blueprint's.** ``--based-on`` chooses which platform role the new role
  inherits; a tenant may rename and staff freely, and may not outrank or resurrect the AI maker.

Holder references are opaque identifiers (``mr_ceo``), never personal names: the platform enforces
*that* a role has a holder, not *who* the person is.

Exit codes: 0 = done (or planned), 1 = refused, 2 = usage problem.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import agency_roles as AR  # noqa: E402
from primitives import DomainError, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_AGENCY = "agency_default"
DEFAULT_DB = "data/villa.db"


def _usage(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _print_roles(service: AR.AgencyRoles) -> None:
    rows = service.roles()
    if not rows:
        print("(no roles yet — run `seed` to start from the platform blueprint)")
        return
    print(f"{'role_id':<22} {'status':<9} {'blueprint':<20} {'rank':<5} holders")
    for role in rows:
        rank = "-" if role.rank is None else str(role.rank)
        holders = ", ".join(role.holders) if role.holders else "—"
        print(f"{role.role_id:<22} {role.status:<9} {role.based_on:<20} {rank:<5} {holders}")
    unstaffed = service.unstaffed_ladder_roles()
    if unstaffed:
        print(f"\nmoney ladder with no holder: {', '.join(unstaffed)}")
        print("go-live stays refused until each of them has a holder (decision D17)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage an agency's roles.")
    parser.add_argument(
        "command",
        choices=["list", "seed", "add", "rename", "disable", "enable", "appoint", "unappoint"],
    )
    parser.add_argument("--agency", default=DEFAULT_AGENCY)
    parser.add_argument("--role", help="the role id")
    parser.add_argument("--label", help="the tenant's own name for the role")
    parser.add_argument("--based-on", dest="based_on", help="which platform blueprint it inherits")
    parser.add_argument("--holder", help="an opaque holder reference (e.g. mr_ceo)")
    parser.add_argument("--actor", help="who is doing this (a human reference)")
    parser.add_argument("--actor-role", dest="actor_role", default="owner",
                        choices=list(AR.ROLE_EDITOR_ROLES))
    parser.add_argument("--apply", action="store_true", help="write (default: plan only)")
    args = parser.parse_args(argv)

    needs_writer = args.command != "list"
    if needs_writer:
        if not os.environ.get("VILLA_DB"):
            return _usage("this command needs VILLA_DB to be named explicitly")
        if not args.actor:
            return _usage("this command needs --actor: an unsigned role change is not a roster")
    db = os.environ.get("VILLA_DB") or DEFAULT_DB
    if needs_writer and not Path(db).exists():
        return _usage(f"no database at {db}")

    conn = connect_sqlite(db)
    try:
        service = AR.AgencyRoles(conn, clock=SystemClock(), agency_id=args.agency)
        if args.command == "list":
            _print_roles(service)
            return 0

        if not args.apply:
            target = args.role or args.holder or ""
            print(f"PLAN ONLY: would {args.command} {target!r} for agency {args.agency}")
            print("nothing was written. Re-run with --apply to do it.")
            return 0

        if args.command == "seed":
            roles = service.seed_blueprint(actor=args.actor, actor_role=args.actor_role)
            print(f"seeded {len(roles)} role(s) for agency {args.agency}")
        elif args.command == "add":
            role = service.add_role(args.role, label=args.label, based_on=args.based_on,
                                    actor=args.actor, actor_role=args.actor_role)
            print(f"added {role.role_id} ({role.label}) inheriting {role.based_on}")
        elif args.command == "rename":
            role = service.rename_role(args.role, label=args.label,
                                       actor=args.actor, actor_role=args.actor_role)
            print(f"renamed {role.role_id} to {role.label!r} (authority unchanged: {role.based_on})")
        elif args.command == "disable":
            role = service.disable_role(args.role, actor=args.actor, actor_role=args.actor_role)
            print(f"disabled {role.role_id}")
        elif args.command == "enable":
            role = service.enable_role(args.role, actor=args.actor, actor_role=args.actor_role)
            print(f"enabled {role.role_id}")
        elif args.command == "appoint":
            role = service.assign_holder(args.role, holder_ref=args.holder,
                                         actor=args.actor, actor_role=args.actor_role)
            print(f"{role.role_id} holders: {', '.join(role.holders)}")
        elif args.command == "unappoint":
            role = service.deactivate_holder(args.role, holder_ref=args.holder,
                                             actor=args.actor, actor_role=args.actor_role)
            print(f"{role.role_id} holders: {', '.join(role.holders) or '—'}")
        print("VERDICT: APPLIED")
        return 0
    except DomainError as exc:
        print(f"REFUSED: [{exc.code}] {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

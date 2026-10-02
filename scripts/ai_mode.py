#!/usr/bin/env python3
"""Press the §17 AI kill switch from the command line — the button a human actually reaches.

    python3 scripts/ai_mode.py list
    python3 scripts/ai_mode.py show     --principal aip_abc123
    python3 scripts/ai_mode.py set      --principal aip_abc123 --mode read_only \\
                                        --actor mr_owner --actor-role owner --reason "tamu salah villa"
    python3 scripts/ai_mode.py set ... --apply

Four rules, the same shape as the inventory importer and the role editor:

* **Dry run unless ``--apply``.** ``list``/``show`` never write; ``set`` prints what it *would* do.
* **Authority is checked in the domain, not here.** ``roles.may(actor_role, "ai.control")`` decides;
  this script only passes the role through. Held by the Owner and the Technology-Security Manager.
* **No default authority.** ``--actor`` and ``--actor-role`` are required for ``set``: an unsigned
  kill-switch change is worse than no change.
* **The reason is optional; the audit row is not.** Every move lands in the event log and the
  chain, with the actor's reference.

Exit codes: 0 = done (or planned), 1 = refused, 2 = usage problem.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import ai_service as AIS  # noqa: E402
import ai_gateway as GW  # noqa: E402
from audit import AuditLog  # noqa: E402
from policy_engine import PolicyResolver  # noqa: E402
from primitives import DomainError, IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_AGENCY = "agency_default"
DEFAULT_DB = "data/villa.db"
MODES = tuple(m.lower() for m in AIS.AI_STATUS_LADDER)


def _usage(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _print_principals(service: AIS.AiService) -> None:
    rows = service.principals()
    if not rows:
        print("(no AI principal exists in this database yet)")
        return
    print(f"{'principal_id':<28} {'name':<24} {'mode':<10} created_at")
    for principal in rows:
        print(
            f"{principal.principal_id:<28} {principal.name:<24} {principal.status:<10}"
            f" {principal.created_at}"
        )
    print("\nthe ladder: " + " > ".join(AIS.AI_STATUS_LADDER) + "  (narrowing is free; widening "
          "needs `activate`, and only the roles that hold ai.control may do either)")


def _print_events(service: AIS.AiService, principal_id: str) -> int:
    principal = service.principal(principal_id)
    events = service.events(principal_id)
    print(f"{principal.name} ({principal.principal_id}) — mode {principal.status}")
    for event in events:
        print(f"  {event['occurred_at']}  {event['kind']:<11} by {event['actor']}"
              + (f" — {event['reason']}" if event['reason'] else ""))
    if not events:
        print("  (no lifecycle event yet)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show or move an AI principal along the §17 ladder.")
    parser.add_argument("command", choices=["list", "show", "set"])
    parser.add_argument("--principal", help="the principal id (from `list`)")
    parser.add_argument("--mode", choices=MODES, help="the rung to move to")
    parser.add_argument("--actor", help="who is doing this (a human reference)")
    parser.add_argument("--actor-role", dest="actor_role", help="that human's role id")
    parser.add_argument("--reason", help="why (written into the audit row)")
    parser.add_argument("--agency", default=DEFAULT_AGENCY)
    parser.add_argument("--apply", action="store_true", help="write (default: plan only)")
    args = parser.parse_args(argv)

    if args.command == "show" and not args.principal:
        return _usage("`show` needs --principal")
    if args.command == "set":
        if not args.principal:
            return _usage("`set` needs --principal")
        if not args.mode:
            return _usage(f"`set` needs --mode (choose from {', '.join(MODES)})")
        if not args.actor or not args.actor_role:
            return _usage(
                "`set` needs --actor and --actor-role: an unsigned kill-switch change is not a "
                f"control (this capability is held by {', '.join(GW.AI_CONTROL_HOLDERS)})"
            )

    if args.command == "set":
        if not os.environ.get("VILLA_DB"):
            return _usage("changing a mode needs VILLA_DB to be named explicitly")
    db = os.environ.get("VILLA_DB") or DEFAULT_DB
    if args.command != "list" and not Path(db).exists():
        return _usage(f"no database at {db}")

    conn = connect_sqlite(db)
    try:
        clock = SystemClock()
        # Random ids: a deterministic factory restarts at 1 every process, so a second run would
        # collide with the ids the first run already stored (found by running this twice, 2026-10-02).
        ids = IdFactory()
        audit = AuditLog(conn, clock=clock, ids=ids)
        # The §16 policy store has no default in the service on purpose: an AI with no deny
        # registry must not start. The operator panel reads it the same way the agents will.
        service = AIS.AiService(
            conn,
            policy=PolicyResolver(conn, clock=clock, agency_id=args.agency),
            audit=audit,
            clock=clock,
            agency_id=args.agency,
        )
        if args.command == "list":
            _print_principals(service)
            return 0
        if args.command == "show":
            _print_events(service, args.principal)
            reachable = service.reachable_modes(args.principal)
            print("reachable from here: " + (", ".join(m.lower() for m in reachable) or "(nothing — retired)"))
            return 0

        mode = args.mode.upper()
        if not args.apply:
            print(f"PLAN ONLY: would move {args.principal} to {mode}")
            print(f"  authority: {args.actor} ({args.actor_role})")
            print(f"  reason   : {args.reason or '(none given)'}")
            print("nothing was written. Re-run with --apply to do it.")
            return 0

        # The gateway owns the rule; the CLI passes the role through and reports the refusal.
        gateway = GW.AiGateway(service, audit=audit, clock=clock, ids=ids, agency_id=args.agency)
        principal = gateway.set_mode(
            args.principal, mode, actor=args.actor, actor_role=args.actor_role,
            reason=args.reason,
        )
        print(f"{principal.name} ({principal.principal_id}) is now {principal.status}")
        if principal.status == AIS.AI_STATUS_ACTIVE:
            print("NOTE: this rung has the widest reach — the whole tool catalogue the policy allows")
        print("VERDICT: APPLIED")
        return 0
    except DomainError as exc:
        print(f"REFUSED: [{exc.code}] {exc}")
        if "principal" in str(exc) or args.principal:
            try:
                reachable = service.reachable_modes(args.principal)
                print("reachable from where it is now: "
                      + (", ".join(m.lower() for m in reachable) or "(nothing — retired)"))
            except DomainError:
                pass
        return 1
    except Exception as exc:  # noqa: BLE001 - an operator tool reports, it does not traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("nothing about the mode was changed unless the line above says APPLIED", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

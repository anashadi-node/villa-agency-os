#!/usr/bin/env python3
"""Migrate a PostgreSQL database explicitly named by the operator.

Usage:
    python3 scripts/pg_migrate.py --dsn postgresql://user@127.0.0.1:5432/db --inspect
    python3 scripts/pg_migrate.py --dsn "host=127.0.0.1 dbname=villa" --inspect
    python3 scripts/pg_migrate.py --dsn postgresql://... --apply

Discipline, as with the SQLite runner and the policy CLI: the target must be named
explicitly. There is no default DSN, no fallback to a local socket, and no silent switch to
SQLite. ``--inspect`` is the default because a migration is a decision, not a side effect of
running the tool. ``--inspect`` is read-only: it does not create the ledger, does not apply
anything, and reports CONFLICTED/UNKNOWN with a nonzero exit code.

Exit codes: 0 ok, 2 refused (unsafe/unknown state), 3 failed mid-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import pg_migrations as PGM  # noqa: E402

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "ip6-localhost"}
LOOPBACK_ADDRS = {"127.0.0.1", "::1"}


def fail(message: str, code: int = 2) -> NoReturn:
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(code)


def resolve_target(raw: str, *, allow_remote: bool = False, expect_baseline: str | None = None) -> str:
    """Parse the target with the driver, then decide whether it is a target we may touch.

    Checking only whether the text starts with ``postgresql://`` is not verification: a
    keyword/value string, a service definition or a socket path can all name a database the
    operator never considered. So the parameters are resolved first, then judged.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    text = raw.strip()
    if not text:
        fail(
            "no target. Pass --dsn or set VILLA_PG_DSN. Migrating whichever database "
            "happens to be reachable is not a decision anyone made."
        )
    if text.lower().startswith("file:") or (text.lower().endswith(".db") and "://" not in text):
        fail("target looks like SQLite; this runner never guesses a dialect")
    try:
        params = conninfo_to_dict(text)
    except Exception as error:  # noqa: BLE001 - the message is the point
        fail(f"target is not a PostgreSQL connection string ({type(error).__name__})")
    if params.get("service"):
        fail(
            "service definitions are refused: the connection then depends on files outside this "
            "argument, so the target cannot be verified from the command line"
        )
    hosts = [part.strip() for part in str(params.get("host") or "").split(",") if part.strip()]
    addrs = [part.strip() for part in str(params.get("hostaddr") or "").split(",") if part.strip()]
    if not hosts and not addrs:
        fail("no host in the target: a unix-socket default is not a decision anyone made")
    if not params.get("dbname"):
        fail("no dbname in the target: refusing to migrate whichever database the role defaults to")
    if not params.get("port"):
        fail(
            "no port in the target: defaulting to 5432 is how a statement reaches the wrong"
            " cluster, and a disposable target is always an explicit host:port"
        )
    unix_sockets = [h for h in hosts if h.startswith("/")]
    remote_names = [h for h in hosts if h not in LOOPBACK_NAMES and not h.startswith("/")]
    remote_addrs = [a for a in addrs if a not in LOOPBACK_ADDRS]
    if (remote_names or remote_addrs) and not (allow_remote and expect_baseline):
        named = ", ".join(remote_names + remote_addrs)
        fail(
            f"remote target refused ({named}): a networked database is not disposable. Pass"
            f" --allow-remote-target together with --expect-baseline <sha256> to name it"
            f" deliberately."
        )
    if unix_sockets and not (allow_remote and expect_baseline):
        fail(
            "unix-socket target refused: the socket path does not prove which cluster answers."
            " Pass --allow-remote-target together with --expect-baseline <sha256>."
        )
    return make_conninfo(**params)


def describe_target(params: dict[str, str]) -> str:
    """Name the target from parsed fields. Never echo the DSN: it carries the password.

    mask_dsn() was the wrong shape of fix — it had to guess every way a password can be
    written (URL userinfo, keyword, quoted keyword, percent-encoding, sslpassword). Printing
    only what this function chose to print cannot leak what it never read.
    """
    host = params.get("host") or params.get("hostaddr") or "?"
    return f"{params.get('user') or '?'}@{host}:{params.get('port') or '?'}/{params.get('dbname') or '?'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", help="PostgreSQL DSN or libpq keyword/value string; never logged in full")
    parser.add_argument("--inspect", action="store_true", help="report state; change nothing (default)")
    parser.add_argument("--apply", action="store_true", help="apply the plan")
    parser.add_argument(
        "--expect-local-baseline",
        "--expect-baseline",
        dest="expect_baseline",
        metavar="SHA256",
        help=(
            "refuse unless the LOCAL approved files hash to this value. This pins what this"
            " checkout would apply; it does not verify the state of a remote database"
        ),
    )
    parser.add_argument(
        "--allow-remote-target",
        action="store_true",
        help="permit a non-loopback host (requires --expect-baseline)",
    )
    parser.add_argument("--lock-timeout", type=int, default=10, help="seconds to wait for the migration lock")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--quiet", action="store_true", help="no progress lines")
    args = parser.parse_args(argv)
    if args.apply and args.inspect:
        fail("--inspect and --apply are mutually exclusive")

    try:
        dsn = resolve_target(
            args.dsn or os.environ.get("VILLA_PG_DSN", ""),
            allow_remote=args.allow_remote_target,
            expect_baseline=args.expect_baseline,
        )
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - a parse surprise is not a traceback for the user
        fail(f"cannot read the target ({type(error).__name__})")
        return 3  # unreachable
    from psycopg.conninfo import conninfo_to_dict

    target_name = describe_target(conninfo_to_dict(dsn))
    migrations = PGM.discover()
    try:
        conn = PGM.connect(dsn)
    except PGM.MigrationRefused as refusal:
        fail(refusal.message)
        return 2  # unreachable
    except Exception as error:  # connection refused, bad DSN, auth
        fail(f"cannot connect ({type(error).__name__}); check target, role and network")
        return 2

    try:
        if not args.apply:
            state = PGM.database_state(conn, migrations)
            if args.json:
                print(json.dumps({"target": target_name, **state}, indent=2))
            else:
                print(f"target {target_name}")
                print(f"status {state['status']}")
                for row in state["steps"]:
                    print(f"  {row['planned_state']:<20} {row['migration']}  ({row['reason']})")
                if state["missing_guards"]:
                    print(f"missing guards: {', '.join(state['missing_guards'])}")
            if state["status"] in (PGM.CONFLICTED, PGM.UNKNOWN):
                detail = []
                if state["conflicts"]:
                    detail.append("conflicted: " + ", ".join(state["conflicts"]))
                if state["missing_guards"]:
                    detail.append("guard drift: " + ", ".join(state["missing_guards"]))
                fail(
                    f"database state is {state['status']}; nothing was changed. "
                    + "; ".join(detail)
                )
            return 0

        report = PGM.apply(
            conn,
            migrations=migrations,
            expect_baseline=args.expect_baseline,
            lock_timeout=args.lock_timeout,
            on_progress=None if args.quiet else (lambda line: print(line)),
        )
        if args.json:
            print(json.dumps({"target": target_name, "report": report.as_dict()}, indent=2))
        else:
            print(
                f"applied={len(report.applied)} reconciled={len(report.reconciled)} "
                f"already={len(report.already)} failed={len(report.failed)}"
            )
            print(f"catalogue: {report.survey}")
            print(f"guards: {'verified' if not report.missing_guards else report.missing_guards}")
        return 0
    except PGM.MigrationRefused as refusal:
        print(f"REFUSED [{refusal.code}]: {refusal.message}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - the operator gets a code, not a traceback
        print(f"FAILED [{type(error).__name__}]: {str(error)[:300]}", file=sys.stderr)
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

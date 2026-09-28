#!/usr/bin/env python3
"""Publish, inspect or withdraw the §56 medium-risk AI tool allowance.

The platform ships with the machine path **closed**. ``security.ai.medium_tool_allowance``
is not seeded in the schema, and that is deliberate: the schema seeds the values the
platform cannot run without (session timeouts, evidence freshness windows, the deny
registry) whose absence is not a valid state, whereas the absence of this permission
*is* a valid state — the safe one. So the owner opens it as a recorded act, and this
tool is how that act is performed and inspected. No code changes when the decision
changes, which is the point of a policy-controlled tool (§56).

    VILLA_DB=data/villa.db python3 scripts/publish_ai_allowance.py status
    VILLA_DB=data/villa.db python3 scripts/publish_ai_allowance.py publish \\
        --approved-by owner_anas
    VILLA_DB=data/villa.db python3 scripts/publish_ai_allowance.py withdraw \\
        --approved-by owner_anas

Environment:
    VILLA_DB    path to the SQLite database (default: data/villa.db)

Exit codes: 0 = success, 1 = the policy store refused the write, 2 = usage or
configuration problem. A non-zero exit from ``publish`` means the allowance was **not**
published and the machine path is still refused — the safe state, not a half-state.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from policy_engine import (  # noqa: E402  (path setup must run first)
    PolicyError,
    PolicyResolver,
    PolicyValueNotPublished,
)
from primitives import DomainError, IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_DB = "data/villa.db"
DEFAULT_AGENCY = "agency_default"
ALLOWANCE_KEY = "security.ai.medium_tool_allowance"

#: The owner's decision of 2026-09-26 (§56): **one** tool — the only §56 MEDIUM tool
#: that has an enforcement point today — opened for a bounded window rather than
#: forever. The other five stay refused, because an allowance should mean "what can be
#: enforced", not "what the specification mentions".
OWNER_DECISION_TOOLS: tuple[str, ...] = ("availability.request",)
OWNER_DECISION_SOURCE = (
    "§56 AI Reservation Agent Autonomy (LOCKED) — medium-risk policy-controlled tools "
    "(owner decision 2026-09-26)"
)
OWNER_DECISION_EFFECTIVE_FROM = "2026-09-27"
OWNER_DECISION_EFFECTIVE_TO = "2026-10-26"
WITHDRAWAL_SOURCE = (
    "§56 medium-tool allowance withdrawn by the owner — the machine path returns to "
    "refused"
)


def _settings() -> Path:
    return Path(os.environ.get("VILLA_DB", DEFAULT_DB))


def _resolver(conn, agency_id: str) -> PolicyResolver:
    return PolicyResolver(
        conn, clock=SystemClock(), ids=IdFactory(), agency_id=agency_id
    )


def _newest_revision(conn, agency_id: str, policy_key: str) -> dict | None:
    """The latest revision regardless of its window — what the owner last decided."""
    row = conn.execute(
        "SELECT policy_revision_id, version, value, effective_from, effective_to,"
        " source_doc_ref, approved_by FROM policy_revisions"
        " WHERE policy_key = ? AND agency_id IN (?, ?)"
        " ORDER BY version DESC, effective_from DESC LIMIT 1",
        (policy_key, agency_id, DEFAULT_AGENCY),
    ).fetchone()
    return dict(row) if row else None


def _print_effective(conn, agency_id: str) -> int:
    resolver = _resolver(conn, agency_id)
    print(f"ALLOWANCE {ALLOWANCE_KEY}")
    try:
        value = resolver.resolve(ALLOWANCE_KEY)
    except PolicyValueNotPublished as exc:
        print(f"  effective now : CLOSED [{exc.code}] — a machine actor is refused")
    else:
        # ``value.value`` is the stored JSON *text* ('[]' is truthy), so the emptiness
        # check goes through the platform's own accessor, as ``ai_service`` does.
        if value.as_json():
            print(f"  effective now : {value.value}")
        else:
            print("  effective now : [] — published and empty: a machine actor is refused")
        print(
            f"  window        : {value.effective_from} → {value.effective_to}"
            f" (version {value.version})"
        )
        print(f"  approved by   : {value.approved_by}")
        print(f"  source        : {value.source_doc_ref}")
        print(f"  fingerprint   : {value.fingerprint[:16]}…")
        return 0

    newest = _newest_revision(conn, agency_id, ALLOWANCE_KEY)
    if newest is None:
        print("  newest revision: none — the owner has never published this value")
        return 0
    # Only two states reach here: the newest revision has not started, or every revision
    # has ended. Saying which one matters — "closed" reads like a withdrawal.
    today = SystemClock().today().isoformat()
    if today < newest["effective_from"]:
        why = f"not yet open — it opens {newest['effective_from']}"
    else:
        why = f"expired {newest['effective_to']} — nothing renews it automatically"
    print(
        f"  newest revision: version {newest['version']} {newest['value']}"
        f" ({newest['effective_from']} → {newest['effective_to']},"
        f" approved by {newest['approved_by']}) — {why}"
    )
    return 0


def _write(conn, agency_id: str, tools: list[str], args: argparse.Namespace, source: str) -> int:
    resolver = _resolver(conn, agency_id)
    value = resolver.publish(
        ALLOWANCE_KEY,
        tools,
        effective_from=args.effective_from,
        effective_to=args.effective_to,
        source_doc_ref=source,
        approved_by=args.approved_by,
    )
    print(
        f"PUBLISHED {ALLOWANCE_KEY} version {value.version} = {value.value}\n"
        f"  window     : {value.effective_from} → {value.effective_to}\n"
        f"  approved by: {value.approved_by}\n"
        f"  fingerprint: {value.fingerprint[:16]}…"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("status", "publish", "withdraw"),
        help="show the effective value, publish the owner's decision, or revoke it",
    )
    parser.add_argument(
        "--approved-by", help="the human who approves this revision (required to write)",
    )
    parser.add_argument("--agency", default=DEFAULT_AGENCY, help="agency scope")
    parser.add_argument(
        "--tools", help=(
            "comma-separated tool list to publish, replacing the owner's default list — "
            "a policy decision should not need a code change"
        ),
    )
    parser.add_argument(
        "--effective-from", default=None,
        help=(
            "first day the value applies (default: today for ``withdraw``, so a "
            "withdrawal takes effect at once rather than waiting for a default date; "
            f"{OWNER_DECISION_EFFECTIVE_FROM} for ``publish``)"
        ),
    )
    parser.add_argument(
        "--effective-to", default=OWNER_DECISION_EFFECTIVE_TO,
        help=(
            "last day the value applies (default: "
            f"{OWNER_DECISION_EFFECTIVE_TO}). The window bounds the exposure: once it "
            "passes, the machine path refuses again with no one having to remember"
        ),
    )
    args = parser.parse_args(argv)
    if args.effective_from is None:
        # A withdrawal has to take effect the day it is issued: falling back to the
        # owner-decision start date would leave the permission open until then, and a
        # security control that closes "tomorrow" is indistinguishable from one that
        # does not close.
        args.effective_from = (
            SystemClock().today().isoformat()
            if args.command == "withdraw"
            else OWNER_DECISION_EFFECTIVE_FROM
        )

    db_path = _settings()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    if args.command in {"publish", "withdraw"} and not (args.approved_by or "").strip():
        print(
            "--approved-by is required: a permission that names no approver is not a "
            "policy (and the store refuses it anyway)",
            file=sys.stderr,
        )
        return 2
    if args.command == "publish" and not os.environ.get("VILLA_DB", "").strip():
        # Granting a machine permission has to name its target. Every other CLI here falls back
        # to ``data/villa.db``, which is the right default for a read or a seed — but an
        # authority grant that lands silently in whatever ``data/villa.db`` happens to sit in
        # the working directory is a permission nobody decided to give. Measured, not
        # theorised: the documented publish command run without ``VILLA_DB`` wrote a revision
        # into this repository's demo database.
        print(
            "VILLA_DB must be set explicitly for `publish`: granting a machine permission has "
            "to name the database it applies to",
            file=sys.stderr,
        )
        return 2

    conn = connect_sqlite(str(db_path))
    try:
        if args.command == "status":
            return _print_effective(conn, args.agency)
        # Say where the write landed: an authority change is worth being able to point at.
        print(f"target database: {db_path}")
        if args.command == "withdraw":
            return _write(conn, args.agency, [], args, WITHDRAWAL_SOURCE)
        tools = (
            [t.strip() for t in args.tools.split(",") if t.strip()]
            if args.tools
            else list(OWNER_DECISION_TOOLS)
        )
        return _write(conn, args.agency, tools, args, OWNER_DECISION_SOURCE)
    except PolicyError as exc:
        print(f"POLICY REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except DomainError as exc:
        print(f"REFUSED [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

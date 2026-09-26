#!/usr/bin/env python3
"""Set one policy value — the owner's manual act, without a code change.

§15 is explicit that thresholds are configuration, not code: "Threshold adalah
configuration, bukan hard-coded". The numbers the owner decides — which roles may grant
a discount, the margin floor as a percentage and as an amount in minor units — therefore
live in the policy store and nowhere else, and this tool is how they get there. It
resolves the key's declared type from the catalogue, coerces what was typed, and lets the
store refuse whatever it would refuse anyway.

    show                        every declared key, and whether an owner has decided it
    show --key K                one key: its definition and its effective value
    set  --key K --value V      append a revision (needs --source-doc-ref and --approved-by)

Three rules are deliberate:

* ``set`` requires ``VILLA_DB`` to be named. A policy value that lands silently in
  whatever ``data/villa.db`` happens to sit in the working directory is a decision nobody
  made — the same reason the allowance CLI demands it.
* A value is never defaulted and never guessed by this tool. An unpublished key means the
  engine refuses; that is the intended state until the owner publishes, not a gap for the
  tool to fill.
* ``set`` cites the document it came from and names the human who approved it, because the
  store refuses a revision without either.

Exit codes: 0 = success, 1 = the policy store refused the write, 2 = usage or
configuration problem. A non-zero exit from ``set`` means the value was **not** published
and the previous revision still governs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from policy_engine import (  # noqa: E402  (path setup must run first)
    POLICY_OPEN_END,
    PolicyResolver,
    PolicyValueNotPublished,
)
from primitives import DomainError, IdFactory, SystemClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

DEFAULT_DB = "data/villa.db"
DEFAULT_AGENCY = "agency_default"

#: Keys the owner has to decide before the commercial path can move at all. Kept here so
#: ``show`` can say out loud what is still waiting, instead of letting the operator
#: discover it from a refusal in the middle of a booking.
DISCOUNT_KEYS: tuple[str, ...] = (
    "commercial.discount.authority_roles",
    "commercial.margin_floor_pct",
    "commercial.margin_floor_minor",
)

_INT_TYPES = ("MINUTES", "HOURS", "DAYS", "COUNT")
_JSON_TYPES = ("JSON", "ROLE_SET")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _db_path() -> Path:
    return Path(os.environ.get("VILLA_DB", DEFAULT_DB))


def _resolver(conn, agency_id: str) -> PolicyResolver:
    return PolicyResolver(conn, clock=SystemClock(), ids=IdFactory(), agency_id=agency_id)


def _coerce(definition: Any, raw: str) -> Any:
    """Turn what the operator typed into the type the store declared for this key.

    The store validates again — this only makes the failure legible before the write.
    """
    kind = definition.value_type
    text = str(raw).strip()
    if kind in _INT_TYPES:
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{kind} expects a whole number, got {text!r}") from None
    if kind == "BOOL":
        lowered = text.lower()
        if lowered not in _TRUE + _FALSE:
            raise ValueError(f"BOOL expects true or false, got {text!r}")
        return lowered in _TRUE
    if kind in _JSON_TYPES:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{kind} expects JSON ({exc.msg}); for a role set try '[\"owner\"]'"
            ) from None
    # PCT is stored as decimal text ("20", "7.5") and TEXT as-is.
    return text


def _declared_keys(conn) -> list:
    return list(
        conn.execute(
            "SELECT policy_key, value_type, owner_role, risk_level, domain, description"
            " FROM policy_defs ORDER BY domain, policy_key"
        )
    )


def _state(resolver: PolicyResolver, key: str) -> tuple[str, str]:
    """``(state, detail)`` for one key — what governs it today, in one line."""
    definition = resolver.definition(key)
    try:
        value = resolver.resolve(key)
    except PolicyValueNotPublished:
        return "WAITING", f"no revision — {definition.owner_role} decides this"
    return (
        "SET",
        f"v{value.version} = {value.value} ({value.effective_from} → {value.effective_to})",
    )


def _show(conn, agency_id: str, key: str | None) -> int:
    resolver = _resolver(conn, agency_id)
    if key:
        definition = resolver.definition(key)
        print(f"POLICY {key}")
        print(f"  type        : {definition.value_type}")
        print(f"  decided by  : {definition.owner_role} (risk {definition.risk_level})")
        print(f"  description : {definition.description}")
        try:
            value = resolver.resolve(key)
        except PolicyValueNotPublished as exc:
            print(f"  effective   : NOT PUBLISHED [{exc.code}] — the engine refuses")
            print("  publish it  : set --key " + key + " --value <value> --source-doc-ref <doc> --approved-by <name>")
            return 0
        print(f"  effective   : {value.value} (version {value.version})")
        print(f"  window      : {value.effective_from} → {value.effective_to}")
        print(f"  approved by : {value.approved_by}")
        print(f"  source      : {value.source_doc_ref}")
        print(f"  fingerprint : {value.fingerprint[:16]}…")
        return 0

    waiting = 0
    for row in _declared_keys(conn):
        state, detail = _state(resolver, row["policy_key"])
        if state == "WAITING":
            waiting += 1
        mark = " " if state == "SET" else "*"
        print(f"{mark} {row['policy_key']:<48} {row['value_type']:<9} {detail}")
    print(f"\n{waiting} key(s) waiting on an owner decision (* marks them).")
    print("A key with no revision is refused by the engine, never defaulted (§15).")
    return 0


def _set(conn, agency_id: str, args: argparse.Namespace) -> int:
    resolver = _resolver(conn, agency_id)
    definition = resolver.definition(args.key)
    try:
        value = _coerce(definition, args.value)
    except ValueError as exc:
        print(f"{args.key}: {exc}", file=sys.stderr)
        return 2
    published = resolver.publish(
        args.key,
        value,
        effective_from=args.effective_from,
        effective_to=args.effective_to,
        source_doc_ref=args.source_doc_ref,
        approved_by=args.approved_by,
    )
    print(
        f"PUBLISHED {args.key} version {published.version} = {published.value}\n"
        f"  window     : {published.effective_from} → {published.effective_to}\n"
        f"  approved by: {published.approved_by}\n"
        f"  source     : {published.source_doc_ref}\n"
        f"  fingerprint: {published.fingerprint[:16]}…"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=("show", "set"),
        help="list the keys and their state, or publish one value",
    )
    parser.add_argument("--key", help="the policy key (required for ``set``)")
    parser.add_argument(
        "--value",
        help=(
            "the value to publish, as declared for the key: a whole number for COUNT, "
            "decimal text for PCT, JSON for ROLE_SET/JSON, true/false for BOOL"
        ),
    )
    parser.add_argument("--agency", default=DEFAULT_AGENCY, help="agency scope")
    parser.add_argument(
        "--approved-by",
        help="the human who decides this revision (required to write)",
    )
    parser.add_argument(
        "--source-doc-ref",
        help=(
            "the document the value comes from — the owner's decision, a contract, a "
            "management note (required to write)"
        ),
    )
    parser.add_argument(
        "--effective-from",
        default=None,
        help=(
            "first day the value applies (default: today — an owner's decision takes "
            "effect when it is made, not on some default date)"
        ),
    )
    parser.add_argument(
        "--effective-to",
        default=POLICY_OPEN_END,
        help=f"last day the value applies (default: {POLICY_OPEN_END}, open-ended)",
    )
    args = parser.parse_args(argv)

    db_path = _db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    if args.command == "set":
        missing = [
            flag
            for flag, given in (
                ("--key", args.key),
                ("--value", args.value),
                ("--source-doc-ref", args.source_doc_ref),
                ("--approved-by", args.approved_by),
            )
            if not (given or "").strip()
        ]
        if missing:
            print(
                f"{', '.join(missing)} required for `set`: a value with no document and no "
                "approver is not a policy decision (the store refuses it anyway)",
                file=sys.stderr,
            )
            return 2
        if not os.environ.get("VILLA_DB", "").strip():
            print(
                "VILLA_DB must be set explicitly for `set`: a policy value that lands "
                "silently in whatever data/villa.db happens to sit in the working "
                "directory is a decision nobody made",
                file=sys.stderr,
            )
            return 2
        if args.effective_from is None:
            args.effective_from = SystemClock().today().isoformat()

    conn = connect_sqlite(str(db_path))
    try:
        if args.command == "show":
            return _show(conn, args.agency, args.key)
        return _set(conn, args.agency, args)
    except DomainError as exc:
        # The store's own refusal, verbatim: an unknown key, a backdated revision, a
        # value outside the declared type, a missing approver.
        print(f"REFUSED [{exc.code}] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

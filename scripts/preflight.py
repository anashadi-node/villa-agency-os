#!/usr/bin/env python3
"""Go-live preflight: the last gate before real guests and real money.

Run this on the machine that will serve the system, against the real database:

    python3 scripts/preflight.py --db data/villa.db \
        --anchor-dir /mnt/worm/anchors \
        --decisions docs/GO_LIVE_DECISIONS.md \
        --backup /backups/villa-2026-09-21.sqlite

It checks three different things on purpose (plus the served ports when
``--host-check`` is given):

**The repository** — schema parity with the PostgreSQL twin, no committed
secrets, no unsafe switches, and every module importable. A tree that cannot
pass its own deploy gate must never be promoted.

**The data** — schema version matches the code, the audit chain verifies, a
recent *verified* anchor exists, referential integrity holds, and the database
does not contain demo data. Shipping a demo seed into production is how
"example guest" rows end up in an owner statement.

**The business** — every decision marked BLOCKING in
``docs/GO_LIVE_DECISIONS.md`` is decided. Green tests cannot substitute for an
undecided payment provider, and this script refuses to pretend otherwise.

Exit codes: 0 = go, 1 = no-go (a check failed), 2 = the preflight could not run
(bad arguments or missing files). Every check prints its own verdict, so the
report can be pasted into a change ticket.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from anchors import AnchorError, verify_published_anchors  # noqa: E402
from audit import AuditLog  # noqa: E402
from observability import collect_metrics, health_checks  # noqa: E402
from primitives import DomainError, SystemClock, parse_iso  # noqa: E402
from schema_loader import SCHEMA_VERSION, applied_version, connect_sqlite  # noqa: E402

import agency_roles as AGENCY_ROLES  # noqa: E402  (path setup must run first)
import roles  # noqa: E402  (path setup must run first)
import settings as SETTINGS  # noqa: E402  (path setup must run first)

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"
DECISION_ROW = re.compile(r"^\|\s*(D\d+)\s*\|(.+)\|\s*$")
ANCHOR_MAX_AGE_DAYS = 45
BACKUP_MIN_BYTES = 4096
BACKUP_MAX_AGE_HOURS = 26


@dataclass(frozen=True)
class Check:
    name: str
    verdict: str
    detail: str


def _check(name: str, verdict: str, detail: str) -> Check:
    return Check(name=name, verdict=verdict, detail=detail)


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------
def check_twin_parity() -> Check:
    try:
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts/generate_pg_twin.py"), "--check"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
    except OSError as exc:  # pragma: no cover - environment problem
        return _check("schema twin parity", FAIL, f"cannot run the twin check: {exc}")
    if result.returncode == 0:
        return _check("schema twin parity", OK, "PostgreSQL twin matches the SQLite source")
    return _check("schema twin parity", FAIL, (result.stdout + result.stderr).strip()[:300])


def check_scans() -> list[Check]:
    checks: list[Check] = []
    for script, label in (
        ("scripts/secret_scan.sh", "secret scan"),
        ("scripts/unsafe_config_scan.sh", "unsafe config scan"),
    ):
        try:
            result = subprocess.run(
                ["bash", str(REPO / script)], capture_output=True, text=True,
                timeout=180, cwd=str(REPO),
            )
        except OSError as exc:  # pragma: no cover
            checks.append(_check(label, FAIL, f"cannot run {script}: {exc}"))
            continue
        output = (result.stdout + result.stderr).strip().splitlines()
        summary = output[-1] if output else "no output"
        verdict = OK if result.returncode == 0 else FAIL
        checks.append(_check(label, verdict, summary[:200]))
    return checks


def check_git_tree_clean() -> Check:
    """A deploy from a dirty tree cannot be reproduced from the commit log."""

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            timeout=60, cwd=str(REPO),
        )
    except OSError as exc:  # pragma: no cover
        return _check("git tree", WARN, f"cannot inspect the tree: {exc}")
    if result.returncode != 0:
        return _check("git tree", WARN, "not a git checkout; deploy provenance unknown")
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        return _check(
            "git tree", WARN,
            f"{len(dirty)} uncommitted path(s): {', '.join(d.split()[-1] for d in dirty[:3])}",
        )
    return _check("git tree", OK, "clean tree")


def check_env_present(names: list[str]) -> Check:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        return _check("environment", WARN, f"not set: {', '.join(missing)}")
    return _check("environment", OK, f"{len(names)} expected variable(s) present")


def check_tracked_env_file() -> Check:
    env_path = REPO / ".env"
    if env_path.exists():
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            capture_output=True, text=True, cwd=str(REPO),
        )
        if tracked.returncode == 0:
            return _check(".env handling", FAIL, ".env is tracked by git")
        return _check(".env handling", WARN, ".env exists locally (untracked) — keep it that way")
    return _check(".env handling", OK, "no .env in the tree")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def check_database(db: Path, anchor_dir: Optional[Path]) -> list[Check]:
    checks: list[Check] = []
    conn = connect_sqlite(str(db))
    try:
        version = applied_version(conn)
        if version == SCHEMA_VERSION:
            checks.append(_check("schema version", OK, version))
        else:
            checks.append(_check(
                "schema version", FAIL,
                f"database reports {version!r}, code expects {SCHEMA_VERSION!r}",
            ))

        try:
            report = AuditLog(conn, clock=SystemClock(), ids=_ids()).verify()
            checks.append(_check(
                "audit chain", OK, f"intact up to seq {getattr(report, 'count', 0)}",
            ))
        except DomainError as exc:
            checks.append(_check("audit chain", FAIL, f"{exc.code}: {exc}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("audit chain", FAIL, f"unreadable: {exc}"))

        if anchor_dir is None:
            checks.append(_check(
                "published anchor", WARN, "not checked (--anchor-dir not given)",
            ))
        else:
            checks.extend(_check_anchor(conn, anchor_dir))

        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if rows:
            checks.append(_check("referential integrity", FAIL, f"{len(rows)} dangling row(s)"))
        else:
            checks.append(_check("referential integrity", OK, "no dangling references"))

        checks.append(check_demo_data(conn))
        checks.append(check_published_ladder_roles(conn))
        checks.append(check_tenant_readiness(conn))

        health = health_checks(conn, clock=SystemClock(), anchor_dir=str(anchor_dir) if anchor_dir else None)
        critical = [c for c in health if c.severity == "CRITICAL"]
        if critical:
            checks.append(_check(
                "system health", FAIL,
                "; ".join(f"{c.name}: {c.detail}" for c in critical)[:300],
            ))
        else:
            checks.append(_check("system health", OK, "no CRITICAL finding"))

        metrics = collect_metrics(conn, clock=SystemClock())
        checks.append(_check(
            "operational load", OK,
            f"{metrics.counters['cases_open']} open case(s), "
            f"{metrics.counters['approvals_pending']} pending approval(s), "
            f"{metrics.counters['reconciliation_open']} open reconciliation(s)",
        ))
    finally:
        conn.close()
    return checks


def _ids():
    from primitives import IdFactory

    return IdFactory(True, "preflight")


def _check_anchor(conn: Any, anchor_dir: Path) -> list[Check]:
    if not anchor_dir.is_dir():
        return [_check("published anchor", FAIL, f"anchor directory missing: {anchor_dir}")]
    try:
        verification = verify_published_anchors(
            conn=conn, audit=AuditLog(conn, clock=SystemClock(), ids=_ids()),
            directory=anchor_dir,
        )
    except (AnchorError, DomainError) as exc:
        return [_check("published anchor", FAIL, f"{getattr(exc, 'code', 'ANCHOR_ERROR')}: {exc}")]

    age_days = None
    try:
        from anchors import latest_published

        latest = latest_published(anchor_dir)
        if latest is not None:
            age_days = (SystemClock().now() - parse_iso(latest.published_at)).days
    except Exception:  # noqa: BLE001 - age is informative, verification is the gate
        age_days = None

    if age_days is not None and age_days > ANCHOR_MAX_AGE_DAYS:
        return [_check(
            "published anchor", FAIL,
            f"latest anchor is {age_days} days old (max {ANCHOR_MAX_AGE_DAYS}); "
            "a stale anchor no longer proves that recent rows exist",
        )]
    detail = f"{verification.latest_file} verified ({verification.files_checked} file(s))"
    if age_days is not None:
        detail += f", {age_days} day(s) old"
    return [_check("published anchor", OK, detail)]


#: The demo seeder (scripts/seed_demo.py) prefixes every identity it writes with
#: this marker, and names the properties in Indonesian so a human spots them too.
DEMO_ID_PREFIX = "demo_"
DEMO_NAME_HINTS = ("contoh", "demo")


#: The §16 key whose published value names the roles that must approve a payment
#: (ADR-0036). A value naming a role the engine cannot rank would answer the first real
#: payment with a refusal that names no cause, so it is checked here and not only in tests:
#: a fresh checkout has no database for a test to read.
PAYMENT_POLICY_KEY = "finance.approval.payment_thresholds_minor"


def _published_role_names(value: Any) -> list[str]:
    """Every role name inside a published payment value, in either published shape.

    Two shapes exist: ADR-0036's ``ladder`` (one ordered list) and the shape before it,
    ``levels`` (a list of ``{min_minor, roles}``). Both are read, because the old revision
    still governs any decision dated before the new one took effect.
    """
    names: list[str] = []
    if not isinstance(value, dict):
        return names
    ladder = value.get("ladder")
    if isinstance(ladder, list):
        names.extend(str(entry) for entry in ladder)
    levels = value.get("levels")
    if isinstance(levels, list):
        for level in levels:
            if isinstance(level, dict) and isinstance(level.get("roles"), list):
                names.extend(str(entry) for entry in level["roles"])
    return names


def check_published_ladder_roles(conn: Any) -> Check:
    """The published payment policy must name roles the engine knows and can rank."""
    row = conn.execute(
        "SELECT version, value FROM policy_revisions WHERE policy_key = ?"
        " ORDER BY version DESC LIMIT 1",
        (PAYMENT_POLICY_KEY,),
    ).fetchone()
    if row is None:
        return _check(
            "policy role names", WARN, f"{PAYMENT_POLICY_KEY} has no published revision yet",
        )
    version, raw = row[0], row[1]
    try:
        value = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        return _check(
            "policy role names", FAIL, f"{PAYMENT_POLICY_KEY} v{version} is unreadable: {exc}",
        )

    names = _published_role_names(value)
    if not names:
        return _check(
            "policy role names", FAIL,
            f"{PAYMENT_POLICY_KEY} v{version} names no role at all",
        )
    unknown = [name for name in names if not roles.is_known_role(name)]
    if unknown:
        return _check(
            "policy role names", FAIL,
            f"{PAYMENT_POLICY_KEY} v{version} names {sorted(set(unknown))}, "
            f"which the ladder does not know",
        )
    unranked = [name for name in names if roles.role_rank(name) is None]
    if unranked:
        return _check(
            "policy role names", FAIL,
            f"{PAYMENT_POLICY_KEY} v{version} names {sorted(set(unranked))}, "
            f"which cannot decide anything",
        )
    ladder = value.get("ladder")
    if isinstance(ladder, list):
        try:
            resolved = roles.approval_ladder(ladder)
        except DomainError as exc:
            return _check(
                "policy role names", FAIL,
                f"{PAYMENT_POLICY_KEY} v{version}: {exc.code} — {exc}",
            )
        return _check(
            "policy role names", OK, f"v{version} ladder {list(resolved)}",
        )
    return _check(
        "policy role names", OK,
        f"v{version} names {[roles.canonical_role(name) for name in names]}",
    )



#: D17: the platform may be ready while a *tenant* is not. Every ACTIVE agency must have filled
#: the settings only it can know (payment provider, WhatsApp number, PBJT source, on-call) and
#: must have at least one SELLABLE property with an ACTIVE unit. This is checked here, not only in
#: tests, because the question "may this agency take real guests" is asked at deploy time.
def check_tenant_readiness(conn: Any) -> Check:
    """Every ACTIVE agency must be ready: settings filled, a sellable property, an active unit."""
    agencies = [
        row[0]
        for row in conn.execute(
            "SELECT agency_id FROM agencies WHERE status = 'ACTIVE' ORDER BY agency_id"
        ).fetchall()
    ]
    if not agencies:
        return _check("tenant readiness", WARN, "no ACTIVE agency to check yet")
    unready: list[str] = []
    for agency_id in agencies:
        staffing = AGENCY_ROLES.AgencyRoles(conn, clock=SystemClock(), agency_id=str(agency_id))
        unstaffed = staffing.unstaffed_ladder_roles()
        # Staffing comes first on purpose: a tenant with no holder for the money ladder cannot
        # approve a payment at all, and the report is truncated, so the worst blocker must survive.
        blockers = (
            ["money ladder has no holder for: " + ", ".join(unstaffed)] if unstaffed else []
        )
        blockers.extend(
            SETTINGS.readiness_for(conn, str(agency_id), clock=SystemClock()).blockers
        )
        if blockers:
            unready.append(f"{agency_id}: " + "; ".join(blockers))
    if unready:
        return _check("tenant readiness", FAIL, " | ".join(unready)[:600])
    return _check("tenant readiness", OK, f"{len(agencies)} agency(ies) ready")


def check_demo_data(conn: Any) -> Check:
    """Demo rows must never reach production: they carry example guest names.

    Checked by identity prefix *and* by name, so a seeder that forgets the
    convention is still caught by the other half.
    """

    prefixed = 0
    for table, column in (("properties", "property_id"), ("units", "unit_id"),
                          ("bookings", "booking_id"), ("cases", "case_id")):
        prefixed += int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?",
            (f"{DEMO_ID_PREFIX}%",),
        ).fetchone()[0])
    named = int(conn.execute(
        "SELECT COUNT(*) FROM properties WHERE lower(name) LIKE '%contoh%' "
        "OR lower(name) LIKE '%demo%'"
    ).fetchone()[0])
    if prefixed or named:
        return _check(
            "demo data", FAIL,
            f"demo rows present ({prefixed} prefixed id(s), {named} example name(s)) — "
            "seed belongs on a demo machine, not in production",
        )
    return _check("demo data", OK, "no demo marker found")


def check_backup(path: Optional[Path]) -> Check:
    if path is None:
        return _check("backup", WARN, "not checked (--backup not given)")
    if not path.exists():
        return _check("backup", FAIL, f"backup not found: {path}")
    size = path.stat().st_size
    if size < BACKUP_MIN_BYTES:
        return _check("backup", FAIL, f"backup is suspiciously small ({size} bytes)")
    import time

    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    if age_hours > BACKUP_MAX_AGE_HOURS:
        return _check("backup", FAIL, f"backup is {age_hours:.1f}h old (max {BACKUP_MAX_AGE_HOURS}h)")
    return _check("backup", OK, f"{size // 1024} KiB, {age_hours:.1f}h old")


# --------------------------------------------------------------------------
# Served ports (--host-check)
# --------------------------------------------------------------------------
#: The loopback ports the units in deploy/systemd-user declare.
DASHBOARD_PORT = 8085
MARKETING_PORT = 8090
PROBE_TIMEOUT_SECONDS = 5.0
#: Headers the public site must always send. A missing one is a regression.
MARKETING_REQUIRED_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}
HONEYPOT_FIELD = "company"


def _get(port: int, path: str = "/") -> Any:
    # Scheme, host and port are built here from an int and a literal; ``path`` cannot
    # change the scheme, so ``file://`` is unreachable (rule 11: measure the real path).
    return urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected - fixed http://127.0.0.1 prefix
        f"http://127.0.0.1:{port}{path}", timeout=PROBE_TIMEOUT_SECONDS
    )


def _post(port: int, path: str, fields: dict[str, str]) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS)  # nosemgrep: dynamic-urllib-use-detected - fixed http://127.0.0.1 prefix


def _primary_route_address() -> Optional[str]:
    """The address the kernel would use to leave this host.

    A UDP connect sends no packet, but it makes the kernel pick a source
    address — which is exactly the address a peer on the same network would
    use to reach this machine. ``getsockname`` therefore reveals it without
    waiting for a network round trip.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("198.51.100.1", 9))  # TEST-NET-2, documentation range
            address = str(sock.getsockname()[0])
    except OSError:
        return None
    return address if address and not address.startswith("127.") else None


def _interface_addresses() -> list[str]:
    """Every non-loopback IPv4 address on this host, from ``ip``; [] when unavailable."""
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=15
        )
    except OSError:  # pragma: no cover - no iproute2
        return []
    if result.returncode != 0:
        return []
    # ``ip`` lists the loopback address too; probing 127.0.0.1 would "succeed"
    # against every correctly bound service and fail this check for no reason.
    return [a for a in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", result.stdout) if not a.startswith("127.")]


def _non_loopback_addresses() -> list[str]:
    """This host's own non-loopback IPv4 addresses, for an exposure probe."""
    addresses: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:  # pragma: no cover - resolver problem
        infos = []
    for info in infos:
        address = str(info[4][0])
        if not address.startswith("127."):
            addresses.add(address)
    primary = _primary_route_address()
    if primary:
        addresses.add(primary)
    addresses.update(a for a in _interface_addresses() if not a.startswith("127."))
    return sorted(addresses)


def _tcp_open(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(PROBE_TIMEOUT_SECONDS)
        return sock.connect_ex((address, port)) == 0


def check_served_ports(
    dashboard_port: int = DASHBOARD_PORT, marketing_port: int = MARKETING_PORT
) -> list[Check]:
    """Probe the running units the way a visitor, and then a bot, would.

    Read-only by construction: the only POST is the honeypot, which the intake
    rejects before the rate limiter and before any database write, so this can
    run against the live site without creating a case.
    """
    checks: list[Check] = []

    try:
        with _get(marketing_port) as response:
            status = response.status
            headers = {name.lower(): value for name, value in response.headers.items()}
            body = response.read()
    except urllib.error.HTTPError as exc:
        checks.append(_check("marketing site", FAIL, f"GET / -> {exc.code} on 127.0.0.1:{marketing_port}"))
    except OSError as exc:
        checks.append(
            _check(
                "marketing site", FAIL,
                f"127.0.0.1:{marketing_port} did not answer ({exc.__class__.__name__})"
                " — is villa-marketing.service running?",
            )
        )
    else:
        missing = [
            name for name, needle in MARKETING_REQUIRED_HEADERS.items()
            if needle not in headers.get(name.lower(), "")
        ]
        if missing:
            checks.append(_check("marketing site", FAIL, f"GET / is missing header(s): {', '.join(missing)}"))
        else:
            checks.append(_check("marketing site", OK, f"GET / -> {status}, {len(body) // 1024} KiB, security headers present"))

        trap = {
            "name": "Preflight Probe",
            "email": "probe@example.invalid",
            "message": "Honeypot probe from scripts/preflight.py — must not create a case.",
            "form_token": uuid.uuid4().hex,
            HONEYPOT_FIELD: "Filled by a bot",
            "property_ref": "",
        }
        try:
            with _post(marketing_port, "/inquiry", trap) as response:
                checks.append(_check("inquiry write path", FAIL, f"a honeypot submission was accepted ({response.status})"))
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                checks.append(_check("inquiry write path", OK, "honeypot rejected with 400, nothing written"))
            else:
                checks.append(_check("inquiry write path", FAIL, f"honeypot submission -> {exc.code} (expected 400)"))
        except OSError as exc:
            checks.append(_check("inquiry write path", FAIL, f"/inquiry did not answer ({exc.__class__.__name__})"))

    try:
        with _get(dashboard_port) as response:
            checks.append(_check("dashboard gate", FAIL, f"the dashboard answered {response.status} without a token"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            checks.append(_check("dashboard gate", OK, "GET / without a token -> 401 (locked)"))
        else:
            checks.append(_check("dashboard gate", WARN, f"GET / without a token -> {exc.code}, not the expected 401"))
    except OSError as exc:
        checks.append(
            _check(
                "dashboard gate", FAIL,
                f"127.0.0.1:{dashboard_port} did not answer ({exc.__class__.__name__})"
                " — is villa-dashboard.service running?",
            )
        )

    addresses = _non_loopback_addresses()
    if not addresses:
        checks.append(_check("loopback only", WARN, "no non-loopback address on this host to probe"))
    else:
        exposed = [
            f"{address}:{port}"
            for address in addresses
            for port in (marketing_port, dashboard_port)
            if _tcp_open(address, port)
        ]
        if exposed:
            checks.append(_check("loopback only", FAIL, "answer(s) off loopback: " + ", ".join(exposed)))
        else:
            checks.append(
                _check(
                    "loopback only", OK,
                    f"nothing answers off loopback ({len(addresses) * 2} address:port pairs tried)",
                )
            )
    return checks


# --------------------------------------------------------------------------
# Business decisions
# --------------------------------------------------------------------------
def parse_decisions(path: Path) -> tuple[list[Check], list[str]]:
    if not path.exists():
        return [_check("go-live decisions", FAIL, f"decisions file not found: {path}")], []
    open_blocking: list[str] = []
    decided = 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = DECISION_ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        number, blocking, status = cells[0], cells[2].upper(), cells[3].upper()
        if number == "D#" or set(status) <= {"-"}:
            continue
        total += 1
        if status == "DECIDED":
            decided += 1
        elif blocking == "BLOCKING":
            open_blocking.append(number)
    if total == 0:
        return [_check("go-live decisions", FAIL, "no decision rows found in the file")], []
    if open_blocking:
        return [
            _check(
                "go-live decisions", FAIL,
                f"{len(open_blocking)} BLOCKING decision(s) still OPEN: {', '.join(open_blocking)}",
            )
        ], open_blocking
    return [
        _check("go-live decisions", OK, f"{decided}/{total} row(s) decided, none blocking OPEN")
    ], []


# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("VILLA_DB", "data/villa.db"))
    parser.add_argument("--anchor-dir", default=os.environ.get("VILLA_ANCHOR_DIR"))
    parser.add_argument("--decisions", default=str(REPO / "docs/GO_LIVE_DECISIONS.md"))
    parser.add_argument("--backup", default=None)
    parser.add_argument(
        "--env", default="VILLA_DB,VILLA_ANCHOR_DIR",
        help="comma separated environment variables that must be set",
    )
    parser.add_argument(
        "--host-check",
        action="store_true",
        help="also probe the running units on loopback (read-only, honeypot only)",
    )
    parser.add_argument("--marketing-port", type=int, default=MARKETING_PORT)
    parser.add_argument("--dashboard-port", type=int, default=DASHBOARD_PORT)
    args = parser.parse_args(argv)

    db = Path(args.db)
    anchor_dir = Path(args.anchor_dir) if args.anchor_dir else None

    checks: list[Check] = []
    checks.append(check_twin_parity())
    checks.extend(check_scans())
    checks.append(check_git_tree_clean())
    checks.append(check_tracked_env_file())
    checks.append(check_env_present([n for n in args.env.split(",") if n]))

    if db.exists():
        checks.extend(check_database(db, anchor_dir))
    else:
        checks.append(_check("database", FAIL, f"database not found: {db}"))

    backup = Path(args.backup) if args.backup else None
    checks.append(check_backup(backup))

    decision_checks, open_blocking = parse_decisions(Path(args.decisions))
    checks.extend(decision_checks)

    if args.host_check:
        checks.extend(check_served_ports(args.dashboard_port, args.marketing_port))

    print("=== GO-LIVE PREFLIGHT ===")
    print(f"repository : {REPO}")
    print(f"database   : {db}")
    print(f"anchors    : {anchor_dir if anchor_dir else '(not checked)'}")
    if args.host_check:
        print(f"ports      : dashboard {args.dashboard_port}, marketing {args.marketing_port}")
    print()
    for check in checks:
        print(f"{check.verdict:<5} {check.name:<22} {check.detail}")
    failures = [c for c in checks if c.verdict == FAIL]
    warnings = [c for c in checks if c.verdict == WARN]
    print()
    if failures:
        print(f"VERDICT: NO-GO — {len(failures)} failed check(s), {len(warnings)} warning(s)")
        if open_blocking:
            print(
                "         the system may be ready while the business is not: "
                "finish docs/GO_LIVE_DECISIONS.md first"
            )
        return 1
    print(f"VERDICT: GO — all checks passed ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

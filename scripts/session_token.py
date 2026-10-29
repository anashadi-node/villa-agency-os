#!/usr/bin/env python3
"""Operator sessions for the dashboard: enrol TOTP, log in, revoke, list, rotate.

Why this exists: the dashboard authenticated callers with one static token per
role. A shared role token cannot say *who* acted, cannot expire, and cannot be
withdrawn for one person without locking everyone out. This CLI mints a
per-person session through :mod:`identity` (ADR-0005): the role stays on the
server, the token expires (idle + absolute), and every login/refusal is written
to ``auth_events``.

What this does **not** prove: there is no password factor in the platform. A
session is opened by asserting the identity and, for MFA roles, presenting a TOTP
code. Whoever can run this command can already read the database, so the gain is
accountability and revocation, not protection from a local attacker. Say that in
the runbook instead of implying more.

The TOTP secret never enters the database: only a ``secret_ref`` does, resolved
from ``VILLA_TOTP_SECRET_<REF>`` in the environment.

Usage:
    python3 scripts/session_token.py enroll  --db data/villa.db --identity alice
    VILLA_TOTP_SECRET_ALICE=... python3 scripts/session_token.py confirm \
        --db data/villa.db --identity alice --code 123456
    VILLA_TOTP_SECRET_ALICE=... python3 scripts/session_token.py login \
        --db data/villa.db --identity alice --role owner --code 123456
    python3 scripts/session_token.py list    --db data/villa.db
    python3 scripts/session_token.py revoke  --db data/villa.db --identity alice
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import identity as I  # noqa: E402
import roles  # noqa: E402
import totp as T  # noqa: E402
from primitives import SystemClock  # noqa: E402


def env_name(secret_ref: str) -> str:
    return "VILLA_TOTP_SECRET_" + secret_ref.upper().replace("-", "_").replace(".", "_")


def connect(db: str) -> sqlite3.Connection:
    path = Path(db)
    if not path.is_file():
        raise SystemExit(f"REFUSED: database tidak ditemukan: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def service(conn: sqlite3.Connection, agency: str) -> I.SessionService:
    return I.SessionService(conn, clock=SystemClock(), agency_id=agency)


def canonical_role(value: str) -> str:
    role = roles.canonical_role(value)
    if role is None:
        raise SystemExit(
            f"REFUSED: peran tidak dikenal: {value!r}; pilihan: {', '.join(roles.ROLE_IDS)}"
        )
    return role


def cmd_enroll(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    svc = service(conn, args.agency)
    ref = args.secret_ref or args.identity
    factor_id = svc.enroll_totp(args.identity, secret_ref=ref)
    secret = T.generate_secret()
    uri = (
        f"otpauth://totp/Villa:{quote(args.identity)}"
        f"?secret={secret}&issuer=Villa&digits=6&period=30"
    )
    print(f"faktor     : {factor_id} (status DISABLED sampai dikonfirmasi)")
    print(f"secret_ref : {ref}")
    print(f"variabel   : {env_name(ref)}=<secret>   (mode 0600, jangan masuk git)")
    print(f"otpauth    : {uri}")
    print()
    print("PERINGATAN: secret di atas hanya ditampilkan sekali. Simpan di aplikasi")
    print("authenticator / password manager sekarang; CLI tidak menyimpannya.")
    print("Langkah berikut: set variabel env lalu jalankan 'confirm' dengan kode 6 digit.")
    conn.commit()
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    svc = service(conn, args.agency)
    factor_id = svc.confirm_totp(args.identity, args.code)
    conn.commit()
    print(f"faktor aktif: {factor_id}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    svc = service(conn, args.agency)
    role = canonical_role(args.role)
    result = svc.open_session(args.identity, role, mfa_code=args.code)
    conn.commit()
    session = result.session
    if args.json:
        print(json.dumps({
            "session_id": session.session_id,
            "identity": session.identity,
            "role": session.role,
            "issued_at": session.issued_at,
            "idle_expires_at": session.idle_expires_at,
            "expires_at": session.expires_at,
            "mfa_satisfied_at": session.mfa_satisfied_at,
            "token": result.token,
        }, ensure_ascii=False))
        return 0
    print(f"identitas  : {session.identity} ({session.role})")
    print(f"sesi       : {session.session_id}")
    print(f"idle s/d   : {session.idle_expires_at}")
    print(f"absolut s/d: {session.expires_at}")
    print(f"mfa        : {session.mfa_satisfied_at or 'tidak diperlukan untuk peran ini'}")
    print()
    print("token (tampil sekali, kirim sebagai 'Authorization: Bearer ...'):")
    print(result.token)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT session_id, identity, role, issued_at, idle_expires_at, expires_at,"
        " revoked_at FROM auth_sessions WHERE agency_id = ? ORDER BY issued_at DESC",
        (args.agency,),
    ).fetchall()
    if not rows:
        print("tidak ada sesi")
        return 0
    for row in rows:
        state = "REVOKED" if row["revoked_at"] else "aktif"
        print(
            f"{row['session_id']:<28} {row['identity']:<14} {row['role']:<20}"
            f" {state:<8} idle={row['idle_expires_at']} absolut={row['expires_at']}"
        )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    svc = service(conn, args.agency)
    if args.session_id:
        svc.revoke(args.session_id, reason=args.reason)
        conn.commit()
        print(f"dicabut: {args.session_id}")
        return 0
    rows = conn.execute(
        "SELECT session_id FROM auth_sessions WHERE agency_id = ? AND identity = ?"
        " AND revoked_at IS NULL",
        (args.agency, args.identity),
    ).fetchall()
    if not rows:
        print("tidak ada sesi aktif untuk identitas itu")
        return 0
    for row in rows:
        svc.revoke(str(row["session_id"]), reason=args.reason)
    conn.commit()
    print(f"dicabut: {len(rows)} sesi milik {args.identity}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    svc = service(conn, args.agency)
    result = svc.rotate(args.token)
    conn.commit()
    print(f"sesi    : {result.session.session_id}")
    print(f"idle s/d: {result.session.idle_expires_at}")
    print("token baru (tampil sekali):")
    print(result.token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sesi operator dashboard (ADR-0005).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True, help="berkas database (VILLA_DB)")
    common.add_argument("--agency", default="agency_default")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll", parents=[common], help="daftarkan faktor TOTP (status DISABLED)")
    enroll.add_argument("--identity", required=True)
    enroll.add_argument("--secret-ref", default=None)
    enroll.set_defaults(func=cmd_enroll)

    confirm = sub.add_parser("confirm", parents=[common], help="aktifkan faktor dengan kode 6 digit")
    confirm.add_argument("--identity", required=True)
    confirm.add_argument("--code", required=True)
    confirm.set_defaults(func=cmd_confirm)

    login = sub.add_parser("login", parents=[common], help="buka sesi, cetak token sekali")
    login.add_argument("--identity", required=True)
    login.add_argument("--role", required=True)
    login.add_argument("--code", default=None)
    login.add_argument("--json", action="store_true")
    login.set_defaults(func=cmd_login)

    listing = sub.add_parser("list", parents=[common], help="daftar sesi")
    listing.set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", parents=[common], help="cabut satu sesi atau semua sesi identitas")
    revoke.add_argument("--session-id", default=None)
    revoke.add_argument("--identity", default=None)
    revoke.add_argument("--reason", default="revoked by operator")
    revoke.set_defaults(func=cmd_revoke)

    rotate = sub.add_parser("rotate", parents=[common], help="tukar token sesi (token lama langsung mati)")
    rotate.add_argument("--token", required=True)
    rotate.set_defaults(func=cmd_rotate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "revoke" and not (args.session_id or args.identity):
        print("REFUSED: beri --session-id atau --identity", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except I.IdentityError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

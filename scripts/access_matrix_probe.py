"""Cetak matriks akses dashboard per peran lewat HTTP sungguhan.

Untuk setiap peran di ``dashboard.ROLES``: buka koneksi sqlite di dalam thread
pelayan (sqlite3 terikat ke thread pembuatnya), bangun app dengan token peran,
sajikan di port acak loopback, minta setiap rute, lalu catat status.

Kontrol negatif ikut dijalankan: tanpa token (harus 401), token palsu (harus 401),
dan token peran rendah + header peran palsu ke /finance (harus 403 — peran tidak
boleh dinaikkan lewat header klien).

Pemakaian:
    python3 scripts/access_matrix_probe.py --db /tmp/salinan.db [--json out.json]

Selalu jalankan pada SALINAN database, bukan ``data/villa.db``.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dashboard as D  # noqa: E402
from primitives import FrozenClock  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

ROUTES = {
    "command_center": "/",
    "cases": "/cases",
    "inventory": "/inventory",
    "finance": "/finance",
    "operations": "/operations",
    "approvals": "/approvals",
    "properties": "/properties",
    "marketing": "/marketing",
    "channels": "/channels",
    "shadow_eval": "/shadow_eval",
    "outbox": "/outbox",
    "audit": "/audit",
}


def probe_role(db: str, role: str, tokens: dict) -> dict[str, int]:
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        conn = connect_sqlite(db)
        handler = D.create_app(
            conn,
            clock=FrozenClock.at("2026-10-25T08:00:00+00:00"),
            actor=f"probe-{role}",
            actor_role=role,
            tokens=tokens,
        )
        server = D.serve(handler, "127.0.0.1", 0)
        box["server"] = server
        ready.set()
        try:
            server.serve_forever()
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if not ready.wait(15):
        raise RuntimeError(f"server untuk peran {role} tidak siap")

    server = box["server"]
    port = server.server_address[1]
    out: dict[str, int] = {}

    def get(path: str, headers: dict | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            return conn.getresponse().status
        finally:
            conn.close()

    try:
        for page, path in ROUTES.items():
            out[page] = get(path, {"Authorization": f"Bearer tok-{role}"})

        low = next(r for r in D.ROLES if r not in ("owner", "finance", "gm"))
        out["_tanpa_token"] = get("/")
        out["_token_palsu"] = get("/", {"Authorization": "Bearer token-palsu"})
        out["_eskalasi_header"] = get(
            "/finance",
            {
                "Authorization": f"Bearer tok-{low}",
                "X-Role": "owner",
                "X-Actor-Role": "owner",
                "X-Dashboard-Role": "owner",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Matriks akses dashboard per peran (HTTP).")
    parser.add_argument("--db", required=True, help="salinan database sqlite")
    parser.add_argument("--json", default=None, help="tulis hasil ke berkas JSON")
    args = parser.parse_args(argv)

    if pathlib.Path(args.db).resolve() == (ROOT / "data" / "villa.db").resolve():
        print("DITOLAK: jalankan pada salinan, bukan data/villa.db", file=sys.stderr)
        return 1

    tokens = {f"tok-{role}": (f"probe-{role}", role) for role in D.ROLES}
    # Log permintaan dashboard ikut stdout; matriks harus tetap bisa diparse.
    with contextlib.redirect_stdout(sys.stderr):
        matrix = {role: probe_role(args.db, role, tokens) for role in D.ROLES}

    pages = list(ROUTES)
    width = max(len(p) for p in pages) + 2
    print(f"{'halaman':<{width}}" + "".join(f"{r:>12}" for r in D.ROLES))
    for page in pages:
        print(f"{page:<{width}}" + "".join(f"{matrix[r][page]:>12}" for r in D.ROLES))
    print()

    mismatches = 0
    for role in D.ROLES:
        for page in pages:
            expected = 200 if role in D.PAGE_ROLES[page] else 403
            if matrix[role][page] != expected:
                mismatches += 1
    controls = {
        "tanpa_token": sorted({m["_tanpa_token"] for m in matrix.values()}),
        "token_palsu": sorted({m["_token_palsu"] for m in matrix.values()}),
        "eskalasi_header": sorted({m["_eskalasi_header"] for m in matrix.values()}),
    }
    print(f"sel diuji: {len(D.ROLES) * len(pages)} · tidak cocok dengan PAGE_ROLES: {mismatches}")
    print(f"kontrol: {controls}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(matrix, indent=2), "utf-8")
        print(f"json: {args.json}")

    ok = (
        mismatches == 0
        and controls["tanpa_token"] == [401]
        and controls["token_palsu"] == [401]
        and controls["eskalasi_header"] == [403]
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

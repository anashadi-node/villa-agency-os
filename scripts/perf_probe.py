"""Measure what one request-at-a-time actually costs, and where the ceiling is.

The scaling claims in ``docs/SCALING_AND_UPGRADE.md`` are numbers from this
script, not opinions. Run it against your own database before you size anything:

    python3 scripts/perf_probe.py --db data/villa.db

It reports, for the public marketing surface:
* per-page latency (median/min/max over 9 samples after 3 warm-ups);
* the wall time for 20 simultaneous visitors;
* the implied ceiling (``1 / latency``) — the arithmetic limit of a
  single-threaded server, so you can tell "we are at the limit" apart from
  "something is wrong".
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import marketing_site as SITE  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402


def start(db: str) -> tuple[Any, str]:
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        # A sqlite connection belongs to the thread that opened it, so the
        # server thread owns both the connection and the site object.
        conn = connect_sqlite(db)
        site = SITE.MarketingSite(conn)
        box["server"] = SITE.serve(SITE.build_handler(site), "127.0.0.1", 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    if not ready.wait(10):
        raise SystemExit("server never became ready")
    port = box["server"].server_address[1]
    return box["server"], f"http://127.0.0.1:{port}"


def get(base: str, path: str) -> float:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(base + path, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:  # a 404 is still a measured round trip
        exc.read()
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Latency and throughput probe")
    parser.add_argument("--db", default=str(REPO / "data" / "villa.db"))
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--paths", default="/,/villas")
    args = parser.parse_args(argv)

    server, base = start(args.db)
    measured: dict[str, list[float]] = {}
    try:
        for path in [p for p in args.paths.split(",") if p]:
            for _ in range(3):
                get(base, path)
            samples = [get(base, path) for _ in range(9)]
            measured[path] = samples
            print(
                f"{path:14s} median {statistics.median(samples) * 1000:7.2f} ms"
                f"   min {min(samples) * 1000:7.2f}   max {max(samples) * 1000:7.2f}"
            )

        n = max(1, args.concurrency)
        latencies: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            elapsed = get(base, "/")
            with lock:
                latencies.append(elapsed)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        wall = time.perf_counter() - started
        print(
            f"\n{n} simultaneous GET / : wall {wall * 1000:7.1f} ms"
            f"  -> {n / wall:6.1f} req/s"
            f"   slowest client {max(latencies) * 1000:.1f} ms"
        )
        home = measured.get("/")
        if home:
            print(f"implied ceiling (1 / median latency): {1 / statistics.median(home):6.1f} req/s")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

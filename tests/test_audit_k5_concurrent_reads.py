"""Audit 2026-11-13, finding K5: an idle socket must not delay anyone else.

Measured before the fix: with one connection open and silent, the next request to
the marketing site and to the dashboard each waited 20.0 s (the read timeout).
Reading now happens on bounded worker threads; handlers still run one at a time
under ``request_lock``. These tests hold both halves: concurrency for reads,
serialization for handlers, plus the ``HEAD`` and ``/healthz`` routes added with it.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from types import SimpleNamespace

import dashboard as D
import marketing_site as M
from schema_loader import connect_sqlite


def _start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def _stub_site():
    return SimpleNamespace(trusted_proxy=False, conn=connect_sqlite(":memory:"))


def test_twenty_idle_sockets_do_not_delay_a_real_request():
    handler = type("H", (M._MarketingRequestHandler,), {"timeout": 5, "site": _stub_site()})
    server = M.serve(handler, "127.0.0.1", 0)
    port = _start(server)
    idle = []
    try:
        for _ in range(20):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")  # never finished
            idle.append(sock)
        started = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        elapsed = time.monotonic() - started
        assert response.status == 200
        assert response.read() == b"ok\n"
        assert elapsed < 1.0, f"a real request waited {elapsed:.2f}s behind idle sockets"
    finally:
        for sock in idle:
            sock.close()
        server.shutdown()
        server.server_close()


def test_handlers_never_overlap():
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    class Slow(M._MarketingRequestHandler):
        timeout = 5
        site = _stub_site()

        def do_GET(self):  # noqa: N802
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.02)
            with guard:
                active["now"] -= 1
            self.send_response(204)
            self.end_headers()

    server = M.serve(Slow, "127.0.0.1", 0)
    port = _start(server)
    try:
        def hit():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/")
            conn.getresponse().read()

        threads = [threading.Thread(target=hit) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert active["max"] == 1, "two handler bodies ran at the same time"
    finally:
        server.shutdown()
        server.server_close()


def test_head_answers_with_get_headers_and_no_body():
    handler = type("H", (M._MarketingRequestHandler,), {"timeout": 5, "site": _stub_site()})
    server = M.serve(handler, "127.0.0.1", 0)
    port = _start(server)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("HEAD", "/healthz")
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Length") == "3"
        assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()


def test_server_bounds_its_reader_threads():
    assert isinstance(D.MAX_CONCURRENT_CONNECTIONS, int) and 1 <= D.MAX_CONCURRENT_CONNECTIONS <= 256
    server = D.serve(D.DashboardRequestHandler, "127.0.0.1", 0)
    try:
        assert isinstance(server.request_lock, type(threading.Lock()))
        assert issubclass(server.RequestHandlerClass, D.SerializedDispatchMixin)
    finally:
        server.server_close()

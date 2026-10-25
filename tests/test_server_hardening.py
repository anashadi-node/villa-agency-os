"""Server hardening: a stalled socket must never own the process.

Both HTTP surfaces here are deliberately single-threaded (``http.server``, one
connection at a time). That choice is defensible for an internal dashboard and a
small public site, but it has one consequence that is not optional: **every read
from the socket needs a deadline**. Without one, a single client that sends half
a request line and then goes quiet blocks every other visitor indefinitely —
an outage that needs no credentials, no exploit, and about three lines of shell.

These tests hold that line:
* the production timeout exists, is bounded, and is not ``None``;
* a half-sent request is actually dropped (behaviour, not just an attribute);
* a well-behaved request is *not* delayed by the deadline (negative control —
  if this failed, the fix would have broken normal traffic);
* the dashboard's response headers carry framing protection, which
  ``default-src 'none'`` does not imply.
"""

from __future__ import annotations

import io
import socket
import threading
import time
from http.server import HTTPServer
from types import SimpleNamespace

import pytest

import dashboard as D
import marketing_site as M

#: Long enough for a real client, short enough to keep the suite fast.
TEST_TIMEOUT = 0.5

#: The favicon route answers 204 without touching the site — but the request
#: log reads ``site.trusted_proxy``, so the stub still needs that one attribute.
STUB_SITE = SimpleNamespace(trusted_proxy=False)


def _serve(handler: type) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stall(port: int) -> socket.socket:
    """Send the start of a request and never finish it."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
    return sock


# -- the deadline exists ------------------------------------------------------ #


def test_marketing_handler_has_a_bounded_read_timeout():
    assert M.CONNECTION_TIMEOUT_SECONDS is not None
    assert 0 < M.CONNECTION_TIMEOUT_SECONDS <= 60
    assert M._MarketingRequestHandler.timeout == M.CONNECTION_TIMEOUT_SECONDS


def test_dashboard_handler_has_a_bounded_read_timeout():
    assert D.CONNECTION_TIMEOUT_SECONDS is not None
    assert 0 < D.CONNECTION_TIMEOUT_SECONDS <= 60
    assert D.DashboardRequestHandler.timeout == D.CONNECTION_TIMEOUT_SECONDS


# -- the deadline actually fires ---------------------------------------------- #


@pytest.mark.parametrize(
    "base, handler_name",
    [
        (M, "_MarketingRequestHandler"),
        (D, "DashboardRequestHandler"),
    ],
)
def test_a_half_sent_request_is_dropped(base, handler_name):
    """The socket must be closed by the server, not held until the client leaves."""
    handler = type(
        "StalledHandler",
        (getattr(base, handler_name),),
        {"timeout": TEST_TIMEOUT, "site": None, "dashboard": None},
    )
    server = _serve(handler)
    try:
        port = server.server_address[1]
        stalled = _stall(port)
        started = time.monotonic()
        stalled.settimeout(5)
        assert stalled.recv(1024) == b"", "server answered a request it never fully received"
        elapsed = time.monotonic() - started
        stalled.close()
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed < 3, f"the stall was held for {elapsed:.1f}s — the deadline did not fire"


def test_a_stalled_client_cannot_hold_the_server_forever():
    """After the stall dies, the *next* client is served — recovery, not just refusal."""
    handler = type(
        "StalledHandler",
        (M._MarketingRequestHandler,),
        {"timeout": TEST_TIMEOUT, "site": STUB_SITE},
    )
    server = _serve(handler)
    try:
        port = server.server_address[1]
        stalled = _stall(port)
        # The stalled socket owns the only serving slot for one timeout window;
        # then it is dropped and the next request is answered.
        second = socket.create_connection(("127.0.0.1", port), timeout=6)
        second.sendall(b"GET /favicon.ico HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        deadline = time.monotonic() + 5
        reply = b""
        while time.monotonic() < deadline and b"204" not in reply:
            chunk = second.recv(4096)
            if not chunk:
                break
            reply += chunk
        second.close()
        stalled.close()
    finally:
        server.shutdown()
        server.server_close()
    assert b"204" in reply, f"the server never recovered: {reply[:120]!r}"


# -- negative control: a normal request is not slowed down -------------------- #


def test_a_prompt_request_is_served_immediately():
    """If this ever fails, the deadline is being applied to healthy traffic."""
    handler = type(
        "PromptHandler",
        (M._MarketingRequestHandler,),
        {"timeout": TEST_TIMEOUT, "site": STUB_SITE},
    )
    server = _serve(handler)
    try:
        port = server.server_address[1]
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.sendall(b"GET /favicon.ico HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        reply = sock.recv(4096)
        elapsed = time.monotonic() - started
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
    assert b"204" in reply
    assert elapsed < TEST_TIMEOUT, f"a healthy request waited {elapsed:.2f}s"


# -- negative control: the detector can actually fail ------------------------- #


def test_without_a_deadline_the_socket_would_stay_held():
    """Proves the test above is not passing for free.

    A handler with ``timeout=None`` (the stdlib default, i.e. the pre-fix state)
    keeps the connection open while the client says nothing. If this ever starts
    failing, the stall test is no longer measuring what it claims to measure.
    """
    handler = type(
        "NoDeadlineHandler",
        (M._MarketingRequestHandler,),
        {"timeout": None, "site": STUB_SITE},
    )
    server = _serve(handler)
    stalled = None
    try:
        stalled = _stall(server.server_address[1])
        time.sleep(1.0)
        stalled.settimeout(0.5)
        with pytest.raises(socket.timeout):
            stalled.recv(1)  # still open: nothing closed it
    finally:
        if stalled is not None:
            stalled.close()  # EOF unblocks the reading thread before shutdown
        server.shutdown()
        server.server_close()


# -- the listen queue absorbs bursts ------------------------------------------ #


def test_a_burst_of_connections_is_not_dropped_and_retried():
    """The pre-fix queue depth (5) turned 20 visitors into ~1.2 s of SYN retries.

    A dropped SYN is retried by the kernel one second later, so the discriminator
    is the *slowest single* connection, not the total: with a deep queue no
    connection waits anywhere near a retransmit timer.
    """
    handler = type(
        "BurstHandler",
        (M._MarketingRequestHandler,),
        {"timeout": 5, "site": STUB_SITE},
    )
    server = M.serve(handler, "127.0.0.1", 0)
    assert isinstance(server, D.ListeningHTTPServer)
    assert server.request_queue_size >= 64
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    latencies: list[float] = []
    replies: list[bytes] = []
    lock = threading.Lock()

    def one() -> None:
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(b"GET /favicon.ico HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        with lock:
            latencies.append(time.monotonic() - started)
            replies.append(data)

    threads = [threading.Thread(target=one) for _ in range(20)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    finally:
        server.shutdown()
        server.server_close()

    assert len(replies) == 20, f"only {len(replies)} of 20 connections completed"
    assert all(b"204" in r for r in replies)
    assert max(latencies) < 0.5, (
        f"slowest connection took {max(latencies)*1000:.0f} ms — "
        "that is the SYN-retransmit signature of a queue that is too shallow"
    )


# -- header injection (CVE-2026-0865) ---------------------------------------- #


def test_a_header_value_cannot_smuggle_a_second_header():
    """The interpreter we ship on still splits on CRLF; our guard must trip first.

    Proven in the same session: ``send_header("X-Probe", "safe\\r\\nInjected: yes")``
    on 3.12.3 produced two headers. Every header in this codebase is a literal, so
    the flaw is unreachable today — this test is what keeps it that way.
    """

    class _Recorder:
        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []

        def send_header(self, keyword: str, value: str) -> None:
            self.seen.append((keyword, value))

    probe = type("_Probe", (D.SafeHeaderMixin, _Recorder), {})()
    probe.send_header("Cache-Control", "no-store")
    assert probe.seen == [("Cache-Control", "no-store")]

    for bad in ("safe\r\nInjected: yes", "safe\nInjected: yes", "x\x00y"):
        with pytest.raises(ValueError):
            probe.send_header("X-Probe", bad)
    with pytest.raises(ValueError):
        probe.send_header("X-Bad\r\nInjected", "value")
    assert len(probe.seen) == 1, "a refused header must not reach the wire"


def test_both_handlers_inherit_the_header_guard():
    assert issubclass(D.DashboardRequestHandler, D.SafeHeaderMixin)
    assert issubclass(M._MarketingRequestHandler, D.SafeHeaderMixin)


def test_the_public_site_does_not_advertise_the_interpreter_version():
    # "Python/3.12.3" in a Server header tells an attacker which stdlib CVEs apply.
    # BaseHTTPRequestHandler.version_string() is literally server_version + " " + sys_version.
    handler = M._MarketingRequestHandler
    assert handler.sys_version == ""
    assert "Python/" not in f"{handler.server_version} {handler.sys_version}"


# -- framing protection ------------------------------------------------------- #


class _Recorder(D.DashboardRequestHandler):
    """Captures the headers ``_write`` emits without needing a live dashboard."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.wfile = io.BytesIO()

    def send_response(self, code, message=None) -> None:  # noqa: N802 - stdlib shape
        self.status = code

    def send_header(self, keyword, value) -> None:  # noqa: N802 - stdlib shape
        self.sent.append((keyword, value))

    def end_headers(self) -> None:  # noqa: N802 - stdlib shape
        pass


def test_dashboard_refuses_to_be_framed():
    handler = _Recorder.__new__(_Recorder)
    handler.sent = []
    handler.wfile = io.BytesIO()
    handler._write(SimpleNamespace(status=200, body="<html></html>"))
    headers = dict(handler.sent)
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "base-uri 'none'" in headers["Content-Security-Policy"]
    # The pre-existing protections must survive the edit.
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"

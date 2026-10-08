"""``scripts/preflight.py --host-check``: the probe must pass a healthy pair and
fail each real defect.

The flag used to be advertised in ``--help`` and then do nothing, so these tests
exist to keep it honest: every check is exercised both ways — a correct service
passes it, and a plausible-looking but wrong service fails it. The honeypot is
the only request that may touch a live site, which is why the write-path check
uses it instead of a real inquiry.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import marketing_site as M  # noqa: E402
import preflight as PF  # noqa: E402
from primitives import IdFactory  # noqa: E402
from schema_loader import connect_sqlite  # noqa: E402

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - http.server API
        pass


class _LockedDashboard(_Quiet):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _OpenDashboard(_Quiet):
    def do_GET(self):  # noqa: N802
        body = b"dashboard without a gate"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BareSite(_Quiet):
    """Answers 200 with no security headers — a plausible but wrong public site."""

    def do_GET(self):  # noqa: N802
        body = b"<html><body>villas</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _HoneypotSwallowingSite(_Quiet):
    """Sends the right headers but accepts a bot submission."""

    def _respond(self, body: bytes) -> None:
        self.send_response(200)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._respond(b"<html><body>villas</body></html>")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._respond(b"thank you")


@contextmanager
def _serve(handler_class, host: str = "127.0.0.1"):
    server = HTTPServer((host, 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _serve_marketing(host: str = "127.0.0.1"):
    """The real public site, wired the way the unit file wires it."""
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        conn = connect_sqlite(":memory:")
        site = M.MarketingSite(conn, ids=IdFactory(seed="pf"), agency_id="agency_default")
        box["server"] = M.serve(M.build_handler(site), host, 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(15), "the marketing server never became ready"
    try:
        yield int(box["server"].server_address[1])
    finally:
        box["server"].shutdown()
        box["server"].server_close()
        thread.join(timeout=5)


def _verdict(checks, name: str) -> str:
    matches = [c for c in checks if c.name == name]
    assert matches, f"no check named {name!r} in {[c.name for c in checks]}"
    return matches[0].verdict


def _detail(checks, name: str) -> str:
    return next(c.detail for c in checks if c.name == name)


class TestHealthyServices:
    def test_the_real_site_and_a_locked_dashboard_pass(self):
        with _serve_marketing() as marketing_port, _serve(_LockedDashboard) as dash_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "marketing site") == PF.OK
        assert _verdict(checks, "inquiry write path") == PF.OK
        assert _verdict(checks, "dashboard gate") == PF.OK
        assert "honeypot rejected" in _detail(checks, "inquiry write path")

    def test_a_loopback_bound_service_is_not_reachable_elsewhere(self):
        with _serve_marketing() as marketing_port, _serve(_LockedDashboard) as dash_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        # With no non-loopback address to try the check can only warn; when this
        # host has one, a loopback-bound server must be invisible on it.
        assert _verdict(checks, "loopback only") in (PF.OK, PF.WARN)


class TestDetectedDefects:
    def test_a_site_without_security_headers_fails(self):
        with _serve(_BareSite) as marketing_port, _serve(_LockedDashboard) as dash_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "marketing site") == PF.FAIL
        assert "missing header(s)" in _detail(checks, "marketing site")
        assert "Content-Security-Policy" in _detail(checks, "marketing site")

    def test_a_site_that_swallows_the_honeypot_fails(self):
        with _serve(_HoneypotSwallowingSite) as marketing_port, _serve(_LockedDashboard) as dash_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "marketing site") == PF.OK  # headers are fine
        assert _verdict(checks, "inquiry write path") == PF.FAIL
        assert "accepted" in _detail(checks, "inquiry write path")

    def test_a_dashboard_that_answers_without_a_token_fails(self):
        with _serve(_OpenDashboard) as dash_port, _serve_marketing() as marketing_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "dashboard gate") == PF.FAIL
        assert "without a token" in _detail(checks, "dashboard gate")

    def test_a_service_that_answers_off_loopback_fails(self, monkeypatch):
        with _serve_marketing() as marketing_port, _serve(_LockedDashboard) as dash_port:
            monkeypatch.setattr(PF, "_non_loopback_addresses", lambda: ["127.0.0.1"])
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "loopback only") == PF.FAIL
        assert f"127.0.0.1:{marketing_port}" in _detail(checks, "loopback only")

    def test_a_port_with_nothing_listening_fails(self):
        with _serve_marketing() as marketing_port:
            checks = PF.check_served_ports(_free_port(), marketing_port)
        assert _verdict(checks, "dashboard gate") == PF.FAIL
        assert "did not answer" in _detail(checks, "dashboard gate")


class TestAddressDiscovery:
    def test_the_loopback_address_ip_reports_is_never_probed(self, monkeypatch):
        """``ip -4 -o addr show`` lists 127.0.0.1; probing it would always "succeed".

        Regression: the first version kept it, so the exposure check failed
        against a perfectly loopback-bound service.
        """
        output = (
            "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
            "2: eth0  inet 192.0.2.10/24 brd 192.0.2.255 scope global eth0\n"
            "3: docker0 inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, output, ""),
        )
        addresses = PF._interface_addresses()
        assert addresses == ["192.0.2.10", "172.17.0.1"]

    def test_no_address_available_warns_instead_of_passing_silently(self, monkeypatch):
        monkeypatch.setattr(PF, "_non_loopback_addresses", lambda: [])
        with _serve_marketing() as marketing_port, _serve(_LockedDashboard) as dash_port:
            checks = PF.check_served_ports(dash_port, marketing_port)
        assert _verdict(checks, "loopback only") == PF.WARN
        assert "no non-loopback address" in _detail(checks, "loopback only")


class TestWiring:
    def test_the_flag_is_no_longer_decorative(self, tmp_path, monkeypatch, capsys):
        """--host-check must actually run the port checks in main()."""
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            PF, "check_served_ports",
            lambda dashboard=0, marketing=0: calls.append((dashboard, marketing))
            or [PF._check("marketing site", PF.OK, "stubbed")],
        )
        # Keep main() from shelling out to the repo scans.
        monkeypatch.setattr(PF, "check_twin_parity", lambda: PF._check("schema twin parity", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_scans", lambda: [])
        monkeypatch.setattr(PF, "check_git_tree_clean", lambda: PF._check("git tree", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_tracked_env_file", lambda: PF._check(".env handling", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_env_present", lambda names: PF._check("environment", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_database", lambda db, anchor: [])
        monkeypatch.setattr(PF, "check_backup", lambda path: PF._check("backup", PF.OK, "stub"))
        monkeypatch.setattr(PF, "parse_decisions", lambda path: ([PF._check("go-live decisions", PF.OK, "stub")], []))

        db = tmp_path / "villa.db"
        db.write_bytes(b"x")
        code = PF.main(["--db", str(db), "--host-check", "--marketing-port", "9123", "--dashboard-port", "9124"])
        out = capsys.readouterr().out
        assert code == 0
        assert calls == [(9124, 9123)]
        assert "ports      : dashboard 9124, marketing 9123" in out
        assert "marketing site" in out

    def test_without_the_flag_no_port_is_probed(self, tmp_path, monkeypatch, capsys):
        calls: list = []
        monkeypatch.setattr(
            PF, "check_served_ports",
            lambda *a, **k: calls.append(a) or [],
        )
        monkeypatch.setattr(PF, "check_twin_parity", lambda: PF._check("schema twin parity", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_scans", lambda: [])
        monkeypatch.setattr(PF, "check_git_tree_clean", lambda: PF._check("git tree", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_tracked_env_file", lambda: PF._check(".env handling", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_env_present", lambda names: PF._check("environment", PF.OK, "stub"))
        monkeypatch.setattr(PF, "check_database", lambda db, anchor: [])
        monkeypatch.setattr(PF, "check_backup", lambda path: PF._check("backup", PF.OK, "stub"))
        monkeypatch.setattr(PF, "parse_decisions", lambda path: ([PF._check("go-live decisions", PF.OK, "stub")], []))

        db = tmp_path / "villa.db"
        db.write_bytes(b"x")
        code = PF.main(["--db", str(db)])
        out = capsys.readouterr().out
        assert code == 0
        assert calls == []
        assert "ports      :" not in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

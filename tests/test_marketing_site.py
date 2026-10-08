"""Tests for the public marketing website (guest surface, no auth).

Covers: public read model (sellable gating, tenant scope, XSS escaping),
inquiry intake write path (guest master + case spine, PII policy, idempotent
replay, honeypot, rate limit, same-origin), serving behaviour (headers, 404s,
read-only crawl), and the static dump.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from http.server import HTTPServer
from uuid import uuid4

import pytest

import marketing_site as M
from cases import DEFAULT_SLA_POLICY_BY_CHANNEL, SLA_POLICIES
from primitives import IdFactory, sha256_hex, to_iso
from schema_loader import connect_sqlite

# --------------------------------------------------------------------------- #
# Seed: two sellable villas (one with an XSS payload in the name), one that
# must never appear (NOT_SELLABLE), one unit under MAINTENANCE.
# --------------------------------------------------------------------------- #


def _seed(conn, clock):
    stamp = to_iso(clock.now())
    # The schema already seeds 'agency_default'; keep the test's stamp update.
    conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_default', 'Agency', 'ACTIVE', ?, ?)"
        " ON CONFLICT(agency_id) DO UPDATE SET updated_at = excluded.updated_at",
        (stamp, stamp),
    )
    rows = [
        # property_id, name, region, province, type, status
        ("prop-1", "Villa <script>alert('xss')</script>", "Ubud", "Bali", "villa", "SELLABLE"),
        ("prop-2", "Joglo Merapi Private Pool", "Kaliurang", "DI Yogyakarta", "villa", "SELLABLE"),
        ("prop-3", "Hidden Backoffice Villa", "Ubud", "Bali", "villa", "NOT_SELLABLE"),
        ("prop-4", "Review Pending Villa", "Canggu", "Bali", "villa", "SELLABLE_WITH_REVIEW"),
    ]
    for property_id, name, region, province, ptype, status in rows:
        conn.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES (?, 'agency_default', ?, ?, ?, ?, 'Asia/Makassar', ?, 'ACTIVATED', ?, ?)",
            (property_id, name, ptype, region, province, status, stamp, stamp),
        )
    unit_rows = [
        # unit_id, property_id, name, type, bed, bath, base, max, status
        ("unit-1", "prop-1", "Main Villa", "entire_villa", 3, 2, 6, 8, "ACTIVE"),
        ("unit-2", "prop-1", "Garden Suite", "suite", 1, 1, 2, 2, "ACTIVE"),
        ("unit-3", "prop-1", "Pool Suite", "suite", 1, 1, 2, 2, "MAINTENANCE"),
        ("unit-4", "prop-2", "Joglo House", "entire_villa", 4, 3, 8, 10, "ACTIVE"),
    ]
    for unit_id, property_id, name, utype, bed, bath, base, mx, status in unit_rows:
        conn.execute(
            "INSERT INTO units (unit_id, agency_id, property_id, name, unit_type, bedrooms,"
            " bathrooms, base_capacity, max_guests, status, created_at, updated_at)"
            " VALUES (?, 'agency_default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (unit_id, property_id, name, utype, bed, bath, base, mx, status, stamp, stamp),
        )


def _make_site(clock, agency_id="agency_default", **kwargs):
    conn = connect_sqlite(":memory:")
    _seed(conn, clock)
    site = M.MarketingSite(
        conn, clock=clock, ids=IdFactory(seed="mkt"), agency_id=agency_id, **kwargs
    )
    return conn, site


def _seeded_db(tmp_path, clock):
    """Seed a file-backed DB from the main thread, close it, return the path."""
    db_path = tmp_path / "marketing.db"
    if not db_path.exists():
        conn = connect_sqlite(db_path)
        _seed(conn, clock)
        conn.close()
    return db_path


@contextmanager
def _live_site(tmp_path, clock, *, seed_data=True):
    """A real HTTP server over a file-backed DB, in the dashboard-test pattern.

    The DB is seeded from the main thread and closed; the server thread opens
    its own connection (sqlite connections are thread-bound). Verification
    happens through a separate reader connection.
    """
    db_path = _seeded_db(tmp_path, clock) if seed_data else (tmp_path / "empty.db")
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        conn = connect_sqlite(db_path)
        site = M.MarketingSite(conn, clock=clock, ids=IdFactory(seed="mkt"))
        box["site"] = site
        box["server"] = M.serve(M.build_handler(site), "127.0.0.1", 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(15), "server never became ready"
    try:
        yield box["site"], f"http://127.0.0.1:{box['server'].server_address[1]}"
    finally:
        box["server"].shutdown()
        box["server"].server_close()
        thread.join(timeout=5)


def _site_and_handler(clock, **kwargs):
    _conn, site = _make_site(clock, **kwargs)
    return site, M.build_handler(site)


# --------------------------------------------------------------------------- #
# In-process HTTP client (same pattern as the dashboard tests)
# --------------------------------------------------------------------------- #


@contextmanager
def _http_server(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _get(base, path, host=None):
    request = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _post(base, path, fields, *, origin=None, host_header=None):
    data = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    if host_header is not None:
        headers["Host"] = host_header
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _valid_fields(**overrides):
    fields = {
        "name": "Budi Santoso",
        "email": "budi@example.com",
        "message": "We are two families looking for a villa near Ubud in July.",
        "form_token": uuid4().hex,
        M.HONEYPOT_FIELD: "",
        "property_ref": "",
    }
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------- #
# Public read model
# --------------------------------------------------------------------------- #


class TestPublicReadModel:
    def test_sellable_only_and_active_units_only(self, clock):
        _conn, site = _make_site(clock)
        villas = site.read_model.sellable_villas()
        names = [v["name"] for v in villas]
        assert "Hidden Backoffice Villa" not in names
        assert "Review Pending Villa" not in names
        assert len(villas) == 2

    def test_province_filter(self, clock):
        _conn, site = _make_site(clock)
        bali = site.read_model.sellable_villas(province="Bali")
        assert [v["name"] for v in bali] == ["Villa <script>alert('xss')</script>"]
        jogja = site.read_model.sellable_villas(province="DI Yogyakarta")
        assert [v["name"] for v in jogja] == ["Joglo Merapi Private Pool"]

    def test_tenant_scope(self, clock):
        _conn, site = _make_site(clock)
        other = M.PublicReadModel(_conn, "agency_other")
        assert other.sellable_villas() == []
        assert other.villa("prop-1") is None
        assert other.active_units("prop-1") == []

    def test_unknown_villa_returns_none(self, clock):
        _conn, site = _make_site(clock)
        assert site.villa_html("prop-missing") is None

    def test_availability_hint_is_safe_without_live_evidence(self, clock):
        _conn, site = _make_site(clock)
        hint = site.read_model.availability_hint("prop-1", to_iso(clock.now()))
        assert "live availability is not published here" in hint
        assert "checks with the villa owner" in hint

    def test_availability_hint_uses_current_evidence_without_promising_instant_confirm(self, clock):
        conn, site = _make_site(clock)
        now = clock.now()
        conn.execute(
            "INSERT INTO availability_evidence (evidence_id, agency_id, unit_id,"
            " evidence_level, source_type, source_ref, captured_at, valid_until,"
            " result, restrictions, occupancy_ranges, can_quote, can_hold, can_confirm,"
            " notes, created_at)"
            " VALUES ('ev_mkt_1', 'agency_default', 'unit-1', 'B', 'manual_ops',"
            " 'ops-note-1', ?, ?, 'AVAILABLE', NULL, '[]', 1, 1, 0, NULL, ?)",
            (to_iso(now), to_iso(now + timedelta(hours=2)), to_iso(now)),
        )
        hint = site.read_model.availability_hint("prop-1", to_iso(now))
        assert "likely available for a short hold" in hint
        assert "instant" not in hint.lower()

    def test_invalid_slug_returns_none_not_crash(self, clock):
        _conn, site = _make_site(clock)
        assert site.villa_html("../etc/passwd") is None


# --------------------------------------------------------------------------- #
# Rendered pages
# --------------------------------------------------------------------------- #


class TestRenderedPages:
    def test_home_escapes_xss_and_shows_sellable(self, clock):
        _conn, site = _make_site(clock)
        html = site.home_html()
        assert "Villa &lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html
        assert "<script>alert('xss')</script>" not in html
        assert "Joglo Merapi Private Pool" in html
        assert "Hidden Backoffice Villa" not in html

    def test_collection_shows_count_and_escapes(self, clock):
        _conn, site = _make_site(clock)
        html = site.villas_html()
        assert "2 villas" in html
        assert "Villa &lt;script&gt;" in html
        assert "Hidden Backoffice Villa" not in html

    def test_destination_pages(self, clock):
        _conn, site = _make_site(clock)
        bali = site.destination_html("bali")
        assert bali is not None and "Ubud" in bali and "Kaliurang" not in bali
        jogja = site.destination_html("yogyakarta")
        assert jogja is not None and "Joglo Merapi Private Pool" in jogja
        assert site.destination_html("lombok") is None

    def test_detail_shows_active_units_only(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-1")
        assert "Main Villa" in html and "Garden Suite" in html
        assert "Pool Suite" not in html  # MAINTENANCE unit stays internal
        assert "Availability hint:" in html
        assert "checks with the villa owner" in html

    def test_inquiry_form_has_honeypot_and_token(self, clock):
        _conn, site = _make_site(clock)
        html = site.inquiry_form_html()
        assert M.HONEYPOT_FIELD in html
        assert 'name="form_token"' in html
        assert 'action="/inquiry"' in html

    def test_inquiry_form_prefill_for_sellable_villa(self, clock):
        _conn, site = _make_site(clock)
        html = site.inquiry_form_html("prop-1")
        assert "You are asking about: Villa" in html

    def test_inquiry_form_ignores_unknown_property(self, clock):
        _conn, site = _make_site(clock)
        html = site.inquiry_form_html("prop-missing")
        assert "You are asking about" not in html

    def test_owner_page_is_public_read_only_copy(self, clock):
        conn, site = _make_site(clock)
        before = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
        html = site.owners_html()
        after = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
        assert "Partner with a local villa team" in html
        assert "Owner-approved quotes" in html
        assert "/inquiry" in html
        assert after == before


# --------------------------------------------------------------------------- #
# Intake (unit level)
# --------------------------------------------------------------------------- #


class TestIntake:
    def test_valid_submission_creates_guest_and_case(self, clock):
        conn, site = _make_site(clock)
        result = site.intake.submit(
            form=_valid_fields(),
            remote_addr="203.0.113.10",
            rate_limiter=M.SlidingWindowRateLimiter(
                clock, max_events=5, window_seconds=600
            ),
            expected_origin_host="villa.example",
            origin_header=None,
        )
        assert result["reference"].startswith("INQ-2026-")
        guest = conn.execute(
            "SELECT full_name, email FROM guests WHERE agency_id='agency_default'"
        ).fetchone()
        assert guest["email"] == "budi@example.com"
        assert "Budi Santoso" in guest["full_name"]
        row = conn.execute(
            "SELECT channel, status, sla_policy FROM cases"
        ).fetchone()
        assert row["channel"] == "website"
        assert row["status"] == "NEW"
        assert row["sla_policy"] == "web_v1"

    def test_sla_deadlines_follow_kernel_policy(self, clock):
        conn, site = _make_site(clock)
        result = site.intake.submit(
            form=_valid_fields(),
            remote_addr="203.0.113.11",
            rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
            expected_origin_host=None,
            origin_header=None,
        )
        row = conn.execute(
            "SELECT first_response_due_at, quote_due_at FROM cases WHERE case_id=?",
            (result["case_id"],),
        ).fetchone()
        policy = SLA_POLICIES[DEFAULT_SLA_POLICY_BY_CHANNEL["website"]]
        assert row["first_response_due_at"] == to_iso(clock.now() + timedelta(minutes=policy.first_response_minutes))
        assert row["quote_due_at"] == to_iso(clock.now() + timedelta(minutes=policy.quote_minutes))

    def test_message_body_is_redacted_and_digest_matches_raw(self, clock):
        conn, site = _make_site(clock)
        site.intake.submit(
            form=_valid_fields(message="Contact me at secret.person@example.com about July"),
            remote_addr="203.0.113.12",
            rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
            expected_origin_host=None,
            origin_header=None,
        )
        row = conn.execute(
            "SELECT content_class, body_redacted, body_sha256 FROM case_messages"
        ).fetchone()
        assert row["content_class"] == "P2"
        assert "secret.person@example.com" not in (row["body_redacted"] or "")
        assert "[EMAIL]" in (row["body_redacted"] or "")
        raw = M.compose_inquiry_body(
            "Budi Santoso",
            "Contact me at secret.person@example.com about July",
        )
        assert row["body_sha256"] == sha256_hex(raw)

    def test_duplicate_email_reuses_guest(self, clock):
        conn, site = _make_site(clock)
        limiter = M.SlidingWindowRateLimiter(clock, max_events=10, window_seconds=600)
        first = site.intake.submit(
            form=_valid_fields(name="Budi Satu"),
            remote_addr="203.0.113.13",
            rate_limiter=limiter,
            expected_origin_host=None,
            origin_header=None,
        )
        second = site.intake.submit(
            form=_valid_fields(name="Budi Dua"),
            remote_addr="203.0.113.13",
            rate_limiter=limiter,
            expected_origin_host=None,
            origin_header=None,
        )
        guests = conn.execute("SELECT COUNT(*) AS n FROM guests").fetchone()["n"]
        assert guests == 1
        assert first["guest_id"] == second["guest_id"]

    def test_idempotent_replay_same_token_same_case(self, clock):
        conn, site = _make_site(clock)
        limiter = M.SlidingWindowRateLimiter(clock, max_events=10, window_seconds=600)
        fields = _valid_fields()
        first = site.intake.submit(
            form=fields, remote_addr="203.0.113.14", rate_limiter=limiter,
            expected_origin_host=None, origin_header=None,
        )
        replay = site.intake.submit(
            form=fields, remote_addr="203.0.113.14", rate_limiter=limiter,
            expected_origin_host=None, origin_header=None,
        )
        cases = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
        assert cases == 1
        assert first["case_id"] == replay["case_id"]
        assert first["reference"] == replay["reference"]

    def test_honeypot_rejected(self, clock):
        conn, site = _make_site(clock)
        with pytest.raises(M.SpamRejected):
            site.intake.submit(
                form=_valid_fields(**{M.HONEYPOT_FIELD: "Spam Co"}),
                remote_addr="203.0.113.15",
                rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
                expected_origin_host=None,
                origin_header=None,
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0

    def test_rate_limit_blocks_sixth_submission(self, clock):
        conn, site = _make_site(clock)
        limiter = M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600)
        for i in range(5):
            site.intake.submit(
                form=_valid_fields(email=f"guest{i}@example.com"),
                remote_addr="203.0.113.16",
                rate_limiter=limiter,
                expected_origin_host=None,
                origin_header=None,
            )
        with pytest.raises(M.RateLimited):
            site.intake.submit(
                form=_valid_fields(email="guest5@example.com"),
                remote_addr="203.0.113.16",
                rate_limiter=limiter,
                expected_origin_host=None,
                origin_header=None,
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 5

    def test_cross_origin_post_rejected(self, clock):
        conn, site = _make_site(clock)
        with pytest.raises(M.FormInvalid):
            site.intake.submit(
                form=_valid_fields(),
                remote_addr="203.0.113.17",
                rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
                expected_origin_host="villa.example",
                origin_header="https://evil.example",
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0

    def test_invalid_email_rejected(self, clock):
        conn, site = _make_site(clock)
        with pytest.raises(M.FormInvalid):
            site.intake.submit(
                form=_valid_fields(email="not-an-email"),
                remote_addr="203.0.113.18",
                rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
                expected_origin_host=None,
                origin_header=None,
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0

    def test_bad_token_rejected(self, clock):
        conn, site = _make_site(clock)
        with pytest.raises(M.FormInvalid):
            site.intake.submit(
                form=_valid_fields(form_token="short"),
                remote_addr="203.0.113.19",
                rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
                expected_origin_host=None,
                origin_header=None,
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0

    def test_missing_message_rejected(self, clock):
        conn, site = _make_site(clock)
        with pytest.raises(M.FormInvalid):
            site.intake.submit(
                form=_valid_fields(message="short"),
                remote_addr="203.0.113.20",
                rate_limiter=M.SlidingWindowRateLimiter(clock, max_events=5, window_seconds=600),
                expected_origin_host=None,
                origin_header=None,
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0


# --------------------------------------------------------------------------- #
# Live server behaviour
# --------------------------------------------------------------------------- #


class TestServedSite:
    def test_all_pages_serve_and_headers(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            for path in ("/", "/villas", "/villas/prop-1", "/villas/prop-2",
                         "/destinations/bali", "/destinations/yogyakarta", "/inquiry"):
                status, headers, html = _get(base, path)
                assert status == 200
                assert headers["X-Content-Type-Options"] == "nosniff"
                assert headers["X-Frame-Options"] == "DENY"
                assert headers["Content-Security-Policy"] == M.CONTENT_SECURITY_POLICY
            status, headers, _html = _get(base, "/")
            assert headers["Cache-Control"] == "public, max-age=300"

    def test_404_unknown_villa_and_route(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            for path in ("/villas/prop-missing", "/villas/..%2Fetc", "/nope", "/villas/prop-3"):
                status, _headers, html = _get(base, path)
                assert status == 404
                assert "Villa Agency" in html  # branded error page

    def test_not_sellable_villa_404s(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            status, _headers, _html = _get(base, "/villas/prop-3")
            assert status == 404

    def test_owner_page_served_over_http(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            status, headers, html = _get(base, "/owners")
            assert status == 200
            assert headers.get("X-Frame-Options") == "DENY"
            assert "Partner with a local villa team" in html
            assert "Hidden Backoffice Villa" not in html

    def test_xss_name_is_escaped_over_http(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            _status, _headers, html = _get(base, "/villas")
            assert "Villa &lt;script&gt;" in html
            assert "<script>alert('xss')</script>" not in html

    def test_post_valid_form_creates_case_and_shows_reference(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            fields = _valid_fields()
            status, headers, html = _post(base, "/inquiry", fields)
            assert status == 200
            assert "INQ-2026-" in html
            assert headers.get("X-Robots-Tag") == "noindex"
            reader = M.connect_sqlite(_seeded_db(tmp_path, clock))
            row = reader.execute("SELECT channel, reference FROM cases").fetchone()
            assert row["channel"] == "website"
            assert row["reference"] in html
            reader.close()

    def test_post_replay_is_idempotent(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            fields = _valid_fields()
            first = _post(base, "/inquiry", fields)
            again = _post(base, "/inquiry", fields)
            assert first[0] == 200 and again[0] == 200
            reader = M.connect_sqlite(_seeded_db(tmp_path, clock))
            cases = reader.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
            reader.close()
            assert cases == 1

    def test_post_honeypot_400_no_case(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            status, _headers, _html = _post(
                base, "/inquiry", _valid_fields(**{M.HONEYPOT_FIELD: "Bot"})
            )
            assert status == 400
            reader = M.connect_sqlite(_seeded_db(tmp_path, clock))
            cases = reader.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
            reader.close()
            assert cases == 0

    def test_post_rate_limit_429(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            for i in range(5):
                status, _h, _html = _post(
                    base, "/inquiry", _valid_fields(email=f"rl{i}@example.com")
                )
                assert status == 200
            status, _h, _html = _post(
                base, "/inquiry", _valid_fields(email="rl5@example.com")
            )
            assert status == 429
            reader = M.connect_sqlite(_seeded_db(tmp_path, clock))
            cases = reader.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
            reader.close()
            assert cases == 5

    def test_post_invalid_body_400(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            status, _h, _html = _post(base, "/inquiry", _valid_fields(email="bad"))
            assert status == 400

    def test_post_unknown_path_405(self, clock, tmp_path):
        with _live_site(tmp_path, clock) as (_site, base):
            status, _h, _html = _post(base, "/villas", _valid_fields())
            assert status == 405

    def test_get_crawl_changes_no_rows(self, clock):
        _conn, site = _make_site(clock)
        def counts():
            tables = ["cases", "case_messages", "guests", "bookings", "holds"]
            return {t: _conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
        before = counts()
        for html in (
            site.home_html(), site.villas_html(), site.destination_html("bali"),
            site.destination_html("yogyakarta"), site.villa_html("prop-1"),
            site.owners_html(), site.inquiry_form_html("prop-1"),
            site.error_html(404, "missing"),
        ):
            assert html
        assert counts() == before

    def test_dump_writes_pages_without_secrets(self, clock, tmp_path):
        _conn, site = _make_site(clock)
        written = site.dump(tmp_path / "site")
        # index + villas + owners + inquiry + 2 destinations + 2 sellable villa pages
        assert len(written) == 8
        assert (tmp_path / "site" / "index.html").exists()
        assert (tmp_path / "site" / "owners.html").exists()
        assert (tmp_path / "site" / "inquiry.html").exists()
        assert (tmp_path / "site" / "destinations" / "bali.html").exists()
        assert (tmp_path / "site" / "villas" / "prop-1.html").exists()
        (tmp_path / "site" / "index.html").read_text().index('href="owners.html"')
        (tmp_path / "site" / "index.html").read_text().index('href="inquiry.html"')
        for html in (p.read_text() for p in tmp_path.rglob("*.html")):
            assert ('href="/' not in html), "dump must contain no site-absolute links"
        blob = "\n".join(p.read_text() for p in tmp_path.rglob("*.html"))
        assert "Hidden Backoffice Villa" not in blob
        assert "API_KEY" not in blob and "SECRET" not in blob


# --------------------------------------------------------------------------- #
# Villa photo placeholders (assets/photos/ embedded as base64 data URIs)
# --------------------------------------------------------------------------- #


def _fake_villa(name: str, **overrides) -> dict:
    villa = dict(
        property_id="prop-x",
        province="Bali",
        region="Canggu",
        name=name,
        property_type="private_villa",
        bedrooms_min=3,
        bedrooms_max=4,
        sleeps_max=8,
        active_units=1,
    )
    villa.update(overrides)
    return villa


class TestVillaPhotos:
    """Photos stay self-contained (data URIs) and degrade to a labelled placeholder."""

    def test_matched_villa_card_embeds_photo_and_no_external_url(self):
        card = str(M._card_html(_fake_villa("Villa Canggu Garden Retreat")))
        assert "data:image/jpeg;base64," in card
        assert "photo placeholder" not in card
        assert "http://" not in card and "https://" not in card

    def test_unmatched_villa_card_falls_back_to_placeholder(self):
        card = str(M._card_html(_fake_villa("Some Unlisted Homestay")))
        assert "photo placeholder" in card
        assert "data:image/jpeg;base64," not in card

    def test_keyword_match_is_case_insensitive_and_covers_merapi(self):
        assert M._villa_photo_uri("Villa ULUWATU Cliffside", size="detail")
        assert M._villa_photo_uri("Joglo Volcano Merapi Residence")
        assert M._villa_photo_uri("Nothing Matches Here") is None

    def test_missing_photo_file_returns_none(self):
        assert M._photo_data_uri("card/does_not_exist.jpg") is None

    def test_home_hero_embeds_background_photo(self, clock):
        _conn, site = _make_site(clock)
        html = site.home_html()
        assert "hero-band{background-image:" in html
        assert "data:image/jpeg;base64," in html

    def test_villa_detail_has_labelled_ambience_strip(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-1")
        assert html is not None
        assert "photo-strip" in html
        assert "Illustrative resort imagery" in html
        # The label must state the strip is NOT the property being sold.
        assert "not this villa" in html


class TestVillaShowcasePage:
    """The villa page carries per-villa copy that never overclaims the property."""

    def test_matched_villa_page_shows_area_note_style_and_disclosure(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-2")  # "Joglo Merapi Private Pool" -> joglo stem
        assert html is not None
        assert "About this villa" in html
        assert "slopes of Mount Merapi" in html
        assert "private villa, let as a whole or by unit" in html
        assert "not verified features of this villa" in html

    def test_unmatched_villa_page_falls_back_without_claiming_features(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-1")  # name carries an XSS payload, no photo keyword
        assert html is not None
        assert "This villa is in Ubud, Bali." in html
        assert "not verified features of this villa" in html

    def test_highlights_report_only_stored_numbers(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-1")
        assert html is not None
        # unit-1 (3 bedrooms, max 8) + unit-2 (1 bedroom, max 2); unit-3 is MAINTENANCE.
        assert "2 units in this villa" in html
        assert "1\u20133 bedrooms per unit" in html
        assert "Sleeps up to 8" in html
        assert "3 units" not in html

    def test_page_carries_no_external_url(self, clock):
        _conn, site = _make_site(clock)
        html = site.villa_html("prop-2")
        assert html is not None
        assert "http://" not in html and "https://" not in html

    def test_lede_and_copy_drop_a_region_repeated_inside_its_province(self, clock):
        """Operator free text can repeat the province inside the region — prose must not."""
        conn, site = _make_site(clock)
        conn.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES ('prop-5', 'agency_default', 'Unlisted Highland Stay', 'villa',"
            " 'yogyakarta', 'DI Yogyakarta', 'Asia/Jakarta', 'SELLABLE', 'ACTIVATED', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        html = site.villa_html("prop-5")
        assert html is not None
        assert "This villa is in DI Yogyakarta." in html
        assert "yogyakarta, DI Yogyakarta" not in html
        assert "<p class=\"lede\">DI Yogyakarta · villa</p>" in html

    def test_card_location_does_not_leave_a_stray_separator(self, clock):
        """A dropped region takes its separator with it, on the card as well as the lede."""
        conn, site = _make_site(clock)
        conn.execute(
            "INSERT INTO properties (property_id, agency_id, name, property_type, region,"
            " province, timezone, status, contract_pack_status, created_at, updated_at)"
            " VALUES ('prop-6', 'agency_default', 'Second Highland Stay', 'villa',"
            " 'yogyakarta', 'DI Yogyakarta', 'Asia/Jakarta', 'SELLABLE', 'ACTIVATED', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        listing = site.villas_html()
        assert '<div class="card-region">DI Yogyakarta</div>' in listing
        assert "DI Yogyakarta · <" not in listing
        assert "\u00b7 \u00b7" not in listing

    def test_story_block_escapes_hostile_copy(self):
        """The copy is plain text from a data source; the render step must escape it."""
        hostile = M.Showcase.VillaShowcase(
            photo_stem=None,
            area_note="<script>alert('xss')</script>",
            style_note="ok",
            highlights=("<img src=x onerror=alert(1)>",),
        )
        story = str(M._villa_story_html(hostile))
        highlights = str(M._highlights_html(hostile))
        assert "<script>" not in story
        assert "&lt;script&gt;" in story
        assert "<img" not in highlights
        assert "&lt;img" in highlights


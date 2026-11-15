"""Tests for the browser session behind ``/unlock`` (ADR-0072).

The invariant under test: the cookie is a *second carrier* for the same token, never a
weaker one. No carrier at all is still 401, a wrong token still sets nothing, and a
cookie the server cannot trust is ignored instead of repaired. The last test records
the limitation the ADR states out loud — ``/lock`` forgets the cookie in the browser,
so a copied cookie keeps working until the token is rotated.
"""

from __future__ import annotations

import http.client
import threading
import urllib.parse
from contextlib import contextmanager

import pytest

import dashboard as D
from primitives import FrozenClock
from schema_loader import connect_sqlite

TOKEN = "tok-owner-1"
TOKENS = {TOKEN: ("agent-owner-1", "owner")}
COOKIE_NAME = D.SESSION_COOKIE


class Reply:
    """One HTTP reply, with the pieces a header assertion needs kept apart."""

    def __init__(self, status: int, pairs: list[tuple[str, str]], body: str) -> None:
        self.status = status
        self.pairs = pairs
        self.body = body
        self.headers = {name.lower(): value for name, value in pairs}

    @property
    def set_cookie(self) -> str | None:
        return self.headers.get("set-cookie")

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


def request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    cookie: str | None = None,
    form: dict[str, str] | None = None,
    content_type: str = "application/x-www-form-urlencoded",
    extra_headers: dict[str, str] | None = None,
) -> Reply:
    """One request over a fresh connection: ``http.client`` never follows a 303."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    body: bytes | None = None
    headers: dict[str, str] = {}
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
    if token is not None:
        headers["X-Dashboard-Token"] = token
    if cookie is not None:
        headers["Cookie"] = cookie
    headers.update(extra_headers or {})
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8", errors="replace")
        return Reply(response.status, list(response.getheaders()), payload)
    finally:
        connection.close()


@contextmanager
def live_server():
    """Serve the dashboard on an ephemeral port over an empty in-memory database.

    ``sqlite3`` binds a connection to the thread that opened it, so the handler is
    built *inside* the serving thread (same reason as ``test_dashboard.live_handler``).
    """
    box: dict = {}
    ready = threading.Event()

    def run() -> None:
        connection = connect_sqlite(":memory:")
        handler = D.create_app(
            connection,
            clock=FrozenClock.at("2026-09-21T08:00:00Z"),
            actor="owner-sakti-1",
            actor_role="owner",
            tokens=TOKENS,
        )
        box["server"] = D.serve(handler, "127.0.0.1", 0)
        ready.set()
        try:
            box["server"].serve_forever()
        finally:
            connection.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(15), "server tidak pernah siap"
    server = box["server"]
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive(), "server thread tidak berhenti"


@pytest.fixture()
def port():
    with live_server() as bound:
        yield bound


# -- the unlock page -------------------------------------------------------


def test_unlock_page_is_public_and_carries_no_cookie(port: int) -> None:
    """A browser with no credential at all must be able to *see* the form."""
    reply = request(port, "GET", "/unlock")

    assert reply.status == 200
    assert reply.set_cookie is None, "halaman form tidak boleh mengirim cookie"
    assert '<form method="post" action="/unlock">' in reply.body
    assert 'name="token"' in reply.body and 'type="password"' in reply.body


def test_unlock_page_needs_no_script(port: int) -> None:
    """CSP ``default-src 'none'`` would block ``<script>`` — so there must be none."""
    reply = request(port, "GET", "/unlock")

    lowered = reply.body.lower()
    assert "<script" not in lowered
    assert "onclick" not in lowered and "onsubmit" not in lowered
    csp = reply.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp


def test_failed_unlock_is_explained_without_echoing_anything(port: int) -> None:
    reply = request(port, "GET", "/unlock?e=1")

    assert reply.status == 200
    assert 'class="err"' in reply.body
    assert "not accepted" in reply.body
    assert TOKEN not in reply.body


# -- exchanging the token for a cookie ------------------------------------


def test_valid_token_sets_one_cookie_and_redirects(port: int) -> None:
    reply = request(port, "POST", "/unlock", form={"token": TOKEN})

    assert reply.status == 303
    assert reply.location == "/"
    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}={TOKEN};")
    assert "HttpOnly" in cookie, "cookie harus HttpOnly: skrip tidak boleh membaca token"
    assert "SameSite=Strict" in cookie, "SameSite=Strict menutup CSRF lintas situs"
    assert "Path=/" in cookie
    assert f"Max-Age={D.SESSION_COOKIE_MAX_AGE_SECONDS}" in cookie
    assert "Secure" not in cookie, "server bicara HTTP polos: Secure = lockout senyap"
    assert reply.body == "", "303 tidak boleh membawa isi halaman"


@pytest.mark.parametrize(
    "submitted",
    [
        pytest.param("tok-salah", id="wrong-token"),
        pytest.param("", id="empty-token"),
        pytest.param("   ", id="blank-token"),
        pytest.param(TOKEN.upper(), id="case-differs"),
    ],
)
def test_rejected_token_sets_no_cookie(port: int, submitted: str) -> None:
    reply = request(port, "POST", "/unlock", form={"token": submitted})

    assert reply.status == 303
    assert reply.location == "/unlock?e=1"
    assert reply.set_cookie is None, "token ditolak tidak boleh mengirim cookie apa pun"
    assert reply.body == "", "token ditolak tidak boleh membaca/menampilkan halaman"


def test_stray_whitespace_around_a_pasted_token_is_trimmed(port: int) -> None:
    """The token comes from a file, so a paste carries a newline or a trailing space.

    Trimming is what makes the paste path work at all: a space cannot travel in a
    cookie, so an untrimmed token would be refused by the cookie builder instead of
    unlocking. The trimmed value is still compared byte-for-byte against the token.
    """
    reply = request(port, "POST", "/unlock", form={"token": f"  {TOKEN}\n "})

    assert reply.status == 303
    assert reply.location == "/"
    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}={TOKEN};")


def test_token_cannot_steer_the_redirect(port: int) -> None:
    """Location is fixed: a token that looks like a path cannot become an open redirect."""
    reply = request(port, "POST", "/unlock", form={"token": "//evil.example/x"})

    assert reply.status == 303
    assert reply.location == "/unlock?e=1"
    assert reply.set_cookie is None


def test_unlock_refuses_a_non_form_body(port: int) -> None:
    reply = request(
        port, "POST", "/unlock", form={"token": TOKEN}, content_type="application/json"
    )

    assert reply.status == 400
    assert reply.set_cookie is None


# -- the cookie as a carrier ---------------------------------------------


def test_cookie_alone_opens_the_dashboard(port: int) -> None:
    reply = request(port, "GET", "/", cookie=f"{COOKIE_NAME}={TOKEN}")

    assert reply.status == 200
    assert "www-authenticate" not in reply.headers


def test_header_still_works(port: int) -> None:
    reply = request(port, "GET", "/", token=TOKEN)

    assert reply.status == 200


def test_no_carrier_at_all_is_still_401(port: int) -> None:
    """The cookie must not have turned the gate into "any request gets in"."""
    reply = request(port, "GET", "/")

    assert reply.status == 401
    assert reply.headers.get("www-authenticate", "").startswith("Bearer")


def test_a_good_header_is_not_rescued_by_nothing_else(port: int) -> None:
    """A bad cookie next to a good header must not make the bad cookie acceptable."""
    reply = request(port, "GET", "/", token=TOKEN, cookie=f"{COOKIE_NAME}=tok-salah")

    assert reply.status == 200


@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param(f"{COOKIE_NAME}=tok-salah", id="unknown-but-short"),
        pytest.param(f"{COOKIE_NAME}=", id="empty-value"),
        pytest.param(f"{COOKIE_NAME}={'a' * 43}", id="right-shape-wrong-value"),
        pytest.param(f"{COOKIE_NAME}={'a' * 400}", id="longer-than-any-token"),
        pytest.param(f"other_name={TOKEN}", id="wrong-cookie-name"),
        pytest.param(f"{COOKIE_NAME}={TOKEN}-extra", id="prefix-only-match"),
    ],
)
def test_untrusted_cookie_is_401_not_repaired(port: int, cookie: str) -> None:
    reply = request(port, "GET", "/", cookie=cookie)

    assert reply.status == 401


def test_a_hostile_cookie_does_not_break_the_next_request(port: int) -> None:
    """Length guard keeps a huge cookie cheap, and the server stays usable."""
    huge = request(port, "GET", "/", cookie=f"{COOKIE_NAME}={'z' * 3000}")
    good = request(port, "GET", "/", cookie=f"{COOKIE_NAME}={TOKEN}")

    assert huge.status == 401
    assert good.status == 200


def test_duplicate_cookie_names_still_somehow_authenticate(port: int) -> None:
    """Two cookies of the same name: the first wins, and it is the real token."""
    reply = request(port, "GET", "/", cookie=f"{COOKIE_NAME}={TOKEN}; {COOKIE_NAME}=tok-salah")

    assert reply.status == 200


# -- /lock ----------------------------------------------------------------


def test_lock_forgets_the_cookie_in_the_browser(port: int) -> None:
    reply = request(port, "POST", "/lock", cookie=f"{COOKIE_NAME}={TOKEN}")

    assert reply.status == 303
    assert reply.location == "/unlock"
    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}=;")
    assert "Max-Age=0" in cookie


def test_lock_does_not_revoke_the_token_itself(port: int) -> None:
    """Documented limitation: the cookie *is* the token, so a copy still works.

    Revocation is rotating the token on the server, not calling ``/lock``. This test
    exists so nobody later "fixes" ``/lock`` into pretending it revokes.
    """
    request(port, "POST", "/lock", cookie=f"{COOKIE_NAME}={TOKEN}")
    replay = request(port, "GET", "/", cookie=f"{COOKIE_NAME}={TOKEN}")

    assert replay.status == 200


# -- the cookie value builder ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "tok-owner-1",
        "A" * 43,
        "abc_DEF-123.~+=:/",
    ],
)
def test_cookie_builder_accepts_plain_tokens(value: str) -> None:
    header = D._set_cookie_header(value)

    assert header.startswith(f"{COOKIE_NAME}={value};")
    assert "HttpOnly" in header and "SameSite=Strict" in header


@pytest.mark.parametrize(
    "value",
    [
        "tok;evil=1",
        "tok\r\nSet-Cookie: other=1",
        "tok\nX-Injected: 1",
        "tok\x00null",
        "tok with space",
        "tok,comma",
        "a" * (D.SESSION_COOKIE_MAX_BYTES + 1),
        "",
    ],
)
def test_cookie_builder_refuses_anything_unsafe(value: str) -> None:
    """Fail closed: a token a cookie cannot carry is refused, never trimmed."""
    with pytest.raises(D.InvalidInput):
        D._set_cookie_header(value)


def test_cookie_builder_can_still_clear() -> None:
    header = D._set_cookie_header("", max_age=0)

    assert header.startswith(f"{COOKIE_NAME}=;")
    assert "Max-Age=0" in header


# -- the probe in front of the gate --------------------------------------


def test_healthz_needs_no_credential(port: int) -> None:
    reply = request(port, "GET", "/healthz")

    assert reply.status == 200
    assert reply.set_cookie is None


# -- the three policies settled in ADR-0072 -------------------------------


def test_session_ttl_is_twelve_hours(port: int) -> None:
    """Policy (b): one working day. Changing it means editing the ADR, not a hunch."""
    assert D.SESSION_COOKIE_MAX_AGE_SECONDS == 12 * 60 * 60


def test_cookie_is_plain_over_plain_http(port: int) -> None:
    """Policy (a): no ``Secure`` on a loopback plain-HTTP hop — it would vanish."""
    reply = request(port, "POST", "/unlock", form={"token": TOKEN})

    cookie = reply.set_cookie or ""
    assert "Max-Age=43200" in cookie
    assert "Secure" not in cookie


def test_cookie_is_secure_when_tls_terminates_in_front(port: int) -> None:
    """Policy (a): behind a TLS proxy the cookie must carry ``Secure``."""
    reply = request(
        port,
        "POST",
        "/unlock",
        form={"token": TOKEN},
        extra_headers={"X-Forwarded-Proto": "https"},
    )

    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}={TOKEN}")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_forwarded_proto_chain_is_read_from_the_first_hop(port: int) -> None:
    """``http, https`` means the browser spoke HTTP to the edge: keep the cookie plain."""
    reply = request(
        port,
        "POST",
        "/unlock",
        form={"token": TOKEN},
        extra_headers={"X-Forwarded-Proto": "http, https"},
    )

    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}={TOKEN}")
    assert "Secure" not in cookie


def test_forwarded_proto_scheme_is_case_insensitive(port: int) -> None:
    """RFC 3986: schemes are case-insensitive, and proxies pad the value."""
    reply = request(
        port,
        "POST",
        "/unlock",
        form={"token": TOKEN},
        extra_headers={"X-Forwarded-Proto": " HTTPS , http"},
    )

    cookie = reply.set_cookie or ""
    assert cookie.startswith(f"{COOKIE_NAME}={TOKEN}")
    assert "Secure" in cookie


def test_lock_clears_with_secure_under_tls(port: int) -> None:
    """Clearing must use the same attributes, or a ``Secure`` cookie survives the lock."""
    reply = request(
        port,
        "POST",
        "/lock",
        cookie=f"{COOKIE_NAME}={TOKEN}",
        extra_headers={"X-Forwarded-Proto": "https"},
    )

    cookie = reply.set_cookie or ""
    assert "Max-Age=0" in cookie
    assert "Secure" in cookie


def test_lock_link_is_a_get_so_a_plain_anchor_works(port: int) -> None:
    """Policy (c): the footer link is a hyperlink, not a form — GET must lock too."""
    reply = request(port, "GET", "/lock", cookie=f"{COOKIE_NAME}={TOKEN}")

    assert reply.status == 303
    assert reply.location == "/unlock"
    assert "Max-Age=0" in (reply.set_cookie or "")


def test_lock_without_a_cookie_is_not_an_error(port: int) -> None:
    """A stale tab pressing Lock must land on the form, not on 401 with a token prompt."""
    reply = request(port, "GET", "/lock")

    assert reply.status == 303
    assert reply.location == "/unlock"


def test_lock_link_is_on_every_page(port: int) -> None:
    """Policy (c): reachable from the page the operator is already looking at."""
    for page in ("/", "/finance"):
        reply = request(port, "GET", page, token=TOKEN)
        assert reply.status == 200, page
        assert 'href="/lock"' in reply.body, page


def test_unlock_page_offers_no_lock_link(port: int) -> None:
    """Nothing is unlocked yet, so there is nothing to lock."""
    reply = request(port, "GET", "/unlock")

    assert reply.status == 200
    assert 'href="/lock"' not in reply.body

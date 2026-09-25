"""Wave 1-G-1 tests: identity carries its agency, and scope is enforced.

ADR-0005 ("Identity, Auth, Session, MFA, RBAC, and Scope") lists *Cross-agency
access denied* among the tests required **before** the production auth module is
built; today's dashboard token is the authority, so the token has to name the
agency it belongs to. The Master Spec puts scope in the backend/domain layer
(§18/§39): a menu that hides a page is not a control, and neither is a caller that
promises to behave.

Nothing here invents a session, an MFA factor or a permission table — those are
the rest of Wave 1-G, and ADR-0005 is still awaiting the owner's acceptance.
"""

from __future__ import annotations

import pytest

import dashboard as D
import primitives as P
from schema_loader import connect_sqlite

NOW = "2026-09-21T08:00:00+00:00"


@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    for agency in ("agency_default", "agency_bali", "agency_jogja"):
        connection.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
            (agency, agency, NOW, NOW),
        )
    yield connection
    connection.close()


def app(conn, *, agency_id: str, tokens):
    return D.Dashboard(
        conn,
        clock=P.FrozenClock.at(NOW),
        actor="owner-sakti-1",
        actor_role="owner",
        agency_id=agency_id,
        tokens=tokens,
    )


def get(dashboard, path: str, principal):
    return dashboard.dispatch(
        D._Request(method="GET", path=path, query={}, form={}, principal=principal)
    )


# --------------------------------------------------------------------------- #
# 1. The token registry knows which agency a caller belongs to
# --------------------------------------------------------------------------- #
def test_a_token_may_name_the_agency_it_belongs_to(conn):
    dashboard = app(
        conn, agency_id="agency_bali",
        tokens={"tok-bali": ("agent-bali-1", "owner", "agency_bali")},
    )
    principal = dashboard.tokens["tok-bali"]
    assert principal.agency_id == "agency_bali"
    assert principal.identity == "agent-bali-1"


def test_an_older_two_part_token_still_works_under_the_default_agency(conn):
    """Backward compatibility: a deployment that never scoped anything is unchanged."""
    dashboard = app(
        conn, agency_id=P.DEFAULT_AGENCY_ID,
        tokens={"tok-plain": ("agent-plain-1", "owner")},
    )
    assert dashboard.tokens["tok-plain"].agency_id == P.DEFAULT_AGENCY_ID


def test_a_token_naming_an_unusable_agency_is_refused(conn):
    with pytest.raises(D.InvalidInput):
        app(conn, agency_id="agency_bali", tokens={"tok": ("agent-1", "owner", "bukan agency!")})


# --------------------------------------------------------------------------- #
# 2. Cross-agency access is denied (the ADR's required test)
# --------------------------------------------------------------------------- #
def test_a_token_from_another_agency_is_refused(conn):
    dashboard = app(
        conn, agency_id="agency_bali",
        tokens={"tok-jogja": ("agent-jogja-1", "owner", "agency_jogja")},
    )
    with pytest.raises(D.Forbidden) as caught:
        get(dashboard, "/", dashboard.tokens["tok-jogja"])
    assert caught.value.details.get("principal_agency") == "agency_jogja"
    assert caught.value.details.get("scope") == "agency_bali"


def test_the_same_token_is_served_inside_its_own_agency(conn):
    dashboard = app(
        conn, agency_id="agency_bali",
        tokens={"tok-bali": ("agent-bali-1", "owner", "agency_bali")},
    )
    response = get(dashboard, "/", dashboard.tokens["tok-bali"])
    assert response.status == 200


def test_cross_agency_access_is_refused_on_every_page(conn):
    dashboard = app(
        conn, agency_id="agency_bali",
        tokens={"tok-jogja": ("agent-jogja-1", "owner", "agency_jogja")},
    )
    principal = dashboard.tokens["tok-jogja"]
    for path in ("/", "/cases", "/finance", "/operations", "/approvals", "/properties", "/audit"):
        with pytest.raises(D.Forbidden):
            get(dashboard, path, principal)


# --------------------------------------------------------------------------- #
# 3. The two refusals that were already there must not weaken
# --------------------------------------------------------------------------- #
def test_a_missing_token_is_unauthenticated(conn):
    dashboard = app(conn, agency_id="agency_bali", tokens={"tok": ("agent-1", "owner", "agency_bali")})
    with pytest.raises(D.Unauthenticated):
        get(dashboard, "/", None)


def test_a_role_outside_the_page_is_still_refused(conn):
    dashboard = app(
        conn, agency_id="agency_bali",
        tokens={"tok-auditor": ("agent-audit-1", "auditor", "agency_bali")},
    )
    with pytest.raises(D.Forbidden) as caught:
        get(dashboard, "/finance", dashboard.tokens["tok-auditor"])
    assert caught.value.details.get("role") == "auditor"

# --------------------------------------------------------------------------- #
# 4. The dashboard's own machinery must not be locked out of itself
# --------------------------------------------------------------------------- #
def test_a_scoped_dashboard_can_still_dump_its_pages(conn, tmp_path):
    """Regression: principals the dashboard builds for itself must carry its agency.

    Without this, a deployment scoped to a real agency renders **zero** pages --
    every internally-built principal (dump, error page) defaulted to
    ``agency_default`` and was refused by the scope check. The CLI proved it
    before the fix: ``--dump`` exited 2 with ``token ini milik agency lain``.
    """
    dashboard = app(conn, agency_id="agency_bali", tokens=None)
    written = dashboard.dump(tmp_path)
    assert written, "a scoped dashboard dumped no pages at all"
    assert any(path.name == "index.html" for path in written)


def test_the_error_page_of_a_scoped_dashboard_still_renders(conn):
    dashboard = app(conn, agency_id="agency_bali", tokens=None)
    response = dashboard.error_response(
        D.Forbidden("peran ini tidak boleh membuka halaman itu", role="auditor")
    )
    assert response.status == 403
    assert response.body

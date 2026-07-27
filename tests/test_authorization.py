"""Roles, module grants, emergency access, and the forced password change.

The load bearing claim in SECURITY.md section 3.3 is that authorization is enforced
server side on every route, never by hiding UI. These tests are what makes that a
claim an auditor can check rather than a sentence in a document.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AuditAction, AuditResult, Module, Role
from app.security.deps import AuthContext, require_module
from app.templating import render
from tests.conftest import KNOWN_PASSWORD, make_user, sign_in


@pytest.fixture
def app_with_module_routes(settings):
    """The real app plus one probe route per module, gated the way real ones will be.

    Phase 1 has no module pages yet, so the gating dependency is exercised through
    routes that exist only for this test. They use the production dependency, not a
    copy of it.
    """
    from app.main import create_app

    app: FastAPI = create_app(settings)

    def make_route(m: Module):
        async def route(
            request: Request,
            auth: AuthContext = Depends(require_module(m)),  # noqa: B008
        ):
            return render(request, "index.html", {"page_title": m.label, "auth": auth})

        return route

    for module in Module:
        app.get(f"/reports/{module.value}")(make_route(module))

    with TestClient(app) as client:
        yield client


def test_viewer_reaches_a_granted_module(app_with_module_routes):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v1.aa@example.invalid", modules=(Module.FINANCIAL,))
        email = user.email

    sign_in(client, email)
    assert client.get("/reports/financial").status_code == 200


def test_viewer_is_refused_a_module_they_lack(app_with_module_routes):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v2.aa@example.invalid", modules=(Module.FINANCIAL,))
        email = user.email

    sign_in(client, email)
    response = client.get("/reports/therapist_utilization")
    assert response.status_code == 403
    assert "do not have access" in response.text


def test_financial_access_does_not_confer_patient_access(app_with_module_routes):
    """The specific separation the build specification calls out."""
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v3.aa@example.invalid", modules=(Module.FINANCIAL,))
        email = user.email

    sign_in(client, email)
    assert client.get("/reports/financial").status_code == 200
    assert client.get("/reports/patient_funnel").status_code == 403


def test_refusal_is_audited(app_with_module_routes):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v4.aa@example.invalid")
        email = user.email

    sign_in(client, email)
    client.get("/reports/financial")

    with client.app.state.db.session() as session:
        denied = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.ACCESS_DENIED))
            .scalars()
            .all()
        )
    assert denied
    assert denied[-1].result == AuditResult.FAILURE


def test_admin_without_grant_gets_in_and_it_is_logged_as_emergency(
    app_with_module_routes,
):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        admin = make_user(session, email="a1.aa@example.invalid", role=Role.ADMIN)
        email = admin.email

    sign_in(client, email)
    assert client.get("/reports/financial").status_code == 200

    with client.app.state.db.session() as session:
        emergency = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.EMERGENCY_ACCESS))
            .scalars()
            .all()
        )
    assert len(emergency) == 1
    assert emergency[0].target_id == Module.FINANCIAL.value


def test_admin_with_grant_is_not_logged_as_emergency(app_with_module_routes):
    """Routine admin work must not drown the break glass signal."""
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        admin = make_user(
            session,
            email="a2.aa@example.invalid",
            role=Role.ADMIN,
            modules=(Module.FINANCIAL,),
        )
        email = admin.email

    sign_in(client, email)
    client.get("/reports/financial")

    with client.app.state.db.session() as session:
        emergency = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.EMERGENCY_ACCESS))
            .scalars()
            .all()
        )
    assert emergency == []


def test_patient_funnel_read_is_logged_as_phi_view(app_with_module_routes):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v5.aa@example.invalid", modules=(Module.PATIENT_FUNNEL,))
        email = user.email

    sign_in(client, email)
    client.get("/reports/patient_funnel?therapist=EXAMPLE")

    with client.app.state.db.session() as session:
        views = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.PHI_VIEW))
            .scalars()
            .all()
        )
    assert len(views) == 1
    # The filters are recorded; the rows returned are not.
    assert "EXAMPLE" in (views[0].detail or "")


def test_aggregate_module_read_is_not_logged_as_phi_view(app_with_module_routes):
    client = app_with_module_routes
    with client.app.state.db.session() as session:
        user = make_user(session, email="v6.aa@example.invalid", modules=(Module.FINANCIAL,))
        email = user.email

    sign_in(client, email)
    client.get("/reports/financial")

    with client.app.state.db.session() as session:
        views = (
            session.execute(select(AuditLog).where(AuditLog.action == AuditAction.PHI_VIEW))
            .scalars()
            .all()
        )
    assert views == []


def test_non_admin_cannot_reach_user_administration(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="m1.aa@example.invalid", role=Role.MANAGER)
        email = user.email

    sign_in(client, email)
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/audit").status_code == 403


def test_anonymous_admin_route_redirects_to_login_with_next(client):
    response = client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/admin/users"


def test_forced_password_change_blocks_everything_else(client):
    with client.app.state.db.session() as session:
        user = make_user(
            session,
            email="new.aa@example.invalid",
            role=Role.ADMIN,
            must_change_password=True,
        )
        email = user.email

    sign_in(client, email)

    # Every other route bounces to the change password page.
    for path in ("/", "/admin/users", "/admin/audit"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/change-password", path

    # The change password page itself is reachable.
    assert client.get("/change-password").status_code == 200


def test_completing_the_forced_change_unblocks_the_app(client):
    with client.app.state.db.session() as session:
        user = make_user(session, email="new.bb@example.invalid", must_change_password=True)
        email = user.email

    sign_in(client, email)
    page = client.get("/change-password")
    import re

    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    response = client.post(
        "/change-password",
        data={
            "csrf_token": token,
            "current_password": KNOWN_PASSWORD,
            "new_password": "brimstone-kettle-orbit-5521",
            "confirm_password": "brimstone-kettle-orbit-5521",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_deactivating_a_user_kills_their_live_session(client):
    with client.app.state.db.session() as session:
        admin = make_user(session, email="a3.aa@example.invalid", role=Role.ADMIN)
        victim = make_user(session, email="v7.aa@example.invalid")
        admin_email, victim_email, victim_id = admin.email, victim.email, victim.id

    victim_client = TestClient(client.app)
    sign_in(victim_client, victim_email)
    assert victim_client.get("/").status_code == 200

    sign_in(client, admin_email)
    import re

    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin/users").text).group(
        1
    )
    client.post(f"/admin/users/{victim_id}/deactivate", data={"csrf_token": token})

    # Not at their next login: now.
    assert victim_client.get("/", follow_redirects=False).status_code == 303

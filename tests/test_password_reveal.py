"""The show and hide toggle on password fields.

The toggle itself is built in the browser, so what can be asserted server side is that
the script is served on the pages that need it, that every password input is shaped so
the script can enhance it, and that revealing a password changes nothing about what the
server stores or logs.

The click behaviour was verified by driving Chromium against a running instance: the
toggle injects on both pages, reveals and re-masks, keeps the typed value, does not
submit the form, drives only its own field when three are on screen, and re-masks
everything on submit. That check is not in this suite because it would need Playwright
and a browser in CI, and a test that skips when the browser is absent is a test that
reports success having asserted nothing.
"""

from __future__ import annotations

import re

from tests.conftest import KNOWN_PASSWORD, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD, make_user, sign_in

SCRIPT = "js/password-reveal.js"


def password_inputs(html: str) -> list[str]:
    return re.findall(r"<input[^>]*type=\"password\"[^>]*>", html)


# ------------------------------------------------------------------- it is delivered


def test_the_script_is_served_on_the_login_page(client):
    """The login page has no session, so a script inside the auth block would miss it."""
    assert SCRIPT in client.get("/login").text


def test_the_script_file_is_reachable(client):
    response = client.get("/static/js/password-reveal.js")
    assert response.status_code == 200
    assert "password-field__toggle" in response.text


def test_the_script_is_served_on_the_change_password_page(seeded_client):
    sign_in(seeded_client, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD)
    assert SCRIPT in seeded_client.get("/change-password").text


# -------------------------------------------------- every field the script can find


def test_every_password_field_is_reachable_by_the_script(client):
    """The script selects on input[type=password]. Nothing may opt out of that."""
    fields = password_inputs(client.get("/login").text)
    assert len(fields) == 1


def test_the_change_password_page_has_all_three_fields(seeded_client):
    sign_in(seeded_client, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD)
    fields = password_inputs(seeded_client.get("/change-password").text)
    assert len(fields) == 3


def test_password_inputs_are_not_wrapped_in_their_label(seeded_client):
    """A button inside a label is invalid HTML and double fires, so the toggle needs
    the input to be a sibling of its label rather than a child."""
    sign_in(seeded_client, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD)
    html = seeded_client.get("/change-password").text

    # No <label ...> ... <input type="password"> ... </label> anywhere.
    wrapped = re.search(
        r"<label(?![^>]*\bfor=)[^>]*>(?:(?!</label>).)*?type=\"password\"",
        html,
        re.S,
    )
    assert wrapped is None, "a password input is still inside its label element"


def test_every_password_field_has_a_label_bound_by_id(seeded_client):
    sign_in(seeded_client, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD)
    html = seeded_client.get("/change-password").text

    for field in password_inputs(html):
        match = re.search(r'id="([^"]+)"', field)
        assert match, f"password input has no id to bind a label to: {field}"
        assert f'for="{match.group(1)}"' in html


def test_the_login_password_field_keeps_its_label(client):
    html = client.get("/login").text
    assert 'for="password"' in html
    assert 'id="password"' in html


# ------------------------------------------------------- revealing changes nothing


def test_the_form_still_posts_the_same_field_names(client):
    """The toggle changes the input's type in the browser, never its name."""
    with client.app.state.db.session() as db:
        email = make_user(db, email="reveal.user@example.invalid").email

    response = sign_in(client, email, KNOWN_PASSWORD)
    assert response.status_code == 303


def test_a_password_submitted_from_a_revealed_field_is_indistinguishable(client):
    """Type=text posts exactly the same body, so a revealed field signs in normally."""
    with client.app.state.db.session() as db:
        email = make_user(db, email="reveal.two@example.invalid").email

    # Whether the browser had the field as password or text, this is the request.
    response = client.post(
        "/login",
        data={"email": email, "password": KNOWN_PASSWORD, "next": "/reports"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_the_password_is_never_echoed_back_into_the_page(client):
    """Whatever the field's type, a failed attempt must not re-render the secret."""
    with client.app.state.db.session() as db:
        make_user(db, email="reveal.three@example.invalid")

    wrong = "definitely-not-the-password-8823"  # noqa: S105
    response = client.post(
        "/login",
        data={"email": "reveal.three@example.invalid", "password": wrong},
        follow_redirects=False,
    )
    assert wrong not in response.text


def test_the_password_is_never_written_to_the_audit_log(client):
    """A reveal toggle makes a password easier to see. It must not make it easier to
    store: the audit trail records the attempt, never the secret."""
    from sqlalchemy import select

    from app.models.audit import AuditLog

    with client.app.state.db.session() as db:
        make_user(db, email="reveal.four@example.invalid")

    wrong = "another-wrong-one-5512"  # noqa: S105
    client.post(
        "/login",
        data={"email": "reveal.four@example.invalid", "password": wrong},
        follow_redirects=False,
    )

    with client.app.state.db.session() as db:
        entries = db.execute(select(AuditLog)).scalars().all()
    assert entries, "the failed attempt should have been audited at all"
    for entry in entries:
        assert wrong not in (entry.detail or "")
        assert wrong not in (entry.target_id or "")


# ---------------------------------------------------------------- the script itself


def test_the_toggle_is_not_a_submit_button(client):
    """Inside a form the default button type is submit, which would send the login
    form instead of revealing the password."""
    source = client.get("/static/js/password-reveal.js").text
    assert 'button.type = "button"' in source


def test_the_script_hides_the_password_again_on_submit(client):
    source = client.get("/static/js/password-reveal.js").text
    assert 'form.addEventListener("submit"' in source


def test_the_toggle_reports_its_state_to_assistive_technology(client):
    source = client.get("/static/js/password-reveal.js").text
    assert "aria-pressed" in source
    assert "aria-label" in source


def test_the_script_is_allowed_by_the_content_security_policy(client):
    """It is a same origin file, not inline, so script-src 'self' covers it."""
    response = client.get("/login")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "<script>" not in response.text, "no inline script to be blocked"

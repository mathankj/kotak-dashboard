"""Flask test-client integration tests for auth routes."""
import pytest
from flask import Flask

from backend.auth import hash_password, install_auth
from backend.auth_storage import write_auth


@pytest.fixture
def app(tmp_path, monkeypatch):
    auth_path = str(tmp_path / "auth.json")
    monkeypatch.setenv("KOTAK_AUTH_FILE", auth_path)
    write_auth(auth_path, password_hash=hash_password("secret123"), session_version=1)

    # _VERSION_CACHE is module-level — clear between tests so a previous
    # test's bump doesn't leak into this fixture's fresh auth file.
    from backend.auth import _invalidate_version_cache
    _invalidate_version_cache()

    app = Flask(__name__, template_folder="../frontend/templates")
    app.secret_key = "test-secret-key"
    app.config["TESTING"] = True
    install_auth(app)

    @app.route("/")
    def home():
        return "home"

    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_get_login_renders_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"password" in r.data.lower()


def test_post_login_correct_password_redirects_with_cookie(client):
    r = client.post("/login", data={"password": "secret123"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/")
    assert "session" in r.headers.get("Set-Cookie", "").lower()


def test_post_login_wrong_password_re_renders_form(client, monkeypatch):
    # Skip the brute-force sleep so the test runs fast.
    monkeypatch.setattr("backend.auth.WRONG_PASSWORD_DELAY_SECS", 0)
    r = client.post("/login", data={"password": "wrong"})
    assert r.status_code == 200  # form re-shown, not redirected
    assert b"invalid" in r.data.lower()


def test_post_login_respects_next_param(client):
    r = client.post("/login?next=/dashboard", data={"password": "secret123"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/dashboard")


def test_post_login_remember_me_extends_cookie(client):
    r = client.post("/login", data={"password": "secret123", "remember_me": "on"})
    set_cookie = r.headers.get("Set-Cookie", "")
    # Permanent cookie has Expires or Max-Age set; default session has neither.
    assert "max-age" in set_cookie.lower() or "expires" in set_cookie.lower()


# ----- Task 5: before_request hook -----

def test_unauthenticated_get_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_authenticated_get_passes_through(client):
    client.post("/login", data={"password": "secret123"})
    r = client.get("/")
    assert r.status_code == 200
    assert r.data == b"home"


def test_login_path_is_exempt(client):
    # Already covered by test_get_login_renders_form, but assert no redirect.
    r = client.get("/login")
    assert r.status_code == 200


def test_static_path_is_exempt(client):
    # Flask already provides /static/<filename>. We just verify the
    # before_request hook lets it through (no /login redirect). The 404
    # is fine — it proves auth didn\'t intercept and the static handler did.
    r = client.get("/static/does-not-exist.css")
    assert r.status_code == 404
    assert "Location" not in r.headers or "/login" not in r.headers["Location"]


def test_healthz_is_exempt(client, app):
    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    r = client.get("/healthz")
    assert r.status_code == 200


def test_stale_session_version_redirects(client):
    from backend.auth import _invalidate_version_cache
    from backend.auth_storage import bump_session_version

    client.post("/login", data={"password": "secret123"})
    # Bump version — simulates another browser changing the password.
    bump_session_version()
    # Bypass the 5s cache so the test sees the bump immediately rather than
    # depending on cache being empty by accident.
    _invalidate_version_cache()
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    assert "expired=1" in r.headers["Location"]


# ----- Task 6: logout + change-password -----

def test_logout_clears_session(client):
    client.post("/login", data={"password": "secret123"})
    r = client.post("/logout")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    # Subsequent request should redirect to /login.
    r2 = client.get("/")
    assert r2.status_code == 302


def test_change_password_get_renders_form(client):
    client.post("/login", data={"password": "secret123"})
    r = client.get("/change-password")
    assert r.status_code == 200
    assert b"current" in r.data.lower()
    assert b"new" in r.data.lower()


def test_change_password_happy_path(client):
    client.post("/login", data={"password": "secret123"})
    r = client.post(
        "/change-password",
        data={"current": "secret123", "new": "newpass1234", "confirm": "newpass1234"},
    )
    assert r.status_code == 302
    # Current browser still works (re-issued cookie with new sid).
    r2 = client.get("/")
    assert r2.status_code == 200
    # New password works on a fresh client.
    fresh = client.application.test_client()
    r3 = fresh.post("/login", data={"password": "newpass1234"})
    assert r3.status_code == 302


def test_change_password_wrong_current(client):
    client.post("/login", data={"password": "secret123"})
    r = client.post(
        "/change-password",
        data={"current": "wrong", "new": "newpass1234", "confirm": "newpass1234"},
    )
    assert r.status_code == 200
    assert b"current password is incorrect" in r.data.lower()


def test_change_password_mismatch(client):
    client.post("/login", data={"password": "secret123"})
    r = client.post(
        "/change-password",
        data={"current": "secret123", "new": "newpass1234", "confirm": "different"},
    )
    assert r.status_code == 200
    assert b"do not match" in r.data.lower()


def test_change_password_too_short(client):
    client.post("/login", data={"password": "secret123"})
    r = client.post(
        "/change-password",
        data={"current": "secret123", "new": "abc", "confirm": "abc"},
    )
    assert r.status_code == 200
    assert b"at least 8" in r.data.lower()


def test_change_password_force_logout_other_browsers(client, app):
    """Other browsers (other test clients) should be kicked out after pw change."""
    other = app.test_client()
    other.post("/login", data={"password": "secret123"})
    # Other client is logged in; now main client changes password.
    client.post("/login", data={"password": "secret123"})
    client.post(
        "/change-password",
        data={"current": "secret123", "new": "newpass1234", "confirm": "newpass1234"},
    )
    # other now has stale cookie.
    r = other.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]

"""Flask test-client integration tests for auth routes."""
from datetime import timedelta

import pytest
from flask import Flask

from backend.auth import bp, hash_password
from backend.auth_storage import write_auth


@pytest.fixture
def app(tmp_path, monkeypatch):
    auth_path = str(tmp_path / "auth.json")
    monkeypatch.setenv("KOTAK_AUTH_FILE", auth_path)
    write_auth(auth_path, password_hash=hash_password("secret123"), session_version=1)

    app = Flask(__name__, template_folder="../frontend/templates")
    app.secret_key = "test-secret-key"
    app.config["TESTING"] = True
    # Set explicitly so the "remember me" test can observe Max-Age on the cookie
    # (Task 5 will replace this fixture with install_auth(app) which sets it too).
    app.permanent_session_lifetime = timedelta(days=30)
    app.register_blueprint(bp)

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

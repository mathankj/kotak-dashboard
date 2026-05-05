"""Flask app-level auth: hashing, login session, change-password, brute-force lockout.

Replaces nginx auth_basic. Sessions are signed cookies (Flask default), no DB.
The shared auth.json file holds the password hash + a session_version counter
that's bumped on password change to invalidate all existing cookies.

This module exposes (added incrementally per the implementation plan):
  - hash_password(plain) / verify_password(stored_hash, plain) — pbkdf2 wrappers
  - bp: a Flask Blueprint with /login, /logout, /change-password (added in later tasks)
  - install_auth(app): registers blueprint + before_request hook + secret key (later task)
"""
import os
import time
from datetime import timedelta

from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(plain):
    """pbkdf2:sha256 with 600k iterations.

    Explicit method= because werkzeug 3.x changed the default from pbkdf2
    to scrypt; the spec calls for pbkdf2:sha256:600000.
    """
    return generate_password_hash(plain, method="pbkdf2:sha256:600000")


def verify_password(stored_hash, plain):
    """Return True iff `plain` matches `stored_hash`. None hash → False."""
    if not stored_hash:
        return False
    try:
        return check_password_hash(stored_hash, plain)
    except (ValueError, TypeError):
        return False


# ---- Brute-force lockout (per-IP, in-memory, this-process-only) ----
#
# Threat model: a 3-person dashboard. We're not at internet scale; per-IP
# in-memory is plenty. State lost on restart = acceptable; an attacker that
# can crash the service to reset counters has bigger leverage anyway.

LOCKOUT_THRESHOLD = 5      # failed attempts within the window → lock
LOCKOUT_WINDOW_SECS = 60   # rolling window length

# {ip: (count, first_failure_ts)} — the timestamp is when the *first*
# failure in this window happened, so the whole window resets together
# rather than the count drifting forever.
_LOCKOUT_STATE = {}


def _prune_lockout(ip):
    """Drop the entry if its window has expired. Return remaining entry or None."""
    entry = _LOCKOUT_STATE.get(ip)
    if not entry:
        return None
    _, first_ts = entry
    if time.time() - first_ts > LOCKOUT_WINDOW_SECS:
        _LOCKOUT_STATE.pop(ip, None)
        return None
    return entry


def record_failed_login(ip):
    """Bump the failure counter for this IP."""
    entry = _prune_lockout(ip)
    if entry is None:
        _LOCKOUT_STATE[ip] = (1, time.time())
    else:
        count, first_ts = entry
        _LOCKOUT_STATE[ip] = (count + 1, first_ts)


def is_locked_out(ip):
    """True iff this IP has hit LOCKOUT_THRESHOLD failures in the window."""
    entry = _prune_lockout(ip)
    if entry is None:
        return False
    count, _ = entry
    return count >= LOCKOUT_THRESHOLD


def clear_lockout(ip):
    """Called on successful login to reset the counter."""
    _LOCKOUT_STATE.pop(ip, None)


# ---- Flask blueprint + login route ----

from flask import (  # noqa: E402  (kept after lockout block for readability)
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from backend.auth_storage import (  # noqa: E402
    AUTH_VERSION_UNINITIALIZED,
    read_auth,
    write_auth,
)

MIN_PASSWORD_LEN = 8

bp = Blueprint("auth", __name__)

# Brute-force friction on every wrong-password POST. Cheap and effective at
# 3-user scale; shaved out of unit tests via monkeypatch.
WRONG_PASSWORD_DELAY_SECS = 1.5


def _client_ip():
    # request.remote_addr is the proxy in our nginx setup, but we don't
    # currently set X-Forwarded-For trust. For lockout this is fine — the
    # proxy IP is constant, so a flood from any single nginx worker still
    # locks out *that conduit*. Acceptable for v1. Revisit when adding
    # X-Forwarded-For trust.
    return request.remote_addr or "?"


def _is_safe_next(target):
    """Only allow same-origin paths to prevent open-redirect."""
    return (
        isinstance(target, str)
        and target.startswith("/")
        and not target.startswith("//")
    )


@bp.route("/login", methods=["GET", "POST"])
def login_view():
    next_url = request.args.get("next") or request.form.get("next") or "/"
    if not _is_safe_next(next_url):
        next_url = "/"

    if request.method == "GET":
        expired = request.args.get("expired") == "1"
        return render_template("login.html", error=None, next=next_url, expired=expired)

    ip = _client_ip()
    if is_locked_out(ip):
        return (
            render_template(
                "login.html",
                error=f"Too many failed attempts. Try again in {LOCKOUT_WINDOW_SECS}s.",
                next=next_url,
                expired=False,
            ),
            429,
        )

    submitted = request.form.get("password", "")
    state = read_auth()
    if state["session_version"] == AUTH_VERSION_UNINITIALIZED:
        return (
            render_template(
                "login.html",
                error="Auth not initialized. Admin must run the reset script.",
                next=next_url,
                expired=False,
            ),
            503,
        )

    if not verify_password(state["password_hash"], submitted):
        record_failed_login(ip)
        if WRONG_PASSWORD_DELAY_SECS:
            time.sleep(WRONG_PASSWORD_DELAY_SECS)
        return render_template(
            "login.html",
            error="Invalid password.",
            next=next_url,
            expired=False,
        )

    # Success.
    clear_lockout(ip)
    session.clear()
    session["sid"] = state["session_version"]
    session["mark"] = "ok"
    if request.form.get("remember_me"):
        session.permanent = True  # uses app.permanent_session_lifetime
    return redirect(next_url)


# ---- before_request hook + install_auth ----

# Per-process cache: avoid disk hit on every request. Also bounds the
# cross-service propagation lag of a session_version bump to ≤TTL.
_VERSION_CACHE = {"value": None, "ts": 0.0}
_VERSION_CACHE_TTL = 5.0  # seconds — see spec; ≤5s cross-service propagation


def _current_session_version():
    now = time.time()
    if _VERSION_CACHE["value"] is None or now - _VERSION_CACHE["ts"] > _VERSION_CACHE_TTL:
        _VERSION_CACHE["value"] = read_auth()["session_version"]
        _VERSION_CACHE["ts"] = now
    return _VERSION_CACHE["value"]


def _invalidate_version_cache():
    """Force the next request to re-read auth.json. Called by /change-password
    so the changing browser does not need to wait out the 5s TTL."""
    _VERSION_CACHE["value"] = None


# Paths exempt from login enforcement. Trailing slash on /static/ matters —
# /static is unlikely but /static-foo would otherwise match if we did
# startswith("/static").
_EXEMPT_PREFIXES = ("/login", "/logout", "/static/", "/healthz")


def _is_exempt(path):
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


def enforce_login():
    """Flask before_request hook — gates every non-exempt route."""
    if _is_exempt(request.path):
        return None
    sid = session.get("sid")
    if sid is None or sid != _current_session_version():
        session.clear()
        return redirect(url_for("auth.login_view", next=request.path, expired=1))
    return None


def install_auth(app):
    """Wire auth into the Flask app: secret key + lifetime + blueprint + hook."""
    secret = os.environ.get("SECRET_KEY")
    if not secret and not app.config.get("TESTING"):
        raise RuntimeError(
            "SECRET_KEY env var is required (generate via "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`)."
        )
    if secret:
        app.secret_key = secret

    # 30-day lifetime applies when session.permanent=True (set on login by
    # the remember_me checkbox). Without remember_me the cookie is a default
    # browser-session cookie that dies when the browser closes.
    app.permanent_session_lifetime = timedelta(days=30)
    app.config.setdefault("SESSION_REFRESH_EACH_REQUEST", False)
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    # HTTP-only deployment for now; flip to True when HTTPS lands.
    app.config.setdefault("SESSION_COOKIE_SECURE", False)

    app.register_blueprint(bp)
    app.before_request(enforce_login)


# ---- Logout + change-password ----

@bp.route("/logout", methods=["POST", "GET"])
def logout_view():
    session.clear()
    return redirect(url_for("auth.login_view"))


@bp.route("/change-password", methods=["GET", "POST"])
def change_password_view():
    if request.method == "GET":
        return render_template("change_password.html", error=None)

    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")

    state = read_auth()
    if not verify_password(state["password_hash"], current):
        return render_template(
            "change_password.html", error="Current password is incorrect."
        )
    if new != confirm:
        return render_template(
            "change_password.html", error="New passwords do not match."
        )
    if len(new) < MIN_PASSWORD_LEN:
        return render_template(
            "change_password.html",
            error=f"New password must be at least {MIN_PASSWORD_LEN} characters.",
        )

    # Write new hash + bump version. The window between read_auth above and
    # write_auth here is microseconds; for a 3-user app, racing with the
    # admin reset CLI is not a real concern. If two browsers POST
    # /change-password concurrently, one of them wins and the other gets
    # kicked to /login on the next request — that's the desired behavior.
    new_version = state["session_version"] + 1
    write_auth(password_hash=hash_password(new), session_version=new_version)
    _invalidate_version_cache()

    # Re-issue THIS browser's cookie with the new sid so we don't log
    # ourselves out — only the *other* browsers get kicked.
    session["sid"] = new_version
    return redirect("/")

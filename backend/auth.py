"""Flask app-level auth: hashing, login session, change-password, brute-force lockout.

Replaces nginx auth_basic. Sessions are signed cookies (Flask default), no DB.
The shared auth.json file holds the password hash + a session_version counter
that's bumped on password change to invalidate all existing cookies.

This module exposes (added incrementally per the implementation plan):
  - hash_password(plain) / verify_password(stored_hash, plain) — pbkdf2 wrappers
  - bp: a Flask Blueprint with /login, /logout, /change-password (added in later tasks)
  - install_auth(app): registers blueprint + before_request hook + secret key (later task)
"""
import time

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
)

from backend.auth_storage import (  # noqa: E402
    AUTH_VERSION_UNINITIALIZED,
    read_auth,
)

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

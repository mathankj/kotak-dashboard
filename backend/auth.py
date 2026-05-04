"""Flask app-level auth: hashing, login session, change-password, brute-force lockout.

Replaces nginx auth_basic. Sessions are signed cookies (Flask default), no DB.
The shared auth.json file holds the password hash + a session_version counter
that's bumped on password change to invalidate all existing cookies.

This module exposes (added incrementally per the implementation plan):
  - hash_password(plain) / verify_password(stored_hash, plain) — pbkdf2 wrappers
  - bp: a Flask Blueprint with /login, /logout, /change-password (added in later tasks)
  - install_auth(app): registers blueprint + before_request hook + secret key (later task)
"""
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

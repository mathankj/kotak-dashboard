# App-level password auth (replace nginx basic auth)

**Date:** 2026-05-04
**Status:** Approved (design phase)
**Scope:** Replace nginx `auth_basic` on both `kotak.service` (port 5000 → nginx port 80) and `kotak-reverse.service` (port 5001 → nginx port 8081) with a Flask-side login system that supports user-driven password change.

## Problem

Today both dashboards are gated by nginx `auth_basic` reading a shared `/etc/nginx/.htpasswd`. Pain points:

1. **No way for the user to change the password** — htpasswd files require root + `nginx -s reload`. Ganesh has no SSH access; he must message the admin every time he wants a new password.
2. **Browser basic-auth dialog is ugly** and can't show product context (no logo, no "remember me", no "forgot password").
3. **No per-event audit** — basic auth gives nothing beyond nginx `access.log`.

We want: a clean login page, a "Change Password" UI button Ganesh can use anytime, support for 3–4 concurrent browser logins by trusted humans (Mathan + Ganesh + Ganesh's friend), and an SSH-based admin reset for the rare case where everyone forgets the password.

## Decisions (from brainstorm)

| Topic | Decision |
|---|---|
| User model | One shared account used by 3+ humans across both services |
| nginx auth | Removed entirely; Flask owns auth |
| Session lifetime | 1 day default, 30 days with "Remember me" |
| Change-password form | 3 fields: current + new + confirm (current required even when logged in) |
| Forgot-password recovery | None in UI; admin SSH-runs `python -m backend.auth_reset_password` |
| Other-session behavior on password change | Force logout everywhere via `session_version` counter |
| Library choice | Plain Flask sessions + `werkzeug.security` (zero new deps) |
| Auth file location | `/home/kotak/shared/auth.json` (shared between both services), path overridable via env `KOTAK_AUTH_FILE` |
| `SECRET_KEY` | One shared value across both services so cookies issued by main are valid on rev |

## Architecture

```
Browser ──HTTP──▶ nginx (80 main, 8081 rev) ──▶ Flask app (5000 / 5001)
                  │                              │
              (no auth_basic)                    ▼
                                          backend/auth.py
                                                 │
                                                 ▼
                          shared file: /home/kotak/shared/auth.json
                          {"password_hash": "...",
                           "session_version": 3,
                           "updated_at": "2026-05-04 09:12:00 IST"}
```

- Both services read+write the same `auth.json` under `fcntl.flock`.
- Both services share the same `SECRET_KEY` (signed-cookie secret) via `.env`.
- Both services read the same `session_version`, so a password change on either service force-logs-out cookies issued by either.

## Components

**New files:**
- `backend/auth.py` — auth blueprint, before_request hook, login/logout/change-password routes, hashing, session-version validation, in-memory IP-based brute-force lockout.
- `backend/auth_reset_password.py` — CLI script (no Flask context); prompts twice, writes new hash, bumps version.
- `frontend/templates/login.html` — login form (themed; reads `KOTAK_UI_THEME`).
- `frontend/templates/change_password.html` — 3-field form.

**Changes:**
- `app.py` — register `auth.bp`, assert `SECRET_KEY` set at boot.
- `.env` — add `SECRET_KEY=<64 hex chars>` and (optionally) `KOTAK_AUTH_FILE`.
- `/etc/nginx/sites-enabled/kotak` and `kotak-reverse` — remove `auth_basic` and `auth_basic_user_file` lines, then `nginx -t && nginx -s reload`.

**One-time Contabo setup:**
```
sudo -u kotak mkdir -p /home/kotak/shared
sudo -u kotak chmod 750 /home/kotak/shared
# Run reset script from EITHER service folder (writes shared file at /home/kotak/shared/auth.json):
cd /home/kotak/kotak-dashboard && sudo -u kotak python3 -m backend.auth_reset_password
```

## Data flow

### Login
1. Browser → any URL.
2. `@before_request` (registered globally on Flask app, exempt: `/login`, `/logout`, `/static/*`, `/healthz`) sees no valid cookie → 302 `/login?next=<orig>`.
3. User submits `password` + `remember_me`.
4. Server: read `auth.json`, `check_password_hash(stored_hash, submitted)`. On match:
   - `session["sid"] = current_session_version`
   - `session["mark"] = "ok"`
   - `session.permanent = remember_me` → cookie expiry 30 d; otherwise default 1 d via `PERMANENT_SESSION_LIFETIME` set on app.
5. 302 to `next` (or `/`).

### Per-request validation
1. `before_request` decodes cookie (Flask does this automatically via `SECRET_KEY`).
2. Reads current `session_version` from `auth.json` (5 s in-memory cache per process to avoid disk hit per request). **Implication:** a password change on service A propagates to service B within ≤5 s (B's cache TTL). Acceptable; tests must not assert instant cross-service invalidation.
3. `if session.get("sid") != current_session_version` → `session.clear()` + 302 `/login?expired=1`.
4. Otherwise pass through.

Reads of `auth.json` use the same `flock` (LOCK_SH for reads, LOCK_EX for writes) so a write in progress on one service can't be torn-read by the other.

### Change password
1. GET `/change-password` → 3-field form.
2. POST: validate `current` matches stored hash; `new == confirm`; `len(new) >= 8`.
3. Under `flock`:
   - write new hash
   - increment `session_version`
   - update `updated_at`
4. Re-issue **current** browser's cookie with new `sid` (other browsers still on old version → next request kicks them to login).
5. 302 to `/` with flash message "Password changed".

### Admin reset (SSH)
```
cd /home/kotak/kotak-dashboard
python -m backend.auth_reset_password
# prompts twice for new password; writes hash + bumps version
```
Bumps `session_version` → all sessions (including admin's own) invalidated.

## Error handling

| Scenario | Behavior |
|---|---|
| `auth.json` missing | `/login` shows "Auth not initialized — admin must run reset script". Protected routes 503. |
| `auth.json` corrupt JSON | Same as missing; log error to `app.log`. |
| `SECRET_KEY` env unset | App raises `RuntimeError` at boot — refuses to start. |
| Wrong password (login) | Form re-shown with generic "Invalid password" + 1.5 s `time.sleep` to slow brute-force. |
| 5 wrong logins in 60 s from one IP | 60 s lockout (in-memory dict `{ip: (count, first_failure_ts)}`); UI shows "Too many attempts, try again in N seconds". |
| Wrong current password (change) | Form re-shown with "Current password is incorrect". |
| `new` ≠ `confirm` | Inline error. |
| `new` length < 8 | Inline error. |
| `flock` contention > 5 s | 500 + log entry (extremely unlikely on this VPS). |
| Cookie has stale `sid` | Silent redirect to `/login?expired=1` with banner "Session expired, please log in again". |

Brute-force lockout deliberately scoped to **per-IP, in-memory, this-process-only**. With two processes (main + rev) and 3–4 humans this is fine; we're not running at internet scale. State lost on restart = acceptable; an attacker that can crash the service to reset counters has bigger leverage already.

## Security notes

- Hash: `werkzeug.security.generate_password_hash` (default `pbkdf2:sha256:600000`). Adequate for a 3-person dashboard. Can swap to argon2 later by adding the `argon2-cffi` dep — out of scope.
- Cookie: signed with `SECRET_KEY` (HMAC-SHA1 by default in Flask 3). `HttpOnly` always; `SameSite=Lax`. `SESSION_COOKIE_SECURE=False` because the current deployment is HTTP-only on port 80 / 8081 (TLS termination is a separate, deferred concern). When HTTPS is added, flip to `True`.
- No CSRF token on login/change-password forms in v1 — out of scope; we have no concurrent state-changing endpoints reachable cross-origin. Add `flask-wtf` later if scope grows.
- TLS: nginx already terminates HTTPS in front (current deployment is HTTP-only on port 80; HTTPS migration is a separate concern). Auth design works under either.

## Testing

### Unit tests (`tests/test_auth.py`)
- Hash → verify roundtrip.
- Read/write `auth.json` under lock; concurrent writes don't corrupt.
- `session_version` bump invalidates a cookie generated at the previous version.
- Brute-force lockout triggers at 5 failures, releases after 60 s.

### Integration tests (`tests/test_auth_routes.py`, Flask test client)
- GET `/` unauthenticated → 302 `/login`.
- POST `/login` with right password → 302 to `/`; cookie set.
- POST `/login` with wrong password → form re-shown, no cookie.
- Change-password happy path: old cookie still works for the changing browser, new cookie issued; a *second* test client with the pre-change cookie gets 302 `/login`.
- Reset script (subprocess) → version increments, file written.

### Manual smoke test (post-deploy)
1. Run reset script with temp password.
2. Four browsers (2 on main, 2 on rev) → all redirect to login.
3. Log in on all four.
4. Change password from browser 1.
5. Browsers 2/3/4 redirect to `/login?expired=1` on next request.
6. Re-login with new password.

## Out of scope (deferred)

- Multi-user accounts (one shared account is the explicit design).
- Email-based password reset (no SMTP infra).
- 2FA / TOTP for the dashboard login (broker login already uses TOTP separately).
- `flask-wtf` CSRF tokens (add later if state-changing endpoints become CSRF-reachable).
- HTTPS migration (independent concern).
- Audit log of login attempts beyond `app.log` lines (could later append to `data/login_history.json` alongside broker login attempts, but that file is currently broker-specific — clean separation preferred for v1).

## Deployment plan

1. Land code on `feature/reverse-gann-phase1` first (rev), test live with Ganesh.
2. After 1–2 days of clean operation on rev, merge to `main`, deploy to production.
3. nginx config edits done by hand on Contabo (one-time, then commit `/etc/nginx/sites-enabled/*` to a separate ops-notes file if not already tracked).
4. `SECRET_KEY` generated once via `python -c "import secrets; print(secrets.token_hex(32))"` and added to `.env` on **both** server folders (same value).

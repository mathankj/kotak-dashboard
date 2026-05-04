"""Auto-login scheduler — runs daily at 08:45 IST.

Replaces cron / systemd timers entirely. A daemon thread sleeps until
the next 08:45 IST, performs a fresh Kotak TOTP login (HTTP — there is
no WS login endpoint), then signals the QuoteFeed to tear down its
stale WebSocket and reconnect with the fresh session token.

Why in-process Python instead of cron:
  * The new session token lands directly in backend.kotak.client._state,
    no file/IPC handoff needed between the scheduler and the running app.
  * Single source of truth — one app.py change deploys to both
    kotak.service (port 5000) and kotak-reverse.service (port 5001) via
    `git pull` on Contabo. No /etc/cron.d files to keep in sync.
  * Survives service restarts: if systemd restarts the app at 11 AM,
    the scheduler re-arms for the *next* 08:45 IST automatically.

Why 08:45 IST and not 09:15 (market open):
  * Indian market opens at 09:15:00 sharp. Login + WS subscribe takes
    ~10 seconds, so logging in at 09:15 misses the opening tick (and
    Kotak's daily OPEN value, which the Gann ladder anchors on).
  * 08:45 leaves a 30-minute buffer — even if the first attempt fails
    and we retry every 60s, we still have 29 retries before market open.

Failure handling:
  * Every attempt logs to stdout (captured by systemd journal).
  * On failure, retries every 60s until 09:14 IST, then gives up for
    the day (manual login button on the dashboard still works).

Holidays / weekends:
  * Runs anyway. NSE is closed Sat/Sun and on holidays, but the broker
    login API still works — a fresh token sits idle, no harm done.
    Simpler than maintaining an NSE holiday calendar.
"""
import threading
import time
from datetime import timedelta

from backend.utils import now_ist
from backend.kotak.client import _state, login as kotak_login
from backend.storage.history import append_history

LOGIN_HOUR_IST = 8
LOGIN_MIN_IST = 45
RETRY_DEADLINE_HOUR = 9
RETRY_DEADLINE_MIN = 14
RETRY_INTERVAL_SECS = 60


def _flush_print(*args, **kwargs):
    """print() wrapper that flushes immediately. Required because systemd
    StandardOutput=append:file makes Python stdout block-buffered, hiding
    [auto_login] log lines until ~4 KB accumulate."""
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def _seconds_until_next(hour, minute):
    """Return seconds from now (IST) until the next HH:MM IST. If today's
    HH:MM has already passed, returns the wait until tomorrow's HH:MM."""
    now = now_ist()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _clear_previous_day_caches(log_fn):
    """Drop every TTL-cached price dict so the dashboard never displays
    yesterday's values past 08:45 IST. The caches will be repopulated
    by the next REST/WS fetch — and once WS ticks resume at 09:15 IST,
    only today's data flows through.

    Imported lazily to avoid a hard import-time dependency on backend.quotes
    (the scheduler module is loaded earlier than quotes during app boot)."""
    try:
        from backend.quotes import (
            _quote_cache, _option_quote_cache, _future_quote_cache,
        )
        _quote_cache.update({"data": {}, "ts": 0, "error": None})
        _option_quote_cache.update({"data": {}, "ts": 0.0,
                                    "error": None, "meta": {}})
        _future_quote_cache.update({"data": {}, "ts": 0.0, "error": None})
        log_fn("[auto_login] cleared previous-day price caches "
               "(_quote_cache, _option_quote_cache, _future_quote_cache)")
    except Exception as e:
        log_fn(f"[auto_login] cache clear failed (non-fatal): "
               f"{type(e).__name__}: {e}")


def _do_login_and_reconnect_ws(quote_feed, log_fn):
    """Fresh Kotak TOTP login + clear stale caches + signal WS reconnect.

    Order matters:
      1. Login first (fail fast if broker auth is down).
      2. Clear previous-day caches BEFORE flipping the WS reconnect flag,
         so any in-flight request sees empty caches rather than yesterday's
         values during the brief reconnect window.
      3. Update _state — the same dict ensure_client() reads, so every
         subsequent request sees the new client.
      4. Flip quote_feed._needs_reconnect = True; the QuoteFeed monitor
         thread tears down the old WS (built with yesterday's token) and
         reconnects via its existing reconnect path.
    """
    client, greeting = kotak_login()
    _clear_previous_day_caches(log_fn)
    _state["client"] = client
    _state["greeting"] = greeting
    _state["login_time"] = now_ist()
    _state["error"] = None
    append_history("success", f"[auto-login 08:45 IST] Logged in as {greeting}")
    if quote_feed is not None:
        # Atomic bool write in CPython — no lock needed.
        quote_feed._needs_reconnect = True


def _loop(quote_feed, log_fn):
    """Forever: sleep until 08:45 IST → login → retry on failure → repeat."""
    while True:
        wait = _seconds_until_next(LOGIN_HOUR_IST, LOGIN_MIN_IST)
        log_fn(f"[auto_login] sleeping {wait/3600:.2f}h until next "
               f"{LOGIN_HOUR_IST:02d}:{LOGIN_MIN_IST:02d} IST")
        time.sleep(wait)
        # Retry loop — every RETRY_INTERVAL_SECS until 09:14 IST.
        while True:
            try:
                _do_login_and_reconnect_ws(quote_feed, log_fn)
                log_fn(f"[auto_login] success at "
                       f"{now_ist().strftime('%H:%M:%S')} IST "
                       f"(WS reconnect signalled)")
                break
            except Exception as e:
                log_fn(f"[auto_login] attempt FAILED: "
                       f"{type(e).__name__}: {e}")
                ist = now_ist()
                if (ist.hour, ist.minute) >= (RETRY_DEADLINE_HOUR,
                                              RETRY_DEADLINE_MIN):
                    log_fn(f"[auto_login] past "
                           f"{RETRY_DEADLINE_HOUR:02d}:"
                           f"{RETRY_DEADLINE_MIN:02d} IST — "
                           f"giving up for today (manual login still works)")
                    break
                time.sleep(RETRY_INTERVAL_SECS)


def start_auto_login_scheduler(quote_feed=None, log_fn=_flush_print):
    """Start the daemon thread. Idempotent-friendly (caller is expected
    to call once at app startup; daemon=True so it dies with the process)."""
    t = threading.Thread(target=_loop, args=(quote_feed, log_fn),
                         daemon=True, name="auto-login")
    t.start()
    return t

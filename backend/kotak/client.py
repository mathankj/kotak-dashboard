"""Kotak Neo client lifecycle: login (TOTP), session caching, safe_call wrapper.

Module-level _state holds the live NeoAPI client. ensure_client() is the single
entry point — it logs in on first call and reuses the cached client thereafter.

Login attempts are recorded in login_history.json via backend.storage.history.
"""
import os

import pyotp
from dotenv import load_dotenv
from neo_api_client import NeoAPI

from backend.kotak.api import call_with_retry, CircuitOpenError
from backend.storage.history import append_history, read_history, HISTORY_FILE
from backend.utils import now_ist

load_dotenv()

# Live session state — mutated by ensure_client() and /refresh route.
_state = {"client": None, "login_time": None, "greeting": None, "error": None}


def login():
    """Fresh login using TOTP. Returns (client, greeting) or raises."""
    client = NeoAPI(
        environment="prod",
        access_token=None,
        neo_fin_key=None,
        consumer_key=os.getenv("KOTAK_CONSUMER_KEY"),
    )
    totp_code = pyotp.TOTP(os.getenv("KOTAK_TOTP_SECRET")).now()
    login_resp = client.totp_login(
        mobile_number=os.getenv("KOTAK_MOBILE"),
        ucc=os.getenv("KOTAK_UCC"),
        totp=totp_code,
    )
    if "error" in login_resp:
        raise RuntimeError(f"totp_login failed: {login_resp['error']}")

    validate_resp = client.totp_validate(mpin=os.getenv("KOTAK_MPIN"))
    if "error" in validate_resp:
        raise RuntimeError(f"totp_validate failed: {validate_resp['error']}")

    greeting = validate_resp.get("data", {}).get("greetingName", "Trader")
    return client, greeting


def ensure_client():
    """Return the cached NeoAPI client; log in on first call."""
    if _state["client"] is None:
        try:
            client, greeting = login()
            _state["client"] = client
            _state["greeting"] = greeting
            _state["login_time"] = now_ist()
            _state["error"] = None
            append_history("success", f"Logged in as {greeting}")
        except Exception as e:
            _state["error"] = str(e)
            _state["client"] = None
            append_history("failed", str(e))
            raise
    return _state["client"]


def safe_call(fn, *args, **kwargs):
    """Call a Kotak SDK method with rate-limit + retry + breaker + try/catch.

    Returns (data, error_str). Treats 'no data found' style responses as empty
    (data=[], error=None) rather than errors, since the SDK uses that pattern
    for empty result sets.

    Rate limiting / retry / circuit-breaker are applied via call_with_retry.
    Breaker-open errors surface as their own error string so the UI can show
    "broker temporarily unavailable" instead of crashing.
    """
    name = getattr(fn, "__name__", "kotak_call")
    try:
        resp = call_with_retry(name, fn, *args, **kwargs)
    except CircuitOpenError as e:
        return None, f"breaker_open: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if isinstance(resp, dict) and "error" in resp:
        err = resp["error"]
        err_list = err if isinstance(err, list) else [err]
        empty_markers = [
            "no holdings found", "no positions", "no orders",
            "no trades", "no data", "not found",
        ]
        for e in err_list:
            msg = (e.get("message") if isinstance(e, dict) else str(e)).lower()
            if any(m in msg for m in empty_markers):
                return [], None
        return None, str(err)
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"], None
    return resp, None

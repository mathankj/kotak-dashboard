"""Standalone Kotak Neo auto-login script.

Run by Windows Task Scheduler every weekday at 09:00 IST via run_login.bat.
Reads credentials from .env at the repo root (one level up from this folder),
performs TOTP login, validates MPIN, and prints the holdings as a smoke check.

This is independent of the Flask app — it only proves the credentials work
and warms the broker session before market open.
"""
import os
import pyotp
from dotenv import load_dotenv
from neo_api_client import NeoAPI

# .env lives at the repo root, not in scripts/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

client = NeoAPI(
    environment='prod',
    access_token=None,
    neo_fin_key=None,
    consumer_key=os.getenv("KOTAK_CONSUMER_KEY"),
)

totp_code = pyotp.TOTP(os.getenv("KOTAK_TOTP_SECRET")).now()
print(f"TOTP: {totp_code}")

login_resp = client.totp_login(
    mobile_number=os.getenv("KOTAK_MOBILE"),
    ucc=os.getenv("KOTAK_UCC"),
    totp=totp_code,
)
print("Login response:", login_resp)

validate_resp = client.totp_validate(mpin=os.getenv("KOTAK_MPIN"))
print("Validate response:", validate_resp)

print("Logged in successfully!")
print("Holdings:", client.holdings())

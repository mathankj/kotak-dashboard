import os
import pyotp
from dotenv import load_dotenv
from neo_api_client import NeoAPI

load_dotenv()

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

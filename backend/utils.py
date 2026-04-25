"""Shared utilities used across backend modules.

Currently: IST timezone helpers. Kept tiny on purpose — anything that needs
shared state belongs in a domain module (kotak/, strategy/, storage/), not here.
"""
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)

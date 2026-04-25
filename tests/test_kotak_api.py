"""Tests for backend/kotak/api.py — RateLimiter, CircuitBreaker, retry, stats."""
import time

import pytest

from backend.kotak.api import (
    RateLimiter, CircuitBreaker, CircuitOpenError,
    call_with_retry, stats, reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


# ---- RateLimiter ----

def test_rate_limiter_immediate_when_tokens_available():
    rl = RateLimiter(rate=10.0, capacity=5)
    t0 = time.monotonic()
    for _ in range(5):
        rl.acquire()
    assert time.monotonic() - t0 < 0.05  # all from initial bucket


def test_rate_limiter_blocks_when_exhausted():
    rl = RateLimiter(rate=10.0, capacity=2)
    rl.acquire()
    rl.acquire()
    t0 = time.monotonic()
    rl.acquire()  # must wait ~0.1s for refill
    elapsed = time.monotonic() - t0
    assert 0.05 < elapsed < 0.30


# ---- CircuitBreaker ----

def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3, cooldown_s=10)
    for _ in range(3):
        assert cb.allow()
        cb.record_failure()
    assert not cb.allow()  # now open


def test_breaker_resets_after_success():
    cb = CircuitBreaker(threshold=3, cooldown_s=10)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    # After success, failures reset to 0; can fail twice more before opening
    assert cb.allow()
    cb.record_failure()
    assert cb.allow()
    cb.record_failure()
    assert cb.allow()
    cb.record_failure()
    assert not cb.allow()


def test_breaker_half_opens_after_cooldown():
    cb = CircuitBreaker(threshold=2, cooldown_s=0.05)
    cb.record_failure()
    cb.record_failure()
    assert not cb.allow()
    time.sleep(0.06)
    assert cb.allow()  # half-open probe


# ---- call_with_retry ----

def test_call_with_retry_success_first_try():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return "ok"
    assert call_with_retry("test", fn) == "ok"
    assert calls["n"] == 1
    s = stats()
    assert s["calls"]["test"]["count"] == 1
    assert s["calls"]["test"]["errors"] == 0
    assert s["calls"]["test"]["retries"] == 0


def test_call_with_retry_succeeds_after_retries():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"
    assert call_with_retry("flaky", fn, base_delay=0.01) == "ok"
    assert calls["n"] == 3
    s = stats()
    assert s["calls"]["flaky"]["count"] == 1
    assert s["calls"]["flaky"]["errors"] == 0
    assert s["calls"]["flaky"]["retries"] == 2


def test_call_with_retry_exhausts_and_raises():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        raise ValueError("boom")
    with pytest.raises(ValueError):
        call_with_retry("broken", fn, base_delay=0.01)
    assert calls["n"] == 3
    s = stats()
    assert s["calls"]["broken"]["errors"] == 1


def test_call_with_retry_passes_args_kwargs():
    def fn(a, b, c=None):
        return (a, b, c)
    assert call_with_retry("x", fn, 1, 2, c=3) == (1, 2, 3)


def test_call_with_retry_blocks_when_breaker_open():
    """After enough failures, breaker opens and call_with_retry raises CircuitOpenError."""
    def fn():
        raise ValueError("fail")
    for _ in range(5):
        with pytest.raises(ValueError):
            call_with_retry("flaky2", fn, base_delay=0.001)
    # Sixth call: breaker is open
    with pytest.raises(CircuitOpenError):
        call_with_retry("flaky2", fn)

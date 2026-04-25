"""Strong-API primitives for Kotak calls.

Provides three concerns wrapped around any callable that hits Kotak:
  1. RateLimiter   — token bucket so we don't exceed Kotak's 5 req/s cap.
  2. CircuitBreaker — after N consecutive failures, fail-fast for cooldown_s.
  3. Retry         — exponential backoff on transient errors.
  4. Stats         — per-name call count, error count, latency sums.

Designed as composable primitives plus one `call_with_retry()` convenience
that wires them together. safe_call() in client.py delegates to this.

Why a free function instead of wrapping the NeoAPI client object?
  - Keeps WS callbacks (client.on_message = ...) working — those need a real
    NeoAPI instance, not a wrapper. Touching attribute assignment on a
    proxy class is fragile.
  - Each call site already passes the bound method, so we add no friction.
"""
import threading
import time
from collections import defaultdict


class RateLimiter:
    """Simple thread-safe token bucket. Default: 5 tokens, refilled at 5/sec."""

    def __init__(self, rate=5.0, capacity=5):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n=1.0):
        """Block until n tokens are available."""
        n = float(n)
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self.rate
            time.sleep(wait)


class CircuitBreaker:
    """Trip after `threshold` consecutive failures; stay open for `cooldown_s`."""

    def __init__(self, threshold=5, cooldown_s=30.0):
        self.threshold = threshold
        self.cooldown_s = float(cooldown_s)
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self):
        """Return True if the call may proceed, False if breaker is open."""
        with self._lock:
            if self._failures < self.threshold:
                return True
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                # Half-open: allow one probe call. On success, fully reset.
                self._failures = self.threshold - 1
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures == self.threshold:
                self._opened_at = time.monotonic()

    def state(self):
        with self._lock:
            return {
                "failures": self._failures,
                "open": self._failures >= self.threshold,
                "opened_at": self._opened_at,
            }


# Module-level singletons — all Kotak calls share the same limiter & breaker.
_limiter = RateLimiter(rate=5.0, capacity=5)
_breaker = CircuitBreaker(threshold=5, cooldown_s=30.0)
_stats = defaultdict(lambda: {"count": 0, "errors": 0, "total_ms": 0.0,
                              "retries": 0})
_stats_lock = threading.Lock()


def _record(name, latency_ms, error, retries):
    with _stats_lock:
        s = _stats[name]
        s["count"] += 1
        s["total_ms"] += latency_ms
        if error:
            s["errors"] += 1
        s["retries"] += retries


class CircuitOpenError(RuntimeError):
    """Raised when a call is blocked by the circuit breaker."""


def call_with_retry(name, fn, *args, max_attempts=3, base_delay=0.2, **kwargs):
    """Execute fn(*args, **kwargs) with rate-limit + retry + breaker + stats.

    Returns whatever fn returns. Raises CircuitOpenError if the breaker is open,
    or whatever fn raised on the final attempt.

    `name` is the label used in stats (e.g. "quotes", "positions"). Callers
    pick a stable name per logical operation, not per object identity.
    """
    if not _breaker.allow():
        raise CircuitOpenError(f"circuit breaker open for {name}")
    last_exc = None
    retries = 0
    t0 = time.monotonic()
    for attempt in range(max_attempts):
        _limiter.acquire()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                retries += 1
                time.sleep(base_delay * (2 ** attempt))
                continue
            latency = (time.monotonic() - t0) * 1000.0
            _record(name, latency, error=True, retries=retries)
            _breaker.record_failure()
            raise
        else:
            latency = (time.monotonic() - t0) * 1000.0
            _record(name, latency, error=False, retries=retries)
            _breaker.record_success()
            return result
    # Defensive — loop should always return or raise.
    raise last_exc if last_exc else RuntimeError(f"{name}: exhausted retries")


def stats():
    """Snapshot of per-method call stats + breaker state. Safe to JSON-encode."""
    with _stats_lock:
        out = {}
        for name, s in _stats.items():
            count = s["count"] or 1
            out[name] = {
                "count": s["count"],
                "errors": s["errors"],
                "retries": s["retries"],
                "avg_ms": round(s["total_ms"] / count, 2),
            }
    return {
        "calls": out,
        "breaker": _breaker.state(),
        "rate_limit": {"rate_per_s": _limiter.rate,
                       "capacity": _limiter.capacity},
    }


def reset_for_tests():
    """Clear all state. ONLY for use in tests."""
    global _stats
    with _stats_lock:
        _stats = defaultdict(lambda: {"count": 0, "errors": 0,
                                       "total_ms": 0.0, "retries": 0})
    _breaker._failures = 0
    _breaker._opened_at = 0.0
    _limiter._tokens = _limiter.capacity

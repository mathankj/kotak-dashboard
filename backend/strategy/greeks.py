"""Black-Scholes Delta with implied-volatility back-solve.

F.3 — show Δ next to LTP for the ATM±2 strikes on /options. Greeks are
not feed-supplied, so we compute them ourselves from spot / strike /
time-to-expiry / option price.

Stdlib only — no numpy / scipy. Bisection IV solver is plenty for a
display-only number; we don't trade on it. Risk-free rate is hard-coded
to 7% (INR repo rate ballpark); a few-bps mis-set on r barely moves Δ for
near-the-money strikes which is all we display.
"""
import math
from datetime import date, datetime, timedelta

# Annualised risk-free rate (continuous compounding). Change here if RBI
# moves and Δ across the chain looks off — but realistically Δ for ATM
# strikes is dominated by σ and T, not r.
RISK_FREE_RATE = 0.07

# Days/year used to convert calendar days-to-expiry into year-fractions.
# 365 keeps it simple; trading-day conventions (252) are noise at the
# precision we display (2 decimal places).
DAYS_PER_YEAR = 365.0


def _norm_cdf(x):
    """Standard normal CDF using math.erf — accurate to ~1e-7."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, t_years, iv, opt_type, r=RISK_FREE_RATE):
    """Black-Scholes price for a European CE/PE. Used by the IV solver."""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        # Intrinsic at/after expiry (or degenerate inputs).
        if opt_type == "CE":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if opt_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_vol(price, spot, strike, t_years, opt_type,
                 r=RISK_FREE_RATE, lo=0.01, hi=4.0, tol=1e-4, max_iter=60):
    """Bisection IV solve. `price` is the observed market price.

    Returns None if the price is below intrinsic (no real IV) or the
    bracket [lo, hi] doesn't contain a root after max_iter halvings.
    """
    if t_years <= 0 or spot <= 0 or strike <= 0 or price is None or price <= 0:
        return None
    # Intrinsic floor — if market price is below intrinsic the option is
    # mispriced (or stale data) and IV is undefined.
    intrinsic = (max(0.0, spot - strike) if opt_type == "CE"
                 else max(0.0, strike - spot))
    if price < intrinsic - 1e-6:
        return None
    f_lo = _bs_price(spot, strike, t_years, lo, opt_type, r) - price
    f_hi = _bs_price(spot, strike, t_years, hi, opt_type, r) - price
    if f_lo * f_hi > 0:
        # Root not bracketed — usually means price is outside the [lo, hi]
        # vol range we consider plausible.
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(spot, strike, t_years, mid, opt_type, r) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _bs_delta(spot, strike, t_years, iv, opt_type, r=RISK_FREE_RATE):
    """Black-Scholes Δ. CE in (0, 1), PE in (-1, 0)."""
    if t_years <= 0 or iv <= 0:
        # At/after expiry: Δ collapses to 1/0 (CE ITM/OTM) or -1/0 (PE).
        if opt_type == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    return _norm_cdf(d1) if opt_type == "CE" else _norm_cdf(d1) - 1.0


def _parse_expiry(expiry):
    """Accept either a `date` or a 'DD-MMM-YYYY' / 'YYYY-MM-DD' string.
    Returns a `date` or None on parse failure.
    """
    if isinstance(expiry, date) and not isinstance(expiry, datetime):
        return expiry
    if isinstance(expiry, datetime):
        return expiry.date()
    if not expiry:
        return None
    s = str(expiry).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def compute_delta(spot, strike, expiry, ltp, opt_type, today=None):
    """High-level: feed spot/strike/expiry/LTP, get Δ.

    Returns a float (rounded to 4 dp) or None if any input is missing or
    the IV solver fails. Best-effort — never raises so the snapshot
    builder can always serve a payload even if the math is unhappy.
    """
    try:
        if spot is None or strike is None or ltp is None or not opt_type:
            return None
        spot = float(spot)
        strike = float(strike)
        ltp = float(ltp)
        if spot <= 0 or strike <= 0 or ltp <= 0:
            return None
        d = _parse_expiry(expiry)
        if d is None:
            return None
        t_today = today or date.today()
        days = (d - t_today).days
        # Expiry day: T~0 — Δ is just intrinsic indicator. We add a small
        # epsilon so afternoon-of-expiry doesn't cliff to 0/1.
        if days <= 0:
            t_years = max(1.0 / 365.0, 1.0 / 24.0 / 365.0)
        else:
            t_years = days / DAYS_PER_YEAR
        iv = _implied_vol(ltp, spot, strike, t_years, opt_type)
        if iv is None:
            return None
        delta = _bs_delta(spot, strike, t_years, iv, opt_type)
        return round(delta, 4)
    except Exception:
        return None

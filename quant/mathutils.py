"""Black-Scholes pricing, greeks and implied-vol inversion.

Shoonya gives premiums and OI but no greeks/IV, so we compute them.
Pure python (math.erf) — no scipy dependency. European options (NSE index
options are European; stock options are American but BS on the monthly
tenor is an acceptable Phase-1 approximation for IV/gamma *ranking*).
"""

from __future__ import annotations

import math

RISK_FREE = 0.07          # ~RBI repo environment; only mildly affects ranks
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_price(is_call: bool, spot: float, strike: float, t_years: float,
             sigma: float, r: float = RISK_FREE) -> float:
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        intrinsic = (spot - strike) if is_call else (strike - spot)
        return max(0.0, intrinsic)
    st = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / st
    d2 = d1 - st
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_gamma(spot: float, strike: float, t_years: float, sigma: float,
             r: float = RISK_FREE) -> float:
    if t_years <= 0 or sigma <= 0:
        return 0.0
    st = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / st
    return _norm_pdf(d1) / (spot * st)


def bs_vega(spot: float, strike: float, t_years: float, sigma: float,
            r: float = RISK_FREE) -> float:
    if t_years <= 0 or sigma <= 0:
        return 0.0
    st = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / st
    return spot * _norm_pdf(d1) * math.sqrt(t_years)


def implied_vol(is_call: bool, price: float, spot: float, strike: float,
                t_years: float, r: float = RISK_FREE) -> float | None:
    """Bisection IV solve. Returns None when the premium is outside
    no-arbitrage bounds (stale/illiquid quote) — callers must skip those."""
    if price <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return None
    intrinsic = max(0.0, (spot - strike) if is_call
                    else (strike - spot) * math.exp(-r * t_years))
    if price < intrinsic - 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(is_call, spot, strike, t_years, hi, r) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(is_call, spot, strike, t_years, mid, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)


def years_to_expiry(expiry_epoch: float, now_epoch: float) -> float:
    return max(0.0, (expiry_epoch - now_epoch)) / (365.0 * 24 * 3600)

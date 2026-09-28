"""Black-Scholes pricing, greeks and implied vol — no scipy, no external deps.

Standalone by design: OptionSmith imports nothing from any other project.
All prices are per SHARE in rupees; multiply by lot size for per-lot rupees.
"""
from __future__ import annotations

import math

RISK_FREE = 0.07          # ~RBI repo environment; only mildly affects ranking
SQRT2PI = math.sqrt(2.0 * math.pi)


# ── normal distribution ────────────────────────────────────────────────
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def _d1_d2(spot: float, strike: float, t: float, sigma: float,
           r: float) -> tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / v
    return d1, d1 - v


# ── pricing & greeks ───────────────────────────────────────────────────
def bs_price(is_call: bool, spot: float, strike: float, t: float,
             sigma: float, r: float = RISK_FREE) -> float:
    """European option price. Degenerate inputs fall back to intrinsic."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    disc = math.exp(-r * t)
    if is_call:
        return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)
    return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(is_call: bool, spot: float, strike: float, t: float,
             sigma: float, r: float = RISK_FREE) -> float:
    # spot <= 0 as well as t/sigma: _d1_d2 takes log(spot/strike), which raises
    # on zero and on negative. bs_gamma and bs_vega already guarded it; delta
    # and theta did not, so an unresolved underlying crashed the greeks instead
    # of returning the degenerate answer — and delta is on the selection path,
    # so the crash landed mid-scan rather than at the quote.
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        itm = (spot > strike) if is_call else (spot < strike)
        return (1.0 if is_call else -1.0) if itm else 0.0
    d1, _ = _d1_d2(spot, strike, t, sigma, r)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def bs_gamma(spot: float, strike: float, t: float, sigma: float,
             r: float = RISK_FREE) -> float:
    # `strike <= 0` too: _d1_d2 divides by the strike, so gamma and vega raised
    # ZeroDivisionError on an input the second-order greeks in this same file
    # already refused — and `position_greeks` calls all of them in one loop, so
    # the leg that vanna handled quietly took the whole evaluation down.
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, t, sigma, r)
    return norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def bs_vega(spot: float, strike: float, t: float, sigma: float,
            r: float = RISK_FREE) -> float:
    """Vega per 1.00 (100 vol points) of sigma; /100 for per-vol-point."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, t, sigma, r)
    return spot * norm_pdf(d1) * math.sqrt(t)


def bs_vanna(spot: float, strike: float, t: float, sigma: float,
             r: float = RISK_FREE) -> float:
    """dVega/dSpot == dDelta/dSigma. Same number, two readings.

    What it is FOR: it says how much the hedge moves when volatility moves.
    A structure with large vanna is one whose delta is not stable under a vol
    shock — so a "delta-neutral" position that is vanna-heavy is neutral only
    at today's IV, and a vol move re-directions it without the spot moving at
    all. That is the failure a delta number alone cannot warn about.

    Sign: NEGATIVE below the money and POSITIVE above, following -d2. Measured
    at S=1000, tau=0.25, sigma=0.30, r=0.07: vanna(900) = -0.663, vanna(1050) =
    +0.374, vanna(1200) = +0.924.

    An earlier version of this docstring stated the reverse. The formula was
    right and only the prose was wrong, which is the more dangerous of the two:
    the number is used to decide whether a "delta-neutral" position is neutral
    in the direction a vol move would take it, and a reader who trusts the
    docstring over the number hedges the wrong way.
    """
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    return -norm_pdf(d1) * d2 / sigma


def bs_volga(spot: float, strike: float, t: float, sigma: float,
             r: float = RISK_FREE) -> float:
    """dVega/dSigma (vomma). Curvature of value in volatility.

    What it is FOR: vega itself is a local slope, so a position with large
    volga gains or loses vega fast as IV moves — its vol exposure is not the
    number on the screen once anything happens. It is largest in the WINGS and
    near zero at the money, which is precisely why a wing-heavy short structure
    is more dangerous than its vega suggests.

    Sign: positive in the wings, where d1 and d2 share a sign, and NEGATIVE in
    the band between the two critical strikes where d1 > 0 > d2 — a vanilla is
    convex in vol only away from the money. Measured: at S=1000, tau=0.25,
    sigma=0.30 the wings give volga(900) = +296.6 and volga(1200) = +473.1,
    while at the forward (S=100, K=107.25, tau=1, sigma=0.40) it is -3.91.
    An earlier docstring claimed "always >= 0", the same prose-vs-formula
    failure bs_vanna records above: the formula matches dVega/dSigma by finite
    difference to 3e-10, and it was only the words that would have made a
    reader trust ATM vol convexity that is not there.
    """
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    return bs_vega(spot, strike, t, sigma, r) * d1 * d2 / sigma


def bs_charm(is_call: bool, spot: float, strike: float, t: float,
             sigma: float, r: float = RISK_FREE) -> float:
    """dDelta/dCALENDAR-TIME — delta decay, PER YEAR, matching bs_theta.

    Divide by 365 for a per-calendar-day figure, or by 252 for a per-trading-day
    one. Stating this matters: theta is per year here, and a charm reported per
    day next to a theta reported per year is how two numbers that should be
    compared get compared wrongly.

    What it is FOR: it says how much the hedge drifts purely from the passage of
    time. Near expiry it grows without bound for a near-the-money option, which
    is the quantitative form of "pin risk" — the delta of an ATM option on
    expiry morning is not a number you can hedge against.

    Sign convention: d(Delta)/dt with t running FORWARD, so a positive value
    means delta RISES as time passes. That is the standard meaning of charm and
    it is the opposite of d(Delta)/dTenor, because tenor SHRINKS as time
    passes: charm = -d(Delta)/dT.

    The first version computed d(Delta)/dT and documented it as d(Delta)/dt —
    the formula validated perfectly against a finite difference in T, which is
    exactly why the error survived: the check confirmed the wrong quantity.
    `position_greeks` divides this by 365 and reports it as the position's
    delta drift per day, so the sign being inverted pointed the hedge the wrong
    way while every test passed.
    """
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    v = sigma * math.sqrt(t)
    # Identical for calls and puts, and that is not an oversight: with no
    # dividend yield Delta_put = Delta_call - 1, and a constant differentiates
    # away. Verified by finite difference — both sides give 0.42093755 on the
    # same inputs. `is_call` is kept in the signature so the call site reads
    # like every other greek here, and so a dividend yield can be added later
    # without changing every caller.
    # NEGATED: the bracket below is d(Delta)/dTenor, and charm is per calendar
    # time, which runs the other way.
    return -norm_pdf(d1) * (2.0 * r * t - d2 * v) / (2.0 * t * v)


def bs_theta(is_call: bool, spot: float, strike: float, t: float,
             sigma: float, r: float = RISK_FREE) -> float:
    """Theta per YEAR (divide by 365 for per-day)."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    term = -(spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(t))
    disc = math.exp(-r * t)
    if is_call:
        return term - r * strike * disc * norm_cdf(d2)
    return term + r * strike * disc * norm_cdf(-d2)


def implied_vol(is_call: bool, price: float, spot: float, strike: float,
                t: float, r: float = RISK_FREE,
                lo: float = 1e-4, hi: float = 5.0) -> float | None:
    """Bisection IV inversion. None when the price is outside no-arb bounds."""
    if price <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None
    intrinsic = max(0.0, (spot - strike * math.exp(-r * t)) if is_call
                    else (strike * math.exp(-r * t) - spot))
    if price < intrinsic - 1e-6:
        return None
    if bs_price(is_call, spot, strike, t, hi, r) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(is_call, spot, strike, t, mid, r) < price:
            lo = mid
        else:
            hi = mid
    iv = 0.5 * (lo + hi)
    return iv if 1e-3 < iv < 4.99 else None


# ── Student-t (fat tails for realistic probability of profit) ──────────
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-9:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, df: float) -> float:
    """Student-t CDF (exact via incomplete beta)."""
    if df <= 0:
        return norm_cdf(x)
    p = 0.5 * _betai(0.5 * df, 0.5, df / (df + x * x))
    return 1.0 - p if x > 0 else p

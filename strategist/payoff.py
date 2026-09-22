"""Exact payoff mathematics for multi-leg option strategies.

A strategy is a list of Legs. All prices in rupees per share; rupee results
are per ONE lot (qty = lot size). Payoffs are at expiry (European-style
evaluation — fine for strategy comparison on monthly stock options).

POP (probability of profit) — three realism upgrades over the classic
"lognormal at ATM IV, any payoff > 0 wins" number (which routinely printed
99.9% for wide short structures):

  1. FAT TAILS: terminal log-return is Student-t (default df=5, env
     STRAT_TAIL_DF; <=2 falls back to lognormal), variance-matched to the IV
     and median-matched to the lognormal's (r - sigma^2/2)t drift. Equity
     returns have power-law tails; the normal assigns ~0 to the crash moves
     that actually blow through condor wings. (E[S_T] does not exist under a
     log-t, so we match MEDIAN + variance — the standard pragmatic overlay.)
  2. FRICTION: a "win" is payoff > costs, not payoff > 0. The caller passes
     the round-trip friction in rupees; per-share threshold = friction / lot.
  3. SMILE: sigma comes from the mean of the LEGS' own implied vols when the
     caller supplies them (wings carry higher IV than ATM in a skewed smile;
     using a single low ATM IV artificially narrows the distribution).

The classic frictionless lognormal-ATM POP is still computed and reported as
`pop_classic_pct` so the optimism gap stays visible. POP remains an estimate
for RANKING, not a promise.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from quant.mathutils import RISK_FREE

# Student-t degrees of freedom for the fat-tailed POP (env-tunable). Daily
# equity/index returns fit df ~ 3-6; 5 is the conventional middle. <=2 has no
# finite variance to match the IV, so it disables the overlay (lognormal).
TAIL_DF = float(os.getenv("STRAT_TAIL_DF", "5"))


# ── Student-t CDF (exact, no scipy) ─────────────────────────────────────
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Numerical Recipes/Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def _ibeta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_cdf(u: float, df: float) -> float:
    """P(T <= u) for Student-t with df degrees of freedom."""
    x = df / (df + u * u)
    p = 0.5 * _ibeta(0.5 * df, 0.5, x)
    return 1.0 - p if u > 0 else p


@dataclass
class Leg:
    side: int              # +1 = buy (long), -1 = sell (short)
    is_call: bool
    strike: float
    premium: float         # per share
    tsym: str = ""
    oi: float = 0.0

    def payoff_at(self, s: float) -> float:
        intrinsic = max(0.0, (s - self.strike) if self.is_call
                        else (self.strike - s))
        return self.side * (intrinsic - self.premium)


@dataclass
class StrategyResult:
    name: str
    legs: list[Leg]
    lot: int
    net_premium: float          # per share; >0 net debit paid, <0 net credit
    max_profit: float           # per share (float('inf') possible)
    max_loss: float             # per share, positive number
    breakevens: list[float]
    pop: float | None           # probability of any profit at expiry
    reward_risk: float | None   # max_profit / max_loss
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        rupee = lambda x: (None if x is None or math.isinf(x)
                           else round(x * self.lot, 0))
        return {
            "name": self.name, "lot": self.lot,
            "legs": [{"side": ("BUY" if l.side > 0 else "SELL"),
                      "type": ("CE" if l.is_call else "PE"),
                      "strike": l.strike, "premium": l.premium,
                      "tsym": l.tsym} for l in self.legs],
            "net_premium_per_share": round(self.net_premium, 2),
            "net_flow": ("DEBIT" if self.net_premium > 0 else "CREDIT"),
            "max_profit_per_lot": ("UNLIMITED" if math.isinf(self.max_profit)
                                    else rupee(self.max_profit)),
            "max_loss_per_lot": rupee(self.max_loss),
            "breakevens": [round(b, 2) for b in self.breakevens],
            "pop_pct": (None if self.pop is None else round(100 * self.pop, 1)),
            "reward_risk": (None if self.reward_risk is None or
                            math.isinf(self.reward_risk)
                            else round(self.reward_risk, 2)),
            **self.details,
        }


def _total_payoff(legs: list[Leg], s: float) -> float:
    return sum(l.payoff_at(s) for l in legs)


def _breakevens(legs: list[Leg], lo: float, hi: float, steps: int = 2000,
                eps: float = 1e-9, offset: float = 0.0):
    """Sign-change scan + bisection refine on (payoff - offset), zero-flat
    aware. offset > 0 gives the FRICTION-ADJUSTED breakevens (where the trade
    covers its costs, not merely the raw premium).

    Zero-net-premium structures have payoff EXACTLY 0 across whole regions
    (e.g. below the lowest strike of a zero-cost fly). A breakeven is only
    the BOUNDARY of such a region, not every grid point inside it."""
    def f(x: float) -> float:
        return _total_payoff(legs, x) - offset

    def sgn(v: float) -> int:
        return 0 if abs(v) <= eps else (1 if v > 0 else -1)

    bes = []
    prev_x = lo
    prev_s = sgn(f(lo))
    for i in range(1, steps + 1):
        x = lo + (hi - lo) * i / steps
        s = sgn(f(x))
        if s != prev_s:
            if prev_s != 0 and s != 0:          # true crossing → bisect
                a, b = prev_x, x
                for _ in range(60):
                    m = 0.5 * (a + b)
                    if (f(a) < 0) != (f(m) < 0):
                        b = m
                    else:
                        a = m
                bes.append(0.5 * (a + b))
            else:                                # entering/leaving a flat-zero
                bes.append(x if prev_s == 0 else prev_x)
        prev_x, prev_s = x, s
    out = []
    for b in bes:
        if not out or abs(b - out[-1]) > 1.5 * (hi - lo) / steps:
            out.append(b)
    return out


def _pop_terminal(legs: list[Leg], spot: float, iv: float, t_years: float,
                  df: float | None = None,
                  threshold: float = 0.0) -> float | None:
    """P(payoff > threshold) at expiry.

    df=None  -> lognormal S_T (drift r, vol=iv) — the classic assumption.
    df>2     -> log-return is Student-t(df), variance-matched to iv^2*t and
                median-matched to the lognormal's (r - iv^2/2)t location, so
                the two POPs are directly comparable; only the tail mass
                differs. threshold (per share) makes wins net of friction.
    """
    if not iv or iv <= 0 or t_years <= 0 or spot <= 0:
        return None
    mu = (RISK_FREE - 0.5 * iv * iv) * t_years   # log-return location
    v = iv * math.sqrt(t_years)
    use_t = df is not None and df > 2.0
    scale = v * math.sqrt((df - 2.0) / df) if use_t else v

    def cdf(s: float) -> float:                  # P(S_T <= s)
        if s <= 0:
            return 0.0
        u = (math.log(s / spot) - mu) / scale
        return _t_cdf(u, df) if use_t else \
            0.5 * (1.0 + math.erf(u / math.sqrt(2.0)))

    lo, hi = spot * 0.25, spot * 2.5
    bounds = [lo] + _breakevens(legs, lo, hi, offset=threshold) + [hi]
    prob = 0.0
    for a, b in zip(bounds[:-1], bounds[1:]):    # profit regions between BEs
        if _total_payoff(legs, 0.5 * (a + b)) - threshold > 0:
            prob += cdf(b) - cdf(a)
    # tails: outside [lo, hi] the payoff sign is constant (piecewise linear)
    if _total_payoff(legs, lo * 0.5) - threshold > 0:
        prob += cdf(lo)
    if _total_payoff(legs, hi * 1.5) - threshold > 0:
        prob += 1.0 - cdf(hi)
    return max(0.0, min(1.0, prob))


def _pop_lognormal(legs: list[Leg], spot: float, iv: float,
                   t_years: float) -> float | None:
    """Classic frictionless lognormal POP (kept for comparison/compat)."""
    return _pop_terminal(legs, spot, iv, t_years, df=None, threshold=0.0)


def evaluate_strategy(name: str, legs: list[Leg], lot: int, spot: float,
                      atm_iv: float | None, t_years: float, *,
                      leg_ivs=None, friction_rupees: float = 0.0,
                      tail_df: float | None = None) -> StrategyResult:
    """Compute exact payoff profile + POP for a leg set.

    Keyword extras (all optional — old positional call sites are unchanged):
      leg_ivs          per-leg implied vols; their mean replaces the single
                       ATM IV as the distribution width (first-order smile).
      friction_rupees  round-trip cost for the WHOLE structure per lot; a
                       "win" then means payoff > friction/lot per share.
      tail_df          Student-t df for fat tails (None -> STRAT_TAIL_DF env,
                       default 5; <=2 -> plain lognormal).

    max_profit/max_loss/breakevens stay GROSS (textbook payoff); the POP and
    breakevens_net are the friction-aware numbers. pop_classic_pct preserves
    the old frictionless lognormal-ATM figure so the gap stays visible.
    """
    net_premium = sum(l.side * l.premium for l in legs)

    # payoff extremes: piecewise-linear → extremes at strikes and far tails
    strikes = sorted({l.strike for l in legs})
    lo, hi = spot * 0.25, spot * 2.5
    probe = ([lo] + strikes + [hi])
    values = [_total_payoff(legs, s) for s in probe]

    # unbounded sides? net long calls → unlimited up; net long puts → big down
    net_calls = sum(l.side for l in legs if l.is_call)
    net_puts = sum(l.side for l in legs if not l.is_call)
    max_profit = max(values)
    if net_calls > 0:
        max_profit = float("inf")
    min_value = min(values)
    if net_calls < 0:
        min_value = -float("inf")
    max_loss = abs(min(0.0, min_value))
    # net long puts: max profit at S=0 already covered by probe at lo… refine:
    if net_puts > 0:
        max_profit = max(max_profit, _total_payoff(legs, 0.01))
    if net_puts < 0:
        max_loss = max(max_loss, abs(min(0.0, _total_payoff(legs, 0.01))))

    rr = None
    if max_loss > 0 and not math.isinf(max_profit):
        rr = max_profit / max_loss

    # distribution width: mean of the legs' own IVs when supplied (a condor's
    # wings price a fatter tail than ATM does), else the ATM IV
    ivs = [float(x) for x in (leg_ivs or []) if x]
    sigma = (sum(ivs) / len(ivs)) if ivs else float(atm_iv or 0.0)
    df = TAIL_DF if tail_df is None else tail_df
    df = df if (df and df > 2.0) else None
    thr = (float(friction_rupees) / lot) if (lot > 0 and friction_rupees) else 0.0

    pop = _pop_terminal(legs, spot, sigma, t_years, df=df, threshold=thr)
    pop_classic = _pop_lognormal(legs, spot, atm_iv or 0.0, t_years)
    details = {
        "pop_classic_pct": (None if pop_classic is None
                            else round(100.0 * pop_classic, 1)),
        "iv_used_pct": (None if not sigma else round(100.0 * sigma, 1)),
        "tail_df": df,
    }
    if thr > 0:
        details["breakevens_net"] = [
            round(b, 2) for b in _breakevens(legs, lo, hi, offset=thr)]

    return StrategyResult(name=name, legs=legs, lot=lot,
                          net_premium=net_premium, max_profit=max_profit,
                          max_loss=max_loss,
                          breakevens=_breakevens(legs, lo, hi),
                          pop=pop, reward_risk=rr, details=details)


# ── self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. Student-t CDF against known values.
    assert abs(_t_cdf(0.0, 5.0) - 0.5) < 1e-12
    assert abs(_t_cdf(2.015, 5.0) - 0.95) < 2e-3       # t-table: t(0.95, 5)
    assert abs(_t_cdf(1.0, 1.0) - 0.75) < 1e-9         # Cauchy: 1/2 + atan(1)/pi
    assert abs(_t_cdf(-2.015, 5.0) - 0.05) < 2e-3      # symmetry

    spot, iv, t = 100.0, 0.25, 30.0 / 365.0
    strangle = [Leg(-1, False, 90.0, 1.5), Leg(-1, True, 110.0, 1.5)]
    call = [Leg(1, True, 100.0, 3.0)]

    # 2. The variance-matched t redistributes mass: peakier center, fatter FAR
    # tails. So the fix bites precisely where the "99.99% POP illusion" lives:
    # (a) WIDE short structures (breakevens 3+ sigma out) lose POP under t —
    wide = [Leg(-1, False, 75.0, 0.3), Leg(-1, True, 125.0, 0.3)]
    pop_ln = _pop_terminal(wide, spot, iv, t)
    pop_t = _pop_terminal(wide, spot, iv, t, df=5.0)
    assert pop_t < pop_ln, (pop_t, pop_ln)
    # (b) — and a far-OTM crash hedge that only pays in the tail gains POP.
    crash_put = [Leg(1, False, 75.0, 0.10)]
    assert _pop_terminal(crash_put, spot, iv, t, df=5.0) \
        > 5.0 * _pop_terminal(crash_put, spot, iv, t), \
        "t must assign real probability to the crash region the normal zeroes"

    # 3. Friction strictly lowers POP; friction-adjusted BE moves outward.
    assert _pop_terminal(call, spot, iv, t, df=5.0, threshold=0.5) \
        < _pop_terminal(call, spot, iv, t, df=5.0)
    be_raw = _breakevens(call, 25.0, 250.0)
    be_net = _breakevens(call, 25.0, 250.0, offset=0.5)
    assert abs(be_raw[0] - 103.0) < 0.01 and abs(be_net[0] - 103.5) < 0.01

    # 4. evaluate_strategy plumbing: smile sigma + friction + classic field.
    r = evaluate_strategy("strangle", strangle, 500, spot, iv, t,
                          leg_ivs=[0.32, 0.30], friction_rupees=80.0)
    d = r.to_dict()
    assert d["pop_classic_pct"] is not None and d["pop_pct"] is not None
    assert d["pop_pct"] < d["pop_classic_pct"], d       # realism gap visible
    assert abs(d["iv_used_pct"] - 31.0) < 1e-6          # mean of leg IVs
    assert d["tail_df"] == TAIL_DF and "breakevens_net" in d
    # net breakevens sit INSIDE the raw ones for a short strangle
    assert d["breakevens_net"][0] > d["breakevens"][0]
    assert d["breakevens_net"][-1] < d["breakevens"][-1]

    # 5. tail_df<=2 falls back to lognormal exactly.
    assert _pop_terminal(call, spot, iv, t, df=None) == \
        evaluate_strategy("c", call, 1, spot, iv, t, tail_df=0.0).pop

    print("payoff.py self-test OK — t-CDF exact; fat tails trim short-premium "
          f"POP {100*pop_ln:.1f}% -> {100*pop_t:.1f}%; friction + smile wired")

"""The intraday leaf contract — every agent speaks exactly this language.

    fn(inp: AgentInput) -> AgentResult

AgentInput bundles everything an agent might read (bars + option chain +
positioning + session context), so the same signature serves price-structure,
momentum, flow and volatility agents. Pure functions: no I/O, no globals.

AgentResult mirrors swing_hyena's: scored (score_buy/sell in [0,100]) | N/A
(na=reason, renormalised away by the combiner) | VETO (kills the signal), plus
side-scoped veto_long / veto_short. shadow=True agents compute+log at weight 0.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from ..options import greeks_for
from ..options.models import Chain


# ── the input bundle ────────────────────────────────────────────────────────
@dataclass
class AgentInput:
    symbol: str                        # underlying, e.g. "NIFTY"
    bars: Any                          # this underlying's intraday bars (pandas DataFrame)
    index_bars: Any                    # index bars for regime context (pandas DataFrame)
    chain: Chain | None                # option chain w/ greeks/IV/oi/prev_oi
    spot: float
    now: dt.datetime
    cfg: dict
    vix: float | None = None
    prev_day: dict = field(default_factory=dict)      # {pdh, pdl, pdc, pdo}
    session: dict = field(default_factory=dict)        # {minutes_since_open, minutes_to_close, or_hi, or_lo, ...}
    positioning: dict = field(default_factory=dict)    # optional oisweep enrichment
    _chain_feats: dict | None = None                   # memoised chain_features

    def feats(self) -> dict:
        if self._chain_feats is None:
            self._chain_feats = chain_features(self.chain)
        return self._chain_feats


# ── result ───────────────────────────────────────────────────────────────────
@dataclass
class AgentResult:
    agent: str
    family: str
    score_buy: float | None = None
    score_sell: float | None = None
    na: str | None = None
    veto: str | None = None
    veto_long: str | None = None
    veto_short: str | None = None
    shadow: bool = False
    detail: str = ""

    @property
    def scored(self) -> bool:
        return self.na is None and self.veto is None


def clip01(x) -> float:
    """Clamp to [0,100]; non-finite propagates as NaN (the wrapper -> N/A).
    NOT max(0,min(100,nan)) which returns 100 in Python — a silent max BUY."""
    if x is None or not math.isfinite(float(x)):
        return float("nan")
    return max(0.0, min(100.0, float(x)))


def z_to_score(z, scale: float = 20.0) -> float:
    """Map a z-score to [0,100] centred at 50 (+/-2.5z saturates)."""
    if z is None or not math.isfinite(float(z)):
        return float("nan")
    return clip01(50.0 + scale * float(z))


REGISTRY: dict[str, tuple] = {}     # name -> (family, wrapped_fn, shadow)


def agent(name: str, family: str, shadow: bool = False) -> Callable:
    def deco(fn):
        def wrapped(inp: AgentInput) -> AgentResult:
            try:
                r = fn(inp)
            except Exception as e:                 # a leaf may never kill the tree
                r = AgentResult(name, family, na=f"error: {type(e).__name__}: {e}")
            r.agent, r.family = name, family
            if shadow:
                r.shadow = True
            if r.scored:                            # non-finite score = missing data -> N/A
                bad = [v is None or not math.isfinite(float(v))
                       for v in (r.score_buy, r.score_sell)]
                if any(bad):
                    return AgentResult(name, family, shadow=r.shadow,
                                       na=f"non-finite score (missing input): {r.detail}"[:160])
            return r
        REGISTRY[name] = (family, wrapped, shadow)
        return wrapped
    return deco


def run_all(inp: AgentInput) -> list[AgentResult]:
    return [fn(inp) for _, (fam, fn, sh) in sorted(REGISTRY.items())]


def run_family(inp: AgentInput, family: str) -> list[AgentResult]:
    return [fn(inp) for _, (fam, fn, sh) in sorted(REGISTRY.items()) if fam == family]


# ── chain-derived features (the shared input for F/V agents) ─────────────────
def chain_features(chain: Chain | None) -> dict:
    """PCR, OI walls, max-pain, ATM IV, net GEX, 25-delta skew, ATM straddle and
    per-side OI-change from a greeks-complete Chain. Returns {} if no chain.

    net_gex sign convention: calls contribute +gamma*oi, puts -gamma*oi (the
    dealer-short-gamma reading), scaled by spot^2/100. Positive → dealers long
    gamma → mean-reverting (pinning) regime; negative → trend-amplifying.
    """
    if chain is None or not chain.quotes:
        return {}
    calls, puts = chain.calls(), chain.puts()
    call_oi = sum(q.oi for q in calls)
    put_oi = sum(q.oi for q in puts)
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else float("nan")
    call_wall = max(calls, key=lambda q: q.oi).strike if calls else float("nan")
    put_wall = max(puts, key=lambda q: q.oi).strike if puts else float("nan")

    # max pain: strike minimising total intrinsic paid out by writers
    strikes = chain.strikes
    def _pain(k: float) -> float:
        tot = 0.0
        for q in calls:
            tot += max(0.0, k - q.strike) * q.oi   # writers pay ITM calls
        for q in puts:
            tot += max(0.0, q.strike - k) * q.oi
        return tot
    max_pain = min(strikes, key=_pain) if strikes else float("nan")

    atm = chain.atm
    atm_c, atm_p = chain.get(atm, True), chain.get(atm, False)
    ivs = [q.iv for q in (atm_c, atm_p) if q and q.iv]
    atm_iv = (sum(ivs) / len(ivs)) if ivs else float("nan")
    atm_straddle = ((atm_c.mid if atm_c else 0.0) + (atm_p.mid if atm_p else 0.0)) or float("nan")

    # net GEX from per-strike gamma * oi
    gex = 0.0
    for q in chain.quotes:
        g = greeks_for(q, chain)
        gm = g.get("gamma") or 0.0
        gex += (gm * q.oi) * (1 if q.is_call else -1)
    net_gex = gex * (chain.spot ** 2) / 100.0

    # 25-delta skew: IV(put, delta~-0.25) - IV(call, delta~+0.25)
    def _near_delta(quotes, target):
        best, bestd = None, 1e9
        for q in quotes:
            if not q.iv:
                continue
            d = greeks_for(q, chain).get("delta")
            if d is None:
                continue
            if abs(d - target) < bestd:
                best, bestd = q, abs(d - target)
        return best
    p25, c25 = _near_delta(puts, -0.25), _near_delta(calls, 0.25)
    skew_25 = ((p25.iv - c25.iv) if (p25 and c25 and p25.iv and c25.iv)
               else float("nan"))

    # per-side OI change (needs prev_oi; 0 prev_oi = unknown, skipped)
    call_doi = sum(q.d_oi for q in calls if q.prev_oi > 0)
    put_doi = sum(q.d_oi for q in puts if q.prev_oi > 0)
    # OTM call footprint: max volume/oi over calls above spot
    otm_call_voloi = max((q.volume / q.oi for q in calls
                          if q.strike > chain.spot and q.oi > 0), default=0.0)

    return {"pcr_oi": pcr_oi, "call_oi": call_oi, "put_oi": put_oi,
            "call_wall": call_wall, "put_wall": put_wall, "max_pain": max_pain,
            "atm": atm, "atm_iv": atm_iv, "atm_straddle": atm_straddle,
            "net_gex": net_gex, "skew_25": skew_25,
            "call_doi": call_doi, "put_doi": put_doi,
            "otm_call_voloi": otm_call_voloi}

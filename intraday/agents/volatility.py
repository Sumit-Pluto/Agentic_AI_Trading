"""Family V — VOLATILITY (per candidate).

VRP / IV context (cf. D9/D15), 25-delta skew (cf. D2), ATM straddle behaviour,
and front-vs-next term structure (cf. D10) from intraday chain features.
"""
from __future__ import annotations

import math

import numpy as np

from ._ta import last
from .base import AgentResult, agent, clip01, z_to_score

# ~ (5-min) bars per trading year, for annualising an intraday realised vol
_BARS_PER_YEAR = {"1m": 375 * 252, "3m": 125 * 252, "5m": 75 * 252, "15m": 25 * 252}


def _realized_vol(b, tf: str) -> float:
    if b is None or len(b) < 12:
        return float("nan")
    rets = b["close"].pct_change().dropna().tail(30)
    if len(rets) < 6:
        return float("nan")
    return float(rets.std(ddof=1) * math.sqrt(_BARS_PER_YEAR.get(tf, 75 * 252)))


@agent("V1_iv_crush_vrp", "V")
def v1(inp):
    """Variance risk premium: ATM IV vs realised intraday vol. High VRP = fear
    premium (favours selling premium / caution long); a fresh IV spike with no
    move is idiosyncratic — stand aside. Primarily a SIZING signal."""
    f = inp.feats()
    iv = f.get("atm_iv")
    if iv is None or iv != iv or iv <= 0:
        return AgentResult("", "", na="ATM IV unavailable")
    rv = _realized_vol(inp.bars, inp.cfg.get("bar_timeframe", "5m"))
    if rv != rv:
        return AgentResult("", "", score_buy=clip01(55 - (iv - 0.15) * 60),
                           score_sell=clip01(55 - (iv - 0.15) * 60),
                           detail=f"IV {iv*100:.0f}% (no RV)")
    vrp = iv * iv - rv * rv
    # high VRP -> premium rich -> caution buying premium; sizing-style symmetric score
    sb = clip01(58 - max(vrp, 0) * 800)
    return AgentResult("", "", score_buy=sb, score_sell=sb,
                       detail=f"IV {iv*100:.0f}% RV {rv*100:.0f}% VRP {vrp:+.3f}")


@agent("V2_put_call_skew", "V")
def v2(inp):
    """25-delta risk reversal (put IV − call IV) normalised by ATM IV (cf. D2).
    A steep put skew is informed put demand → long veto / short candidate."""
    f = inp.feats()
    skew, iv = f.get("skew_25"), f.get("atm_iv")
    if skew is None or skew != skew or not iv:
        return AgentResult("", "", na="skew unavailable")
    rr = skew / iv                                   # normalised risk reversal
    if rr > 0.12:
        return AgentResult("", "", score_buy=25.0, score_sell=72.0,
                           veto_long=f"steep put skew rr {rr:+.2f} — informed put demand",
                           detail=f"put skew rr {rr:+.2f}")
    sb = z_to_score(-rr * 4.0)                        # positive skew (put fear) -> bearish
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb, detail=f"skew rr {rr:+.2f}")


@agent("V3_atm_straddle", "V")
def v3(inp):
    """ATM straddle as % of spot = the market's implied intraday move. Cheap vs
    realised → long vol; rich → short vol. A vol/sizing signal (symmetric)."""
    f = inp.feats()
    st = f.get("atm_straddle")
    if st is None or st != st or inp.spot <= 0:
        return AgentResult("", "", na="ATM straddle unavailable")
    implied = st / inp.spot * 100.0
    rv = _realized_vol(inp.bars, inp.cfg.get("bar_timeframe", "5m"))
    ref = inp.cfg.get("straddle_ref_pct", 0.8)        # typical intraday straddle %
    sb = z_to_score((ref - implied) / max(ref, 0.1) * 0.8)  # cheap straddle -> higher
    return AgentResult("", "", score_buy=sb, score_sell=sb,
                       detail=f"straddle {implied:.2f}% of spot")


@agent("V4_term_structure_kink", "V", shadow=True)
def v4(inp):
    """Front vs next-expiry ATM IV kink (cf. D10): a large front premium flags an
    event window. Needs the next-expiry ATM IV in positioning['next_atm_iv'];
    shadow until the scanner fetches the second expiry."""
    f = inp.feats()
    front = f.get("atm_iv")
    nxt = inp.positioning.get("next_atm_iv")
    if front is None or front != front or nxt is None:
        return AgentResult("", "", na="next-expiry ATM IV not provided")
    kink = (front - nxt) * 100.0
    if kink > 8:
        return AgentResult("", "", veto=f"event kink {kink:.0f} vol-pts — front rich")
    sb = clip01(58 - max(kink, 0) * 2)
    return AgentResult("", "", score_buy=sb, score_sell=sb, detail=f"term kink {kink:+.1f}")

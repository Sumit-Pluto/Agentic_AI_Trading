"""Family M — MOMENTUM (per candidate, intraday bars)."""
from __future__ import annotations

from ._ta import atr, ema, last, rsi, true_range
from .base import AgentResult, agent, clip01, z_to_score


@agent("M1_rsi2", "M")
def m1(inp):
    """Connors RSI(2) extreme, direction-aware: deep-oversold in an up-EMA is a
    long pullback; deep-overbought in a down-EMA is a short."""
    b = inp.bars
    if b is None or len(b) < 15:
        return AgentResult("", "", na="bars thin")
    r = last(rsi(b["close"], 2))
    if r != r:
        return AgentResult("", "", na="rsi2 unavailable")
    up_trend = last(ema(b["close"], 9)) >= last(ema(b["close"], 21))
    if r <= 10 and up_trend:
        return AgentResult("", "", score_buy=clip01(70 + (10 - r)), score_sell=30.0,
                           detail=f"RSI2 {r:.0f} oversold in uptrend")
    if r >= 90 and not up_trend:
        return AgentResult("", "", score_buy=30.0, score_sell=clip01(70 + (r - 90)),
                           detail=f"RSI2 {r:.0f} overbought in downtrend")
    sb = z_to_score((50 - r) / 40.0)            # mild mean-revert tilt otherwise
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb, detail=f"RSI2 {r:.0f}")


@agent("M2_raschke", "M")
def m2(inp):
    """Raschke 3/10 momentum: fast-EMA vs slow-EMA spread, normalised by ATR."""
    b = inp.bars
    if b is None or len(b) < 12:
        return AgentResult("", "", na="bars thin")
    spread = last(ema(b["close"], 3)) - last(ema(b["close"], 10))
    a = last(atr(b))
    if a != a or a <= 0:
        return AgentResult("", "", na="atr unavailable")
    sb = z_to_score(spread / a * 1.2)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"3/10 spread {spread/a:+.2f} ATR")


@agent("M3_volume_shock", "M")
def m3(inp):
    """Participation spike: this bar's volume vs the rolling intraday mean,
    signed by the bar's direction (a big up-bar on volume = demand)."""
    b = inp.bars
    if b is None or len(b) < 20 or "volume" not in b:
        return AgentResult("", "", na="volume history thin")
    vol = last(b["volume"]); ma = b["volume"].tail(20).mean()
    if not ma or ma <= 0:
        return AgentResult("", "", na="no volume baseline")
    shock = vol / ma
    if shock < 1.5:
        return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail=f"vol x{shock:.1f}")
    dir_ = 1.0 if last(b["close"]) >= last(b["open"]) else -1.0
    sb = z_to_score(dir_ * min(shock - 1.0, 3.0) * 0.5)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"vol x{shock:.1f} {'up' if dir_>0 else 'down'} bar")


@agent("M4_range_expansion", "M")
def m4(inp):
    """True-range expansion vs ATR = trend ignition; signed by bar direction."""
    b = inp.bars
    if b is None or len(b) < 15:
        return AgentResult("", "", na="bars thin")
    tr = last(true_range(b)); a = last(atr(b))
    if a != a or a <= 0:
        return AgentResult("", "", na="atr unavailable")
    exp = tr / a
    if exp < 1.3:
        return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail=f"TR x{exp:.1f}")
    dir_ = 1.0 if last(b["close"]) >= last(b["open"]) else -1.0
    sb = z_to_score(dir_ * min(exp - 1.0, 2.0) * 0.7)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"TR x{exp:.1f} expansion")

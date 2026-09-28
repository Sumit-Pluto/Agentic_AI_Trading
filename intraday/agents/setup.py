"""Family S — SETUP / price structure (per candidate, intraday bars).

Reads the underlying's 5m bars + session context (opening range, first-hour
range, previous-day levels) that the scanner precomputes into inp.session /
inp.prev_day.
"""
from __future__ import annotations

from ._ta import atr, last, session_vwap
from .base import AgentResult, agent, clip01, z_to_score


def _atr_now(b):
    a = last(atr(b))
    return a if a == a and a > 0 else max(last(b["close"]) * 0.002, 1e-6)


@agent("S1_orb", "S")
def s1(inp):
    """Opening-range breakout: close beyond the first `or_minutes` hi/lo, size by
    distance in ATRs, require the breaking bar's volume >= recent average."""
    b, s = inp.bars, inp.session
    hi, lo = s.get("or_hi"), s.get("or_lo")
    if b is None or len(b) < 3 or hi is None or lo is None:
        return AgentResult("", "", na="opening range not set")
    if s.get("minutes_since_open", 0) < s.get("or_minutes", 15) + 1:
        return AgentResult("", "", na="still inside the opening range window")
    close = last(b["close"])
    a = _atr_now(b)
    vol = last(b["volume"]); vol_ma = b["volume"].tail(20).mean()
    vol_ok = 1.0 if (vol_ma and vol >= vol_ma) else 0.5
    if close > hi:
        sb = z_to_score((close - hi) / a * vol_ok)
        return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                           detail=f"ORB up +{(close-hi)/a:.1f} ATR")
    if close < lo:
        ss = z_to_score((lo - close) / a * vol_ok)
        return AgentResult("", "", score_buy=100 - ss, score_sell=ss,
                           detail=f"ORB down -{(lo-close)/a:.1f} ATR")
    return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail="inside opening range")


@agent("S2_vwap_reversion", "S")
def s2(inp):
    """Stretch from session VWAP in ATRs. Far above → fade (sell), far below →
    buy — unless the move is trending (handled by momentum/regime), so this is a
    soft mean-revert tilt scaled by stretch."""
    b = inp.bars
    if b is None or len(b) < 10:
        return AgentResult("", "", na="bars thin")
    vwap = last(b["vwap"]) if "vwap" in b else last(session_vwap(b))
    close = last(b["close"])
    if vwap != vwap or vwap <= 0:
        return AgentResult("", "", na="vwap unavailable")
    stretch = (close - vwap) / _atr_now(b)          # +ve above vwap
    sb = z_to_score(-stretch * 0.6)                 # above vwap -> lower buy (fade)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"{stretch:+.1f} ATR from VWAP")


@agent("S3_first_hour_range", "S")
def s3(inp):
    """After the first hour, position vs the first-hour hi/lo — a break with room
    is a trend-day tell; sitting mid-range is chop (neutral)."""
    b, s = inp.bars, inp.session
    fh, fl = s.get("first_hour_hi"), s.get("first_hour_lo")
    if fh is None or fl is None or s.get("minutes_since_open", 0) < 61:
        return AgentResult("", "", na="first hour not complete")
    close = last(b["close"]); rng = max(fh - fl, _atr_now(b))
    pos = (close - fl) / rng            # 0 at low, 1 at high
    if close > fh:
        return AgentResult("", "", score_buy=z_to_score((close - fh) / rng * 2),
                           score_sell=40.0, detail="above first-hour high")
    if close < fl:
        return AgentResult("", "", score_buy=40.0,
                           score_sell=z_to_score((fl - close) / rng * 2),
                           detail="below first-hour low")
    return AgentResult("", "", score_buy=clip01(35 + pos * 30),
                       score_sell=clip01(35 + (1 - pos) * 30),
                       detail=f"inside FH range pos {pos:.2f}")


@agent("S4_prev_day_levels", "S")
def s4(inp):
    """Break/hold of previous-day high/low/close (PDH/PDL/PDC)."""
    b, pd_ = inp.bars, inp.prev_day
    pdh, pdl = pd_.get("pdh"), pd_.get("pdl")
    if pdh is None or pdl is None:
        return AgentResult("", "", na="previous-day levels missing")
    close = last(b["close"]); a = _atr_now(b)
    if close > pdh:
        return AgentResult("", "", score_buy=z_to_score((close - pdh) / a),
                           score_sell=40.0, detail=f"above PDH {pdh:.0f}")
    if close < pdl:
        return AgentResult("", "", score_buy=40.0,
                           score_sell=z_to_score((pdl - close) / a),
                           detail=f"below PDL {pdl:.0f}")
    return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail="between PDH/PDL")


@agent("S5_gap_follow_fade", "S")
def s5(inp):
    """Opening gap vs prev close: a gap that holds its direction after the first
    bars is a follow (trade with it); one that fills is a fade."""
    b, pd_, s = inp.bars, inp.prev_day, inp.session
    pdc = pd_.get("pdc")
    if pdc is None or b is None or len(b) < 2:
        return AgentResult("", "", na="prev close / open bar missing")
    day_open = s.get("day_open") or float(b["open"].iloc[0])
    gap = (day_open - pdc) / pdc * 100.0
    if abs(gap) < 0.15:
        return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail=f"flat open {gap:+.2f}%")
    close = last(b["close"])
    holding = (close - day_open) * (1 if gap > 0 else -1) >= 0    # moving with the gap
    if gap > 0:
        sb = z_to_score(min(abs(gap), 2.0) * (0.7 if holding else -0.5))
    else:
        sb = z_to_score(-min(abs(gap), 2.0) * (0.7 if holding else -0.5))
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"gap {gap:+.2f}% {'follow' if holding else 'fade'}")

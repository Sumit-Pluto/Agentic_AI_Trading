"""Family R — REGIME (index-level, run once per tick).

Folded into {on, scalar, vetoes} by combiner.regime_gate: it scales threshold +
size and can veto the whole session, but never scores an individual candidate.
The scanner feeds these an AgentInput whose bars/chain are the INDEX's.
R6 is the index-level event gate (Catalyst §C2), run in the regime pass so a
macro slot halts the whole book, not one name.
"""
from __future__ import annotations

from ._ta import adx, ema, last, rolling_z
from .base import AgentResult, agent, clip01, z_to_score


@agent("R1_index_trend", "R")
def r1(inp):
    """Index 5m trend: close vs VWAP + fast/slow EMA + ADX strength."""
    b = inp.bars
    if b is None or len(b) < 20:
        return AgentResult("", "", na="index bars thin")
    close = last(b["close"])
    vwap = last(b["vwap"]) if "vwap" in b else float("nan")
    ef, es = last(ema(b["close"], 9)), last(ema(b["close"], 21))
    adx_ = last(adx(b))
    trend = 0.0
    if vwap == vwap:
        trend += 1.0 if close > vwap else -1.0
    trend += 1.0 if ef > es else -1.0
    strength = min(max((adx_ - 15) / 20.0, 0.0), 1.5) if adx_ == adx_ else 0.3
    sb = z_to_score(trend * strength * 0.8)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"trend {trend:+.0f} adx {adx_:.0f} vwap {'>' if close>vwap else '<'}")


@agent("R2_breadth", "R")
def r2(inp):
    """Market breadth — % of the universe trading above its VWAP (the scanner
    computes it across all symbols and passes it in session['breadth_pct'])."""
    bp = inp.session.get("breadth_pct")
    if bp is None:
        return AgentResult("", "", na="breadth not computed (single-symbol run)")
    # 50% = neutral; map 20..80% to a conviction z
    return AgentResult("", "", score_buy=clip01(bp), score_sell=clip01(100 - bp),
                       detail=f"breadth {bp:.0f}% > VWAP")


@agent("R3_vix_regime", "R")
def r3(inp):
    """INDIA VIX level + slope. High/rising vol → risk-off scalar + veto spikes."""
    vix = inp.vix
    if vix is None:
        return AgentResult("", "", na="VIX unavailable")
    slope = inp.session.get("vix_slope", 0.0)          # (vix_now/vix_ref - 1)*100
    if vix >= 28 or slope >= 12:
        return AgentResult("", "", veto=f"VIX {vix:.1f} (slope {slope:+.0f}%) — risk-off")
    # lower VIX = calmer = more risk-on; ~11 calm, ~20 elevated
    sb = clip01(70 - (vix - 12) * 3 - max(slope, 0) * 1.5)
    return AgentResult("", "", score_buy=sb, score_sell=sb,
                       detail=f"VIX {vix:.1f} slope {slope:+.0f}%")


@agent("R4_index_pcr_shift", "R")
def r4(inp):
    """Index PCR-OI level + intrabar shift → directional bias. Rising PCR from
    put writing (price stable) is bullish; falling PCR bearish."""
    f = inp.feats()
    pcr = f.get("pcr_oi")
    if pcr is None or pcr != pcr:
        return AgentResult("", "", na="index chain PCR unavailable")
    d_pcr = inp.positioning.get("d_pcr", 0.0)          # scanner-tracked intrabar change
    # PCR ~0.9-1.0 neutral; >1.2 put-heavy (support/bullish), <0.7 call-heavy (resistance)
    lvl = (pcr - 1.0)
    sb = z_to_score(lvl * 1.2 + d_pcr * 2.0)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"PCR {pcr:.2f} dPCR {d_pcr:+.2f}")


@agent("R5_index_gamma_flip", "R")
def r5(inp):
    """Net dealer GEX sign. Positive (dealers long gamma) → pinning/mean-revert,
    lower conviction; negative → trend-amplifying, higher conviction. A size/
    regime signal (score_buy == score_sell), not directional."""
    f = inp.feats()
    gex = f.get("net_gex")
    if gex is None or gex != gex:
        return AgentResult("", "", na="index gamma unavailable")
    # negative gex -> trend regime -> higher regime score; positive -> pinning
    sb = clip01(55 - (1.0 if gex > 0 else -1.0) * min(abs(gex) / 1e6, 1.0) * 20)
    return AgentResult("", "", score_buy=sb, score_sell=sb,
                       detail=f"net GEX {gex:+.2e} ({'pinning' if gex>0 else 'trending'})")


@agent("R6_index_event_gate", "R")
def r6(inp):
    """Macro event slot (RBI/CPI/budget/expiry) within a window → halt the book.
    Windows come from cfg['event_windows'] = [{'ts': iso, 'label': ...}] or the
    session dict. No event → pass at neutral."""
    mins = inp.session.get("minutes_to_event")
    label = inp.session.get("next_event", "")
    if mins is not None and 0 <= mins <= inp.cfg.get("event_halt_minutes", 10):
        return AgentResult("", "", veto=f"{label or 'macro event'} in {mins:.0f} min — stand aside")
    return AgentResult("", "", score_buy=55.0, score_sell=55.0,
                       detail=(f"next event {label} in {mins:.0f}m" if mins is not None
                               else "no imminent event"))

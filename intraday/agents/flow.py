"""Family F — FLOW / open-interest (per candidate).

Reproduces the scoring shapes of swing_hyena's Family-D options agents (d5 OI
buildup, d12 PCR mechanism, d11 walls/max-pain, d13 OTM footprint, d8 O:S ratio)
using intraday chain-derived features (base.chain_features) instead of EOD
bhavcopy columns. All read inp.feats() + the underlying's recent bars.
"""
from __future__ import annotations

from ._ta import atr, last
from .base import AgentResult, agent, clip01, z_to_score


def _px_move_atr(b, n: int = 3) -> float:
    if b is None or len(b) < n + 1:
        return 0.0
    a = last(atr(b))
    if a != a or a <= 0:
        return 0.0
    return float(b["close"].iloc[-1] - b["close"].iloc[-1 - n]) / a


@agent("F1_oi_buildup", "F")
def f1(inp):
    """OI build-up/unwind quadrant (cf. D5). Net fresh put writing with price
    holding = support building (bullish); fresh call writing with price soft =
    resistance (bearish). prev_oi==0 legs are unknown and skipped upstream."""
    f = inp.feats()
    cd, pd_ = f.get("call_doi"), f.get("put_doi")
    if cd is None or pd_ is None or (cd == 0 and pd_ == 0):
        return AgentResult("", "", na="OI change unavailable (no prev_oi)")
    net = pd_ - cd                                   # put-heavy additions = bullish
    total = abs(cd) + abs(pd_) + 1.0
    px = _px_move_atr(inp.bars)
    sb = z_to_score((net / total) * 1.4 + px * 0.4)
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"dOI put {pd_:+.0f} call {cd:+.0f}, px {px:+.1f} ATR")


@agent("F2_pcr_mechanism", "F")
def f2(inp):
    """PCR-OI level + intrabar shift, mechanism-aware (cf. D12): rising PCR while
    price is stable = fresh put writing near support = bullish confirmation."""
    f = inp.feats()
    pcr = f.get("pcr_oi")
    if pcr is None or pcr != pcr:
        return AgentResult("", "", na="PCR unavailable")
    d_pcr = inp.positioning.get("d_pcr", 0.0)
    stable = abs(_px_move_atr(inp.bars)) < 0.5
    base = 50.0 + (pcr - 1.0) * 25.0
    if d_pcr > 0.05 and stable:
        base += 8.0
    elif d_pcr < -0.05 and stable:
        base -= 8.0
    sb = clip01(base)
    return AgentResult("", "", score_buy=sb, score_sell=clip01(100 - sb),
                       detail=f"PCR {pcr:.2f} dPCR {d_pcr:+.2f}{' stable' if stable else ''}")


@agent("F3_oi_walls_maxpain", "F")
def f3(inp):
    """Strike-OI walls as S/R + max-pain pull (cf. D11). Pinned just under a CE
    wall in a positive-gamma regime late in the day is a long veto."""
    f = inp.feats()
    cw, pw, mp = f.get("call_wall"), f.get("put_wall"), f.get("max_pain")
    spot = inp.spot
    if not (cw == cw and pw == pw) or spot <= 0:
        return AgentResult("", "", na="OI walls unavailable")
    room_up = (cw - spot) / spot * 100.0
    above_pw = (spot - pw) / spot * 100.0
    mins_left = inp.session.get("minutes_to_close", 999)
    near_ce = 0 < room_up < 0.4 and inp.feats().get("net_gex", 0) > 0
    if near_ce and mins_left < 90:
        return AgentResult("", "", score_buy=35.0, score_sell=58.0,
                           veto_long=f"pinned under CE wall {cw:.0f} (pin pocket)",
                           detail=f"under CE wall {cw:.0f}, {mins_left:.0f}m left")
    pull = 0.0
    if mp == mp:
        pull = -(spot - mp) / spot * 100.0           # max-pain drags spot toward it
    sb = clip01(50 + min(room_up, 3) * 4 - max(-above_pw, 0) * 6 + pull * 2)
    return AgentResult("", "", score_buy=sb, score_sell=clip01(100 - sb),
                       detail=f"walls PE {pw:.0f}/CE {cw:.0f} maxpain {mp:.0f}")


@agent("F4_otm_footprint", "F")
def f4(inp):
    """OTM call volume/OI spike with flat price = informed call buying (cf. D13)."""
    f = inp.feats()
    r = f.get("otm_call_voloi", 0.0)
    flat = abs(_px_move_atr(inp.bars)) < 0.5
    if r > 3 and flat:
        return AgentResult("", "", score_buy=64.0, score_sell=36.0,
                           detail=f"OTM call vol/OI {r:.1f} on flat price")
    return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail=f"OTM vol/OI {r:.1f}")


@agent("F5_os_ratio", "F")
def f5(inp):
    """Option-volume / underlying-volume ratio (cf. D8). A spike = news brewing;
    direction from the PCR shift."""
    f = inp.feats()
    b = inp.bars
    opt_vol = (f.get("call_oi", 0) or 0)  # placeholder if chain lacks volume
    chain = inp.chain
    if chain is None or b is None or len(b) < 3:
        return AgentResult("", "", na="chain/bars unavailable")
    opt_vol = sum(q.volume for q in chain.quotes)
    und_vol = float(b["volume"].tail(3).sum())
    if und_vol <= 0 or opt_vol <= 0:
        return AgentResult("", "", na="volume unavailable")
    ratio = opt_vol / und_vol
    thresh = inp.cfg.get("os_ratio_alert", 8.0)
    if ratio < thresh:
        return AgentResult("", "", score_buy=50.0, score_sell=50.0, detail=f"O/S {ratio:.1f}")
    d_pcr = inp.positioning.get("d_pcr", 0.0)
    sb = 60.0 if d_pcr < 0 else 40.0                 # falling PCR w/ activity = call-side
    return AgentResult("", "", score_buy=sb, score_sell=100 - sb,
                       detail=f"O/S {ratio:.1f} dPCR {d_pcr:+.2f}")

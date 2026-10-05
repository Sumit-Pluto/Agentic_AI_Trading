"""Selector feature bundle — everything the FUT-vs-OPT router reads.

Built from objects the scanner/loop already hold (chain, candidate option leg,
signal family scores, session clock). Pure: no I/O, no globals. Missing data
is NaN/None and the router treats it as "no edge claimed", never as zero cost.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field


def _num(x, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if math.isfinite(v) else default


@dataclass
class SelectorFeatures:
    symbol: str = ""
    direction: str = "BUY"          # underlying view
    # option-leg costs
    spread_pct: float = float("nan")
    oi: int = 0
    volume: int = 0
    iv: float | None = None        # leg IV, decimal
    iv_percentile: float | None = None  # 0..1 vs own history; None = unknown
    delta: float | None = None
    theta_per_share: float | None = None  # Rs/share/day (negative for long)
    entry_prem: float = float("nan")
    breakeven_pts: float = float("nan")   # premium + spread cost in underlying pts
    # regime / context
    trend_score: float = float("nan")     # -100..+100 aligned to direction; NaN = unknown
    minutes_to_close: float = float("nan")
    dte_days: float = float("nan")
    event_blackout: bool = False
    atr_pts: float = float("nan")         # expected intraday move proxy
    futures_available: bool = False
    extras: dict = field(default_factory=dict)


def build_features(*, symbol: str, direction: str, chain, quote,
                   greeks: dict | None = None, family_scores: dict | None = None,
                   regime: dict | None = None, session: dict | None = None,
                   iv_percentile: float | None = None,
                   atr_pts: float | None = None,
                   event_blackout: bool = False,
                   futures_available: bool = False) -> SelectorFeatures:
    """Assemble features. `chain`/`quote` may be None (illiquid) — features then
    stay NaN and the router falls back to futures-or-skip, never to a guess."""
    f = SelectorFeatures(symbol=symbol, direction=direction)
    if quote is not None:
        f.spread_pct = _num(getattr(quote, "spread_pct", float("nan")))
        try:
            f.oi = int(getattr(quote, "oi", 0) or 0)
        except (TypeError, ValueError):
            f.oi = 0
        try:
            f.volume = int(getattr(quote, "volume", 0) or 0)
        except (TypeError, ValueError):
            f.volume = 0
        f.iv = getattr(quote, "iv", None)
        ask = _num(getattr(quote, "ask", float("nan")))
        bid = _num(getattr(quote, "bid", float("nan")))
        f.entry_prem = ask if ask == ask else _num(getattr(quote, "ltp", float("nan")))
        half_spread = (ask - bid) / 2.0 if ask == ask and bid == bid and ask >= bid else 0.0
        lot = 1
        if chain is not None:
            lot = int(getattr(chain, "lot_size", 1) or 1)
        # breakeven in underlying points ≈ premium + half-spread, converted
        # through delta (points of underlying per point of premium ≈ 1/delta).
        g = greeks or {}
        d = g.get("delta", getattr(quote, "delta", None))
        try:
            f.delta = abs(float(d)) if d is not None else None
        except (TypeError, ValueError):
            f.delta = None
        th = g.get("theta")
        try:
            f.theta_per_share = float(th) if th is not None else None
        except (TypeError, ValueError):
            f.theta_per_share = None
        if f.entry_prem == f.entry_prem and f.delta and f.delta > 0.05:
            f.breakeven_pts = (f.entry_prem + max(half_spread, 0.0)) / f.delta
    if chain is not None:
        try:
            f.dte_days = float(chain.days_to_expiry)
        except Exception:
            pass
    sess = session or {}
    f.minutes_to_close = _num(sess.get("minutes_to_close", float("nan")))
    # trend aligned to the signal direction: momentum family + regime scalar.
    try:
        fam = dict(family_scores or {})
        m = _num(fam.get("M", float("nan")))
        reg_scalar = _num((regime or {}).get("scalar", float("nan")))
        if m == m:
            f.trend_score = m - 50.0  # >0 means momentum agrees with direction
            if reg_scalar == reg_scalar:
                f.trend_score *= max(reg_scalar, 0.0)
    except Exception:
        pass
    if iv_percentile is not None:
        try:
            f.iv_percentile = max(0.0, min(1.0, float(iv_percentile)))
        except (TypeError, ValueError):
            f.iv_percentile = None
    if atr_pts is not None:
        f.atr_pts = _num(atr_pts)
    f.event_blackout = bool(event_blackout)
    f.futures_available = bool(futures_available)
    return f

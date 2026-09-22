"""signals/swing/engine.py — the Rbknox-at-Order-Block swing fusion.

Decision #2: the Order-Block engine supplies the breakout + OB zone + retrace
tap (the context); the Rbknox (Knoxville) divergence firing at the tap IS the
swing trigger — replacing the OB engine's own pop-out/decisive-close trigger.

Flow (bullish; bearish mirrors):
  1. ``ob_reversal.detect(df)`` — break of a major swing high on a volume spike,
     the OB zone at the base of the impulse, and the retrace tap back into it.
  2. Require the setup has tapped the zone (status retracing/armed/signal) and
     price is currently at/near the OB.
  3. ``compute_knoxville(df)`` — require a **bullish** KD confirmed on the last
     bar (the reversal "showing at the order block").
  4. → swing BUY, carrying the OB levels (entry / stop / target / rr).

Fires only on the most recent bar (no look-ahead — both engines are causal).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from signals.swing.knoxville import (KnoxParams, compute_knoxville,
                                     min_bars_required)
from signals.swing.ob_reversal import OBParams
from signals.swing.ob_reversal import detect as ob_detect

# price must be within this fraction of the OB-zone height of the zone for the
# Knoxville reversal to count as happening "at the order block"
ZONE_NEAR_FRAC = 0.5
# the OB must have been tapped (price returned into the zone) already
TAPPED = ("retracing", "armed", "signal")


@dataclass
class SwingCandidate:
    """One Rbknox+OB swing setup on the last bar."""
    direction: str                 # BUY / SELL
    price: float                   # current close
    entry: float
    stop: float
    target: float
    rr: float | None
    ob_top: float
    ob_bottom: float
    move_pct: float
    knox_rsi: float | None
    knox_k: float | None
    knox_d: float | None
    reasons: list = field(default_factory=list)
    symbol: str | None = None
    segment: str | None = None
    interval: str = "1d"

    @property
    def detail(self) -> str:
        return (f"{self.direction} swing @OB[{self.ob_bottom:.1f}-{self.ob_top:.1f}] "
                f"knox rsi={self.knox_rsi} k={self.knox_k} d={self.knox_d} "
                f"rr={self.rr} tgt={self.target} sl={self.stop} ({self.interval})")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detail"] = self.detail
        return d


def swing_signal(df: pd.DataFrame, symbol: str | None = None,
                 segment: str | None = None, interval: str = "1d",
                 ob_params: OBParams | None = None,
                 knox_params: KnoxParams | None = None) -> SwingCandidate | None:
    """Rbknox-at-OB swing signal on a chronological OHLCV frame, or None."""
    kp = knox_params or KnoxParams()
    if df is None or len(df) < max(60, min_bars_required(kp)):
        return None

    ob = ob_detect(df, ob_params)
    if not ob or ob.get("status") not in TAPPED or ob.get("tap_idx") is None:
        return None

    close = float(df["close"].iloc[-1])
    ob_top, ob_bot = float(ob["ob_top"]), float(ob["ob_bottom"])
    tol = max(ob_top - ob_bot, 1e-9) * ZONE_NEAR_FRAC
    if not (ob_bot - tol) <= close <= (ob_top + tol):   # price at/near the OB
        return None

    kr = compute_knoxville(df, kp)
    side = ob["side"]
    if side == "LONG" and kr.bull:
        direction, entry = "BUY", float(ob.get("entry") or ob_top)
    elif side == "SHORT" and kr.bear:
        direction, entry = "SELL", float(ob.get("entry") or ob_bot)
    else:
        return None

    reasons = list(ob.get("reasons", []))
    reasons.append("Rbknox " + ("bullish" if direction == "BUY" else "bearish")
                   + " reversal at OB")
    return SwingCandidate(
        direction=direction, price=round(close, 2), entry=round(entry, 2),
        stop=float(ob["stop"]), target=float(ob["target"]), rr=ob.get("rr"),
        ob_top=ob_top, ob_bottom=ob_bot, move_pct=float(ob.get("move_pct") or 0.0),
        knox_rsi=kr.rsi, knox_k=kr.stoch_k, knox_d=kr.stoch_d,
        reasons=reasons, symbol=symbol, segment=segment, interval=interval)

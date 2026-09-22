"""Signal engine v5 — the full 3-indicator confirmation stack (user's
design, finalized off the NAUKRI Jul-3 chart):

  1. OBS label = DIRECTION BIAS.  The label text is the direction:
     red "Look to buy" (RSI exits oversold, bottoms)  -> BUY bias,
     green "Look to sell" (RSI exits overbought, tops) -> SELL bias.
     The most recent label of the session holds the bias; triangles are
     chart decorations only.
  2. Supertrend(10, 3.0) = TREND.  Absolute rule preserved: a BUY can
     only fire while the supertrend is bullish (SELL mirrored) — this is
     inherent in the entry engine below.
  3. ST Pullback v2 = ENTRY.  The faithful Pine port
     (signals/pullback.py) decides the actual entry bar: trend leg ->
     retracement into the value zone (0.3-2.2 ATR, wick-based) ->
     momentum confirmation candle near the line, ADX chop filter, per-leg
     and cooldown limits.  A BARE SUPERTREND FLIP IS NOT AN ENTRY — the
     NAUKRI gap-flip BUY that TradingView never printed is exactly what
     this kills.

A signal fires when the v2 entry direction MATCHES the current OBS
bias — PLUS (user rule, re-added 2026-07-05): the OBS TRIANGLES are
direct entries again, kind="triangle": up-triangle = BUY when the
supertrend is bullish, down-triangle = SELL when bearish (trend
agreement absolute, MIN_FLIP_AGE whipsaw guard applies).  The result
then goes to the quant agent tree for numeric confirmation
(engine/scanner.py owns that step).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime

import pandas as pd

from .obs import ObsState, evaluate as obs_evaluate, evaluate_series
from .pullback import MAX_ENTRY_ATR, pullback_series

BUY = "BUY"
SELL = "SELL"


@dataclass
class Candidate:
    symbol: str
    direction: str              # BUY / SELL
    price: float                # close of the trigger bar
    bar_time: datetime
    obs: ObsState
    st_bull: bool
    st_line: float
    kind: str = "pullback"      # every v5 entry is a v2 pullback entry
    armed_by: dict | None = None    # {"label": "Look to sell",
                                    #  "bar_time": iso} — the OBS label
                                    # holding the bias at entry time

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "direction": self.direction,
                "price": self.price, "bar_time": self.bar_time.isoformat(),
                "st_bull": self.st_bull, "st_line": self.st_line,
                "kind": self.kind, "armed_by": self.armed_by,
                "obs": {"rsi": round(self.obs.rsi, 2),
                        "percent_r": round(self.obs.percent_r, 2),
                        "regime": ("up" if self.obs.up_regime else
                                   "down" if self.obs.down_regime else "none")}}


OPEN_GUARD = dtime(9, 30)      # no ENTRIES during gap discovery (bias may
                               # still be set by an earlier label)
# Back-compat aliases (older code imports these names)
PULLBACK_MAX_ATR = MAX_ENTRY_ATR
FADE_EXT_ATR = MAX_ENTRY_ATR
MIN_FLIP_AGE = 3               # superseded by the v2 engine's own
                               # MIN_FLIP_BARS; kept for old imports


def day_signals(df: pd.DataFrame, st_period: int = 10,
                st_factor: float = 3.0) -> list[dict]:
    """All v5 signals of the LAST session in df, in bar order.

    Single source of truth for BOTH the live sweep (check_signal) and the
    day simulator — they can never drift apart again.  Each entry:
    {bar, direction, st_bull, st_line, armed_bar}."""
    if df is None or len(df) < 60:
        return []
    day = df.index[-1].date()
    obs_ser = evaluate_series(df)
    pb = pullback_series(df, st_period, st_factor)

    out: list[dict] = []
    bulls = pb["st_bull"].values
    bias, bias_bar = None, None
    age = 0
    for i, ts in enumerate(df.index):
        # supertrend regime age (anti-whipsaw guard for triangle entries)
        if i > 0 and bool(bulls[i - 1]) == bool(bulls[i]):
            age += 1
        else:
            age = 1
        if ts.date() != day:
            continue
        # bias update first: label text = direction, last label wins
        if bool(obs_ser["label_buy"].iloc[i]):
            bias, bias_bar = BUY, i
        if bool(obs_ser["label_sell"].iloc[i]):
            bias, bias_bar = SELL, i
        if i < 60 or ts.time() < OPEN_GUARD:
            continue
        st_bull = bool(bulls[i])
        # ── v2 pullback entry, gated by OBS label bias ──────────────────
        d = BUY if bool(pb["bull_sig"].iloc[i]) else \
            (SELL if bool(pb["bear_sig"].iloc[i]) else None)
        if d is not None and d == bias:
            out.append({"bar": i, "direction": d, "kind": "pullback",
                        "st_bull": st_bull,
                        "st_line": float(pb["st_line"].iloc[i]),
                        "armed_bar": bias_bar})
            continue
        # ── OBS triangle entry (user rule 2026-07-05: up-triangle = BUY,
        # down-triangle = SELL) — trend agreement is absolute, plus the
        # MIN_FLIP_AGE whipsaw guard ─────────────────────────────────────
        if age >= MIN_FLIP_AGE:
            if bool(obs_ser["buy_tri"].iloc[i]) and st_bull:
                out.append({"bar": i, "direction": BUY, "kind": "triangle",
                            "st_bull": st_bull,
                            "st_line": float(pb["st_line"].iloc[i]),
                            "armed_bar": None})
            elif bool(obs_ser["sell_tri"].iloc[i]) and not st_bull:
                out.append({"bar": i, "direction": SELL, "kind": "triangle",
                            "st_bull": st_bull,
                            "st_line": float(pb["st_line"].iloc[i]),
                            "armed_bar": None})
    return out


def check_signal(symbol: str, df: pd.DataFrame,
                 st_period: int = 10, st_factor: float = 3.0) -> Candidate | None:
    """Live gate: fires when the LAST closed bar is a v5 entry."""
    sigs = day_signals(df, st_period, st_factor)
    if not sigs or sigs[-1]["bar"] != len(df) - 1:
        return None
    s = sigs[-1]
    armed = None
    if s["armed_bar"] is not None:
        armed = {"label": ("Look to buy" if s["direction"] == BUY
                           else "Look to sell"),
                 "bar_time": df.index[s["armed_bar"]].isoformat()}
    elif s["kind"] == "triangle":
        armed = {"label": ("up triangle" if s["direction"] == BUY
                           else "down triangle"),
                 "bar_time": df.index[s["bar"]].isoformat()}
    return Candidate(symbol=symbol, direction=s["direction"],
                     price=float(df["close"].iloc[-1]),
                     bar_time=df.index[-1].to_pydatetime(),
                     obs=obs_evaluate(df),
                     st_bull=s["st_bull"], st_line=s["st_line"],
                     kind=s["kind"], armed_by=armed)

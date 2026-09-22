"""Market-structure indicator — MAJOR swings + HH / HL / LH / LL labels.

The raw fractal-pivot approach treats every little wiggle as a swing, so the
scanner kept breaking *minor* highs instead of the real structural ones. This
module collapses the noise with an ATR-scaled ZigZag: a reversal only counts as a
MAJOR swing once price travels ≥ ``mult × ATR`` against the running extreme. The
surviving pivots are the big turning points (the rectangle zones the client
draws), each labelled relative to the previous same-type pivot:

  * a HIGH  → **HH** (higher high) if above the prior major high, else **LH**
  * a LOW   → **HL** (higher low) if above the prior major low, else **LL**

A bullish trend = sequence of HH + HL; bearish = LH + LL; a flip is a Change of
Character (CHoCH). Small HH/HL can live inside a larger leg — only these major
pivots are returned, so the OB scanner targets the big reversals.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, window: int = 14) -> np.ndarray:
    """Wilder-style Average True Range (simple rolling mean of true range)."""
    pc = np.empty_like(c)
    pc[0] = c[0]
    pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(window, min_periods=max(3, window // 2)).mean().to_numpy()


def zigzag(h: np.ndarray, l: np.ndarray, atr_arr: np.ndarray, mult: float) -> list[tuple]:
    """Major pivots as (idx, price, kind) where kind ∈ {'H','L'}; each leg moves
    ≥ ``mult × ATR`` from the prior extreme (so minor wiggles are dropped)."""
    n = len(h)
    piv: list[tuple] = []
    if n < 3:
        return piv
    direction = 0                 # 0 unknown, +1 up-leg, -1 down-leg
    hh, hh_i = h[0], 0            # running high since last confirmed low
    ll, ll_i = l[0], 0            # running low since last confirmed high
    for i in range(1, n):
        if h[i] > hh:
            hh, hh_i = h[i], i
        if l[i] < ll:
            ll, ll_i = l[i], i
        a = atr_arr[i]
        thr = (mult * a) if (a == a and a > 0) else (hh * 0.01)
        if direction >= 0 and (hh - l[i]) >= thr:        # fell ≥thr from the high → high confirmed
            piv.append((hh_i, float(hh), "H"))
            direction = -1
            ll, ll_i = l[i], i
            hh, hh_i = h[i], i
        elif direction <= 0 and (h[i] - ll) >= thr:       # rose ≥thr from the low → low confirmed
            piv.append((ll_i, float(ll), "L"))
            direction = 1
            hh, hh_i = h[i], i
            ll, ll_i = l[i], i
    return piv


def label_pivots(piv: list[tuple]) -> list[dict]:
    """Tag each major pivot HH/HL/LH/LL vs the previous same-type pivot."""
    out: list[dict] = []
    last_h = last_l = None
    for idx, price, kind in piv:
        if kind == "H":
            label = "HH" if (last_h is not None and price > last_h) else ("LH" if last_h is not None else "H")
            last_h = price
        else:
            label = "HL" if (last_l is not None and price > last_l) else ("LL" if last_l is not None else "L")
            last_l = price
        out.append({"idx": int(idx), "price": round(float(price), 2), "kind": kind, "label": label})
    return out


def analyze(df: pd.DataFrame, atr_window: int = 14, zz_mult: float = 3.0) -> dict:
    """Return {'pivots': [labelled major pivots], 'trend': up|down|None}."""
    if df is None or len(df) < atr_window + 3:
        return {"pivots": [], "trend": None}
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    pivots = label_pivots(zigzag(h, l, atr(h, l, c, atr_window), zz_mult))
    # trend from the last two labels of each type
    labels = [p["label"] for p in pivots]
    trend = None
    if labels:
        recent = labels[-4:]
        ups = sum(1 for x in recent if x in ("HH", "HL"))
        downs = sum(1 for x in recent if x in ("LH", "LL"))
        trend = "up" if ups > downs else ("down" if downs > ups else None)
    return {"pivots": pivots, "trend": trend}

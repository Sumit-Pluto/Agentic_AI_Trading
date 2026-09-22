"""Swing market structure — MAJOR swings + HH / HL / LH / LL labels.

Faithful port of the old intraday ``market_structure`` service. The raw
fractal-pivot approach treats every little wiggle as a swing, so scanners
keep breaking *minor* highs instead of the real structural ones. This module
collapses the noise with an ATR-scaled ZigZag: a reversal only counts as a
MAJOR swing once price travels >= ``zz_mult x ATR`` against the running
extreme. The surviving pivots are the big turning points, each labelled
relative to the previous same-side pivot:

  * a HIGH -> **HH** (higher high) if above the prior major high, else **LH**
  * a LOW  -> **HL** (higher low)  if above the prior major low,  else **LL**
  * the first pivot of each side has no reference -> label ``None``

A bullish trend = sequence of HH + HL; bearish = LH + LL; otherwise RANGE.

IMPORTANT: ``atr`` here is a plain rolling MEAN of true range — deliberately
NOT Wilder/rma smoothing (signals.indicators.atr). The ``zz_mult=3.0`` tuning
was calibrated against this simple mean; swapping in Wilder smoothing changes
the reversal threshold and shifts every pivot.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, window: int = 14) -> np.ndarray:
    """Plain rolling mean of true range (window 14, min_periods max(3, window//2)).

    Deliberately NOT Wilder smoothing — see module docstring.
    """
    pc = np.empty_like(c)
    pc[0] = c[0]
    pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(window, min_periods=max(3, window // 2)).mean().to_numpy()


def zigzag(h: np.ndarray, l: np.ndarray, atr_arr: np.ndarray, mult: float) -> list[tuple]:
    """Major pivots as (idx, price, kind) where kind in {'H','L'}; each leg moves
    >= ``mult x ATR`` from the prior extreme (so minor wiggles are dropped).
    On NaN/non-positive ATR the threshold falls back to ``hh * 0.01``."""
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
        if direction >= 0 and (hh - l[i]) >= thr:        # fell >=thr from the high -> high confirmed
            piv.append((hh_i, float(hh), "H"))
            direction = -1
            ll, ll_i = l[i], i
            hh, hh_i = h[i], i
        elif direction <= 0 and (h[i] - ll) >= thr:       # rose >=thr from the low -> low confirmed
            piv.append((ll_i, float(ll), "L"))
            direction = 1
            hh, hh_i = h[i], i
            ll, ll_i = l[i], i
    return piv


def label_pivots(piv: list[tuple]) -> list[dict]:
    """Tag each major pivot HH/HL/LH/LL vs the previous same-side pivot.
    The first pivot of each side has no reference and gets label None."""
    out: list[dict] = []
    last_h = last_l = None
    for idx, price, kind in piv:
        if kind == "H":
            label = None if last_h is None else ("HH" if price > last_h else "LH")
            last_h = price
        else:
            label = None if last_l is None else ("HL" if price > last_l else "LL")
            last_l = price
        out.append({"i": int(idx), "price": round(float(price), 2),
                    "kind": kind, "label": label})
    return out


def _iso(ts) -> str:
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


def analyze(df: pd.DataFrame, atr_window: int = 14, zz_mult: float = 3.0) -> dict:
    """Return {'pivots': [labelled major pivots], 'trend': 'UP'|'DOWN'|'RANGE'}.

    Each pivot: {'i': int, 'time': iso str, 'price': float,
                 'kind': 'H'|'L', 'label': 'HH'|'HL'|'LH'|'LL'|None}.
    Trend from the last four labels: more HH/HL -> UP, more LH/LL -> DOWN,
    else RANGE (copies the old module's read).
    """
    if df is None or len(df) < atr_window + 3:
        return {"pivots": [], "trend": "RANGE"}
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    pivots = label_pivots(zigzag(h, l, atr(h, l, c, atr_window), zz_mult))
    index = df.index
    for p in pivots:
        p["time"] = _iso(index[p["i"]])
    labels = [p["label"] for p in pivots]
    trend = "RANGE"
    if labels:
        recent = labels[-4:]
        ups = sum(1 for x in recent if x in ("HH", "HL"))
        downs = sum(1 for x in recent if x in ("LH", "LL"))
        trend = "UP" if ups > downs else ("DOWN" if downs > ups else "RANGE")
    return {"pivots": pivots, "trend": trend}


# ═════════════════════════════ self-test ═════════════════════════════════
if __name__ == "__main__":
    def _mk_df(closes: list[float]) -> pd.DataFrame:
        cl = np.asarray(closes, float)
        op = np.roll(cl, 1)
        op[0] = cl[0]
        hi = np.maximum(op, cl) + 0.2
        lo = np.minimum(op, cl) - 0.2
        idx = pd.date_range("2026-06-29 09:15", periods=len(cl), freq="5min")
        return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": cl,
                             "volume": np.full(len(cl), 1000.0)}, index=idx)

    def _path(start: float, legs: list[float], step: float = 3.0) -> list[float]:
        # warm-up chop keeps ATR realistic through the min_periods window
        p = [start, start + step] * 4
        cur = p[-1]
        for move in legs:
            sgn = 1.0 if move > 0 else -1.0
            for _ in range(int(round(abs(move) / step))):
                cur += sgn * step
                p.append(cur)
        return p

    # ── uptrend: +30 legs with -15 pullbacks (ATR≈3.4, thr≈10.2) ─────────
    up_df = _mk_df(_path(1000.0, [30, -15, 30, -15, 30, -15, 30]))
    res = analyze(up_df)
    piv = res["pivots"]
    assert len(piv) >= 6, f"expected >=6 pivots, got {len(piv)}"
    kinds = [p["kind"] for p in piv]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), f"kinds not alternating: {kinds}"
    labels = [p["label"] for p in piv]
    assert labels == [None, None, "HL", "HH", "HL", "HH", "HL"], f"labels: {labels}"
    assert res["trend"] == "UP", f"trend: {res['trend']}"
    for p in piv:
        assert isinstance(p["i"], int) and isinstance(p["price"], float)
        assert p["time"] == up_df.index[p["i"]].isoformat()
    hs = [p["price"] for p in piv if p["kind"] == "H"]
    ls = [p["price"] for p in piv if p["kind"] == "L"]
    assert hs == sorted(hs) and ls == sorted(ls), "uptrend extremes must ascend"

    # ── downtrend mirror ─────────────────────────────────────────────────
    dn_df = _mk_df(_path(1000.0, [-30, 15, -30, 15, -30, 15, -30]))
    res_dn = analyze(dn_df)
    labels_dn = [p["label"] for p in res_dn["pivots"]]
    assert labels_dn == [None, None, "LH", "LL", "LH", "LL", "LH"], f"labels: {labels_dn}"
    assert res_dn["trend"] == "DOWN", f"trend: {res_dn['trend']}"

    # ── degenerate inputs ────────────────────────────────────────────────
    assert analyze(None) == {"pivots": [], "trend": "RANGE"}
    assert analyze(up_df.iloc[:10]) == {"pivots": [], "trend": "RANGE"}

    # ── ATR is a plain rolling mean, not Wilder ──────────────────────────
    h = up_df["high"].to_numpy(float)
    l = up_df["low"].to_numpy(float)
    c = up_df["close"].to_numpy(float)
    a = atr(h, l, c, 14)
    assert np.isnan(a[:6]).all() and not np.isnan(a[6:]).any(), "min_periods=7 warm-up"
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    assert abs(a[20] - tr[7:21].mean()) < 1e-9, "ATR must be the plain rolling mean of TR"

    print("pivots:", [(p["i"], p["kind"], p["label"], p["price"]) for p in piv])
    print("trend up:", res["trend"], "| trend down:", res_dn["trend"])
    print("self-test OK")

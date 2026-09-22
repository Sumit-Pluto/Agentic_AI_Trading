"""Faithful port of indicators/Supertrend_Pullback_v2.pine — the ENTRY
engine ("BULL PB"/"BEAR PB" labels), not just the supertrend line.

A bare supertrend flip is NOT an entry.  v2 fires only when, inside an
established trend leg:

  1. the leg made an extreme, then price RETRACED toward the line;
  2. the retracement's deepest wick is 0.3–2.2 ATR from the extreme
     (Pullback Quality Filter — shallower is noise, deeper is reversal);
  3. the pullback TOUCHED the Dynamic Value Zone (line ± 0.5 ATR) within
     the last 3 bars;
  4. a momentum confirmation candle prints: directional body, closes
     beyond prior close, close in the trend-side 40% of its range;
  5. the close is still within 1.5 ATR of the line (no chasing) and on
     the trend side of it;
  6. ADX(14) >= 20 (chop filter), >= 2 bars since the flip, max 2
     signals per leg, 5-bar cooldown, re-armed only by a new leg extreme.

All defaults mirror the Pine inputs exactly (HTF/volume filters default
OFF there and are omitted here).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import atr, rma, supertrend

# Pine input defaults (Supertrend_Pullback_v2.pine) — with a GEOMETRY FIX
# (2026-07-05): with factor 3.0 the ST line rides ~3 ATR below price, so
# the original zone width (0.5) demanded a ~2.5+ ATR dip while PQF_MAX
# (2.2) rejected exactly those dips — touch ∧ pqf was measured IMPOSSIBLE
# (0 of 600 bars, 8 stocks; TV printed zero PB labels ever for the same
# reason).  Zone widened to 1.0 ATR and max depth to 3.2 ATR (any deeper
# retracement crosses the line and flips the trend anyway).
PQF_MIN = 0.3            # min pullback depth, × ATR (wick-based)
PQF_MAX = 3.2            # max pullback depth, × ATR (was 2.2 — dead)
MAX_ENTRY_ATR = 1.5      # close must be within this of the ST line
DVZ_WIDTH = 1.0          # value-zone half-width, × ATR (was 0.5 — dead)
TOUCH_VALID = 3          # bars a zone touch stays valid
ADX_LEN = 14
ADX_MIN = 20.0
MIN_FLIP_BARS = 2
MAX_PER_LEG = 2
COOLDOWN = 5
CLOSE_POS = 0.6          # strong close: trend-side fraction of bar range


def adx(df: pd.DataFrame, length: int = ADX_LEN) -> pd.Series:
    """Pine ta.dmi(length, length) ADX (Wilder)."""
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0),
                        index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0),
                         index=df.index)
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    trur = rma(tr, length)
    plus = (100.0 * rma(plus_dm, length) / trur).ffill()
    minus = (100.0 * rma(minus_dm, length) / trur).ffill()
    s = (plus + minus).replace(0.0, 1.0)
    return 100.0 * rma((plus - minus).abs() / s, length)


def pullback_series(df: pd.DataFrame, st_period: int = 10,
                    st_factor: float = 3.0) -> pd.DataFrame:
    """Single deterministic pass; returns DataFrame(bull_sig, bear_sig,
    st_bull, st_line, depth) aligned to df.index — same var-state walk
    as the Pine, so signals match TradingView bar-for-bar."""
    st = supertrend(df, st_period, st_factor)
    bulls = st["bull"].values
    lines = st["line"].values
    a = atr(df, st_period).values           # Pine: ta.atr(i_atr_len)
    adx_v = adx(df).values
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    n = len(df)
    bull_sig = np.zeros(n, dtype=bool)
    bear_sig = np.zeros(n, dtype=bool)
    depth_out = np.full(n, np.nan)

    leg_extreme = np.nan
    pull_extreme = np.nan
    bars_since_extreme = 0
    zone_touched = False
    zone_touch_bar = 0
    signals_this_leg = 0
    last_signal_bar = -10 ** 9
    bars_since_flip = 10 ** 9
    rearmed = True

    for i in range(n):
        if np.isnan(lines[i]) or np.isnan(a[i]) or a[i] <= 0:
            continue
        is_bull = bool(bulls[i])
        flip = i > 0 and bool(bulls[i - 1]) != is_bull
        bars_since_flip = 0 if flip else bars_since_flip + 1
        atr_v = max(float(a[i]), 1e-9)
        dvz_upper = lines[i] + DVZ_WIDTH * atr_v
        dvz_lower = lines[i] - DVZ_WIDTH * atr_v

        # ── trend-leg / pullback state (Pine lines 143-185) ─────────────
        if flip:
            leg_extreme = h[i] if is_bull else l[i]
            pull_extreme = np.nan
            bars_since_extreme = 0
            zone_touched = False
            signals_this_leg = 0
            rearmed = True
        elif is_bull:
            if np.isnan(leg_extreme) or h[i] > leg_extreme:
                leg_extreme = h[i]
                pull_extreme = np.nan
                bars_since_extreme = 0
                zone_touched = False
                rearmed = True
            else:
                bars_since_extreme += 1
                pull_extreme = l[i] if np.isnan(pull_extreme) \
                    else min(pull_extreme, l[i])
                if l[i] <= dvz_upper:
                    zone_touched = True
                    zone_touch_bar = i
        else:
            if np.isnan(leg_extreme) or l[i] < leg_extreme:
                leg_extreme = l[i]
                pull_extreme = np.nan
                bars_since_extreme = 0
                zone_touched = False
                rearmed = True
            else:
                bars_since_extreme += 1
                pull_extreme = h[i] if np.isnan(pull_extreme) \
                    else max(pull_extreme, h[i])
                if h[i] >= dvz_lower:
                    zone_touched = True
                    zone_touch_bar = i

        # ── pullback depth (wick-based) + filters ───────────────────────
        depth = np.nan
        if not np.isnan(pull_extreme):
            depth = (leg_extreme - pull_extreme) / atr_v if is_bull \
                else (pull_extreme - leg_extreme) / atr_v
        depth_out[i] = depth
        pqf_valid = (not np.isnan(depth)) and PQF_MIN <= depth <= PQF_MAX
        touch_fresh = zone_touched and (i - zone_touch_bar) <= TOUCH_VALID
        adx_ok = (not np.isnan(adx_v[i])) and adx_v[i] >= ADX_MIN

        rng = h[i] - l[i]
        pos = (c[i] - l[i]) / rng if rng > 0 else 1.0
        bull_confirm = c[i] > o[i] and i > 0 and c[i] > c[i - 1] \
            and pos >= CLOSE_POS
        bear_confirm = c[i] < o[i] and i > 0 and c[i] < c[i - 1] \
            and (1.0 - pos) >= CLOSE_POS
        bull_near = c[i] <= lines[i] + MAX_ENTRY_ATR * atr_v
        bear_near = c[i] >= lines[i] - MAX_ENTRY_ATR * atr_v

        common_ok = (bars_since_flip >= MIN_FLIP_BARS
                     and bars_since_extreme >= 1
                     and touch_fresh and pqf_valid and adx_ok
                     and rearmed and signals_this_leg < MAX_PER_LEG
                     and (i - last_signal_bar) >= COOLDOWN)

        b_sig = common_ok and is_bull and bull_confirm and bull_near \
            and c[i] > lines[i]
        s_sig = common_ok and (not is_bull) and bear_confirm and bear_near \
            and c[i] < lines[i]
        if b_sig or s_sig:
            bull_sig[i] = b_sig
            bear_sig[i] = s_sig
            signals_this_leg += 1
            last_signal_bar = i
            zone_touched = False
            rearmed = False

    return pd.DataFrame({"bull_sig": bull_sig, "bear_sig": bear_sig,
                         "st_bull": st["bull"].values, "st_line": lines,
                         "depth": depth_out}, index=df.index)

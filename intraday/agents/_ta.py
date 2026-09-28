"""Shared intraday technical primitives — one definition, used by every family.

Operates on an intraday bar DataFrame with columns:
    time, open, high, low, close, volume, prev_close, vwap
(prev_close = previous bar's close; vwap = session cumulative VWAP, precomputed
by the bar builder). All functions are NaN-tolerant and never raise on short
history — an agent that gets NaN back degrades to N/A via the @agent wrapper.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(bars: pd.DataFrame) -> pd.Series:
    """Wilder true range: max(H-L, |H-Cprev|, |L-Cprev|)."""
    h, l_, pc = bars["high"], bars["low"], bars["prev_close"]
    return np.maximum(h - l_, np.maximum((h - pc).abs(), (l_ - pc).abs()))


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(bars).rolling(n, min_periods=max(2, n // 2)).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=max(2, n // 2)).mean()


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=max(2, n // 2)).mean()


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI. n=2 gives the Connors RSI2 momentum extreme."""
    d = series.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    rs = (up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
          / dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().replace(0, np.nan))
    return 100.0 - 100.0 / (1.0 + rs)


def adx(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ADX — trend strength (>=25 = trending). Ported from x_tree._adx."""
    up = bars["high"].diff()
    dn = -bars["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(bars)
    atr_ = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=bars.index).ewm(
        alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_.replace(0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=bars.index).ewm(
        alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    """Cumulative session VWAP from typical price * volume. The bar builder
    normally precomputes a `vwap` column; this is the fallback / offline path."""
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol = bars["volume"].fillna(0.0)
    cum_v = vol.cumsum().replace(0, np.nan)
    return (tp * vol).cumsum() / cum_v


def donchian_high(bars: pd.DataFrame, n: int) -> pd.Series:
    return bars["high"].rolling(n, min_periods=max(2, n // 2)).max()


def donchian_low(bars: pd.DataFrame, n: int) -> pd.Series:
    return bars["low"].rolling(n, min_periods=max(2, n // 2)).min()


def rolling_z(series: pd.Series, n: int) -> pd.Series:
    m = series.rolling(n, min_periods=max(2, n // 2)).mean()
    s = series.rolling(n, min_periods=max(2, n // 2)).std(ddof=1)
    return (series - m) / s.replace(0, np.nan)


def pct_rank(series: pd.Series, window: int | None = None) -> float:
    """Percentile of the LAST value within `series` (0..100). Rough IV-rank /
    percentile primitive; None-safe, returns NaN on <2 points."""
    v = series.dropna()
    if window:
        v = v.tail(window)
    if len(v) < 2:
        return float("nan")
    last = v.iloc[-1]
    return float((v <= last).mean() * 100.0)


def last(series: pd.Series, default: float = float("nan")) -> float:
    v = series.dropna()
    return float(v.iloc[-1]) if len(v) else default

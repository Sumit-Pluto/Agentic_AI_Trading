"""Indicator math shared by the signal engine and quant agents.

All functions take pandas Series/DataFrames of a 5-minute candle history
(oldest first, columns: open, high, low, close, volume) and return Series
aligned to the input index. Wilder smoothing matches TradingView's ta.rma
so the Python signals reproduce the Pine originals bar-for-bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rma(series: pd.Series, length: int) -> pd.Series:
    """TradingView ta.rma (Wilder's smoothing)."""
    return series.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    up = rma(delta.clip(lower=0.0), length)
    down = rma((-delta).clip(lower=0.0), length)
    rs = up / down.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[down == 0] = 100.0
    out[up == 0] = 0.0
    return out


def williams_r(df: pd.DataFrame, length: int = 24,
               src: str = "close") -> pd.Series:
    hh = df["high"].rolling(length).max()
    ll = df["low"].rolling(length).min()
    return 100.0 * (df[src] - hh) / (hh - ll)


def atr(df: pd.DataFrame, length: int = 10) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, length)


def supertrend(df: pd.DataFrame, period: int = 10,
               factor: float = 3.0) -> pd.DataFrame:
    """Returns DataFrame(line, bull) matching Pine ta.supertrend semantics:
    bull=True when price is above the line (Pine direction < 0)."""
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = hl2 + factor * a
    lower_basic = hl2 - factor * a

    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    bull = np.full(n, False)
    close = df["close"].values
    ub, lb = upper_basic.values, lower_basic.values

    for i in range(n):
        if np.isnan(ub[i]):
            continue
        if np.isnan(upper[i - 1]) if i > 0 else True:
            upper[i], lower[i] = ub[i], lb[i]
            bull[i] = True
            line[i] = lower[i]
            continue
        upper[i] = ub[i] if (ub[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = lb[i] if (lb[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        if bull[i - 1]:
            bull[i] = close[i] >= lower[i]
        else:
            bull[i] = close[i] > upper[i]
        line[i] = lower[i] if bull[i] else upper[i]

    return pd.DataFrame({"line": line, "bull": bull}, index=df.index)


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP: resets each trading day (index must be tz-aware
    or naive datetimes)."""
    day = pd.Series(df.index.date, index=df.index)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_pv / cum_v


def ema(close: pd.Series, length: int) -> pd.Series:
    return close.ewm(span=length, min_periods=length, adjust=False).mean()


def sma(close: pd.Series, length: int) -> pd.Series:
    return close.rolling(length).mean()

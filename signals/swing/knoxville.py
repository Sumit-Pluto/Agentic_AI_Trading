"""Python port of RB_Indicator.pine — Rob Booker "Knoxville Divergence" (KD).

Goal: reproduce TradingView's signal logic bar-for-bar so the scanner reports the
same Bullish/Bearish KD that the Pine indicator prints. The Mean-Reversion band
layer in the Pine script is explicitly a *visual* confirmation (it does not gate
the signal), so it is intentionally NOT implemented here — the scanner signal is
exactly `showBullKD` / `showBearKD`.

Fidelity notes:
  * RSI uses Wilder's RMA smoothing, matching TradingView's `ta.rsi`. The RMA
    seed differs from TradingView's by a hair on the *first* ~length bars, but it
    converges to identical values long before the latest bar (which is all the
    scanner reads), so it does not affect signals.
  * The only remaining source of divergence from a TradingView chart is the
    underlying OHLC feed (yfinance vs TradingView's vendor, dividend adjustment).
    That is tuned at the data layer, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class KnoxParams:
    # Divergence
    lookback: int = 200     # search window for the prior price extreme
    min_bars: int = 1       # min bars between the two pivots
    # Momentum
    mom_len: int = 20
    # RSI filter
    rsi_len: int = 14
    rsi_upper: int = 70
    rsi_lower: int = 30
    # Stoch-RSI confirmation
    use_stoch: bool = True
    stoch_win: int = 5      # confirm within N bars of the KD
    st_rsi_len: int = 14
    st_len: int = 14
    st_ksm: int = 3
    st_dsm: int = 3


@dataclass
class KnoxResult:
    bull: bool          # bullish KD confirmed (alert) on the last bar
    bear: bool          # bearish KD confirmed (alert) on the last bar
    # Whether the divergence PIVOT itself is on the last bar — i.e. the KD label
    # prints on the current candle, not 1-N bars back (confirmation can lag the
    # pivot by up to stoch_win bars).
    bull_div_last: bool
    bear_div_last: bool
    close: float
    rsi: float | None
    stoch_k: float | None
    stoch_d: float | None


# ----------------------------- TA primitives -------------------------------
def _rma(values: np.ndarray, length: int) -> np.ndarray:
    """Wilder's RMA (TradingView ta.rma): seed = SMA of first `length`, then
    rma = alpha*v + (1-alpha)*rma_prev with alpha = 1/length."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < length or length <= 0:
        return out
    alpha = 1.0 / length
    out[length - 1] = float(np.mean(values[:length]))
    for i in range(length, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _wilder_rsi(close: np.ndarray, length: int) -> np.ndarray:
    """TradingView ta.rsi(close, length)."""
    n = len(close)
    if n < length + 1:
        return np.full(n, np.nan)
    change = np.diff(close, prepend=close[0])  # change[0] = 0 (treated as flat)
    gain = np.where(change > 0, change, 0.0)
    loss = np.where(change < 0, -change, 0.0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)
    rsi = np.full(n, np.nan)
    valid = ~np.isnan(avg_gain) & ~np.isnan(avg_loss)
    # down == 0 -> 100 ; up == 0 -> 0 ; else 100 - 100/(1+up/down)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss != 0, avg_gain / avg_loss, np.nan)
        base = 100.0 - 100.0 / (1.0 + rs)
    rsi[valid] = base[valid]
    rsi[valid & (avg_loss == 0)] = 100.0
    rsi[valid & (avg_gain == 0)] = 0.0
    return rsi


def _sma(values: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(values).rolling(length, min_periods=length).mean().to_numpy()


def _stoch_rsi(rsi_src: np.ndarray, st_len: int) -> np.ndarray:
    """ta.stoch(src, src, src, st_len) = 100 * (src - lowest) / (highest - lowest)."""
    s = pd.Series(rsi_src)
    hh = s.rolling(st_len, min_periods=st_len).max()
    ll = s.rolling(st_len, min_periods=st_len).min()
    rng = (hh - ll)
    out = 100.0 * (s - ll) / rng
    out = out.where(rng != 0, 0.0)  # flat window -> 0 (avoid div-by-zero)
    return out.to_numpy()


def _rolling_max(values: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(values).rolling(length, min_periods=1).max().to_numpy()


def _rolling_min(values: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(values).rolling(length, min_periods=1).min().to_numpy()


def min_bars_required(p: KnoxParams) -> int:
    """Bars needed before the last-bar signal is trustworthy."""
    return p.lookback + max(p.mom_len, p.rsi_len, p.st_rsi_len + p.st_len) + 5


# ----------------------------- main port -----------------------------------
def _compute(df: pd.DataFrame, p: KnoxParams) -> dict:
    """Run the full KD logic over a chronological OHLC frame and return every
    per-bar series + signal flag. Shared by compute_knoxville and explain."""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)

    mom = close - np.concatenate([np.full(p.mom_len, np.nan), close[:-p.mom_len]]) \
        if p.mom_len < n else np.full(n, np.nan)
    rsi = _wilder_rsi(close, p.rsi_len)

    # Stoch-RSI %K (blue) and %D (red).
    stoch_src = _wilder_rsi(close, p.st_rsi_len)
    k_line = _sma(_stoch_rsi(stoch_src, p.st_len), p.st_ksm)
    d_line = _sma(k_line, p.st_dsm)

    roll_high = _rolling_max(high, p.lookback)
    roll_low = _rolling_min(low, p.lookback)
    roll_rsi_hi = _rolling_max(rsi, p.lookback)
    roll_rsi_lo = _rolling_min(rsi, p.lookback)

    show_bear = np.zeros(n, dtype=bool)
    show_bull = np.zeros(n, dtype=bool)
    # per-bar diagnostics (for the explain endpoint)
    hi_piv = np.zeros(n, dtype=bool)
    lo_piv = np.zeros(n, dtype=bool)
    bear_div = np.zeros(n, dtype=bool)
    bull_div = np.zeros(n, dtype=bool)
    rdn = np.zeros(n, dtype=bool)
    rup = np.zeros(n, dtype=bool)
    bskd_arr = np.full(n, 10**9, dtype=np.int64)
    bsb_arr = np.full(n, 10**9, dtype=np.int64)

    bars_since_kd = 10**9
    kd_fired = False
    bars_since_bull = 10**9
    kd_bull_fired = False

    for i in range(n):
        bearish_div = False
        bullish_div = False
        is_high_pivot = False
        is_low_pivot = False

        lo = i - p.lookback + 1          # window start (real index)
        hi_end = i - p.min_bars          # window end, inclusive (real index)
        if lo >= 0 and hi_end >= lo:
            # --- prior swing high inside [lo .. hi_end], most-recent on ties ---
            win_h = high[lo:hi_end + 1]
            rel_h = len(win_h) - 1 - int(np.argmax(win_h[::-1]))
            ph_idx = lo + rel_h
            prior_high = high[ph_idx]
            prior_high_mom = mom[ph_idx]
            is_high_pivot = high[i] == roll_high[i]
            if (is_high_pivot and high[i] > prior_high
                    and not np.isnan(mom[i]) and not np.isnan(prior_high_mom)
                    and mom[i] < prior_high_mom
                    and not np.isnan(roll_rsi_hi[i]) and roll_rsi_hi[i] >= p.rsi_upper):
                bearish_div = True

            # --- prior swing low inside [lo .. hi_end], most-recent on ties ---
            win_l = low[lo:hi_end + 1]
            rel_l = len(win_l) - 1 - int(np.argmin(win_l[::-1]))
            pl_idx = lo + rel_l
            prior_low = low[pl_idx]
            prior_low_mom = mom[pl_idx]
            is_low_pivot = low[i] == roll_low[i]
            if (is_low_pivot and low[i] < prior_low
                    and not np.isnan(mom[i]) and not np.isnan(prior_low_mom)
                    and mom[i] > prior_low_mom
                    and not np.isnan(roll_rsi_lo[i]) and roll_rsi_lo[i] <= p.rsi_lower):
                bullish_div = True

        # --- Stoch-RSI roll-over (needs current + previous bar) ---
        roll_down = roll_up = False
        if i > 0 and not any(np.isnan(x) for x in
                             (k_line[i], d_line[i], k_line[i - 1], d_line[i - 1])):
            crossunder = k_line[i - 1] >= d_line[i - 1] and k_line[i] < d_line[i]
            crossover = k_line[i - 1] <= d_line[i - 1] and k_line[i] > d_line[i]
            roll_down = ((crossunder or k_line[i] < d_line[i])
                         and k_line[i] <= k_line[i - 1] and d_line[i] <= d_line[i - 1])
            roll_up = ((crossover or k_line[i] > d_line[i])
                       and k_line[i] >= k_line[i - 1] and d_line[i] >= d_line[i - 1])

        # --- bearish state machine ---
        if bearish_div:
            bars_since_kd = 0
            kd_fired = False
        else:
            bars_since_kd += 1
        if p.use_stoch:
            sb = bars_since_kd <= p.stoch_win and roll_down and not kd_fired
            if sb:
                kd_fired = True
        else:
            sb = bearish_div
        show_bear[i] = sb

        # --- bullish state machine (mirror) ---
        if bullish_div:
            bars_since_bull = 0
            kd_bull_fired = False
        else:
            bars_since_bull += 1
        if p.use_stoch:
            su = bars_since_bull <= p.stoch_win and roll_up and not kd_bull_fired
            if su:
                kd_bull_fired = True
        else:
            su = bullish_div
        show_bull[i] = su

        hi_piv[i] = is_high_pivot
        lo_piv[i] = is_low_pivot
        bear_div[i] = bearish_div
        bull_div[i] = bullish_div
        rdn[i] = roll_down
        rup[i] = roll_up
        bskd_arr[i] = bars_since_kd
        bsb_arr[i] = bars_since_bull

    return dict(
        n=n, close=close, rsi=rsi, k_line=k_line, d_line=d_line,
        show_bear=show_bear, show_bull=show_bull,
        hi_piv=hi_piv, lo_piv=lo_piv, bear_div=bear_div, bull_div=bull_div,
        rdn=rdn, rup=rup, bskd=bskd_arr, bsb=bsb_arr,
    )


def compute_knoxville(df: pd.DataFrame, p: KnoxParams) -> KnoxResult:
    """Return whether the last (most recent) bar fired a bull/bear KD."""
    s = _compute(df, p)
    last = s["n"] - 1

    def _f(arr: np.ndarray) -> float | None:
        v = arr[last]
        return None if v is None or np.isnan(v) else round(float(v), 2)

    return KnoxResult(
        bull=bool(s["show_bull"][last]),
        bear=bool(s["show_bear"][last]),
        bull_div_last=bool(s["bull_div"][last]),
        bear_div_last=bool(s["bear_div"][last]),
        close=round(float(s["close"][last]), 2),
        rsi=_f(s["rsi"]),
        stoch_k=_f(s["k_line"]),
        stoch_d=_f(s["d_line"]),
    )


def explain_knoxville(df: pd.DataFrame, p: KnoxParams, tail: int = 24) -> dict:
    """Per-bar breakdown for auditing why a symbol did/didn't signal: the last
    `tail` bars with pivot/divergence/stoch/signal flags, plus the most recent
    signal dates. Use this to compare bar-for-bar against TradingView."""
    s = _compute(df, p)
    n = s["n"]
    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    def _v(arr, i) -> float | None:
        v = arr[i]
        return None if v is None or np.isnan(v) else round(float(v), 2)

    bars = []
    for i in range(max(0, n - tail), n):
        bars.append({
            "date": dates[i],
            "close": round(float(s["close"][i]), 2),
            "rsi": _v(s["rsi"], i),
            "stoch_k": _v(s["k_line"], i),
            "stoch_d": _v(s["d_line"], i),
            "high_pivot": bool(s["hi_piv"][i]),
            "bearish_div": bool(s["bear_div"][i]),
            "low_pivot": bool(s["lo_piv"][i]),
            "bullish_div": bool(s["bull_div"][i]),
            "stoch_roll_down": bool(s["rdn"][i]),
            "stoch_roll_up": bool(s["rup"][i]),
            "bars_since_bear_div": int(s["bskd"][i]) if s["bskd"][i] < 10**8 else None,
            "bars_since_bull_div": int(s["bsb"][i]) if s["bsb"][i] < 10**8 else None,
            "bear_signal": bool(s["show_bear"][i]),
            "bull_signal": bool(s["show_bull"][i]),
        })

    bear_idx = [i for i in range(n) if s["show_bear"][i]]
    bull_idx = [i for i in range(n) if s["show_bull"][i]]
    last = n - 1
    return {
        "last_date": dates[last],
        "fired_on_last_bar": {
            "bear": bool(s["show_bear"][last]),
            "bull": bool(s["show_bull"][last]),
        },
        "last_bear_signal": dates[bear_idx[-1]] if bear_idx else None,
        "last_bull_signal": dates[bull_idx[-1]] if bull_idx else None,
        "bars": bars,
    }

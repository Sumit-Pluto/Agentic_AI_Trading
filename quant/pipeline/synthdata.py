"""quant/pipeline/synthdata.py — dummy market data with a KNOWN injected signal.

Purpose: exercise + verify the whole pipeline with no broker and no paid data. It
generates everything the feature modules consume — per-stock 5-minute candles, a
per-strike option chain (OI + IV per strike), an index series, a daily VIX series,
an event calendar, ban flags, liquidity tiers, era tags — and injects a learnable
relationship so the training test can assert the model recovers real signal.

The injected truth (per stock-day): a hidden `edge in {-1,0,+1}`.
  * candle drift per bar = edge * DRIFT + noise  -> forward outcomes correlate with edge
  * edge days run at LOW vix; range days at HIGH vix                (regime signal)
  * on up-edge days the CALL wall sits far above spot (room to run) (chain signal)
    on down-edge days the PUT wall sits far below spot
So a trade whose direction matches the edge, in low vix, with a far wall in its
direction, wins more often. The model must combine regime + chain + direction to see it.
This is a PIPELINE test fixture, not a market simulator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BARS_PER_DAY = 75          # 09:15..15:30 in 5-min bars
DRIFT = 0.0011             # per-bar drift on an edge day
VOL = 0.0026               # per-bar noise
CACHE_DIR = os.path.join("data", "pipeline_dummy")


@dataclass
class DummyData:
    symbols: list
    candles: dict                 # sym -> 5m OHLCV DataFrame (DatetimeIndex)
    index: pd.DataFrame           # index 5m OHLCV
    vix: dict                     # 'YYYY-MM-DD' -> vix level
    chain: dict                   # (sym, 'YYYY-MM-DD') -> {'spot':, 'strikes': DataFrame}
    meta: dict                    # (sym, 'YYYY-MM-DD') -> {edge, vix, ban, liq_tier, days_to_event, era}
    days: list = field(default_factory=list)


def _day_bars(start_ts):
    return pd.date_range(start_ts, periods=BARS_PER_DAY, freq="5min")


def _ohlc_from_close(close, rng):
    close = np.asarray(close, float)
    openp = np.empty_like(close)
    openp[0] = close[0]
    openp[1:] = close[:-1]
    wig = np.abs(rng.normal(0, VOL, len(close))) * close
    high = np.maximum(openp, close) + wig
    low = np.minimum(openp, close) - wig
    vol = rng.integers(800, 20000, len(close)).astype(float)
    return openp, high, low, close, vol


def _chain(spot, edge, rng):
    """Per-strike OI + IV. Walls placed to correlate with the day's edge."""
    step = round(spot * 0.01, 1) or 1.0
    atm = round(spot / step) * step
    strikes = np.array([atm + step * k for k in range(-8, 9)])
    # base OI hump around ATM
    base = 1e5 * np.exp(-((strikes - spot) / (4 * step)) ** 2)
    ce_oi = base * rng.uniform(0.7, 1.3, len(strikes))
    pe_oi = base * rng.uniform(0.7, 1.3, len(strikes))
    # place walls (OI spikes) per edge
    call_wall = spot * (1 + (0.035 if edge > 0 else 0.012))   # far above on up days
    put_wall = spot * (1 - (0.035 if edge < 0 else 0.012))    # far below on down days
    ce_oi[np.argmin(np.abs(strikes - call_wall))] *= 3.0
    pe_oi[np.argmin(np.abs(strikes - put_wall))] *= 3.0
    base_iv = (12.0 if edge != 0 else 18.0) / 100.0
    ce_iv = base_iv + 0.02 * (strikes - spot) / spot + rng.normal(0, 0.003, len(strikes))
    pe_iv = base_iv - 0.02 * (strikes - spot) / spot + rng.normal(0, 0.003, len(strikes))
    df = pd.DataFrame({"strike": strikes, "ce_oi": ce_oi, "pe_oi": pe_oi,
                       "ce_iv": np.clip(ce_iv, 0.05, 1.0),
                       "pe_iv": np.clip(pe_iv, 0.05, 1.0)})
    return {"spot": float(spot), "strikes": df}


def generate(n_stocks=6, n_days=120, seed=7, start="2024-01-01") -> DummyData:
    rng = np.random.default_rng(seed)
    symbols = [f"STK{i:02d}" for i in range(n_stocks)]
    liq = {s: ["A", "B", "C"][i % 3] for i, s in enumerate(symbols)}
    day_dates = pd.bdate_range(start, periods=n_days)
    days = [d.strftime("%Y-%m-%d") for d in day_dates]
    era_break = pd.Timestamp("2024-05-01")

    candles = {s: [] for s in symbols}
    index_frames = []
    vix = {}
    chain = {}
    meta = {}

    # index level path (shared market factor)
    idx_level = 20000.0
    for di, d in enumerate(day_dates):
        dstr = days[di]
        market_edge = rng.choice([-1, 0, 1], p=[0.3, 0.4, 0.3])
        # index bars
        start_ts = d + pd.Timedelta(hours=9, minutes=15)
        bars = _day_bars(start_ts)
        idx_ret = market_edge * DRIFT * 0.6 + rng.normal(0, VOL * 0.7, BARS_PER_DAY)
        idx_close = idx_level * np.cumprod(1 + idx_ret)
        idx_level = float(idx_close[-1])
        o, h, l, c, v = _ohlc_from_close(idx_close, rng)
        index_frames.append(pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                                          "volume": v}, index=bars))
        days_to_event = int((5 - (di % 6)))     # a fake earnings-ish cadence

        for s in symbols:
            # stock edge = market edge biased + own idiosyncratic
            edge = market_edge if rng.uniform() < 0.6 else rng.choice([-1, 0, 1])
            vlevel = (11.0 if edge != 0 else 18.0) + rng.normal(0, 1.5)
            vlevel = float(np.clip(vlevel, 8, 35))
            ret = edge * DRIFT + rng.normal(0, VOL, BARS_PER_DAY)
            last = candles[s][-1]["close"].iloc[-1] if candles[s] else float(rng.uniform(200, 3000))
            close = last * np.cumprod(1 + ret)
            o, h, l, c, v = _ohlc_from_close(close, rng)
            candles[s].append(pd.DataFrame({"open": o, "high": h, "low": l,
                                            "close": c, "volume": v}, index=bars))
            spot = float(c[0])
            chain[(s, dstr)] = _chain(spot, edge, rng)
            vix[dstr] = vlevel
            meta[(s, dstr)] = {"edge": int(edge), "vix": vlevel,
                               "ban": bool(rng.uniform() < 0.05),
                               "liq_tier": liq[s], "days_to_event": days_to_event,
                               "era": "post" if d >= era_break else "pre"}

    candles = {s: pd.concat(frames).sort_index() for s, frames in candles.items()}
    index = pd.concat(index_frames).sort_index()
    return DummyData(symbols=symbols, candles=candles, index=index, vix=vix,
                     chain=chain, meta=meta, days=days)


def persist(dd: DummyData, out_dir=CACHE_DIR):
    os.makedirs(out_dir, exist_ok=True)
    for s, df in dd.candles.items():
        df.to_parquet(os.path.join(out_dir, f"candles_{s}.parquet"))
    dd.index.to_parquet(os.path.join(out_dir, "index.parquet"))
    pd.Series(dd.vix).to_json(os.path.join(out_dir, "vix.json"))
    rows = []
    for (s, d), m in dd.meta.items():
        rows.append({"symbol": s, "date": d, **m})
    pd.DataFrame(rows).to_parquet(os.path.join(out_dir, "meta.parquet"))
    # chain: flatten to one parquet
    crows = []
    for (s, d), ch in dd.chain.items():
        for _, r in ch["strikes"].iterrows():
            crows.append({"symbol": s, "date": d, "spot": ch["spot"], **r.to_dict()})
    pd.DataFrame(crows).to_parquet(os.path.join(out_dir, "chain.parquet"))
    return out_dir

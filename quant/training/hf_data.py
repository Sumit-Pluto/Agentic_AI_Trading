"""quant/training/hf_data.py — pull & cache NSE minute data from HuggingFace.

Source: xxparthparekhxx/indian-stock-market-minute-data (minute split, 2022-2026,
schema symbol,timestamp[UTC],open,high,low,close,volume,oi). We filter to the few
symbols we want (predicate pushdown — the dataset is sorted by symbol, so only the
relevant row groups are read; no 10 GB download), convert UTC→IST-naive, and cache
per-symbol 1-minute and 5-minute parquet locally so the replay/backtester run
offline and fast thereafter.

    python -m quant.training.hf_data RELIANCE HDFCBANK ICICIBANK
        -> data/hf_cache/<SYM>_1m.parquet and <SYM>_5m.parquet

Note: `oi` is 0 for cash equities in this dataset, so OI-based agents stay on
defaults (nothing to learn) — as intended.
"""

from __future__ import annotations

import os
import sys
import time

import pandas as pd

REPO = "datasets/xxparthparekhxx/indian-stock-market-minute-data/minute"
CACHE_DIR = os.path.join("data", "hf_cache")

AGG = {"open": "first", "high": "max", "low": "min",
       "close": "last", "volume": "sum", "oi": "last"}


def _to_ist_naive(ts_series) -> pd.DatetimeIndex:
    """UTC (tz-aware) -> IST wall-clock, tz-naive (the agents' trading clock)."""
    idx = pd.to_datetime(ts_series, utc=True)
    return idx.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)


def extract(symbols, out_dir: str = CACHE_DIR, repo: str = REPO,
            start: str | None = None, end: str | None = None) -> dict:
    """Filter-read `symbols` from the HF minute dataset, cache 1m + 5m parquet.

    start/end (YYYY-MM-DD) optionally bound the date range. Returns a per-symbol
    summary {sym: {rows_1m, rows_5m, first, last}}."""
    import pyarrow.dataset as ds
    from huggingface_hub import HfFileSystem

    symbols = [s.upper() for s in symbols]
    fs = HfFileSystem()
    files = fs.glob(f"{repo}/*.parquet")
    if not files:
        raise SystemExit(f"no parquet shards found under {repo}")
    dset = ds.dataset(files, filesystem=fs, format="parquet")
    filt = ds.field("symbol").isin(symbols)
    scn = dset.scanner(
        filter=filt,
        columns=["symbol", "timestamp", "open", "high", "low", "close",
                 "volume", "oi"])
    t0 = time.time()
    df = scn.to_table().to_pandas()
    print(f"  read {len(df):,} rows for {symbols} in {time.time()-t0:.1f}s")

    df["ts"] = _to_ist_naive(df["timestamp"])
    df = df[df["ts"].notna()]
    if start:
        df = df[df["ts"] >= pd.Timestamp(start)]
    if end:
        df = df[df["ts"] < pd.Timestamp(end)]

    os.makedirs(out_dir, exist_ok=True)
    summary: dict = {}
    for sym, g in df.groupby("symbol"):
        g = (g.set_index("ts").sort_index()
             [["open", "high", "low", "close", "volume", "oi"]])
        g = g[~g.index.duplicated(keep="last")]
        if g.empty:
            continue
        g.to_parquet(os.path.join(out_dir, f"{sym}_1m.parquet"))
        g5 = g.resample("5min").agg(AGG).dropna(subset=["open", "close"])
        g5.to_parquet(os.path.join(out_dir, f"{sym}_5m.parquet"))
        summary[sym] = {"rows_1m": len(g), "rows_5m": len(g5),
                        "first": str(g.index.min()), "last": str(g.index.max())}
        print(f"  {sym}: {len(g):,} 1m -> {len(g5):,} 5m  "
              f"[{g.index.min().date()} .. {g.index.max().date()}]")
    return summary


def load_5m(symbol: str, cache_dir: str = CACHE_DIR) -> pd.DataFrame | None:
    p = os.path.join(cache_dir, f"{symbol.upper()}_5m.parquet")
    return pd.read_parquet(p) if os.path.exists(p) else None


def load_1m(symbol: str, cache_dir: str = CACHE_DIR) -> pd.DataFrame | None:
    p = os.path.join(cache_dir, f"{symbol.upper()}_1m.parquet")
    return pd.read_parquet(p) if os.path.exists(p) else None


def cached_symbols(cache_dir: str = CACHE_DIR) -> list[str]:
    if not os.path.isdir(cache_dir):
        return []
    return sorted(f[:-len("_5m.parquet")] for f in os.listdir(cache_dir)
                  if f.endswith("_5m.parquet"))


if __name__ == "__main__":
    syms = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not syms:
        print("usage: python -m quant.training.hf_data SYM1 SYM2 ...")
        raise SystemExit(1)
    out = extract(syms)
    print("\ndone:", out)

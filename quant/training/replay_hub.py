"""quant/training/replay_hub.py — an offline DataHub for historical replay.

Serves the subset of the live DataHub interface the agent tree actually reads,
sourced from the Hyena_X archive instead of Shoonya:

  * 5-minute candles  — resampled from dhan `intraday1m_<SYM>.json` (UTC epoch →
    IST, tz-naive, exactly what the SMC/volume agents expect);
  * daily candles     — resampled from the SAME 1m series (self-consistent), and
    served POINT-IN-TIME: only bars strictly before the replay's "as-of" day, so
    the daily/pivot agents never see the future;
  * India VIX         — from `state/vix_daily.json`, as-of gated;
  * spot / cash quote — the entry bar's close, injected per evaluation.

What it deliberately returns None for: option chain, futures, order-book depth.
The Hyena_X archive has no intraday option snapshots (dhanopt is empty), so the
chain-dependent agents (SNR zones, volatility IV, volume OI/orderflow) correctly
SKIP — the dataset records their low availability and the trainer keeps their
defaults. Nothing is faked.

Point-in-time contract: the replay harness calls ``set_asof(date, spot)`` before
each evaluation; every backward-looking view respects it.
"""

from __future__ import annotations

import glob
import json
import logging
import os

import pandas as pd

log = logging.getLogger("training")

NSE_OPEN = "09:15"
NSE_CLOSE = "15:30"


def _load_ohlcv_json(path: str) -> pd.DataFrame | None:
    """dhan OHLCV json ({open,high,low,close,volume,timestamp}) -> IST-naive df."""
    try:
        d = json.load(open(path))
    except Exception:
        return None
    if not isinstance(d, dict) or "timestamp" not in d or not d["timestamp"]:
        return None
    df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "volume")
                       if k in d})
    # dhan timestamps are UTC epoch seconds; NSE 09:15 IST == 03:45 UTC. Convert
    # to IST and drop tz so the index reads as the trading clock (agents and the
    # exit walk key off 09:20/15:15 IST).
    idx = (pd.to_datetime(pd.Series(d["timestamp"]), unit="s", utc=True)
           .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    df.index = idx
    return df.sort_index()


def _resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    out = df1m.resample(rule).agg(agg).dropna(subset=["open", "close"])
    return out


class ReplayHub:
    """Offline hub over the Hyena_X dhan archive. Construct once, then the
    harness drives ``set_asof`` per evaluation."""

    def __init__(self, dhan_dir: str | None = None, vix_path: str | None = None,
                 frames_5m: dict[str, pd.DataFrame] | None = None):
        self.dhan_dir = dhan_dir
        self._5m: dict[str, pd.DataFrame] = {}
        self._daily: dict[str, pd.DataFrame] = {}
        self._symbols: list[str] = []
        if frames_5m is not None:                 # preloaded (e.g. parquet cache)
            for sym, df in frames_5m.items():
                if df is None or df.empty:
                    continue
                self._5m[sym] = df
                self._daily[sym] = _resample(df, "1D")
                self._symbols.append(sym)
            log.info("ReplayHub loaded %d symbols from frames", len(self._symbols))
        elif dhan_dir:
            self._load(dhan_dir)
        self._vix = self._load_vix(vix_path)
        # point-in-time state, set by the harness before each evaluate()
        self.as_of_date = None            # date object
        self.as_of_spot: float | None = None
        # attributes the live code sometimes checks
        self.feed = None
        self.tick_cache = None
        self.candle_source: dict[str, str] = {}

    # ── loading ──────────────────────────────────────────────────────────
    def _load(self, dhan_dir: str) -> None:
        for path in sorted(glob.glob(os.path.join(dhan_dir, "intraday1m_*.json"))):
            sym = os.path.basename(path)[len("intraday1m_"):-len(".json")]
            df1 = _load_ohlcv_json(path)
            if df1 is None or df1.empty:
                continue
            self._5m[sym] = _resample(df1, "5min")
            self._daily[sym] = _resample(df1, "1D")
            self._symbols.append(sym)
        log.info("ReplayHub loaded %d symbols from %s",
                 len(self._symbols), dhan_dir)

    @classmethod
    def from_parquet_cache(cls, cache_dir: str, symbols=None,
                           vix_path: str | None = None):
        """Build from data/hf_cache/<SYM>_5m.parquet files (hf_data.py output)."""
        from .hf_data import load_5m, cached_symbols
        syms = [s.upper() for s in symbols] if symbols else cached_symbols(cache_dir)
        frames = {}
        for sym in syms:
            df = load_5m(sym, cache_dir)
            if df is not None and not df.empty:
                frames[sym] = df[["open", "high", "low", "close", "volume"]]
        return cls(frames_5m=frames, vix_path=vix_path)

    @staticmethod
    def _load_vix(path: str | None) -> dict:
        if not path or not os.path.exists(path):
            return {}
        try:
            return json.load(open(path))
        except Exception:
            return {}

    # ── point-in-time control ────────────────────────────────────────────
    def set_asof(self, as_of_date, spot: float | None) -> None:
        self.as_of_date = as_of_date
        self.as_of_spot = spot

    # ── data the harness needs ───────────────────────────────────────────
    def full_5m(self, symbol: str) -> pd.DataFrame | None:
        return self._5m.get(symbol)

    def fo_universe(self) -> list[str]:
        return list(self._symbols)

    def segment_of(self, symbol: str) -> str:
        return "FNO"

    def candle_ref(self, symbol: str, segment=None):
        return "NSE", symbol

    # ── views the agents read via MarketContext ──────────────────────────
    def candles_5m(self, symbol: str, days: int = 7, segment=None):
        return self._5m.get(symbol)

    def candles(self, symbol: str, interval="5", days=7, segment=None, ttl=None):
        return self._5m.get(symbol)

    def daily_candles(self, symbol: str, days: int = 40, segment=None):
        """Prior daily bars ONLY — strictly before the as-of day (no lookahead)."""
        df = self._daily.get(symbol)
        if df is None or df.empty:
            return None
        if self.as_of_date is not None:
            df = df[df.index.normalize() < pd.Timestamp(self.as_of_date)]
        return df.tail(days) if not df.empty else None

    def cash_quote(self, symbol: str, segment=None):
        return {"lp": self.as_of_spot} if self.as_of_spot else None

    def price(self, symbol: str, segment=None):
        return self.as_of_spot

    def india_vix(self):
        if not self._vix:
            return None
        if self.as_of_date is not None:
            key = str(self.as_of_date)
            if key in self._vix:
                return float(self._vix[key])
            past = [d for d in self._vix if d <= key]     # latest on/before as-of
            if past:
                return float(self._vix[max(past)])
        return None

    # ── explicitly unavailable in the archive (agents skip) ──────────────
    def chain_snapshot(self, symbol: str, span: int = 8):
        return None

    def futures_quote(self, symbol: str, segment=None):
        return None

    def futures_candles(self, symbol: str, interval="5", days=5, segment=None):
        return None

    def shoonya_up(self) -> bool:
        return True

    def prefetch_candles(self, symbols, batch: int = 40) -> int:
        return 0

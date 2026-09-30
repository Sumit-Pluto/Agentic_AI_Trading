"""Tick → OHLCV bar aggregator (plan Q1: engine builds bars locally from the
Gateway's touchline WS; seed the early bars from /api/candles so indicators are
warm at the open).

Pure and offline-testable: feed it ticks, read a pandas bar frame. The live path
wires GatewayWS.on_tick → BarBuilder.on_tick; tests feed synthetic ticks.
"""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd

_TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


class BarBuilder:
    def __init__(self, timeframe: str = "5m", keep: int = 240):
        self.tf = _TF_SECONDS.get(timeframe, 300)
        self.keep = keep
        self._bars: dict[str, list[dict]] = defaultdict(list)   # symbol -> closed bars
        self._cur: dict[str, dict] = {}                          # symbol -> forming bar
        self._last_cum_vol: dict[str, float] = {}
        self._last_ltp: dict[str, float] = {}

    def _bucket(self, ts: float) -> int:
        return int(ts // self.tf) * self.tf

    def seed(self, symbol: str, candles: list[dict]) -> None:
        """Warm from /api/candles output (oldest-first {time,open,high,low,close,volume})."""
        out = []
        for c in candles or []:
            try:
                out.append({"bucket": int(float(c.get("time") or 0)),
                            "open": float(c["open"]), "high": float(c["high"]),
                            "low": float(c["low"]), "close": float(c["close"]),
                            "volume": float(c.get("volume") or 0.0)})
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            self._bars[symbol] = out[-self.keep:]
            self._last_ltp[symbol] = out[-1]["close"]

    def on_tick(self, symbol: str, ltp: float, cum_vol: float | None, ts: float) -> None:
        """One touchline tick. cum_vol is the day's cumulative volume (Shoonya `v`);
        per-bar volume is its delta. ts is epoch seconds (feed time or arrival)."""
        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return
        if not math.isfinite(ltp) or ltp <= 0:
            return
        self._last_ltp[symbol] = ltp
        b = self._bucket(ts)
        # per-bar volume from cumulative delta
        dv = 0.0
        if cum_vol is not None:
            try:
                cv = float(cum_vol)
                prev = self._last_cum_vol.get(symbol)
                dv = max(0.0, cv - prev) if prev is not None else 0.0
                self._last_cum_vol[symbol] = cv
            except (TypeError, ValueError):
                dv = 0.0
        cur = self._cur.get(symbol)
        if cur is None or cur["bucket"] != b:
            if cur is not None:
                self._bars[symbol].append(cur)
                self._bars[symbol] = self._bars[symbol][-self.keep:]
            cur = {"bucket": b, "open": ltp, "high": ltp, "low": ltp,
                   "close": ltp, "volume": dv}
            self._cur[symbol] = cur
        else:
            cur["high"] = max(cur["high"], ltp)
            cur["low"] = min(cur["low"], ltp)
            cur["close"] = ltp
            cur["volume"] += dv

    def spot(self, symbol: str) -> float:
        return self._last_ltp.get(symbol, 0.0)

    def bars(self, symbol: str, include_forming: bool = True) -> pd.DataFrame | None:
        """Closed bars (+ the forming one) as a DataFrame with prev_close + vwap."""
        rows = list(self._bars.get(symbol, []))
        if include_forming and symbol in self._cur:
            rows = rows + [self._cur[symbol]]
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["prev_close"] = df["close"].shift(1).fillna(df["open"])
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_v = df["volume"].cumsum().replace(0, float("nan"))
        df["vwap"] = (tp * df["volume"]).cumsum() / cum_v
        df["vwap"] = df["vwap"].fillna(df["close"])
        df["time"] = df["bucket"]
        return df

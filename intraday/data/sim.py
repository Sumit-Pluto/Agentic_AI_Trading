"""SimContext — a simulated intraday market for demos and offline runs.

Implements the same context surface the scanner/loop use (symbols, bars, chain,
spot, positioning, vix, prev_day, now, is_square_off), so the ENTIRE engine runs
against it unchanged — the single-code-path payoff: swap SimContext for
LiveContext and the same agents/risk/rules/exits trade a real feed.

The price path is a regime-switching random walk (trends flip every ~15–25
bars) so momentum/setup/regime agents get real signal; option OI is a
distance-from-ATM profile with a drifting put/call skew so the flow/vol agents
contribute. It advances a simulated 5-min clock and rolls to the next session
after the close, so a demo runs indefinitely.
"""
from __future__ import annotations

import datetime as dt
import math
import random

import pandas as pd

from ..options.chain_builder import fill_missing_ivs, from_gateway_payload
from ..options.models import IST, MARKET_CLOSE, MARKET_OPEN


def _next_weekday_open(d: dt.date) -> dt.datetime:
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(9, 20), IST)


class _Sym:
    def __init__(self, spot: float, step: float, lot: int, rng: random.Random):
        self.step_strike = step
        self.lot = lot
        self.rng = rng
        self.bars: list[dict] = []
        self.drift = 0.0
        self.drift_left = 0
        self.skew = 0.0            # drifting put/call OI skew
        # warm up ~40 bars so indicators are ready at the open
        px = spot
        for _ in range(40):
            px = self._tick_price(px)
            self._append(px)

    def _tick_price(self, px: float) -> float:
        if self.drift_left <= 0:
            self.drift = self.rng.choice([1, -1, 0, 1, -1]) * self.rng.uniform(0.0006, 0.0016)
            self.drift_left = self.rng.randint(12, 26)
        self.drift_left -= 1
        noise = self.rng.gauss(0, 0.0009)
        return max(px * (1.0 + self.drift + noise), 1.0)

    def _append(self, close: float):
        o = self.bars[-1]["close"] if self.bars else close
        hi = max(o, close) * (1 + abs(self.rng.gauss(0, 0.0004)))
        lo = min(o, close) * (1 - abs(self.rng.gauss(0, 0.0004)))
        vol = self.rng.uniform(8000, 20000)
        self.bars.append({"open": o, "high": hi, "low": lo, "close": close, "volume": vol})
        self.bars = self.bars[-240:]

    def advance(self):
        self._append(self._tick_price(self.bars[-1]["close"]))
        self.skew = max(-0.4, min(0.4, self.skew + self.rng.gauss(0, 0.05)))

    @property
    def spot(self) -> float:
        return self.bars[-1]["close"]

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.bars)
        df["prev_close"] = df["close"].shift(1).fillna(df["open"])
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cv = df["volume"].cumsum().replace(0, float("nan"))
        df["vwap"] = ((tp * df["volume"]).cumsum() / cv).fillna(df["close"])
        df["time"] = range(len(df))
        return df


class SimContext:
    index_symbol = "NIFTY"

    def __init__(self, cfg: dict, seed: int = 7):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self._now = _next_weekday_open(dt.date(2026, 9, 28))
        self._tf = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(cfg.get("bar_timeframe", "5m"), 5)
        self._vix = 12.5
        specs = {"NIFTY": (24800.0, 100.0, 50), "BANKNIFTY": (52000.0, 100.0, 15)}
        uni = cfg.get("universe", ["NIFTY", "BANKNIFTY"])
        self._syms = {s: _Sym(*specs.get(s, (20000.0, 100.0, 25)), rng=self.rng)
                      for s in uni}
        self._prev_day = {s: self._prev_levels(o.bars) for s, o in self._syms.items()}
        self._chains: dict[str, object] = {}
        self._rebuild_chains()

    # ---- clock / lifecycle ----
    def now(self) -> dt.datetime:
        return self._now

    def is_square_off(self, now: dt.datetime | None = None) -> bool:
        now = now or self._now
        hh = self.cfg.get("square_off_time", "15:15")
        h, m = (int(x) for x in str(hh).split(":"))
        return now.timetz() >= dt.time(h, m, tzinfo=IST)

    def step(self):
        """Advance one bar; roll to the next session after the close."""
        self._now += dt.timedelta(minutes=self._tf)
        if self._now.timetz() >= dt.time(15, 30, tzinfo=IST):
            # new session: reset clock + prev-day levels
            self._prev_day = {s: self._prev_levels(o.bars) for s, o in self._syms.items()}
            self._now = _next_weekday_open(self._now.date() + dt.timedelta(days=1))
        for o in self._syms.values():
            o.advance()
        self._vix = max(9.0, min(30.0, self._vix + self.rng.gauss(0, 0.25)))
        self._rebuild_chains()

    # ---- context surface ----
    def symbols(self) -> list[str]:
        return list(self._syms.keys())

    def bars(self, symbol: str):
        o = self._syms.get(symbol)
        return o.frame() if o else None

    def spot(self, symbol: str) -> float:
        o = self._syms.get(symbol)
        return o.spot if o else 0.0

    def chain(self, symbol: str):
        return self._chains.get(symbol)

    def positioning(self, symbol: str) -> dict:
        return {"d_pcr": self._syms[symbol].skew * 0.3 if symbol in self._syms else 0.0}

    def vix(self) -> float:
        return round(self._vix, 2)

    def prev_day(self, symbol: str) -> dict:
        return self._prev_day.get(symbol, {})

    # ---- helpers ----
    @staticmethod
    def _prev_levels(bars: list[dict]) -> dict:
        hs = [b["high"] for b in bars[-75:]] or [0]
        ls = [b["low"] for b in bars[-75:]] or [0]
        return {"pdh": max(hs), "pdl": min(ls), "pdc": bars[-1]["close"],
                "pdo": bars[-75]["open"] if len(bars) >= 75 else bars[0]["open"]}

    def _rebuild_chains(self):
        dte = 3 + (self._now.weekday() % 5)         # a few days to weekly expiry
        expiry = (self._now.date() + dt.timedelta(days=dte)).isoformat()
        iv = self._vix / 100.0
        for sym, o in self._syms.items():
            spot = o.spot
            step = o.step_strike
            atm = round(spot / step) * step
            rows = []
            for i in range(-8, 9):
                k = atm + i * step
                # OI bell around ATM, tilted by the drifting skew
                base = 120000 * math.exp(-((i / 5.0) ** 2))
                ce_oi = int(base * (1 - o.skew) + 5000)
                pe_oi = int(base * (1 + o.skew) + 5000)
                t_yr = max(dte, 0.5) / 365.0
                from ..options.mathx import bs_price
                leg = {}
                for right, is_call, oi in (("CE", True, ce_oi), ("PE", False, pe_oi)):
                    mid = max(bs_price(is_call, spot, k, t_yr, iv, 0.06), 0.05)
                    leg[right] = {"quoted": True, "strike": k, "ltp": round(mid, 2),
                                  "bid": round(mid * 0.995, 2), "ask": round(mid * 1.005, 2),
                                  "oi_num": oi, "prev_oi": int(oi * (1 - o.skew * 0.1)),
                                  "volume": int(oi * self.rng.uniform(0.2, 0.6)),
                                  "prev_close": round(mid, 2), "lot_size": o.lot,
                                  "token": f"{sym}{int(k)}{right}", "tsym": f"{sym}{int(k)}{right}"}
                rows.append({"strike": k, **leg})
            payload = {"symbol": sym, "exchange": "NFO", "spot": spot,
                       "expiry_iso": expiry, "lot_size": o.lot,
                       "underlying_exchange": "NSE", "underlying_token": sym,
                       "chain": rows, "quality": {"quote_span_s": 0.0}}
            # Anchor the chain to SIM time BEFORE IV inversion: Chain.t_years
            # defaults `asof` to the real clock, which marches past the
            # simulated window and then reads every sim chain as expired
            # (t=0 → no IVs, no greeks, and the V/F agents plus sizing
            # silently degrade to fallbacks).
            ch = from_gateway_payload(payload, sym)
            ch.asof = self._now
            self._chains[sym] = fill_missing_ivs(ch)

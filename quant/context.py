"""MarketContext — everything an agent may look at, lazily fetched, cached.

Agents never call Shoonya directly; they read ctx.* properties. Any property
may be None (chain illiquid, VIX token missing, API hiccup) and agents must
return (None, reason) in that case so the tree skips them — the user rule:
"if Shoonya doesn't have the data, skip that agent".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class OptionLeg:
    tsym: str
    token: str
    ltp: float | None = None
    oi: float | None = None
    volume: float | None = None
    iv: float | None = None          # filled by ChainSnapshot.compute_ivs()
    bid: float | None = None         # best bid (bp1) — executable SELL price
    ask: float | None = None         # best ask (sp1) — executable BUY price


@dataclass
class StrikeRow:
    strike: float
    ce: OptionLeg | None = None
    pe: OptionLeg | None = None


@dataclass
class ChainSnapshot:
    symbol: str
    expiry_epoch: float
    lot: int
    spot: float
    strikes: list[StrikeRow] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)

    def atm_strike(self) -> float | None:
        if not self.strikes:
            return None
        return min((s.strike for s in self.strikes),
                   key=lambda k: abs(k - self.spot))

    def compute_ivs(self):
        """Fill leg.iv via BS inversion. Illiquid/stale premiums -> iv=None."""
        from qcore import implied_vol, years_to_expiry   # C++ kernel; falls
        #                                back to quant.mathutils if not built
        t = years_to_expiry(self.expiry_epoch, time.time())
        for row in self.strikes:
            if row.ce and row.ce.iv is None and row.ce.ltp:
                row.ce.iv = implied_vol(True, row.ce.ltp, self.spot,
                                        row.strike, t)
            if row.pe and row.pe.iv is None and row.pe.ltp:
                row.pe.iv = implied_vol(False, row.pe.ltp, self.spot,
                                        row.strike, t)
        return self


class MarketContext:
    """Per-signal evaluation context. Built by the scanner, consumed by agents."""

    def __init__(self, hub, symbol: str, direction: str,
                 df5: pd.DataFrame, spot: float):
        self.hub = hub                  # DataHub (or a stub in tests)
        self.symbol = symbol
        self.direction = direction      # BUY / SELL
        self.df = df5                   # closed 5m candles, oldest first
        self.spot = spot
        self.now = time.time()
        self.meta: dict = {}            # cross-agent scratch space
        self._cache: dict = {}

    def _get(self, key: str, fetch):
        if key not in self._cache:
            try:
                self._cache[key] = fetch()
            except Exception:
                self._cache[key] = None
        return self._cache[key]

    # ── lazily-fetched views ────────────────────────────────────────────
    @property
    def chain(self) -> ChainSnapshot | None:
        return self._get("chain", lambda: self.hub.chain_snapshot(self.symbol))

    @property
    def futures(self) -> dict | None:
        """Nearest-expiry stock future quote: ltp, oi, poi, volume, pc ..."""
        return self._get("fut", lambda: self.hub.futures_quote(self.symbol))

    @property
    def vix(self) -> float | None:
        return self._get("vix", lambda: self.hub.india_vix())

    @property
    def daily(self) -> pd.DataFrame | None:
        """~30 daily candles for pivots / prior-day levels."""
        return self._get("daily", lambda: self.hub.daily_candles(self.symbol))

    @property
    def cash_quote(self) -> dict | None:
        """Fresh cash-series quote: depth, tbq/tsq for orderflow agents."""
        return self._get("cq", lambda: self.hub.cash_quote(self.symbol))

    @property
    def atr14(self) -> float | None:
        def calc():
            from signals.indicators import atr
            a = atr(self.df, 14)
            v = float(a.iloc[-1])
            return v if v == v and v > 0 else None   # NaN guard
        return self._get("atr", calc)

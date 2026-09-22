"""Offline integration test: full tree evaluation with a stubbed DataHub.

No Shoonya login needed — proves the whole pipeline (indicators → signal →
context → every agent → weighted composite) runs and degrades gracefully
when derivatives data is missing.

Run:  python tests/test_offline.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from quant.config import QuantConfig
from quant.context import ChainSnapshot, MarketContext, OptionLeg, StrikeRow
from quant.registry import build_root


def make_candles(n=500, seed=3) -> pd.DataFrame:
    """Synthetic 5m candles across multiple sessions (09:15–15:25 IST)."""
    rng = np.random.default_rng(seed)
    times, day = [], pd.Timestamp("2026-06-22 09:15")
    while len(times) < n:
        t = day
        while t.time() <= pd.Timestamp("2026-06-22 15:25").time() and len(times) < n:
            times.append(t)
            t += pd.Timedelta(minutes=5)
        day += pd.Timedelta(days=1)
        while day.weekday() >= 5:
            day += pd.Timedelta(days=1)
        day = day.replace(hour=9, minute=15)
    drift = np.concatenate([np.linspace(0, -30, n // 3),
                            np.linspace(-30, 45, n // 3),
                            np.linspace(45, 30, n - 2 * (n // 3))])
    close = 1000 + drift + rng.normal(0, 2.0, n).cumsum() * 0.3
    high = close + np.abs(rng.normal(1.5, 0.8, n))
    low = close - np.abs(rng.normal(1.5, 0.8, n))
    open_ = np.roll(close, 1) + rng.normal(0, 0.5, n)
    open_[0] = close[0]
    vol = rng.integers(5_000, 80_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": np.maximum.reduce([open_, close, high]),
                         "low": np.minimum.reduce([open_, close, low]),
                         "close": close, "volume": vol, "oi": vol * 0},
                        index=pd.DatetimeIndex(times[:n]))


def make_chain(spot: float) -> ChainSnapshot:
    step = 20.0
    atm = round(spot / step) * step
    snap = ChainSnapshot(symbol="TESTSTK", lot=500, spot=spot,
                         expiry_epoch=time.time() + 12 * 86400)
    rng = np.random.default_rng(11)
    for i in range(-6, 7):
        k = atm + i * step
        m = max(0.0, spot - k)
        mp = max(0.0, k - spot)
        ce = OptionLeg(tsym=f"TESTC{int(k)}", token=str(1000 + i),
                       ltp=round(m + 12 * np.exp(-abs(i) / 3) + 1, 2),
                       oi=float(rng.integers(10_000, 400_000)),
                       volume=float(rng.integers(1_000, 60_000)))
        pe = OptionLeg(tsym=f"TESTP{int(k)}", token=str(2000 + i),
                       ltp=round(mp + 12 * np.exp(-abs(i) / 3) + 1, 2),
                       oi=float(rng.integers(10_000, 400_000)),
                       volume=float(rng.integers(1_000, 60_000)))
        snap.strikes.append(StrikeRow(strike=k, ce=ce, pe=pe))
    return snap


class StubHub:
    """DataHub stand-in. rich=True serves chain/futures/vix; rich=False
    simulates 'Shoonya has no data' so every derivatives agent must skip."""

    def __init__(self, df, rich=True):
        self.df, self.rich = df, rich
        self.spot = float(df["close"].iloc[-1])

    def candles_5m(self, s):
        return self.df

    def daily_candles(self, s):
        if not self.rich:
            return None
        d = self.df.resample("1D").agg({"open": "first", "high": "max",
                                        "low": "min", "close": "last",
                                        "volume": "sum"}).dropna()
        return d

    def cash_quote(self, s):
        if not self.rich:
            return None
        return {"lp": str(self.spot), "tbq": "1200000", "tsq": "800000",
                "bq1": "5000", "sq1": "4000", "bp1": str(self.spot - 0.1),
                "sp1": str(self.spot + 0.1), "v": "2400000"}

    def india_vix(self):
        return 14.2 if self.rich else None

    def futures_quote(self, s):
        if not self.rich:
            return None
        return {"lp": str(self.spot * 1.002), "oi": "5600000",
                "poi": "5100000", "v": "900000", "pc": "1.4",
                "lot": 500, "expiry_epoch": time.time() + 12 * 86400}

    def chain_snapshot(self, s):
        return make_chain(self.spot) if self.rich else None


def run(direction: str, rich: bool):
    df = make_candles()
    hub = StubHub(df, rich=rich)
    ctx = MarketContext(hub, "TESTSTK", direction, df,
                        float(df["close"].iloc[-1]))
    cfg = QuantConfig(path="/tmp/quant_config_test.json")
    root = build_root()
    tree = root.evaluate(ctx, cfg)

    def show(node, depth=0):
        mark = ("--" if node.score is None else
                f"{node.score:5.1f} {'PASS' if node.passed else 'fail'}")
        state = "" if node.enabled else " [off]"
        print(f"  {'  ' * depth}{node.key:<22} {mark}{state}  | {node.detail[:60]}")
        for c in node.children:
            show(c, depth + 1)

    print(f"\n═══ direction={direction} rich_data={rich} "
          f"composite={tree.score} ═══")
    show(tree)
    assert tree is not None
    leaf_count = sum(1 for _ in root.walk()) - 1
    print(f"  nodes evaluated: {leaf_count}")
    if rich:
        assert tree.score is not None, "rich data must produce a composite"
    return tree


if __name__ == "__main__":
    for rich in (True, False):
        for direction in ("BUY", "SELL"):
            run(direction, rich)
    print("\nOFFLINE INTEGRATION TEST PASSED")

"""quant/pipeline/strategies.py — LAYER A pluggable trigger sources.

Each plug turns candles into TriggerEvents (the shared contract). Adding a strategy
is a registry entry; the rest of the pipeline never changes. Three plugs here:
  * grid           — the unbiased firehose (every Nth bar, both directions) — the
                     bulk of the training set (analogue of agent_primary)
  * orb            — opening-range breakout
  * pullback       — trend pullback + reclaim
strategy_id / setup_id ride into the feature vector so the meta-model learns
per-strategy reliability.
"""

from __future__ import annotations

import pandas as pd

from .contracts import TriggerEvent

ENTRY_START_BAR = 6        # skip the first 30 min
MIN_FWD = 6                # need this many bars ahead to label


def _entry_window(df):
    n = len(df)
    return range(ENTRY_START_BAR, n - MIN_FWD)


def grid(dd, bank, stride=5):
    ev = []
    for sym, df in dd.candles.items():
        byday = {d: g for d, g in df.groupby(df.index.normalize())}
        for _, g in byday.items():
            n = len(g)
            for pos in range(ENTRY_START_BAR, n - MIN_FWD, stride):
                ts = g.index[pos]
                price = float(g["close"].iloc[pos])
                for direction in ("BUY", "SELL"):
                    ev.append(TriggerEvent("grid", "grid", sym, direction,
                                           ts.isoformat(), price))
    return ev


def orb(dd, bank):
    ev = []
    for sym, df in dd.candles.items():
        for _, g in df.groupby(df.index.normalize()):
            if len(g) < ENTRY_START_BAR + MIN_FWD + 1:
                continue
            orh = g["high"].iloc[:ENTRY_START_BAR].max()
            orl = g["low"].iloc[:ENTRY_START_BAR].min()
            for pos in range(ENTRY_START_BAR, len(g) - MIN_FWD):
                c = float(g["close"].iloc[pos])
                ts = g.index[pos]
                if c > orh:
                    ev.append(TriggerEvent("orb", "orb_up", sym, "BUY", ts.isoformat(), c))
                    break
                if c < orl:
                    ev.append(TriggerEvent("orb", "orb_dn", sym, "SELL", ts.isoformat(), c))
                    break
    return ev


def pullback(dd, bank):
    ev = []
    for sym, df in dd.candles.items():
        f = bank.cols[sym]
        for _, g in df.groupby(df.index.normalize()):
            for pos in range(ENTRY_START_BAR, len(g) - MIN_FWD):
                ts = g.index[pos]
                r = f.loc[ts] if ts in f.index else None
                if r is None:
                    continue
                c = float(g["close"].iloc[pos])
                if r["trend"] > 0 and r["ret_3"] < 0 and r["ret_1"] > 0:
                    ev.append(TriggerEvent("pullback", "pb_long", sym, "BUY", ts.isoformat(), c))
                elif r["trend"] < 0 and r["ret_3"] > 0 and r["ret_1"] < 0:
                    ev.append(TriggerEvent("pullback", "pb_short", sym, "SELL", ts.isoformat(), c))
    return ev


REGISTRY = {"grid": grid, "orb": orb, "pullback": pullback}


def all_triggers(dd, bank, plugs=("grid", "orb", "pullback"), **kw):
    out = []
    for p in plugs:
        fn = REGISTRY[p]
        out.extend(fn(dd, bank, **kw) if p == "grid" else fn(dd, bank))
    return out

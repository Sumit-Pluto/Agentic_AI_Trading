"""quant/pipeline/labeler.py — LAYER C triple-barrier labeler (train time).

Grades each TriggerEvent by its own barrier geometry (stop / target / horizon-or-EOD),
net of a cost floor. label=1 if realized return clears cost. Walk stays within the
entry day (intraday system). Returns (label, pnl_pct, exit_reason).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def label_event(event, dd, bank, cost_pct=0.05):
    sym = event.symbol
    df = dd.candles[sym]
    ts = pd.Timestamp(event.bar_time)
    day = df[df.index.normalize() == ts.normalize()]
    if ts not in day.index:
        return None
    pos = day.index.get_loc(ts)
    if not isinstance(pos, (int, np.integer)):
        return None
    fwd = len(day) - 1 - pos
    if fwd < 1:
        return None
    entry = float(event.entry_price)
    atr_abs = float(bank.at(sym, ts).get("atr_abs", entry * 0.01)) or entry * 0.01
    sign = 1.0 if event.direction == "BUY" else -1.0
    target = entry + sign * event.target_atr * atr_abs
    stop = entry - sign * event.stop_atr * atr_abs
    last = min(pos + event.horizon_bars, len(day) - 1)
    reason, exit_px = "eod", float(day["close"].iloc[last])
    for j in range(pos + 1, last + 1):
        hi = float(day["high"].iloc[j])
        lo = float(day["low"].iloc[j])
        # conservative: check stop before target on the same bar
        if event.direction == "BUY":
            if lo <= stop:
                reason, exit_px = "stop", stop; break
            if hi >= target:
                reason, exit_px = "target", target; break
        else:
            if hi >= stop:
                reason, exit_px = "stop", stop; break
            if lo <= target:
                reason, exit_px = "target", target; break
    pnl_pct = sign * (exit_px - entry) / entry * 100.0
    label = 1 if pnl_pct > cost_pct else 0
    return label, round(pnl_pct, 4), reason

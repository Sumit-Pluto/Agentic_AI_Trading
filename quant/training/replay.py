"""quant/training/replay.py — backtest the agent tree over historical candles.

This is the BACK-DATA path (run it before you have weeks of forward collection):
replay the SAME `scanner.evaluate` scoring + veto logic the live app uses, bar by
bar over the Hyena_X 1-minute history (resampled to 5m), and grade every scored
(symbol, direction) with the engine's own exit walk. Output is training samples
in the identical schema `dataset.py`/`trainer.py` consume — so a backtest and
forward collection feed the exact same model.

Point-in-time honesty:
  * features come from the candle PREFIX up to and including the entry bar
    (the agent sees no future bar), and daily/VIX views are as-of gated;
  * the label comes from `engine.exits.simulate_position` walking the REMAINING
    bars of the entry day (real stop / target-1 / trail / EOD) — the triple
    barrier, same as live;
  * chain/futures are absent in the archive, so those agents skip (recorded as
    low availability, not faked).

Sampling: a grid over the entry window (every `stride` 5m bars) in BOTH
directions — the unbiased analogue of the agent-primary sweep that scores every
symbol both ways. Not the indicator trigger; we're training the tree itself.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime

import pandas as pd

from .collector import flatten_families, flatten_leaves, DEFAULT_PATH

log = logging.getLogger("training")

ENTRY_START = dtime(9, 20)
ENTRY_LAST = dtime(15, 15)


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def run_replay(dhan_dir: str | None = None, vix_path: str | None = None,
               out_path: str = DEFAULT_PATH, *, hub=None, symbols=None,
               stride: int = 5, prefix_bars: int = 250, min_forward_bars: int = 6,
               min_prefix_bars: int = 30, cost: float = 0.0,
               entry_start: str = "09:20", entry_last: str = "15:15",
               both_dirs: bool = True, limit: int = 0,
               start: str | None = None, end: str | None = None,
               fresh: bool = False, progress=print) -> dict:
    """Replay and write labeled samples. Returns a summary dict.

    Provide either dhan_dir (Hyena JSON) or a prebuilt `hub` (e.g.
    ReplayHub.from_parquet_cache). start/end (YYYY-MM-DD) bound the bars used —
    e.g. train only on 2022-2024, leaving 2025+ for an out-of-sample backtest."""
    # heavy imports here so `import quant.training` stays light
    from engine.scanner import Scanner
    from engine.exits import simulate_position
    from quant.registry import build_root
    from quant.config import QuantConfig
    from .replay_hub import ReplayHub

    if hub is None:
        hub = ReplayHub(dhan_dir, vix_path)
    all_syms = hub.fo_universe()
    if symbols:
        want = {s.upper() for s in symbols}
        all_syms = [s for s in all_syms if s.upper() in want]
    if limit:
        all_syms = all_syms[:limit]
    if not all_syms:
        return {"error": f"no matching symbols in {dhan_dir}"}

    scanner = Scanner(hub, build_root(), QuantConfig(), poll_seconds=0)
    e_start, e_last = _parse_hhmm(entry_start), _parse_hhmm(entry_last)
    directions = ("BUY", "SELL") if both_dirs else ("BUY",)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mode = "w" if fresh else "a"
    written = wins = losses = skipped = 0

    with open(out_path, mode) as out:
        for si, sym in enumerate(all_syms):
            df = hub.full_5m(sym)
            if df is None or df.empty:
                continue
            if start:
                df = df[df.index >= pd.Timestamp(start)]
            if end:
                df = df[df.index < pd.Timestamp(end)]
            if df is None or len(df) < min_prefix_bars + min_forward_bars:
                continue
            n = len(df)
            sym_written = 0
            # pre-slice per trading day once — labeling on the day frame is ~5x
            # faster than on the full multi-day frame (supertrend is recomputed
            # per call), and gives the identical intraday exit walk
            day_frames = {d: g for d, g in df.groupby(df.index.normalize())}
            # candidate entry bars: inside the entry window, with room to label
            for i in range(min_prefix_bars, n - min_forward_bars, stride):
                ts = df.index[i]
                t = ts.time()
                if t < e_start or t >= e_last:
                    continue
                spot = float(df["close"].iloc[i])
                day_df = day_frames.get(ts.normalize())
                if day_df is None:
                    continue
                local_i = day_df.index.get_loc(ts)
                if not isinstance(local_i, int):        # dup ts -> take first
                    local_i = int(pd.Series(range(len(day_df)),
                                            index=day_df.index)[ts].iloc[0])
                if local_i + min_forward_bars >= len(day_df):
                    continue                            # not enough same-day fwd
                hub.set_asof(ts.date(), spot)
                prefix = df.iloc[max(0, i - prefix_bars + 1): i + 1]
                for direction in directions:
                    try:
                        res = scanner.evaluate(sym, direction, df=prefix,
                                               price=spot, strategy="backtest",
                                               segment="FNO")
                    except Exception as e:
                        log.debug("evaluate %s %s failed: %s", sym, direction, e)
                        res = None
                    if not res or res.get("score") is None:
                        skipped += 1
                        continue
                    try:
                        walk = simulate_position(day_df, local_i, direction, spot)
                        pnl = float(walk.get("pnl_per_share") or 0.0)
                        reason = (walk["exits"][-1].get("reason")
                                  if walk.get("exits") else "eod")
                        bars = int(walk.get("bars_held") or 0)
                    except Exception as e:
                        log.debug("label %s %s failed: %s", sym, direction, e)
                        skipped += 1
                        continue
                    label = 1 if pnl > cost else 0
                    row = {
                        "ts": ts.isoformat(), "bar_time": ts.isoformat(),
                        "date": str(ts.date()), "symbol": sym,
                        "direction": direction, "segment": "FNO",
                        "source": "backtest", "price": spot,
                        "composite": round(float(res["score"]), 2),
                        "accepted": bool(res.get("accepted")),
                        "vetoed": bool(res.get("vetoed_by")),
                        "families": flatten_families(res.get("tree")),
                        "leaves": flatten_leaves(res.get("tree")),
                        "labeled": True, "label": label,
                        "outcome_pnl": round(pnl, 6),
                        "exit_reason": reason, "bars_held": bars,
                        "label_method": "exit_walk",
                    }
                    out.write(json.dumps(row, default=str) + "\n")
                    written += 1
                    sym_written += 1
                    wins += label
                    losses += (1 - label)
            if progress:
                progress(f"  [{si+1}/{len(all_syms)}] {sym}: "
                         f"{sym_written} samples")

    return {"symbols": len(all_syms), "written": written, "wins": wins,
            "losses": losses, "skipped": skipped,
            "win_rate": round(100.0 * wins / written, 1) if written else None,
            "out_path": out_path}

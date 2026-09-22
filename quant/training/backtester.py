"""quant/training/backtester.py — out-of-sample backtest, trained vs default.

Runs the REAL scoring/exit logic over a held-out period and reports P&L two ways,
for two entry modes, under two weight sets — the head-to-head that answers "do
the trained per-stock weights actually beat the untrained defaults?".

Entry modes (both requested):
  * "confirm" — indicator + agent: the OBS+Supertrend trigger (signals.day_signals)
    fires the idea, the agent tree must ACCEPT it (composite >= threshold, not
    vetoed). This is the live intraday sweep's logic.
  * "agents"  — naked agent buy/sell: no indicator. At each bar the tree scores
    BOTH directions and fires on the agent-primary rules (score >= min_score,
    edge over the opposite side >= margin, rising-edge crossing, veto-clean,
    per-side cooldown). This is the AgentScanner's logic.

Every entry is walked with engine.exits.simulate_position (real stop / target-1
partial / supertrend trail / EOD) — the same labeling used in training.

Weight sets are just different QuantConfig files, so "trained" vs "default" is a
config swap; the code path is identical. Point-in-time throughout (features from
the bar's prefix; daily/VIX as-of gated).
"""

from __future__ import annotations

import logging
from datetime import time as dtime

import numpy as np
import pandas as pd

log = logging.getLogger("training")

ENTRY_START = dtime(9, 20)
ENTRY_LAST = dtime(15, 15)
LOOKBACK_BARS = 400


# ── metrics ──────────────────────────────────────────────────────────────────
def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "total_pnl_pct": 0.0,
                "avg_pnl_pct": None, "expectancy_pct": None,
                "profit_factor": None, "max_drawdown_pct": None,
                "avg_win_pct": None, "avg_loss_pct": None}
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak)
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    n = len(pnls)
    return {
        "trades": n,
        "win_rate": round(100.0 * len(wins) / n, 1),
        "total_pnl_pct": round(float(pnls.sum()), 3),
        "avg_pnl_pct": round(float(pnls.mean()), 4),
        "expectancy_pct": round(float(pnls.mean()), 4),
        "avg_win_pct": round(float(wins.mean()), 4) if len(wins) else None,
        "avg_loss_pct": round(float(losses.mean()), 4) if len(losses) else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(float(dd.min()), 3) if len(dd) else 0.0,
    }


# ── one symbol, one mode ─────────────────────────────────────────────────────
def _backtest_symbol(scanner, hub, sym, mode, start, end, *,
                     stride=3, min_score=70.0, margin=10.0, cooldown_min=45.0,
                     min_forward_bars=6) -> list[dict]:
    from engine.exits import simulate_position

    full = hub.full_5m(sym)
    if full is None or full.empty:
        return []
    if end:
        full = full[full.index < pd.Timestamp(end)]
    day_keys = sorted({ts.normalize() for ts in full.index
                       if (not start or ts >= pd.Timestamp(start))})
    trades: list[dict] = []
    last_fired: dict[str, pd.Timestamp] = {}

    def _walk(day_slice, ts, direction, price, score):
        try:
            li = day_slice.index.get_loc(ts)
            if not isinstance(li, (int, np.integer)):
                return
            if li + min_forward_bars >= len(day_slice):
                return
            w = simulate_position(day_slice, int(li), direction, price)
            pnl = float(w.get("pnl_per_share") or 0.0)
            trades.append({
                "symbol": sym, "mode": mode, "direction": direction,
                "entry_time": ts.isoformat(), "entry": round(price, 3),
                "exit_reason": (w["exits"][-1].get("reason")
                                if w.get("exits") else "eod"),
                "pnl_per_share": round(pnl, 4),
                "pnl_pct": round(pnl / price * 100.0, 4) if price else 0.0,
                "score": round(float(score), 1)})
        except Exception as e:
            log.debug("walk %s %s failed: %s", sym, direction, e)

    for day in day_keys:
        window = full[full.index <= day + pd.Timedelta(hours=23)]
        window = window.tail(LOOKBACK_BARS + 80)
        day_slice = window[window.index.normalize() == day]
        if len(day_slice) < min_forward_bars + 2:
            continue

        if mode == "confirm":
            from signals.engine import day_signals
            for s in day_signals(window):
                i = s["bar"]
                ts = window.index[i]
                if ts.normalize() != day or not (ENTRY_START <= ts.time() < ENTRY_LAST):
                    continue
                direction = s["direction"]
                last = last_fired.get(direction)
                if last is not None and (ts - last).total_seconds() < cooldown_min * 60:
                    continue
                price = float(window["close"].iloc[i])
                hub.set_asof(ts.date(), price)
                res = scanner.evaluate(sym, direction, df=window.iloc[:i + 1],
                                       price=price, strategy="backtest",
                                       segment="FNO")
                if res and res.get("accepted"):
                    last_fired[direction] = ts
                    _walk(day_slice, ts, direction, price, res["score"])

        else:  # "agents" — naked, both directions, rising-edge trigger
            prev = {"BUY": None, "SELL": None}
            di = {ts: k for k, ts in enumerate(day_slice.index)}
            for pos in range(0, len(day_slice), stride):
                ts = day_slice.index[pos]
                if not (ENTRY_START <= ts.time() < ENTRY_LAST):
                    prev = {"BUY": None, "SELL": None}
                    continue
                gi = window.index.get_loc(ts)
                if not isinstance(gi, (int, np.integer)):
                    continue
                price = float(window["close"].iloc[gi])
                hub.set_asof(ts.date(), price)
                scores = {}
                for d in ("BUY", "SELL"):
                    r = scanner.evaluate(sym, d, df=window.iloc[:gi + 1],
                                         price=price, strategy="backtest",
                                         segment="FNO")
                    scores[d] = r
                for d in ("BUY", "SELL"):
                    r = scores.get(d)
                    if not r or r.get("score") is None:
                        continue
                    opp = scores.get("SELL" if d == "BUY" else "BUY")
                    opp_s = float(opp["score"]) if opp and opp.get("score") is not None else 0.0
                    sc = float(r["score"])
                    p = prev[d]
                    fire = (sc >= min_score and (sc - opp_s) >= margin
                            and not r.get("vetoed_by")
                            and p is not None and p < min_score)
                    if fire:
                        last = last_fired.get(d)
                        if last is None or (ts - last).total_seconds() >= cooldown_min * 60:
                            last_fired[d] = ts
                            _walk(day_slice, ts, d, price, sc)
                    prev[d] = sc
    return trades


# ── run one weight set across symbols/modes ──────────────────────────────────
def run_backtest(config_path: str | None, symbols, start, end, *,
                 cache_dir="data/hf_cache", vix_path=None, modes=("confirm", "agents"),
                 stride=3, min_score=70.0, margin=10.0, progress=print) -> dict:
    from engine.scanner import Scanner
    from quant.registry import build_root
    from quant.config import QuantConfig
    from .replay_hub import ReplayHub

    hub = ReplayHub.from_parquet_cache(cache_dir, symbols=symbols, vix_path=vix_path)
    cfg = QuantConfig(config_path) if config_path else QuantConfig("___defaults_only___")
    scanner = Scanner(hub, build_root(), cfg, poll_seconds=0)
    syms = symbols or hub.fo_universe()

    result: dict = {"by_mode": {}}
    for mode in modes:
        all_trades: list[dict] = []
        by_symbol: dict = {}
        for sym in syms:
            tr = _backtest_symbol(scanner, hub, sym, mode, start, end,
                                  stride=stride, min_score=min_score, margin=margin)
            by_symbol[sym] = _metrics(tr)
            all_trades.extend(tr)
            if progress:
                progress(f"    [{mode}] {sym}: {len(tr)} trades")
        result["by_mode"][mode] = {"overall": _metrics(all_trades),
                                   "by_symbol": by_symbol,
                                   "trades": all_trades}
    return result


def compare(trained_config_path: str, symbols, start, end, *,
            cache_dir="data/hf_cache", vix_path=None,
            modes=("confirm", "agents"), stride=3, progress=print) -> dict:
    """Run the SAME period with trained per-stock weights and with defaults."""
    if not symbols:                          # default to whatever is cached
        from .hf_data import cached_symbols
        symbols = cached_symbols(cache_dir)
    if progress:
        progress("== DEFAULT weights ==")
    default = run_backtest(None, symbols, start, end, cache_dir=cache_dir,
                           vix_path=vix_path, modes=modes, stride=stride,
                           progress=progress)
    if progress:
        progress("== TRAINED weights ==")
    trained = run_backtest(trained_config_path, symbols, start, end,
                           cache_dir=cache_dir, vix_path=vix_path, modes=modes,
                           stride=stride, progress=progress)
    return {"default": default, "trained": trained,
            "symbols": list(symbols), "start": start, "end": end}


def format_comparison(cmp: dict) -> str:
    lines = [f"\n=== BACKTEST {cmp['start']}..{cmp['end']}  "
             f"symbols={cmp['symbols']} ===",
             "(trained per-stock weights vs untrained defaults, same trades)"]
    for mode in cmp["trained"]["by_mode"]:
        d = cmp["default"]["by_mode"][mode]["overall"]
        t = cmp["trained"]["by_mode"][mode]["overall"]
        lines.append(f"\n── mode: {mode} ──")
        lines.append(f"  {'metric':<18}{'DEFAULT':>12}{'TRAINED':>12}{'Δ':>12}")
        for key, label in [("trades", "trades"), ("win_rate", "win %"),
                           ("total_pnl_pct", "total P&L %"),
                           ("expectancy_pct", "expectancy %"),
                           ("profit_factor", "profit factor"),
                           ("max_drawdown_pct", "max DD %")]:
            dv, tv = d.get(key), t.get(key)
            delta = ""
            if isinstance(dv, (int, float)) and isinstance(tv, (int, float)):
                delta = f"{tv - dv:+.3f}"
            lines.append(f"  {label:<18}{str(dv):>12}{str(tv):>12}{delta:>12}")
    return "\n".join(lines)

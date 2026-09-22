"""quant/training/labeler.py — attach a forward WIN/LOSS outcome to each sample.

A sample records what the agents saw at entry; the label records what happened
next. We grade every sample the SAME way the live system would have exited the
trade — engine.exits.simulate_position walks the rest of the entry day with the
real stop / target-1 partial / supertrend-trail / EOD rules — and call it a win
when the realized per-share P&L clears the cost floor. That keeps the training
target identical to production reality rather than an arbitrary horizon.

Fallback: if the exit engine can't be used (import failure, or a sample from a
segment it doesn't walk), we fall back to a fixed-horizon directional return
(``method="horizon"``) so labeling still makes progress.

Candles come from a *provider* so this is testable offline and works with both
the live hub and fetch_history CSVs:

    provider(symbol, date) -> DataFrame | None
        DatetimeIndex (that day's 5-minute bars), columns open/high/low/close.

Only samples whose entry day has enough *forward* bars get labeled; the rest
stay pending and are retried on the next run (idempotent — safe to re-run).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import pandas as pd

log = logging.getLogger("training")

try:
    from engine.exits import simulate_position as _simulate_position
except Exception:                          # keep the labeler usable without it
    _simulate_position = None


# ── candle providers ────────────────────────────────────────────────────────
class HubCandlesProvider:
    """Live/day candles from the running DataHub. Best run at/after EOD so the
    forward bars for same-day samples already exist."""

    def __init__(self, hub):
        self.hub = hub

    def __call__(self, symbol: str, date: str):
        try:
            df = self.hub.candles_5m(symbol)
        except Exception:
            return None
        if df is None or df.empty:
            return None
        return _slice_day(df, date)


class CsvCandlesProvider:
    """5-minute candles from fetch_history.py CSVs in a directory.

    Accepts the fetch_history layout (time,open,high,low,close,volume,oi) and is
    tolerant of the yfinance layout (Datetime,Open,High,Low,Close,...). Files
    are matched by symbol prefix, e.g. RELIANCE-EQ_5m_30d.csv for 'RELIANCE'."""

    def __init__(self, directory: str):
        self.dir = directory
        self._cache: dict[str, pd.DataFrame | None] = {}

    def _load(self, symbol: str) -> pd.DataFrame | None:
        if symbol in self._cache:
            return self._cache[symbol]
        df = None
        try:
            base = symbol.split("-")[0].upper()
            for fn in sorted(os.listdir(self.dir)):
                if not fn.lower().endswith(".csv"):
                    continue
                stem = fn.split("_")[0].split("-")[0].upper()
                if stem == base and "5m" in fn:
                    df = _read_candle_csv(os.path.join(self.dir, fn))
                    break
        except Exception as e:
            log.debug("csv provider load %s failed: %s", symbol, e)
        self._cache[symbol] = df
        return df

    def __call__(self, symbol: str, date: str):
        df = self._load(symbol)
        return None if df is None else _slice_day(df, date)


def _read_candle_csv(path: str) -> pd.DataFrame | None:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    tcol = cols.get("time") or cols.get("datetime") or cols.get("date")
    if not tcol:
        return None
    df.index = pd.to_datetime(df[tcol], errors="coerce")
    df = df[df.index.notna()]
    ren = {}
    for want in ("open", "high", "low", "close", "volume"):
        if want in cols:
            ren[cols[want]] = want
    df = df.rename(columns=ren)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].sort_index()


def _slice_day(df: pd.DataFrame, date: str) -> pd.DataFrame | None:
    """Return the bars belonging to ``date`` (tz-naive comparison)."""
    idx = df.index
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
            df = df.copy()
            df.index = idx
    except Exception:
        pass
    day = df[idx.normalize() == pd.Timestamp(date)]
    return day if not day.empty else None


def _entry_index(day_df: pd.DataFrame, bar_time: str) -> int | None:
    """Position of the entry bar (== bar_time, else the last bar before it)."""
    try:
        ts = pd.Timestamp(bar_time)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
    except Exception:
        return None
    idx = day_df.index
    pos = idx.searchsorted(ts, side="right") - 1
    if pos < 0:
        return None
    return int(pos)


# ── labeling ────────────────────────────────────────────────────────────────
def _label_one(day_df, bar_time, direction, entry_price,
               method, min_forward_bars, horizon_bars, cost):
    """-> (label 0/1, pnl_per_share, exit_reason, bars_held) or None if the
    forward window isn't available yet."""
    entry_i = _entry_index(day_df, bar_time)
    if entry_i is None:
        return None
    forward = len(day_df) - 1 - entry_i
    if forward < min_forward_bars:
        return None                          # not enough future yet — retry later

    entry = float(entry_price) if entry_price is not None \
        else float(day_df["close"].iloc[entry_i])

    use_walk = method == "exit_walk" and _simulate_position is not None
    if use_walk:
        try:
            walk = _simulate_position(day_df, entry_i, direction, entry)
            pnl = float(walk.get("pnl_per_share") or 0.0)
            reason = (walk["exits"][-1].get("reason")
                      if walk.get("exits") else "eod")
            bars = int(walk.get("bars_held") or forward)
            return (1 if pnl > cost else 0), round(pnl, 6), reason, bars
        except Exception as e:
            log.debug("exit-walk label failed (%s %s), using horizon: %s",
                      direction, bar_time, e)

    # fixed-horizon directional return fallback
    j = min(entry_i + horizon_bars, len(day_df) - 1)
    exit_px = float(day_df["close"].iloc[j])
    sign = 1.0 if str(direction).upper() == "BUY" else -1.0
    pnl = sign * (exit_px - entry)
    return (1 if pnl > cost else 0), round(pnl, 6), "horizon", int(j - entry_i)


def label_samples(provider, samples_path: str = None, *,
                  method: str = "exit_walk", min_forward_bars: int = 6,
                  horizon_bars: int = 12, cost: float = 0.0) -> dict:
    """Backfill outcomes for every unlabeled sample the provider can serve.

    method            'exit_walk' (faithful, default) or 'horizon'
    min_forward_bars  need at least this many bars after entry to label
    horizon_bars      lookahead for the 'horizon' method / fallback (12 = 1h)
    cost              per-share cost floor; pnl must clear it to count as a win

    Rewrites the file in place (atomic). Idempotent — already-labeled rows are
    left untouched, so it's safe to re-run each session. Returns a summary."""
    from .collector import DEFAULT_PATH
    samples_path = samples_path or DEFAULT_PATH
    if not os.path.exists(samples_path):
        return {"labeled_now": 0, "still_pending": 0, "total": 0,
                "wins": 0, "losses": 0, "no_candles": 0}

    rows = []
    with open(samples_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # group pending rows by (symbol, date) so each day's candles load once
    pending: dict[tuple, list[int]] = {}
    for i, r in enumerate(rows):
        if not r.get("labeled"):
            pending.setdefault((r.get("symbol"), r.get("date")), []).append(i)

    labeled_now = wins = losses = no_candles = 0
    for (symbol, date), idxs in pending.items():
        day_df = provider(symbol, date)
        if day_df is None or day_df.empty:
            no_candles += len(idxs)
            continue
        for i in idxs:
            r = rows[i]
            res = _label_one(day_df, r.get("bar_time"), r.get("direction"),
                             r.get("price"), method, min_forward_bars,
                             horizon_bars, cost)
            if res is None:
                continue                     # forward window not ready
            label, pnl, reason, bars = res
            r.update(labeled=True, label=label, outcome_pnl=pnl,
                     exit_reason=reason, bars_held=bars, label_method=method)
            labeled_now += 1
            wins += label
            losses += (1 - label)

    if labeled_now:
        _rewrite(samples_path, rows)

    still_pending = sum(1 for r in rows if not r.get("labeled"))
    return {"labeled_now": labeled_now, "still_pending": still_pending,
            "total": len(rows), "wins": wins, "losses": losses,
            "no_candles": no_candles}


def _rewrite(path: str, rows: list[dict]) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

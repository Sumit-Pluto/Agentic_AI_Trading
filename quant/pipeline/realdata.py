"""quant/pipeline/realdata.py — load REAL Options_data into the pipeline.

Produces the SAME `DummyData` shape that `synthdata.generate()` returns, so every
downstream layer (features / labeler / model) runs unchanged — we only swap the
data source. Maps:

    raw/minute_candles/{SYM}_5min.csv  -> candles      (5-min OHLCV per stock)
    raw/minute_candles/NIFTY_5min.csv  -> index
    raw/index_close/*  (India VIX row) -> vix           ('YYYY-MM-DD' -> level)
    derived/iv_greeks/iv_greeks_*.csv  -> chain         (per-strike ce/pe oi+iv, front expiry)
    raw/mwpl/mwpl_*.csv (in_ban)       -> meta['ban']
    raw/corporate_actions (board mtgs) -> meta['days_to_event'] (next Financial Results)
    median 5m volume tertiles          -> meta['liq_tier']

Usage:
    from quant.pipeline import realdata, pipeline as P
    dd = realdata.load(symbols=["RELIANCE","HDFCBANK"], start="2024-09-02", end="2024-10-31")
    res = P.train(dd)
"""
from __future__ import annotations

import json
import os
import glob

import numpy as np
import pandas as pd

from .synthdata import DummyData

DATA_DIR = os.environ.get("OPTIONS_DATA_DIR", "/Users/mac/Downloads/Options_data")


def _p(*a):
    return os.path.join(DATA_DIR, *a)


def _read_candles(sym, start, end, data_dir):
    f = os.path.join(data_dir, "raw", "minute_candles", f"{sym}_5min.csv")
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz=df.index.tz)]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz=df.index.tz) + pd.Timedelta(days=1)]
    return df[["open", "high", "low", "close", "volume"]]


def _load_vix(days, data_dir):
    """India VIX close per trading day -> {'YYYY-MM-DD': level}."""
    vix = {}
    for dstr in days:
        f = os.path.join(data_dir, "raw", "index_close", f"ind_close_all_{dstr}.csv")
        if not os.path.exists(f):
            continue
        try:
            d = pd.read_csv(f)
            row = d[d["Index Name"].str.strip() == "India VIX"]
            if len(row):
                vix[dstr] = float(row["Closing Index Value"].iloc[0])
        except Exception:
            pass
    return vix


def _load_bans(days, data_dir):
    """Set of banned symbols per day -> {'YYYY-MM-DD': set(symbols)}."""
    bans = {}
    for dstr in days:
        f = os.path.join(data_dir, "raw", "mwpl", f"mwpl_{dstr}.csv")
        if not os.path.exists(f):
            continue
        try:
            d = pd.read_csv(f)
            banned = d.loc[d["in_ban"].astype(str).isin(["True", "1", "1.0"]), "symbol"]
            bans[dstr] = set(banned.astype(str))
        except Exception:
            bans[dstr] = set()
    return bans


def _pivot_chain_day(g):
    """One (symbol, day) iv_greeks group -> {'spot', 'strikes': df[strike,ce_oi,pe_oi,ce_iv,pe_iv]}."""
    front = g["expiry"].min()                       # near-month
    gg = g[g["expiry"] == front]
    ce = gg[gg["opt_type"] == "CE"].set_index("strike")
    pe = gg[gg["opt_type"] == "PE"].set_index("strike")
    strikes = sorted(set(ce.index) | set(pe.index))
    if not strikes:
        return None
    rows = []
    for k in strikes:
        rows.append({
            "strike": float(k),
            "ce_oi": float(ce["oi"].get(k, 0.0) or 0.0),
            "pe_oi": float(pe["oi"].get(k, 0.0) or 0.0),
            "ce_iv": float(ce["iv"].get(k, np.nan)),
            "pe_iv": float(pe["iv"].get(k, np.nan)),
        })
    df = pd.DataFrame(rows)
    med_iv = np.nanmedian(np.concatenate([df["ce_iv"].values, df["pe_iv"].values]))
    if not np.isfinite(med_iv):
        med_iv = 0.3
    df["ce_iv"] = df["ce_iv"].fillna(med_iv).clip(0.01, 3.0)
    df["pe_iv"] = df["pe_iv"].fillna(med_iv).clip(0.01, 3.0)
    return {"spot": float(g["spot"].iloc[0]), "strikes": df}


def _load_chain(symbols, days, data_dir, verbose):
    """chain[(sym,'YYYY-MM-DD')] from per-day iv_greeks files."""
    chain = {}
    symset = set(symbols)
    for i, dstr in enumerate(days):
        f = os.path.join(data_dir, "derived", "iv_greeks", f"iv_greeks_{dstr}.csv")
        if not os.path.exists(f):
            continue
        d = pd.read_csv(f, usecols=["symbol", "expiry", "strike", "opt_type", "iv", "oi", "spot"])
        d = d[d["symbol"].isin(symset)]
        for sym, g in d.groupby("symbol"):
            ch = _pivot_chain_day(g)
            if ch is not None:
                chain[(sym, dstr)] = ch
        if verbose and i % 20 == 0:
            print(f"  chain: {i+1}/{len(days)} days", flush=True)
    return chain


def _event_lookup(symbols, data_dir):
    """Per symbol: sorted Financial-Results (earnings) dates, for days_to_event."""
    f = os.path.join(data_dir, "raw", "corporate_actions",
                     "board_meetings_01-09-2024_17-09-2026.json")
    ev = {}
    if not os.path.exists(f):
        return ev
    bm = json.load(open(f))
    for r in bm:
        purp = str(r.get("bm_purpose", ""))
        if "Financial Results" not in purp:
            continue
        sym = str(r.get("bm_symbol", ""))
        try:
            d = pd.Timestamp(r.get("bm_date")).normalize()
        except Exception:
            continue
        ev.setdefault(sym, []).append(d)
    for s in ev:
        ev[s] = np.array(sorted(set(ev[s])))
    return ev


def _days_to_event(ev_dates, day_ts):
    if ev_dates is None or len(ev_dates) == 0:
        return 30.0
    fut = ev_dates[ev_dates >= day_ts]
    return float((fut[0] - day_ts).days) if len(fut) else 30.0


def load(symbols=None, start=None, end=None, data_dir=DATA_DIR,
         with_chain=True, verbose=True) -> DummyData:
    if verbose:
        print(f"[realdata] loading from {data_dir}", flush=True)
    mc = os.path.join(data_dir, "raw", "minute_candles")
    if symbols is None:
        symbols = sorted(os.path.basename(x)[:-9] for x in glob.glob(os.path.join(mc, "*_5min.csv"))
                         if not os.path.basename(x).startswith(("NIFTY", "BANKNIFTY", "FINNIFTY")))

    # candles
    candles, liq_med = {}, {}
    for sym in symbols:
        df = _read_candles(sym, start, end, data_dir)
        if df is None or df.empty:
            if verbose:
                print(f"  skip {sym}: no candles in range", flush=True)
            continue
        candles[sym] = df
        liq_med[sym] = float(df["volume"].median())
    symbols = list(candles.keys())
    if not symbols:
        raise RuntimeError("no candles loaded for requested symbols/range")

    # index (NIFTY as the market factor)
    index = _read_candles("NIFTY", start, end, data_dir)
    if index is None:
        # fall back to the mean of loaded stocks so FeatureBank still works
        index = pd.concat([c["close"] for c in candles.values()], axis=1).mean(axis=1).to_frame("close")
        for col in ("open", "high", "low"):
            index[col] = index["close"]
        index["volume"] = 0.0

    # trading days = dates present in the index candles
    days = [d.strftime("%Y-%m-%d") for d in sorted(pd.Index(index.index.normalize().unique()))]

    # liquidity tiers (A/B/C by median 5m volume tertiles)
    if liq_med:
        q1, q2 = np.quantile(list(liq_med.values()), [1/3, 2/3])
        liq_tier = {s: ("A" if v >= q2 else "B" if v >= q1 else "C") for s, v in liq_med.items()}
    else:
        liq_tier = {}

    vix = _load_vix(days, data_dir)
    bans = _load_bans(days, data_dir)
    events = _event_lookup(symbols, data_dir)
    chain = _load_chain(symbols, days, data_dir, verbose) if with_chain else {}

    era_break = pd.Timestamp("2025-09-01")
    meta = {}
    for sym in symbols:
        ev = events.get(sym)
        for d in candles[sym].index.normalize().unique():
            dstr = d.strftime("%Y-%m-%d")
            meta[(sym, dstr)] = {
                "vix": vix.get(dstr, 15.0),
                "ban": sym in bans.get(dstr, ()),
                "liq_tier": liq_tier.get(sym, "A"),
                "era": "post" if d.tz_localize(None) >= era_break else "pre",
                "days_to_event": _days_to_event(ev, d.tz_localize(None)),
            }

    if verbose:
        print(f"[realdata] {len(symbols)} symbols, {len(days)} days, "
              f"{len(chain)} chain-days, vix={len(vix)} bans={sum(len(v) for v in bans.values())}", flush=True)
    return DummyData(symbols=symbols, candles=candles, index=index, vix=vix,
                     chain=chain, meta=meta, days=days)


def _cli(argv=None):
    import argparse
    import pickle
    import time
    from . import pipeline as P

    ap = argparse.ArgumentParser(prog="python -m quant.pipeline.realdata",
                                 description="Train the meta-model on real Options_data.")
    ap.add_argument("--smoke", action="store_true",
                    help="6 liquid symbols, Sep-Oct 2024 (fast end-to-end check)")
    ap.add_argument("--symbols", default=None,
                    help="'all', an integer N (first N by name), or comma list")
    ap.add_argument("--start", default="2024-09-02")
    ap.add_argument("--end", default="2026-01-21")      # candle coverage ends here
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--workers", type=int, default=0,
                    help="0=auto (cores-1), 1=serial, N=parallel build over N workers")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--no-chain", action="store_true", help="skip option chain (faster, weaker)")
    ap.add_argument("--out", default=None, help="path to save {model,bank,report} pickle")
    a = ap.parse_args(argv)

    if a.smoke:
        syms, a.start, a.end = ["RELIANCE", "HDFCBANK", "INFY", "SBIN", "ICICIBANK", "TCS"], "2024-09-02", "2024-10-31"
    elif a.symbols in (None, "all"):
        syms = None
    elif a.symbols.isdigit():
        mc = os.path.join(a.data_dir, "raw", "minute_candles")
        allsyms = sorted(os.path.basename(x)[:-9] for x in glob.glob(os.path.join(mc, "*_5min.csv"))
                         if not os.path.basename(x).startswith(("NIFTY", "BANKNIFTY", "FINNIFTY")))
        syms = allsyms[:int(a.symbols)]
    else:
        syms = [s.strip() for s in a.symbols.split(",")]

    t0 = time.time()
    dd = load(symbols=syms, start=a.start, end=a.end, data_dir=a.data_dir, with_chain=not a.no_chain)
    print(f"[load] {time.time()-t0:.1f}s", flush=True)
    t1 = time.time()
    workers = a.workers if a.workers > 0 else max(1, (os.cpu_count() or 2) - 1)
    if workers > 1:
        from .ptrain import train_parallel
        res = train_parallel(dd, stride=a.stride, n_workers=workers, progress=print)
    else:
        res = P.train(dd, stride=a.stride, progress=print)
    print(f"[train] {time.time()-t1:.1f}s ({workers} workers)", flush=True)
    rep = res["report"]
    print(json.dumps({k: rep[k] for k in ("samples", "features", "base_rate",
                                          "holdout", "walk_forward")}, indent=2, default=str))
    print("top features:", list(rep["top_features"])[:10])

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "wb") as f:
            pickle.dump({"model": res["model"], "bank": res.get("bank"), "report": rep}, f)
        with open(a.out + ".report.json", "w") as f:
            json.dump(rep, f, indent=2, default=str)
        print(f"[saved] model -> {a.out}  (report -> {a.out}.report.json)")


if __name__ == "__main__":
    _cli()

#!/usr/bin/env python3
"""Fetch historical OHLCV candles from Shoonya -> CSVs in data/history/.

yfinance has been removed; this tool now uses the Shoonya API exclusively.
It needs today's cached session — run the app once to log in (creates
.session_token) and then this reuses it (no browser needed).

Symbols accept three forms:
  * a bare / -EQ NSE cash symbol:  RELIANCE  or  RELIANCE-EQ
  * EXCH:TRADINGSYMBOL:            NSE:RELIANCE-EQ , NFO:RELIANCE30JUL26F
  * explicit EXCH|TOKEN:           NSE|2885     (intraday only)

Intraday uses TPSeries (1/3/5/10/15/30/60 min); daily uses EODChartData.
Shoonya returns newest-first; rows are written oldest-first.

Examples:
    python fetch_history.py RELIANCE-EQ --interval 5 --days 30
    python fetch_history.py NSE:RELIANCE-EQ --daily --days 365
    python fetch_history.py "NSE|2885" --interval 5 --days 5
"""

import argparse
import csv
import os
import time

from dotenv import load_dotenv

from shoonya_client import ShoonyaSession, build_session, load_scripmaster

OUTDIR = os.path.join("data", "history")


def _session() -> ShoonyaSession:
    load_dotenv()
    s = build_session()
    if not s.restore_session():
        raise SystemExit(
            "no cached Shoonya session for today — run the app once to log in "
            "(creates .session_token), then retry.")
    return s


def _resolve(sym: str) -> tuple[str, str, str]:
    """-> (exchange, token, tradingsymbol). Accepts EXCH|TOKEN, EXCH:TSYM, or a
    bare / -EQ NSE symbol."""
    if "|" in sym:
        exch, tok = sym.split("|", 1)
        return exch.upper(), tok, ""
    exch = "NSE"
    if ":" in sym:
        exch, sym = sym.split(":", 1)
        exch = exch.upper()
    master = load_scripmaster(exch)
    for cand in (sym, f"{sym}-EQ", sym.upper(), f"{sym.upper()}-EQ"):
        row = master.get(cand)
        if row:
            return exch, row.get("Token"), cand
    raise SystemExit(f"symbol {sym!r} not found in {exch} scrip master")


def fetch_intraday(s: ShoonyaSession, sym: str, interval: int, days: int):
    os.makedirs(OUTDIR, exist_ok=True)
    exch, tok, tsym = _resolve(sym)
    end = time.time()
    rows = s.get_time_series(exch, tok, end - days * 86400, end,
                             interval=str(interval))
    if not rows:
        print(f"!! no data for {sym} ({interval}m, {days}d)")
        return None
    safe = (tsym or f"{exch}_{tok}").replace(":", "_").replace(" ", "_")
    path = os.path.join(OUTDIR, f"{safe}_{interval}m_{days}d.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume", "oi"])
        for r in reversed(rows):                 # newest-first -> oldest-first
            w.writerow([r.get("time"), r.get("into"), r.get("inth"),
                        r.get("intl"), r.get("intc"),
                        r.get("intv", 0), r.get("intoi", 0)])
    print(f"{sym:<18} {interval:>3}m {days:>4}d -> {len(rows):>6} rows   {path}")
    return path


def fetch_daily(s: ShoonyaSession, sym: str, days: int):
    os.makedirs(OUTDIR, exist_ok=True)
    exch, tok, tsym = _resolve(sym)
    if not tsym:
        raise SystemExit("daily needs a trading symbol (EXCH:TSYM), not a token")
    end = time.time()
    rows = s.get_daily_history(exch, tsym, end - days * 86400, end)
    if not rows:
        print(f"!! no daily data for {sym} ({days}d)")
        return None
    path = os.path.join(OUTDIR, f"{tsym.replace(' ', '_')}_1d_{days}d.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in reversed(rows):
            w.writerow([r.get("time"), r.get("into"), r.get("inth"),
                        r.get("intl"), r.get("intc"), r.get("intv", 0)])
    print(f"{sym:<18}  1d {days:>4}d -> {len(rows):>6} rows   {path}")
    return path


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="*",
                   help="Shoonya symbols (default: RELIANCE-EQ)")
    p.add_argument("--interval", type=int, default=5,
                   help="intraday minutes: 1 3 5 10 15 30 60 (default 5)")
    p.add_argument("--days", type=int, default=30, help="lookback days")
    p.add_argument("--daily", action="store_true",
                   help="fetch daily EOD bars instead of intraday")
    a = p.parse_args()

    symbols = a.symbols or ["RELIANCE-EQ"]
    s = _session()
    for sym in symbols:
        if a.daily:
            fetch_daily(s, sym, a.days)
        else:
            fetch_intraday(s, sym, a.interval, a.days)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Download Shoonya scripmaster from public CDN (no auth required).

Usage:
    python3 download_scripmaster.py
"""

import csv
import io
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)

OUTPUT_PATH = Path(__file__).parent / "scripmaster.csv"

# Shoonya CDN — publicly accessible, updated daily
EXCHANGE_URLS = {
    "NSE":   "https://api.shoonya.com/NSE_symbols.txt.zip",
    "NFO":   "https://api.shoonya.com/NFO_symbols.txt.zip",
    "MCX":   "https://api.shoonya.com/MCX_symbols.txt.zip",
    "BSE":   "https://api.shoonya.com/BSE_symbols.txt.zip",
    "CDS":   "https://api.shoonya.com/CDS_symbols.txt.zip",
    "NCDEX": "https://api.shoonya.com/NCDEX_symbols.txt.zip",
}

# Shoonya format: Exchange,Token,LotSize,GNGD,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,...
# GNGD = price multiplier that converts (qty × price) into actual rupees. Ex: MCX LEAD
# quotes Rs/kg with qty in MT (lotsize=5), so GNGD=1000 turns the 5×price product into Rs.
COL_MAP = {
    "Exchange":      "exch",
    "Token":         "token",
    "TradingSymbol": "tsym",
    "Symbol":        "sym",        # underlying name e.g. NATURALGAS
    "Instrument":    "instrumenttype",
    "Expiry":        "expd",
    "OptionType":    "opttype",
    "StrikePrice":   "strikeprice",
    "LotSize":       "lotsize",
    "TickSize":      "ticksize",
    "GNGD":          "prcftr",
}

OUR_COLS = ["exch", "token", "tsym", "sym", "instrumenttype", "expd", "opttype", "strikeprice", "lotsize", "ticksize", "prcftr"]


def download_exchange(name: str, url: str) -> list[dict]:
    print(f"  {name}... ", end="", flush=True)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                content = f.read().decode("utf-8", errors="replace")
        rows = []
        for row in csv.DictReader(io.StringIO(content)):
            normalized = {dst: row.get(src, "").strip() for src, dst in COL_MAP.items()}
            # Futures use "XX" for opttype — normalize to empty string
            if normalized.get("opttype") == "XX":
                normalized["opttype"] = ""
            rows.append(normalized)
        print(f"{len(rows):,} symbols")
        return rows
    except Exception as e:
        print(f"FAILED: {e}")
        return []


def download_and_save(output_path: Path = OUTPUT_PATH) -> dict:
    """Download every exchange's scripmaster from the CDN and write the merged
    CSV to output_path (atomically). Returns {"total": int, "per_exchange": {..}}.

    Raises RuntimeError if nothing was downloaded so callers (CLI, API endpoint)
    never overwrite a good file with an empty one.
    """
    all_rows: list[dict] = []
    per_exchange: dict[str, int] = {}
    for exchange, url in EXCHANGE_URLS.items():
        rows = download_exchange(exchange, url)
        per_exchange[exchange] = len(rows)
        all_rows.extend(rows)

    if not all_rows:
        raise RuntimeError("No symbols downloaded (all exchanges failed).")

    # Write to a temp file then replace, so a mid-write failure can't corrupt
    # the existing scripmaster the running app is reading.
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUR_COLS)
        writer.writeheader()
        writer.writerows(all_rows)
    tmp_path.replace(output_path)

    return {"total": len(all_rows), "per_exchange": per_exchange, "path": str(output_path)}


def main():
    print("Downloading Shoonya scripmaster from CDN (no auth needed)...")
    try:
        stats = download_and_save()
    except RuntimeError as e:
        print(f"\n{e} Check your internet connection.")
        sys.exit(1)

    print(f"\n✓ {stats['total']:,} symbols saved to {stats['path']}")


if __name__ == "__main__":
    main()

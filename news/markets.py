"""Market tape (Twelve Data, one batched /quote call) + best-effort NSE FII/DII.

These are GLOBAL macro instruments (US indices, treasury yield, DXY, crude,
gold, USD/INR, US index futures) that Shoonya/NSE does not provide — so per the
data-source decision they come from one non-yfinance global source (Twelve
Data). Everything Indian (candles, option chain, quotes) is on Shoonya. yfinance
has been removed entirely.

fetch_tape() -> {name: Quote|None} for the macro tickers; Quote =
{'price', 'chg_pct' (vs previous close), 'asof'}. A single Twelve Data /quote
call covers all symbols; any per-symbol gap -> None for that name only. Needs
TWELVEDATA_KEY in the environment; without it every chip degrades to n/a.

fii_dii() -> {'date', 'fii_net_cr', 'dii_net_cr'} | None from
https://www.nseindia.com/api/fiidiiTradeReact with a cookie-warmup GET on
nseindia.com first. NSE geo/bot-blocks freely; every failure path returns
None, never an exception.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

try:
    from .indianapi import _mark
except ImportError:  # direct script execution
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from indianapi import _mark

# tape name -> Twelve Data symbol. VERIFY on first live run: symbology and
# coverage vary by Twelve Data plan; any symbol the API doesn't return degrades
# that one chip to n/a (never crashes). Edit here to retune — nothing else
# changes. (Index futures, GIFT Nifty and treasury yield may be absent on lower
# tiers; ES/NQ serve as the overnight proxy when present.)
TICKERS = {"usdinr": "USD/INR", "crude_brent": "BRENT", "crude_wti": "WTI",
           "us10y": "US10Y", "dxy": "DXY", "spx": "SPX",
           "nasdaq": "IXIC", "nikkei": "N225", "hangseng": "HSI",
           "gold": "XAU/USD",
           "spx_fut": "ES", "nasdaq_fut": "NQ", "dow_fut": "YM",
           "gift_nifty": "GIFT NIFTY"}

HEALTH: dict = {}

_NSE_HOME = "https://www.nseindia.com"
_NSE_FIIDII = "https://www.nseindia.com/api/fiidiiTradeReact"
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/fii-dii",
}


# ------------------------------------------------------------------- tape
_TD_QUOTE_URL = "https://api.twelvedata.com/quote"


def _quote_from_td(entry) -> dict | None:
    """One Twelve Data /quote entry -> {price, chg_pct, asof} | None.

    Tolerates per-symbol error objects ({'status':'error',...}) and missing
    fields, deriving percent change from previous_close when absent.
    """
    if not isinstance(entry, dict) or entry.get("status") == "error":
        return None
    price = _num(entry.get("close"))
    if price is None:
        return None
    chg = _num(entry.get("percent_change"))
    if chg is None:
        prev = _num(entry.get("previous_close"))
        chg = (price / prev - 1.0) * 100.0 if prev else 0.0
    ts = entry.get("datetime") or entry.get("timestamp")
    return {"price": round(price, 4), "chg_pct": round(chg, 3),
            "asof": str(ts) if ts is not None else ""}


def _td_fetch() -> dict | None:
    """Batched Twelve Data /quote over all tape symbols -> {symbol: entry}.
    None on missing key / transport error (every failure path is guarded)."""
    key = os.getenv("TWELVEDATA_KEY", "").strip()
    if not key:
        _mark(HEALTH, "twelvedata.tape", False, "TWELVEDATA_KEY not set")
        return None
    try:
        import requests
        syms = ",".join(sorted(set(TICKERS.values())))
        r = requests.get(_TD_QUOTE_URL,
                         params={"symbol": syms, "apikey": key}, timeout=10)
        r.raise_for_status()
        payload = r.json()
        # a single-symbol response is the bare quote object, not keyed by symbol
        if isinstance(payload, dict) and "symbol" in payload:
            payload = {payload["symbol"]: payload}
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        _mark(HEALTH, "twelvedata.tape", False, f"{type(e).__name__}: {e}")
        return None


# ---- free fallback source (no key): Yahoo v8/chart --------------------------
# Fills any tape name Twelve Data didn't return — including the WHOLE tape when
# TWELVEDATA_KEY is unset. Unofficial endpoint, so every failure degrades that
# one chip to n/a, never raises. GIFT Nifty has no Yahoo ticker (it is an
# NSE-IX product) so it stays n/a here — needs a paid/specialised feed.
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_YAHOO_MAP = {
    "spx": "^GSPC", "nasdaq": "^IXIC", "spx_fut": "ES=F", "nasdaq_fut": "NQ=F",
    "dow_fut": "YM=F", "usdinr": "INR=X", "crude_brent": "BZ=F",
    "crude_wti": "CL=F", "gold": "GC=F", "nikkei": "^N225", "hangseng": "^HSI",
    "us10y": "^TNX", "dxy": "DX-Y.NYB",
}


def _yahoo_one(name: str, sym: str):
    """One Yahoo /chart quote -> (name, {price, chg_pct, asof}) | (name, None)."""
    import requests
    try:
        r = requests.get(_YAHOO_CHART + sym,
                         params={"range": "2d", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        px = _num(meta.get("regularMarketPrice"))
        if px is None:
            return name, None
        prev = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
        chg = round((px - prev) / prev * 100.0, 2) if prev else None
        ts = meta.get("regularMarketTime")
        asof = (datetime.fromtimestamp(ts, timezone.utc).isoformat()
                if isinstance(ts, (int, float)) else None)
        return name, {"price": px, "chg_pct": chg, "asof": asof}
    except Exception:
        return name, None


def _yahoo_fetch(names) -> dict:
    """{name: Quote} for the requested tape names Yahoo can serve (failed/absent
    ones omitted). Concurrent, short-timeout, fully guarded."""
    wanted = [(n, _YAHOO_MAP[n]) for n in names if n in _YAHOO_MAP]
    if not wanted:
        return {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="ytape") as ex:
            futs = [ex.submit(_yahoo_one, n, s) for n, s in wanted]
            for f in as_completed(futs):
                try:
                    n, q = f.result()
                    if q:
                        out[n] = q
                except Exception:
                    pass
    except Exception:
        pass
    _mark(HEALTH, "yahoo.tape", bool(out),
          None if out else "no symbol returned data")
    return out


def fetch_tape(data=None) -> dict:
    """{name: Quote|None}. Primary source Twelve Data (needs TWELVEDATA_KEY);
    any gap — including the whole tape when the key is unset — is filled from
    the free Yahoo fallback. `data` = injected TD fixture for tests (which skips
    the network AND the fallback, so tests stay offline/deterministic)."""
    live = data is None
    if data is None:
        data = _td_fetch()
    tape = ({name: _quote_from_td(data.get(sym)) for name, sym in TICKERS.items()}
            if data else {name: None for name in TICKERS})
    td_got = sum(1 for q in tape.values() if q is not None)
    if data:                              # TD returned a payload -> its coverage
        if td_got:
            _mark(HEALTH, "twelvedata.tape", True)
            if td_got < len(TICKERS):
                HEALTH["twelvedata.tape"]["error"] = \
                    f"{len(TICKERS) - td_got}/{len(TICKERS)} symbols empty"
        else:
            _mark(HEALTH, "twelvedata.tape", False, "no symbol returned data")
    if live:                              # real call -> fill gaps from free Yahoo
        missing = {n for n, q in tape.items() if q is None}
        for n, q in (_yahoo_fetch(missing) if missing else {}).items():
            if q:
                tape[n] = q
    return tape


# ----------------------------------------------------------------- fii/dii
def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_fii_dii(rows) -> dict | None:
    """NSE fiidiiTradeReact payload -> {date, fii_net_cr, dii_net_cr}|None."""
    if isinstance(rows, dict):
        rows = rows.get("data") if isinstance(rows.get("data"), list) \
            else [rows]
    if not isinstance(rows, list):
        return None
    fii = dii = date = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category", "")).upper()
        net = _num(row.get("netValue"))
        if net is None:
            buy, sell = _num(row.get("buyValue")), _num(row.get("sellValue"))
            if buy is not None and sell is not None:
                net = buy - sell
        if "FII" in cat or "FPI" in cat:
            fii = net
        elif "DII" in cat:
            dii = net
        date = row.get("date") or date
    if fii is None and dii is None:
        return None
    return {"date": date, "fii_net_cr": fii, "dii_net_cr": dii}


def fii_dii(fixture=None) -> dict | None:
    """Best-effort NSE FII/DII net flows (Rs cr). Geo/bot-block -> None."""
    if fixture is not None:
        return normalize_fii_dii(fixture)
    try:
        import requests
        s = requests.Session()
        s.headers.update(_BROWSER_HEADERS)
        s.get(_NSE_HOME, timeout=8)          # cookie warmup
        r = s.get(_NSE_FIIDII, timeout=8)
        r.raise_for_status()
        out = normalize_fii_dii(r.json())
        if out is None:
            _mark(HEALTH, "nse.fii_dii", False, "unrecognized payload")
        else:
            _mark(HEALTH, "nse.fii_dii", True)
        return out
    except Exception as e:
        _mark(HEALTH, "nse.fii_dii", False, f"{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Twelve Data /quote batch response fixture (keyed by symbol).
    resp = {
        "USD/INR": {"symbol": "USD/INR", "close": "83.50",
                    "previous_close": "83.00", "percent_change": "0.60241",
                    "datetime": "2026-07-02"},
        # percent_change absent -> derived from previous_close (+2.5%)
        "BRENT": {"symbol": "BRENT", "close": "82.0",
                  "previous_close": "80.0", "datetime": "2026-07-02"},
        "XAU/USD": {"symbol": "XAU/USD", "close": "2400.0",
                    "percent_change": "0.0", "datetime": "2026-07-02"},
        # per-symbol error object -> None for that chip only
        "SPX": {"symbol": "SPX", "status": "error",
                "message": "symbol not available on your plan"},
    }
    tape = fetch_tape(data=resp)
    assert set(tape) == set(TICKERS)
    q = tape["usdinr"]
    assert q is not None and abs(q["price"] - 83.5) < 1e-9
    assert abs(q["chg_pct"] - 0.602) < 1e-3
    assert q["asof"].startswith("2026-07-02")
    assert abs(tape["crude_brent"]["chg_pct"] - 2.5) < 1e-9   # derived
    assert tape["gold"]["chg_pct"] == 0.0 and tape["gold"]["price"] == 2400.0
    assert tape["spx"] is None                                # per-symbol error
    assert tape["us10y"] is None and tape["dxy"] is None      # absent symbols
    assert HEALTH["twelvedata.tape"]["ok"] is True
    assert "symbols empty" in (HEALTH["twelvedata.tape"]["error"] or "")

    # empty / missing payload -> all None, no crash
    assert all(v is None for v in fetch_tape(data={}).values())

    # fii/dii normalization from the documented NSE shape
    fixture = [
        {"category": "DII **", "date": "03-Jul-2026",
         "buyValue": "12,345.67", "sellValue": "11,000.00",
         "netValue": "1,345.67"},
        {"category": "FII/FPI *", "date": "03-Jul-2026",
         "buyValue": "9,876.54", "sellValue": "11,111.11",
         "netValue": "-1,234.57"},
    ]
    out = fii_dii(fixture=fixture)
    assert out == {"date": "03-Jul-2026", "fii_net_cr": -1234.57,
                   "dii_net_cr": 1345.67}

    # netValue missing -> derived from buy - sell
    out2 = fii_dii(fixture=[{"category": "FII", "date": "02-Jul-2026",
                             "buyValue": "100", "sellValue": "40"}])
    assert out2["fii_net_cr"] == 60.0 and out2["dii_net_cr"] is None

    # wrapped payload + garbage -> handled / None
    assert fii_dii(fixture={"data": fixture})["dii_net_cr"] == 1345.67
    assert fii_dii(fixture=[]) is None
    assert fii_dii(fixture="totally wrong") is None
    assert fii_dii(fixture=[{"category": "OTHER", "netValue": "5"}]) is None

    print("markets self-test OK")

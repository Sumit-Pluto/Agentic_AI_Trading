"""Per-symbol, per-strike OI/volume history from the NSE UDiFF F&O bhavcopy.

Free end-of-day source used by the strategist for 20-session baselines
(volume averages, OI series for z-scores and rollover detection).

Data flow
    fetch(symbol)  -> downloads any missing daily bhavcopy zips (weekends
                      skipped, holidays tolerated as 404), caches raw zips
                      under  state/bhavcopy/  and maintains a per-symbol
                      extract  state/oi_history/{SYMBOL}.csv  with columns:
                      date, expiry, strike, opt_type, oi, chg_oi, volume, close
    get_history()  -> reads the cached extract only (no network)
    baselines()    -> per-(strike, opt_type) dict for the FRONT expiry:
                      {vol_avg20, oi_series, vol_series, sessions,
                       next_oi_series}   (next_* = same strike, next expiry,
                      used for rollover exclusion)

Honesty rules (from docs/specs/strategist_rules.md):
  * NEVER raise on network failure — return whatever is cached, else None.
  * Downloads may 503/timeout outside India — a circuit breaker silently
    stops retrying for NET_BREAKER_S after the first hard failure.
  * 404 for a past date = exchange holiday — remembered in missing.json so
    it is not retried forever.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger("strategist.history")

_ROOT = Path(__file__).resolve().parent.parent
BHAVCOPY_DIR = _ROOT / "state" / "bhavcopy"
HISTORY_DIR = _ROOT / "state" / "oi_history"

URL_TMPL = ("https://nsearchives.nseindia.com/content/fo/"
            "BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")
USER_AGENT = "Mozilla/5.0"
TIMEOUT_S = 30
DEFAULT_SESSIONS = 20          # rulebook: 20-session baselines
SCAN_EXTRA_DAYS = 14           # extra weekdays scanned to absorb holidays
RETRY_MISS_S = 6 * 3600        # transient misses retried after 6h
NET_BREAKER_S = 600            # after a hard network failure, go quiet 10 min

# UDiFF columns we consume (per spec); date comes from the file name.
_NEEDED = {"TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnIntrst",
           "ChngInOpnIntrst", "TtlTradgVol", "ClsPric", "FinInstrmTp"}
_KEEP_INSTR = {"STO", "IDO"}   # stock options + index options

_net_down_until = 0.0          # module-level network circuit breaker


# ── internal helpers ────────────────────────────────────────────────────

def _set_dirs(base) -> None:
    """Redirect cache dirs (tests only)."""
    global BHAVCOPY_DIR, HISTORY_DIR
    BHAVCOPY_DIR = Path(base) / "bhavcopy"
    HISTORY_DIR = Path(base) / "oi_history"


def _zip_path(d: date) -> Path:
    return BHAVCOPY_DIR / f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"


def _weekdays_back(n: int) -> list[date]:
    """Most recent n weekdays, newest first (today included if a weekday)."""
    out, d = [], date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _missing_path() -> Path:
    return BHAVCOPY_DIR / "missing.json"


def _load_missing() -> dict:
    try:
        return json.loads(_missing_path().read_text())
    except Exception:
        return {}


def _save_missing(missing: dict) -> None:
    try:
        BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)
        _missing_path().write_text(json.dumps(missing))
    except Exception:
        pass


def _try_download(d: date, zp: Path, missing: dict) -> bool:
    """Download one day's bhavcopy zip. Never raises. Records misses."""
    global _net_down_until
    if time.time() < _net_down_until:
        return False
    ymd = d.strftime("%Y%m%d")
    rec = missing.get(ymd)
    if rec:
        if rec.get("code") == 404 and (date.today() - d).days >= 2:
            return False                      # settled exchange holiday
        if time.time() - rec.get("ts", 0) < RETRY_MISS_S:
            return False                      # too soon to retry
    req = urllib.request.Request(URL_TMPL.format(ymd=ymd),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = resp.read()
        if not data or data[:2] != b"PK":     # HTML error page, not a zip
            raise ValueError("response is not a zip archive")
        zp.parent.mkdir(parents=True, exist_ok=True)
        tmp = zp.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(zp)
        missing.pop(ymd, None)
        return True
    except urllib.error.HTTPError as e:
        missing[ymd] = {"code": int(e.code), "ts": time.time()}
        if e.code >= 500:                     # 503 outside India etc.
            _net_down_until = time.time() + NET_BREAKER_S
        log.debug("bhavcopy %s HTTP %s", ymd, e.code)
    except Exception as e:                    # URLError, timeout, bad body…
        missing[ymd] = {"code": 0, "ts": time.time()}
        _net_down_until = time.time() + NET_BREAKER_S
        log.debug("bhavcopy %s failed: %s", ymd, e)
    return False


def _parse_zip(zp: Path, symbol: str) -> pd.DataFrame | None:
    """Extract STO+IDO rows of `symbol` from a cached UDiFF zip."""
    try:
        with zipfile.ZipFile(zp) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                return None
            with z.open(names[0]) as fh:
                raw = pd.read_csv(fh, dtype=str,
                                  usecols=lambda c: c.strip() in _NEEDED)
    except Exception as e:
        log.debug("parse %s failed: %s", zp.name, e)
        return None
    raw.columns = [c.strip() for c in raw.columns]
    if not _NEEDED.issubset(raw.columns):
        return None
    mask = (raw["FinInstrmTp"].str.strip().isin(_KEEP_INSTR)
            & (raw["TckrSymb"].str.strip() == symbol))
    raw = raw[mask]
    if raw.empty:
        return pd.DataFrame(columns=["expiry", "strike", "opt_type",
                                     "oi", "chg_oi", "volume", "close"])
    out = pd.DataFrame({
        "expiry": pd.to_datetime(raw["XpryDt"].str.strip(),
                                 errors="coerce").dt.strftime("%Y-%m-%d"),
        "strike": pd.to_numeric(raw["StrkPric"], errors="coerce"),
        "opt_type": raw["OptnTp"].str.strip().str.upper(),
        "oi": pd.to_numeric(raw["OpnIntrst"], errors="coerce"),
        "chg_oi": pd.to_numeric(raw["ChngInOpnIntrst"], errors="coerce"),
        "volume": pd.to_numeric(raw["TtlTradgVol"], errors="coerce"),
        "close": pd.to_numeric(raw["ClsPric"], errors="coerce"),
    })
    out = out.dropna(subset=["expiry", "strike", "oi"])
    return out[out["opt_type"].isin(["CE", "PE"])]


def _extract_path(symbol: str) -> Path:
    return HISTORY_DIR / f"{symbol.upper()}.csv"


def _update_extract(symbol: str, have: list[tuple[date, Path]]) -> None:
    """Append rows for any cached day not yet in the per-symbol extract."""
    p = _extract_path(symbol)
    old, have_dates = None, set()
    if p.exists():
        try:
            old = pd.read_csv(p, dtype={"date": str, "expiry": str})
            have_dates = set(old["date"].astype(str))
        except Exception:
            old, have_dates = None, set()
    frames = [old] if old is not None and not old.empty else []
    added = False
    for d, zp in have:
        ds = d.strftime("%Y-%m-%d")
        if ds in have_dates:
            continue
        part = _parse_zip(zp, symbol)
        if part is None or part.empty:
            continue
        part.insert(0, "date", ds)
        frames.append(part)
        added = True
    if not frames or (not added and old is not None):
        return
    df = pd.concat(frames, ignore_index=True)
    df = (df.drop_duplicates(["date", "expiry", "strike", "opt_type"],
                             keep="last")
            .sort_values(["date", "expiry", "strike", "opt_type"]))
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)


# ── public API ──────────────────────────────────────────────────────────

def fetch(symbol: str, sessions: int = DEFAULT_SESSIONS,
          download: bool = True) -> pd.DataFrame | None:
    """Ensure up to `sessions` recent bhavcopy days are cached + extracted
    for `symbol`; return the history DataFrame (cached data on any failure,
    None if nothing is available). Never raises."""
    try:
        BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        missing = _load_missing()
        have: list[tuple[date, Path]] = []
        for d in _weekdays_back(sessions * 2 + SCAN_EXTRA_DAYS):
            zp = _zip_path(d)
            if not zp.exists() and download:
                _try_download(d, zp, missing)
            if zp.exists():
                have.append((d, zp))
                if len(have) >= sessions:
                    break
        _save_missing(missing)
        _update_extract(symbol, have)
    except Exception as e:                    # belt & braces: never raise
        log.debug("fetch(%s) degraded: %s", symbol, e)
    return get_history(symbol)


def get_history(symbol: str) -> pd.DataFrame | None:
    """Read the cached per-symbol extract. No network. None if absent."""
    try:
        p = _extract_path(symbol)
        if not p.exists():
            return None
        df = pd.read_csv(p, dtype={"date": str, "expiry": str, "opt_type": str})
        if df.empty:
            return None
        for col in ("strike", "oi", "chg_oi", "volume", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["strike", "oi"])
        if df.empty:
            return None
        return (df.sort_values(["date", "expiry", "strike", "opt_type"])
                  .reset_index(drop=True))
    except Exception as e:
        log.debug("get_history(%s) failed: %s", symbol, e)
        return None


def baselines(symbol: str,
              sessions: int = DEFAULT_SESSIONS) -> dict | None:
    """Per-(strike, opt_type) baselines for the FRONT expiry:

        {(strike, "CE"/"PE"): {"vol_avg20": float|None,
                               "oi_series":  pd.Series (date-indexed),
                               "vol_series": pd.Series,
                               "sessions":   int,
                               "next_oi_series": pd.Series|None}}

    next_oi_series = same strike/type in the NEXT expiry (rollover checks).
    Returns None when no usable history is cached."""
    try:
        df = get_history(symbol)
        if df is None or df.empty:
            return None
        last_date = df["date"].max()
        expiries = sorted(df["expiry"].dropna().unique())
        live = [e for e in expiries if e >= last_date]
        front = live[0] if live else expiries[-1]
        nxt = live[1] if len(live) > 1 else None

        out: dict = {}
        fdf = df[df["expiry"] == front]
        for (k, ot), g in fdf.groupby(["strike", "opt_type"]):
            g = g.sort_values("date").drop_duplicates("date", keep="last")
            oi_s = pd.Series(g["oi"].to_numpy(dtype=float),
                             index=list(g["date"]))
            vol_s = pd.Series(g["volume"].to_numpy(dtype=float),
                              index=list(g["date"]))
            tail = vol_s.tail(sessions)
            out[(round(float(k), 2), str(ot))] = {
                "vol_avg20": float(tail.mean()) if len(tail) else None,
                "oi_series": oi_s,
                "vol_series": vol_s,
                "sessions": int(len(oi_s)),
                "next_oi_series": None,
            }
        if nxt:
            ndf = df[df["expiry"] == nxt]
            for (k, ot), g in ndf.groupby(["strike", "opt_type"]):
                key = (round(float(k), 2), str(ot))
                if key not in out:
                    continue                 # rollover only matters vs front
                g = g.sort_values("date").drop_duplicates("date", keep="last")
                out[key]["next_oi_series"] = pd.Series(
                    g["oi"].to_numpy(dtype=float), index=list(g["date"]))
        return out or None
    except Exception as e:
        log.debug("baselines(%s) failed: %s", symbol, e)
        return None


# ── self-test (synthetic zips; no network) ──────────────────────────────

if __name__ == "__main__":
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="hist_selftest_"))
    _set_dirs(base)
    BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)

    # 12 synthetic weekday bhavcopies ending yesterday-ish
    days = [d for d in _weekdays_back(13)][1:13]      # skip today
    days.reverse()                                     # oldest first
    for di, d in enumerate(days):
        rows = []
        for exp in ("2026-07-30", "2026-08-27"):
            for k in (960, 980, 1000, 1020, 1040):
                for ot in ("CE", "PE"):
                    base_oi = 5000 + (k - 960) // 20 * 300 + (500 if ot == "PE" else 0)
                    grow = di * 100 if exp == "2026-07-30" else di * 250
                    rows.append({
                        "TradDt": d.strftime("%Y-%m-%d"), "FinInstrmTp": "STO",
                        "TckrSymb": "TESTSYM", "XpryDt": exp, "StrkPric": k,
                        "OptnTp": ot, "OpnIntrst": base_oi + grow,
                        "ChngInOpnIntrst": 100 + di,
                        "TtlTradgVol": 1500 + di * 13 + (k % 100),
                        "ClsPric": 25.5 + di * 0.1})
        # noise rows that MUST be filtered out
        rows.append({"TradDt": d.strftime("%Y-%m-%d"), "FinInstrmTp": "STF",
                     "TckrSymb": "TESTSYM", "XpryDt": "2026-07-30",
                     "StrkPric": "", "OptnTp": "", "OpnIntrst": 99999,
                     "ChngInOpnIntrst": 0, "TtlTradgVol": 5, "ClsPric": 1000})
        rows.append({"TradDt": d.strftime("%Y-%m-%d"), "FinInstrmTp": "STO",
                     "TckrSymb": "OTHER", "XpryDt": "2026-07-30",
                     "StrkPric": 500, "OptnTp": "CE", "OpnIntrst": 777,
                     "ChngInOpnIntrst": 1, "TtlTradgVol": 10, "ClsPric": 9})
        csv_text = pd.DataFrame(rows).to_csv(index=False)
        zp = _zip_path(d)
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(zp.name[:-4], csv_text)

    df = fetch("TESTSYM", sessions=10, download=False)
    assert df is not None and len(df) == 10 * 20, f"rows={0 if df is None else len(df)}"
    assert set(df.columns) == {"date", "expiry", "strike", "opt_type",
                               "oi", "chg_oi", "volume", "close"}
    assert not (df["oi"] == 99999).any() and not (df["strike"] == 500).any()

    bl = baselines("TESTSYM")
    key = (1000.0, "CE")
    assert bl is not None and key in bl
    assert bl[key]["sessions"] == 10
    assert bl[key]["vol_avg20"] and bl[key]["vol_avg20"] > 0
    assert bl[key]["next_oi_series"] is not None
    assert len(bl[key]["next_oi_series"]) == 10
    assert float(bl[key]["oi_series"].iloc[-1]) > float(bl[key]["oi_series"].iloc[0])

    # unknown symbol → honest None, never an exception
    assert get_history("NOSUCHSYM") is None and baselines("NOSUCHSYM") is None

    # network breaker: fetch with downloads "failing" degrades to None
    _net_down_until = time.time() + 999
    assert fetch("TESTSYM2", sessions=2, download=True) is None

    # idempotent second fetch (no re-parse, same result)
    df2 = fetch("TESTSYM", sessions=10, download=False)
    assert df2 is not None and len(df2) == len(df)

    print(f"history self-test OK — {len(df)} rows, "
          f"{len(bl)} baseline legs, vol_avg20[1000CE]={bl[key]['vol_avg20']:.0f}")

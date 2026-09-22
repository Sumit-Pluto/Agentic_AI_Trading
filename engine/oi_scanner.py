"""engine/oi_scanner.py — fast OI-buildup + option-wall scanner (decision #1).

A "hit" = a fresh futures Open-Interest buildup (the price×OI matrix) CONFIRMED
by option-wall structure:

  price↑ + OI↑  → long buildup      (BUY)     — new longs
  price↓ + OI↑  → short buildup     (SELL)    — new shorts
  price↑ + OI↓  → short covering    (BUY)     — shorts exiting
  price↓ + OI↓  → long unwinding    (SELL)    — longs exiting

Confirmation (walls, from the option chain): a BUY buildup must be holding above
the put wall (support) with room below the call wall; a SELL buildup must be
capped below the call wall (resistance) with room above the put wall.

Concurrent per-symbol scan over the F&O + MCX universe; ranked hits persisted to
state/oi_signals.json for the UI + "save to watchlist". Runs on its own cadence.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

log = logging.getLogger("oi")

STORE = os.path.join("state", "oi_signals.json")

# (price_up, oi_up) -> (label, direction)
BUILDUP = {
    (True, True):  ("long_buildup", "BUY"),
    (False, True): ("short_buildup", "SELL"),
    (True, False): ("short_covering", "BUY"),
    (False, False): ("long_unwinding", "SELL"),
}


def _session_change(df):
    """(px0, px1, oi0, oi1) over the latest session (first vs last bar)."""
    try:
        dates = df.index.normalize()
        sess = df[dates == dates[-1]]
        if len(sess) < 2:
            sess = df
    except Exception:
        sess = df
    return (float(sess["close"].iloc[0]), float(sess["close"].iloc[-1]),
            float(sess["oi"].iloc[0]), float(sess["oi"].iloc[-1]))


def _walls(chain):
    """(put_wall, call_wall) = strikes with the most PE / CE open interest."""
    put_wall = call_wall = None
    put_oi = call_oi = 0.0
    for r in chain.strikes:
        if r.pe and (r.pe.oi or 0) > put_oi:
            put_oi, put_wall = r.pe.oi, float(r.strike)
        if r.ce and (r.ce.oi or 0) > call_oi:
            call_oi, call_wall = r.ce.oi, float(r.strike)
    return put_wall, call_wall


def _confirmed(direction, price, put_wall, call_wall) -> bool:
    if direction == "BUY":
        return (put_wall is not None and price >= put_wall
                and (call_wall is None or price <= call_wall))
    return (call_wall is not None and price <= call_wall
            and (put_wall is None or price >= put_wall))


class OIScanner:
    def __init__(self, hub, interval_sec: int | None = None, workers: int = 8):
        self.hub = hub
        self.interval_sec = interval_sec or int(os.getenv("OI_SCAN_SECONDS", "180"))
        self.workers = max(1, int(os.getenv("OI_WORKERS", str(workers))))
        self.min_oi_chg = float(os.getenv("OI_MIN_OI_CHG", "2.0"))    # %
        self.min_px_chg = float(os.getenv("OI_MIN_PX_CHG", "0.3"))    # %
        self.signals: list[dict] = []
        self.last_scan: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load()

    def universe(self) -> list[str]:
        try:
            u = self.hub.fo_universe() + self.hub.mcx_universe()
        except Exception:
            u = []
        return list(dict.fromkeys(u))

    def _scan_symbol(self, sym: str) -> dict | None:
        df = self.hub.futures_candles(sym)
        if df is None or len(df) < 4 or "oi" not in df.columns:
            return None
        px0, px1, oi0, oi1 = _session_change(df)
        if px0 <= 0 or oi0 <= 0:
            return None
        px_chg = (px1 - px0) / px0 * 100.0
        oi_chg = (oi1 - oi0) / oi0 * 100.0
        if abs(oi_chg) < self.min_oi_chg or abs(px_chg) < self.min_px_chg:
            return None
        kind, direction = BUILDUP[(px_chg > 0, oi_chg > 0)]

        put_wall = call_wall = None
        try:
            chain = self.hub.chain_snapshot(sym)
            if chain and chain.strikes:
                put_wall, call_wall = _walls(chain)
        except Exception:
            pass
        if not _confirmed(direction, px1, put_wall, call_wall):
            return None      # buildup not confirmed by wall structure

        return {"symbol": sym, "segment": self.hub.segment_of(sym),
                "strategy": "oi", "kind": kind, "direction": direction,
                "price": round(px1, 2), "px_chg_pct": round(px_chg, 2),
                "oi_chg_pct": round(oi_chg, 2), "put_wall": put_wall,
                "call_wall": call_wall, "confirmed": True,
                "strength": round(abs(oi_chg) + abs(px_chg), 2),
                "detail": (f"{kind} px{px_chg:+.1f}% oi{oi_chg:+.1f}% "
                           f"| PW={put_wall} CW={call_wall} ✓")}

    def sweep(self) -> int:
        syms = self.universe()
        out: list[dict] = []
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="oi") as ex:
            futs = {ex.submit(self._scan_symbol, s): s for s in syms}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    out.append(r)
        out.sort(key=lambda h: h.get("strength", 0.0), reverse=True)
        with self._lock:
            self.signals = out
            self.last_scan = datetime.now().isoformat(timespec="seconds")
            self._save()
        log.info("OI sweep: %d confirmed buildups across %d symbols",
                 len(out), len(syms))
        return len(out)

    def snapshot(self) -> dict:
        with self._lock:
            return {"last_scan": self.last_scan, "count": len(self.signals),
                    "signals": list(self.signals)}

    def start(self) -> None:
        threading.Thread(target=self._run, name="oi-scanner",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as e:
                log.warning("OI sweep error: %s", e)
            if self._stop.wait(self.interval_sec):
                break

    def _save(self) -> None:
        try:
            os.makedirs("state", exist_ok=True)
            tmp = STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"last_scan": self.last_scan,
                           "signals": self.signals}, f)
            os.replace(tmp, STORE)
        except Exception:
            pass

    def _load(self) -> None:
        try:
            with open(STORE) as f:
                d = json.load(f)
            self.signals = d.get("signals", [])
            self.last_scan = d.get("last_scan")
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    # Offline smoke test — stub hub with a price×OI matrix + option chain; no
    # network. Asserts buildup classification, MCX segment routing, and that
    # only WALL-CONFIRMED, above-threshold hits are emitted.
    import sys
    import tempfile
    from types import SimpleNamespace as NS

    import numpy as np
    import pandas as pd

    STORE = os.path.join(tempfile.mkdtemp(), "oi_signals.json")

    def _fut(px0, px1, oi0, oi1):
        idx = pd.date_range("2026-07-10 09:15", periods=6, freq="5min")
        close, oi = np.linspace(px0, px1, 6), np.linspace(oi0, oi1, 6)
        return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                             "close": close, "volume": 1000.0, "oi": oi}, index=idx)

    def _chain(spot, pw, cw):
        rows = [NS(strike=float(k),
                   pe=NS(oi=90000 if k == pw else 1000),
                   ce=NS(oi=90000 if k == cw else 1000)) for k in (pw, spot, cw)]
        return NS(strikes=rows)

    class StubHub:
        DATA = {  # sym: (px0, px1, oi0, oi1, put_wall, call_wall)
            "GOODBUY":  (2900.0, 2960.0, 100000, 108000, 2900.0, 3050.0),  # confirmed
            "LEAKBUY":  (500.0,  512.0,  100000, 106000, 520.0,  560.0),   # below PW -> unconfirmed
            "CRUDEOIL": (6800.0, 6870.0, 50000,  53000,  6800.0, 6950.0),  # MCX, confirmed
            "FLATSYM":  (100.0,  100.1,  100000, 100050, 95.0,   105.0),   # sub-threshold
        }
        fo_universe = lambda self: ["GOODBUY", "LEAKBUY", "FLATSYM"]
        mcx_universe = lambda self: ["CRUDEOIL"]
        futures_candles = lambda self, s: _fut(*self.DATA[s][:4]) if s in self.DATA else None
        chain_snapshot = lambda self, s: _chain((self.DATA[s][0] + self.DATA[s][1]) / 2,
                                                self.DATA[s][4], self.DATA[s][5])
        segment_of = lambda self, s: "MCX" if s == "CRUDEOIL" else "FNO"

    sc = OIScanner(StubHub())
    sc.sweep()
    hits = {h["symbol"]: h for h in sc.snapshot()["signals"]}
    assert hits.get("GOODBUY", {}).get("kind") == "long_buildup", "confirmed BUY buildup"
    assert hits.get("GOODBUY", {}).get("direction") == "BUY"
    assert hits.get("CRUDEOIL", {}).get("segment") == "MCX", "MCX segment routed"
    assert "LEAKBUY" not in hits, "wall structure must reject unconfirmed buildup"
    assert "FLATSYM" not in hits, "sub-threshold px/oi move must be ignored"
    print("oi_scanner smoke test: OK (buildup classified; MCX segment routed; "
          "wall-confirmation filters unconfirmed + sub-threshold)")
    sys.exit(0)

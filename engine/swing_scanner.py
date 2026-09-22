"""engine/swing_scanner.py — Rbknox + Order-Block swing scanner.

Scans the combined universe (F&O + Cash + MCX) on daily AND intraday (60m/15m)
timeframes, runs the Rbknox-at-OB fusion (signals.swing.swing_signal) on each,
ranks the hits, and persists them to state/swing_signals.json for the UI +
alerts. Runs on its own slow cadence on a background thread — independent of the
fast intraday sweep.

Data comes from Shoonya via DataHub (segment-aware daily_candles / candles), so
this works for Cash equities, F&O underlyings and MCX commodities alike.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from signals.swing import swing_signal
from signals.swing.knoxville import KnoxParams, min_bars_required

log = logging.getLogger("swing")

STORE = os.path.join("state", "swing_signals.json")
PARAMS_STORE = os.path.join("state", "swing_params.json")

# user-editable Rbknox (Knoxville Divergence) fields -> (type, min, max).
# Bounds keep edits sane; unknown keys are ignored.
_KNOX_FIELDS = {
    "lookback": (int, 5, 400), "min_bars": (int, 1, 100),
    "mom_len": (int, 2, 200), "rsi_len": (int, 2, 200),
    "rsi_upper": (int, 50, 100), "rsi_lower": (int, 0, 50),
    "use_stoch": (bool, None, None), "stoch_win": (int, 1, 50),
    "st_rsi_len": (int, 2, 200), "st_len": (int, 2, 200),
    "st_ksm": (int, 1, 50), "st_dsm": (int, 1, 50),
}
# (interval, lookback-days) timeframes to scan. "1d" -> daily_candles (EOD),
# numeric -> intraday TPSeries minutes. Daily fetch is wide enough to cover the
# Rbknox divergence lookback (default 200 bars -> needs ~1yr+ of daily history).
TIMEFRAMES = [("1d", 500), ("60", 60), ("15", 20)]


class SwingScanner:
    def __init__(self, hub, interval_sec: int | None = None, workers: int = 8):
        self.hub = hub
        self.interval_sec = interval_sec or int(
            os.getenv("SWING_SCAN_SECONDS", "300"))
        self.workers = max(1, int(os.getenv("SWING_WORKERS", str(workers))))
        self.signals: list[dict] = []
        self.last_scan: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.knox = KnoxParams()           # Rbknox params (user-editable via UI)
        self._load_params()
        self._load()

    # ── universe ─────────────────────────────────────────────────────────
    def universe(self) -> list[str]:
        try:
            u = (self.hub.fo_universe() + self.hub.cash_universe()
                 + self.hub.mcx_universe())
        except Exception:
            u = []
        return list(dict.fromkeys(u))          # dedupe, preserve order

    # ── scan ─────────────────────────────────────────────────────────────
    def _candles(self, sym: str, seg: str, interval: str, days: int):
        if interval == "1d":
            return self.hub.daily_candles(sym, days=days, segment=seg)
        return self.hub.candles(sym, interval=interval, days=days, segment=seg)

    def _scan_symbol(self, sym: str) -> list[dict]:
        seg = self.hub.segment_of(sym)
        need = min_bars_required(self.knox)     # adapts to the current params
        hits: list[dict] = []
        for interval, days in TIMEFRAMES:
            try:
                df = self._candles(sym, seg, interval, days)
                if df is None or len(df) < need:
                    continue
                label = interval if interval == "1d" else interval + "m"
                sig = swing_signal(df, symbol=sym, segment=seg, interval=label,
                                   knox_params=self.knox)
                if sig is not None:
                    hits.append(sig.to_dict())
            except Exception as e:
                log.debug("swing %s %s failed: %s", sym, interval, e)
        return hits

    def sweep(self) -> int:
        syms = self.universe()
        out: list[dict] = []
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="swing") as ex:
            futs = {ex.submit(self._scan_symbol, s): s for s in syms}
            for fut in as_completed(futs):
                try:
                    out.extend(fut.result())
                except Exception:
                    pass
        # rank: strongest reward:risk, then biggest displacement
        out.sort(key=lambda h: ((h.get("rr") or 0.0), (h.get("move_pct") or 0.0)),
                 reverse=True)
        with self._lock:
            self.signals = out
            self.last_scan = datetime.now().isoformat(timespec="seconds")
            self._save()
        log.info("swing sweep: %d signals across %d symbols", len(out), len(syms))
        return len(out)

    def snapshot(self) -> dict:
        with self._lock:
            return {"last_scan": self.last_scan, "count": len(self.signals),
                    "signals": list(self.signals)}

    # ── Rbknox params (user-editable) ────────────────────────────────────
    def get_params(self) -> dict:
        """Current Rbknox params + the editable-field spec (bounds) for the UI."""
        p = dataclasses.asdict(self.knox)
        spec = {k: {"type": ("bool" if t is bool else "int"),
                    "min": lo, "max": hi}
                for k, (t, lo, hi) in _KNOX_FIELDS.items()}
        return {"params": p, "spec": spec,
                "defaults": dataclasses.asdict(KnoxParams())}

    def set_params(self, updates: dict) -> dict:
        """Validate + apply Rbknox edits (clamped to bounds), persist, and
        return the new params. Ignores unknown keys and bad values."""
        cur = dataclasses.asdict(self.knox)
        for k, v in (updates or {}).items():
            spec = _KNOX_FIELDS.get(k)
            if spec is None:
                continue
            typ, lo, hi = spec
            try:
                if typ is bool:
                    val = (v.strip().lower() in ("1", "true", "yes", "on")
                           if isinstance(v, str) else bool(v))
                else:
                    val = int(float(v))
                    val = max(lo, min(hi, val))
            except (ValueError, TypeError):
                continue
            cur[k] = val
        # keep the RSI band coherent (lower strictly below upper)
        if cur["rsi_lower"] >= cur["rsi_upper"]:
            cur["rsi_lower"] = max(0, cur["rsi_upper"] - 1)
        with self._lock:
            self.knox = KnoxParams(**cur)
            self._save_params()
        return cur

    def _save_params(self) -> None:
        try:
            os.makedirs("state", exist_ok=True)
            tmp = PARAMS_STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dataclasses.asdict(self.knox), f, indent=2)
            os.replace(tmp, PARAMS_STORE)
        except Exception as e:
            log.debug("swing params save failed: %s", e)

    def _load_params(self) -> None:
        try:
            with open(PARAMS_STORE) as f:
                d = json.load(f)
            names = {f.name for f in dataclasses.fields(KnoxParams)}
            self.knox = KnoxParams(**{k: v for k, v in d.items() if k in names})
        except (OSError, ValueError, TypeError):
            self.knox = KnoxParams()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        threading.Thread(target=self._run, name="swing-scanner",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as e:
                log.warning("swing sweep error: %s", e)
            if self._stop.wait(self.interval_sec):
                break

    # ── persistence (atomic write) ───────────────────────────────────────
    def _save(self) -> None:
        try:
            os.makedirs("state", exist_ok=True)
            tmp = STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"last_scan": self.last_scan,
                           "signals": self.signals}, f)
            os.replace(tmp, STORE)
        except Exception as e:
            log.debug("swing save failed: %s", e)

    def _load(self) -> None:
        try:
            with open(STORE) as f:
                d = json.load(f)
            self.signals = d.get("signals", [])
            self.last_scan = d.get("last_scan")
        except (OSError, ValueError):
            pass

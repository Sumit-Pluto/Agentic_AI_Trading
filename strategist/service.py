"""strategist/service.py — StrategyAdvisor: the full per-symbol report.

analyze(symbol) wires everything together:

  DataHub.chain_snapshot(symbol, span=10)  -> live chain (+ IVs)
  strategist.history (if present)          -> bhavcopy sessions / baselines
  strategist.metrics (if present)          -> metric table (else a small
                                              inline fallback computed from
                                              the chain, honestly labelled)
  strategist.footprints (if present)       -> footprint templates
  state/iv_store/{symbol}.json (if present)-> ATM IV percentile
  view.build_view -> recommend.recommend   -> generated + benchmark menu

The sibling metrics/footprints/history modules are developed independently;
their entry points and signatures are resolved defensively (multiple
candidate names, keyword matching via inspect). Anything missing degrades
to an honest "insufficient data" field — never fabricated.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from quant.mathutils import years_to_expiry
except ModuleNotFoundError:                      # direct-script execution
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from quant.mathutils import years_to_expiry

from strategist.recommend import recommend
from strategist.view import build_view, chain_atm_iv

CHAIN_SPAN = 10                     # strategist wants a wider window
IV_STORE_DIR = Path(__file__).resolve().parents[1] / "state" / "iv_store"
MIN_IV_SESSIONS = 5                 # percentile needs at least this history
IST_OFFSET_S = int(5.5 * 3600)
PROVISIONAL_AFTER = 15 * 60         # 15:00 IST (minutes)
PROVISIONAL_BEFORE = 9 * 60 + 45    # 09:45 IST (minutes)

_METRIC_FNS = ("compute_metrics", "compute", "build_metrics", "build",
               "metrics", "analyze", "run")
_FOOTPRINT_FNS = ("detect_footprints", "detect", "find_footprints",
                  "build_footprints", "footprints", "scan", "analyze")
_HISTORY_GET_FNS = ("get_history", "get", "load", "history", "fetch")
_BASELINE_FNS = ("baselines", "get_baselines", "baseline")


# ── defensive glue for independently-developed sibling modules ─────────
def _module(name: str):
    try:
        return importlib.import_module(f"strategist.{name}")
    except Exception:
        return None


def _flex_call(fn, pool: dict):
    """Call fn feeding only the kwargs its signature asks for (None values
    included — sibling modules are None-tolerant by contract); None when a
    required parameter cannot be named or the call raises."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        try:
            return fn(pool.get("chain"))
        except Exception:
            return None
    kwargs = {}
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if pname in pool:
            kwargs[pname] = pool[pname]
        elif p.default is p.empty:
            return None
    try:
        return fn(**kwargs)
    except Exception:
        return None


def _call_first(mod, names: tuple, pool: dict):
    if mod is None:
        return None
    for n in names:
        fn = getattr(mod, n, None)
        if callable(fn):
            res = _flex_call(fn, pool)
            if res is not None:
                return res
    return None


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    td = getattr(obj, "to_dict", None)
    if callable(td):
        try:
            d = td()
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return dataclasses.asdict(obj)
        except Exception:
            pass
    return {"repr": str(obj)}


def _json_safe(x):
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.ndarray):
        return [_json_safe(v) for v in x.tolist()]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


# ── inline fallback metrics (chain-only, per rulebook definitions) ─────
def _fallback_metrics(chain) -> dict:
    """Minimal honest metric set straight off the live chain, used only
    when the full metrics module is unavailable. Labelled as such."""
    rows = list(getattr(chain, "strikes", []) or [])
    strikes = np.array([float(r.strike) for r in rows])

    def _oi(leg):
        try:
            return float(leg.oi) if leg and leg.oi is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    ce_oi = np.array([_oi(r.ce) for r in rows])
    pe_oi = np.array([_oi(r.pe) for r in rows])
    out: dict = {"source": "inline-fallback (metrics module unavailable)",
                 "spot": float(chain.spot), "lot": int(chain.lot or 0)}
    if len(rows):
        if ce_oi.sum() > 0:
            out["call_wall"] = float(strikes[int(ce_oi.argmax())])
            top2 = np.sort(ce_oi)[-2:].sum()
            out["call_concentration"] = round(float(top2 / ce_oi.sum()), 4)
        if pe_oi.sum() > 0:
            out["put_wall"] = float(strikes[int(pe_oi.argmax())])
            top2 = np.sort(pe_oi)[-2:].sum()
            out["put_concentration"] = round(float(top2 / pe_oi.sum()), 4)
        if ce_oi.sum() > 0 and pe_oi.sum() > 0:
            out["pcr_oi"] = round(float(pe_oi.sum() / ce_oi.sum()), 4)
            # standard max-pain minimisation over listed strikes
            pain = [float((ce_oi * np.maximum(s - strikes, 0.0)).sum()
                          + (pe_oi * np.maximum(strikes - s, 0.0)).sum())
                    for s in strikes]
            out["max_pain"] = float(strikes[int(np.argmin(pain))])
    iv = chain_atm_iv(chain)
    if iv is not None:
        out["atm_iv"] = round(float(iv), 4)
    return out


# ── iv percentile from the self-recorded store ──────────────────────────
def _iv_percentile(symbol: str, current_iv) -> tuple[float | None, int]:
    if current_iv is None:
        return None, 0
    path = IV_STORE_DIR / f"{symbol}.json"
    if not path.exists():
        return None, 0
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None, 0
    ivs: list[float] = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                n = v.get("iv")
            else:
                n = v
            try:
                n = float(n)
            except (TypeError, ValueError):
                continue
            if math.isfinite(n) and n > 0:
                ivs.append(n)
    if len(ivs) < MIN_IV_SESSIONS:
        return None, len(ivs)
    pct = 100.0 * sum(1 for x in ivs if x <= float(current_iv)) / len(ivs)
    return round(pct, 1), len(ivs)


def _history_sessions(hist) -> int:
    if hist is None:
        return 0
    try:
        import pandas as pd
        if isinstance(hist, pd.DataFrame):
            for col in ("date", "session", "trade_date"):
                if col in hist.columns:
                    return int(hist[col].nunique())
            return int(len(hist))
    except Exception:
        pass
    if isinstance(hist, dict):
        for k in ("sessions", "n_sessions"):
            if isinstance(hist.get(k), int):
                return hist[k]
        return len(hist)
    try:
        return len(hist)
    except TypeError:
        return 0


def _is_provisional(now: float | None = None) -> bool:
    """Intraday OI is lagged/provisional after 15:00 and before 09:45 IST."""
    t = time.gmtime((now if now is not None else time.time()) + IST_OFFSET_S)
    minutes = t.tm_hour * 60 + t.tm_min
    return minutes >= PROVISIONAL_AFTER or minutes < PROVISIONAL_BEFORE


# ── the advisor ─────────────────────────────────────────────────────────
class StrategyAdvisor:
    def __init__(self, hub):
        self.hub = hub

    # data acquisition, each step individually shielded ------------------
    def _chain(self, symbol: str):
        try:
            return self.hub.chain_snapshot(symbol, span=CHAIN_SPAN)
        except TypeError:
            try:
                return self.hub.chain_snapshot(symbol)
            except Exception:
                return None
        except Exception:
            return None

    def _futures(self, symbol: str):
        try:
            return self.hub.futures_quote(symbol)
        except Exception:
            return None

    def analyze(self, symbol: str, profile: str = "balanced") -> dict:
        symbol = str(symbol).upper().strip()
        now = time.time()
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        provisional = _is_provisional(now)

        chain = self._chain(symbol)
        if chain is None or not getattr(chain, "strikes", None):
            return {"symbol": symbol, "error": "no usable option chain",
                    "generated_at": generated_at,
                    "data_quality": {"chain_ok": False,
                                     "history_sessions": 0,
                                     "provisional": provisional}}
        try:
            chain.compute_ivs()
        except Exception:
            pass

        futures = self._futures(symbol)

        # iv percentile from the self-recorded store (chain ATM IV as probe)
        atm_iv = chain_atm_iv(chain)
        iv_pct, iv_sessions = _iv_percentile(symbol, atm_iv)

        # history / baselines (sibling module, optional; cache-first with
        # on-demand bhavcopy fetch as fallback — both never raise)
        hist_mod = _module("history")
        pool = {"symbol": symbol, "sym": symbol, "hub": self.hub,
                "datahub": self.hub, "chain": chain, "snapshot": chain,
                "snap": chain, "chain_snapshot": chain,
                "spot": getattr(chain, "spot", None),
                "lot": getattr(chain, "lot", None),
                "futures": futures, "fut": futures,
                "iv_percentile": iv_pct, "iv_store_percentile": iv_pct}
        hist = _call_first(hist_mod, _HISTORY_GET_FNS, pool)
        pool.update({"history": hist, "hist": hist, "oi_history": hist,
                     "df": hist})
        baselines = _call_first(hist_mod, _BASELINE_FNS, pool)
        pool.update({"baselines": baselines, "base": baselines,
                     "hist_baselines": baselines})
        history_sessions = _history_sessions(hist)

        # metrics (sibling module, else honest inline fallback)
        metrics = _call_first(_module("metrics"), _METRIC_FNS, pool)
        metrics_fallback = metrics is None
        if metrics_fallback:
            try:
                metrics = _fallback_metrics(chain)
            except Exception:
                metrics = None
        if iv_pct is not None and isinstance(metrics, dict) \
                and metrics.get("iv_percentile") is None:
            metrics = {**metrics, "iv_percentile": iv_pct}
        pool["metrics"] = metrics
        ms = _as_dict(metrics).get("history_sessions")
        if isinstance(ms, int):
            history_sessions = max(history_sessions, ms)

        # footprints (sibling module, optional)
        footprints = _call_first(_module("footprints"), _FOOTPRINT_FNS, pool)
        footprints_missing = footprints is None
        footprints = list(footprints) if footprints else []

        # view + recommendations
        view = build_view(metrics, footprints, chain=chain)
        recommendations = recommend(chain, metrics, footprints, view,
                                    profile=profile)

        # report assembly, all fields honest --------------------------------
        expiry_epoch = float(getattr(chain, "expiry_epoch", 0.0) or 0.0)
        dte = round(max(0.0, (expiry_epoch - now)) / 86400.0, 1)
        metrics_field = (_as_dict(metrics) if metrics is not None else
                         {"status": "insufficient data",
                          "reason": "metrics module unavailable and inline "
                                    "fallback failed"})
        fp_field = ([_as_dict(fp) for fp in footprints] if footprints else
                    ([] if not footprints_missing else
                     [{"status": "insufficient data",
                       "reason": "footprints module unavailable"}]))

        report = {
            "symbol": symbol,
            "spot": float(chain.spot),
            "expiry": (datetime.fromtimestamp(expiry_epoch)
                       .strftime("%Y-%m-%d") if expiry_epoch else None),
            "dte": dte,
            "lot": int(getattr(chain, "lot", 0) or 0),
            "metrics": metrics_field,
            "footprints": fp_field,
            "view": view.to_dict(),
            "recommendations": recommendations,
            "generated_at": generated_at,
            "data_quality": {
                "chain_ok": True,
                "history_sessions": history_sessions,
                "provisional": provisional,
                "iv_history_sessions": iv_sessions,
                "metrics_source": ("module" if not metrics_fallback
                                   else "inline-fallback"),
            },
        }
        return _json_safe(report)


# ── self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    from strategist.view import synthetic_chain

    # keep the self-test offline: no bhavcopy downloads
    _h = _module("history")
    if _h is not None and hasattr(_h, "fetch"):
        _h.fetch = lambda *a, **k: (_h.get_history(a[0])
                                    if a and hasattr(_h, "get_history")
                                    else None)

    class FakeHub:
        """Chain for TESTSTK, nothing else — exercises every fallback."""

        def chain_snapshot(self, symbol, span=8):
            return synthetic_chain() if symbol == "TESTSTK" else None

        def futures_quote(self, symbol):
            # strings on purpose: quote dicts carry strings
            return {"lp": "1002.50", "oi": "100000", "poi": "98000",
                    "lot": "250"}

        def cash_quote(self, symbol):
            return {"lp": "1000.15"}

    advisor = StrategyAdvisor(FakeHub())
    rep = advisor.analyze("TESTSTK")
    assert rep["symbol"] == "TESTSTK" and "error" not in rep
    for key in ("spot", "expiry", "dte", "lot", "metrics", "footprints",
                "view", "recommendations", "generated_at", "data_quality"):
        assert key in rep, key
    assert rep["data_quality"]["chain_ok"] is True
    assert isinstance(rep["data_quality"]["provisional"], bool)
    assert {"bias", "vol_mult", "range_conviction", "rationale"} <= set(rep["view"])
    recs = rep["recommendations"]
    assert {"generated", "benchmarks", "disclaimer", "sizing_note"} <= set(recs)
    assert isinstance(recs["generated"], list) and len(recs["generated"]) <= 5
    # walls must surface whichever metrics source served the report
    # (sibling module shapes walls as {'strike':..., 'oi':...})
    cw = rep["metrics"].get("call_wall")
    pw = rep["metrics"].get("put_wall")
    cw = cw.get("strike") if isinstance(cw, dict) else cw
    pw = pw.get("strike") if isinstance(pw, dict) else pw
    assert cw == 1100.0 and pw == 900.0, (cw, pw)
    assert rep["metrics"].get("max_pain") is not None
    json.dumps(rep)                       # report must be JSON-serialisable

    missing = advisor.analyze("MISSING")
    assert missing.get("error") == "no usable option chain"
    assert missing["data_quality"]["chain_ok"] is False

    print("service.py self-test OK — %d generated, %d benchmarks, "
          "metrics=%s, dq=%s" % (len(recs["generated"]),
                                 len(recs["benchmarks"]),
                                 rep["data_quality"]["metrics_source"],
                                 rep["data_quality"]))

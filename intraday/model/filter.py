"""ModelFilter — map our live data to the trained model's 31-feature vector and
return a calibrated win-probability, used to gate/scale agent signals.

Resilient by design: if the pickle, lightgbm, scipy or the pipeline module is
missing, the filter DISABLES itself (win_prob → None, passes → True) so the
engine keeps trading on the agents alone. The model's own report shows weak
out-of-sample power, so it is a filter that can VETO/scale, never invent trades.
"""
from __future__ import annotations

import math
import os
import pickle
from pathlib import Path

from ..agents._ta import atr, ema, last, rsi
from ..agents.base import chain_features

# the model's feature schema (quant/pipeline/features.py)
NUMERIC = ["ret_1", "ret_3", "ret_6", "atr_pct", "rsi", "trend", "rv_20", "vwap_dev",
           "rel_vol", "range_pos", "rel_strength", "beta", "vix", "pcr_oi",
           "dist_call_wall_atr", "dist_put_wall_atr", "gex", "atm_iv", "skew_25",
           "total_oi_z", "dte", "days_to_event", "tod_bucket", "dow",
           "spread_ticks", "depth_imb", "dir_sign"]
CATEGORICAL = ["strategy_id", "setup_id", "era", "liq_tier"]


def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


class ModelFilter:
    def __init__(self, model, min_prob: float):
        self.model = model
        self.min_prob = min_prob

    @property
    def enabled(self) -> bool:
        return self.model is not None

    # ---- construction ----
    @classmethod
    def maybe(cls, cfg: dict) -> "ModelFilter":
        """Build from cfg, or a disabled filter if unavailable. Never raises."""
        min_prob = float(cfg.get("model_filter_min_prob", 0.5))
        if not cfg.get("model_filter_enabled", False):
            return cls(None, min_prob)
        path = cfg.get("model_path") or os.environ.get(
            "INTRADAY_MODEL", str(Path(__file__).resolve().parents[2] / "state" / "model_full.pkl"))
        try:
            import lightgbm  # noqa: F401  (the pickle needs it to unpickle the booster)
            with open(path, "rb") as fh:
                obj = pickle.load(fh)
            model = obj["model"] if isinstance(obj, dict) else obj
            if not hasattr(model, "predict"):
                return cls(None, min_prob)
            return cls(model, min_prob)
        except Exception:
            return cls(None, min_prob)   # missing file/deps → disabled, engine trades on agents

    # ---- feature vector ----
    def _features(self, sig, chain, bars, index_bars, vix, now) -> dict:
        f = {c: 0.0 for c in NUMERIC}
        close = last(bars["close"]) if bars is not None and len(bars) else (chain.spot if chain else 0.0)
        if bars is not None and len(bars) > 6:
            c = bars["close"]
            f["ret_1"] = _f(c.iloc[-1] / c.iloc[-2] - 1)
            f["ret_3"] = _f(c.iloc[-1] / c.iloc[-4] - 1)
            f["ret_6"] = _f(c.iloc[-1] / c.iloc[-7] - 1)
            a = last(atr(bars))
            f["atr_pct"] = _f(a / close) if close else 0.0
            f["rsi"] = _f(last(rsi(c, 14)), 50.0)
            f["trend"] = 1.0 if last(ema(c, 9)) >= last(ema(c, 21)) else -1.0
            f["rv_20"] = _f(c.pct_change().tail(20).std())
            if "vwap" in bars:
                f["vwap_dev"] = _f((close - last(bars["vwap"])) / close) if close else 0.0
            vol = bars.get("volume")
            if vol is not None and vol.tail(20).mean():
                f["rel_vol"] = _f(vol.iloc[-1] / vol.tail(20).mean())
            hi, lo = last(bars["high"]), last(bars["low"])
            f["range_pos"] = _f((close - lo) / (hi - lo)) if hi > lo else 0.5
        if index_bars is not None and len(index_bars) > 6 and bars is not None and len(bars) > 6:
            ic = index_bars["close"]
            f["rel_strength"] = _f(f["ret_6"] - (ic.iloc[-1] / ic.iloc[-7] - 1))
        f["beta"] = 1.0
        f["vix"] = _f(vix, 15.0)
        cf = chain_features(chain) if chain else {}
        a = last(atr(bars)) if bars is not None else 0.0
        a = a if a and a == a and a > 0 else max(close * 0.002, 1.0)
        spot = chain.spot if chain else close
        f["pcr_oi"] = _f(cf.get("pcr_oi"), 1.0)
        cw, pw = cf.get("call_wall"), cf.get("put_wall")
        f["dist_call_wall_atr"] = _f((cw - spot) / a) if cw else 0.0
        f["dist_put_wall_atr"] = _f((spot - pw) / a) if pw else 0.0
        f["gex"] = _f(cf.get("net_gex"))
        f["atm_iv"] = _f(cf.get("atm_iv"))
        f["skew_25"] = _f(cf.get("skew_25"))
        f["dte"] = float(chain.days_to_expiry) if chain else 0.0
        f["days_to_event"] = _f((sig and (sig.regime or {}).get("days_to_event")), 30.0) if sig else 30.0
        hr = now.hour + now.minute / 60.0
        f["tod_bucket"] = float(0 if hr < 11 else 1 if hr < 13 else 2 if hr < 14.5 else 3)
        f["dow"] = float(now.weekday())
        # ATM leg microstructure
        leg = chain.get(chain.atm, sig.direction == "BUY") if chain else None
        if leg:
            f["spread_ticks"] = _f((leg.ask - leg.bid) / 0.05) if leg.ask > leg.bid else 0.0
            tot = (leg.bid_qty or 0) + (leg.ask_qty or 0)
            f["depth_imb"] = _f(((leg.bid_qty or 0) - (leg.ask_qty or 0)) / tot) if tot else 0.0
        f["dir_sign"] = 1.0 if sig.direction == "BUY" else -1.0
        strat = (sig.instrument or {}).get("strategy") if sig.instrument else None
        strat = strat or getattr(sig, "strategy", None) or "orb"
        oi = cf.get("call_oi", 0) + cf.get("put_oi", 0)
        liq = "high" if oi > 300000 else "mid" if oi > 80000 else "low"
        return {**f, "strategy_id": str(strat), "setup_id": str(strat),
                "era": str(now.year), "liq_tier": liq}

    def win_prob(self, sig, chain, bars, index_bars, vix, now) -> float | None:
        if not self.enabled:
            return None
        try:
            import pandas as pd
            row = self._features(sig, chain, bars, index_bars, vix, now)
            X = pd.DataFrame([row])[NUMERIC + CATEGORICAL]
            return float(self.model.predict(X)[0])
        except Exception:
            return None

    def passes(self, prob: float | None) -> bool:
        """True = allowed to trade. A disabled filter or an unscorable row never
        blocks (prob None → pass); a real prob must clear the threshold."""
        return prob is None or prob >= self.min_prob

"""Persistent config for the agent tree: toggles, weights, thresholds.

Stored in quant_config.json next to the project. Layout:
{
  "signal_threshold": 60.0,            # composite score needed to accept a signal
  "default_agent_threshold": 50.0,     # green/red line per agent unless overridden
  "agents": {
    "volatility.india_vix": {"enabled": true, "weight_buy": 1.0,
                              "weight_sell": 1.2, "threshold": 50}
  },
  "per_symbol": {                      # every stock has its own behaviour
    "RELIANCE": {"volatility": {"weight_buy": 0.6}}
  }
}
Only overrides are stored; anything absent falls back to the agent class
defaults. The backtesting/training module (Phase 2) will write the trained
weights into this same file.
"""

from __future__ import annotations

import json
import os
import threading

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "quant_config.json")


class QuantConfig:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {"signal_threshold": 60.0,
                           "default_agent_threshold": 50.0,
                           "agents": {}, "per_symbol": {}}
        self.load()

    # ── persistence ─────────────────────────────────────────────────────
    def load(self):
        try:
            with open(self.path) as f:
                loaded = json.load(f)
            self.data.update(loaded)
        except (OSError, ValueError):
            pass                                # first run: defaults

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    # ── lookups used by QuantAgent.evaluate ─────────────────────────────
    def _agent(self, key: str) -> dict:
        return self.data.get("agents", {}).get(key, {})

    def enabled(self, key: str, default: bool = True) -> bool:
        return bool(self._agent(key).get("enabled", default))

    def threshold(self, key: str) -> float:
        return float(self._agent(key).get(
            "threshold", self.data.get("default_agent_threshold", 50.0)))

    def veto_below(self, key: str, default: float | None = None) -> float | None:
        """Hard veto floor for gatekeeper agents: if the agent scored BELOW
        this, the signal is rejected regardless of the composite. None =
        agent has no veto power. (Born from a COLPAL sim entry where market
        structure scored 19/100 — fresh BOS_DOWN — yet the averaged
        composite still passed.)"""
        v = self._agent(key).get("veto_below", default)
        return None if v is None else float(v)

    def weight(self, key: str, symbol: str, direction: str,
               default_buy: float, default_sell: float) -> float:
        field = "weight_buy" if direction == "BUY" else "weight_sell"
        # per-symbol override wins, then global agent override, then default
        sym = self.data.get("per_symbol", {}).get(symbol, {}).get(key, {})
        if field in sym:
            return float(sym[field])
        agent = self._agent(key)
        if field in agent:
            return float(agent[field])
        return default_buy if direction == "BUY" else default_sell

    @property
    def signal_threshold(self) -> float:
        return float(self.data.get("signal_threshold", 60.0))

    # ── mutations from the UI ───────────────────────────────────────────
    def set_enabled(self, key: str, enabled: bool):
        self.data.setdefault("agents", {}).setdefault(key, {})["enabled"] = enabled
        self.save()

    def set_weight(self, key: str, direction: str, value: float,
                   symbol: str | None = None):
        field = "weight_buy" if direction == "BUY" else "weight_sell"
        if symbol:
            node = (self.data.setdefault("per_symbol", {})
                    .setdefault(symbol, {}).setdefault(key, {}))
        else:
            node = self.data.setdefault("agents", {}).setdefault(key, {})
        node[field] = float(value)
        self.save()

    def set_threshold(self, key: str, value: float):
        self.data.setdefault("agents", {}).setdefault(key, {})["threshold"] = float(value)
        self.save()

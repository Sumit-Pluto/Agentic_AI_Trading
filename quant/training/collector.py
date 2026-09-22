"""quant/training/collector.py — capture agent evaluations as training samples.

The collector is a *tap*: the scanners call ``collector.record(result, source)``
for every (symbol, direction) they evaluate. It flattens the score tree and
appends one compact JSON line to ``state/agent_training/samples.jsonl``.

Two hard rules, because this runs inside the live sweep:
  * never raise — a broken collector must never kill a sweep (everything is
    wrapped; failures are logged at debug and swallowed);
  * never change trading behaviour — it only reads ``result`` and appends.

Disable entirely with AGENTS_TRAINING_COLLECT=0.

Sample schema (one JSON object per line):
    {
      "ts":        "2026-07-06T13:30:05",   # when recorded
      "bar_time":  "2026-07-06T13:30:00",   # signal bar (from trigger if present)
      "date":      "2026-07-06",            # entry day (for the labeler)
      "symbol":    "CGPOWER",
      "direction": "BUY",
      "segment":   "FNO",
      "source":    "agent_primary",         # which scanner produced it
      "price":     920.30,                  # entry reference price
      "composite": 64.9,                    # the tree's weighted score
      "accepted":  true,
      "vetoed":    false,
      "families":  {"smc": 57.2, "snr": 61.0, "volatility": 70.1,
                    "volume": 48.0, "macro": null},
      "leaves":    {"smc_structure": 62.8, "smc_zones": 46.2, ...},
      # filled later by the labeler:
      "labeled":   false, "label": null, "outcome_pnl": null,
      "exit_reason": null, "bars_held": null
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("training")

# canonical family order — the root tree's direct children (quant/registry.py)
FAMILIES = ("smc", "snr", "volatility", "volume", "macro")

DEFAULT_DIR = os.path.join("state", "agent_training")
DEFAULT_PATH = os.path.join(DEFAULT_DIR, "samples.jsonl")


def _enabled() -> bool:
    return os.getenv("AGENTS_TRAINING_COLLECT", "1") == "1"


def flatten_families(tree: dict | None) -> dict:
    """Root tree -> {family_key: score|None} for the 5 top-level children.

    ``tree`` is the dict from Scanner.evaluate()['tree'] (root key 'quant').
    A family absent from the tree, or unavailable/skipped, is recorded as None
    so the dataset stage can decide how to impute it — never silently 0 (0 is a
    real, strongly-against score and must not be confused with 'no data')."""
    out: dict[str, float | None] = {k: None for k in FAMILIES}
    if not tree:
        return out
    for child in tree.get("children") or []:
        key = child.get("key")
        if key in out:
            score = child.get("score")
            out[key] = None if score is None else float(score)
    return out


def flatten_leaves(tree: dict | None) -> dict:
    """All leaf nodes (no children) -> {key: score|None}. Captured for future
    deeper training; the v1 trainer fits family-level weights only."""
    out: dict[str, float | None] = {}
    if not tree:
        return out
    stack = [tree]
    while stack:
        node = stack.pop()
        kids = node.get("children") or []
        if kids:
            stack.extend(kids)
        else:
            key = node.get("key")
            if key and key != "quant":
                score = node.get("score")
                out[key] = None if score is None else float(score)
    return out


class TrainingCollector:
    """Append-only writer for training samples. Thread-safe, fail-silent."""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()

    def record(self, result: dict, source: str) -> None:
        """Append one sample from a Scanner.evaluate() result dict. Safe to
        call on every evaluation — no-op if disabled or if result is unusable."""
        if not _enabled() or not result:
            return
        try:
            score = result.get("score")
            if score is None:
                return                      # nothing to learn from a skip
            trig = result.get("trigger") or {}
            bar_time = (trig.get("bar_time")
                        or result.get("evaluated_at")
                        or datetime.now().isoformat(timespec="seconds"))
            date = str(bar_time)[:10]
            row = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "bar_time": bar_time,
                "date": date,
                "symbol": result.get("symbol"),
                "direction": result.get("direction"),
                "segment": result.get("segment"),
                "source": source,
                "price": result.get("price"),
                "composite": round(float(score), 2),
                "accepted": bool(result.get("accepted")),
                "vetoed": bool(result.get("vetoed_by")),
                "families": flatten_families(result.get("tree")),
                "leaves": flatten_leaves(result.get("tree")),
                "labeled": False, "label": None, "outcome_pnl": None,
                "exit_reason": None, "bars_held": None,
            }
            line = json.dumps(row, default=str)
            with self._lock:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(line + "\n")
        except Exception as e:                       # never kill a sweep
            log.debug("training collector skipped a sample: %s", e)


# module-level default, so the scanners can share one writer without threading
# it through every constructor
default_collector = TrainingCollector()


def record(result: dict, source: str) -> None:
    """Convenience: record via the process-wide default collector."""
    default_collector.record(result, source)

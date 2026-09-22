"""Agent-tree core: every quant check is a QuantAgent node in one tree.

Design rules (Phase 1):
  * Leaf agents implement compute(ctx) -> (score 0-100 | None, detail str).
    100 = strongly supports the signal direction in ctx.direction.
    None = cannot compute (missing data) -> agent is SKIPPED, never guessed.
  * Branch agents aggregate enabled+available children by weight.
  * Weights are per direction (buy/sell) and can be overridden per symbol —
    stock behaviour is not general, so RELIANCE can weight volatility
    differently than TCS. Defaults live in code; overrides in quant_config.json.
  * Every node can be toggled off from the UI; disabled nodes are excluded
    from the weighted sum (their weight is redistributed implicitly by
    normalization).
  * Adding a future agent (macro, DOM, volume profile) = subclass QuantAgent,
    add to the tree in registry.py. Nothing else changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("quant")

BUY = "BUY"
SELL = "SELL"


@dataclass
class AgentScore:
    """Evaluation result for one node, nested for the UI drill-down."""
    key: str
    name: str
    score: float | None          # 0-100; None = unavailable/skipped
    available: bool              # data existed and score computed
    enabled: bool                # toggle state at evaluation time
    weight: float                # effective weight used for this direction
    threshold: float             # green/red line for the UI
    detail: str                  # one-line human explanation
    children: list["AgentScore"] = field(default_factory=list)

    @property
    def passed(self) -> bool | None:
        if self.score is None:
            return None
        return self.score >= self.threshold

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "score": self.score,
            "available": self.available, "enabled": self.enabled,
            "weight": self.weight, "threshold": self.threshold,
            "passed": self.passed, "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
        }


class QuantAgent:
    """Base node. Subclass and either:
       * override compute(ctx) for a LEAF, or
       * pass children for a BRANCH (aggregation is automatic).
    """
    key: str = "agent"
    name: str = "Agent"
    # default weights, overridable via config / per symbol
    default_weight_buy: float = 1.0
    default_weight_sell: float = 1.0
    # future-stub agents ship disabled and show "coming soon" in the UI
    default_enabled: bool = True
    description: str = ""

    def __init__(self, children: list["QuantAgent"] | None = None):
        self.children: list[QuantAgent] = children or []

    # ── leaf API ────────────────────────────────────────────────────────
    def compute(self, ctx) -> tuple[float | None, str]:
        """Return (score 0-100 or None, detail). Leaf agents override."""
        raise NotImplementedError

    # ── evaluation (uniform for leaf & branch) ──────────────────────────
    def evaluate(self, ctx, cfg) -> AgentScore:
        enabled = cfg.enabled(self.key, default=self.default_enabled)
        weight = cfg.weight(self.key, ctx.symbol, ctx.direction,
                            self.default_weight_buy, self.default_weight_sell)
        threshold = cfg.threshold(self.key)

        if not enabled:
            return AgentScore(self.key, self.name, None, False, False,
                              weight, threshold, "disabled by user",
                              [c.evaluate(ctx, cfg) for c in self.children])

        if self.children:                      # branch: weighted rollup
            kids = [c.evaluate(ctx, cfg) for c in self.children]
            usable = [k for k in kids if k.enabled and k.available
                      and k.score is not None and k.weight > 0]
            if not usable:
                return AgentScore(self.key, self.name, None, False, True,
                                  weight, threshold,
                                  "no child agent had data", kids)
            total_w = sum(k.weight for k in usable)
            score = sum(k.score * k.weight for k in usable) / total_w
            detail = f"{len(usable)}/{len(kids)} sub-agents contributed"
            return AgentScore(self.key, self.name, round(score, 1), True,
                              True, weight, threshold, detail, kids)

        # leaf: guarded compute
        try:
            score, detail = self.compute(ctx)
        except Exception as e:                 # a broken agent must never
            log.warning("agent %s failed: %s", self.key, e)   # kill a signal
            score, detail = None, f"error: {e}"
        if score is not None:
            score = max(0.0, min(100.0, float(score)))
        return AgentScore(self.key, self.name,
                          None if score is None else round(score, 1),
                          score is not None, True, weight, threshold,
                          detail, [])

    # ── introspection for UI/config ─────────────────────────────────────
    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def describe(self) -> dict:
        return {
            "key": self.key, "name": self.name,
            "description": self.description,
            "default_enabled": self.default_enabled,
            "children": [c.describe() for c in self.children],
        }

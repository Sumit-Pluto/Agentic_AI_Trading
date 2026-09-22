"""quant/pipeline/filters.py — LAYER E pluggable, user-toggleable filter stack.

Each filter is independent and switchable via config (disabled = skipped entirely,
never evaluated). Kinds: veto (drop candidate), multiplier (scale P(win)/size),
annotate (log only). Every decision is returned so it can be logged to the outcome
DB and its realized value measured later. See docs/INTRADAY_PIPELINE_Vnext.md §5.
"""

from __future__ import annotations

from .contracts import FilterResult


class Filter:
    id = "filter"
    kind = "veto"                 # veto | multiplier | annotate
    default_enabled = True

    def evaluate(self, cand) -> FilterResult:
        raise NotImplementedError


class CostFloor(Filter):
    id, kind = "cost_floor", "veto"
    def evaluate(self, cand):
        e = cand.event
        p_be = e.stop_atr / (e.stop_atr + e.target_atr)   # break-even win prob
        p = cand.p_win if cand.p_win is not None else 0.0
        ok = p >= p_be + 0.02
        return FilterResult(ok, 1.0, f"p={p:.2f} vs breakeven {p_be:.2f}")


class RegimeGate(Filter):
    id, kind = "regime_gate", "multiplier"
    def evaluate(self, cand):
        vix = cand.features.get("vix", 15.0)
        factor = 1.2 if vix < 14 else (0.7 if vix > 22 else 1.0)   # danger = high vix
        return FilterResult(True, factor, f"vix={vix:.1f} -> x{factor}")


class BanGate(Filter):
    id, kind = "ban_gate", "veto"
    def evaluate(self, cand):
        banned = cand.features.get("ban", 0.0) >= 1.0
        return FilterResult(not banned, 1.0, "F&O ban" if banned else "ok")


class LiquidityScreen(Filter):
    id, kind = "liquidity_screen", "veto"
    def evaluate(self, cand):
        tier = cand.features.get("liq_tier", "A")
        return FilterResult(tier != "C", 1.0, f"liq_tier={tier}")


class WallProximity(Filter):
    id, kind = "wall_proximity", "veto"
    def evaluate(self, cand):
        f = cand.features
        if cand.event.direction == "BUY":
            d = f.get("dist_call_wall_atr", 99)
        else:
            d = f.get("dist_put_wall_atr", 99)
        return FilterResult(d >= 0.6, 1.0, f"wall_dist={d:.2f} ATR")


class DTEGate(Filter):
    id, kind = "dte_gate", "veto"
    def evaluate(self, cand):
        dte = cand.features.get("dte", 5)
        return FilterResult(dte >= 1, 1.0, f"dte={dte:.0f}")


class EventBlackout(Filter):
    id, kind = "event_blackout", "veto"
    default_enabled = False       # opt-in
    def evaluate(self, cand):
        d = cand.features.get("days_to_event", 5)
        return FilterResult(d > 0, 1.0, f"days_to_event={d:.0f}")


ALL_FILTERS = [CostFloor, RegimeGate, BanGate, LiquidityScreen,
               WallProximity, DTEGate, EventBlackout]


class FilterStack:
    def __init__(self, config: dict | None = None):
        config = config or {}
        self.filters = []
        for cls in ALL_FILTERS:
            cfg = config.get(cls.id, {})
            enabled = cfg.get("enabled", cls.default_enabled)
            if enabled:
                self.filters.append(cls())

    @property
    def enabled_ids(self):
        return [f.id for f in self.filters]

    def apply(self, cand):
        """Run enabled filters; mutate candidate; return the decision log."""
        log = []
        for flt in self.filters:
            res = flt.evaluate(cand)
            log.append((flt.id, res.passed, round(res.factor, 3), res.reason))
            if flt.kind == "veto" and not res.passed:
                cand.vetoed_by = flt.id
                break                       # first veto stops the chain
            if flt.kind == "multiplier":
                cand.filter_factor *= res.factor
        return log

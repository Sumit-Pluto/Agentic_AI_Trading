"""FUT-vs-OPT router — deterministic v1 decision rules.

Order of evaluation (first hit wins):
  1. Hard vetoes (untradable state) → SKIP or forced leg.
  2. Forced-futures gates (each independently toggleable via cfg).
  3. Forced-options gates (defined-risk demand).
  4. Scored comparison on expected net edge; ties default to FUT
     (simpler fills, no decay, cleaner exits through the I0–I8 machine).

Every branch returns a human-readable `reason` that is journalled with the
signal, so the choice is retrainable from the outcome DB later.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .features import SelectorFeatures

FUT = "FUT"
OPT = "OPT"
SKIP = "SKIP"


@dataclass
class Decision:
    kind: str                 # FUT | OPT | SKIP
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)


def _cfg(cfg: dict | None) -> dict:
    return dict(cfg or {})


def choose(feats: SelectorFeatures, cfg: dict | None = None) -> Decision:
    """Pure routing decision from precomputed features."""
    c = _cfg(cfg)
    if not c.get("selector_enabled", True):
        return Decision(OPT, "selector disabled → legacy option leg",
                        {"path": "disabled"})
    d: dict = {}

    # ---- 1. hard vetoes ----
    if feats.event_blackout and c.get("selector_event_skip", True):
        return Decision(SKIP, "event blackout → no trade",
                        {"path": "event_veto"})
    if not feats.futures_available:
        # No futures path configured: keep legacy behaviour (option leg if any).
        if feats.entry_prem != feats.entry_prem:
            return Decision(SKIP, "no tradable option leg and no futures",
                            {"path": "no_instrument"})
        return Decision(OPT, "futures unavailable → option leg",
                        {"path": "fut_unavailable"})
    if feats.entry_prem != feats.entry_prem:
        return Decision(FUT, "no tradable option leg → futures",
                        {"path": "no_opt_leg"})

    # ---- 2. forced-futures gates (each toggleable) ----
    max_spread = float(c.get("selector_max_spread_pct", 3.0))
    if feats.spread_pct == feats.spread_pct and feats.spread_pct > max_spread:
        return Decision(FUT, f"option spread {feats.spread_pct:.1f}% > {max_spread:.1f}% → futures",
                        {"path": "illiquid_spread", "spread_pct": feats.spread_pct})
    min_oi = int(c.get("selector_min_opt_oi", 500))
    if feats.oi < min_oi:
        return Decision(FUT, f"option OI {feats.oi} < {min_oi} → futures",
                        {"path": "thin_oi", "oi": feats.oi})
    ivp_hi = float(c.get("selector_ivp_fut_above", 0.70))
    if feats.iv_percentile is not None and feats.iv_percentile >= ivp_hi:
        return Decision(FUT, f"IV rank {feats.iv_percentile:.2f} ≥ {ivp_hi:.2f} (expensive premium) → futures",
                        {"path": "high_iv", "ivp": feats.iv_percentile})
    late_min = float(c.get("selector_late_session_min", 75.0))
    if feats.minutes_to_close == feats.minutes_to_close and feats.minutes_to_close < late_min:
        dte_floor = float(c.get("selector_late_dte_floor", 1.0))
        if feats.dte_days != feats.dte_days or feats.dte_days <= dte_floor:
            return Decision(FUT, f"{feats.minutes_to_close:.0f} min to close on ≤{dte_floor:.0f} DTE → futures (theta)",
                            {"path": "late_session", "min_to_close": feats.minutes_to_close})
    trend_min = float(c.get("selector_trend_fut_above", 15.0))
    if feats.trend_score == feats.trend_score and feats.trend_score >= trend_min:
        be = feats.breakeven_pts
        exp = feats.atr_pts
        # Strong momentum: only keep the option if its breakeven is a small
        # fraction of the expected move (gamma pays for itself).
        frac = float(c.get("selector_trend_breakeven_frac", 0.35))
        if not (be == be and exp == exp and exp > 0 and be <= frac * exp):
            return Decision(FUT, f"trend score {feats.trend_score:.0f} ≥ {trend_min:.0f} → futures (delta-1)",
                            {"path": "trend", "trend": feats.trend_score})

    # ---- 3. forced-options gates ----
    if c.get("selector_require_defined_risk", False):
        return Decision(OPT, "defined-risk required → options",
                        {"path": "defined_risk"})
    ivp_lo = float(c.get("selector_ivp_opt_below", 0.30))
    if (feats.iv_percentile is not None and feats.iv_percentile <= ivp_lo
            and feats.trend_score == feats.trend_score and feats.trend_score < 0):
        return Decision(OPT, f"cheap premium (IV rank {feats.iv_percentile:.2f}) + no trend → options",
                        {"path": "cheap_iv_range"})

    # ---- 4. scored comparison ----
    # Cost of the option in underlying points vs expected move; futures cost is
    # friction only (small constant in points-equivalent, configured per symbol
    # universe via selector_fut_friction_pts).
    be = feats.breakeven_pts if feats.breakeven_pts == feats.breakeven_pts else float("inf")
    exp = feats.atr_pts if feats.atr_pts == feats.atr_pts and feats.atr_pts > 0 else float("nan")
    theta_day = abs(feats.theta_per_share or 0.0)
    delta = abs(feats.delta or 0.5)
    prem = feats.entry_prem if feats.entry_prem == feats.entry_prem else 0.0
    theta_pts = (theta_day / max(delta, 0.05)) if theta_day else 0.0
    fut_cost = float(c.get("selector_fut_friction_pts", 0.0))
    if exp != exp:  # no move estimate: fall back to premium-cheapness heuristic
        cheap = prem > 0 and prem <= float(c.get("selector_cheap_prem", 20.0))
        kind = OPT if cheap else FUT
        return Decision(kind, f"no move estimate; premium {'cheap' if cheap else 'rich'} → {kind.lower()}",
                        {"path": "no_estimate", "prem": prem})
    # Range/no-trend path: a straight linear comparison can never favour the
    # option (it always pays breakeven+theta for the same expected move). The
    # option's edge is convexity + defined risk, which pays in range-bound or
    # counter-trend trades where the breakeven is a small fraction of the
    # expected move — there the premium is cheap insurance, not a drag.
    range_frac = float(c.get("selector_range_breakeven_frac", 0.5))
    if feats.trend_score != feats.trend_score or feats.trend_score < trend_min:
        if be == be and exp == exp and exp > 0 and be <= range_frac * exp:
            return Decision(OPT, f"no trend + breakeven {be:.0f} ≤ {range_frac:.0%} of move {exp:.0f} → options",
                            {"path": "range", "breakeven": round(be, 1)})
    opt_edge = exp - be - theta_pts
    fut_edge = exp - fut_cost
    d["opt_edge"] = round(opt_edge, 2)
    d["fut_edge"] = round(fut_edge, 2)
    margin = float(c.get("selector_edge_margin_pts", 0.0))
    if opt_edge > fut_edge + margin:
        return Decision(OPT, f"option edge {opt_edge:.1f} > futures {fut_edge:.1f} → options",
                        {"path": "scored", **d})
    return Decision(FUT, f"futures edge {fut_edge:.1f} ≥ option {opt_edge:.1f} → futures",
                    {"path": "scored", **d})


def build_futures_instrument(*, symbol: str, direction: str, spot: float,
                             lot_size: int = 1, exch: str = "NFO",
                             token: str = "", tsym: str = "",
                             expiry: str | None = None) -> dict:
    """Synthesise the FUT instrument descriptor.

    When a live futures quote/token is available the caller fills token/tsym/
    entry from it; otherwise entry falls back to spot so paper/backtest sizing
    still works (single code path) and the live broker maps tsym at submit.
    """
    px = float(spot) if spot and math.isfinite(float(spot)) else 0.0
    return {"kind": FUT, "symbol": symbol, "direction": direction,
            "exch": exch, "token": token, "tsym": tsym or f"{symbol}-FUT",
            "strike": 0.0, "right": "FUT", "expiry": expiry,
            "lot_size": int(lot_size or 1), "entry_prem": px, "ask": px,
            "bid": px, "fut_px": px, "delta": 1.0}


def select(*, signal, chain, quote, greeks: dict | None = None,
           opt_leg: dict | None = None, session: dict | None = None,
           iv_percentile: float | None = None, atr_pts: float | None = None,
           event_blackout: bool = False, futures_quote: dict | None = None,
           cfg: dict | None = None) -> tuple[dict | None, Decision]:
    """Top-level entry: given the scanner's OPT leg, return the instrument to
    trade (OPT leg or synthesised FUT) plus the decision. Returns (None, SKIP)
    when neither leg is tradable."""
    c = _cfg(cfg)
    feats = build_features(
        symbol=signal.symbol, direction=signal.direction, chain=chain,
        quote=quote, greeks=greeks,
        family_scores=getattr(signal, "family_scores", None),
        regime=getattr(signal, "regime", None), session=session,
        iv_percentile=iv_percentile, atr_pts=atr_pts,
        event_blackout=event_blackout,
        # Opt-in: FUT routes only when a futures quote is wired or the
        # deployment sets futures_available=1. Legacy contexts (no future()
        # on ctx, flag absent) keep byte-identical OPT behaviour.
        futures_available=(futures_quote is not None) or bool(c.get("futures_available", False)))
    decision = choose(feats, c)
    if decision.kind == SKIP:
        return None, decision
    if decision.kind == FUT:
        fq = futures_quote or {}
        spot = getattr(chain, "spot", 0.0) if chain is not None else 0.0
        inst = build_futures_instrument(
            symbol=signal.symbol, direction=signal.direction,
            spot=float(fq.get("px") or spot or 0.0),
            lot_size=int(fq.get("lot_size") or (getattr(chain, "lot_size", 0) or 0) or 1),
            exch=str(fq.get("exch") or "NFO"), token=str(fq.get("token") or ""),
            tsym=str(fq.get("tsym") or ""),
            expiry=(getattr(chain, "expiry", None).isoformat()
                    if getattr(chain, "expiry", None) is not None and
                    hasattr(getattr(chain, "expiry", None), "isoformat") else None))
        inst["selector_reason"] = decision.reason
        return inst, decision
    leg = dict(opt_leg or {})
    leg["kind"] = OPT
    leg["selector_reason"] = decision.reason
    return (leg or None), decision

"""Combiner (PDF §2.4) — ported from swing_hyena, families S/F/V/M/C.

  per-symbol agent scores -> family score = agent-weighted mean of available
  agents (brain.agent_w; weight<=0 prunes; N/A renormalised)
  -> composite = brain.family_w-weighted mix over AVAILABLE families
  -> cross-sectional blend: final = 0.6*raw + 0.4*(50 + 20*z_xs(raw))
  -> symbol residual tilt (lambda(n)-shrunk), mirrored, clipped [0,100]
  -> hard VETOES bypass weighting; side-scoped veto_long/veto_short gate one side
  -> REGIME scales threshold & size, never the scores
  -> direction fires only if score_side >= threshold AND beats the other side by
     margin (anti-ambiguity rule)
"""
from __future__ import annotations

import numpy as np

from ..agents.base import AgentResult
from ..contracts import STOCK_FAMILIES, Brain


def regime_gate(r_results: list[AgentResult]) -> dict:
    """Fold the R family (+R6 event gate) into {on, scalar, vetoes, detail}."""
    vetoes = [r for r in r_results if r.veto]
    scored = [r for r in r_results if r.scored and not r.shadow]
    if not scored:
        return {"on": True, "scalar": 0.5, "vetoes": [v.veto for v in vetoes],
                "detail": "no regime agents scored — half size"}
    avg = float(np.mean([r.score_buy for r in scored]))
    on = avg >= 40 and not vetoes
    scalar = 0.0 if not on else min(1.0, max(0.3, (avg - 30) / 50))
    return {"on": on, "scalar": scalar, "vetoes": [v.veto for v in vetoes],
            "avg": avg, "detail": f"regime avg {avg:.0f}"
                                  + (f"; vetoes {[v.veto for v in vetoes]}" if vetoes else "")}


def combine_stock(results_by_symbol: dict[str, list[AgentResult]],
                  brain: Brain) -> dict[str, dict]:
    """Cross-sectional combine over the candidates for one bar, brain-weighted."""
    agent_w = brain.agent_w or {}
    family_w = brain.family_w or {}

    def _mix(fam_scores: dict[str, float]) -> float:
        if not fam_scores:
            return np.nan
        wsum = sum(family_w.get(f, 0.0) for f in fam_scores)
        if wsum <= 0:
            return float(np.mean(list(fam_scores.values())))
        return float(sum(family_w.get(f, 0.0) * v for f, v in fam_scores.items()) / wsum)

    interim = {}
    for sym, rs in results_by_symbol.items():
        vetoes = [r for r in rs if r.veto and r.family in STOCK_FAMILIES]
        veto_long = [r for r in rs if getattr(r, "veto_long", None) and r.family in STOCK_FAMILIES]
        veto_short = [r for r in rs if getattr(r, "veto_short", None) and r.family in STOCK_FAMILIES]
        fam_b, fam_s, na_list = {}, {}, []
        n_scored = 0
        for fam in STOCK_FAMILIES:
            num_b = num_s = den = 0.0
            for r in rs:
                if r.family != fam or r.shadow:
                    continue
                if r.na:
                    na_list.append(f"{r.agent}: {r.na}")
                    continue
                if not r.scored:
                    continue
                n_scored += 1
                w = float(agent_w.get(r.agent, 1.0))
                if w <= 0:
                    continue
                num_b += w * float(r.score_buy)
                num_s += w * float(r.score_sell)
                den += w
            if den > 0:
                fam_b[fam] = num_b / den
                fam_s[fam] = num_s / den
        interim[sym] = {"fam_b": fam_b, "fam_s": fam_s, "vetoes": vetoes,
                        "veto_long": veto_long, "veto_short": veto_short, "na": na_list,
                        "raw_b": _mix(fam_b), "raw_s": _mix(fam_s), "n_scored": n_scored}

    def xz(d: dict[str, float]) -> dict[str, float]:
        v = np.array([x for x in d.values() if np.isfinite(x)], float)
        if len(v) < 2 or np.std(v) < 1e-9:
            return {k: (x - 50.0) / 20.0 if np.isfinite(x) else np.nan for k, x in d.items()}
        mu, sd = v.mean(), v.std()
        return {k: (x - mu) / sd if np.isfinite(x) else np.nan for k, x in d.items()}

    xzb = xz({s: v["raw_b"] for s, v in interim.items()})
    xzs = xz({s: v["raw_s"] for s, v in interim.items()})

    out = {}
    for sym, v in interim.items():
        zb_, zs_ = xzb.get(sym, np.nan), xzs.get(sym, np.nan)
        sb = (0.6 * v["raw_b"] + 0.4 * (50 + 20 * zb_)) if np.isfinite(v["raw_b"]) else np.nan
        ss = (0.6 * v["raw_s"] + 0.4 * (50 + 20 * zs_)) if np.isfinite(v["raw_s"]) else np.nan
        delta = float(brain.symbol_residual.get(sym, {}).get("delta", 0.0))
        tilt = 20.0 * brain.lam(sym) * delta
        if np.isfinite(sb):
            sb = float(min(100.0, max(0.0, sb + tilt)))
        if np.isfinite(ss):
            ss = float(min(100.0, max(0.0, ss - tilt)))
        out[sym] = {"score_buy": sb, "score_sell": ss,
                    "family_buy": v["fam_b"], "family_sell": v["fam_s"],
                    "vetoes": [x.veto for x in v["vetoes"]],
                    "veto_agents": [x.agent for x in v["vetoes"]],
                    "veto_long": [x.veto_long for x in v["veto_long"]],
                    "veto_short": [x.veto_short for x in v["veto_short"]],
                    "n_scored": v["n_scored"], "na": v["na"]}
    return out


def resolve_direction(sc: dict, threshold: float, margin: float) -> str | None:
    """Anti-ambiguity: a side fires only if >= threshold AND beats the opposite
    side by margin. Vetoes kill everything."""
    if sc["vetoes"]:
        return None
    b, s = sc.get("score_buy", np.nan), sc.get("score_sell", np.nan)
    long_blocked = bool(sc.get("veto_long"))
    short_blocked = bool(sc.get("veto_short"))
    if (not long_blocked and np.isfinite(b) and b >= threshold
            and (not np.isfinite(s) or b - s >= margin)):
        return "BUY"
    if (not short_blocked and np.isfinite(s) and s >= threshold
            and (not np.isfinite(b) or s - b >= margin)):
        return "SELL"
    return None

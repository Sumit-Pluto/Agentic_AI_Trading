"""strategist/recommend.py — classic benchmark menu + combined recommendation.

benchmarks() builds the rulebook's strategy menu (bull put credit, bull call
debit, bear call credit, bear put debit, iron condor, long straddle, pin
butterfly) from the live chain when the regime preconditions fit, reusing
the generator's leg-construction helpers, and ranks them by

    rank = regime_fit (0-1) x confidence_weight (LOW .4 / MED .6 / MH .8 /
           HIGH 1.0) x min(POP x RR, 1.0)

returning the top 3. recommend() bundles the GENERATOR's top-5 with the
benchmarks plus the mandatory "views, not orders" disclaimer and the
max-loss <= 2%-of-capital sizing rule. Missing data degrades honestly.
"""

from __future__ import annotations

import time

import numpy as np

try:
    from quant.mathutils import years_to_expiry
except ModuleNotFoundError:                      # direct-script execution
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from quant.mathutils import years_to_expiry

from strategist.generator import (FRICTION_PER_LEG, build_universe, classify,
                                  contract_at, generate, grid_for,
                                  legs_payoff, make_legs, strike_step)
from strategist.payoff import evaluate_strategy
from strategist.view import (CONF_WEIGHTS, _conf_weight, _dir_sign, _kind,
                             chain_atm_iv)

# ── rulebook constants ──────────────────────────────────────────────────
BULL_TH = 0.15                 # view.bias >= -> bullish regime
BEAR_TH = -0.15                # view.bias <= -> bearish regime
RANGE_CONVICTION_TH = 0.5      # iron-condor precondition
PIN_DTE_MAX = 5.0              # pin butterfly only when DTE <= 5
PIN_CONC_TH = 0.40             # or heavy single-strike concentration
BENCH_TOP = 3
FIT_FULL, FIT_PARTIAL = 1.0, 0.6
CONF_DEFAULT_W = 0.6           # no supporting footprint -> MED weight

DISCLAIMER = ("Views, not orders: every candidate is a probabilistic read of "
              "the OI/IV footprint, not trade advice. Re-check premiums, "
              "spreads and liquidity before any order.")
SIZING_NOTE = ("Sizing rule: keep worst-case loss <= 2% of trading capital. "
               "Use the max_loss_per_lot rupee figure to size lots; "
               "spread margins apply on short legs.")


def _vol_regime(view) -> str:
    vm = float(getattr(view, "vol_mult", 1.0) or 1.0)
    if vm > 1.0:
        return "iv_cheap"
    if vm < 1.0:
        return "iv_rich"
    return "normal"


def _direction(view) -> str:
    b = float(getattr(view, "bias", 0.0) or 0.0)
    if b >= BULL_TH:
        return "bullish"
    if b <= BEAR_TH:
        return "bearish"
    return "neutral"


def _support_conf(footprints, kinds: tuple = (), direction: int = 0) -> float:
    """Confidence weight of the strongest footprint backing a template."""
    best = 0.0
    for fp in footprints or []:
        k = _kind(fp)
        if kinds and any(s in k for s in kinds):
            best = max(best, _conf_weight(fp))
        elif direction and _dir_sign(fp) == direction:
            best = max(best, _conf_weight(fp))
    return best or CONF_DEFAULT_W


def _strikes(universe, is_call: bool) -> list[float]:
    return sorted({c.strike for c in universe if c.is_call == is_call})


def _at_or_below(ks: list[float], x: float) -> float | None:
    below = [k for k in ks if k <= x + 1e-9]
    return max(below) if below else None


def _at_or_above(ks: list[float], x: float) -> float | None:
    above = [k for k in ks if k >= x - 1e-9]
    return min(above) if above else None


def _shift(ks: list[float], k: float, n: int) -> float | None:
    """Strike n listed-strikes away from k (falls back one step nearer)."""
    if k not in ks:
        return None
    i = ks.index(k)
    for off in (n, (n - 1 if n > 0 else n + 1)):
        j = i + off
        if off != 0 and 0 <= j < len(ks):
            return ks[j]
    return None


def _nearest(ks: list[float], x: float) -> float | None:
    return min(ks, key=lambda k: abs(k - x)) if ks else None


def benchmarks(chain, metrics, view, footprints=None) -> list[dict]:
    """Rulebook menu, built only when preconditions fit; top 3 by rank."""
    universe = build_universe(chain, cap=None)   # full liquid chain: the menu
    if not universe or view is None:             # is constructed, not searched
        return []
    spot = float(chain.spot)
    lot = max(1, int(getattr(chain, "lot", 0) or 1))
    now = time.time()
    t_years = years_to_expiry(chain.expiry_epoch, now)
    dte = max(0.0, (chain.expiry_epoch - now) / 86400.0)
    atm = chain_atm_iv(chain)
    S = grid_for(chain)
    try:
        w = np.asarray(view.pdf_weights(S), dtype=float)
        if w.shape != S.shape or w.sum() <= 0:
            raise ValueError
        w = w / w.sum()
    except Exception:
        w = np.full(S.size, 1.0 / S.size)

    direction = _direction(view)
    vol = _vol_regime(view)
    cw = getattr(view, "call_wall", None)
    pw = getattr(view, "put_wall", None)
    step = strike_step(chain)
    max_pain = None
    if isinstance(metrics, dict):
        max_pain = metrics.get("max_pain") or metrics.get("maxpain")
    else:
        max_pain = getattr(metrics, "max_pain", None)
    try:
        max_pain = float(max_pain) if max_pain is not None else None
    except (TypeError, ValueError):
        max_pain = None
    conc = None
    for name in ("call_concentration", "put_concentration",
                 "concentration_call", "concentration_put", "concentration"):
        v = (metrics.get(name) if isinstance(metrics, dict)
             else getattr(metrics, name, None)) if metrics is not None else None
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            v = None
        if v is not None:
            conc = max(conc or 0.0, v)

    Kc, Kp = _strikes(universe, True), _strikes(universe, False)
    fps = list(footprints) if footprints else []
    candidates: list[tuple] = []   # (legs, fit, conf_w, rationale, margin)

    def credit_fit() -> float:
        return FIT_FULL if vol == "iv_rich" else FIT_PARTIAL

    def debit_fit() -> float:
        return FIT_FULL if vol == "iv_cheap" else FIT_PARTIAL

    if direction == "bullish" and vol != "iv_cheap" and pw and Kp:
        sk = _at_or_below(Kp, pw)
        lk = _shift(Kp, sk, -2) if sk else None
        if sk and lk and lk < sk:
            legs = make_legs((contract_at(universe, sk, False), -1, 1),
                             (contract_at(universe, lk, False), 1, 1))
            if legs:
                candidates.append((legs, credit_fit(),
                                   _support_conf(fps, direction=1),
                                   f"bullish + {vol}: short put at/below put "
                                   f"wall {pw:g}, long put {lk:g}",
                                   "short-leg SPAN margin, spread-offset"))
    if direction == "bullish" and vol != "iv_rich" and Kc:
        lk = _nearest(Kc, spot)
        sk = _at_or_above(Kc, cw) if cw else None
        if lk is not None and (sk is None or sk <= lk):
            sk = _shift(Kc, lk, 2)
        if lk and sk and sk > lk:
            legs = make_legs((contract_at(universe, lk, True), 1, 1),
                             (contract_at(universe, sk, True), -1, 1))
            if legs:
                candidates.append((legs, debit_fit(),
                                   _support_conf(fps, direction=1),
                                   f"bullish + {vol}: long call {lk:g}, short "
                                   f"call at call wall {sk:g}",
                                   "margin = net debit paid"))
    if direction == "bearish" and vol != "iv_cheap" and cw and Kc:
        sk = _at_or_above(Kc, cw)
        lk = _shift(Kc, sk, 2) if sk else None
        if sk and lk and lk > sk:
            legs = make_legs((contract_at(universe, sk, True), -1, 1),
                             (contract_at(universe, lk, True), 1, 1))
            if legs:
                candidates.append((legs, credit_fit(),
                                   _support_conf(fps, direction=-1),
                                   f"bearish + {vol}: short call at/above call "
                                   f"wall {cw:g}, long call {lk:g}",
                                   "short-leg SPAN margin, spread-offset"))
    if direction == "bearish" and vol != "iv_rich" and Kp:
        lk = _nearest(Kp, spot)
        sk = _at_or_below(Kp, pw) if pw else None
        if lk is not None and (sk is None or sk >= lk):
            sk = _shift(Kp, lk, -2)
        if lk and sk and sk < lk:
            legs = make_legs((contract_at(universe, lk, False), 1, 1),
                             (contract_at(universe, sk, False), -1, 1))
            if legs:
                candidates.append((legs, debit_fit(),
                                   _support_conf(fps, direction=-1),
                                   f"bearish + {vol}: long put {lk:g}, short "
                                   f"put at put wall {sk:g}",
                                   "margin = net debit paid"))
    if (direction == "neutral" and vol != "iv_cheap" and cw and pw
            and getattr(view, "range_conviction", 0.0) >= RANGE_CONVICTION_TH
            and Kc and Kp):
        sc = _at_or_above(Kc, cw)
        sp = _at_or_below(Kp, pw)
        lc = _shift(Kc, sc, 2) if sc else None
        lp = _shift(Kp, sp, -2) if sp else None
        if sc and sp and lc and lp and sp < spot < sc and lp < sp and lc > sc:
            legs = make_legs((contract_at(universe, sp, False), -1, 1),
                             (contract_at(universe, lp, False), 1, 1),
                             (contract_at(universe, sc, True), -1, 1),
                             (contract_at(universe, lc, True), 1, 1))
            if legs:
                fit = FIT_FULL if vol == "iv_rich" else FIT_PARTIAL
                candidates.append((legs, fit,
                                   _support_conf(fps, kinds=("condor", "pin")),
                                   f"neutral + {vol} + range conviction "
                                   f"{view.range_conviction:.2f}: shorts just "
                                   f"beyond walls {sp:g}P/{sc:g}C, wings "
                                   f"{lp:g}/{lc:g}",
                                   "4-leg condor margin, spread-offset"))
    straddle_fp = any("straddle" in _kind(fp) for fp in fps)
    if straddle_fp and vol == "iv_cheap" and Kc and Kp:
        k = _nearest([k for k in Kc if k in Kp] or Kc, spot)
        cc, cp = contract_at(universe, k, True), contract_at(universe, k, False)
        if cc and cp and abs(cc.strike - cp.strike) < 1e-9:
            legs = make_legs((cc, 1, 1), (cp, 1, 1))
            if legs:
                candidates.append((legs, FIT_FULL,
                                   _support_conf(fps, kinds=("straddle",)),
                                   "straddle_build footprint + iv_cheap: "
                                   f"long ATM straddle {k:g}",
                                   "margin = premium paid"))
    pin_level = max_pain if max_pain is not None else None
    if (dte <= PIN_DTE_MAX and pin_level is not None
            and (abs(pin_level - spot) <= 1.5 * step
                 or (conc or 0.0) >= PIN_CONC_TH) and Kc):
        center = _nearest(Kc, pin_level)
        loK = _shift(Kc, center, -1) if center else None
        hiK = _shift(Kc, center, 1) if center else None
        if center and loK and hiK and loK < center < hiK:
            legs = make_legs((contract_at(universe, loK, True), 1, 1),
                             (contract_at(universe, center, True), -1, 2),
                             (contract_at(universe, hiK, True), 1, 1))
            if legs:
                candidates.append((legs, 0.9,
                                   _support_conf(fps, kinds=("pin", "condor")),
                                   f"DTE {dte:.1f} <= {PIN_DTE_MAX:g} + max-pain "
                                   f"magnet {pin_level:g}: butterfly on {center:g}",
                                   "margin = net debit paid"))

    out = []
    for legs, fit, conf_w, why, margin in candidates:
        try:
            # fat-tailed, friction-aware POP at the legs' own smile IVs —
            # same realism as the generator (see payoff.evaluate_strategy)
            result = evaluate_strategy(
                classify(legs), legs, lot, spot, atm, t_years,
                leg_ivs=[getattr(contract_at(universe, l.strike, l.is_call),
                                 "iv", None) for l in legs],
                friction_rupees=FRICTION_PER_LEG * len(legs))
        except Exception:
            continue
        pop = result.pop if result.pop is not None else 0.0
        rr = result.reward_risk
        quality = min((pop * rr) if (rr is not None) else pop, 1.0)
        rank = fit * conf_w * max(0.0, quality)
        e_ps = float(legs_payoff(legs, S) @ w)
        n_lots = len(legs)
        d = result.to_dict()
        d.update({
            "label": result.name,
            "benchmark": True,
            "regime_fit": round(fit, 2),
            "confidence_weight": round(conf_w, 2),
            "rank_score": round(rank, 4),
            "expected_pnl_per_lot": round(e_ps * lot, 0),
            "friction_rupees": round(FRICTION_PER_LEG * n_lots, 0),
            "margin_note": margin,
            "rationale": why,
        })
        out.append(d)
    out.sort(key=lambda d: -d["rank_score"])
    return out[:BENCH_TOP]


def recommend(chain, metrics, footprints, view,
              profile: str = "balanced") -> dict:
    """Full recommendation bundle: generator top-5, benchmark top-3,
    disclaimer + sizing rule. Honest note when the chain is unusable.
    profile: conservative / balanced / aggressive (POP floor + ranking)."""
    if chain is None or not getattr(chain, "strikes", None):
        return {"generated": [], "benchmarks": [],
                "note": "insufficient data: no usable option chain",
                "disclaimer": DISCLAIMER, "sizing_note": SIZING_NOTE}
    try:
        generated = generate(chain, metrics, view, top_n=5, profile=profile)
    except Exception as e:
        generated = []
        note = f"generator failed: {e}"
    else:
        note = None
    try:
        bench = benchmarks(chain, metrics, view, footprints)
    except Exception:
        bench = []
    out = {"generated": generated, "benchmarks": bench,
           "profile": str(profile).lower(),
           "disclaimer": DISCLAIMER, "sizing_note": SIZING_NOTE}
    if note:
        out["note"] = note
    elif not generated and not bench:
        out["note"] = ("no candidate cleared the POP/reward-risk/max-loss "
                       "floors — no trade is a valid answer")
    return out


# ── self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    from strategist.view import (build_view, demo_footprints, demo_metrics,
                                 synthetic_chain)

    ch = synthetic_chain()
    m, f = demo_metrics(ch), demo_footprints()

    # neutral + iv_rich + strong walls -> iron condor benchmark expected
    m_rich = {**m, "iv_percentile": 92.0}
    f_neutral = [fp for fp in f if fp["kind"] == "condor_build"]
    v_neutral = build_view(m_rich, f_neutral, chain=ch)
    b = benchmarks(ch, m_rich, v_neutral, f_neutral)
    assert isinstance(b, list) and len(b) <= BENCH_TOP
    labels = [x["label"] for x in b]
    assert any("condor" in l for l in labels), labels
    for x in b:
        for key in ("rank_score", "regime_fit", "margin_note", "rationale",
                    "expected_pnl_per_lot", "max_loss_per_lot"):
            assert key in x, key

    # bullish + iv_rich -> bull put credit spread appears
    bull_fps = f + [{"kind": "call_vertical_build", "direction": "bullish",
                     "confidence": "HIGH"}] * 2
    v_bull = build_view(m_rich, bull_fps, chain=ch)
    assert v_bull.bias >= BULL_TH, v_bull.bias
    b2 = benchmarks(ch, m_rich, v_bull, bull_fps)
    assert any("bull put spread" == x["label"] for x in b2), \
        [x["label"] for x in b2]

    # straddle footprint + iv_cheap -> long straddle offered
    strad_fps = [{"kind": "straddle_build", "direction": "neutral",
                  "confidence": "MED_HIGH"}]
    m_cheap = {**m, "iv_percentile": 8.0}
    v_ch = build_view(m_cheap, strad_fps, chain=ch)
    b3 = benchmarks(ch, m_cheap, v_ch, strad_fps)
    assert any(x["label"] == "long straddle" for x in b3), \
        [x["label"] for x in b3]

    # full bundle + graceful degradation
    r = recommend(ch, m_rich, f_neutral, v_neutral)
    assert set(r) >= {"generated", "benchmarks", "disclaimer", "sizing_note"}
    assert isinstance(r["generated"], list) and len(r["generated"]) <= 5
    r_none = recommend(None, m, f, v_neutral)
    assert r_none["generated"] == [] and "note" in r_none

    print("recommend.py self-test OK — benchmarks: %s | generated %d"
          % (labels, len(r["generated"])))

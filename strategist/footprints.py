"""Footprint detection — rulebook templates 1-8 over ChainMetrics.

Templates (docs/specs/strategist_rules.md):
  1 single_leg_build      ΔOI% ≥ +20% AND volume ≥ 3× 20d avg  → MEDIUM
  2 straddle_build        CE+PE same near-ATM strike both build,
                          sizes within 40%                      → MED_HIGH
  3 call_vertical_build   two adjacent OTM call builds, 50% match → HIGH
  4 put_vertical_build    mirror on puts                         → HIGH
  5 condor_build          2 call + 2 put wing builds, flat center,
                          HIGH if sizes within 50% else MEDIUM
  6 unwind                wall strike losing >30% OI (warning)   → HIGH
  7 pcr_extreme           PCR_OI >1.2 / <0.6 contrarian modifier → MEDIUM
  8 rollover              matched −front/+next ΔOI near expiry — no view,
                          those strikes are EXCLUDED from 1-5 FIRST.

Liquidity guard: a leg participates only if OI ≥ 500 and it has a live
premium (metrics per-leg `liquid`). Unusable chain → detect() returns []
(the advisor then reports "no reliable read"). Confidence is downgraded one
level when baselines are missing (<10 sessions) or the chain is thin.

Structural precedence: condor → verticals → straddle → single legs; a leg
consumed by a larger structure does not additionally fire single_leg_build.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .metrics import (BUILD_DOI_PCT, MIN_HISTORY_SESSIONS, PCR_BEARISH_CROWD,
                      PCR_BULLISH_CROWD, UNWIND_DOI_PCT)

# ── template thresholds (module constants) ──────────────────────────────
NEAR_ATM_STEPS = 2            # straddle strike within 2 ladder steps of ATM
STRADDLE_SIZE_TOL = 0.40      # CE/PE ΔOI sizes within 40% of each other
VERTICAL_SIZE_TOL = 0.50      # adjacent-strike ΔOI sizes within 50%
CONDOR_SIZE_TOL = 0.50        # 4-wing ΔOI min/max ≥ 50% → HIGH
CONDOR_MIN_MATCH = 0.25       # below this the 4 builds aren't "a" structure
CENTER_FLAT_PCT = 0.10        # |ΔOI%| < 10% counts as flat center

CONF_ORDER = ("LOW", "MEDIUM", "MED_HIGH", "HIGH")


def _downgrade(conf: str) -> str:
    i = CONF_ORDER.index(conf)
    return CONF_ORDER[max(0, i - 1)]


def _match(values: list) -> float | None:
    """min/max ratio of positive ΔOI sizes; None if any non-positive."""
    if not values or any(v is None or v <= 0 for v in values):
        return None
    return min(values) / max(values)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.0f}%"


def _n(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+,.0f}"


@dataclass
class Footprint:
    kind: str
    strikes: list = field(default_factory=list)
    opt_types: list = field(default_factory=list)   # aligned with strikes
    direction: str = "NEUTRAL"       # BULL / BEAR / NEUTRAL / VOL
    confidence: str = "LOW"          # LOW / MEDIUM / MED_HIGH / HIGH
    evidence: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "strikes": list(self.strikes),
                "opt_types": list(self.opt_types),
                "direction": self.direction, "confidence": self.confidence,
                "evidence": self.evidence}


def _adjacent_build_pairs(builds: set, ladder: list[float], ot: str,
                          otm) -> list[tuple[float, float]]:
    pairs = []
    for k1, k2 in zip(ladder, ladder[1:]):
        if not (otm(k1) and otm(k2)):
            continue
        if (round(k1, 2), ot) in builds and (round(k2, 2), ot) in builds:
            pairs.append((k1, k2))
    return pairs


def detect(metrics, chain, baselines) -> list[Footprint]:
    """Run templates 1-8 (rollover exclusion first). Never raises; an
    unusable/missing chain returns [] so the advisor can say so honestly."""
    if metrics is None or chain is None or not getattr(chain, "strikes", None):
        return []
    if not getattr(metrics, "liquidity_ok", False):
        return []                    # rulebook: unusable chain → no read

    per: dict = metrics.per_leg or {}
    spot = chain.spot
    ladder = sorted({r.strike for r in chain.strikes})

    sessions = 0
    if baselines:
        try:
            sessions = max((int(v.get("sessions", 0))
                            for v in baselines.values()), default=0)
        except (TypeError, AttributeError):
            sessions = 0
    downgraded = (not baselines or sessions < MIN_HISTORY_SESSIONS
                  or bool(getattr(metrics, "chain_thin", False)))
    conf = (lambda c: _downgrade(c)) if downgraded else (lambda c: c)
    dg_note = (" [confidence downgraded: "
               + ("thin chain" if getattr(metrics, "chain_thin", False)
                  else f"history {sessions}/{MIN_HISTORY_SESSIONS} sessions")
               + "]") if downgraded else ""

    # ── template 8 FIRST: rollover-excluded strikes drop out of 1-5 ────
    excluded = set(getattr(metrics, "rollover_excluded", None) or ())

    def leg(k: float, ot: str):
        return per.get((round(k, 2), ot))

    def doi(k: float, ot: str):
        l = leg(k, ot)
        return None if l is None else l.get("delta_oi")

    def dpct(k: float, ot: str):
        l = leg(k, ot)
        return None if l is None else l.get("delta_oi_pct")

    builds = {key for key, l in per.items()
              if l.get("liquid") and key not in excluded
              and l.get("delta_oi_pct") is not None
              and l["delta_oi_pct"] >= BUILD_DOI_PCT}

    out: list[Footprint] = []
    consumed: set = set()

    # ── template 5: condor_build (before verticals — consumes 4 legs) ──
    call_pairs = _adjacent_build_pairs(builds, ladder, "CE", lambda k: k > spot)
    put_pairs = _adjacent_build_pairs(builds, ladder, "PE", lambda k: k < spot)
    best = None
    for cp in call_pairs:
        for pp in put_pairs:
            inner_put, inner_call = max(pp), min(cp)
            if inner_put >= inner_call:
                continue
            centers = [k for k in ladder if inner_put < k < inner_call]
            flat = all(abs(dpct(k, ot) or 0.0) < CENTER_FLAT_PCT
                       for k in centers for ot in ("CE", "PE"))
            if not flat:
                continue
            ds = [doi(pp[0], "PE"), doi(pp[1], "PE"),
                  doi(cp[0], "CE"), doi(cp[1], "CE")]
            ratio = _match(ds)
            if ratio is None or ratio < CONDOR_MIN_MATCH:
                continue
            score = sum(ds)
            if best is None or score > best[0]:
                best = (score, ratio, pp, cp, ds)
    if best:
        _, ratio, pp, cp, ds = best
        base_conf = "HIGH" if ratio >= CONDOR_SIZE_TOL else "MEDIUM"
        out.append(Footprint(
            kind="condor_build",
            strikes=[pp[0], pp[1], cp[0], cp[1]],
            opt_types=["PE", "PE", "CE", "CE"],
            direction="NEUTRAL", confidence=conf(base_conf),
            evidence=(f"wing builds PE {pp[0]:g}/{pp[1]:g} ΔOI {_n(ds[0])}"
                      f"({_pct(dpct(pp[0], 'PE'))})/{_n(ds[1])}"
                      f"({_pct(dpct(pp[1], 'PE'))}), CE {cp[0]:g}/{cp[1]:g} "
                      f"ΔOI {_n(ds[2])}({_pct(dpct(cp[0], 'CE'))})/{_n(ds[3])}"
                      f"({_pct(dpct(cp[1], 'CE'))}); center strikes "
                      f"|ΔOI%|<{CENTER_FLAT_PCT:.0%}; size match "
                      f"{ratio:.0%} — short-iron-condor/range footprint"
                      + dg_note)))
        for k in pp:
            consumed.add((round(k, 2), "PE"))
        for k in cp:
            consumed.add((round(k, 2), "CE"))

    # ── templates 3/4: call/put vertical builds ────────────────────────
    for ot, kind, direction, otm in (
            ("CE", "call_vertical_build", "BULL", lambda k: k > spot),
            ("PE", "put_vertical_build", "BEAR", lambda k: k < spot)):
        avail = {b for b in builds if b not in consumed}
        pairs = _adjacent_build_pairs(avail, ladder, ot, otm)
        pairs.sort(key=lambda p: -((doi(p[0], ot) or 0) + (doi(p[1], ot) or 0)))
        for k1, k2 in pairs:
            if (round(k1, 2), ot) in consumed or (round(k2, 2), ot) in consumed:
                continue
            d1, d2 = doi(k1, ot), doi(k2, ot)
            ratio = _match([d1, d2])
            if ratio is None or ratio < (1 - VERTICAL_SIZE_TOL):
                continue
            out.append(Footprint(
                kind=kind, strikes=[k1, k2], opt_types=[ot, ot],
                direction=direction, confidence=conf("HIGH"),
                evidence=(f"adjacent OTM {ot} builds {k1:g}/{k2:g}: ΔOI "
                          f"{_n(d1)}({_pct(dpct(k1, ot))}) / {_n(d2)}"
                          f"({_pct(dpct(k2, ot))}), size match {ratio:.0%} — "
                          f"directional-capped vertical footprint" + dg_note)))
            consumed.add((round(k1, 2), ot))
            consumed.add((round(k2, 2), ot))

    # ── template 2: straddle_build (near-ATM, both sides) ──────────────
    atm = chain.atm_strike()
    if atm is not None and atm in ladder:
        ai = ladder.index(atm)
        near = [k for i, k in enumerate(ladder)
                if abs(i - ai) <= NEAR_ATM_STEPS]
        for k in near:
            kc, kp = (round(k, 2), "CE"), (round(k, 2), "PE")
            if kc in consumed or kp in consumed:
                continue
            if kc in builds and kp in builds:
                dc, dp = doi(k, "CE"), doi(k, "PE")
                ratio = _match([dc, dp])
                if ratio is None or ratio < (1 - STRADDLE_SIZE_TOL):
                    continue
                out.append(Footprint(
                    kind="straddle_build", strikes=[k, k],
                    opt_types=["CE", "PE"], direction="VOL",
                    confidence=conf("MED_HIGH"),
                    evidence=(f"near-ATM {k:g} CE+PE both building: ΔOI CE "
                              f"{_n(dc)}({_pct(dpct(k, 'CE'))}), PE {_n(dp)}"
                              f"({_pct(dpct(k, 'PE'))}), size match "
                              f"{ratio:.0%} — long-straddle/vol-expansion "
                              f"footprint" + dg_note)))
                consumed.add(kc)
                consumed.add(kp)

    # ── template 1: single_leg_build (whatever survives) ───────────────
    for key in sorted(builds - consumed):
        k, ot = key
        l = per[key]
        # volume condition: ≥3× 20d avg when a baseline exists; without
        # baselines degrade to plain ΔOI% (confidence already downgraded)
        if l.get("volume_spike") is False:
            continue
        if ot == "CE" and k >= spot:
            direction = "BULL"
            read = "new long calls at/above spot"
        elif ot == "PE" and k <= spot:
            direction = "BEAR"
            read = "new long puts at/below spot"
        else:
            continue                       # ITM build — ambiguous, no signal
        vol_bit = (f", volume {l['vol_mult']:.1f}x 20d avg"
                   if l.get("vol_mult") is not None
                   else ", volume baseline unavailable")
        z_bit = (f", ΔOI z={l['doi_z']:+.1f}"
                 if l.get("doi_z") is not None else "")
        out.append(Footprint(
            kind="single_leg_build", strikes=[k], opt_types=[ot],
            direction=direction, confidence=conf("MEDIUM"),
            evidence=(f"{ot} {k:g} ΔOI {_n(l['delta_oi'])}"
                      f"({_pct(l['delta_oi_pct'])}){vol_bit}{z_bit} — "
                      f"{read}" + dg_note)))

    # ── template 6: unwind at a wall (warning modifier) ────────────────
    for w in getattr(metrics, "wall_unwinds", None) or []:
        out.append(Footprint(
            kind="unwind", strikes=[w["strike"]], opt_types=[w["opt_type"]],
            direction="NEUTRAL", confidence=conf("HIGH"),
            evidence=(f"{w['side'].replace('_', ' ').lower()} {w['strike']:g} "
                      f"losing OI: ΔOI {_n(w.get('delta_oi'))} "
                      f"({_pct(w.get('delta_oi_pct'))}, threshold "
                      f"{UNWIND_DOI_PCT:.0%}) — wall weakening, warns against "
                      f"range/S-R theses" + dg_note)))

    # ── template 7: pcr_extreme (contrarian modifier) ──────────────────
    pcr = getattr(metrics, "pcr_oi", None)
    if pcr is not None:
        if pcr > PCR_BEARISH_CROWD:
            out.append(Footprint(
                kind="pcr_extreme", direction="BULL",
                confidence=conf("MEDIUM"),
                evidence=(f"PCR_OI {pcr:.2f} > {PCR_BEARISH_CROWD} — crowd "
                          f"positioned bearish; mild contrarian bullish tilt "
                          f"(modifier, not standalone)" + dg_note)))
        elif pcr < PCR_BULLISH_CROWD:
            out.append(Footprint(
                kind="pcr_extreme", direction="BEAR",
                confidence=conf("MEDIUM"),
                evidence=(f"PCR_OI {pcr:.2f} < {PCR_BULLISH_CROWD} — crowd "
                          f"positioned bullish; mild contrarian bearish tilt "
                          f"(modifier, not standalone)" + dg_note)))

    # ── template 8 (informational tail): what was excluded and why ─────
    if excluded:
        ex = sorted(excluded)
        out.append(Footprint(
            kind="rollover", strikes=[k for k, _ in ex],
            opt_types=[ot for _, ot in ex], direction="NEUTRAL",
            confidence="LOW",
            evidence=(f"{len(ex)} strike-leg(s) show matched -front/+next "
                      f"month ΔOI near expiry (DTE "
                      f"{getattr(metrics, 'dte_days', 0) or 0:.1f}) — neutral "
                      f"roll, excluded from build templates")))

    return out


# ── self-test (synthetic chain with a deliberate condor footprint) ──────

if __name__ == "__main__":
    import json

    from strategist.metrics import (_synthetic_baselines_for_tests,
                                    _synthetic_chain_for_tests,
                                    compute_metrics)

    chain = _synthetic_chain_for_tests()      # spot 1000, 17 strikes, step 20

    # full-data run: 20-session baselines → no downgrade
    bl = _synthetic_baselines_for_tests(chain)
    m = compute_metrics(chain, bl)
    fps = detect(m, chain, bl)
    kinds = [f.kind for f in fps]
    assert "condor_build" in kinds, kinds
    c = next(f for f in fps if f.kind == "condor_build")
    assert c.direction == "NEUTRAL" and c.confidence == "HIGH", c
    assert set(c.strikes) == {900.0, 920.0, 1080.0, 1100.0}
    assert c.opt_types == ["PE", "PE", "CE", "CE"]
    assert any(ch.isdigit() for ch in c.evidence)         # numbers-bearing
    # condor consumed its legs: no vertical/single duplicates of the wings
    assert not any(f.kind in ("call_vertical_build", "put_vertical_build")
                   for f in fps), kinds
    assert not any(f.kind == "single_leg_build" for f in fps), kinds
    assert not any(f.kind == "straddle_build" for f in fps), kinds

    # degraded run: no baselines → one-level confidence downgrade
    m0 = compute_metrics(chain, None)
    fps0 = detect(m0, chain, None)
    c0 = next(f for f in fps0 if f.kind == "condor_build")
    assert c0.confidence == "MED_HIGH", c0.confidence
    assert "downgraded" in c0.evidence

    for f in fps + fps0:
        json.dumps(f.to_dict())

    # graceful degradation: missing/unusable inputs → []
    assert detect(None, None, None) == []
    assert detect(m, None, None) == []
    from quant.context import ChainSnapshot
    empty = ChainSnapshot(symbol="X", expiry_epoch=0, lot=0, spot=0)
    assert detect(compute_metrics(empty), empty, None) == []

    print("footprints self-test OK —",
          [(f.kind, f.direction, f.confidence) for f in fps])

"""strategist/view.py — quantitative market view from metrics + footprints.

Implements the rulebook's "View distribution" step (strategist_rules.md,
Strategy GENERATOR §1): metrics + footprints -> a View carrying

  bias b in [-1, 1]        directional tilt (footprints weighted by
                           confidence + wall-headroom asymmetry +
                           spot-vs-max-pain pull)
  vol_mult                 iv_cheap -> 1.15, iv_rich -> 0.85, else 1.0
                           (IV percentile when available, else absolute bands)
  range_conviction w [0,1] walls strength + condor/pinning footprints +
                           gamma-positive zone, damped by unwind flags
  rationale                human-readable evidence trail

and terminal-price density helpers: a mean-shifted lognormal
  mu = ln(spot) + (r - sigma^2/2) T + b * 0.6 * sigma * sqrt(T),
  sigma = atm_iv * vol_mult,
optionally mixed (weight w * 0.5) with a between-walls truncated component.
`View.pdf_weights(grid)` returns numpy weights normalised to sum 1.

The metrics / footprints objects are produced by sibling modules whose exact
shape may vary; all field access here is tolerant (dict OR attribute OR
to_dict()), and every missing input degrades honestly to a neutral reading
with the degradation recorded in `rationale` — nothing is fabricated.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

try:
    from quant.mathutils import RISK_FREE, years_to_expiry
except ModuleNotFoundError:                      # direct-script execution
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from quant.mathutils import RISK_FREE, years_to_expiry

# ── rulebook constants ──────────────────────────────────────────────────
CONF_WEIGHTS = {"LOW": 0.4, "MED": 0.6, "MEDIUM": 0.6,
                "MED_HIGH": 0.8, "MEDIUM_HIGH": 0.8, "MH": 0.8,
                "HIGH": 1.0}
CONF_DEFAULT = 0.5             # unknown confidence -> middle-low weight

BIAS_MU_COEF = 0.6             # mu shift = b * 0.6 * sigma * sqrt(T)
W_FP, W_WALL, W_PAIN = 0.5, 0.25, 0.25   # bias component weights
FP_SAT = 2.0                   # ~two HIGH footprints saturate the fp component
MAXPAIN_SAT = 0.025            # 2.5% spot distance saturates max-pain pull
PCR_MODIFIER_SCALE = 0.5       # pcr_extreme is a modifier, not a standalone

IV_CHEAP_MULT = 1.15           # expect realised > implied
IV_RICH_MULT = 0.85            # expect realised < implied
IV_PCTL_CHEAP = 25.0           # percentile bands (when history available)
IV_PCTL_RICH = 75.0
IV_ABS_CHEAP = 0.18            # absolute ATM IV bands (fallback)
IV_ABS_RICH = 0.45

RANGE_MIX_COEF = 0.5           # mixture weight on truncated part = w * 0.5
W_RANGE_WALLS = 0.30           # spot sitting between two live walls
W_RANGE_CONC = 0.20            # top-2 strike OI concentration (pinning)
W_RANGE_CONDOR = 0.30          # condor/pinning footprints
W_RANGE_GAMMA = 0.15           # inside dealer long-gamma zone
W_RANGE_STRADDLE = 0.20        # straddle build = expansion, SUBTRACTED
UNWIND_DAMP = 0.35             # any unwind flag multiplies w by this
CONC_SAT = 0.5                 # concentration that earns full W_RANGE_CONC

DEFAULT_SIGMA = 0.30           # honest fallback sigma when ATM IV missing
DEFAULT_T = 7.0 / 365.0        # fallback horizon when expiry missing


# ── tolerant field access (sibling metric/footprint APIs may vary) ─────
def _num(v):
    if isinstance(v, dict):                    # e.g. {'strike': 1100, 'oi': ...}
        for k in ("strike", "level", "value", "k"):
            if v.get(k) is not None:
                v = v[k]
                break
        else:
            return None
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _mget(metrics, *names, default=None):
    """Lookup on a metrics object OR dict, falling back to .to_dict()."""
    if metrics is None:
        return default
    d = metrics if isinstance(metrics, dict) else None
    if d is None:
        td = getattr(metrics, "to_dict", None)
        if callable(td):
            try:
                d = td()
            except Exception:
                d = None
    for n in names:
        if not isinstance(metrics, dict):
            v = getattr(metrics, n, None)
            if v is not None:
                return v
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


_WALL_NAMES = {
    "call": ("call_wall", "call_wall_strike", "max_call_oi_strike",
             "resistance_strike", "resistance"),
    "put": ("put_wall", "put_wall_strike", "max_put_oi_strike",
            "support_strike", "support"),
}


def _wall(metrics, side: str):
    v = _num(_mget(metrics, *_WALL_NAMES[side]))
    if v is not None:
        return v
    nest = _mget(metrics, "walls", "oi_walls")
    if isinstance(nest, dict):
        alt = "resistance" if side == "call" else "support"
        for k in (side, f"{side}_wall", alt):
            v = _num(nest.get(k))
            if v is not None:
                return v
    return None


def _fget(fp, *names, default=None):
    if fp is None:
        return default
    if isinstance(fp, dict):
        for n in names:
            if fp.get(n) is not None:
                return fp[n]
        return default
    for n in names:
        v = getattr(fp, n, None)
        if v is not None:
            return v
    td = getattr(fp, "to_dict", None)
    if callable(td):
        try:
            d = td()
            for n in names:
                if isinstance(d, dict) and d.get(n) is not None:
                    return d[n]
        except Exception:
            pass
    return default


def _conf_weight(fp) -> float:
    c = _fget(fp, "confidence", "conf")
    s = getattr(c, "name", None)
    if s is None:
        s = c if isinstance(c, str) else ("" if c is None else str(c))
    s = str(s).upper().replace("CONFIDENCE.", "").strip()
    if s in CONF_WEIGHTS:
        return CONF_WEIGHTS[s]
    n = _num(c)
    if n is not None:
        return max(0.0, min(1.0, n if n <= 1.0 else n / 100.0))
    return CONF_DEFAULT


def _dir_sign(fp) -> int:
    d = str(_fget(fp, "direction", "bias", "dir") or "").lower()
    if "bull" in d or d in ("up", "long", "+1", "1"):
        return 1
    if "bear" in d or d in ("down", "short", "-1"):
        return -1
    return 0


def _kind(fp) -> str:
    return str(_fget(fp, "kind", "type", "name") or "").lower()


def chain_atm_iv(chain):
    """Mean of the ATM CE/PE implied vols on a ChainSnapshot, else None."""
    if chain is None or not getattr(chain, "strikes", None):
        return None
    try:
        k = chain.atm_strike()
        row = next((r for r in chain.strikes if r.strike == k), None)
        if row is None:
            return None
        ivs = [leg.iv for leg in (row.ce, row.pe)
               if leg is not None and _num(leg.iv)]
        return (sum(ivs) / len(ivs)) if ivs else None
    except Exception:
        return None


# ── the View ────────────────────────────────────────────────────────────
@dataclass
class View:
    bias: float                       # [-1, 1]
    vol_mult: float                   # 1.15 / 0.85 / 1.0
    range_conviction: float           # [0, 1]
    rationale: list[str] = field(default_factory=list)
    # density context (may be None -> honest fallbacks used, flagged above)
    spot: float | None = None
    atm_iv: float | None = None
    t_years: float | None = None
    put_wall: float | None = None
    call_wall: float | None = None
    iv_basis: str = "none"

    # ── terminal-price density helpers ─────────────────────────────────
    def sigma(self) -> float:
        iv = self.atm_iv if (self.atm_iv and self.atm_iv > 0) else DEFAULT_SIGMA
        return iv * self.vol_mult

    def density_params(self) -> tuple[float, float]:
        """(mu, sd) of ln(S_T): mean-shifted lognormal per the rulebook."""
        T = self.t_years if (self.t_years and self.t_years > 0) else DEFAULT_T
        sig = self.sigma()
        sd = sig * math.sqrt(T)
        mu = (math.log(self.spot) + (RISK_FREE - 0.5 * sig * sig) * T
              + self.bias * BIAS_MU_COEF * sd)
        return mu, sd

    def pdf_weights(self, grid) -> np.ndarray:
        """Normalized probability weights (sum 1) for a terminal S-grid.

        Mean-shifted lognormal; when range_conviction w > 0 and both walls
        are known, mixture with a between-walls truncated component at
        weight w * RANGE_MIX_COEF.
        """
        grid = np.asarray(grid, dtype=float)
        n = grid.size
        if n == 0:
            return np.zeros(0)
        uniform = np.full(n, 1.0 / n)
        if not self.spot or self.spot <= 0:
            return uniform
        mu, sd = self.density_params()
        if not (sd > 0) or not math.isfinite(mu):
            return uniform
        base = np.zeros(n)
        pos = grid > 0
        g = grid[pos]
        base[pos] = np.exp(-((np.log(g) - mu) ** 2) / (2.0 * sd * sd)) / (g * sd)
        s = base.sum()
        if not np.isfinite(s) or s <= 0:
            return uniform
        base = base / s
        w = self.range_conviction
        if (w > 0 and self.put_wall and self.call_wall
                and self.put_wall < self.call_wall):
            mask = (grid >= self.put_wall) & (grid <= self.call_wall)
            if mask.any():
                trunc = np.where(mask, base, 0.0)
                ts = trunc.sum()
                if ts > 0:
                    m = min(1.0, w * RANGE_MIX_COEF)
                    base = (1.0 - m) * base + m * (trunc / ts)
        tot = base.sum()
        return base / tot if tot > 0 else uniform

    def to_dict(self) -> dict:
        return {"bias": round(float(self.bias), 3),
                "vol_mult": round(float(self.vol_mult), 3),
                "range_conviction": round(float(self.range_conviction), 3),
                "rationale": list(self.rationale)}


# ── builder ─────────────────────────────────────────────────────────────
def build_view(metrics, footprints, chain=None) -> View:
    """metrics + footprints (+ optional ChainSnapshot for fallbacks) -> View.

    Any missing input degrades to a neutral reading and is flagged in
    `rationale`; nothing is fabricated.
    """
    fps = list(footprints) if footprints else []
    rationale: list[str] = []

    spot = _num(_mget(metrics, "spot", "spot_price", "underlying"))
    if spot is None and chain is not None:
        spot = _num(getattr(chain, "spot", None))

    atm_iv = _num(_mget(metrics, "atm_iv", "iv_atm", "atm_implied_vol"))
    if atm_iv is None:
        atm_iv = chain_atm_iv(chain)

    t_years = _num(_mget(metrics, "t_years", "tte", "time_to_expiry"))
    if t_years is None:
        dte = _num(_mget(metrics, "dte", "dte_days", "days_to_expiry"))
        if dte is not None:
            t_years = max(dte, 0.0) / 365.0
    if t_years is None and chain is not None and getattr(chain, "expiry_epoch", None):
        t_years = years_to_expiry(chain.expiry_epoch, time.time())

    call_wall = _wall(metrics, "call")
    put_wall = _wall(metrics, "put")
    max_pain = _num(_mget(metrics, "max_pain", "maxpain", "max_pain_strike"))
    iv_pct = _num(_mget(metrics, "iv_percentile", "atm_iv_percentile",
                        "iv_pctile", "iv_rank"))
    call_conc = _num(_mget(metrics, "call_concentration", "concentration_call",
                           "conc_call", "call_oi_concentration"))
    put_conc = _num(_mget(metrics, "put_concentration", "concentration_put",
                          "conc_put", "put_oi_concentration"))
    generic_conc = _num(_mget(metrics, "concentration", "oi_concentration"))
    gflip = _num(_mget(metrics, "gex_flip", "gamma_flip", "gex_flip_level",
                       "flip_level"))

    # ── bias b in [-1, 1] ───────────────────────────────────────────────
    fp_score, n_dir = 0.0, 0
    unwind_seen, condor_w, straddle_w = False, 0.0, 0.0
    for fp in fps:
        kind = _kind(fp)
        if "rollover" in kind or "rollout" in kind:
            continue                                   # neutral roll: excluded
        cw = _conf_weight(fp)
        if "unwind" in kind:
            unwind_seen = True
            continue
        if "condor" in kind or "pinning" in kind or "pin" == kind:
            condor_w = max(condor_w, cw)
        if "straddle" in kind:
            straddle_w = max(straddle_w, cw)
        d = _dir_sign(fp)
        if d:
            if "pcr" in kind:
                cw *= PCR_MODIFIER_SCALE               # modifier, not standalone
            fp_score += d * cw
            n_dir += 1
    if _mget(metrics, "unwind_flags", "wall_unwinds", "unwind"):
        unwind_seen = True

    parts: list[tuple[float, float]] = []
    if fps:
        fp_comp = max(-1.0, min(1.0, fp_score / FP_SAT))
        parts.append((fp_comp, W_FP))
        rationale.append(f"footprint bias {fp_comp:+.2f} "
                         f"({n_dir} directional footprint(s))")
    if (spot and call_wall and put_wall and call_wall > put_wall):
        up, dn = call_wall - spot, spot - put_wall
        if up + dn > 0:
            wall_comp = max(-1.0, min(1.0, (up - dn) / (up + dn)))
            parts.append((wall_comp, W_WALL))
            rationale.append(
                f"wall headroom asymmetry {wall_comp:+.2f} "
                f"(call wall {call_wall:g} / put wall {put_wall:g})")
    if spot and max_pain:
        pain_comp = max(-1.0, min(1.0, (max_pain / spot - 1.0) / MAXPAIN_SAT))
        parts.append((pain_comp, W_PAIN))
        rationale.append(f"max-pain pull {pain_comp:+.2f} "
                         f"(max pain {max_pain:g} vs spot {spot:g})")
    if parts:
        tot_w = sum(w for _, w in parts)
        bias = max(-1.0, min(1.0, sum(c * w for c, w in parts) / tot_w))
    else:
        bias = 0.0
        rationale.append("insufficient data for a directional view — "
                         "bias set neutral")

    # ── vol multiplier ──────────────────────────────────────────────────
    if iv_pct is not None:
        if iv_pct <= IV_PCTL_CHEAP:
            vol_mult, tag = IV_CHEAP_MULT, "iv_cheap"
        elif iv_pct >= IV_PCTL_RICH:
            vol_mult, tag = IV_RICH_MULT, "iv_rich"
        else:
            vol_mult, tag = 1.0, "normal"
        iv_basis = f"percentile {iv_pct:.0f}"
        rationale.append(f"vol view {tag} (IV percentile {iv_pct:.0f})")
    elif atm_iv is not None:
        if atm_iv < IV_ABS_CHEAP:
            vol_mult, tag = IV_CHEAP_MULT, "iv_cheap"
        elif atm_iv > IV_ABS_RICH:
            vol_mult, tag = IV_RICH_MULT, "iv_rich"
        else:
            vol_mult, tag = 1.0, "normal"
        iv_basis = f"absolute {atm_iv:.1%}"
        rationale.append(f"vol view {tag} (ATM IV {atm_iv:.1%}, "
                         "no percentile history — absolute bands)")
    else:
        vol_mult, iv_basis = 1.0, "none"
        rationale.append("ATM IV unavailable — neutral vol view, density "
                         f"uses default sigma {DEFAULT_SIGMA:.0%} (degraded)")

    # ── range conviction w in [0, 1] ────────────────────────────────────
    w_range = 0.0
    if (spot and put_wall and call_wall and put_wall < spot < call_wall):
        w_range += W_RANGE_WALLS
        rationale.append(f"spot inside walls {put_wall:g}-{call_wall:g} "
                         f"(+{W_RANGE_WALLS:.2f} range)")
    concs = [c for c in (call_conc, put_conc) if c is not None]
    if not concs and generic_conc is not None:
        concs = [generic_conc]
    if concs:
        conc = sum(concs) / len(concs)
        add = W_RANGE_CONC * max(0.0, min(1.0, conc / CONC_SAT))
        if add > 0:
            w_range += add
            rationale.append(f"top-2 OI concentration {conc:.0%} "
                             f"(+{add:.2f} range/pinning)")
    if condor_w > 0:
        w_range += W_RANGE_CONDOR * condor_w
        rationale.append(f"condor/pinning footprint "
                         f"(+{W_RANGE_CONDOR * condor_w:.2f} range)")
    if straddle_w > 0:
        w_range -= W_RANGE_STRADDLE * straddle_w
        rationale.append(f"straddle-build footprint = expansion risk "
                         f"(-{W_RANGE_STRADDLE * straddle_w:.2f} range)")
    if gflip is not None and spot and spot > gflip:
        w_range += W_RANGE_GAMMA
        rationale.append(f"spot above gamma flip {gflip:g} — dealer "
                         f"long-gamma zone (+{W_RANGE_GAMMA:.2f} range)")
    if unwind_seen:
        w_range *= UNWIND_DAMP
        rationale.append("unwind flag(s) present — range conviction damped "
                         f"x{UNWIND_DAMP}")
    w_range = max(0.0, min(1.0, w_range))

    return View(bias=bias, vol_mult=vol_mult, range_conviction=w_range,
                rationale=rationale, spot=spot, atm_iv=atm_iv,
                t_years=t_years, put_wall=put_wall, call_wall=call_wall,
                iv_basis=iv_basis)


# ── synthetic fixtures (shared by the strategist self-tests) ───────────
def synthetic_chain(spot: float = 1000.0, n_side: int = 8, step: float = 20.0,
                    lot: int = 250, dte_days: float = 12.0, seed: int = 7):
    """Fake but plausible ChainSnapshot: 17 strikes around spot 1000, step 20,
    BS premiums on a mild smile, OI walls at 900 PE / 1100 CE and a deliberate
    short-iron-condor footprint (wing builds at 940/900 PE + 1060/1100 CE).
    Outermost strikes are left illiquid (OI < 500) to exercise the guard."""
    from quant.context import ChainSnapshot, OptionLeg, StrikeRow
    from quant.mathutils import bs_price
    rng = np.random.default_rng(seed)
    now = time.time()
    t = dte_days / 365.0
    snap = ChainSnapshot(symbol="TESTSTK", expiry_epoch=now + dte_days * 86400,
                         lot=lot, spot=spot)
    strikes = [spot + step * (i - n_side) for i in range(2 * n_side + 1)]
    oi_boost = {("CE", 1100.0): 9000, ("CE", 1060.0): 5200,
                ("PE", 900.0): 8500, ("PE", 940.0): 5000}
    for k in strikes:
        row = StrikeRow(strike=k)
        for is_call, name in ((True, "ce"), (False, "pe")):
            mny = (k / spot) - 1.0
            iv = 0.24 + 0.55 * mny * mny + (0.03 if (not is_call and k < spot) else 0.0)
            prem = bs_price(is_call, spot, k, t, iv)
            prem = max(0.05, round(prem / 0.05) * 0.05)
            edge = abs(k - spot) / step
            if edge >= n_side - 0.5:                      # outermost: illiquid
                oi = float(rng.integers(150, 450))
            else:
                oi = 900.0 + 2600.0 * math.exp(-edge / 3.0) \
                     + float(rng.integers(0, 400))
            oi = float(oi_boost.get(("CE" if is_call else "PE", k), oi))
            vol = oi * float(rng.uniform(0.15, 0.6))
            # two-sided quote around the fair value (~2% half-spread, 0.05
            # tick) so consumers exercise the executable bid/ask path
            half = max(0.05, round(0.02 * prem / 0.05) * 0.05)
            leg = OptionLeg(tsym=f"TESTSTK{'C' if is_call else 'P'}{int(k)}",
                            token=f"{int(k)}{'C' if is_call else 'P'}",
                            ltp=prem, oi=oi, volume=vol, iv=iv,
                            bid=max(0.05, round((prem - half) / 0.05) * 0.05),
                            ask=round((prem + half) / 0.05) * 0.05)
            setattr(row, name, leg)
        snap.strikes.append(row)
    return snap


def demo_metrics(chain=None) -> dict:
    spot = getattr(chain, "spot", 1000.0) if chain is not None else 1000.0
    return {"spot": spot, "atm_iv": 0.24, "call_wall": 1100.0,
            "put_wall": 900.0, "max_pain": 1000.0, "pcr_oi": 0.95,
            "call_concentration": 0.42, "put_concentration": 0.40,
            "dte": 12.0, "lot": getattr(chain, "lot", 250) if chain else 250}


def demo_footprints() -> list[dict]:
    return [
        {"kind": "condor_build", "direction": "neutral", "confidence": "HIGH",
         "strikes": {"ce": [1060.0, 1100.0], "pe": [900.0, 940.0]},
         "rationale": "matched wing builds 940/900 PE + 1060/1100 CE, "
                      "centre strikes flat"},
        {"kind": "single_leg_build", "direction": "bullish",
         "confidence": "MEDIUM", "strikes": [1040.0],
         "rationale": "1040 CE ΔOI +26% on 3.4x volume"},
    ]


# ── self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    ch = synthetic_chain()
    m, f = demo_metrics(ch), demo_footprints()
    v = build_view(m, f, chain=ch)
    assert -1.0 <= v.bias <= 1.0 and 0.0 <= v.range_conviction <= 1.0
    assert v.vol_mult in (IV_CHEAP_MULT, IV_RICH_MULT, 1.0)
    assert v.rationale, "rationale must not be empty"

    grid = np.linspace(750.0, 1250.0, 150)
    wts = v.pdf_weights(grid)
    assert wts.shape == grid.shape and abs(wts.sum() - 1.0) < 1e-9
    assert (wts >= 0).all()

    # bias must shift the density mean in its own direction
    bull = [{"kind": "call_vertical_build", "direction": "bullish",
             "confidence": "HIGH"}] * 2
    bear = [{"kind": "put_vertical_build", "direction": "bearish",
             "confidence": "HIGH"}] * 2
    vb = build_view(m, bull, chain=ch)
    vs = build_view(m, bear, chain=ch)
    mb = float(grid @ vb.pdf_weights(grid))
    ms = float(grid @ vs.pdf_weights(grid))
    assert vb.bias > vs.bias and mb > ms, (vb.bias, vs.bias, mb, ms)

    # range mixture concentrates mass between the walls
    inside = (grid >= 900.0) & (grid <= 1100.0)
    flat = build_view({"spot": 1000.0, "atm_iv": 0.24, "dte": 12.0}, [],
                      chain=ch)
    assert v.range_conviction > 0
    assert wts[inside].sum() > flat.pdf_weights(grid)[inside].sum()

    # vol regimes: percentile beats absolute bands
    v_cheap = build_view({**m, "iv_percentile": 10.0}, [], chain=ch)
    v_rich = build_view({**m, "iv_percentile": 92.0}, [], chain=ch)
    assert v_cheap.vol_mult == IV_CHEAP_MULT and v_rich.vol_mult == IV_RICH_MULT

    # graceful degradation: no metrics, no footprints, no chain
    v0 = build_view(None, None)
    w0 = v0.pdf_weights(grid)
    assert v0.bias == 0.0 and abs(w0.sum() - 1.0) < 1e-9

    # unwind damps range conviction
    v_unw = build_view(m, f + [{"kind": "unwind", "direction": "neutral",
                                "confidence": "HIGH"}], chain=ch)
    assert v_unw.range_conviction < v.range_conviction

    print("view.py self-test OK — bias %+.3f vol_mult %.2f w %.2f | %s"
          % (v.bias, v.vol_mult, v.range_conviction, v.rationale[0]))

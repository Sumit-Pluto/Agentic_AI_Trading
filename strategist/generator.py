"""strategist/generator.py — the strategy GENERATOR (rulebook §Strategy GENERATOR).

Creates structures rather than picking from a menu: enumerates 1–4-leg
combinations of liquid contracts on the live chain, scores each candidate by
view-conditional return on risk with friction, applies bounded-loss and
floor constraints, dedupes by payoff-shape correlation, then re-evaluates
the finalists EXACTLY via payoff.evaluate_strategy and labels them with a
shape classifier.

Vectorised throughout: per-contract expiry payoff vectors on a 150-point
S-grid (0.75–1.25 x spot), combos = signed row sums; tail safety comes from
net-calls / net-puts sign checks per enumeration family, with a check-grid
covering every strike kink plus far tails for exact piecewise-linear
max-loss. Universe capped at the 24 contracts nearest ATM so the full
enumeration stays well under ~3 s.
"""

from __future__ import annotations

import itertools
import math
import os
import time
from dataclasses import dataclass

import numpy as np

try:
    from quant.mathutils import years_to_expiry
except ModuleNotFoundError:                      # direct-script execution
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from quant.mathutils import years_to_expiry

from strategist.payoff import Leg, evaluate_strategy
from strategist.view import chain_atm_iv

# ── rulebook constants ──────────────────────────────────────────────────
OI_FLOOR = 500                 # liquidity guard: leg counts only if OI >= 500
MAX_UNIVERSE = 24              # contracts nearest ATM
GRID_POINTS = 150
GRID_LO_FRAC, GRID_HI_FRAC = 0.75, 1.25
RUPEE_LOSS_CAP = 15000.0       # max loss cap per lot (₹)
FRICTION_PER_LEG = 40.0        # ₹ round-trip cost per leg-lot
POP_FLOOR = 0.25               # quick grid-weight POP floor (aggressive base)
RR_FLOOR = 0.15                # reward:risk floor
# Executable pricing (kills the mid-price illusion): BUY legs cost the ASK,
# SELL legs earn the BID — mid stays for display only. Kept in sync with
# zeroloss.py's convention; STRAT_SLIP_FRAC adds haircut beyond the spread.
SPREAD_WIDE_FRAC = 0.25        # spread > 25% of mid -> flag "wide — verify fill"
SPREAD_HARD_FRAC = 0.60        # spread > 60% of mid -> quote unusable, skip
SPREAD_ABS_MIN = 0.50          # ₹/share: relative gates bind only above this —
                               # a 1-2 tick spread on a ₹0.10 wing is normal and
                               # fully charged by ask/bid pricing anyway; the
                               # mid-price illusion needs an ABSOLUTELY wide gap
EXTRA_SLIP = float(os.getenv("STRAT_SLIP_FRAC", "0"))

# Trader profiles: min POP is enforced BOTH on the view-weighted search POP
# and on the exact recomputed POP of the finalists (no more low-POP
# slip-throughs); 'rank' picks the final ordering objective.
PROFILES = {
    "aggressive":   {"min_pop": 0.25, "rank": "return_on_risk"},
    "balanced":     {"min_pop": 0.40, "rank": "pop_weighted"},
    "conservative": {"min_pop": 0.60, "rank": "income"},
}
DEFAULT_PROFILE = "balanced"
DEDUPE_CORR = 0.95             # payoff-vector correlation dedupe threshold
MAX_SPAN_STEPS = 6             # strike span <= 6 steps
NET_PREM_SANITY_FRAC = 0.6     # |net premium| <= 60% of spot (sanity)
MIN_MAX_LOSS_PS = 0.05         # per-share; smaller = stale-quote artefact
POOL_SIZE = 240                # survivors kept per batch / before dedupe
RR_SANITY_CAP = 30.0           # reward:risk beyond this on STALE (ltp-only)
                               # pricing = parity-violating phantom (stale
                               # last-trades from different moments jointly
                               # forming an "arbitrage") — reject it


@dataclass(frozen=True)
class Contract:
    is_call: bool
    strike: float
    premium: float             # reference MID (or LTP when stale) — display only
    tsym: str
    oi: float
    iv: float | None = None
    token: str = ""
    ltp_based: bool = False    # True = no live two-sided quote; premium is a
                               # last-trade guess, not an executable price
    bid: float = 0.0
    ask: float = 0.0
    buy_px: float = 0.0        # executable: what a BUY actually pays (ask+slip)
    sell_px: float = 0.0       # executable: what a SELL actually earns (bid-slip)
    wide: bool = False         # two-sided but spread > SPREAD_WIDE_FRAC of mid


# ── universe & payoff plumbing (reused by recommend.py) ────────────────
def build_universe(chain, cap: int | None = MAX_UNIVERSE) -> list[Contract]:
    """Liquid contracts only (OI >= OI_FLOOR, live premium), capped at the
    `cap` nearest ATM (None = no cap, e.g. for direct benchmark
    construction), deterministic order (strike, type)."""
    if chain is None or not getattr(chain, "strikes", None):
        return []
    spot = float(getattr(chain, "spot", 0.0) or 0.0)
    out: list[Contract] = []
    for row in chain.strikes:
        for leg, is_call in ((row.ce, True), (row.pe, False)):
            if leg is None:
                continue
            try:
                ltp = float(leg.ltp) if leg.ltp is not None else 0.0
                oi = float(leg.oi) if leg.oi is not None else 0.0
                bid = float(leg.bid) if getattr(leg, "bid", None) else 0.0
                ask = float(leg.ask) if getattr(leg, "ask", None) else 0.0
            except (TypeError, ValueError):
                continue
            # EXECUTABLE pricing: buys pay the ask, sells earn the bid. The
            # mid is kept for display only — averaging a ₹0.10 bid with a ₹5
            # ask fabricates a ₹2.55 leg nobody will fill (and stale LTPs from
            # different moments jointly fabricate parity-violating combos).
            wide = False
            if bid > 0 and ask >= bid:
                premium, stale = 0.5 * (bid + ask), False
                spread = ask - bid
                if (spread > SPREAD_ABS_MIN
                        and spread > SPREAD_HARD_FRAC * premium):
                    continue               # quote too wide to be executable
                wide = (spread > SPREAD_ABS_MIN
                        and spread > SPREAD_WIDE_FRAC * premium)
                buy_px = ask * (1.0 + EXTRA_SLIP)
                sell_px = bid * (1.0 - EXTRA_SLIP)
            elif ltp > 0:
                premium, stale = ltp, True
                buy_px = sell_px = ltp     # last-trade guess both ways
            else:
                continue
            if premium <= 0 or oi < OI_FLOOR:
                continue
            out.append(Contract(is_call=is_call, strike=float(row.strike),
                                premium=premium, tsym=leg.tsym or "", oi=oi,
                                iv=leg.iv, token=leg.token or "",
                                ltp_based=stale, bid=bid, ask=ask,
                                buy_px=buy_px, sell_px=sell_px, wide=wide))
    out.sort(key=lambda c: (abs(c.strike - spot), c.strike, not c.is_call))
    if cap is not None:
        out = out[:cap]
    out.sort(key=lambda c: (c.strike, not c.is_call))
    return out


def strike_step(chain) -> float:
    ks = sorted({float(r.strike) for r in getattr(chain, "strikes", [])})
    diffs = np.diff(ks)
    diffs = diffs[diffs > 0] if len(diffs) else diffs
    if len(diffs):
        return float(np.median(diffs))
    spot = float(getattr(chain, "spot", 0.0) or 0.0)
    return max(1.0, 0.02 * spot)


def grid_for(chain) -> np.ndarray:
    spot = float(chain.spot)
    return np.linspace(GRID_LO_FRAC * spot, GRID_HI_FRAC * spot, GRID_POINTS)


def _payoff_matrix(universe: list[Contract], S: np.ndarray,
                   px: np.ndarray | None = None) -> np.ndarray:
    """Row i = LONG expiry payoff per share of contract i on grid S, priced
    at px[i] (defaults to the reference mid/LTP premium)."""
    if px is None:
        px = np.array([c.premium for c in universe])
    V = np.empty((len(universe), S.size))
    for i, c in enumerate(universe):
        intr = np.maximum(S - c.strike, 0.0) if c.is_call \
            else np.maximum(c.strike - S, 0.0)
        V[i] = intr - px[i]
    return V


def legs_payoff(legs: list[Leg], S) -> np.ndarray:
    """Exact per-share expiry payoff of a leg list on grid S (numpy)."""
    S = np.asarray(S, dtype=float)
    out = np.zeros_like(S)
    for l in legs:
        intr = np.maximum(S - l.strike, 0.0) if l.is_call \
            else np.maximum(l.strike - S, 0.0)
        out += l.side * (intr - l.premium)
    return out


def contract_at(universe: list[Contract], strike: float,
                is_call: bool) -> Contract | None:
    best, bd = None, float("inf")
    for c in universe:
        if c.is_call != is_call:
            continue
        d = abs(c.strike - strike)
        if d < bd:
            best, bd = c, d
    return best


def make_legs(*specs) -> list[Leg]:
    """specs: (contract, side, qty) -> Leg list (qty via duplication so
    payoff.evaluate_strategy stays exact). Legs carry the EXECUTABLE price
    for their side — buys at the ask, sells at the bid — falling back to the
    reference premium when no two-sided quote exists."""
    legs: list[Leg] = []
    for contract, side, qty in specs:
        if contract is None:
            return []
        px = (getattr(contract, "buy_px", 0.0) if side > 0
              else getattr(contract, "sell_px", 0.0)) or contract.premium
        for _ in range(int(qty)):
            legs.append(Leg(side=1 if side > 0 else -1, is_call=contract.is_call,
                            strike=contract.strike, premium=px,
                            tsym=contract.tsym, oi=contract.oi))
    return legs


# ── enumeration families (tail-safe coefficient sets) ───────────────────
# Coefficients are side*qty per column; every set keeps net-calls >= 0 AND
# net-puts >= 0 so tails never unbound (rulebook bounded-loss rule).
_PAIR = [(1, 1), (1, -1), (-1, 1)]
_TWO_SAME = _PAIR + [(2, -1), (-1, 2)]                 # + ratio/backspread
_THREE_SAME = [(1, 1, 1), (1, 1, -1), (1, -1, 1), (-1, 1, 1),
               (1, -2, 1), (-1, 2, -1), (2, -1, -1), (-1, -1, 2)]
_FOUR_2C2P = [(a, b, c, d) for (a, b) in _PAIR for (c, d) in _PAIR]


def _cartesian(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    ra = np.repeat(A, len(B), axis=0)
    rb = np.tile(B, (len(A), 1))
    return np.hstack([ra, rb])


def _families(universe: list[Contract]) -> list[tuple[np.ndarray, list[tuple]]]:
    n = len(universe)
    ci = np.array([i for i, c in enumerate(universe) if c.is_call], dtype=int)
    pi = np.array([i for i, c in enumerate(universe) if not c.is_call],
                  dtype=int)
    fams: list[tuple[np.ndarray, list[tuple]]] = []
    fams.append((np.arange(n).reshape(-1, 1), [(1,)]))         # 1-leg long only
    for side in (ci, pi):
        if len(side) >= 2:
            idx2 = np.array(list(itertools.combinations(side.tolist(), 2)),
                            dtype=int)
            fams.append((idx2, _TWO_SAME))
        if len(side) >= 3:
            idx3 = np.array(list(itertools.combinations(side.tolist(), 3)),
                            dtype=int)
            fams.append((idx3, _THREE_SAME))                   # incl. 3+1 qty-2
    if len(ci) >= 1 and len(pi) >= 1:                          # 1C+1P long-long
        cross = _cartesian(ci.reshape(-1, 1), pi.reshape(-1, 1))
        fams.append((cross, [(1, 1)]))
    if len(ci) >= 2 and len(pi) >= 1:                          # 2C+1P (put long)
        c2 = np.array(list(itertools.combinations(ci.tolist(), 2)), dtype=int)
        fams.append((_cartesian(c2, pi.reshape(-1, 1)),
                     [(a, b, 1) for (a, b) in _PAIR]))
    if len(pi) >= 2 and len(ci) >= 1:                          # 1C+2P (call long)
        p2 = np.array(list(itertools.combinations(pi.tolist(), 2)), dtype=int)
        fams.append((_cartesian(ci.reshape(-1, 1), p2),
                     [(1, a, b) for (a, b) in _PAIR]))
    if len(ci) >= 2 and len(pi) >= 2:                          # 2C+2P family
        c2 = np.array(list(itertools.combinations(ci.tolist(), 2)), dtype=int)
        p2 = np.array(list(itertools.combinations(pi.tolist(), 2)), dtype=int)
        fams.append((_cartesian(c2, p2), _FOUR_2C2P))
    return fams


# ── shape classifier ────────────────────────────────────────────────────
def _merged(legs: list[Leg]) -> list[tuple[bool, float, int, int]]:
    agg: dict[tuple[bool, float, int], int] = {}
    for l in legs:
        key = (l.is_call, float(l.strike), 1 if l.side > 0 else -1)
        agg[key] = agg.get(key, 0) + 1
    return sorted([(k[0], k[1], k[2], q) for k, q in agg.items()],
                  key=lambda x: (not x[0], x[1]))


def classify(legs: list[Leg]) -> str:
    """Leg-pattern shape names; anything unrecognised = 'custom structure'."""
    if not legs:
        return "custom structure"
    m = _merged(legs)
    calls = sorted([x for x in m if x[0]], key=lambda x: x[1])
    puts = sorted([x for x in m if not x[0]], key=lambda x: x[1])
    n = len(m)

    if n == 1:
        is_call, _, side, qty = m[0]
        base = f"{'long' if side > 0 else 'short'} {'call' if is_call else 'put'}"
        return base + (" (2 lots)" if qty == 2 else "")

    if n == 2 and (len(calls) == 2 or len(puts) == 2):
        t = "call" if len(calls) == 2 else "put"
        (_, k1, s1, q1), (_, k2, s2, q2) = calls if len(calls) == 2 else puts
        if q1 == q2 == 1:
            if s1 > 0 and s2 < 0:          # long lower + short higher
                return f"bull {t} spread"
            if s1 < 0 and s2 > 0:          # short lower + long higher
                return f"bear {t} spread"
            return "custom structure"
        if t == "call":
            if s1 < 0 and s2 > 0 and q2 == 2:
                return "call ratio backspread"
            if s1 > 0 and q1 == 2 and s2 < 0:
                return "call ratio spread"
        else:
            if s2 < 0 and s1 > 0 and q1 == 2:
                return "put ratio backspread"
            if s2 > 0 and q2 == 2 and s1 < 0:
                return "put ratio spread"
        return "custom structure"

    if n == 2 and len(calls) == 1 and len(puts) == 1:
        (_, kc, sc, qc), (_, kp, sp, qp) = calls[0], puts[0]
        if qc == qp:
            if sc > 0 and sp > 0:
                if kc == kp:
                    return "long straddle"
                return "long strangle" if kp < kc else "long guts"
            if sc < 0 and sp < 0:
                return "short straddle" if kc == kp else "short strangle"
            return "risk reversal"
        return "custom structure"

    if n == 3 and (len(calls) == 3 or len(puts) == 3):
        t = "call" if len(calls) == 3 else "put"
        rows = calls if len(calls) == 3 else puts
        (_, k1, s1, q1), (_, k2, s2, q2), (_, k3, s3, q3) = rows
        if (q1, q2, q3) == (1, 2, 1):
            wings_equal = abs((k2 - k1) - (k3 - k2)) < 1e-4
            if (s1, s2, s3) == (1, -1, 1):
                return (f"long {t} butterfly" if wings_equal
                        else f"broken-wing {t} butterfly")
            if (s1, s2, s3) == (-1, 1, -1):
                return (f"short {t} butterfly" if wings_equal
                        else f"short broken-wing {t} butterfly")
        return "custom structure"

    if n == 4 and len(calls) == 2 and len(puts) == 2 \
            and all(x[3] == 1 for x in m):
        (_, p1, sp1, _), (_, p2, sp2, _) = puts
        (_, c1, sc1, _), (_, c2, sc2, _) = calls
        if (sp1, sp2, sc1, sc2) == (1, -1, -1, 1) and p2 <= c1:
            return "iron butterfly" if p2 == c1 else "iron condor"
        if (sp1, sp2, sc1, sc2) == (-1, 1, 1, -1) and p2 <= c1:
            return ("reverse iron butterfly" if p2 == c1
                    else "reverse iron condor")
        return "custom structure"

    if n == 4 and (len(calls) == 4 or len(puts) == 4) \
            and all(x[3] == 1 for x in m):
        t = "call" if len(calls) == 4 else "put"
        rows = calls if len(calls) == 4 else puts
        sides = tuple(x[2] for x in rows)
        k = [x[1] for x in rows]
        if sides == (1, -1, -1, 1):
            equal = abs((k[1] - k[0]) - (k[3] - k[2])) < 1e-4
            return f"{t} condor" if equal else f"broken-wing {t} condor"
        if sides == (-1, 1, 1, -1):
            return f"short {t} condor"
        return "custom structure"

    return "custom structure"


# ── candidate scoring & selection ───────────────────────────────────────
def _pdf_weights(view, S: np.ndarray) -> np.ndarray:
    w = None
    if view is not None and hasattr(view, "pdf_weights"):
        try:
            w = np.asarray(view.pdf_weights(S), dtype=float)
        except Exception:
            w = None
    if (w is None or w.shape != S.shape or not np.isfinite(w).all()
            or w.sum() <= 0 or (w < 0).any()):
        w = np.full(S.size, 1.0 / S.size)
    return w / w.sum()


def _dedupe(pool: list, top_n: int) -> list:
    """Greedy keep-best by payoff-vector correlation > DEDUPE_CORR."""
    kept, units = [], []
    for cand in pool:
        r = cand[7]
        rn = r - r.mean()
        nrm = float(np.linalg.norm(rn))
        u = rn / nrm if nrm > 1e-12 else None
        dup = False
        for ku in units:
            if u is not None and ku is not None:
                if float(u @ ku) > DEDUPE_CORR:
                    dup = True
                    break
            elif u is None and ku is None:
                dup = True
                break
        if not dup:
            kept.append(cand)
            units.append(u)
            if len(kept) >= top_n:
                break
    return kept


def _legs_from(universe: list[Contract], idx_row, coef) -> list[Leg]:
    specs = []
    for j, cf in enumerate(coef):
        c = universe[int(idx_row[j])]
        specs.append((c, 1 if cf > 0 else -1, abs(int(cf))))
    return make_legs(*specs)


def _alignment_note(view, e_lot: float, friction: float) -> str:
    b = float(getattr(view, "bias", 0.0) or 0.0)
    vm = float(getattr(view, "vol_mult", 1.0) or 1.0)
    wr = float(getattr(view, "range_conviction", 0.0) or 0.0)
    head = (f"view: bias {b:+.2f}, vol x{vm:.2f}, range-w {wr:.2f} -> "
            f"E[P&L|view] ₹{e_lot:,.0f}/lot before ₹{friction:.0f} friction")
    rat = getattr(view, "rationale", None)
    if rat:
        head += " | " + "; ".join(str(x) for x in list(rat)[:2])
    return head


def generate(chain, metrics, view, top_n: int = 5,
             profile: str = DEFAULT_PROFILE) -> list[dict]:
    """Enumerate + score + dedupe + exact-evaluate. Returns up to top_n dicts
    (StrategyResult.to_dict() + label/expected_pnl_per_lot/score/
    view_alignment). Empty list when the chain is unusable — never fabricates.

    profile: 'conservative' (POP>=60%, income-style ranking), 'balanced'
    (POP>=40%, probability-weighted return on risk — default), 'aggressive'
    (POP>=25%, pure expected-return-on-risk)."""
    prof = PROFILES.get(str(profile).lower(), PROFILES[DEFAULT_PROFILE])
    min_pop = prof["min_pop"]
    universe = build_universe(chain)
    if not universe:
        return []
    spot = float(chain.spot)
    lot = max(1, int(getattr(chain, "lot", 0) or 1))
    cap_ps = RUPEE_LOSS_CAP / lot
    S = grid_for(chain)
    w = _pdf_weights(view, S)

    K = np.array([c.strike for c in universe])
    prem_buy = np.array([getattr(c, "buy_px", 0.0) or c.premium
                         for c in universe])
    prem_sell = np.array([getattr(c, "sell_px", 0.0) or c.premium
                          for c in universe])
    ks = np.unique(K)
    chk = np.unique(np.concatenate(
        [ks, [0.5 * ks.min(), S[0], S[-1], 1.6 * ks.max()]]))
    # side-aware payoff matrices: a LONG leg is bought at the ask, a SHORT leg
    # is written at the bid — the enumeration picks per coefficient sign, so
    # every combo is priced as it would actually FILL, not at fantasy mid.
    VA, VB = _payoff_matrix(universe, S, prem_buy), \
        _payoff_matrix(universe, S, prem_sell)
    VAc, VBc = _payoff_matrix(universe, chk, prem_buy), \
        _payoff_matrix(universe, chk, prem_sell)
    step = strike_step(chain)
    t_years = years_to_expiry(chain.expiry_epoch, time.time())

    pool: list[tuple] = []
    for idx, coefs in _families(universe):
        Km = K[idx]
        span_ok = (Km.max(axis=1) - Km.min(axis=1)) \
            <= MAX_SPAN_STEPS * step + 1e-9
        if not span_ok.any():
            continue
        idxs = idx[span_ok]
        for coef in coefs:
            c = np.asarray(coef, dtype=float)
            P = np.zeros((len(idxs), S.size))
            Pc = np.zeros((len(idxs), chk.size))
            net_prem = np.zeros(len(idxs))
            for j in range(idxs.shape[1]):
                col = idxs[:, j]
                if c[j] > 0:                       # buy at ask
                    P += c[j] * VA[col]
                    Pc += c[j] * VAc[col]
                    net_prem += c[j] * prem_buy[col]
                else:                              # sell at bid
                    P += c[j] * VB[col]
                    Pc += c[j] * VBc[col]
                    net_prem += c[j] * prem_sell[col]
            max_loss = np.maximum(0.0, -Pc.min(axis=1))
            max_prof = np.maximum(P.max(axis=1), Pc.max(axis=1))
            E = P @ w
            friction = FRICTION_PER_LEG * float(np.abs(c).sum())
            # a "win" must clear the round-trip costs, not merely 0
            pop = (P > friction / lot).astype(float) @ w
            with np.errstate(divide="ignore", invalid="ignore"):
                score = np.where(max_loss > 0,
                                 (E * lot - friction) / (max_loss * lot),
                                 -np.inf)
            ok = ((max_loss >= MIN_MAX_LOSS_PS) & (max_loss <= cap_ps)
                  & (pop >= min_pop) & (max_prof >= RR_FLOOR * max_loss)
                  & (np.abs(net_prem) <= NET_PREM_SANITY_FRAC * spot)
                  & (score > 0))
            if not ok.any():
                continue
            sel = np.nonzero(ok)[0]
            if sel.size > POOL_SIZE:
                sel = sel[np.argsort(score[sel])[::-1][:POOL_SIZE]]
            for i in sel:
                pool.append((float(score[i]), float(E[i]), float(pop[i]),
                             float(max_loss[i]), float(max_prof[i]),
                             idxs[i].copy(), coef, P[i].copy()))
    if not pool:
        return []
    pool.sort(key=lambda x: -x[0])
    # over-select finalists: the exact-POP post-filter below will thin them
    finalists = _dedupe(pool[:4 * POOL_SIZE], max(top_n * 4, 20))

    atm = chain_atm_iv(chain)
    candidates: list[dict] = []
    for score, E, pop, ml, mp, idx_row, coef, _row in finalists:
        legs = _legs_from(universe, idx_row, coef)
        label = classify(legs)
        n_lots = sum(abs(int(q)) for q in coef)
        friction = FRICTION_PER_LEG * n_lots
        # exact re-evaluation: fat-tailed, friction-aware, at the LEGS' own
        # IVs (first-order smile) — see payoff.evaluate_strategy
        result = evaluate_strategy(
            label, legs, lot, spot, atm, t_years,
            leg_ivs=[universe[int(j)].iv for j in idx_row],
            friction_rupees=friction)
        stale = any(universe[int(j)].ltp_based for j in idx_row)
        wide = any(getattr(universe[int(j)], "wide", False) for j in idx_row)
        rr_exact = result.reward_risk
        # stale-price sanity: an absurd RR built on last-trade guesses is a
        # parity-violating phantom (the ABB/guts-condor class) — reject it
        if stale and rr_exact is not None and rr_exact > RR_SANITY_CAP:
            continue
        if stale and result.max_loss * lot < 3 * friction:
            continue                      # near-arb on stale prices = fake
        d = result.to_dict()
        d.update({
            "label": label + (" (LTP-priced — verify)" if stale else
                              (" (wide spread — verify fill)" if wide else "")),
            "ltp_based": stale,
            "wide_spread": wide,
            "expected_pnl_per_lot": round(float(E) * lot, 0),
            "score": round(float(score), 4),
            "pop_view_pct": round(100.0 * float(pop), 1),
            "friction_rupees": round(friction, 0),
            "view_alignment": _alignment_note(view, float(E) * lot, friction),
        })
        # exact POP governs the floor; fall back to view-POP if IV missing
        pop_exact = (result.pop if result.pop is not None else float(pop))
        rr = result.reward_risk or 0.0
        d["_pop_exact"] = pop_exact
        if prof["rank"] == "pop_weighted":
            d["_rank"] = float(score) * pop_exact
        elif prof["rank"] == "income":
            d["_rank"] = pop_exact * min(rr, 1.5)
        else:                                   # return_on_risk (aggressive)
            d["_rank"] = float(score)
        candidates.append(d)

    passing = [d for d in candidates if d["_pop_exact"] >= min_pop]
    relaxed = False
    if not passing:                             # honest fallback, flagged
        passing, relaxed = candidates, True
    passing.sort(key=lambda d: -d["_rank"])
    out = passing[:top_n]
    for d in out:
        d["profile"] = str(profile).lower()
        d["rank_score"] = round(d.pop("_rank"), 4)
        d.pop("_pop_exact", None)
        if relaxed:
            d["note"] = (f"no structure met the {int(min_pop * 100)}% POP "
                         "floor for this profile — closest shown")
    return out


# ── self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    from strategist.view import (build_view, demo_footprints, demo_metrics,
                                 synthetic_chain)

    ch = synthetic_chain()                      # deliberate condor footprint
    uni = build_universe(ch)
    assert 0 < len(uni) <= MAX_UNIVERSE
    assert all(c.oi >= OI_FLOOR and c.premium > 0 for c in uni)

    # executable pricing: a BUY fills at the ask, a SELL at the bid — never mid
    two_sided = next(c for c in uni if not c.ltp_based)
    lb, ls = make_legs((two_sided, 1, 1)), make_legs((two_sided, -1, 1))
    assert abs(lb[0].premium - two_sided.ask * (1 + EXTRA_SLIP)) < 1e-9
    assert abs(ls[0].premium - two_sided.bid * (1 - EXTRA_SLIP)) < 1e-9
    assert lb[0].premium > two_sided.premium > ls[0].premium  # mid is between

    m, f = demo_metrics(ch), demo_footprints()
    view = build_view(m, f, chain=ch)

    t0 = time.time()
    res = generate(ch, m, view, top_n=5)
    dt = time.time() - t0
    assert dt < 3.0, f"enumeration too slow: {dt:.2f}s"
    assert isinstance(res, list) and 1 <= len(res) <= 5, len(res)
    for r in res:
        for key in ("label", "expected_pnl_per_lot", "score",
                    "view_alignment", "legs", "max_loss_per_lot", "pop_pct",
                    "pop_classic_pct", "friction_rupees"):
            assert key in r, key
        assert r["max_loss_per_lot"] is not None
        assert r["max_loss_per_lot"] <= RUPEE_LOSS_CAP + 1e-6
        assert 1 <= len(r["legs"]) <= 4
        assert not r["ltp_based"], "synthetic chain is two-sided now"

    # view=None degrades to uniform weights without blowing up
    res_uniform = generate(ch, m, None, top_n=3)
    assert isinstance(res_uniform, list)

    # empty / unusable chain -> honest empty list
    assert generate(None, m, view) == []

    # classifier spot checks
    c950p = contract_at(uni, 950, False)
    c900p = contract_at(uni, 900, False)
    c1060c = contract_at(uni, 1060, True)
    c1100c = contract_at(uni, 1100, True)
    atmc = contract_at(uni, 1000, True)
    atmp = contract_at(uni, 1000, False)
    assert classify(make_legs((c950p, -1, 1), (c900p, 1, 1))) == "bull put spread"
    assert classify(make_legs((c900p, 1, 1), (c950p, -1, 1),
                              (c1060c, -1, 1), (c1100c, 1, 1))) == "iron condor"
    assert classify(make_legs((atmc, 1, 1), (atmp, 1, 1))) == "long straddle"
    c1020c = contract_at(uni, 1020, True)
    c1080c = contract_at(uni, 1080, True)
    bwb = classify(make_legs((atmc, 1, 1), (c1020c, -1, 2), (c1080c, 1, 1)))
    assert bwb == "broken-wing call butterfly", bwb
    fly = classify(make_legs((atmc, 1, 1), (c1020c, -1, 2),
                             (contract_at(uni, 1040, True), 1, 1)))
    assert fly == "long call butterfly", fly

    print(f"generator.py self-test OK — {len(res)} candidates in {dt:.2f}s; "
          f"top: {res[0]['label']} score {res[0]['score']} "
          f"E ₹{res[0]['expected_pnl_per_lot']:,.0f}/lot")

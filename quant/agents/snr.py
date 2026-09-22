"""Support & Resistance agent family (spec: docs/specs/snr.json).

Leaves:
  * oi_walls     — put/call OI walls with ATR-normalized headroom scoring and
                   a strict per-strike liquidity guard.
  * gamma_levels — GEX walls + gamma-flip regime from Black-Scholes gammas on
                   chain-implied IVs (naive SqueezeMetrics dealer convention).
  * max_pain     — argmin of aggregate option-writer pain, DTE-scaled pull.
  * pivot_levels — floor-trader pivots + prior-day H/L (options-free fallback).
  * dom          — future stub (order-book depth levels).

Conventions (spec notes):
  * ATR_ref = ATR(14) on daily candles (Wilder); fallback ATR(14, 5m)*8.66
    (sqrt of 75 five-minute bars per NSE session). Single normalizer shared by
    all leaves so scores are comparable across stocks of any price.
  * Spec 'flags' ('no_chain'/'illiquid_chain'/'no_iv') map to (None, reason)
    returns here: the QuantAgent branch rollup renormalizes weights onto the
    children that produced information (worst case pivot_levels alone), which
    is exactly the spec's fallback protocol. The flag is recorded in
    ctx.meta['snr.flags'] for the orchestrator.
  * OI UNIT CHECK (one-time, per deployment): Shoonya NFO OI is assumed to be
    in units/shares. If the feed actually returns NSE-style contract counts,
    multiply OI by lot_size wherever it is compared to lot-based thresholds
    and inside GEX.
  * GEX dealer-sign caveats (SqueezeMetrics naive convention: dealers long
    calls, short puts): (a) OI carries no long/short side information;
    (b) on single stocks retail speculative call BUYING can invert the call
    sign — treat GEX as a heuristic, not ground truth (hence modest weight);
    (c) monthly stock-option IVs are noisy, so per-strike GEX magnitude
    matters less than the sign pattern and strike locations.
"""

from __future__ import annotations

import datetime as _dt
import math

from ..base import QuantAgent
try:                                           # C++ kernel; else Python reference
    from qcore import bs_gamma, years_to_expiry
except ImportError:
    from ..mathutils import bs_gamma, years_to_expiry

# ── shared params ───────────────────────────────────────────────────────────
ATR_5M_SCALE = 8.66              # sqrt(75) 5-min bars per NSE session

# oi_walls params
OIW_MIN_OI_LOTS = 3
OIW_MAX_REL_SPREAD = 0.25        # depth not in snapshot; ltp>0 used as proxy
OIW_ROOM_FULL = 1.5              # daily ATRs for full headroom
OIW_SUP_FULL = 1.5
OIW_STRENGTH_MIN = 1.5

# gamma_levels params
GEX_WINDOW = 6                   # strikes each side of ATM
GEX_INTRINSIC_PAD = 0.05
GEX_MIN_IV_STRIKES = 4
GEX_T_FLOOR = 0.5 / 365.0
GEX_DTE_DAMP_T = 2.0 / 365.0     # <=2 days to expiry -> damp toward 50
GEX_DAMP_FACTOR = 0.7

# max_pain params
MP_PULL_SCALE = 1.5              # ATR_ref multiples for full pull
MP_DTE_FULL = 15.0
MP_RELEVANCE_FLOOR = 0.2
MP_MIN_CANDIDATE_STRIKES = 5
MP_MIN_TOTAL_OI_LOTS = 20

# pivot_levels params
PIV_ROOM_FULL = 1.0
PIV_SUP_FULL = 0.75
PIV_CLUSTER_TOL = 0.15
PIV_CLUSTER_ROOM_PENALTY = 0.85
PIV_CLUSTER_SUPPORT_BONUS = 1.15
PIV_MISSING_SUPPORT = 0.30


# ── shared helpers ──────────────────────────────────────────────────────────
def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


_COLMAP = {"into": "open", "inth": "high", "intl": "low", "intc": "close"}


def _std_ohlc(df):
    """Return a frame with float high/low/close columns, or None."""
    if df is None or len(df) == 0:
        return None
    d = df
    if not {"high", "low", "close"} <= set(map(str, d.columns)):
        d = d.rename(columns=_COLMAP)
        if not {"high", "low", "close"} <= set(map(str, d.columns)):
            return None
    try:
        return d[["high", "low", "close"]].astype(float)
    except Exception:
        return None


def _atr_ref(ctx) -> float | None:
    """Daily ATR(14) (Wilder); fallback 5m ATR(14)*sqrt(75). Cached per ctx."""
    cached = ctx.meta.get("snr.atr_ref")
    if cached is not None:
        return cached
    val = None
    try:
        d = _std_ohlc(ctx.daily)
        if d is not None and len(d) >= 15:
            from signals.indicators import atr
            v = float(atr(d, 14).iloc[-1])
            if v == v and v > 0:
                val = v
    except Exception:
        val = None
    if val is None:
        try:
            a = ctx.atr14
            if a and a > 0:
                val = float(a) * ATR_5M_SCALE
        except Exception:
            val = None
    if val is not None:
        ctx.meta["snr.atr_ref"] = val
    return val


def _flag(ctx, flag: str):
    ctx.meta.setdefault("snr.flags", []).append(flag)


def _sorted_rows(chain):
    return sorted(chain.strikes, key=lambda r: r.strike)


def _strike_step(strikes: list[float]) -> float | None:
    diffs = sorted(b - a for a, b in zip(strikes, strikes[1:]) if b > a)
    if not diffs:
        return None
    return diffs[len(diffs) // 2]


# ── 1. OI Walls ─────────────────────────────────────────────────────────────
class OIWallsAgent(QuantAgent):
    key = "oi_walls"
    name = "OI Walls"
    description = ("Put/call OI support & resistance walls, ATR-normalized "
                   "headroom scoring with per-strike liquidity guards")
    default_weight_buy = 0.35
    default_weight_sell = 0.35

    @staticmethod
    def _leg_valid(leg, lot: float) -> bool:
        """CE/PE at K valid iff OI >= min_oi_lots*lot AND traded/quoted.
        Snapshot has no bid/ask depth, so a live positive ltp stands in for
        the tight-spread alternative of the spec."""
        if leg is None or leg.oi is None or leg.oi < OIW_MIN_OI_LOTS * lot:
            return False
        if (leg.volume or 0.0) >= lot:
            return True
        return bool(leg.ltp and leg.ltp > 0)

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None or len(chain.strikes) < 4:
            _flag(ctx, "no_chain")
            return None, "no_chain: option chain unavailable/<4 strikes"
        atr_ref = _atr_ref(ctx)
        if not atr_ref:
            return None, "no ATR reference (daily & 5m ATR unavailable)"
        lot = chain.lot or 1
        spot = chain.spot or ctx.spot
        rows = _sorted_rows(chain)

        valid_ce = [(r.strike, float(r.ce.oi)) for r in rows
                    if self._leg_valid(r.ce, lot)]
        valid_pe = [(r.strike, float(r.pe.oi)) for r in rows
                    if self._leg_valid(r.pe, lot)]
        ce_above = [(k, oi) for k, oi in valid_ce if k >= spot]
        pe_below = [(k, oi) for k, oi in valid_pe if k <= spot]
        if len(valid_ce) < 3 or len(valid_pe) < 3 or not ce_above or not pe_below:
            _flag(ctx, "illiquid_chain")
            return None, (f"illiquid_chain: valid CE={len(valid_ce)} "
                          f"PE={len(valid_pe)} (need 3+3 incl. one each side)")

        # argmax OI; ties -> strike closest to spot (nearer wall binds)
        call_wall, call_oi = max(
            ce_above, key=lambda t: (t[1], -abs(t[0] - spot)))
        put_wall, put_oi = max(
            pe_below, key=lambda t: (t[1], -abs(t[0] - spot)))

        med_ce = _median([oi for _, oi in valid_ce]) or 1.0
        med_pe = _median([oi for _, oi in valid_pe]) or 1.0
        weak_c = (call_oi / med_ce) < OIW_STRENGTH_MIN
        weak_p = (put_oi / med_pe) < OIW_STRENGTH_MIN

        d_res = (call_wall - spot) / atr_ref
        d_sup = (spot - put_wall) / atr_ref

        if ctx.direction == "BUY":
            room = _clip(d_res / OIW_ROOM_FULL, 0.0, 1.0)
            if weak_c:
                room = max(room, 0.6)
            support = _clip(1.0 - d_sup / OIW_SUP_FULL, 0.0, 1.0)
            if weak_p:
                support *= 0.6
            score = round(65.0 * room + 35.0 * support)
        else:
            room = _clip(d_sup / OIW_ROOM_FULL, 0.0, 1.0)
            if weak_p:
                room = max(room, 0.6)
            backstop = _clip(1.0 - d_res / OIW_SUP_FULL, 0.0, 1.0)
            if weak_c:
                backstop *= 0.6
            score = round(65.0 * room + 35.0 * backstop)

        detail = (f"call wall {call_wall:g} ({d_res:+.2f} ATR"
                  f"{', weak' if weak_c else ''}), put wall {put_wall:g} "
                  f"({d_sup:+.2f} ATR{', weak' if weak_p else ''})")
        return float(score), detail


# ── 2. Gamma Exposure Levels ────────────────────────────────────────────────
class GammaLevelsAgent(QuantAgent):
    key = "gamma_levels"
    name = "Gamma Levels (GEX)"
    description = ("Per-strike GEX walls and gamma-flip regime from "
                   "chain-implied IVs (naive dealer-sign convention)")
    default_weight_buy = 0.25
    default_weight_sell = 0.25

    @staticmethod
    def _usable_premium(leg, strike: float, spot: float) -> float | None:
        """Spec step 2 adapted to snapshot fields: ltp usable only if the
        option traded (v>0) and premium > intrinsic + pad."""
        if leg is None or not leg.ltp or leg.ltp <= 0:
            return None
        if (leg.volume or 0.0) <= 0:
            return None
        return float(leg.ltp)

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None or len(chain.strikes) < 4:
            _flag(ctx, "no_chain")
            return None, "no_chain: option chain unavailable/<4 strikes"
        atr_ref = _atr_ref(ctx)
        if not atr_ref:
            return None, "no ATR reference (daily & 5m ATR unavailable)"
        spot = chain.spot or ctx.spot
        T = max(years_to_expiry(chain.expiry_epoch, ctx.now), GEX_T_FLOOR)

        if not ctx.meta.get("snr.ivs_done"):
            try:
                chain.compute_ivs()
            except Exception:
                pass
            ctx.meta["snr.ivs_done"] = True

        rows = _sorted_rows(chain)
        strikes = [r.strike for r in rows]
        step = _strike_step(strikes)
        if step is None:
            return None, "degenerate chain: single strike"
        atm = chain.atm_strike()
        i_atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
        window = rows[max(0, i_atm - GEX_WINDOW): i_atm + GEX_WINDOW + 1]

        # per-leg implied sigma with skip rules
        sig_c: dict[float, float] = {}
        sig_p: dict[float, float] = {}
        atm_row = rows[i_atm]
        for r in window:
            k = r.strike
            pc = self._usable_premium(r.ce, k, spot)
            if pc is not None and pc > max(0.0, spot - k) + GEX_INTRINSIC_PAD \
                    and r.ce.iv:
                sig_c[k] = float(r.ce.iv)
            pp = self._usable_premium(r.pe, k, spot)
            if pp is not None and pp > max(0.0, k - spot) + GEX_INTRINSIC_PAD \
                    and r.pe.iv:
                sig_p[k] = float(r.pe.iv)

        n_iv = len(sig_c) + len(sig_p)
        flat = False
        if n_iv < GEX_MIN_IV_STRIKES:
            # flat-vol fallback: Brenner-Subrahmanyam ATM straddle approx
            c_atm = self._usable_premium(atm_row.ce, atm_row.strike, spot)
            p_atm = self._usable_premium(atm_row.pe, atm_row.strike, spot)
            if c_atm and p_atm and spot > 0 and T > 0:
                sflat = (c_atm + p_atm) / (0.8 * spot * math.sqrt(T))
                if sflat > 0 and math.isfinite(sflat):
                    sig_c = {r.strike: sflat for r in window}
                    sig_p = dict(sig_c)
                    flat = True
            if not flat:
                _flag(ctx, "no_iv")
                return None, (f"no_iv: only {n_iv} legs with valid IV and "
                              "no usable ATM straddle")

        # per-strike entries: same-strike IVs must match by parity, so a
        # missing side reuses the other side's sigma
        entries = []      # (K, oi_call, sigma_call, oi_put, sigma_put)
        for r in window:
            k = r.strike
            sc = sig_c.get(k) or sig_p.get(k)
            sp_ = sig_p.get(k) or sig_c.get(k)
            if sc is None and sp_ is None:
                continue
            oc = float(r.ce.oi) if (r.ce and r.ce.oi) else 0.0
            op = float(r.pe.oi) if (r.pe and r.pe.oi) else 0.0
            if oc <= 0 and op <= 0:
                continue
            entries.append((k, oc, sc, op, sp_))
        if not entries:
            _flag(ctx, "no_iv")
            return None, "no_iv: no window strike has both OI and a usable IV"

        # naive-convention GEX at current spot (rupee delta-notional / 1% move)
        gex: dict[float, float] = {}
        for k, oc, sc, op, sp_ in entries:
            g = 0.0
            if oc > 0 and sc:
                g += bs_gamma(spot, k, T, sc) * oc
            if op > 0 and sp_:
                g -= bs_gamma(spot, k, T, sp_) * op
            gex[k] = g * spot * spot * 0.01

        pos_above = [(k, v) for k, v in gex.items() if k >= spot and v > 0]
        pos_below = [(k, v) for k, v in gex.items() if k <= spot and v > 0]
        k_res = max(pos_above, key=lambda t: t[1])[0] if pos_above else None
        k_sup = max(pos_below, key=lambda t: t[1])[0] if pos_below else None

        # gamma flip: re-evaluate the aggregate GEX profile at each strike
        # level (per-strike sigmas held fixed), linear-interpolate the zero
        def g_at(x: float) -> float:
            tot = 0.0
            for k, oc, sc, op, sp_ in entries:
                if oc > 0 and sc:
                    tot += bs_gamma(x, k, T, sc) * oc
                if op > 0 and sp_:
                    tot -= bs_gamma(x, k, T, sp_) * op
            return tot * x * x * 0.01

        xs = sorted(k for k, *_ in entries)
        gvals = [(x, g_at(x)) for x in xs]
        cross = [i for i in range(len(gvals) - 1)
                 if gvals[i][1] * gvals[i + 1][1] < 0]
        if cross:
            i = min(cross, key=lambda j:
                    abs(0.5 * (gvals[j][0] + gvals[j + 1][0]) - spot))
            x0, g0 = gvals[i]
            x1, g1 = gvals[i + 1]
            flip = x0 + (0.0 - g0) * (x1 - x0) / (g1 - g0)
        elif all(v >= 0 for _, v in gvals):
            flip = xs[0] - step          # firmly positive-gamma regime
        elif all(v <= 0 for _, v in gvals):
            flip = xs[-1] + step         # firmly negative-gamma regime
        else:
            flip = min(gvals, key=lambda t: abs(t[1]))[0]

        regime = _clip(0.5 + (spot - flip) / (2.0 * atr_ref), 0.0, 1.0)
        if ctx.direction == "BUY":
            room = 0.75 if k_res is None else _clip(
                (k_res - spot) / atr_ref, 0.0, 1.0)
            sup = 0.40 if k_sup is None else _clip(
                1.0 - (spot - k_sup) / atr_ref, 0.0, 1.0)
            score = 40.0 * regime + 35.0 * room + 25.0 * sup
        else:
            regime_s = 1.0 - regime
            room = 0.75 if k_sup is None else _clip(
                (spot - k_sup) / atr_ref, 0.0, 1.0)
            shield = 0.40 if k_res is None else _clip(
                1.0 - (k_res - spot) / atr_ref, 0.0, 1.0)
            score = 40.0 * regime_s + 35.0 * room + 25.0 * shield

        damped = T <= GEX_DTE_DAMP_T
        if damped:                        # expiry pin-risk damping
            score = 50.0 + (score - 50.0) * GEX_DAMP_FACTOR
        score = round(_clip(score, 0.0, 100.0))

        detail = (f"flip {flip:.0f} (regime {regime:.2f}), "
                  f"GEX lid {k_res if k_res is not None else '—'}, "
                  f"GEX floor {k_sup if k_sup is not None else '—'}"
                  f"{', flat-vol' if flat else ''}"
                  f"{', expiry-damped' if damped else ''}")
        return float(score), detail


# ── 3. Max Pain Pull ────────────────────────────────────────────────────────
class MaxPainAgent(QuantAgent):
    key = "max_pain"
    name = "Max Pain Pull"
    description = ("Argmin of option-writer pain; pull toward max pain "
                   "scaled by days-to-expiry relevance")
    default_weight_buy = 0.10
    default_weight_sell = 0.10

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None or not chain.strikes:
            _flag(ctx, "no_chain")
            return None, "no_chain: option chain unavailable"
        atr_ref = _atr_ref(ctx)
        if not atr_ref:
            return None, "no ATR reference (daily & 5m ATR unavailable)"
        lot = chain.lot or 1
        spot = chain.spot or ctx.spot
        rows = _sorted_rows(chain)

        data = []  # (K, oi_ce, oi_pe)
        for r in rows:
            oc = float(r.ce.oi) if (r.ce and r.ce.oi) else 0.0
            op = float(r.pe.oi) if (r.pe and r.pe.oi) else 0.0
            data.append((r.strike, oc, op))
        cands = [k for k, oc, op in data if oc + op > 0]
        total_oi = sum(oc + op for _, oc, op in data)
        if len(cands) < MP_MIN_CANDIDATE_STRIKES or \
                total_oi < MP_MIN_TOTAL_OI_LOTS * lot:
            _flag(ctx, "illiquid_chain")
            return None, (f"illiquid_chain: {len(cands)} candidate strikes, "
                          f"total OI {total_oi:.0f} (< {MP_MIN_TOTAL_OI_LOTS} lots)")

        def pain(ks: float) -> float:
            return sum(oc * max(0.0, ks - k) + op * max(0.0, k - ks)
                       for k, oc, op in data)

        mp = min(cands, key=lambda ks: (pain(ks), abs(ks - spot)))
        dte = max(0.0, (chain.expiry_epoch - ctx.now) / 86400.0)
        relevance = _clip(1.0 - dte / MP_DTE_FULL, MP_RELEVANCE_FLOOR, 1.0)
        pull = _clip((mp - spot) / (MP_PULL_SCALE * atr_ref), -1.0, 1.0)

        if ctx.direction == "BUY":
            score = round(50.0 + 50.0 * pull * relevance)
        else:
            score = round(50.0 - 50.0 * pull * relevance)
        detail = (f"max pain {mp:g} vs spot {spot:g} "
                  f"(pull {pull:+.2f}, DTE {dte:.0f}d, rel {relevance:.2f})")
        return float(score), detail


# ── 4. Floor-Trader Pivot Levels ────────────────────────────────────────────
class PivotLevelsAgent(QuantAgent):
    key = "pivot_levels"
    name = "Pivot Levels"
    description = ("Floor-trader pivots + prior-day H/L from daily candles; "
                   "options-free fallback S&R")
    default_weight_buy = 0.30
    default_weight_sell = 0.30

    @staticmethod
    def _prior_day_hlc(ctx):
        """Prior completed session H, L, C. Daily series first, then a 5m
        session-aggregation fallback so the agent works whenever 5m history
        spans more than one session."""
        today = _dt.date.fromtimestamp(ctx.now)
        d = _std_ohlc(ctx.daily)
        if d is not None:
            try:
                prior = d[[dt < today for dt in d.index.date]]
                if len(prior):
                    row = prior.iloc[-1]
                    return (float(row["high"]), float(row["low"]),
                            float(row["close"]))
            except Exception:
                pass
        try:
            df = ctx.df
            dates = sorted({dt for dt in df.index.date if dt < today})
            if dates:
                mask = df.index.date == dates[-1]
                sess = df[mask]
                if len(sess):
                    return (float(sess["high"].max()),
                            float(sess["low"].min()),
                            float(sess["close"].iloc[-1]))
        except Exception:
            pass
        return None

    def compute(self, ctx):
        atr_ref = _atr_ref(ctx)
        if not atr_ref:
            return None, "no ATR reference (daily & 5m ATR unavailable)"
        hlc = self._prior_day_hlc(ctx)
        if hlc is None:
            return None, "no prior-day OHLC (daily series & 5m history today-only)"
        h, l, c = hlc
        spot = float(ctx.spot)

        p = (h + l + c) / 3.0
        levels = [p, 2 * p - l, p + (h - l), h + 2 * (p - l),      # P R1 R2 R3
                  2 * p - h, p - (h - l), l - 2 * (h - p),         # S1 S2 S3
                  h, l]                                            # PDH PDL
        levels.sort()

        # cluster-merge levels closer than cluster_tol * ATR_ref
        merged: list[tuple[float, int]] = []       # (level, count)
        run = [levels[0]]
        for lv in levels[1:]:
            if lv - run[-1] < PIV_CLUSTER_TOL * atr_ref:
                run.append(lv)
            else:
                merged.append((sum(run) / len(run), len(run)))
                run = [lv]
        merged.append((sum(run) / len(run), len(run)))

        above = [(lv, n) for lv, n in merged if lv > spot]
        below = [(lv, n) for lv, n in merged if lv < spot]
        r_star = min(above, key=lambda t: t[0]) if above else None
        s_star = max(below, key=lambda t: t[0]) if below else None
        head_up = (r_star[0] - spot) / atr_ref if r_star else None
        dist_dn = (spot - s_star[0]) / atr_ref if s_star else None

        if ctx.direction == "BUY":
            room = 1.0 if r_star is None else _clip(
                head_up / PIV_ROOM_FULL, 0.0, 1.0)
            if r_star is not None and r_star[1] >= 2:
                room *= PIV_CLUSTER_ROOM_PENALTY
            support = PIV_MISSING_SUPPORT if s_star is None else _clip(
                1.0 - dist_dn / PIV_SUP_FULL, 0.0, 1.0)
            if s_star is not None and s_star[1] >= 2:
                support = min(1.0, support * PIV_CLUSTER_SUPPORT_BONUS)
            score = round(65.0 * room + 35.0 * support)
        else:
            room = 1.0 if s_star is None else _clip(
                dist_dn / PIV_ROOM_FULL, 0.0, 1.0)
            if s_star is not None and s_star[1] >= 2:
                room *= PIV_CLUSTER_ROOM_PENALTY
            backstop = PIV_MISSING_SUPPORT if r_star is None else _clip(
                1.0 - head_up / PIV_SUP_FULL, 0.0, 1.0)
            if r_star is not None and r_star[1] >= 2:
                backstop = min(1.0, backstop * PIV_CLUSTER_SUPPORT_BONUS)
            score = round(65.0 * room + 35.0 * backstop)

        rs = (f"{r_star[0]:.1f}({'x' + str(r_star[1]) if r_star[1] >= 2 else '1'})"
              if r_star else "blue-sky")
        ss = (f"{s_star[0]:.1f}({'x' + str(s_star[1]) if s_star[1] >= 2 else '1'})"
              if s_star else "free-fall")
        detail = (f"next R {rs}"
                  f"{f' +{head_up:.2f} ATR' if head_up is not None else ''}, "
                  f"next S {ss}"
                  f"{f' -{dist_dn:.2f} ATR' if dist_dn is not None else ''}")
        return float(score), detail


# ── 5. DOM / depth (future stub) ────────────────────────────────────────────
class DOMLevelsAgent(QuantAgent):
    key = "dom"
    name = "DOM / Depth Levels"
    description = "Order-book depth support/resistance (future)"
    default_weight_buy = 0.20
    default_weight_sell = 0.20
    default_enabled = False

    def compute(self, ctx):
        return None, "future: needs order-book history — best for short term"


# ── family branch ───────────────────────────────────────────────────────────
class SNRFamily(QuantAgent):
    key = "snr"
    name = "Support & Resistance"
    description = "OI walls, gamma levels, max pain, pivots, DOM"
    default_weight_buy = 1.0
    default_weight_sell = 1.0


def build() -> QuantAgent:
    return SNRFamily([
        OIWallsAgent(),
        GammaLevelsAgent(),
        MaxPainAgent(),
        PivotLevelsAgent(),
        DOMLevelsAgent(),
    ])


# ── synthetic self-test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import time

    sys.path.insert(0, "/Users/mac/Downloads/div_4_doc/Trading_project_2.0")
    import numpy as np
    import pandas as pd

    from quant.context import ChainSnapshot, OptionLeg, StrikeRow
    from qcore import bs_price   # C++ kernel (auto-fallback to mathutils)
    from signals.indicators import atr as _atr5

    class Cfg:
        def enabled(self, key, default=True):
            return default

        def threshold(self, key):
            return 50.0

        def weight(self, key, symbol, direction, db, ds):
            return db

    def make_df(now_epoch):
        # 4 NSE sessions x 75 bars of synthetic 5m candles ending yesterday
        rng = np.random.default_rng(7)
        idx = []
        end_day = _dt.date.fromtimestamp(now_epoch) - _dt.timedelta(days=1)
        days, d = [], end_day
        while len(days) < 4:
            if d.weekday() < 5:
                days.append(d)
            d -= _dt.timedelta(days=1)
        for day in reversed(days):
            start = _dt.datetime.combine(day, _dt.time(9, 15))
            idx += [start + _dt.timedelta(minutes=5 * i) for i in range(75)]
        n = len(idx)
        close = 100.0 + np.cumsum(rng.normal(0, 0.15, n))
        high = close + rng.uniform(0.05, 0.35, n)
        low = close - rng.uniform(0.05, 0.35, n)
        open_ = close + rng.normal(0, 0.1, n)
        vol = rng.integers(1000, 9000, n).astype(float)
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": vol,
                             "oi": np.zeros(n)},
                            index=pd.DatetimeIndex(idx))

    def make_daily(df):
        day = pd.Series(df.index.date, index=df.index)
        g = df.groupby(day)
        out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                            "low": g["low"].min(), "close": g["close"].last(),
                            "volume": g["volume"].sum()})
        out.index = pd.DatetimeIndex([pd.Timestamp(d) for d in out.index])
        # pad to 20 rows so daily ATR(14) exists
        pads = []
        first = out.index[0]
        base = float(out["close"].iloc[0])
        for i in range(1, 21 - len(out)):
            pads.append(pd.DataFrame(
                {"open": base, "high": base + 1.2, "low": base - 1.2,
                 "close": base + ((-1) ** i) * 0.4, "volume": 5e5},
                index=[first - pd.Timedelta(days=i)]))
        return pd.concat(pads[::-1] + [out]).sort_index()

    def make_chain(spot, now_epoch, lot=500, dte_days=12.0, sigma=0.30):
        step = 2.5
        atm = round(spot / step) * step
        exp = now_epoch + dte_days * 86400.0
        t = dte_days / 365.0
        rows = []
        for i in range(-8, 9):
            k = atm + i * step
            coi = 3e5 if i == 2 else (1e5 + 1e4 * abs(i))     # call wall +2
            poi = 3.5e5 if i == -2 else (1e5 + 1e4 * abs(i))  # put wall  -2
            ce = OptionLeg("TESTC", "1", ltp=round(
                max(0.05, bs_price(True, spot, k, t, sigma)), 2),
                oi=coi, volume=5 * lot)
            pe = OptionLeg("TESTP", "2", ltp=round(
                max(0.05, bs_price(False, spot, k, t, sigma)), 2),
                oi=poi, volume=5 * lot)
            rows.append(StrikeRow(k, ce, pe))
        return ChainSnapshot("TEST", exp, lot, spot, rows)

    class Ctx:
        def __init__(self, direction, df, chain, daily):
            self.symbol = "TEST"
            self.direction = direction
            self.df = df
            self.spot = float(df["close"].iloc[-1])
            self.now = time.time()
            self.meta = {}
            self.chain = chain
            self.futures = None
            self.vix = None
            self.daily = daily
            self.cash_quote = None
            try:
                v = float(_atr5(df, 14).iloc[-1])
                self.atr14 = v if v == v and v > 0 else None
            except Exception:
                self.atr14 = None

    now = time.time()
    df = make_df(now)
    daily = make_daily(df)
    chain = make_chain(float(df["close"].iloc[-1]), now)
    cfg = Cfg()
    agent = build()
    print("tree:", [c.key for c in agent.walk()])

    scenarios = [
        ("full BUY", Ctx("BUY", df, chain, daily)),
        ("full SELL", Ctx("SELL", df, chain, daily)),
        ("no chain/daily BUY",
         Ctx("BUY", df, None, None)),
        ("no chain/daily SELL",
         Ctx("SELL", df, None, None)),
    ]
    failures = 0
    for label, ctx in scenarios:
        res = agent.evaluate(ctx, cfg)
        print(f"\n[{label}] family={res.score} ({res.detail})")
        for k in res.children:
            print(f"  {k.key:14s} score={k.score} avail={k.available} "
                  f"enabled={k.enabled} :: {k.detail}")
            if k.detail.startswith("error:"):
                failures += 1
        # every leaf must return without exception (error: detail = exception)
    # data-present scenarios must produce scores on all 4 live leaves
    for label, ctx in scenarios[:2]:
        res = agent.evaluate(ctx, cfg)
        live = [k for k in res.children if k.key != "dom"]
        assert all(k.score is not None for k in live), (label, live)
    assert failures == 0, f"{failures} leaf exceptions"
    print("\nself-test OK")

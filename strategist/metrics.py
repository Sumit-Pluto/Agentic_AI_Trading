"""ChainMetrics — every metric in the rulebook table, computed from a live
ChainSnapshot plus (optionally) bhavcopy baselines and a futures quote.

Contract: docs/specs/strategist_rules.md ("Metrics to compute per symbol").
Graceful degradation everywhere: anything that cannot be computed honestly
lands in `insufficient` with a reason — never fabricated.

Sign convention for GEX (recorded in `assumptions`): dealers are assumed net
LONG calls and net SHORT puts (customers short calls / long puts), so
net GEX per strike = (call gamma·OI − put gamma·OI) × lot × spot² × 1%.
Actual dealer positioning is unobservable from OI alone.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from quant.mathutils import RISK_FREE, bs_gamma, years_to_expiry

# ── rulebook thresholds (module constants) ──────────────────────────────
BUILD_DOI_PCT = 0.20          # ΔOI% ≥ +20%  → build
UNWIND_DOI_PCT = -0.30        # ΔOI% ≤ −30%  → unwind
VOL_SPIKE_MULT = 3.0          # volume ≥ 3× 20-session average
Z_UNUSUAL = 2.0               # |z| > 2σ required when history exists
MIN_LEG_OI = 500              # liquidity guard: leg counts only if OI ≥ 500
MIN_LIQUID_LEGS = 6           # fewer liquid legs → chain unusable
THIN_CHAIN_LEGS = 10          # fewer → "thin chain", confidence downgrade
PCR_BEARISH_CROWD = 1.2       # PCR_OI above → crowd bearish (contrarian)
PCR_BULLISH_CROWD = 0.6       # PCR_OI below → crowd bullish (contrarian)
MIN_HISTORY_SESSIONS = 10     # < 10 sessions → degrade z-rules, downgrade
ROLLOVER_DTE_MAX = 7          # rollover filter active only near expiry
ROLL_FRONT_DROP_PCT = -0.10   # front-month ΔOI% at/below this …
ROLL_MATCH_LO, ROLL_MATCH_HI = 0.5, 2.0   # … matched by next-month build
GEX_PCT_MOVE = 0.01           # GEX scaled per 1% spot move
SKEW_TARGET_DELTA = 0.25      # 25Δ skew
SKEW_DELTA_TOL = 0.12         # nearest leg must be within this of 25Δ
PROVISIONAL_BEFORE_MIN = 9 * 60 + 45   # before 09:45 IST → provisional
PROVISIONAL_AFTER_MIN = 15 * 60        # from 15:00 IST → provisional
TOP_N_CONCENTRATION = 2

GEX_SIGN_ASSUMPTION = (
    "GEX assumes dealers net LONG calls / net SHORT puts (customers short "
    "calls, long puts): net GEX = (call gamma*OI - put gamma*OI) * lot * "
    "spot^2 * 1%. Dealer books are not observable from OI alone.")


def _f(x) -> float | None:
    """Safe float coercion for Shoonya string quote fields."""
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _as_floats(series) -> list[float]:
    """pd.Series / list / tuple → plain list of floats (order preserved)."""
    if series is None:
        return []
    try:
        return [float(v) for v in list(series)]
    except (TypeError, ValueError):
        return []


def _bs_delta(is_call: bool, spot: float, strike: float,
              t: float, sigma: float | None) -> float | None:
    if not sigma or sigma <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None
    st = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * sigma * sigma) * t) / st
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if is_call else nd1 - 1.0


def _is_provisional(now_epoch: float | None = None) -> bool:
    """True before 09:45 IST or from 15:00 IST (intraday OI is ~3-min lagged
    and provisional until the 16:15 true-up)."""
    t = time.time() if now_epoch is None else now_epoch
    ist = datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=5,
                                                                 minutes=30)
    hm = ist.hour * 60 + ist.minute
    return hm < PROVISIONAL_BEFORE_MIN or hm >= PROVISIONAL_AFTER_MIN


def _key(strike: float, opt_type: str) -> tuple[float, str]:
    return (round(float(strike), 2), opt_type)


@dataclass
class ChainMetrics:
    symbol: str = ""
    spot: float | None = None
    lot: int | None = None
    expiry_epoch: float | None = None
    dte_days: float | None = None
    atm_strike: float | None = None
    atm_iv: float | None = None
    atm_iv_percentile: float | None = None      # pass-through from iv_store
    skew_25d: float | None = None
    call_wall: dict | None = None               # {"strike","oi"}
    put_wall: dict | None = None
    pcr_oi: float | None = None
    pcr_vol: float | None = None
    pcr_flag: str | None = None                 # BEARISH_CROWD/BULLISH_CROWD
    concentration_call: float | None = None     # top-2 OI share, call side
    concentration_put: float | None = None
    max_pain: float | None = None
    gex_profile: list = field(default_factory=list)  # [{"strike","gex",...}]
    gamma_flip: float | None = None
    gex_regime: str | None = None               # DEALER_LONG/SHORT_GAMMA
    per_leg: dict = field(default_factory=dict)  # (strike,ot) -> leg metrics
    wall_unwinds: list = field(default_factory=list)
    rollover_excluded: set = field(default_factory=set)  # {(strike, ot)}
    liquid_leg_count: int = 0
    liquidity_ok: bool = False
    chain_thin: bool = True
    provisional: bool = False
    baselines_available: bool = False
    history_sessions: int = 0
    futures_view: dict | None = None
    assumptions: list = field(default_factory=list)
    insufficient: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        r = lambda x, n=4: (None if x is None else round(float(x), n))
        leg_key = lambda k: f"{k[0]:g}{k[1]}"
        return {
            "symbol": self.symbol, "spot": r(self.spot, 2), "lot": self.lot,
            "expiry_epoch": self.expiry_epoch, "dte_days": r(self.dte_days, 1),
            "atm_strike": self.atm_strike, "atm_iv": r(self.atm_iv),
            "atm_iv_percentile": r(self.atm_iv_percentile, 1),
            "skew_25d": r(self.skew_25d),
            "call_wall": self.call_wall, "put_wall": self.put_wall,
            "pcr_oi": r(self.pcr_oi, 3), "pcr_vol": r(self.pcr_vol, 3),
            "pcr_flag": self.pcr_flag,
            "concentration_call": r(self.concentration_call, 3),
            "concentration_put": r(self.concentration_put, 3),
            "max_pain": self.max_pain,
            "gex_profile": [{"strike": g["strike"], "gex": round(g["gex"], 0)}
                            for g in self.gex_profile],
            "gamma_flip": r(self.gamma_flip, 2), "gex_regime": self.gex_regime,
            "per_leg": {leg_key(k): v for k, v in self.per_leg.items()},
            "wall_unwinds": self.wall_unwinds,
            "rollover_excluded": sorted(leg_key(k)
                                        for k in self.rollover_excluded),
            "liquid_leg_count": self.liquid_leg_count,
            "liquidity_ok": self.liquidity_ok, "chain_thin": self.chain_thin,
            "provisional": self.provisional,
            "baselines_available": self.baselines_available,
            "history_sessions": self.history_sessions,
            "futures_view": self.futures_view,
            "assumptions": self.assumptions,
            "insufficient": self.insufficient,
        }


# ── sub-computations ────────────────────────────────────────────────────

def _futures_view(futures: dict | None, spot: float | None) -> dict | None:
    if not futures:
        return None
    lp, pc = _f(futures.get("lp")), _f(futures.get("c"))
    oi, poi = _f(futures.get("oi")), _f(futures.get("poi"))
    view = {"ltp": lp, "prev_close": pc, "oi": oi, "poi": poi,
            "doi": (oi - poi) if oi is not None and poi else None,
            "basis": (lp - spot) if lp is not None and spot else None,
            "buildup": None}
    if lp and pc and oi is not None and poi:
        up, oi_up = lp >= pc, oi >= poi
        view["buildup"] = ("LONG_BUILDUP" if up and oi_up else
                           "SHORT_BUILDUP" if oi_up else
                           "SHORT_COVERING" if up else "LONG_UNWINDING")
    return view


def _max_pain(rows) -> float | None:
    strikes = [r.strike for r in rows]
    if not strikes:
        return None
    best_k, best_pay = None, None
    for k in strikes:
        pay = 0.0
        for r in rows:
            if r.ce and r.ce.oi:
                pay += r.ce.oi * max(0.0, k - r.strike)
            if r.pe and r.pe.oi:
                pay += r.pe.oi * max(0.0, r.strike - k)
        if best_pay is None or pay < best_pay:
            best_k, best_pay = k, pay
    return best_k


def _skew_25d(rows, spot: float, t: float) -> tuple[float | None, str | None]:
    best_put, best_call = None, None      # (|delta gap|, iv)
    for r in rows:
        if r.ce and r.ce.iv:
            d = _bs_delta(True, spot, r.strike, t, r.ce.iv)
            if d is not None:
                gap = abs(d - SKEW_TARGET_DELTA)
                if best_call is None or gap < best_call[0]:
                    best_call = (gap, r.ce.iv)
        if r.pe and r.pe.iv:
            d = _bs_delta(False, spot, r.strike, t, r.pe.iv)
            if d is not None:
                gap = abs(d + SKEW_TARGET_DELTA)
                if best_put is None or gap < best_put[0]:
                    best_put = (gap, r.pe.iv)
    if not best_put or not best_call:
        return None, "no legs with valid IV for 25-delta skew"
    if best_put[0] > SKEW_DELTA_TOL or best_call[0] > SKEW_DELTA_TOL:
        return None, "nearest legs too far from 25-delta (thin/narrow chain)"
    return best_put[1] - best_call[1], None


def _gex(rows, spot: float, lot: int, t: float):
    """Per-strike net GEX profile + gamma-flip via spot-grid re-evaluation."""
    legs = []                                        # (sign, strike, iv, oi)
    profile = []
    for r in rows:
        net, any_iv = 0.0, False
        if r.ce and r.ce.iv and r.ce.oi:
            legs.append((+1.0, r.strike, r.ce.iv, r.ce.oi))
            net += bs_gamma(spot, r.strike, t, r.ce.iv) * r.ce.oi
            any_iv = True
        if r.pe and r.pe.iv and r.pe.oi:
            legs.append((-1.0, r.strike, r.pe.iv, r.pe.oi))
            net -= bs_gamma(spot, r.strike, t, r.pe.iv) * r.pe.oi
            any_iv = True
        if any_iv:
            profile.append({"strike": r.strike,
                            "gex": net * lot * spot * spot * GEX_PCT_MOVE})
    if not legs:
        return [], None, None, "no legs with valid IV for GEX"

    def total(s: float) -> float:
        return sum(sign * bs_gamma(s, k, t, iv) * oi
                   for sign, k, iv, oi in legs) * lot * s * s * GEX_PCT_MOVE

    lo, hi = rows[0].strike, rows[-1].strike
    n = 80
    grid = [lo + (hi - lo) * i / n for i in range(n + 1)]
    vals = [total(s) for s in grid]
    flips = []
    for i in range(n):
        if (vals[i] < 0) != (vals[i + 1] < 0) and vals[i + 1] != vals[i]:
            x0 = grid[i] + (grid[i + 1] - grid[i]) * (-vals[i]) / (vals[i + 1] - vals[i])
            flips.append(x0)
    flip = min(flips, key=lambda x: abs(x - spot)) if flips else None
    regime = ("DEALER_LONG_GAMMA" if total(spot) > 0 else "DEALER_SHORT_GAMMA")
    return profile, flip, regime, None


# ── main entrypoint ─────────────────────────────────────────────────────

def compute_metrics(chain, hist_baselines: dict | None = None,
                    futures: dict | None = None,
                    iv_store_percentile: float | None = None) -> ChainMetrics:
    """Compute the full rulebook metric set. Never raises on missing data —
    unavailable metrics stay None with a reason in `insufficient`."""
    m = ChainMetrics(atm_iv_percentile=iv_store_percentile,
                     provisional=_is_provisional(),
                     assumptions=[GEX_SIGN_ASSUMPTION])

    if chain is None or not getattr(chain, "strikes", None):
        m.insufficient["chain"] = "no option chain snapshot"
        return m

    chain.compute_ivs()                     # once, per contract
    rows = sorted(chain.strikes, key=lambda r: r.strike)
    now = time.time()
    m.symbol, m.spot, m.lot = chain.symbol, chain.spot, chain.lot
    m.expiry_epoch = chain.expiry_epoch
    m.dte_days = max(0.0, (chain.expiry_epoch - now) / 86400.0)
    m.atm_strike = chain.atm_strike()
    m.futures_view = _futures_view(futures, chain.spot)
    t = years_to_expiry(chain.expiry_epoch, now)

    # normalize baselines keys / meta
    bl: dict = {}
    if hist_baselines:
        for k, v in hist_baselines.items():
            try:
                bl[_key(k[0], str(k[1]).upper())] = v
            except (TypeError, IndexError, ValueError):
                continue
    m.baselines_available = bool(bl)
    m.history_sessions = max((int(v.get("sessions", 0)) for v in bl.values()),
                             default=0)
    if not bl:
        m.insufficient["baselines"] = ("no bhavcopy history cached — volume "
                                       "spikes/z-scores unavailable, ΔOI% "
                                       "thresholds only")
    elif m.history_sessions < MIN_HISTORY_SESSIONS:
        m.insufficient["baselines"] = (f"only {m.history_sessions} history "
                                       f"sessions (<{MIN_HISTORY_SESSIONS}) — "
                                       "z-scores degraded to plain ΔOI%")

    today_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    tot = {"CE": {"oi": 0.0, "vol": 0.0}, "PE": {"oi": 0.0, "vol": 0.0}}
    side_ois = {"CE": [], "PE": []}
    any_poi = False

    for r in rows:
        for ot, leg in (("CE", r.ce), ("PE", r.pe)):
            if leg is None:
                continue
            oi = float(leg.oi or 0.0)
            volume = float(leg.volume or 0.0)
            liquid = bool(oi >= MIN_LEG_OI and leg.ltp)
            tot[ot]["oi"] += oi
            tot[ot]["vol"] += volume
            side_ois[ot].append((r.strike, oi))

            # ΔOI: live oi−poi preferred; else yesterday from history
            poi = _f(getattr(leg, "poi", None))
            src = None
            if poi and poi > 0:
                src, prior = "live", poi
                any_poi = True
            else:
                prior = None
                lb = bl.get(_key(r.strike, ot))
                hist_oi = _as_floats(lb.get("oi_series")) if lb else []
                if lb is not None and hist_oi:
                    idx = list(lb["oi_series"].index) if hasattr(
                        lb["oi_series"], "index") else []
                    if idx and str(idx[-1]) == today_iso and len(hist_oi) > 1:
                        prior = hist_oi[-2]     # don't diff against today
                    else:
                        prior = hist_oi[-1]
                    src = "history" if prior and prior > 0 else None
            delta_oi = (oi - prior) if prior and prior > 0 else None
            delta_pct = (delta_oi / prior) if delta_oi is not None else None

            # history-derived stats
            lb = bl.get(_key(r.strike, ot))
            vol_avg = hist_doi_last = doi_z = vol_z = None
            sessions = 0
            if lb:
                sessions = int(lb.get("sessions", 0))
                va = lb.get("vol_avg20")
                vol_avg = float(va) if va else None
                hoi = _as_floats(lb.get("oi_series"))
                diffs = [b - a for a, b in zip(hoi, hoi[1:])]
                if diffs:
                    hist_doi_last = diffs[-1]
                if (sessions >= MIN_HISTORY_SESSIONS and len(diffs) >= 2
                        and delta_oi is not None):
                    sd = statistics.stdev(diffs)
                    if sd > 0:
                        doi_z = (delta_oi - statistics.mean(diffs)) / sd
                hvol = _as_floats(lb.get("vol_series"))
                if sessions >= MIN_HISTORY_SESSIONS and len(hvol) >= 2:
                    sd = statistics.stdev(hvol)
                    if sd > 0:
                        vol_z = (volume - statistics.mean(hvol)) / sd
            vol_mult = (volume / vol_avg) if vol_avg else None
            volume_spike = (None if vol_mult is None
                            else vol_mult >= VOL_SPIKE_MULT)

            m.per_leg[_key(r.strike, ot)] = {
                "strike": round(float(r.strike), 2), "opt_type": ot,
                "tsym": leg.tsym, "ltp": leg.ltp, "oi": oi, "volume": volume,
                "iv": leg.iv, "liquid": liquid,
                "poi": prior, "poi_source": src,
                "delta_oi": delta_oi,
                "delta_oi_pct": (None if delta_pct is None
                                 else round(delta_pct, 4)),
                "hist_doi_last": hist_doi_last,
                "doi_z": None if doi_z is None else round(doi_z, 2),
                "vol_z": None if vol_z is None else round(vol_z, 2),
                "vol_avg20": vol_avg,
                "vol_mult": None if vol_mult is None else round(vol_mult, 2),
                "volume_spike": volume_spike,
                "build": bool(delta_pct is not None
                              and delta_pct >= BUILD_DOI_PCT),
                "unwind": bool(delta_pct is not None
                               and delta_pct <= UNWIND_DOI_PCT),
                "rollover_excluded": False,
            }

    if not any_poi and not bl:
        m.insufficient["delta_oi"] = ("no prev-day OI (poi) in quotes and no "
                                      "history — ΔOI unavailable")

    # liquidity guard
    m.liquid_leg_count = sum(1 for v in m.per_leg.values() if v["liquid"])
    atm_liquid = any(v["liquid"] for k, v in m.per_leg.items()
                     if m.atm_strike is not None
                     and k[0] == round(m.atm_strike, 2))
    m.liquidity_ok = m.liquid_leg_count >= MIN_LIQUID_LEGS and atm_liquid
    m.chain_thin = m.liquid_leg_count < THIN_CHAIN_LEGS
    if not m.liquidity_ok:
        m.insufficient["liquidity"] = (
            f"only {m.liquid_leg_count} liquid legs "
            f"(OI>={MIN_LEG_OI} & live premium) — no reliable read")

    # walls + concentration
    for ot, wall_attr, conc_attr in (("CE", "call_wall", "concentration_call"),
                                     ("PE", "put_wall", "concentration_put")):
        ois = [x for x in side_ois[ot] if x[1] > 0]
        if not ois:
            m.insufficient[wall_attr] = f"no open interest on {ot} side"
            continue
        k, oi = max(ois, key=lambda x: x[1])
        setattr(m, wall_attr, {"strike": k, "oi": oi})
        total = sum(x[1] for x in ois)
        top = sorted((x[1] for x in ois), reverse=True)[:TOP_N_CONCENTRATION]
        setattr(m, conc_attr, sum(top) / total if total > 0 else None)

    # PCR
    if tot["CE"]["oi"] > 0:
        m.pcr_oi = tot["PE"]["oi"] / tot["CE"]["oi"]
        if m.pcr_oi > PCR_BEARISH_CROWD:
            m.pcr_flag = "BEARISH_CROWD"
        elif m.pcr_oi < PCR_BULLISH_CROWD:
            m.pcr_flag = "BULLISH_CROWD"
    else:
        m.insufficient["pcr_oi"] = "zero call OI in window"
    if tot["CE"]["vol"] > 0:
        m.pcr_vol = tot["PE"]["vol"] / tot["CE"]["vol"]
    else:
        m.insufficient["pcr_vol"] = "zero call volume in window"

    # max pain
    m.max_pain = _max_pain(rows)
    if m.max_pain is None:
        m.insufficient["max_pain"] = "no strikes with OI"

    # ATM IV
    atm_ivs = []
    for r in rows:
        if m.atm_strike is not None and r.strike == m.atm_strike:
            for leg in (r.ce, r.pe):
                if leg is not None and leg.iv:
                    atm_ivs.append(leg.iv)
    if atm_ivs:
        m.atm_iv = sum(atm_ivs) / len(atm_ivs)
    else:
        m.insufficient["atm_iv"] = "no valid IV at ATM strike (stale quotes)"

    # 25Δ skew
    if t > 0:
        m.skew_25d, why = _skew_25d(rows, chain.spot, t)
        if why:
            m.insufficient["skew_25d"] = why
    else:
        m.insufficient["skew_25d"] = "at/past expiry"

    # GEX profile + flip
    if t > 0 and chain.lot:
        m.gex_profile, m.gamma_flip, m.gex_regime, why = _gex(
            rows, chain.spot, chain.lot, t)
        if why:
            m.insufficient["gex"] = why
        elif m.gamma_flip is None:
            m.insufficient["gamma_flip"] = "no sign change inside strike window"
    else:
        m.insufficient["gex"] = "at/past expiry or missing lot size"

    # unwind flags on the walls (rulebook template 6)
    for wall, ot, side in ((m.call_wall, "CE", "CALL_WALL"),
                           (m.put_wall, "PE", "PUT_WALL")):
        if not wall:
            continue
        leg = m.per_leg.get(_key(wall["strike"], ot))
        if leg and leg["unwind"]:
            m.wall_unwinds.append({
                "side": side, "strike": wall["strike"], "opt_type": ot,
                "delta_oi_pct": leg["delta_oi_pct"],
                "delta_oi": leg["delta_oi"]})

    # rollover exclusion (needs next-expiry OI series, only near expiry)
    if m.dte_days is not None and m.dte_days <= ROLLOVER_DTE_MAX and bl:
        for key, leg in m.per_leg.items():
            lb = bl.get(key)
            if not lb:
                continue
            nxt = _as_floats(lb.get("next_oi_series"))
            if len(nxt) < 2:
                continue
            front_d = leg["delta_oi"]
            front_pct = leg["delta_oi_pct"]
            next_d = nxt[-1] - nxt[-2]
            if (front_d is not None and front_pct is not None
                    and front_pct <= ROLL_FRONT_DROP_PCT and next_d > 0
                    and ROLL_MATCH_LO <= next_d / abs(front_d) <= ROLL_MATCH_HI):
                leg["rollover_excluded"] = True
                m.rollover_excluded.add(key)
    elif (m.dte_days is not None and m.dte_days <= ROLLOVER_DTE_MAX
          and not bl):
        m.insufficient["rollover"] = ("near expiry but no next-month history "
                                      "— rollover filter inactive")

    return m


# ── synthetic fixtures (shared by metrics/footprints self-tests) ────────

def _synthetic_chain_for_tests(spot: float = 1000.0, step: float = 20.0,
                               n_side: int = 8, lot: int = 500,
                               dte_days: float = 20.0):
    """17 strikes around spot 1000 step 20, BS-priced premiums, plausible OI,
    with a deliberate short-iron-condor footprint: builds on 1080/1100 CE and
    900/920 PE (ΔOI% ≥ +20%), flat center, matched sizes."""
    from quant.context import ChainSnapshot, OptionLeg, StrikeRow
    from quant.mathutils import bs_price

    now = time.time()
    t = dte_days / 365.0
    snap = ChainSnapshot(symbol="SYNTEST", expiry_epoch=now + dte_days * 86400,
                         lot=lot, spot=spot)
    oi_ce = {840: 900, 860: 1000, 880: 1400, 900: 2000, 920: 2600, 940: 3200,
             960: 4200, 980: 5200, 1000: 6000, 1020: 6400, 1040: 6800,
             1060: 7200, 1080: 12000, 1100: 15000, 1120: 5200, 1140: 2600,
             1160: 1500}
    oi_pe = {840: 400, 860: 1600, 880: 6000, 900: 16000, 920: 10000,
             940: 7000, 960: 6400, 980: 6000, 1000: 5800, 1020: 4200,
             1040: 3000, 1060: 2200, 1080: 1500, 1100: 1100, 1120: 800,
             1140: 700, 1160: 600}
    # condor footprint: explicit prev-day OI for the four wing builds
    poi_ce = {1080: 9000.0, 1100: 12000.0}    # +33% / +25%
    poi_pe = {900: 13000.0, 920: 8000.0}      # +23% / +25%
    vol_ce = {1080: 9000.0}                   # volume spike leg (4.5x of 2000)

    for i in range(2 * n_side + 1):
        k = spot + step * (i - n_side)
        ik = int(k)
        iv_k = 0.25 + 0.12 * (spot - k) / spot
        row = StrikeRow(strike=k)
        for ot in ("CE", "PE"):
            is_call = ot == "CE"
            ltp = max(0.05, round(bs_price(is_call, spot, k, t, iv_k), 2))
            oi = float((oi_ce if is_call else oi_pe)[ik])
            poi = (poi_ce if is_call else poi_pe).get(ik, round(oi / 1.04))
            volume = (vol_ce.get(ik, 1600.0 + (ik % 5) * 80) if is_call
                      else 1600.0 + (ik % 7) * 60)
            leg = OptionLeg(tsym=f"SYNTEST{ik}{ot}", token=f"{ik}{ot}",
                            ltp=ltp, oi=oi, volume=volume)
            leg.poi = float(poi)              # what live quotes would carry
            setattr(row, ot.lower(), leg)
        snap.strikes.append(row)
    return snap


def _synthetic_baselines_for_tests(chain, sessions: int = 20) -> dict:
    """Deterministic 20-session baselines consistent with the synthetic
    chain: oi_series ends at each leg's poi, vol averages ~2000."""
    import pandas as pd
    from datetime import date as _date

    dates: list[str] = []
    d = _date.today() - timedelta(days=1)
    while len(dates) < sessions:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    dates.reverse()

    out = {}
    for row in chain.strikes:
        for ot, leg in (("CE", row.ce), ("PE", row.pe)):
            if leg is None:
                continue
            poi = float(getattr(leg, "poi", None) or leg.oi or 0.0)
            ovals = [round(poi * (1 + 0.004 * (((i * 3) % 7) - 3)), 0)
                     for i in range(sessions)]
            ovals[-1] = poi
            vvals = [2000.0 + 150.0 * (((i * 5) % 4) - 1.5)
                     for i in range(sessions)]
            out[(round(row.strike, 2), ot)] = {
                "vol_avg20": sum(vvals) / len(vvals),
                "oi_series": pd.Series(ovals, index=dates),
                "vol_series": pd.Series(vvals, index=dates),
                "sessions": sessions,
                "next_oi_series": None,
            }
    return out


if __name__ == "__main__":
    import json

    # provisional-window logic on fixed IST instants
    utc = timezone.utc
    assert _is_provisional(datetime(2026, 7, 3, 2, 0, tzinfo=utc).timestamp())      # 07:30 IST
    assert not _is_provisional(datetime(2026, 7, 3, 5, 0, tzinfo=utc).timestamp())  # 10:30 IST
    assert _is_provisional(datetime(2026, 7, 3, 9, 35, tzinfo=utc).timestamp())     # 15:05 IST

    chain = _synthetic_chain_for_tests()
    fut = {"lp": "1004.50", "c": "998.00", "oi": "1500000",
           "poi": "1400000", "v": "250000"}

    # run 1: live poi only, no history
    m = compute_metrics(chain, None, fut, 62.0)
    assert m.call_wall and m.call_wall["strike"] == 1100.0, m.call_wall
    assert m.put_wall and m.put_wall["strike"] == 900.0, m.put_wall
    lg = m.per_leg[(1080.0, "CE")]
    assert lg["build"] and abs(lg["delta_oi"] - 3000) < 1e-6
    assert lg["poi_source"] == "live"
    assert m.per_leg[(1000.0, "CE")]["build"] is False
    assert m.liquidity_ok and not m.chain_thin
    assert m.per_leg[(840.0, "PE")]["liquid"] is False        # OI 400 < 500
    assert m.pcr_oi is not None and 0.5 < m.pcr_oi < 1.5
    assert m.max_pain is not None and 880 <= m.max_pain <= 1100
    assert m.atm_iv is not None and 0.15 < m.atm_iv < 0.40
    assert m.skew_25d is not None and m.skew_25d > 0          # puts richer
    assert m.gex_profile and m.gamma_flip is not None
    assert 840 <= m.gamma_flip <= 1160
    assert m.futures_view and m.futures_view["buildup"] == "LONG_BUILDUP"
    assert m.atm_iv_percentile == 62.0
    assert not m.wall_unwinds and not m.rollover_excluded
    assert "baselines" in m.insufficient
    json.dumps(m.to_dict())

    # run 2: with 20-session baselines → spikes + z-scores
    bl = _synthetic_baselines_for_tests(chain)
    m2 = compute_metrics(chain, bl, None, None)
    assert m2.baselines_available and m2.history_sessions == 20
    l2 = m2.per_leg[(1080.0, "CE")]
    assert l2["volume_spike"] is True and l2["vol_mult"] >= VOL_SPIKE_MULT
    assert l2["doi_z"] is not None and l2["doi_z"] > Z_UNUSUAL
    assert m2.per_leg[(1000.0, "PE")]["volume_spike"] is False
    json.dumps(m2.to_dict())

    # run 3: graceful degradation on a missing chain
    m3 = compute_metrics(None)
    assert not m3.liquidity_ok and "chain" in m3.insufficient
    json.dumps(m3.to_dict())

    print("metrics self-test OK — walls CE1100/PE900, "
          f"max_pain={m.max_pain:g}, atm_iv={m.atm_iv:.3f}, "
          f"skew25={m.skew_25d:+.4f}, flip={m.gamma_flip:.1f}, "
          f"pcr_oi={m.pcr_oi:.2f}, condor legs z={l2['doi_z']}")

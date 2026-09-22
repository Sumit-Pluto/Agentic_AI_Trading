"""Volume & Flow agent family (spec: docs/specs/volume.json).

Leaves:
  * pcr               — writer-dominated Indian PCR from the monthly stock
                        option chain (OI + day volume), mild contrarian with
                        stock-adjusted bands (neutral ~0.6, not the index 1.0).
  * orderflow         — tbq/tsq + top-5 depth pressure ratio with spoof /
                        spread / circuit guards from the cash GetQuotes.
  * vwap              — session VWAP distance (volume-weighted z) + slope,
                        tuned for mean-reversion entries.
  * rel_volume        — time-of-day adjusted relative volume vs same-slot
                        medians of prior sessions in ctx.df.
  * futures_oi_regime — 4-quadrant long/short buildup from stock futures
                        price + OI deltas (daily + intraday blend).
  * volume_profile    — FUTURE stub (needs trusted 1-min/tick history).

Conventions:
  * Spec 'return 50/50 with flag X' for MISSING data maps to (None, reason)
    here: the QuantAgent branch rollup renormalizes weights onto children
    that produced information, which is exactly the spec's fallback protocol.
    Flags are recorded in ctx.meta['volume.flags'] for the orchestrator.
    Cases where data EXISTS but is untradeable (near-circuit book) return a
    genuine neutral 50 instead.
  * Shoonya quote dicts (ctx.cash_quote / ctx.futures) deliver values as
    STRINGS — every field goes through the _f() safe-float helper.

HONEST CAVEATS (from the spec, kept deliberately in code):
  * orderflow: tbq/tsq are PENDING limit orders, not executed flow —
    spoofable (park-and-cancel), blind to icebergs and market orders, and
    biased in trends (passive buyers rest lower as price falls, inflating
    tbq exactly when price is weak). At circuit locks the imbalance is real
    but untradeable. Short-horizon context only; low weight.
  * futures_oi_regime: OI change cannot tell who initiated (a rising-OI
    up-move may be hedgers shorting against cash longs); short-covering
    rallies routinely fail; arbitrage/physical-settlement hedges pollute
    stock futures OI; intraday OI can lag price. Regime CONTEXT only, never
    a standalone direction call — hence the [10,90] clamp.
  * rel_volume: >4x spikes are often news events where mean-reversion logic
    breaks; the map caps at 90 (not 100) and the event-risk agent elsewhere
    handles the rest — never extrapolate above 4x.
  * vwap: TPSeries `intvwap` is NOT trusted as session VWAP (interval-scoped
    in practice); cumulative sum(tp*v)/sum(v) from own candles is the safe
    default, upgraded to the exchange `ap` field when a cash quote is at hand.
"""

from __future__ import annotations

import math

from ..base import QuantAgent

# ── pcr params ──────────────────────────────────────────────────────────────
PCR_BAND_PCT = 0.10
PCR_STRIKES_EACH_SIDE = 10
PCR_W_OI = 0.7
PCR_W_VOL = 0.3
PCR_OI_POINTS = [(0.30, 20.0), (0.60, 50.0), (1.20, 80.0)]
PCR_VOL_POINTS = [(0.30, 35.0), (0.60, 50.0), (1.50, 65.0)]
PCR_MIN_CHAIN_OI = 25000.0
PCR_EXPIRY_GUARD_DAYS = 2.0
PCR_EXPIRY_SHRINK = 0.5

# ── orderflow params ────────────────────────────────────────────────────────
OF_W_TOTAL = 0.6
OF_W_TOP5 = 0.4
OF_SLOPE = 250.0
OF_SPOOF_DELTA = 0.15
OF_WS_MAX_AGE_S = 60.0
OF_MAX_SPREAD_BPS = 30.0
OF_CIRCUIT_BUFFER_PCT = 0.5

# ── vwap params ─────────────────────────────────────────────────────────────
VW_K_SLOPE = 6
VW_FLAT_THR = 0.10            # % per ~30min
VW_Z_SLOPE = 20.0
VW_KNIFE_Z = 3.0
VW_KNIFE_CAP = 60.0
VW_SLOPE_BONUS = 10.0
VW_SLOPE_PENALTY = -15.0
VW_MIN_BARS = 6
VW_SD_ATR_FLOOR = 0.25        # of ATR14(5m)
VW_SD_BPS_FLOOR = 0.0005      # 5 bps of VWAP

# ── rel_volume params ───────────────────────────────────────────────────────
RV_N_SESSIONS = 10
RV_MIN_SESSIONS = 5
RV_MIN_BARS_SESSION = 60      # skip half days / gappy sessions
RV_W_BAR = 0.6
RV_W_CUM = 0.4
RV_MAP_POINTS = [(0.0, 25.0), (0.5, 25.0), (0.8, 45.0), (1.2, 55.0),
                 (2.0, 75.0), (4.0, 90.0)]
RV_SHRINK = 0.6               # keep 60% of distance from 50 if few sessions

# ── futures_oi_regime params ────────────────────────────────────────────────
FUT_K_INTRA = 12              # 5m bars (1 hour)
FUT_K_INTRA_MIN = 6
FUT_P_FLAT = 0.0010           # 0.10% both timeframes
FUT_OI_FLAT_DAY = 0.005       # 0.5%
FUT_OI_FLAT_INTRA = 0.003     # 0.3%
FUT_OI_REF_DAY = 0.020        # 2.0%
FUT_OI_REF_INTRA = 0.010      # 1.0%
FUT_SF_LO, FUT_SF_HI = 0.5, 1.5
FUT_SCORE_LO, FUT_SCORE_HI = 10.0, 90.0
FUT_W_DAY = 0.5
FUT_W_INTRA = 0.5
FUT_EXPIRY_GUARD_DAYS = 3.0
FUT_MIN_VOL_LOTS = 100.0
FUT_BASE_BUY = {"LONG_BUILDUP": 80.0, "SHORT_COVERING": 65.0, "NEUTRAL": 50.0,
                "LONG_UNWINDING": 35.0, "SHORT_BUILDUP": 20.0}


# ── shared helpers ──────────────────────────────────────────────────────────
def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _interp(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation; flat extrapolation at both ends."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _f(d: dict, key: str) -> float | None:
    """Safe float from a Shoonya quote dict (values arrive as strings)."""
    try:
        v = d.get(key)
        if v is None or v == "":
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _flag(ctx, flag: str):
    ctx.meta.setdefault("volume.flags", []).append(flag)


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def _today_slice(df):
    """Bars of the most recent session in df (assumed the live session)."""
    last_day = df.index[-1].date()
    mask = [dt == last_day for dt in df.index.date]
    return df[mask]


# ── 1. Put-Call Ratio ───────────────────────────────────────────────────────
class PCRAgent(QuantAgent):
    key = "pcr"
    name = "Put-Call Ratio"
    description = ("Monthly stock-chain PCR (OI 0.7 / volume 0.3 blend); "
                   "writer-dominated Indian reading: high PCR = put-writing "
                   "support = bullish lean; scores compressed to [20,80]")
    default_weight_buy = 0.15
    default_weight_sell = 0.15

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None or not chain.strikes:
            _flag(ctx, "no_chain")
            return None, "no_chain: option chain unavailable"
        spot = chain.spot or ctx.spot
        if not spot or spot <= 0:
            return None, "no usable spot for strike band"
        atm = chain.atm_strike()
        if atm is None:
            return None, "no_chain: empty strike ladder"

        rows = sorted(chain.strikes, key=lambda r: r.strike)
        band = [r for r in rows if abs(r.strike - spot) / spot <= PCR_BAND_PCT]
        below = [r for r in band if r.strike < atm][-PCR_STRIKES_EACH_SIDE:]
        above = [r for r in band if r.strike > atm][:PCR_STRIKES_EACH_SIDE]
        at = [r for r in band if r.strike == atm]
        sel = below + at + above

        ce_oi = pe_oi = ce_vol = pe_vol = 0.0
        used = 0
        for r in sel:
            coi = float(r.ce.oi) if (r.ce and r.ce.oi) else 0.0
            poi = float(r.pe.oi) if (r.pe and r.pe.oi) else 0.0
            if coi + poi <= 0:              # untraded row — ignore
                continue
            used += 1
            ce_oi += coi
            pe_oi += poi
            ce_vol += float(r.ce.volume) if (r.ce and r.ce.volume) else 0.0
            pe_vol += float(r.pe.volume) if (r.pe and r.pe.volume) else 0.0

        if ce_oi + pe_oi < PCR_MIN_CHAIN_OI:
            _flag(ctx, "illiquid_chain")
            return None, (f"illiquid_chain: band OI {ce_oi + pe_oi:.0f} "
                          f"< {PCR_MIN_CHAIN_OI:.0f}")

        pcr_oi = (pe_oi / ce_oi) if ce_oi > 0 else None
        pcr_vol = (pe_vol / ce_vol) if ce_vol > 0 else None
        s_oi = (_clip(_interp(PCR_OI_POINTS, pcr_oi), 20.0, 80.0)
                if pcr_oi is not None else None)
        s_vol = (_clip(_interp(PCR_VOL_POINTS, pcr_vol), 35.0, 65.0)
                 if pcr_vol is not None else None)

        if s_oi is not None and s_vol is not None:
            buy = PCR_W_OI * s_oi + PCR_W_VOL * s_vol
        elif s_oi is not None:
            buy = s_oi
        elif s_vol is not None:
            buy = s_vol
        else:
            return None, "pcr unavailable: zero call OI and call volume in band"
        buy = _clip(buy, 20.0, 80.0)

        dte = max(0.0, (chain.expiry_epoch - ctx.now) / 86400.0)
        guarded = dte <= PCR_EXPIRY_GUARD_DAYS
        if guarded:                    # unwinding/rollover dominates OI
            buy = 50.0 + (buy - 50.0) * PCR_EXPIRY_SHRINK

        score = round(buy) if ctx.direction == "BUY" else round(100.0 - buy)
        detail = (f"PCR_OI {pcr_oi:.2f}" if pcr_oi is not None else "PCR_OI n/a")
        detail += (f", PCR_VOL {pcr_vol:.2f}" if pcr_vol is not None
                   else ", PCR_VOL n/a")
        detail += (f" over {used} strikes (band OI {ce_oi + pe_oi:.0f})"
                   f"{f', expiry-shrunk DTE {dte:.0f}d' if guarded else ''}")
        return float(score), detail


# ── 2. Order-book Pressure ──────────────────────────────────────────────────
class OrderflowAgent(QuantAgent):
    key = "orderflow"
    name = "Order-book Pressure"
    description = ("Pending-qty imbalance: tbq/tsq (0.6) + top-5 depth (0.4) "
                   "with spoof-persistence, spread and circuit guards. "
                   "Pending orders != trades — most gameable input, low weight")
    default_weight_buy = 0.15
    default_weight_sell = 0.15

    def compute(self, ctx):
        q = ctx.cash_quote
        if not q:
            _flag(ctx, "stale")
            return None, "no cash quote (GetQuotes failed / not fetched)"
        tbq, tsq = _f(q, "tbq"), _f(q, "tsq")
        if tbq is None or tsq is None:
            _flag(ctx, "stale")
            return None, "quote lacks tbq/tsq"

        lp = _f(q, "lp") or (float(ctx.spot) if ctx.spot else None)
        uc, lc = _f(q, "uc"), _f(q, "lc")
        if lp is not None:
            buf = OF_CIRCUIT_BUFFER_PCT / 100.0
            if (uc and lp >= uc * (1.0 - buf)) or \
                    (lc and lc > 0 and lp <= lc * (1.0 + buf)):
                _flag(ctx, "near_circuit")
                return 50.0, (f"near_circuit: lp {lp:g} within "
                              f"{OF_CIRCUIT_BUFFER_PCT}% of band "
                              f"[{lc}, {uc}] — one-sided frozen book")

        pr = tbq / (tbq + tsq) if (tbq + tsq) > 0 else 0.5

        bids = [_f(q, f"bq{i}") for i in range(1, 6)]
        asks = [_f(q, f"sq{i}") for i in range(1, 6)]
        bsum = sum(v for v in bids if v)
        asum = sum(v for v in asks if v)
        depth_seen = any(v is not None for v in bids + asks)
        if depth_seen:
            ti = bsum / (bsum + asum) if (bsum + asum) > 0 else 0.5
            raw = OF_W_TOTAL * pr + OF_W_TOP5 * ti
        else:                             # no depth fields -> total qty alone
            ti = None
            raw = pr

        confidence = 1.0
        notes = []
        ws = ctx.meta.get("orderflow.pr_ws")   # optional: (pr, epoch) cache
        try:
            if ws and ctx.now - float(ws[1]) <= OF_WS_MAX_AGE_S \
                    and abs(pr - float(ws[0])) > OF_SPOOF_DELTA:
                confidence *= 0.5          # book flipped inside a minute
                notes.append("spoof?")
        except Exception:
            pass
        bp1, sp1 = _f(q, "bp1"), _f(q, "sp1")
        if bp1 and sp1 and (bp1 + sp1) > 0:
            spread_bps = (sp1 - bp1) / ((sp1 + bp1) / 2.0) * 10000.0
            if spread_bps > OF_MAX_SPREAD_BPS:
                confidence *= 0.5
                notes.append(f"wide {spread_bps:.0f}bps")
        else:
            spread_bps = None

        buy_full = _clip(50.0 + (raw - 0.5) * OF_SLOPE, 0.0, 100.0)
        buy = 50.0 + (buy_full - 50.0) * confidence
        score = buy if ctx.direction == "BUY" else 100.0 - buy

        detail = (f"PR {pr:.3f} (tbq {tbq:.0f}/tsq {tsq:.0f})"
                  + (f", top5 {ti:.3f}" if ti is not None else ", no depth")
                  + (f", conf {confidence:g}"
                     f" [{' '.join(notes)}]" if confidence < 1.0 else ""))
        return round(score, 1), detail


# ── 3. Session VWAP distance & slope ────────────────────────────────────────
class VWAPAgent(QuantAgent):
    key = "vwap"
    name = "VWAP Distance"
    description = ("Volume-weighted z-distance from session VWAP + 30-min "
                   "VWAP slope; mean-reversion scoring (below flat/rising "
                   "VWAP = discount for BUY) with falling-knife cap")
    default_weight_buy = 0.25
    default_weight_sell = 0.25

    def compute(self, ctx):
        df = ctx.df
        if df is None or len(df) == 0:
            return None, "no 5m candles"
        need = {"high", "low", "close", "volume"}
        if not need <= set(map(str, df.columns)):
            return None, "candles lack OHLCV columns"
        tdf = _today_slice(df)
        n_bars = len(tdf)
        vol = tdf["volume"].astype(float)
        v_sum = float(vol.sum())
        if v_sum <= 0:
            _flag(ctx, "no_volume")
            return None, "no_volume: zero session volume (halted?)"

        from signals.indicators import vwap_session
        vser = vwap_session(df)
        tv = vser[vser.index.isin(tdf.index)].dropna()
        vwap_candle = float(tv.iloc[-1]) if len(tv) else None
        if vwap_candle is None or not math.isfinite(vwap_candle):
            return None, "VWAP not computable from session candles"

        # exchange day-average price is authoritative when available
        src = "candles"
        vwap = vwap_candle
        try:
            q = ctx.cash_quote
            ap = _f(q, "ap") if q else None
            if ap and ap > 0:
                vwap, src = ap, "ap"
        except Exception:
            pass

        tp = (tdf["high"].astype(float) + tdf["low"].astype(float)
              + tdf["close"].astype(float)) / 3.0
        sd = math.sqrt(float((vol * (tp - vwap) ** 2).sum()) / v_sum)
        floors = [VW_SD_BPS_FLOOR * vwap]
        try:
            if ctx.atr14:
                floors.append(VW_SD_ATR_FLOOR * float(ctx.atr14))
        except Exception:
            pass
        sd = max(sd, *floors)
        if sd <= 0:
            return None, "degenerate VWAP dispersion"
        z = (float(ctx.spot) - vwap) / sd

        # cumulative-VWAP slope over K bars (all-available >=3 fallback)
        slope_pct = None
        if len(tv) >= 3:
            k = VW_K_SLOPE if len(tv) >= VW_K_SLOPE + 1 else len(tv) - 1
            v0 = float(tv.iloc[-1 - k])
            if v0 > 0:
                slope_pct = (float(tv.iloc[-1]) - v0) / v0 * 100.0
        if slope_pct is None:
            trend = "FLAT"
        elif slope_pct >= VW_FLAT_THR:
            trend = "UP"
        elif slope_pct <= -VW_FLAT_THR:
            trend = "DOWN"
        else:
            trend = "FLAT"

        if ctx.direction == "BUY":
            base = _clip(50.0 - VW_Z_SLOPE * z, 5.0, 95.0)
            if z < -VW_KNIFE_Z:            # falling knife, not a dip
                base = min(base, VW_KNIFE_CAP)
            adj = {"UP": VW_SLOPE_BONUS, "FLAT": 0.0,
                   "DOWN": VW_SLOPE_PENALTY}[trend]
        else:
            base = _clip(50.0 + VW_Z_SLOPE * z, 5.0, 95.0)
            if z > VW_KNIFE_Z:             # blowoff stretch
                base = min(base, VW_KNIFE_CAP)
            adj = {"DOWN": VW_SLOPE_BONUS, "FLAT": 0.0,
                   "UP": VW_SLOPE_PENALTY}[trend]
        score = _clip(base + adj, 0.0, 100.0)

        early = n_bars < VW_MIN_BARS
        if early:                          # pre-09:45 noise shrink
            score = 50.0 + (score - 50.0) * 0.5

        detail = (f"VWAP {vwap:.2f} ({src}), z {z:+.2f}, slope "
                  f"{'n/a' if slope_pct is None else f'{slope_pct:+.3f}%'} "
                  f"{trend}, {n_bars} bars{' (early)' if early else ''}")
        return round(score, 1), detail


# ── 4. Relative Volume (time-of-day adjusted) ───────────────────────────────
class RelVolumeAgent(QuantAgent):
    key = "rel_volume"
    name = "Relative Volume"
    description = ("Last-bar + cumulative volume vs same-time-slot medians of "
                   "prior sessions; direction-agnostic conviction (dead tape "
                   "-> 25, capped 90 above 4x — news-spike territory)")
    default_weight_buy = 0.25
    default_weight_sell = 0.25

    def compute(self, ctx):
        df = ctx.df
        if df is None or len(df) == 0:
            return None, "no 5m candles"
        if "volume" not in set(map(str, df.columns)):
            return None, "candles lack volume"

        today = df.index[-1].date()
        slot = df.index[-1].time()         # last COMPLETED bar's start slot
        prior_days = sorted({d for d in df.index.date if d < today})
        sessions = []
        for d in prior_days:
            sd = df[[dt == d for dt in df.index.date]]
            if len(sd) >= RV_MIN_BARS_SESSION:
                sessions.append(sd)
        sessions = sessions[-RV_N_SESSIONS:]
        if not sessions:
            _flag(ctx, "no_history")
            return None, "no_history: no full prior session in candle frame"

        slot_vols, cum_vols = [], []
        for sd in sessions:
            times = list(sd.index.time)
            hit = [i for i, t in enumerate(times) if t == slot]
            if hit:
                slot_vols.append(float(sd["volume"].iloc[hit[0]]))
            upto = [i for i, t in enumerate(times) if t <= slot]
            if upto:
                cum_vols.append(float(sd["volume"].iloc[upto].sum()))

        base_bar = _median(slot_vols)
        if base_bar <= 0:                  # substitute mean of slot medians
            by_slot: dict = {}
            for sd in sessions:
                for t, v in zip(sd.index.time, sd["volume"].astype(float)):
                    by_slot.setdefault(t, []).append(v)
            meds = [_median(v) for v in by_slot.values()]
            base_bar = (sum(meds) / len(meds)) if meds else 0.0
        if base_bar <= 0:
            _flag(ctx, "no_history")
            return None, "no_history: zero baseline slot volume"

        tdf = _today_slice(df)
        v_bar = float(df["volume"].iloc[-1])
        rvol_bar = v_bar / base_bar
        base_cum = _median(cum_vols)
        if base_cum > 0:
            rvol_cum = float(tdf["volume"].sum()) / base_cum
            rvol = RV_W_BAR * rvol_bar + RV_W_CUM * rvol_cum
        else:
            rvol_cum = None
            rvol = rvol_bar

        # volume confirms conviction for WHICHEVER direction triggered
        score = _interp(RV_MAP_POINTS, rvol)
        few = len(sessions) < RV_MIN_SESSIONS
        if few:
            score = 50.0 + (score - 50.0) * RV_SHRINK

        detail = (f"RVOL {rvol:.2f} (bar {rvol_bar:.2f}"
                  + (f", cum {rvol_cum:.2f}" if rvol_cum is not None else "")
                  + f") @ {slot.strftime('%H:%M')} vs {len(sessions)} sessions"
                  + (" (few-session shrink)" if few else ""))
        return round(score, 1), detail


# ── 5. Futures OI Regime (4-quadrant buildup) ───────────────────────────────
class FuturesOIRegimeAgent(QuantAgent):
    key = "futures_oi_regime"
    name = "Futures OI Regime"
    description = ("Long/short buildup 4-quadrant from futures price+OI "
                   "deltas, daily (oi vs poi, pc) + intraday blend, expiry-"
                   "week daily discard; context only, clamped [10,90]")
    default_weight_buy = 0.20
    default_weight_sell = 0.20

    @staticmethod
    def _quadrant(dp: float, doi: float, oi_flat: float) -> str:
        if abs(dp) < FUT_P_FLAT or abs(doi) < oi_flat:
            return "NEUTRAL"
        if dp > 0:
            return "LONG_BUILDUP" if doi > 0 else "SHORT_COVERING"
        return "SHORT_BUILDUP" if doi > 0 else "LONG_UNWINDING"

    @classmethod
    def _tf_score(cls, dp: float, doi: float, oi_flat: float,
                  oi_ref: float) -> tuple[float, str]:
        quad = cls._quadrant(dp, doi, oi_flat)
        sf = _clip(abs(doi) / oi_ref, FUT_SF_LO, FUT_SF_HI)
        base = FUT_BASE_BUY[quad]
        return _clip(50.0 + (base - 50.0) * sf, FUT_SCORE_LO, FUT_SCORE_HI), quad

    def compute(self, ctx):
        fut = ctx.futures
        if not fut:
            _flag(ctx, "no_futures")
            return None, "no_futures: futures quote unavailable"

        lot = _f(fut, "ls") or _f(fut, "lot")
        day_vol = _f(fut, "v")
        if lot and day_vol is not None and day_vol < lot * FUT_MIN_VOL_LOTS:
            _flag(ctx, "no_futures")
            return None, (f"no_futures: illiquid leg (day vol {day_vol:.0f} "
                          f"< {FUT_MIN_VOL_LOTS:.0f} lots)")

        # DAILY: pc (day % change) preferred, else (lp-c)/c; oi vs poi
        lp, c = _f(fut, "lp"), _f(fut, "c")
        pc = _f(fut, "pc")
        oi, poi = _f(fut, "oi"), _f(fut, "poi")
        dp_day = (pc / 100.0 if pc is not None
                  else ((lp - c) / c if (lp and c and c > 0) else None))
        doi_day = (oi - poi) / poi if (oi is not None and poi and poi > 0) \
            else None
        day = (self._tf_score(dp_day, doi_day, FUT_OI_FLAT_DAY, FUT_OI_REF_DAY)
               if (dp_day is not None and doi_day is not None) else None)

        # INTRADAY: last-hour close+oi deltas from today's 5m bars (oi column)
        intra = None
        dp_i = doi_i = None
        try:
            df = ctx.df
            if df is not None and len(df) and \
                    {"close", "oi"} <= set(map(str, df.columns)):
                tdf = _today_slice(df)
                n = len(tdf)
                k = FUT_K_INTRA if n >= FUT_K_INTRA + 1 else n - 1
                if k >= FUT_K_INTRA_MIN:
                    c0 = float(tdf["close"].iloc[-1 - k])
                    c1 = float(tdf["close"].iloc[-1])
                    o0 = float(tdf["oi"].iloc[-1 - k])
                    o1 = float(tdf["oi"].iloc[-1])
                    if c0 > 0 and o0 > 0 and math.isfinite(o1):
                        dp_i = (c1 - c0) / c0
                        doi_i = (o1 - o0) / o0
                        intra = self._tf_score(dp_i, doi_i, FUT_OI_FLAT_INTRA,
                                               FUT_OI_REF_INTRA)
        except Exception:
            intra = None

        exp = _f(fut, "expiry_epoch")
        dte = (exp - ctx.now) / 86400.0 if exp else None
        w_day, w_intra = FUT_W_DAY, FUT_W_INTRA
        expiry_week = dte is not None and dte <= FUT_EXPIRY_GUARD_DAYS
        if expiry_week:                    # rollover fakes 'unwinding'
            w_day, w_intra = 0.0, 1.0

        parts, wsum, tags = 0.0, 0.0, []
        if day is not None and w_day > 0:
            parts += w_day * day[0]
            wsum += w_day
            tags.append(f"day {day[1]} (dP {dp_day * 100:+.2f}%, "
                        f"dOI {doi_day * 100:+.2f}%)")
        if intra is not None and w_intra > 0:
            parts += w_intra * intra[0]
            wsum += w_intra
            tags.append(f"intra {intra[1]} (dP {dp_i * 100:+.2f}%, "
                        f"dOI {doi_i * 100:+.2f}%)")
        if wsum <= 0:
            _flag(ctx, "no_futures")
            why = ("expiry week: daily OI distorted and no intraday OI"
                   if expiry_week and day is not None
                   else "no_futures: neither daily (oi/poi) nor intraday "
                        "(df oi column) deltas available")
            return None, why

        buy = parts / wsum
        score = buy if ctx.direction == "BUY" else 100.0 - buy
        detail = "; ".join(tags) + \
            (f"; expiry-week (DTE {dte:.0f}d, daily dropped)"
             if expiry_week else "")
        return round(score, 1), detail


# ── 6. Volume Profile (future stub) ─────────────────────────────────────────
class VolumeProfileAgent(QuantAgent):
    key = "volume_profile"
    name = "Volume Profile"
    description = ("POC / value-area scoring from a 20-day composite profile "
                   "(future — needs trusted 1-min/tick history)")
    default_weight_buy = 0.0
    default_weight_sell = 0.0
    default_enabled = False

    def compute(self, ctx):
        return None, ("future: needs nightly-cached 1-min (or tick) candles "
                      "binned into a POC/VAH/VAL profile — 5m OHLCV is too "
                      "coarse to trust")


# ── family branch ───────────────────────────────────────────────────────────
class VolumeFamily(QuantAgent):
    key = "volume"
    name = "Volume & Flow"
    description = "PCR, orderflow, VWAP, relative volume, futures OI regime"
    default_weight_buy = 1.0
    default_weight_sell = 1.0


def build() -> QuantAgent:
    return VolumeFamily([
        PCRAgent(),
        OrderflowAgent(),
        VWAPAgent(),
        RelVolumeAgent(),
        FuturesOIRegimeAgent(),
        VolumeProfileAgent(),
    ])


# ── synthetic self-test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import datetime as _dt
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

    def make_df(today_bars=48, full_days=6):
        """full_days full prior sessions + a partial live session, with a
        rising oi column so the futures intraday leg exercises."""
        rng = np.random.default_rng(11)
        d = _dt.date(2026, 7, 3)           # a Friday
        days = []
        while len(days) < full_days + 1:
            if d.weekday() < 5:
                days.append(d)
            d -= _dt.timedelta(days=1)
        days = list(reversed(days))
        idx = []
        for i, day in enumerate(days):
            bars = today_bars if i == len(days) - 1 else 75
            start = _dt.datetime.combine(day, _dt.time(9, 15))
            idx += [start + _dt.timedelta(minutes=5 * j) for j in range(bars)]
        n = len(idx)
        close = 100.0 + np.cumsum(rng.normal(0, 0.12, n))
        high = close + rng.uniform(0.05, 0.3, n)
        low = close - rng.uniform(0.05, 0.3, n)
        open_ = close + rng.normal(0, 0.08, n)
        vol = rng.integers(2000, 9000, n).astype(float)
        oi = 1.0e6 + 4000.0 * np.arange(n)          # steadily building OI
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": vol, "oi": oi},
                            index=pd.DatetimeIndex(idx))

    def make_chain(spot, now_epoch, lot=500, dte_days=12.0, sigma=0.30):
        step = 2.5
        atm = round(spot / step) * step
        t = dte_days / 365.0
        rows = []
        for i in range(-8, 9):
            k = atm + i * step
            coi = 1.0e5 + 8e3 * abs(i)
            poi = 1.3e5 + 6e3 * abs(i)               # put-heavy: PCR > 1
            ce = OptionLeg("TC", "1", ltp=round(
                max(0.05, bs_price(True, spot, k, t, sigma)), 2),
                oi=coi, volume=40 * lot)
            pe = OptionLeg("TP", "2", ltp=round(
                max(0.05, bs_price(False, spot, k, t, sigma)), 2),
                oi=poi, volume=55 * lot)
            rows.append(StrikeRow(k, ce, pe))
        return ChainSnapshot("TEST", now_epoch + dte_days * 86400.0,
                             lot, spot, rows)

    def make_cash_quote(spot):
        # Shoonya-style: everything is a string
        return {"lp": f"{spot:.2f}", "ap": f"{spot * 0.998:.2f}",
                "tbq": "520000", "tsq": "340000",
                "bq1": "1200", "bq2": "900", "bq3": "700", "bq4": "500",
                "bq5": "400", "sq1": "800", "sq2": "650", "sq3": "500",
                "sq4": "450", "sq5": "300",
                "bp1": f"{spot - 0.05:.2f}", "sp1": f"{spot + 0.05:.2f}",
                "uc": f"{spot * 1.10:.2f}", "lc": f"{spot * 0.90:.2f}",
                "v": "2400000"}

    def make_futures(spot, now_epoch):
        return {"lp": f"{spot * 1.002:.2f}", "c": f"{spot * 0.995:.2f}",
                "pc": "0.70", "oi": "5200000", "poi": "5000000",
                "v": "1800000", "ls": "500",
                "expiry_epoch": now_epoch + 20 * 86400.0}

    class Ctx:
        def __init__(self, direction, df, chain=None, cash_quote=None,
                     futures=None, daily=None, vix=None):
            self.symbol = "TEST"
            self.direction = direction
            self.df = df
            self.spot = float(df["close"].iloc[-1])
            self.now = df.index[-1].timestamp() + 300.0
            self.meta = {}
            self.chain = chain
            self.futures = futures
            self.vix = vix
            self.daily = daily
            self.cash_quote = cash_quote
            try:
                v = float(_atr5(df, 14).iloc[-1])
                self.atr14 = v if v == v and v > 0 else None
            except Exception:
                self.atr14 = None

    df = make_df()
    spot = float(df["close"].iloc[-1])
    now = df.index[-1].timestamp() + 300.0
    chain = make_chain(spot, now)
    cq = make_cash_quote(spot)
    fut = make_futures(spot, now)

    # sparse frame: 3 bars today only, zero volume (halted) — worst case
    sparse = df.iloc[-3:].copy()
    sparse["volume"] = 0.0
    sparse["oi"] = 0.0

    cfg = Cfg()
    agent = build()
    print("tree:", [c.key for c in agent.walk()])

    scenarios = [
        ("full BUY", Ctx("BUY", df, chain, cq, fut)),
        ("full SELL", Ctx("SELL", df, chain, cq, fut)),
        ("bare BUY (no chain/quote/futures)", Ctx("BUY", df)),
        ("bare SELL", Ctx("SELL", df)),
        ("sparse+halted BUY", Ctx("BUY", sparse)),
    ]
    failures = 0
    for label, ctx in scenarios:
        res = agent.evaluate(ctx, cfg)
        print(f"\n[{label}] family={res.score} ({res.detail}) "
              f"flags={ctx.meta.get('volume.flags')}")
        for k in res.children:
            print(f"  {k.key:18s} score={k.score} avail={k.available} :: "
                  f"{k.detail}")
            if k.detail.startswith("error:"):
                failures += 1
    assert failures == 0, f"{failures} leaf exceptions"

    # data-present scenarios: all 5 live leaves must score
    for label, ctx in scenarios[:2]:
        res = agent.evaluate(ctx, cfg)
        live = [k for k in res.children if k.key != "volume_profile"]
        assert all(k.score is not None for k in live), (label, live)
    # BUY/SELL mirror on directional leaves
    b = agent.evaluate(Ctx("BUY", df, chain, cq, fut), cfg)
    s = agent.evaluate(Ctx("SELL", df, chain, cq, fut), cfg)
    for kb, ks in zip(b.children, s.children):
        if kb.key in ("pcr", "orderflow", "futures_oi_regime") and \
                kb.score is not None and ks.score is not None:
            assert abs((kb.score + ks.score) - 100.0) <= 1.0, (kb.key, kb, ks)
    # rel_volume is direction-agnostic
    rb = next(k for k in b.children if k.key == "rel_volume")
    rs = next(k for k in s.children if k.key == "rel_volume")
    assert rb.score == rs.score
    print("\nself-test OK")

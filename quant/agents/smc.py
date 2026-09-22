"""ICT / Smart-Money-Concepts agent family (spec: docs/specs/smc.json).

Five leaf agents, all working off ctx.df (closed 5m candles, naive-IST
DatetimeIndex, oldest first) only — no chain / depth / VIX needed:

  smc_structure  BOS / CHoCH / MSS via 3-bar fractals + trend state machine
  smc_zones      order blocks, FVGs, breakers with full lifecycle
  smc_liquidity  equal-high/low pools, PDH/PDL, sweeps, dealing range
  smc_timing     NSE session killzones + opening range / gap / expiry mods
  smc_advanced   Power-of-3 (AMD) blended with displacement-leg FVG cluster

Shared conventions (spec notes): clamp 0-100, 50 = neutral; Wilder ATR14 on
the shared 5m array; the FVG detector is shared between zones and advanced;
<60 closed bars -> neutral 50 (degraded), missing frame -> (None, reason).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

try:
    import qcore                   # C++ zone-sim kernel; _simulate_all falls
except ImportError:                # back to the Python _simulate when absent
    qcore = None
from quant.base import QuantAgent
from signals.indicators import atr

# ── shared params ────────────────────────────────────────────────────────
ATR_PERIOD = 14
MIN_BARS = 60

# structure params
FRACTAL_WING = 3
PIVOT_HISTORY = 20
BREAK_BUFFER = 0.05          # * ATR14
EVENT_LOOKBACK = 40          # bars
EVENT_HALFLIFE = 15.0        # bars (decay constant)
MSS_BODY_MIN = 0.6           # * ATR14

# zones params
DISP_BODY_MULT = 1.2         # * ATR
DISP_TWO_BAR_MULT = 1.8      # * ATR
FVG_MIN_GAP = 0.15           # * ATR
FVG_MID_BODY_MIN = 0.8       # * ATR
INVALIDATION_BUFFER = 0.05   # * ATR
MITIGATIONS_TO_DEATH = 2
MITIGATION_SEPARATION = 3    # bars fully outside
BREAKER_TTL = 50             # bars
MAX_ZONES_PER_SIDE = 8
ZONE_MAX_AGE = 200           # bars
SUPPORT_DECAY = 0.6          # * ATR
OVERHEAD_DECAY = 0.9         # * ATR
ZONES_BASE_SCORE = 45.0

# liquidity params
MINOR_FRACTAL_N = 2
POOL_LOOKBACK = 75           # bars
CLUSTER_TOL_ATR = 0.15       # * ATR
CLUSTER_TOL_BPS = 0.0005     # 5 bps of price
SWEEP_PIERCE_BUFFER = 0.05   # * ATR
RECLAIM_WINDOW = 2           # bars after pierce bar
BREAKDOWN_BARS = 3
STRONG_SWEEP_BODY = 0.6      # * ATR
SWEEP_LOOKBACK = 15          # bars
SWEEP_HALFLIFE = 8.0         # bars
DEALING_RANGE = 100          # bars
EXTERNAL_MULT = 1.25
LIQ_BASE_SCORE = 40.0

# timing params (minutes since midnight IST; bar belongs to window with its START)
KILLZONES = [
    (555, 565, 40),   # 09:15-09:25 erratic price discovery
    (565, 630, 80),   # 09:25-10:30 open-drive killzone
    (630, 690, 62),   # 10:30-11:30 post-open continuation
    (690, 780, 35),   # 11:30-13:00 mid-day chop
    (780, 855, 60),   # 13:00-14:15 re-engagement (London open)
    (855, 910, 75),   # 14:15-15:10 closing-hour killzone
    (910, 931, 25),   # 15:10-15:30 auto-square-off tail
]
OR_BARS = 6                  # 09:15-09:45
OR_END_MIN = 585             # 09:45
OR_MOD = 8
GAP_THRESHOLD = 0.003        # 0.30 %
GAP_MOD = 6
GAP_WINDOW_END = 630         # 10:30
EXPIRY_MOD = -10
EXPIRY_FROM_MIN = 690        # 11:30
SESSION_START_MIN = 555      # 09:15

# advanced params
ACCUM_END_MIN = 615          # 10:15 (12 bars)
ACCUM_END_FALLBACK = 645     # 10:45 (18 bars)
ACCUM_MIN_RANGE = 0.8        # * ATR
AMD_SWEEP_BUFFER = 0.1       # * ATR
AMD_RECLAIM_WINDOW = 4       # bars counting the pierce bar
FRESH_REVERSAL_WINDOW = 3    # bars
DISP_RECLAIM_BODY = 0.6      # * ATR
LEG_NET_MOVE = 1.8           # * ATR
LEG_MAX_BARS = 5
LEG_PURITY = 0.70
LEG_SCAN = 30                # bars
FVG_COUNT_CAP = 3
FVG_PROXIMITY = 0.2          # * ATR
BLEND_A = 0.55
BLEND_B = 0.45


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, float(x)))


# ── shared per-context computation (candles + ATR + FVGs) ────────────────
def make_shared(df: pd.DataFrame) -> dict:
    """Ctx-independent shared computation: OHLC arrays + ATR + FVG list.

    ``df`` must be a candle DataFrame (open/high/low/close, oldest first).
    External code can feed the result straight into the leaf internals, e.g.
    ``SmcStructure._run_state_machine(make_shared(df))`` — no MarketContext
    needed.
    """
    try:
        a_series = atr(df, ATR_PERIOD)
        a_vals = a_series.values.astype(float)
        a_last = float(a_vals[-1]) if len(a_vals) else float("nan")
    except Exception:
        a_vals = np.full(len(df), np.nan)
        a_last = float("nan")
    shared = {
        "n": len(df),
        "o": df["open"].values.astype(float),
        "h": df["high"].values.astype(float),
        "l": df["low"].values.astype(float),
        "c": df["close"].values.astype(float),
        "atr_vals": a_vals,
        "atr": a_last,
        "index": df.index,
    }
    shared["fvgs"] = _find_fvgs(shared)
    return shared


def _shared(ctx):
    """Compute-once cache in ctx.meta: ATR series/last + FVG list."""
    df = getattr(ctx, "df", None)
    if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0:
        return None
    cached = ctx.meta.get("_smc_shared")
    if cached is not None and cached.get("n") == len(df):
        return cached
    shared = make_shared(df)
    ctx.meta["_smc_shared"] = shared
    return shared


def _atr_at(sh, i: int) -> float | None:
    """ATR value at bar i, falling back to the latest valid value."""
    v = sh["atr_vals"][i] if 0 <= i < len(sh["atr_vals"]) else float("nan")
    if v == v and v > 0:
        return float(v)
    v = sh["atr"]
    return float(v) if v == v and v > 0 else None


def _find_fvgs(sh) -> list[dict]:
    """3-candle imbalances with the spec's noise filters (shared detector)."""
    o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
    n = sh["n"]
    out = []
    for i in range(2, n):
        a, b = i - 2, i - 1
        A = _atr_at(sh, b)
        if not A:
            continue
        if abs(c[b] - o[b]) < FVG_MID_BODY_MIN * A:
            continue
        if l[i] > h[a] and (l[i] - h[a]) >= FVG_MIN_GAP * A:
            out.append({"kind": "bull", "a": a, "cbar": i,
                        "bottom": float(h[a]), "top": float(l[i])})
        elif h[i] < l[a] and (l[a] - h[i]) >= FVG_MIN_GAP * A:
            out.append({"kind": "bear", "a": a, "cbar": i,
                        "bottom": float(h[i]), "top": float(l[a])})
    return out


def _pivots(sh, wing: int) -> list[tuple[int, float, str]]:
    """Confirmed fractal pivots: >=/<= on the left, strict on the right.
    Confirmation bar = i + wing (only fully-confirmable pivots returned)."""
    h, l = sh["h"], sh["l"]
    n = sh["n"]
    out = []
    for i in range(wing, n - wing):
        if all(h[i] >= h[i - k] for k in range(1, wing + 1)) and \
           all(h[i] > h[i + k] for k in range(1, wing + 1)):
            out.append((i, float(h[i]), "H"))
        if all(l[i] <= l[i - k] for k in range(1, wing + 1)) and \
           all(l[i] < l[i + k] for k in range(1, wing + 1)):
            out.append((i, float(l[i]), "L"))
    return out


def _session_minute(ts) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


# ═════════════════════════════ 1. STRUCTURE ══════════════════════════════
class SmcStructure(QuantAgent):
    key = "smc_structure"
    name = "SMC Market Structure (BOS / CHoCH / MSS)"
    description = ("3-bar fractal swings + trend state machine emitting "
                   "BOS/CHoCH/MSS events, scored with recency decay")
    default_weight_buy = 1.0
    default_weight_sell = 1.0

    def compute(self, ctx):
        sh = _shared(ctx)
        if sh is None:
            return None, "no 5m candle data"
        n = sh["n"]
        if n < MIN_BARS:
            return 50.0, f"degraded: only {n} bars (<{MIN_BARS}) — neutral"
        A = sh["atr"]
        if not (A == A and A > 0):
            return None, "ATR14 unavailable"

        st = self._run_state_machine(sh)
        trend, events = st["trend"], st["events"]
        highs, lows = st["highs"], st["lows"]
        o, c = sh["o"], sh["c"]
        P = float(c[-1])
        buy = ctx.direction == "BUY"

        # trend term
        if trend == "UP":
            trend_term = 15.0 if buy else -15.0
        elif trend == "DOWN":
            trend_term = -15.0 if buy else 15.0
        else:
            trend_term = 0.0

        # event term (most recent event, only if within lookback)
        buy_vals = {"MSS_UP": 35, "CHoCH_UP": 28, "BOS_UP": 18,
                    "BOS_DOWN": -18, "CHoCH_DOWN": -28, "MSS_DOWN": -35}
        event_term, ev_txt = 0.0, "none"
        if events:
            ev, bar = events[-1]
            bars_since = (n - 1) - bar
            if bars_since <= EVENT_LOOKBACK:
                v = buy_vals[ev] if buy else -buy_vals[ev]
                event_term = v * math.exp(-bars_since / EVENT_HALFLIFE)
                ev_txt = f"{ev}({bars_since}b ago)"

        # geometry term
        geom = 0.0
        if len(highs) >= 2 and len(lows) >= 2:
            highs_up = highs[-1][1] > highs[-2][1]
            lows_up = lows[-1][1] > lows[-2][1]
            if highs_up and lows_up:
                geom = 8.0 if buy else -8.0
            elif (not highs_up) and (not lows_up):
                geom = -8.0 if buy else 8.0
        prox = 0.0
        last_pl = lows[-1][1] if lows else None
        last_ph = highs[-1][1] if highs else None
        if buy:
            if last_pl is not None and 0 <= P - last_pl <= 0.5 * A:
                prox += 7.0     # buying at structural support
            if last_ph is not None and 0 <= last_ph - P <= 0.5 * A:
                prox -= 7.0     # buying into resistance
        else:
            if last_ph is not None and 0 <= last_ph - P <= 0.5 * A:
                prox += 7.0     # selling at resistance
            if last_pl is not None and 0 <= P - last_pl <= 0.5 * A:
                prox -= 7.0     # selling into support

        score = _clamp(50.0 + trend_term + event_term + geom + prox)
        detail = (f"trend={trend} evt={ev_txt} "
                  f"t{trend_term:+.0f} e{event_term:+.1f} "
                  f"g{geom + prox:+.0f} -> {score:.0f}")
        return score, detail

    @staticmethod
    def _run_state_machine(sh) -> dict:
        o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
        n = sh["n"]
        piv = _pivots(sh, FRACTAL_WING)
        confirm: dict[int, list] = {}
        for i, price, kind in piv:
            confirm.setdefault(i + FRACTAL_WING, []).append((i, price, kind))

        trend = "NONE"
        ph = None    # last unbroken pivot high (idx, price)
        pl = None    # last unbroken pivot low
        highs: list[tuple[int, float]] = []
        lows: list[tuple[int, float]] = []
        events: list[tuple[str, int]] = []

        for t in range(n):
            for (i, price, kind) in confirm.get(t, ()):
                if kind == "H":
                    highs.append((i, price))
                    ph = (i, price)
                else:
                    lows.append((i, price))
                    pl = (i, price)
            A = _atr_at(sh, t)
            if not A:
                continue
            buf = BREAK_BUFFER * A
            # ── bull break of last unbroken pivot high ──
            if ph is not None and c[t] > ph[1] + buf:
                if trend == "UP":
                    events.append(("BOS_UP", t))
                elif trend == "DOWN":
                    ev = "CHoCH_UP"
                    prev_low = next((pr for idx, pr in reversed(lows)
                                     if idx < ph[0]), None)
                    leg_low = float(l[ph[0]:t + 1].min())
                    if (prev_low is not None and leg_low < prev_low
                            and abs(c[t] - o[t]) >= MSS_BODY_MIN * A):
                        ev = "MSS_UP"     # CHoCH + sweep + displacement
                    events.append((ev, t))
                    trend = "UP"
                else:
                    events.append(("BOS_UP", t))
                    trend = "UP"
                ph = None                 # pivot consumed
            # ── bear break of last unbroken pivot low ──
            if pl is not None and c[t] < pl[1] - buf:
                if trend == "DOWN":
                    events.append(("BOS_DOWN", t))
                elif trend == "UP":
                    ev = "CHoCH_DOWN"
                    prev_high = next((pr for idx, pr in reversed(highs)
                                      if idx < pl[0]), None)
                    leg_high = float(h[pl[0]:t + 1].max())
                    if (prev_high is not None and leg_high > prev_high
                            and abs(c[t] - o[t]) >= MSS_BODY_MIN * A):
                        ev = "MSS_DOWN"
                    events.append((ev, t))
                    trend = "DOWN"
                else:
                    events.append(("BOS_DOWN", t))
                    trend = "DOWN"
                pl = None
        return {"trend": trend, "events": events,
                "highs": highs[-PIVOT_HISTORY:], "lows": lows[-PIVOT_HISTORY:]}


# ═════════════════════════════ 2. ZONES ══════════════════════════════════
class SmcZones(QuantAgent):
    key = "smc_zones"
    name = "SMC Zones (Order Blocks, FVG, Breaker)"
    description = ("Order blocks, fair value gaps and breakers with "
                   "mitigation/invalidation lifecycle; exponential "
                   "distance-to-zone scoring")
    default_weight_buy = 0.9
    default_weight_sell = 0.9

    def compute(self, ctx):
        sh = _shared(ctx)
        if sh is None:
            return None, "no 5m candle data"
        n = sh["n"]
        if n < MIN_BARS:
            return 50.0, f"degraded: only {n} bars (<{MIN_BARS}) — neutral"
        A = sh["atr"]
        if not (A == A and A > 0):
            return None, "ATR14 unavailable"

        zones = self._active_zones(sh)
        P = float(sh["c"][-1])
        buy = ctx.direction == "BUY"

        sup_pol, ovh_pol = ("bull", "bear") if buy else ("bear", "bull")
        base_map = {("OB", True): 40.0, ("OB", False): 35.0,
                    ("BRK", False): 32.0, ("BRK", True): 32.0,
                    ("FVG", False): 30.0, ("FVG", True): 30.0}

        def fresh_mult(z):
            f = 1.0 if z["mitig"] == 0 else 0.65
            if z["weakened"]:
                f *= 0.7
            return f

        # support side: nearest same-direction zone at/behind price
        support_term, sup_txt = 0.0, "none"
        best = None
        for z in zones:
            if z["pol"] != sup_pol:
                continue
            if buy:
                ok = z["top"] <= P or (z["bottom"] <= P <= z["top"])
                d = max(0.0, P - z["top"])
            else:
                ok = z["bottom"] >= P or (z["bottom"] <= P <= z["top"])
                d = max(0.0, z["bottom"] - P)
            if ok and (best is None or d < best[0]):
                best = (d, z)
        if best is not None:
            d, z = best
            base = base_map[(z["kind"], z["has_fvg"])]
            support_term = base * fresh_mult(z) * math.exp(-d / (SUPPORT_DECAY * A))
            sup_txt = f"{z['kind']}{'+FVG' if z['has_fvg'] and z['kind'] == 'OB' else ''}@{d / A:.2f}ATR"

        # overhead side: nearest opposite zone in front of price
        overhead_term, extra = 0.0, 0.0
        best = None
        for z in zones:
            if z["pol"] != ovh_pol:
                continue
            inside = z["bottom"] <= P <= z["top"]
            if inside:
                extra = -10.0
            if buy:
                ok = z["bottom"] >= P or inside
                d2 = max(0.0, z["bottom"] - P)
            else:
                ok = z["top"] <= P or inside
                d2 = max(0.0, P - z["top"])
            if ok and (best is None or d2 < best[0]):
                best = (d2, z)
        if best is not None:
            d2, z = best
            overhead_term = 25.0 * fresh_mult(z) * math.exp(-d2 / (OVERHEAD_DECAY * A))

        score = _clamp(ZONES_BASE_SCORE + support_term - overhead_term + extra)
        detail = (f"sup={sup_txt}{support_term:+.1f} ovh{-overhead_term:+.1f}"
                  f"{' inside-opp-10' if extra else ''} "
                  f"({len(zones)} active) -> {score:.0f}")
        return score, detail

    # ── zone detection + lifecycle ───────────────────────────────────────
    @classmethod
    def _active_zones(cls, sh) -> list[dict]:
        o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
        n = sh["n"]
        zones: list[dict] = []

        # order blocks
        for i in range(n - 1):
            A = _atr_at(sh, i + 1)
            if not A:
                continue
            # bullish OB: bearish candle then bullish displacement closing above its high
            if c[i] < o[i] and c[i + 1] > o[i + 1]:
                b1 = c[i + 1] - o[i + 1]
                end = None
                if b1 >= DISP_BODY_MULT * A and c[i + 1] > h[i]:
                    end = i + 1
                elif (i + 2 < n and c[i + 2] > o[i + 2]
                      and b1 + (c[i + 2] - o[i + 2]) >= DISP_TWO_BAR_MULT * A
                      and c[i + 2] > h[i]):
                    end = i + 2
                if end is not None:
                    has_fvg = any(z["kind"] == "bull" and z["a"] >= i
                                  and z["cbar"] <= i + 3 for z in sh["fvgs"])
                    zones.append({"kind": "OB", "pol": "bull",
                                  "bottom": float(l[i]), "top": float(h[i]),
                                  "formed": end, "has_fvg": has_fvg})
            # bearish OB mirrored
            if c[i] > o[i] and c[i + 1] < o[i + 1]:
                b1 = o[i + 1] - c[i + 1]
                end = None
                if b1 >= DISP_BODY_MULT * A and c[i + 1] < l[i]:
                    end = i + 1
                elif (i + 2 < n and c[i + 2] < o[i + 2]
                      and b1 + (o[i + 2] - c[i + 2]) >= DISP_TWO_BAR_MULT * A
                      and c[i + 2] < l[i]):
                    end = i + 2
                if end is not None:
                    has_fvg = any(z["kind"] == "bear" and z["a"] >= i
                                  and z["cbar"] <= i + 3 for z in sh["fvgs"])
                    zones.append({"kind": "OB", "pol": "bear",
                                  "bottom": float(l[i]), "top": float(h[i]),
                                  "formed": end, "has_fvg": has_fvg})

        # FVGs
        for f in sh["fvgs"]:
            zones.append({"kind": "FVG", "pol": f["kind"],
                          "bottom": f["bottom"], "top": f["top"],
                          "formed": f["cbar"], "has_fvg": False})

        # lifecycle + breaker conversion (zone sims run in one batched pass)
        cls._simulate_all(zones, sh)
        final: list[dict] = []
        breakers: list[dict] = []
        for z in zones:
            if z["state"] == "INVALIDATED":
                if z["kind"] == "OB":     # polarity flip -> breaker
                    breakers.append({"kind": "BRK",
                                     "pol": "bear" if z["pol"] == "bull" else "bull",
                                     "bottom": z["bottom"], "top": z["top"],
                                     "formed": z["inv_at"], "has_fvg": False})
                continue                  # invalidated FVGs simply die
            if z["state"] == "DEAD":
                continue
            if (n - 1) - z["formed"] > ZONE_MAX_AGE:
                continue
            final.append(z)
        cls._simulate_all(breakers, sh)   # breakers simulated as a batch too
        for brk in breakers:
            if brk["state"] not in ("INVALIDATED", "DEAD") \
                    and (n - 1) - brk["formed"] <= BREAKER_TTL:
                final.append(brk)

        # cap 8 per polarity, newest first
        out = []
        for pol in ("bull", "bear"):
            side = sorted((z for z in final if z["pol"] == pol),
                          key=lambda z: -z["formed"])
            out.extend(side[:MAX_ZONES_PER_SIDE])
        return out

    @staticmethod
    def _simulate(z, sh):
        """FRESH -> MITIGATED -> DEAD, or INVALIDATED; weakened flag on
        wick-pierce-but-hold (mitigation-block variant)."""
        o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
        n = sh["n"]
        A = sh["atr"]
        buf = INVALIDATION_BUFFER * (A if A == A and A > 0 else 0.0)
        bull = z["pol"] == "bull"
        bot, top = z["bottom"], z["top"]
        mitig, weakened, state = 0, False, "FRESH"
        in_episode, outside_run = False, 10 ** 9
        inv_at = None
        for t in range(z["formed"] + 1, n):
            if bull:
                if c[t] < bot - buf:
                    state, inv_at = "INVALIDATED", t
                    break
                if l[t] < bot - buf and c[t] >= bot:
                    weakened = True
                touch = l[t] <= top
                fully_outside = (l[t] > top) or (h[t] < bot)
            else:
                if c[t] > top + buf:
                    state, inv_at = "INVALIDATED", t
                    break
                if h[t] > top + buf and c[t] <= top:
                    weakened = True
                touch = h[t] >= bot
                fully_outside = (l[t] > top) or (h[t] < bot)
            if in_episode:
                if fully_outside:
                    in_episode, outside_run = False, 1
            else:
                if fully_outside:
                    outside_run += 1
                elif touch:
                    if mitig == 0 or outside_run >= MITIGATION_SEPARATION:
                        mitig += 1
                        if mitig >= MITIGATIONS_TO_DEATH:
                            state = "DEAD"
                            break
                    in_episode = True
                    outside_run = 0
        if state == "FRESH" and mitig >= 1:
            state = "MITIGATED"
        z.update({"state": state, "mitig": mitig,
                  "weakened": weakened, "inv_at": inv_at})

    @classmethod
    def _simulate_all(cls, zones, sh):
        """Simulate the lifecycle of every zone in one pass.

        Uses the batched C++ kernel (``qcore.simulate_zones``, GIL released,
        matches ``_simulate`` bit-for-bit) when the extension is built; else the
        pure-Python per-zone ``_simulate``. Identical results either way — the
        C++ path just collapses the O(zones×n) loop off the interpreter.
        """
        fn = getattr(qcore, "simulate_zones", None)
        if fn is None or not zones:
            for z in zones:
                cls._simulate(z, sh)
            return
        A = sh["atr"]
        buf = INVALIDATION_BUFFER * (A if (A == A and A > 0) else 0.0)
        bottoms = np.array([z["bottom"] for z in zones], dtype=float)
        tops = np.array([z["top"] for z in zones], dtype=float)
        is_bull = np.array([1 if z["pol"] == "bull" else 0 for z in zones],
                           dtype=np.intc)
        formed = np.array([z["formed"] for z in zones], dtype=np.intc)
        st, mi, wk, iv = fn(sh["o"], sh["h"], sh["l"], sh["c"], float(buf),
                            bottoms, tops, is_bull, formed,
                            MITIGATION_SEPARATION, MITIGATIONS_TO_DEATH)
        names = ("FRESH", "MITIGATED", "DEAD", "INVALIDATED")
        for i, z in enumerate(zones):
            z["state"] = names[int(st[i])]
            z["mitig"] = int(mi[i])
            z["weakened"] = bool(wk[i])
            z["inv_at"] = None if int(iv[i]) < 0 else int(iv[i])


# ═════════════════════════════ 3. LIQUIDITY ══════════════════════════════
class SmcLiquidity(QuantAgent):
    key = "smc_liquidity"
    name = "SMC Liquidity (pools, sweeps, dealing range)"
    description = ("Equal-high/low clusters + PDH/PDL, sweep-and-reclaim "
                   "detection, discount/premium within 100-bar dealing range")
    default_weight_buy = 1.0
    default_weight_sell = 1.0

    def compute(self, ctx):
        sh = _shared(ctx)
        if sh is None:
            return None, "no 5m candle data"
        n = sh["n"]
        if n < MIN_BARS:
            return 50.0, f"degraded: only {n} bars (<{MIN_BARS}) — neutral"
        A = sh["atr"]
        if not (A == A and A > 0):
            return None, "ATR14 unavailable"

        h, l, c = sh["h"], sh["l"], sh["c"]
        P = float(c[-1])
        tol = max(CLUSTER_TOL_ATR * A, CLUSTER_TOL_BPS * P)
        buy = ctx.direction == "BUY"

        # dealing range
        rh = float(h[-DEALING_RANGE:].max())
        rl = float(l[-DEALING_RANGE:].min())
        range_pos = (P - rl) / max(rh - rl, 1e-9)

        pools_h, pools_l = self._pools(sh, tol, rh, rl)
        sweeps = self._sweeps(sh, pools_h, pools_l, A)

        # sweep term: most recent sweep event, if within lookback
        sweep_term, sw_txt = 0.0, "none"
        if sweeps:
            ev = max(sweeps, key=lambda e: e["bar"])
            bars_since = (n - 1) - ev["bar"]
            if bars_since <= SWEEP_LOOKBACK:
                decay = math.exp(-bars_since / SWEEP_HALFLIFE)
                supportive = (ev["side"] == "sell") if buy else (ev["side"] == "buy")
                if supportive:
                    q = 1.0
                    if ev["strong"]:
                        q *= 1.2
                    if ev["external"]:
                        q *= EXTERNAL_MULT
                    q *= 1.0 + 0.1 * (ev["size"] - 2)
                    q = min(q, 1.5)
                    sweep_term = 38.0 * q * decay
                    sw_txt = f"{ev['side']}-sweep {bars_since}b Q{q:.2f}"
                else:
                    sweep_term = -20.0 * decay
                    sw_txt = f"opp {ev['side']}-sweep {bars_since}b"

        # target / risk pools (untapped only)
        if buy:
            targets = [p for p in pools_h if p["untapped"] and p["level"] > P]
            risks = [p for p in pools_l if p["untapped"] and p["level"] < P]
        else:
            targets = [p for p in pools_l if p["untapped"] and p["level"] < P]
            risks = [p for p in pools_h if p["untapped"] and p["level"] > P]
        target_term = 0.0
        if targets:
            d = min(abs(p["level"] - P) for p in targets)
            if 0.7 * A <= d <= 3.0 * A:
                target_term = 10.0
            elif d < 0.7 * A:
                target_term = 4.0
        risk_term = 0.0
        if risks and min(abs(P - p["level"]) for p in risks) <= 1.0 * A:
            risk_term = -12.0

        if buy:
            range_term = max(-15.0, min(15.0, (0.5 - range_pos) * 30.0))
        else:
            range_term = max(-15.0, min(15.0, (range_pos - 0.5) * 30.0))

        score = _clamp(LIQ_BASE_SCORE + sweep_term + target_term
                       + risk_term + range_term)
        detail = (f"sweep={sw_txt}{sweep_term:+.1f} tgt{target_term:+.0f} "
                  f"risk{risk_term:+.0f} pos={range_pos:.2f}"
                  f"({range_term:+.1f}) -> {score:.0f}")
        return score, detail

    # ── pool construction ────────────────────────────────────────────────
    @staticmethod
    def _pools(sh, tol, rh, rl):
        h, l = sh["h"], sh["l"]
        n = sh["n"]
        piv = _pivots(sh, MINOR_FRACTAL_N)
        cutoff = n - POOL_LOOKBACK

        def cluster(swings, is_high):
            groups: list[dict] = []
            for i, p in sorted(swings, key=lambda x: -x[0]):   # newest first
                placed = False
                for g in groups:
                    ref = g["ext"]
                    if abs(p - ref) <= tol:
                        g["members"].append((i, p))
                        g["ext"] = max(ref, p) if is_high else min(ref, p)
                        g["newest"] = max(g["newest"], i)
                        placed = True
                        break
                if not placed:
                    groups.append({"ext": p, "members": [(i, p)], "newest": i})
            return [{"level": g["ext"], "size": len(g["members"]),
                     "newest": g["newest"], "external": False}
                    for g in groups if len(g["members"]) >= 2]

        pools_h = cluster([(i, p) for i, p, k in piv
                           if k == "H" and i >= cutoff], True)
        pools_l = cluster([(i, p) for i, p, k in piv
                           if k == "L" and i >= cutoff], False)

        # PDH / PDL from the previous session's bars
        try:
            days = sh["index"].normalize()
            uniq = days.unique()
            if len(uniq) >= 2:
                prev_mask = (days == uniq[-2]).values if hasattr(days == uniq[-2], "values") \
                    else np.asarray(days == uniq[-2])
                idxs = np.where(prev_mask)[0]
                if len(idxs):
                    last_prev = int(idxs[-1])
                    pools_h.append({"level": float(h[idxs].max()), "size": 2,
                                    "newest": last_prev, "external": True})
                    pools_l.append({"level": float(l[idxs].min()), "size": 2,
                                    "newest": last_prev, "external": True})
        except Exception:
            pass

        for p in pools_h:
            if abs(p["level"] - rh) <= tol:
                p["external"] = True       # dealing-range extreme
            p["untapped"] = not (h[p["newest"] + 1:] > p["level"] + tol).any()
        for p in pools_l:
            if abs(p["level"] - rl) <= tol:
                p["external"] = True
            p["untapped"] = not (l[p["newest"] + 1:] < p["level"] - tol).any()
        return pools_h, pools_l

    # ── sweep detection ──────────────────────────────────────────────────
    @staticmethod
    def _sweeps(sh, pools_h, pools_l, A):
        o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
        n = sh["n"]
        buf = SWEEP_PIERCE_BUFFER * A
        events = []
        # sell-side sweeps of lows pools (bullish event)
        for p in pools_l:
            lvl = p["level"]
            for s in range(p["newest"] + 1, n):
                if l[s] < lvl - buf:
                    for r in range(s, min(s + RECLAIM_WINDOW + 1, n)):
                        if c[r] > lvl:
                            events.append({
                                "bar": r, "side": "sell", "size": p["size"],
                                "external": p["external"],
                                "strong": abs(c[r] - o[r]) >= STRONG_SWEEP_BODY * A})
                            break
                    break   # each pool resolves on first pierce (swept or breakdown)
        # buy-side sweeps of highs pools (bearish event)
        for p in pools_h:
            lvl = p["level"]
            for s in range(p["newest"] + 1, n):
                if h[s] > lvl + buf:
                    for r in range(s, min(s + RECLAIM_WINDOW + 1, n)):
                        if c[r] < lvl:
                            events.append({
                                "bar": r, "side": "buy", "size": p["size"],
                                "external": p["external"],
                                "strong": abs(c[r] - o[r]) >= STRONG_SWEEP_BODY * A})
                            break
                    break
        return events


# ═════════════════════════════ 4. TIMING ═════════════════════════════════
class SmcTiming(QuantAgent):
    key = "smc_timing"
    name = "NSE Session Killzones (timing)"
    description = ("Killzone table for the 09:15-15:30 NSE session with "
                   "opening-range, gap and expiry-day modifiers")
    default_weight_buy = 0.6
    default_weight_sell = 0.6

    def compute(self, ctx):
        df = getattr(ctx, "df", None)
        if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0:
            return None, "no 5m candle data"
        ts = df.index[-1]
        m = _session_minute(ts)
        base = None
        for start, end, sc in KILLZONES:
            if start <= m < end:
                base = float(sc)
                break
        if base is None:
            base = 40.0 if m < SESSION_START_MIN else 25.0
        buy = ctx.direction == "BUY"
        P = float(df["close"].iloc[-1])

        # today's / previous-day slices
        days = df.index.normalize()
        today = days[-1]
        tmask = np.asarray(days == today)
        tidx = np.where(tmask)[0]
        first_today_min = _session_minute(df.index[tidx[0]]) if len(tidx) else None

        # opening-range modifier (from 09:45 onward, needs open coverage)
        or_mod, or_txt = 0.0, ""
        if (m >= OR_END_MIN and first_today_min is not None
                and first_today_min <= SESSION_START_MIN + 5):
            mins = np.array([_session_minute(df.index[i]) for i in tidx])
            or_sel = tidx[(mins >= SESSION_START_MIN) & (mins < OR_END_MIN)]
            if len(or_sel):
                or_hi = float(df["high"].values[or_sel].max())
                or_lo = float(df["low"].values[or_sel].min())
                if P > or_hi:
                    or_mod = OR_MOD if buy else -OR_MOD
                    or_txt = " P>ORH"
                elif P < or_lo:
                    or_mod = -OR_MOD if buy else OR_MOD
                    or_txt = " P<ORL"

        # gap modifier (before 10:30 only)
        gap_mod, gap_txt = 0.0, ""
        prev_idx = np.where(~tmask)[0]
        if (m < GAP_WINDOW_END and len(prev_idx) and len(tidx)
                and first_today_min is not None
                and first_today_min <= SESSION_START_MIN + 5):
            pdc = float(df["close"].values[prev_idx[-1]])
            today_open = float(df["open"].values[tidx[0]])
            if pdc > 0:
                gap = (today_open - pdc) / pdc
                if abs(gap) >= GAP_THRESHOLD:
                    up = gap > 0
                    gap_mod = (GAP_MOD if up else -GAP_MOD) if buy \
                        else (-GAP_MOD if up else GAP_MOD)
                    gap_txt = f" gap{gap * 100:+.2f}%"

        # expiry-day modifier (orchestrator-supplied flag; absent -> 0)
        exp_mod = 0.0
        if ctx.meta.get("is_expiry_day") and m >= EXPIRY_FROM_MIN:
            exp_mod = float(EXPIRY_MOD)

        score = _clamp(base + or_mod + gap_mod + exp_mod)
        detail = (f"{ts.strftime('%H:%M')} base={base:.0f}{or_txt}"
                  f"{f'({or_mod:+.0f})' if or_mod else ''}{gap_txt}"
                  f"{f'({gap_mod:+.0f})' if gap_mod else ''}"
                  f"{' expiry-10' if exp_mod else ''} -> {score:.0f}")
        return score, detail


# ═════════════════════════════ 5. ADVANCED ═══════════════════════════════
class SmcAdvanced(QuantAgent):
    key = "smc_advanced"
    name = "Power of 3 (AMD) + Displacement-FVG Cluster"
    description = ("Session-adapted Accumulation-Manipulation-Distribution "
                   "(Judas swing) blended 55/45 with displacement-leg FVG "
                   "cluster confluence")
    default_weight_buy = 0.7
    default_weight_sell = 0.7

    def compute(self, ctx):
        sh = _shared(ctx)
        if sh is None:
            return None, "no 5m candle data"
        n = sh["n"]
        if n < MIN_BARS:
            return 50.0, f"degraded: only {n} bars (<{MIN_BARS}) — neutral"
        A = sh["atr"]
        if not (A == A and A > 0):
            return None, "ATR14 unavailable"
        buy = ctx.direction == "BUY"
        P = float(sh["c"][-1])

        a_score, a_txt = self._component_a(sh, A, P, buy)
        b_score, b_txt = self._component_b(sh, A, P, buy)
        score = _clamp(BLEND_A * a_score + BLEND_B * b_score)
        return score, (f"A[{a_txt}]={a_score:.0f} B[{b_txt}]={b_score:.0f} "
                       f"-> {score:.0f}")

    # ── component A: session AMD / Judas swing ──────────────────────────
    @staticmethod
    def _component_a(sh, A, P, buy):
        o, h, l, c = sh["o"], sh["h"], sh["l"], sh["c"]
        idx = sh["index"]
        days = idx.normalize()
        tmask = np.asarray(days == days[-1])
        tsel = np.where(tmask)[0]
        if not len(tsel):
            return 50.0, "no session bars"
        mins = np.array([_session_minute(idx[i]) for i in tsel])
        last_min = int(mins[-1])

        win_end = ACCUM_END_MIN
        acc = tsel[mins < win_end]
        if not len(acc) or last_min < win_end:
            return 50.0, "accum window open"
        ar_hi, ar_lo = float(h[acc].max()), float(l[acc].min())
        if ar_hi - ar_lo < ACCUM_MIN_RANGE * A:
            win_end = ACCUM_END_FALLBACK          # widen to 09:15-10:45
            if last_min < win_end:
                return 50.0, "accum window open (widened)"
            acc = tsel[mins < win_end]
            ar_hi, ar_lo = float(h[acc].max()), float(l[acc].min())
        ar_mid = (ar_hi + ar_lo) / 2.0
        post = tsel[mins >= win_end]

        buf = AMD_SWEEP_BUFFER * A

        def find_manip(down: bool):
            """First qualifying push per side; reclaim within 4 bars or breakout."""
            for k, t in enumerate(post):
                pierced = (l[t] < ar_lo - buf) if down else (h[t] > ar_hi + buf)
                if pierced:
                    for j in range(k, min(k + AMD_RECLAIM_WINDOW, len(post))):
                        r = post[j]
                        if (c[r] > ar_lo) if down else (c[r] < ar_hi):
                            return {"reclaim_bar": int(r),
                                    "disp": abs(c[r] - o[r]) >= DISP_RECLAIM_BODY * A}
                    return None     # breakout, not manipulation
            return None

        man_dn = find_manip(True)
        man_up = find_manip(False)
        if man_dn and man_up:
            oper = ("DOWN", man_dn) if man_dn["reclaim_bar"] >= man_up["reclaim_bar"] \
                else ("UP", man_up)
        elif man_dn:
            oper = ("DOWN", man_dn)
        elif man_up:
            oper = ("UP", man_up)
        else:
            return 50.0, "no manipulation"

        side, man = oper
        last_bar = sh["n"] - 1
        fresh = (last_bar - man["reclaim_bar"]) <= FRESH_REVERSAL_WINDOW
        bullish_model = side == "DOWN"    # Judas below -> bullish day model
        if (bullish_model and buy) or ((not bullish_model) and (not buy)):
            # supportive day model
            if bullish_model:
                if P > ar_mid:
                    s = 85.0
                elif ar_lo <= P <= ar_mid:
                    s = 70.0
                elif P < ar_lo and fresh:
                    s = 60.0
                else:
                    s = 55.0
            else:
                if P < ar_mid:
                    s = 85.0
                elif ar_mid <= P <= ar_hi:
                    s = 70.0
                elif P > ar_hi and fresh:
                    s = 60.0
                else:
                    s = 55.0
            if man["disp"]:
                s = min(95.0, s + 8.0)
        else:
            # opposing day model
            if bullish_model:               # SELL vs bullish model
                s = 30.0 if P > ar_mid else 40.0
            else:                           # BUY vs bearish model
                s = 30.0 if P < ar_mid else 40.0
        return s, f"judas-{side.lower()}"

    # ── component B: displacement leg + FVG cluster ──────────────────────
    @staticmethod
    def _component_b(sh, A, P, buy):
        o, c = sh["o"], sh["c"]
        n = sh["n"]
        lo = max(0, n - LEG_SCAN)
        want = 1 if buy else -1

        oper = None
        for e in range(n - 1, lo - 1, -1):
            for L in range(LEG_MAX_BARS, 0, -1):
                s = e - L + 1
                if s < lo:
                    continue
                net = c[e] - o[s]
                if abs(net) < LEG_NET_MOVE * A:
                    continue
                d = 1 if net > 0 else -1
                pure = sum(1 for j in range(s, e + 1)
                           if (c[j] - o[j]) * d > 0)
                if pure < LEG_PURITY * L:
                    continue
                kind = "bull" if d > 0 else "bear"
                fvgs = [f for f in sh["fvgs"] if f["kind"] == kind
                        and f["a"] >= s and f["cbar"] <= e]
                if fvgs:
                    oper = {"dir": d, "s": s, "e": e, "fvgs": fvgs}
                break                          # widest leg at this end bar
            if oper:
                break

        if oper is None:
            return 50.0, "no leg"
        nf = min(len(oper["fvgs"]), FVG_COUNT_CAP)
        if oper["dir"] == want:
            s = 60.0 + 8.0 * nf
            prox = FVG_PROXIMITY * A
            in_fvg = False
            for f in oper["fvgs"]:
                filled = (c[f["cbar"] + 1:] < f["bottom"]).any() if f["kind"] == "bull" \
                    else (c[f["cbar"] + 1:] > f["top"]).any()
                if filled:
                    continue
                if f["bottom"] - prox <= P <= f["top"] + prox:
                    in_fvg = True
                    break
            if in_fvg:
                s += 12.0
            s = min(96.0, s)
            return s, f"leg-with {nf}FVG{'+in' if in_fvg else ''}"
        s = max(20.0, 32.0 - 4.0 * nf)
        return s, f"leg-against {nf}FVG"


# ═════════════════════════════ family branch ═════════════════════════════
class SmcFamily(QuantAgent):
    key = "smc"
    name = "ICT / SMC Concepts"
    description = "Structure, zones, liquidity, timing, AMD"
    default_weight_buy = 1.0
    default_weight_sell = 1.0


def build() -> QuantAgent:
    return SmcFamily(children=[
        SmcStructure(),
        SmcZones(),
        SmcLiquidity(),
        SmcTiming(),
        SmcAdvanced(),
    ])


# ═════════════════════════════ self-test ═════════════════════════════════
if __name__ == "__main__":
    import time as _time

    class _Cfg:
        def enabled(self, key, default=True):
            return default

        def threshold(self, key):
            return 50

        def weight(self, key, symbol, direction, db, ds):
            return db if direction == "BUY" else ds

    class _Ctx:
        def __init__(self, df, direction):
            self.symbol = "TEST"
            self.direction = direction
            self.df = df
            self.spot = float(df["close"].iloc[-1]) if len(df) else 0.0
            self.now = _time.time()
            self.chain = None
            self.futures = None
            self.vix = None
            self.daily = None
            self.cash_quote = None
            self.atr14 = None
            self.meta = {}

    def _mk_df(n_bars):
        rng = np.random.default_rng(7)
        times = []
        day0 = pd.Timestamp("2026-06-29 09:15")     # Monday
        d = 0
        while len(times) < n_bars + 80:
            day = day0 + pd.Timedelta(days=d)
            d += 1
            if day.weekday() >= 5:
                continue
            for k in range(75):                     # 09:15 .. 15:25 starts
                times.append(day + pd.Timedelta(minutes=5 * k))
        idx = pd.DatetimeIndex(times[-n_bars:]) if n_bars else pd.DatetimeIndex([])
        n = len(idx)
        ret = rng.normal(0, 0.0015, n)
        ret[60:66] += 0.004                          # a displacement leg
        ret[120:124] -= 0.005
        close = 100.0 * np.exp(np.cumsum(ret))
        open_ = np.roll(close, 1)
        if n:
            open_[0] = 100.0
        high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.0012, n))
        low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.0012, n))
        vol = rng.integers(1000, 9000, n)
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "volume": vol, "oi": 0},
                            index=idx)

    cfg = _Cfg()
    tree = build()
    print("tree:", [a.key for a in tree.walk()])

    failures = []
    for nbars, label in [(250, "full"), (30, "short")]:
        df = _mk_df(nbars)
        for direction in ("BUY", "SELL"):
            ctx = _Ctx(df, direction)
            ctx.meta["is_expiry_day"] = (label == "full")
            res = tree.evaluate(ctx, cfg)
            print(f"\n[{label} {direction}] family={res.score} ({res.detail})")
            for ch in res.children:
                print(f"  {ch.key:15s} score={ch.score!s:6s} avail={ch.available} "
                      f"| {ch.detail}")
                if ch.detail.startswith("error:"):
                    failures.append((label, direction, ch.key, ch.detail))

    if failures:
        raise SystemExit(f"FAIL: {failures}")
    print("\nself-test OK — no leaf raised")

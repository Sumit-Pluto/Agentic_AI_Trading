"""Volatility agent family — VIX regime, IV level/skew, vega env, events.

Spec: docs/specs/volatility.json. Five leaves under the `volatility` branch:

  india_vix  — maps ctx.vix onto India-calibrated regime bands, plus a
               day-change adjustment computed from our own daily VIX store
               (state/vix_daily.json, updated inside compute) — no extra
               quote calls.
  iv_level   — inverts ATM monthly premiums to IV, scores blended IV
               percentile from a self-recorded per-symbol daily store
               (state/iv_store/<SYMBOL>.json) bootstrapped with absolute
               Indian single-stock IV bands.
  iv_skew    — 5%-OTM put-minus-call IV normalized by ATM IV with a
               5-day steepening adjustment off the same store.
  vega_env   — thin vol-crush risk flag for option execution (vomma/veta
               deliberately NOT scored: no vol-of-vol history exists).
  event      — proximity-decayed penalty from a user-maintained
               events.json at repo root + auto-added monthly F&O expiry
               Tuesdays. Missing file = no events = baseline score.

All stores are JSON under state/; corrupt/missing stores never crash.
pw_linear lives here (mathutils.py is frozen).
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import os
import statistics
import time

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:                                   # pragma: no cover
    _IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

from quant.base import QuantAgent, BUY
try:                                         # C++ kernel; else Python reference
    from qcore import bs_vega, years_to_expiry
except ImportError:
    from quant.mathutils import bs_vega, years_to_expiry

log = logging.getLogger("quant.volatility")

# ── paths ────────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_DIR = os.path.join(_ROOT, "state")
IV_STORE_DIR = os.path.join(STATE_DIR, "iv_store")
VIX_STORE_PATH = os.path.join(STATE_DIR, "vix_daily.json")
EVENTS_FILE = os.path.join(_ROOT, "events.json")

STORE_CAP = 400            # newest dates kept per store
_CACHE_TTL = 60.0          # store mtime-cache seconds


# ── shared helpers ───────────────────────────────────────────────────────
def pw_linear(x: float, pts) -> float:
    """Piecewise-linear interpolation over sorted (x_i, y_i); clamped ends."""
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])            # unreachable with sorted pts


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _ist_date(epoch: float) -> dt.date:
    return dt.datetime.fromtimestamp(epoch, tz=_IST).date()


# JSON store I/O — tolerant, atomic, 60s mtime cache -----------------------
_json_cache: dict = {}     # path -> (mtime, loaded_at, data)


def _load_store(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    now = time.time()
    ent = _json_cache.get(path)
    if ent and ent[0] == st.st_mtime and now - ent[1] < _CACHE_TTL:
        return ent[2]
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _json_cache[path] = (st.st_mtime, now, data)
    return data


def _save_store(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            mt = time.time()
        _json_cache[path] = (mt, time.time(), data)
    except Exception as e:              # persistence must never kill a score
        log.warning("store save failed %s: %s", path, e)


def _prune_dates(store: dict, cap: int = STORE_CAP) -> dict:
    if len(store) <= cap:
        return store
    keep = sorted(store.keys())[-cap:]
    return {k: store[k] for k in keep}


class IVStore:
    """Per-symbol daily {date: {'iv': float, 'skew': float|None}} store."""

    def __init__(self, symbol: str):
        safe = "".join(c for c in symbol.upper() if c.isalnum() or c in "-_")
        self.path = os.path.join(IV_STORE_DIR, f"{safe or 'UNKNOWN'}.json")

    def load(self) -> dict:
        return _load_store(self.path)

    def upsert(self, date_str: str, field: str, value: float) -> None:
        data = dict(self.load())
        rec = dict(data.get(date_str) or {})
        rec[field] = value
        data[date_str] = rec
        _save_store(self.path, _prune_dates(data))

    def history(self, field: str, today: str, limit: int) -> list[float]:
        """Newest-last values of `field`, excluding today, up to `limit`."""
        data = self.load()
        out = []
        for d in sorted(data.keys()):
            if d >= today:
                continue
            rec = data.get(d)
            if isinstance(rec, dict):
                v = rec.get(field)
                if isinstance(v, (int, float)):
                    out.append(float(v))
        return out[-limit:]


# ── ATM IV / percentile machinery shared by iv_level/iv_skew/vega_env ────
MIN_T_YEARS = 2.0 / 365.0
IV_SANITY = (0.05, 3.00)
ABS_TO_PCTL = [(0.12, 5), (0.18, 15), (0.25, 35), (0.32, 55),
               (0.42, 75), (0.60, 92), (0.80, 99)]
MIN_HIST = 20
FULL_HIST = 60
PCTL_WINDOW = 252


def _sorted_rows(chain):
    return sorted(chain.strikes, key=lambda r: r.strike)


def _row_ivs(row) -> list[float]:
    out = []
    for leg in (row.ce, row.pe):
        if leg is not None and leg.iv is not None and leg.iv > 0:
            out.append(float(leg.iv))
    return out


def _compute_atm_iv(chain, now_epoch: float):
    """Return (atm_iv, atm_strike, reason). atm_iv None => reason set."""
    t = years_to_expiry(chain.expiry_epoch, now_epoch)
    if t < MIN_T_YEARS:
        return None, None, "monthly expiry too close for stable IV"
    try:
        chain.compute_ivs()             # idempotent — set legs are skipped
    except Exception as e:
        return None, None, f"IV inversion failed: {e}"
    rows = _sorted_rows(chain)
    if not rows:
        return None, None, "empty option chain"
    k = chain.atm_strike()
    if k is None:
        return None, None, "no ATM strike"
    idx = min(range(len(rows)), key=lambda i: abs(rows[i].strike - k))
    ivs = _row_ivs(rows[idx])
    if not ivs:                         # nearest-first scan atm±1 then ±2
        for off in (-1, 1, -2, 2):
            j = idx + off
            if 0 <= j < len(rows):
                ivs = _row_ivs(rows[j])
                if ivs:
                    k = rows[j].strike
                    break
    if not ivs:
        return None, None, "no invertible ATM premium"
    atm_iv = sum(ivs) / len(ivs)
    if not (IV_SANITY[0] <= atm_iv <= IV_SANITY[1]):
        return None, None, f"ATM IV {atm_iv:.2f} out of sanity range"
    return atm_iv, k, None


def _blended_percentile(symbol: str, atm_iv: float, today: str):
    """(percentile, n_hist) blending empirical store pct with abs bands."""
    hist = IVStore(symbol).history("iv", today, PCTL_WINDOW)
    n = len(hist)
    p_abs = pw_linear(atm_iv, ABS_TO_PCTL)
    if n >= MIN_HIST:
        p_emp = 100.0 * sum(1 for h in hist if h < atm_iv) / n
        w = min(1.0, n / float(FULL_HIST))
        return w * p_emp + (1.0 - w) * p_abs, n
    return p_abs, n


# ═════════════════════════════════════════════════════════════════════════
# 1. India VIX Regime
# ═════════════════════════════════════════════════════════════════════════
VIX_ANCHORS_BUY = [(9, 70), (11, 85), (13, 90), (15, 82), (17, 70),
                   (20, 52), (24, 38), (30, 20), (40, 8)]
VIX_ANCHORS_SELL = [(9, 60), (11, 70), (13, 78), (15, 82), (17, 80),
                    (20, 68), (24, 55), (30, 38), (40, 20)]
DAYCHG_COEF_BUY, DAYCHG_CLAMP_BUY = -1.5, (-15.0, 10.0)
DAYCHG_COEF_SELL, DAYCHG_CLAMP_SELL = 1.0, (-10.0, 10.0)


def _vix_regime(v: float) -> str:
    if v < 11:
        return "complacent"
    if v < 14:
        return "calm"
    if v < 18:
        return "normal"
    if v < 24:
        return "elevated"
    if v <= 32:
        return "fear"
    return "panic"


class IndiaVixAgent(QuantAgent):
    key = "india_vix"
    name = "India VIX Regime"
    description = ("India VIX mapped onto India-calibrated regime bands "
                   "(<11 complacent … >32 panic) with a day-change "
                   "adjustment from the local daily VIX store.")
    default_weight_buy = 1.0
    default_weight_sell = 1.0

    def compute(self, ctx):
        v = ctx.vix
        if v is None or v <= 0:
            return None, "India VIX unavailable"
        v = float(v)

        today = _ist_date(ctx.now).isoformat()
        # day change vs the most recent prior stored close
        pc = None
        try:
            store = dict(_load_store(VIX_STORE_PATH))
            prior = [d for d in store.keys()
                     if d < today and isinstance(store[d], (int, float))]
            if prior:
                prev = float(store[max(prior)])
                if prev > 0:
                    pc = (v - prev) / prev * 100.0
            store[today] = round(v, 2)      # last write of the day wins
            _save_store(VIX_STORE_PATH, _prune_dates(store))
        except Exception as e:
            log.warning("vix store failed: %s", e)

        if ctx.direction == BUY:
            base = pw_linear(v, VIX_ANCHORS_BUY)
            adj = (_clamp(DAYCHG_COEF_BUY * pc, *DAYCHG_CLAMP_BUY)
                   if pc is not None else 0.0)
        else:
            base = pw_linear(v, VIX_ANCHORS_SELL)
            adj = (_clamp(DAYCHG_COEF_SELL * pc, *DAYCHG_CLAMP_SELL)
                   if pc is not None else 0.0)
        score = _clamp(base + adj, 0.0, 100.0)
        detail = f"VIX {v:.2f} ({_vix_regime(v)})"
        if pc is not None:
            detail += f", day chg {pc:+.1f}%"
        return score, detail


# ═════════════════════════════════════════════════════════════════════════
# 2. ATM IV Level (percentile with absolute-band bootstrap)
# ═════════════════════════════════════════════════════════════════════════
IVL_SCORE_BUY = [(0, 55), (20, 80), (40, 95), (55, 85), (70, 60),
                 (85, 38), (100, 15)]
IVL_SCORE_SELL = [(0, 50), (20, 70), (40, 85), (55, 88), (70, 72),
                  (85, 52), (100, 25)]


class IVLevelAgent(QuantAgent):
    key = "iv_level"
    name = "ATM IV Level"
    description = ("ATM monthly IV scored via blended IV percentile from a "
                   "self-recorded daily store, bootstrapped with absolute "
                   "Indian single-stock IV bands.")
    default_weight_buy = 1.0
    default_weight_sell = 1.0

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None:
            return None, "option chain unavailable"
        atm_iv, atm_strike, why = _compute_atm_iv(chain, ctx.now)
        if atm_iv is None:
            return None, why

        today = _ist_date(ctx.now).isoformat()
        p, n = _blended_percentile(ctx.symbol, atm_iv, today)
        IVStore(ctx.symbol).upsert(today, "iv", round(atm_iv, 4))

        anchors = IVL_SCORE_BUY if ctx.direction == BUY else IVL_SCORE_SELL
        score = pw_linear(p, anchors)

        ctx.meta["atm_iv"] = atm_iv
        ctx.meta["ivp"] = p
        ctx.meta["atm_strike"] = atm_strike

        boot = ", bootstrap" if n < MIN_HIST else ""
        detail = f"ATM IV {atm_iv*100:.1f}%, pctl {p:.0f} ({n}d hist{boot})"
        return score, detail


# ═════════════════════════════════════════════════════════════════════════
# 3. IV Skew (put-call skew, 25-delta proxy)
# ═════════════════════════════════════════════════════════════════════════
OTM_MONEYNESS = 0.05
SKEW_SCORE_BUY = [(-0.15, 80), (-0.08, 88), (0.0, 82), (0.05, 72),
                  (0.10, 55), (0.18, 35), (0.30, 18)]
SKEW_SCORE_SELL = [(-0.15, 30), (-0.08, 45), (0.0, 62), (0.05, 75),
                   (0.10, 85), (0.18, 78), (0.30, 60)]
SKEW_ADJ_COEF = 250.0
SKEW_ADJ_CLAMP_BUY = (-15.0, 8.0)
SKEW_ADJ_CLAMP_SELL = (-8.0, 12.0)
SKEW_BASELINE_DAYS = 5
SKEW_BASELINE_MIN = 3


def _liquid(leg) -> bool:
    return (leg is not None and leg.iv is not None and leg.iv > 0
            and leg.ltp is not None and leg.ltp > 0
            and ((leg.oi or 0) > 0 or (leg.volume or 0) > 0))


class IVSkewAgent(QuantAgent):
    key = "iv_skew"
    name = "IV Skew"
    description = ("5%-OTM put-minus-call IV normalized by ATM IV "
                   "(25-delta proxy) with a 5-day steepening adjustment.")
    default_weight_buy = 0.8
    default_weight_sell = 0.8

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None:
            return None, "option chain unavailable"

        atm_iv = ctx.meta.get("atm_iv")
        atm_strike = ctx.meta.get("atm_strike")
        if atm_iv is None or atm_strike is None:
            atm_iv, atm_strike, why = _compute_atm_iv(chain, ctx.now)
            if atm_iv is None:
                return None, f"no ATM IV ({why})"

        rows = _sorted_rows(chain)
        spot = chain.spot
        atm_idx = min(range(len(rows)),
                      key=lambda i: abs(rows[i].strike - atm_strike))

        # primary: 5%-OTM liquid pair
        put_row = call_row = None
        put_cands = [r for r in rows if r.strike < atm_strike and _liquid(r.pe)]
        if put_cands:
            put_row = min(put_cands,
                          key=lambda r: abs(r.strike - spot * (1 - OTM_MONEYNESS)))
        call_cands = [r for r in rows if r.strike > atm_strike and _liquid(r.ce)]
        if call_cands:
            call_row = min(call_cands,
                           key=lambda r: abs(r.strike - spot * (1 + OTM_MONEYNESS)))

        atm_only = False
        if put_row is None or call_row is None:
            put_row = call_row = None
            for off in (2, 1):          # symmetric ladder ±2 then ±1
                lo, hi = atm_idx - off, atm_idx + off
                if 0 <= lo and hi < len(rows) \
                        and _liquid(rows[lo].pe) and _liquid(rows[hi].ce):
                    put_row, call_row = rows[lo], rows[hi]
                    break
            if put_row is None:         # last resort: ATM row's own PE/CE
                r0 = rows[atm_idx]
                if (r0.pe is not None and r0.pe.iv is not None
                        and r0.ce is not None and r0.ce.iv is not None):
                    put_row = call_row = r0
                    atm_only = True
        if put_row is None or call_row is None:
            return None, "chain too illiquid for skew"

        put_iv, call_iv = float(put_row.pe.iv), float(call_row.ce.iv)
        r = (put_iv - call_iv) / atm_iv

        anchors = SKEW_SCORE_BUY if ctx.direction == BUY else SKEW_SCORE_SELL
        base = pw_linear(r, anchors)

        # steepening vs own 5-day median
        today = _ist_date(ctx.now).isoformat()
        store = IVStore(ctx.symbol)
        d = None
        adj = 0.0
        try:
            hist = store.history("skew", today, SKEW_BASELINE_DAYS)
            if len(hist) >= SKEW_BASELINE_MIN:
                d = r - statistics.median(hist)
                if ctx.direction == BUY:
                    adj = _clamp(-SKEW_ADJ_COEF * d, *SKEW_ADJ_CLAMP_BUY)
                else:
                    adj = _clamp(SKEW_ADJ_COEF * d, *SKEW_ADJ_CLAMP_SELL)
        except Exception as e:
            log.warning("skew baseline failed: %s", e)
        store.upsert(today, "skew", round(r, 4))

        score = _clamp(base + adj, 0.0, 100.0)
        detail = (f"rel skew {r:+.3f} (put {put_iv*100:.1f}% @ "
                  f"{put_row.strike:g} / call {call_iv*100:.1f}% @ "
                  f"{call_row.strike:g})")
        if atm_only:
            detail += " [ATM skew only]"
        if d is not None:
            detail += f", 5d chg {d:+.3f}"
        return score, detail


# ═════════════════════════════════════════════════════════════════════════
# 4. Vega Environment (vol-crush risk flag; vomma/veta unscored)
# ═════════════════════════════════════════════════════════════════════════
HIGH_VEGA_T = 12.0 / 365.0
HIGH_IVP = 70.0
LOW_IVP = 30.0
VEGA_SCORE_CRUSH = 30.0
VEGA_SCORE_CHEAP = 65.0
VEGA_SCORE_NEUTRAL = 50.0


class VegaEnvAgent(QuantAgent):
    """Vol-crush risk flag for OPTION execution only.

    HONEST ASSESSMENT: the trade signal executes on the CASH stock, so
    vega/vomma/veta do not touch the position's P&L at all — they only
    matter if the user expresses the signal via options. Vomma
    (vega*d1*d2/sigma) and veta are computable point-in-time from BS, but
    scoring them needs a vol-of-vol history or a multi-expiry surface;
    Shoonya gives one monthly expiry, no IV history beyond what iv_store
    accumulates, and no intraday IV ticks — any 'vomma score' would be
    numerology. Minimal useful version: flag rich-IV/high-vega regimes
    where long-option execution eats vol crush. Score deliberately
    compressed around neutral (30/50/65) so it cannot swing the tree.
    FUTURE: with 120+ stored IV days per symbol, stdev of daily IV changes
    becomes a real vol-of-vol input and vomma exposure can be scored.
    """

    key = "vega_env"
    name = "Vega Environment"
    description = ("Vol-crush risk flag for option execution; vomma/veta "
                   "not scored (no vol-of-vol data).")
    default_weight_buy = 0.3
    default_weight_sell = 0.3
    default_enabled = True      # ships as a live risk-flag (thin version)

    def compute(self, ctx):
        chain = ctx.chain
        if chain is None:
            return None, "option chain unavailable"
        t = years_to_expiry(chain.expiry_epoch, ctx.now)

        atm_iv = ctx.meta.get("atm_iv")
        ivp = ctx.meta.get("ivp")
        atm_strike = ctx.meta.get("atm_strike")
        if atm_iv is None or atm_strike is None:
            atm_iv, atm_strike, why = _compute_atm_iv(chain, ctx.now)
            if atm_iv is None:
                return None, f"no ATM IV ({why})"
        if ivp is None:
            today = _ist_date(ctx.now).isoformat()
            ivp, _n = _blended_percentile(ctx.symbol, atm_iv, today)

        # implied move to expiry (detail only)
        straddle_pct = None
        try:
            rows = _sorted_rows(chain)
            r0 = min(rows, key=lambda r: abs(r.strike - atm_strike))
            if (r0.ce is not None and r0.ce.ltp and r0.pe is not None
                    and r0.pe.ltp and chain.spot > 0):
                straddle_pct = (r0.ce.ltp + r0.pe.ltp) / chain.spot * 100.0
        except Exception:
            pass
        try:
            vega_1pct = bs_vega(chain.spot, atm_strike, t, atm_iv) * 0.01
        except Exception:
            vega_1pct = 0.0

        high_vega_env = t >= HIGH_VEGA_T
        if high_vega_env and ivp >= HIGH_IVP:
            score, note = VEGA_SCORE_CRUSH, \
                "rich IV + high vega: long-option execution eats vol crush"
        elif high_vega_env and ivp <= LOW_IVP:
            score, note = VEGA_SCORE_CHEAP, \
                "cheap vega: long-option execution favourable"
        else:
            score, note = VEGA_SCORE_NEUTRAL, "neutral"

        strad = f"{straddle_pct:.1f}% move" if straddle_pct is not None else "n/a"
        detail = (f"vega env: T={t*365:.0f}d, IVpctl {ivp:.0f}, straddle "
                  f"{strad}, vega {vega_1pct:.2f}/pt — {note}; vomma/veta "
                  f"not scored (no vol-of-vol data)")
        return score, detail


# ═════════════════════════════════════════════════════════════════════════
# 5. Event Proximity (manual calendar + auto monthly expiry Tuesdays)
# ═════════════════════════════════════════════════════════════════════════
N_BEFORE = 3
BASE_PENALTY = {"high": 60.0, "medium": 35.0, "low": 15.0}
PROXIMITY = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.4}
DAY_AFTER = 0.5
MARKET_SCOPE_FACTOR = 0.8


def _last_tuesday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() != 1:             # Tuesday
        d -= dt.timedelta(days=1)
    return d


def _load_events() -> list[dict]:
    """events.json at repo root: array of {date, type, scope, severity}.
    Also accepts the {'events': [...]} wrapper. Missing/corrupt -> []."""
    try:
        with open(EVENTS_FILE, "r") as f:
            raw = json.load(f)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("events")
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        try:
            d = dt.date.fromisoformat(str(e.get("date", "")))
        except Exception:
            continue
        sev = str(e.get("severity", "")).lower()
        if sev not in BASE_PENALTY:
            continue
        scope = str(e.get("scope", "MARKET"))
        name = str(e.get("type") or e.get("name") or "event")
        out.append({"date": d, "type": name, "scope": scope, "severity": sev})
    return out


class EventAgent(QuantAgent):
    key = "event"
    name = "Event Proximity"
    description = ("Proximity-decayed penalty for calendar events "
                   "(events.json + auto monthly F&O expiry Tuesdays); "
                   "worst single event within 3 days wins.")
    default_weight_buy = 1.2
    default_weight_sell = 1.2

    def compute(self, ctx):
        today = _ist_date(ctx.now)
        events = _load_events()         # missing file -> [] -> baseline 100

        # auto-add monthly F&O expiry Tuesdays (current + next month)
        y, m = today.year, today.month
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        for ey, em in ((y, m), (ny, nm)):
            events.append({"date": _last_tuesday(ey, em),
                           "type": "monthly F&O expiry",
                           "scope": "MARKET", "severity": "low"})

        worst = None                    # (penalty, event, d)
        for e in events:
            scope = e["scope"]
            is_market = scope.upper() == "MARKET"
            if not (is_market or scope.upper() == ctx.symbol.upper()):
                continue
            d = (e["date"] - today).days
            if 0 <= d <= N_BEFORE:
                prox = PROXIMITY[d]
            elif (d == -1 and not is_market
                    and e["severity"] in ("high", "medium")):
                prox = DAY_AFTER
            else:
                continue
            pen = (BASE_PENALTY[e["severity"]] * prox
                   * (MARKET_SCOPE_FACTOR if is_market else 1.0))
            if worst is None or pen > worst[0]:
                worst = (pen, e, d)

        if worst is None:
            return 100.0, f"no events within {N_BEFORE}d"
        pen, e, d = worst
        score = _clamp(100.0 - pen, 0.0, 100.0)
        when = f"in {d}d" if d >= 0 else "yesterday"
        detail = (f"{e['type']} ({e['scope']}, {e['severity']}) "
                  f"{when} -> -{pen:.0f}")
        return score, detail


# ═════════════════════════════════════════════════════════════════════════
# family branch
# ═════════════════════════════════════════════════════════════════════════
class VolatilityBranch(QuantAgent):
    key = "volatility"
    name = "Volatility"
    description = "VIX regime, IV level/skew, vega env, events"


def build() -> QuantAgent:
    return VolatilityBranch(children=[
        IndiaVixAgent(),
        IVLevelAgent(),
        IVSkewAgent(),
        VegaEnvAgent(),
        EventAgent(),
    ])


# ── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import numpy as np
    import pandas as pd

    from quant.context import ChainSnapshot, OptionLeg, StrikeRow

    def make_df(now: float, n: int = 250) -> pd.DataFrame:
        idx = pd.date_range(end=pd.Timestamp.fromtimestamp(now),
                            periods=n, freq="5min")
        rng = np.random.default_rng(7)
        close = 1000 + np.cumsum(rng.normal(0, 2.0, n))
        high = close + rng.uniform(0.5, 3.0, n)
        low = close - rng.uniform(0.5, 3.0, n)
        openp = close + rng.normal(0, 1.0, n)
        vol = rng.integers(1000, 9000, n).astype(float)
        oi = np.full(n, 5e5)
        return pd.DataFrame({"open": openp, "high": high, "low": low,
                             "close": close, "volume": vol, "oi": oi},
                            index=idx)

    def make_chain(spot: float, now: float) -> ChainSnapshot:
        strikes = []
        for k in range(900, 1101, 25):
            dist = abs(k - spot) / spot
            ce = OptionLeg("CE", "1", ltp=max(spot - k, 0) + 12.0,
                           oi=5000, volume=300,
                           iv=0.30 + 0.20 * dist + (0.00 if k >= spot else 0.01))
            pe = OptionLeg("PE", "2", ltp=max(k - spot, 0) + 13.0,
                           oi=6000, volume=250,
                           iv=0.32 + 0.25 * dist)
            strikes.append(StrikeRow(strike=float(k), ce=ce, pe=pe))
        return ChainSnapshot(symbol="TESTSYM", expiry_epoch=now + 20 * 86400,
                             lot=500, spot=spot, strikes=strikes)

    class Ctx:
        def __init__(self, direction, df, chain, vix, now):
            self.symbol = "TESTSYM"
            self.direction = direction
            self.df = df
            self.spot = float(df["close"].iloc[-1])
            self.now = now
            self.meta = {}
            self.chain = chain
            self.futures = None
            self.vix = vix
            self.daily = None
            self.cash_quote = None
            self.atr14 = 5.0

    class Cfg:
        def enabled(self, key, default=True):
            return default

        def threshold(self, key):
            return 50

        def weight(self, key, symbol, direction, db, ds):
            return db

    now = time.time()
    df = make_df(now)
    spot = float(df["close"].iloc[-1])
    agent = build()
    print("tree:", [c.key for c in agent.walk()])

    scenarios = [
        ("full BUY", Ctx("BUY", df, make_chain(spot, now), 14.5, now)),
        ("full SELL", Ctx("SELL", df, make_chain(spot, now), 14.5, now)),
        ("no chain/vix BUY", Ctx("BUY", df, None, None, now)),
        ("no chain/vix SELL", Ctx("SELL", df, None, None, now)),
    ]
    failures = 0
    for label, ctx in scenarios:
        res = agent.evaluate(ctx, Cfg())
        print(f"\n[{label}] family={res.score} ({res.detail})")
        for k in res.children:
            print(f"  {k.key:10s} score={k.score} avail={k.available} "
                  f":: {k.detail}")
            if k.detail.startswith("error:"):
                failures += 1
    # full-data runs: every leaf must have a score
    for label, ctx in scenarios[:2]:
        res = agent.evaluate(ctx, Cfg())
        assert all(k.score is not None for k in res.children), (label, res)
    # missing-data runs: event must still score, chain/vix leaves skip clean
    for label, ctx in scenarios[2:]:
        res = agent.evaluate(ctx, Cfg())
        ev = [k for k in res.children if k.key == "event"][0]
        assert ev.score is not None, (label, ev)
        for k in res.children:
            if k.key != "event":
                assert k.score is None, (label, k.key, k.detail)
    assert failures == 0, f"{failures} leaf exceptions"
    print("\nself-test OK")

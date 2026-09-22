"""Order-Block Reversal detector (Smart-Money Concepts) — the engine behind the
24×7 OB-Reversal scanner.

Pattern (bullish; bearish is the mirror), per the client's spec:

  1) Liquidity sweep   — price dips below a recent swing low (traps shorts / hits
                         stops), then reverses.
  2) Bullish impulse   — strong consecutive green candles (displacement) up.
  3) Break of Structure— that impulse CLOSES above a prior swing high, on volume
                         ABOVE the average (the breakout).
  4) Order-Block zone  — the LAST bearish (down) candle right before the impulse;
                         the zone is that candle's full range (low→high wick).
  5) Retracement       — price pulls back DOWN into the OB zone, and the pullback
                         volume DECREASES (stays below the breakout volume).
  6) Trigger (pop out) — price taps the zone, then a candle CLOSES back ABOVE it.
                         That candle's FULL range (low→high, wicks included) becomes
                         the "trigger box".
  7) Entry (BUY)       — a LATER candle CLOSES beyond the trigger box (above it for
                         a buy). Only then is it a confirmed signal — this filters
                         the trap where price pops out then sinks deeper into the OB.
                         Stop below the zone; target the breakout extreme.

Everything is deterministic and uses ONLY data up to the evaluated bar (swings
are confirmed with `pivot_k` bars on each side, so there is no look-ahead).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class OBParams:
    min_bars: int = 40          # need enough history to find structure
    vol_ma: int = 20            # average-volume window
    vol_mult: float = 2.0       # breakout candle volume must be ≥ this × average (a real spike)
    pivot_k: int = 3            # (legacy) raw fractal window — kept for fallback only
    zz_mult: float = 3.0        # MAJOR-swing filter: a structural pivot needs a leg of
                                # ≥ this × ATR (drops minor HH/HL/LL noise — we break/anchor
                                # only the big reversal zones, per the client's structure)
    atr_window: int = 14
    min_bos_distance: int = 5   # the broken swing high/low must be ≥ this many bars
                                # BEFORE the breakout candle (real structural gap,
                                # not just the adjacent candle's high)
    max_impulse_span: int = 24  # the move's ORIGIN (base) can be up to this many bars
                                # before the BOS — so the OB is the start of the trend
    max_impulse_lookback: int = 6   # how far back to look for the origin's down/up candle
    peak_follow: int = 4        # bars after BOS to include when finding the breakout extreme
    min_impulse_pct: float = 1.5    # the move from the OB to the breakout extreme must be
                                # ≥ this %% — guarantees real DISTANCE (OB sits well below
                                # the high), so the entry isn't jammed against the target
    scan_window: int = 70       # only consider a BOS within the last N bars
    max_watch_age: int = 15     # drop a 'watch' if price hasn't pulled back to the OB
                                # within this many bars of the breakout peak (stale — the
                                # retracement never came, so it's not actionable)
    max_armed_age: int = 6      # drop an 'armed' setup (popped out of the OB, trigger box
                                # set) if no later candle CLOSES beyond the box within this
                                # many bars — the breakout failed, not actionable
    retrace_avg_factor: float = 1.1  # retrace AVG volume must be ≤ this × the normal average
                                     # (volume should dry up on the pullback, not re-spike)
    stop_buffer_frac: float = 0.1    # SL sits this fraction of the OB range beyond it
    min_rr: float = 1.5         # drop setups whose reward:risk (entry→target vs entry→stop)
                                # is below this — keeps the entry well away from the target
    # FOLLOW-THROUGH quality — the fixes for "signal fires, then price reverses
    # midway and never reaches target". A real continuation breaks decisively and
    # is entered with room still left to run.
    bos_body_frac: float = 0.45     # BOS candle must CLOSE within this fraction of its own
                                    # range (a decisive break body, not a marginal/exhaustion poke)
    confirm_body_frac: float = 0.45 # the confirming candle must close strong in its range too
    confirm_margin_frac: float = 0.08  # …and clear the trigger box by ≥ this × the box height
                                       # (a real close beyond the range, not a 1-tick poke)
    max_entry_progress: float = 0.55   # entry must sit within this fraction of the OB→extreme
                                       # move — past it we're chasing (no room → reverses midway)
    target_frac: float = 0.85       # aim for this fraction of the entry→breakout-extreme move;
                                    # the exact prior high is often not reached (price stalls
                                    # just under it), so a slightly closer target is reachable
    fresh_bars: int = 3         # entry within this many bars = a FRESH signal


_RANK = {"signal": 4, "armed": 3, "retracing": 2, "watch": 1}


def _swings(arr: np.ndarray, k: int, kind: str) -> list[int]:
    """Indices of swing highs ('high') or lows ('low') — confirmed pivots (an
    extreme of the ±k window). The last k bars can't be confirmed yet."""
    out = []
    n = len(arr)
    for i in range(k, n - k):
        win = arr[i - k:i + k + 1]
        if kind == "high" and arr[i] == win.max() and arr[i] > arr[i - 1]:
            out.append(i)
        elif kind == "low" and arr[i] == win.min() and arr[i] < arr[i - 1]:
            out.append(i)
    return out


def _detect_dir(o, h, l, c, v, vavg, p: OBParams, bullish: bool, pivots: list[dict]) -> Optional[dict]:
    n = len(c)
    # MAJOR structure pivots only (ATR ZigZag) — we break/anchor the big reversal
    # highs & lows, not the minor HH/HL/LL noise that lives inside a leg.
    maj_h = [(pv["idx"], pv["price"]) for pv in pivots if pv["kind"] == "H"]
    maj_l = [(pv["idx"], pv["price"]) for pv in pivots if pv["kind"] == "L"]
    if not maj_h or not maj_l:
        return None
    gap = p.min_bos_distance

    # 1) Break of Structure — the candle must CLOSE beyond a MAJOR swing sitting
    #    ≥ gap bars back, on a ≥ vol_mult× volume spike. A close (not just a wick
    #    poke) is required so the break is real, per the client's spec.
    bos_idx = bos_level = None
    lo_b = max(p.vol_ma, n - p.scan_window)
    for b in range(n - 1, lo_b - 1, -1):
        if not (vavg[b] > 0 and v[b] >= p.vol_mult * vavg[b]):
            continue
        rng_b = h[b] - l[b]
        if bullish:
            prior = [pr for (idx, pr) in maj_h if idx <= b - gap]
            strong = rng_b > 0 and (c[b] - l[b]) / rng_b >= p.bos_body_frac
            if prior and c[b] > prior[-1] and c[b] > o[b] and strong:   # decisive CLOSE above a MAJOR swing high
                bos_idx, bos_level = b, prior[-1]; break
        else:
            prior = [pr for (idx, pr) in maj_l if idx <= b - gap]
            strong = rng_b > 0 and (h[b] - c[b]) / rng_b >= p.bos_body_frac
            if prior and c[b] < prior[-1] and c[b] < o[b] and strong:   # decisive CLOSE below a MAJOR swing low
                bos_idx, bos_level = b, prior[-1]; break
    if bos_idx is None:
        return None
    breakout_vol = float(v[bos_idx])

    # 2) the IMPULSE leg = from the BASE (the extreme low/high in the window before
    #    the BOS) up to the PEAK (the extreme made by the breakout). The OB zone is
    #    the last counter-trend candle AT THE BASE — i.e. the START of the trend,
    #    deep below the breakout high (not jammed against it).
    span_start = max(0, bos_idx - p.max_impulse_span)
    peak_end = min(n, bos_idx + 1 + p.peak_follow)
    if bullish:
        lows_before = [idx for (idx, pr) in maj_l if idx < bos_idx]
        origin_idx = lows_before[-1] if lows_before else (
            span_start + int(np.argmin(l[span_start:bos_idx + 1])))   # the MAJOR low = base
        peak_idx = origin_idx + int(np.argmax(h[origin_idx:peak_end]))
        impulse_high, impulse_low = float(h[peak_idx]), float(l[origin_idx])
        ob_idx, g = origin_idx, 0
        while ob_idx > 0 and not (c[ob_idx] < o[ob_idx]) and g < p.max_impulse_lookback:
            ob_idx -= 1; g += 1                      # last DOWN candle at the base
        ob_top, ob_bottom = float(h[ob_idx]), float(l[ob_idx])
        if ob_top <= ob_bottom or ob_top <= 0:
            return None
        move_pct = (impulse_high - ob_top) / ob_top * 100.0
    else:
        highs_before = [idx for (idx, pr) in maj_h if idx < bos_idx]
        origin_idx = highs_before[-1] if highs_before else (
            span_start + int(np.argmax(h[span_start:bos_idx + 1])))   # the MAJOR high = base
        peak_idx = origin_idx + int(np.argmin(l[origin_idx:peak_end]))
        impulse_low, impulse_high = float(l[peak_idx]), float(h[origin_idx])
        ob_idx, g = origin_idx, 0
        while ob_idx > 0 and not (c[ob_idx] > o[ob_idx]) and g < p.max_impulse_lookback:
            ob_idx -= 1; g += 1                      # last UP candle at the base
        ob_top, ob_bottom = float(h[ob_idx]), float(l[ob_idx])
        if ob_top <= ob_bottom or ob_bottom <= 0:
            return None
        move_pct = (ob_bottom - impulse_low) / ob_bottom * 100.0

    # require a SIGNIFICANT displacement — this is what gives the OB real distance
    # from the breakout extreme (the bug before: tiny moves → entry at the high).
    if move_pct < p.min_impulse_pct:
        return None

    # 3) liquidity sweep at the base (swept a prior swing extreme then reclaimed)
    sweep_level = None
    base_end = max(ob_idx, origin_idx) + 1
    if bullish:
        cand = [pr for (idx, pr) in maj_l if idx < ob_idx]
        seg_lo_base = float(l[max(0, ob_idx - 2):base_end].min())
        if cand and seg_lo_base <= cand[-1]:
            sweep_level = seg_lo_base
    else:
        cand = [pr for (idx, pr) in maj_h if idx < ob_idx]
        seg_hi_base = float(h[max(0, ob_idx - 2):base_end].max())
        if cand and seg_hi_base >= cand[-1]:
            sweep_level = seg_hi_base

    # 4) RETRACE back to the OB AFTER the peak, then a TWO-STEP confirmed entry.
    #    Only bars after the peak count — so an entry means price came back DOWN to
    #    the base zone and bounced, i.e. the entry is at the LOW, not the high.
    #      (a) TRIGGER — price taps the OB and a candle CLOSES back out of it (the
    #          "pop out"); that candle's full range (low→high, wicks) is the box.
    #      (b) ENTRY   — a LATER candle CLOSES beyond the box (above for a buy, below
    #          for a sell). Only then is it a signal. Filters the common trap where
    #          price pops out, then sinks deeper into the OB before going anywhere.
    status = "watch"
    tap_idx = trigger_idx = entry_idx = None
    trigger_top = trigger_bot = None
    entry = None
    retrace_vol_ok = True
    retrace_avg_vol = None
    norm_vol = float(vavg[bos_idx])
    for i in range(peak_idx + 1, n):
        if (l[i] <= ob_top) if bullish else (h[i] >= ob_bottom):
            tap_idx = i; break
    if tap_idx is not None:
        status = "retracing"
        seg = v[peak_idx + 1:tap_idx + 1]
        if len(seg):
            retrace_avg_vol = float(seg.mean())
            # volume must DRY UP on the pullback: no candle re-spikes near the
            # breakout, AND the average pullback volume is at/below normal.
            no_respike = float(seg.max()) < breakout_vol
            below_avg = retrace_avg_vol <= norm_vol * p.retrace_avg_factor
            retrace_vol_ok = bool(no_respike and below_avg)
        # (a) TRIGGER — first candle to CLOSE back out of the OB after the tap.
        if retrace_vol_ok:
            for i in range(tap_idx, n):
                popped = (c[i] > ob_top) if bullish else (c[i] < ob_bottom)
                if popped:
                    trigger_idx = i
                    trigger_top, trigger_bot = float(h[i]), float(l[i])
                    status = "armed"
                    break
        # (b) ENTRY — a LATER candle must CLOSE DECISIVELY beyond the trigger box to
        #     confirm: clear it by a margin AND close strong in its own range. A
        #     marginal poke that stalls is exactly the "fires then reverses" trap.
        if trigger_idx is not None:
            box_h = max(trigger_top - trigger_bot, 1e-9)
            margin = box_h * p.confirm_margin_frac
            end = min(n, trigger_idx + 1 + p.max_armed_age)   # confirm within the armed window
            for j in range(trigger_idx + 1, end):
                rng = max(h[j] - l[j], 1e-9)
                if bullish:
                    if c[j] < ob_bottom:            # reclaim failed → setup dead
                        return None
                    body_ok = (c[j] - l[j]) / rng >= p.confirm_body_frac
                    if c[j] > trigger_top + margin and body_ok:   # decisive close above → BUY
                        entry_idx, entry = j, float(trigger_top); status = "signal"; break
                else:
                    if c[j] > ob_top:               # reclaim failed → setup dead
                        return None
                    body_ok = (h[j] - c[j]) / rng >= p.confirm_body_frac
                    if c[j] < trigger_bot - margin and body_ok:   # decisive close below → SELL
                        entry_idx, entry = j, float(trigger_bot); status = "signal"; break
            # drop a STALE 'armed' — the box never confirmed within max_armed_age bars.
            if status == "armed" and (n - 1 - trigger_idx) > p.max_armed_age:
                return None

    # drop STALE watches — price never pulled back to the OB within max_watch_age
    # bars of the breakout peak (the retracement isn't coming → not actionable).
    if status == "watch" and (n - 1 - peak_idx) > p.max_watch_age:
        return None

    # 5) levels — ENTRY at the OB (base), TARGET = the breakout extreme (far away),
    #    STOP just beyond the OB. → big, sane R:R.
    buf = (ob_top - ob_bottom) * p.stop_buffer_frac
    if bullish:
        stop = round(ob_bottom - buf, 2)
        ref_entry = entry if entry is not None else (trigger_top if trigger_top is not None else ob_top)
        target_full = impulse_high
        target = round(ref_entry + (target_full - ref_entry) * p.target_frac, 2)
        risk = max(ref_entry - stop, 1e-6)
        progress = (ref_entry - ob_top) / max(impulse_high - ob_top, 1e-9)
    else:
        stop = round(ob_top + buf, 2)
        ref_entry = entry if entry is not None else (trigger_bot if trigger_bot is not None else ob_bottom)
        target_full = impulse_low
        target = round(ref_entry - (ref_entry - target_full) * p.target_frac, 2)
        risk = max(stop - ref_entry, 1e-6)
        progress = (ob_bottom - ref_entry) / max(ob_bottom - impulse_low, 1e-9)
    ref_entry = round(ref_entry, 2)
    # CHASING guard — if the entry already sits past max_entry_progress of the
    # OB→extreme move, there's little room left to run; that's the classic setup
    # that "fires then reverses midway". Drop it.
    if progress > p.max_entry_progress:
        return None
    rr_implied = round(abs(target - ref_entry) / risk, 2)
    # quality gate: the entry must sit well away from the (reachable) target
    if rr_implied < p.min_rr:
        return None

    vx = round(breakout_vol / norm_vol, 1) if norm_vol else None
    rvx = round(retrace_avg_vol / norm_vol, 1) if (retrace_avg_vol and norm_vol) else None
    reasons = [f"breakout {vx}× vol spike" if vx else "breakout", f"impulse +{round(move_pct, 1)}%"]
    if sweep_level is not None:
        reasons.append("liquidity sweep")
    if status in ("retracing", "armed", "signal"):
        reasons.append((f"retrace vol {rvx}× avg" if rvx is not None else "retrace")
                       + (" ✓ dried up" if retrace_vol_ok else " ✗ not dried up"))
    if status == "armed":
        reasons.append("popped out of OB — awaiting decisive close beyond trigger range")
    if status == "signal":
        reasons.append(f"decisive close beyond range · entry {round(progress * 100)}% into move")

    return {
        "side": "LONG" if bullish else "SHORT",
        "status": status,
        "ob_top": round(ob_top, 2), "ob_bottom": round(ob_bottom, 2),
        "bos_level": round(float(bos_level), 2),
        "sweep_level": round(sweep_level, 2) if sweep_level is not None else None,
        "move_pct": round(move_pct, 2),
        "breakout_vol": round(breakout_vol, 0), "avg_vol": round(norm_vol, 0),
        "vol_x": vx, "retrace_avg_vol": round(retrace_avg_vol, 0) if retrace_avg_vol else None,
        "retrace_vol_x": rvx,
        "entry": round(entry, 2) if entry is not None else None,
        "stop": stop, "target": target, "target_full": round(float(target_full), 2),
        "entry_progress": round(float(progress), 3), "rr": rr_implied,
        "ob_idx": int(ob_idx), "origin_idx": int(origin_idx),
        "peak_idx": int(peak_idx), "bos_idx": int(bos_idx),
        "tap_idx": int(tap_idx) if tap_idx is not None else None,
        "trigger_idx": int(trigger_idx) if trigger_idx is not None else None,
        "trigger_top": round(trigger_top, 2) if trigger_top is not None else None,
        "trigger_bot": round(trigger_bot, 2) if trigger_bot is not None else None,
        "entry_idx": int(entry_idx) if entry_idx is not None else None,
        "bars_since_entry": (n - 1 - entry_idx) if entry_idx is not None else None,
        "fresh": (entry_idx is not None and (n - 1 - entry_idx) <= p.fresh_bars),
        "retrace_vol_ok": bool(retrace_vol_ok),
        "reasons": reasons,
    }


def detect(df: pd.DataFrame, params: Optional[OBParams] = None) -> Optional[dict]:
    """Return the best OB-reversal setup on this frame (or None). Prefers a
    confirmed 'signal' over 'retracing'/'watch', then the most recent."""
    p = params or OBParams()
    if df is None or len(df) < p.min_bars:
        return None
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(df) < p.min_bars:
        return None
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    vavg = pd.Series(v).rolling(p.vol_ma, min_periods=max(5, p.vol_ma // 2)).mean().to_numpy()
    vavg = np.nan_to_num(vavg, nan=0.0)

    # MAJOR market structure (HH/HL/LH/LL) — the scanner breaks/anchors only these.
    from . import market_structure as ms
    struct = ms.analyze(df, atr_window=p.atr_window, zz_mult=p.zz_mult)
    pivots = struct["pivots"]

    out = []
    for bull in (True, False):
        try:
            s = _detect_dir(o, h, l, c, v, vavg, p, bull, pivots)
        except Exception:
            s = None
        if s:
            out.append(s)
    if not out:
        return None
    out.sort(key=lambda s: (_RANK.get(s["status"], 0), s["bos_idx"]), reverse=True)
    best = out[0]
    best["structure"] = pivots          # labelled major pivots (for the chart "indicator")
    best["trend"] = struct.get("trend")
    return best

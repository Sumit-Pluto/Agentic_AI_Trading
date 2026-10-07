"""I0–I8 intraday exit state machine.

Evaluated top-down each bar (I0/I1 also per tick); a higher rule overrides a
lower one; the trailing stop is ratchet-only. Priced off the UNDERLYING for
invalidation/trailing (spot is liquid) and the option PREMIUM for the P&L stop
(the leg is noisy). Parametrised by Position.side so it serves long CE/PE now
and short premium later. manage() mutates the position's trailing state
(max_prem, max_fav_spot, stop, breakeven_done, partial_done, age_bars) and
returns an ExitDecision.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..agents._ta import adx, atr, last
from ..options.models import IST


@dataclass
class ExitMarket:
    now: dt.datetime
    spot: float                 # underlying
    leg_bid: float              # current option bid (exit price for a long)
    leg_ask: float
    leg_mid: float
    bars: object = None         # underlying intraday bars (for ATR/ADX/VWAP)
    vwap: float | None = None
    is_square_off: bool = False
    bars_elapsed: float = 1.0   # bars held since the last manage() call; the
                                # loop passes wall-clock elapsed bars so age is
                                # in bars at any engine cadence (direct callers
                                # age one bar per call, the historical default)


@dataclass
class ExitDecision:
    action: str                 # "HOLD" | "EXIT" | "PARTIAL"
    reason: str = ""
    qty_frac: float = 1.0       # fraction to exit for PARTIAL
    exit_px: float | None = None  # marketable exit price (bid for a long)


def _atr_pts(bars, spot) -> float:
    if bars is None or len(bars) < 3:
        return max(spot * 0.002, 1.0)
    a = last(atr(bars))
    return a if a == a and a > 0 else max(spot * 0.002, 1.0)


def manage(pos, mkt: ExitMarket, cfg: dict) -> ExitDecision:
    cfg = cfg or {}
    right = str(pos.right).upper()
    long = pos.is_long
    # Direction of the UNDERLYING bet (drives the stop/trailing/VWAP logic):
    # a long call / short put / long future is bullish; the mirrors bearish.
    # (Inferring it from CE-vs-PE alone treats every FUT leg as bearish and
    # stop-hunts long futures on the first tick after entry.)
    bullish = (long if right == "FUT"
               else (right.startswith("C") == long))
    prem = mkt.leg_mid if mkt.leg_mid > 0 else mkt.leg_bid
    exit_px = mkt.leg_bid if long else mkt.leg_ask     # marketable exit for the side
    try:
        step_age = max(float(mkt.bars_elapsed), 0.0)
    except (TypeError, ValueError):
        step_age = 1.0
    pos.age_bars += step_age

    # premium-based R multiple
    rps = pos.risk_per_share or 1.0
    r_mult = ((prem - pos.entry_px) if long else (pos.entry_px - prem)) / rps

    # ── MIN-HOLD GUARD ─────────────────────────────────────────────────────
    # A fresh fill must not die to quote noise (I2), a stall read (I5) or a
    # VWAP wobble (I6) seconds after entry — those exits wait until the trade
    # has breathed for `min_hold_seconds`. Hard protection (I0 square-off,
    # I1 stops) and profit-taking (I3) stay immediate. An unusable clock
    # disables the guard (historical behaviour) rather than blocking exits.
    try:
        hold_s = (mkt.now - pos.entry_ts).total_seconds()
    except Exception:
        hold_s = float("inf")
    try:
        min_hold = float(cfg.get("min_hold_seconds", 90.0) or 0.0)
    except (TypeError, ValueError):
        min_hold = 0.0
    guarded = min_hold > 0 and hold_s < min_hold
    deferred: list[str] = []

    # ── I0 SESSION SQUARE-OFF (absolute override) ────────────────────────────
    if mkt.is_square_off:
        return ExitDecision("EXIT", "I0 square-off", exit_px=exit_px)

    # ── I1 HARD STOP ─────────────────────────────────────────────────────────
    #   (a) underlying invalidation (primary): spot through the structural stop
    if pos.stop and pos.stop > 0:
        if bullish and mkt.spot <= pos.stop:
            return ExitDecision("EXIT", f"I1 underlying stop {pos.stop:.0f}", exit_px=exit_px)
        if (not bullish) and mkt.spot >= pos.stop:
            return ExitDecision("EXIT", f"I1 underlying stop {pos.stop:.0f}", exit_px=exit_px)
    #   (b) premium stop (secondary): longs stop when the premium decays,
    #       shorts (adopted/manual — the engine never shorts options itself)
    #       stop when it rallies. Futures are excluded (no decaying premium;
    #       their I1a underlying stop covers them).
    prem_loss_pct = float(cfg.get("max_prem_loss_pct", 40.0)) / 100.0
    if long:
        floor_ = pos.entry_px * (1.0 - prem_loss_pct)
        if prem <= floor_:
            return ExitDecision("EXIT", f"I1 premium stop {floor_:.2f}", exit_px=exit_px)
    elif right != "FUT":
        ceil_ = pos.entry_px * (1.0 + prem_loss_pct)
        if prem >= ceil_:
            return ExitDecision("EXIT", f"I1 premium stop {ceil_:.2f} (short)", exit_px=exit_px)

    # ── I2 SPREAD / LIQUIDITY GUARD ──────────────────────────────────────────
    if mkt.leg_bid <= 0 or mkt.leg_ask <= 0:
        if guarded:
            deferred.append("I2 one-sided book")
        else:
            return ExitDecision("EXIT", "I2 one-sided book", exit_px=max(mkt.leg_bid, 0.05))
    spread_pct = (mkt.leg_ask - mkt.leg_bid) / mkt.leg_mid * 100.0 if mkt.leg_mid > 0 else 999
    if spread_pct > float(cfg.get("max_spread_pct", 8.0)):
        if guarded:
            deferred.append(f"I2 spread {spread_pct:.0f}%")
        else:
            return ExitDecision("EXIT", f"I2 spread {spread_pct:.0f}%", exit_px=exit_px)

    # ── I3 TARGET / PARTIAL BOOK ─────────────────────────────────────────────
    t1, t2 = float(cfg.get("target_r_1", 1.0)), float(cfg.get("target_r_2", 2.0))
    if r_mult >= t2:
        return ExitDecision("EXIT", f"I3 target +{t2:g}R", exit_px=exit_px)
    if r_mult >= t1 and not pos.partial_done:
        pos.partial_done = True
        pos.breakeven_done = True
        pos.stop = pos.entry_spot                       # move underlying stop to breakeven
        frac = float(cfg.get("partial_pct_1", 50.0)) / 100.0
        return ExitDecision("PARTIAL", f"I3 book {frac*100:.0f}% at +{t1:g}R, stop->BE",
                            qty_frac=frac, exit_px=exit_px)

    # ── I4 TRAILING STOP (ratchet-only, arms after +1R) ─────────────────────
    if mkt.spot == mkt.spot:
        pos.max_fav_spot = (max(pos.max_fav_spot or mkt.spot, mkt.spot) if bullish
                            else min(pos.max_fav_spot or mkt.spot, mkt.spot))
    if pos.breakeven_done and mkt.bars is not None:
        a = _atr_pts(mkt.bars, mkt.spot)
        adx_ = last(adx(mkt.bars))
        mult = float(cfg.get("trail_atr_mult", 2.5)) * (1.2 if (adx_ == adx_ and adx_ >= 25) else 1.0)
        if bullish:
            trail = pos.max_fav_spot - mult * a
            pos.stop = max(pos.stop, trail)             # ratchet up only
        else:
            trail = pos.max_fav_spot + mult * a
            pos.stop = min(pos.stop, trail) if pos.stop else trail  # ratchet down only

    # ── I5 TIME / THETA STALL (one-shot) ─────────────────────────────────────
    if (not pos.stall_checked and pos.age_bars >= int(cfg.get("stall_bars", 12))
            and r_mult < 0.5):
        if guarded:
            deferred.append("I5 stall")
        else:
            pos.stall_checked = True
            return ExitDecision("EXIT", f"I5 stall {pos.age_bars} bars <0.5R", exit_px=exit_px)

    # ── I6 STRUCTURE FLIP (5m close back through VWAP against the position) ──
    vwap = mkt.vwap
    if vwap and vwap > 0:
        flip = ((bullish and mkt.spot < vwap and r_mult < t1)
                or ((not bullish) and mkt.spot > vwap and r_mult < t1))
        if flip:
            if guarded:
                deferred.append("I6 VWAP flip")
            else:
                side_txt = "below" if bullish else "above"
                return ExitDecision("EXIT", f"I6 back {side_txt} VWAP", exit_px=exit_px)

    # ── I7 IV-CRUSH / EVENT (optional; needs an iv-drop flag from the loop) ─
    # left to the loop to flag via cfg/session; no data here → skip.

    # ── I8 HOLD ──────────────────────────────────────────────────────────────
    if long:
        pos.max_prem = max(pos.max_prem or prem, prem)
    note = ""
    if deferred:
        left = max(0.0, min_hold - hold_s)
        note = f"; min-hold {left:.0f}s left, deferred {', '.join(deferred)}"
    return ExitDecision("HOLD", f"hold ({r_mult:+.1f}R, age {pos.age_bars:.1f}){note}")

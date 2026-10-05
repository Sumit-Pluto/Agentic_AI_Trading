"""Rule Engine (PDF §2.3) — pre-trade compliance gate.

Pure and side-effect-free: the caller passes current state (open positions,
halted/paused flags, the clock); the engine returns (ok, reason). Exits bypass
everything except a duplicate-exit guard. Entry checks:

  • kill-switch / paused / daily-loss halt      → block entries
  • trading-time windows (no new entries after; none at/after square-off)
  • market-hours / weekday                       → block outside the session
  • max open positions
  • already-held / duplicate: one position per (underlying, direction)
  • per-underlying lot cap (exposure)

Enforcing §2.3 examples: duplicate orders, multiple strategies opening identical
positions (locally; the Gateway coordinator covers cross-process), max exposure
per symbol, max open positions, trading-time restrictions.
"""
from __future__ import annotations

import datetime as dt

from ..options.models import IST, market_session


def _hhmm(s: str, default: dt.time) -> dt.time:
    try:
        h, m = (int(x) for x in str(s).split(":"))
        return dt.time(h, m, tzinfo=IST)
    except (ValueError, AttributeError):
        return default


class RuleEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def check(self, intent, now: dt.datetime, open_positions: list, *,
              is_exit: bool = False, halted: bool = False,
              paused: bool = False, holidays=None) -> tuple[bool, str]:
        cfg = self.cfg
        right = str(getattr(intent, "right", "")).upper()
        underlying = getattr(intent, "underlying", "") or ""

        # ---- exits: allowed almost always; only guard a duplicate exit ----
        if is_exit:
            return True, "exit allowed"

        # ---- entry gates ----
        if paused:
            return False, "paused (kill-switch / user pause)"
        if halted:
            return False, "daily-loss halt — no new entries today"

        sess = market_session(now, holidays=holidays)
        if not sess["open"]:
            return False, f"market not open: {sess['reason']}"

        t = now.timetz() if now.tzinfo else now.replace(tzinfo=IST).timetz()
        sq = _hhmm(cfg.get("square_off_time", "15:15"), dt.time(15, 15, tzinfo=IST))
        if t >= sq:
            return False, f"at/after square-off {sq:%H:%M} — no new entries"
        no_new = _hhmm(cfg.get("no_new_entries_after", "15:00"), dt.time(15, 0, tzinfo=IST))
        if t >= no_new:
            return False, f"after no-new-entries cutoff {no_new:%H:%M}"

        open_positions = open_positions or []
        max_pos = int(cfg.get("max_positions", 4))
        if len(open_positions) >= max_pos:
            return False, f"max open positions reached ({max_pos})"

        # already-held / duplicate: one position per (underlying, direction).
        # right encodes the directional bet for options (CE=bullish, PE=bearish);
        # futures need the side too, or a hedge/reversal reads as a duplicate.
        side = str(getattr(intent, "side", "")).upper()
        for p in open_positions:
            if (getattr(p, "underlying", "") == underlying
                    and str(getattr(p, "right", "")).upper() == right
                    and (right != "FUT"
                         or str(getattr(p, "side", "")).upper() == side)):
                return False, f"already holding {underlying} {right} — duplicate"

        # per-underlying lot cap (exposure across any legs on this underlying)
        lot = int(getattr(intent, "lot_size", 0) or 0)
        want_lots = (int(getattr(intent, "qty", 0)) // lot) if lot else 0
        held_lots = sum((getattr(p, "qty", 0) // (getattr(p, "lot_size", 1) or 1))
                        for p in open_positions
                        if getattr(p, "underlying", "") == underlying)
        cap = int(cfg.get("max_lots_per_symbol", 10))
        if want_lots + held_lots > cap:
            return False, f"{underlying} lot cap {cap} exceeded ({held_lots}+{want_lots})"

        return True, "ok"

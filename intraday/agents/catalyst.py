"""Family C — CATALYST (per candidate).

C1 gates a single name around its own scheduled event (earnings/board meeting/
result). The index-level macro gate is R6 (run in the regime pass). Symbol event
timing comes from cfg['symbol_events'] = {SYMBOL: {'minutes': m, 'label': ...}}
or inp.session['symbol_event_minutes'].
"""
from __future__ import annotations

from .base import AgentResult, agent


@agent("C1_event_gate", "C")
def c1(inp):
    """A scheduled name-specific event within the window → veto (stand aside; the
    move is a coin-flip and IV is rich). No event → neutral pass so the family
    isn't dropped from the composite."""
    mins = inp.session.get("symbol_event_minutes")
    label = inp.session.get("symbol_event_label", "")
    if mins is None:
        ev = (inp.cfg.get("symbol_events", {}) or {}).get(inp.symbol)
        if isinstance(ev, dict):
            mins, label = ev.get("minutes"), ev.get("label", "")
    if mins is not None and 0 <= mins <= inp.cfg.get("event_halt_minutes", 30):
        return AgentResult("", "", veto=f"{label or 'event'} for {inp.symbol} in {mins:.0f}m")
    return AgentResult("", "", score_buy=52.0, score_sell=52.0,
                       detail=(f"event {label} in {mins:.0f}m" if mins is not None
                               else "no scheduled event"))

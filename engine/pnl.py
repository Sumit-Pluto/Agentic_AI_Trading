"""P&L dashboard — pure, read-only aggregation over the paper-trading state.

build_dashboard(scanner)
    account tiles (LIVE paper positions only — sims never pollute the
    account numbers) + one unified trade list (live + sim, newest first).

trade_detail(scanner, trade_id)
    everything known about one trade: the row, its lifecycle (plan,
    stop_history, exits, levels, wall_ref, water marks, mfe/mae for sims),
    the FULL signal result recorded at entry (agent tree, score, vetoes)
    and a plain-language narrative derived strictly from recorded data.

No side effects, no network, no persistence. Every input is optional:
missing engines/books/hubs degrade to empty fields, never exceptions.
"""

from __future__ import annotations

from datetime import date, datetime

OPEN, PARTIAL, CLOSED = "OPEN", "PARTIAL", "CLOSED"
PAPER_TAIL = 2000          # paper-book rows searched for the entry audit


# ── tolerant primitives ──────────────────────────────────────────────────
def _f(v, default=None):
    """float() that never raises."""
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _n(v) -> str:
    """Compact human number: 804.5500 -> '804.55', 100.0 -> '100'."""
    x = _f(v)
    if x is None:
        return "n/a"
    return format(round(x, 4), "g")


def _live_positions(scanner) -> list[dict]:
    """All PaperPositions as plain dicts; [] when the engine is missing."""
    exits = getattr(scanner, "exits", None)
    if exits is None:
        return []
    try:
        summ = exits.summary()
        if isinstance(summ, dict):
            return [p for p in (summ.get("positions") or [])
                    if isinstance(p, dict)]
    except Exception:
        pass
    try:                                  # engine without summary()
        return [p.to_dict() for p in getattr(exits, "positions", {}).values()]
    except Exception:
        return []


def _last_close(scanner, symbol):
    """Last 5m close via the hub, guarded; None when unavailable."""
    hub = getattr(scanner, "hub", None)
    if hub is None or not symbol:
        return None
    try:
        df = hub.candles_5m(symbol)
        if df is None or getattr(df, "empty", True):
            return None
        return float(df["close"].iloc[-1])
    except Exception:
        return None


# ── unified trade rows ───────────────────────────────────────────────────
def _live_row(p: dict) -> dict:
    exits = [e for e in (p.get("exits") or []) if isinstance(e, dict)]
    last = exits[-1] if exits else {}
    return {"id": p.get("id"), "source": "paper",
            "symbol": p.get("symbol"), "direction": p.get("direction"),
            "entry_time": p.get("entry_time"),
            "entry": _f(p.get("entry_price")),
            "state": p.get("state"),
            "exit_time": last.get("time"),
            "exit_price": _f(last.get("price")),
            "exit_reason": last.get("reason"),
            "pnl_per_share": _f(p.get("realized_pnl_per_share"), 0.0),
            "qty_units": _f(p.get("qty_units"))}


def _sim_row(t: dict) -> dict:
    sym = str(t.get("symbol") or "?")
    et = str(t.get("entry_time") or "")
    return {"id": f"sim:{sym}:{et}", "source": "sim",
            "symbol": sym, "direction": t.get("direction"),
            "entry_time": et or None,
            "entry": _f(t.get("entry")),
            "state": CLOSED,               # sim walks always finish the day
            "exit_time": t.get("exit_time"),
            "exit_price": _f(t.get("exit")),
            "exit_reason": t.get("exit_reason"),
            "pnl_per_share": _f(t.get("pnl_per_share"), 0.0),
            "score": _f(t.get("score"))}


def _sim_trades(scanner) -> list[dict]:
    try:
        st = getattr(scanner, "sim_status", None) or {}
        return [t for t in (st.get("trades") or []) if isinstance(t, dict)]
    except Exception:
        return []


# ── account tiles (LIVE paper only) ──────────────────────────────────────
def _account(scanner, positions: list[dict]) -> dict:
    realized_total = realized_today = unreal = 0.0
    open_count = 0
    closed_pnls: list[float] = []
    today = date.today().isoformat()
    for p in positions:
        r = _f(p.get("realized_pnl_per_share"), 0.0)
        realized_total += r
        for e in p.get("exits") or []:
            if isinstance(e, dict) and str(e.get("time", ""))[:10] == today:
                realized_today += _f(e.get("pnl_per_share"), 0.0)
        state = p.get("state")
        if state in (OPEN, PARTIAL):
            open_count += 1
            px = _last_close(scanner, p.get("symbol"))
            entry = _f(p.get("entry_price"))
            qty = _f(p.get("qty_units"), 0.0)
            if px is not None and entry is not None and qty:
                sign = 1.0 if p.get("direction") == "BUY" else -1.0
                unreal += (px - entry) * sign * qty
        elif state == CLOSED:
            closed_pnls.append(r)
    wins = [x for x in closed_pnls if x > 0]
    losses = [x for x in closed_pnls if x < 0]
    n = len(closed_pnls)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"realized_total_ps": round(realized_total, 4),
            "realized_today_ps": round(realized_today, 4),
            "unrealized_ps": round(unreal, 4),
            "open_count": open_count,
            "trades_total": n,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
            "avg_win_ps": round(gross_win / len(wins), 4) if wins else None,
            "avg_loss_ps": (round(sum(losses) / len(losses), 4)
                            if losses else None),
            "profit_factor": (round(gross_win / gross_loss, 3)
                              if gross_loss > 1e-12 else None),
            "best_ps": round(max(closed_pnls), 4) if closed_pnls else None,
            "worst_ps": round(min(closed_pnls), 4) if closed_pnls else None,
            "sim_included": False}


def build_dashboard(scanner) -> dict:
    positions = _live_positions(scanner)
    rows = [_live_row(p) for p in positions]
    rows += [_sim_row(t) for t in _sim_trades(scanner)]
    rows.sort(key=lambda r: str(r.get("entry_time") or ""), reverse=True)
    return {"account": _account(scanner, positions),
            "trades": rows,
            "generated_at": datetime.now().isoformat(timespec="seconds")}


# ── trade detail ─────────────────────────────────────────────────────────
def trade_detail(scanner, trade_id) -> dict | None:
    """Full lifecycle + entry audit + narrative for one trade.
    None when the id is unknown (the API layer turns that into a 404)."""
    tid = str(trade_id or "").strip()
    if not tid:
        return None
    if tid.startswith("sim:"):
        return _sim_detail(scanner, tid)
    return _live_detail(scanner, tid)


def _live_detail(scanner, tid: str) -> dict | None:
    pos = None
    for p in _live_positions(scanner):
        if str(p.get("id")) == tid:
            pos = p
            break
    if pos is None:
        return None
    row = _live_row(pos)
    lifecycle = {
        "plan": pos.get("plan") if isinstance(pos.get("plan"), dict) else {},
        "stop_history": [h for h in (pos.get("stop_history") or [])
                         if isinstance(h, dict)],
        "exits": [e for e in (pos.get("exits") or []) if isinstance(e, dict)],
        "levels": pos.get("levels") or [],
        "wall_ref": pos.get("wall_ref"),
        "hwm": _f(pos.get("hwm")), "lwm": _f(pos.get("lwm")),
        "stop": _f(pos.get("stop")),
        "target1": _f(pos.get("target1")),
        "target2": _f(pos.get("target2"))}
    audit = _entry_audit_live(scanner, pos)
    return {**row, "lifecycle": lifecycle, "entry_audit": audit,
            "narrative": _narrative(row, lifecycle, audit)}


def _entry_audit_live(scanner, pos: dict):
    """The paper_entry row logged when this position's signal was accepted:
    it carries the FULL agent tree + trigger + score + any vetoes."""
    book = getattr(scanner, "paper", None)
    if book is None:
        return None
    try:
        rows = book.tail(PAPER_TAIL) or []
    except Exception:
        return None
    sym, direction = pos.get("symbol"), pos.get("direction")
    et = str(pos.get("entry_time") or "")
    best = None
    for r in rows:
        if not isinstance(r, dict) or r.get("type") != "paper_entry":
            continue
        if r.get("symbol") != sym:
            continue
        if direction and r.get("direction") not in (None, direction):
            continue
        trig = r.get("trigger") if isinstance(r.get("trigger"), dict) else {}
        if et and str(trig.get("bar_time") or "") == et:
            best = r                    # keep the LAST match
    return best


def _sim_detail(scanner, tid: str) -> dict | None:
    try:
        _, sym, et = tid.split(":", 2)
    except ValueError:
        return None
    trade = None
    for t in _sim_trades(scanner):
        if str(t.get("symbol")) == sym and str(t.get("entry_time") or "") == et:
            trade = t
            break
    # the signal feed row (sim=True) carries the tree AND the walked exits
    audit = walk = None
    try:
        signals = list(getattr(scanner, "signals", None) or [])
    except Exception:
        signals = []
    for s in signals:
        if not isinstance(s, dict) or not s.get("sim"):
            continue
        if str(s.get("symbol")) != sym:
            continue
        trig = s.get("trigger") if isinstance(s.get("trigger"), dict) else {}
        if str(trig.get("bar_time") or "") == et:
            audit = s
            if isinstance(s.get("sim_trade"), dict):
                walk = s["sim_trade"]
            break
    if trade is None and audit is None:
        return None
    if trade is None:                     # feed row survived, status row gone
        last = [e for e in ((walk or {}).get("exits") or [])
                if isinstance(e, dict)]
        last = last[-1] if last else {}
        trade = {"symbol": sym, "direction": (audit or {}).get("direction"),
                 "entry_time": et, "entry": (audit or {}).get("price"),
                 "exit_time": last.get("time"), "exit": last.get("price"),
                 "exit_reason": last.get("reason"),
                 "pnl_per_share": (walk or {}).get("pnl_per_share"),
                 "score": (audit or {}).get("score")}
    row = _sim_row(trade)
    walk = walk or {}
    plan = walk.get("plan") if isinstance(walk.get("plan"), dict) else {}
    hist = [h for h in (walk.get("stop_moves") or []) if isinstance(h, dict)]
    lifecycle = {
        "plan": plan,
        "stop_history": hist,
        "exits": [e for e in (walk.get("exits") or []) if isinstance(e, dict)],
        "levels": [], "wall_ref": None,
        "hwm": None, "lwm": None,
        "mfe": _f(walk.get("mfe")), "mae": _f(walk.get("mae")),
        "stop": (_f(hist[-1].get("to")) if hist else _f(plan.get("stop"))),
        "target1": _f(plan.get("target1")),
        "target2": _f(plan.get("target2"))}
    return {**row, "lifecycle": lifecycle, "entry_audit": audit,
            "narrative": _narrative(row, lifecycle, audit)}


# ── narrative: the trade's story, strictly from recorded data ────────────
def _stop_move_text(h: dict) -> str:
    to = _n(h.get("to"))
    r = str(h.get("reason") or "")
    if r.startswith("t1_breakeven"):
        inner = r[len("t1_breakeven"):].strip().strip("()")
        return (f"stop -> {to} (breakeven after T1 "
                + (inner if inner else "partial") + ")")
    if r.startswith("oi_level"):
        lvl = r[len("oi_level"):].strip()
        return f"stop -> {to} (OI ladder level {lvl or '?'} crossed)"
    if r.startswith("supertrend"):
        return f"trail stop -> {to} (supertrend)"
    if r.startswith("wall_unwind"):
        return f"stop -> {to} (wall unwind: {r[len('wall_unwind'):].strip()})"
    return f"stop -> {to} ({r or 'stop moved'})"


def _exit_text(e: dict) -> str:
    reason = str(e.get("reason") or "?")
    units = _f(e.get("units"))
    part = (f" {units:g}u" if units is not None and units < 1.0 - 1e-9
            else "")
    pnl = _f(e.get("pnl_per_share"))
    tail = f" -> {pnl:+.2f}/sh" if pnl is not None else ""
    return f"EXIT {reason}{part} @ {_n(e.get('price'))}{tail}"


def _narrative(row: dict, lifecycle: dict, audit) -> list[str]:
    d = str(row.get("direction") or "?")
    plan = lifecycle.get("plan") or {}
    hist = [h for h in (lifecycle.get("stop_history") or [])
            if isinstance(h, dict)]
    exits = [e for e in (lifecycle.get("exits") or []) if isinstance(e, dict)]
    out: list[str] = []

    # 1. ENTRY line — price, score, initial stop + its basis, targets
    score = _f(audit.get("score")) if isinstance(audit, dict) else None
    if score is None:
        score = _f(row.get("score"))
    initial = (hist[0] if hist
               and str(hist[0].get("reason", "")).startswith("initial") else None)
    stop0 = _f(initial.get("to")) if initial else _f(plan.get("stop"))
    basis = None
    if initial:
        basis = str(initial.get("reason"))[len("initial:"):].strip() or None
    if not basis:
        basis = plan.get("stop_basis")
    bits = []
    if score is not None:
        bits.append(f"score {score:.1f}")
    if stop0 is not None:
        bits.append(f"stop {_n(stop0)}" + (f" ({basis})" if basis else ""))
    t1, t2 = _f(lifecycle.get("target1")), _f(lifecycle.get("target2"))
    if t1 is not None:
        bits.append(f"t1 {_n(t1)}")
    if t2 is not None:
        bits.append(f"t2 {_n(t2)}")
    out.append(f"ENTRY {d} @ {_n(row.get('entry'))}"
               + (" — " + ", ".join(bits) if bits else ""))

    # 2. merge stop moves + exits chronologically. Same-timestamp order
    #    mirrors the engine's bar processing: target exits book before the
    #    ratchets they cause; the EOD square-off is always last in its bar.
    moves = hist[1:] if initial else hist
    events = ([("exit", str(e.get("time") or ""),
                2 if e.get("reason") == "eod" else 0, e) for e in exits]
              + [("stop", str(h.get("time") or ""), 1, h) for h in moves])
    events.sort(key=lambda x: (x[1], x[2]))
    for kind, _t, _p, obj in events:
        out.append(_exit_text(obj) if kind == "exit"
                   else _stop_move_text(obj))

    # 3. RESULT — derived from the recorded exits only, no invention
    state = row.get("state")
    pnl = _f(row.get("pnl_per_share"), 0.0)
    qty = _f(row.get("qty_units"))
    if state in (OPEN, PARTIAL):
        left = f", {qty:g}u still working" if qty else ""
        out.append(f"POSITION {state} — realized {pnl:+.2f}/sh so far{left}")
        return out
    reasons = [str(e.get("reason") or "") for e in exits]
    last = exits[-1] if exits else {}
    if pnl > 0:
        why = []
        if "target2" in reasons:
            why.append("both targets were hit")
        elif "target1" in reasons:
            why.append("target1 was hit for a partial")
        lr = last.get("reason")
        if lr == "stop":
            lp, e0 = _f(last.get("price")), _f(row.get("entry"))
            if lp is not None and e0 is not None and (
                    (lp >= e0) if d == "BUY" else (lp <= e0)):
                why.append("the ratcheted stop locked in the rest above "
                           "entry" if d == "BUY" else
                           "the ratcheted stop locked in the rest below "
                           "entry")
            else:
                why.append("the profit outweighed the final stop-out")
        elif lr == "eod":
            why.append("the remainder was squared off in profit at EOD")
        elif lr == "target2":
            pass                               # already covered above
        out.append(f"RESULT: {pnl:+.2f}/sh — the trade won because "
                   + (" and ".join(why) if why else
                      "realized exits netted positive"))
    elif pnl < 0:
        if "stop" in reasons:
            cause = (f"stop ({basis}) was hit before target" if basis
                     else "the stop was hit before target")
        elif "eod" in reasons:
            cause = "the EOD square-off closed it before target"
        else:
            cause = "exits realized less than entry"
        line = f"RESULT: {pnl:+.2f}/sh — lost because {cause}"
        mae = _f(lifecycle.get("mae"))
        if mae is None:                        # live: derive from water marks
            e0 = _f(row.get("entry"))
            lwm, hwm = _f(lifecycle.get("lwm")), _f(lifecycle.get("hwm"))
            if e0 is not None:
                if d == "BUY" and lwm is not None:
                    mae = max(0.0, e0 - lwm)
                elif d == "SELL" and hwm is not None:
                    mae = max(0.0, hwm - e0)
        atr = _f(plan.get("atr"))
        if mae is not None and atr:
            line += f"; MAE -{mae / atr:.2f} ATR"
        out.append(line)
    else:
        out.append("RESULT: +0.00/sh — flat: exits netted out at entry")
    return out


# ── self-test (no network, stub scanner) ─────────────────────────────────
if __name__ == "__main__":
    import pandas as pd

    NOW = datetime.now()
    TODAY = NOW.strftime("%Y-%m-%dT10:30:00")
    TODAY_L8R = NOW.strftime("%Y-%m-%dT11:05:00")
    TODAY_MID = NOW.strftime("%Y-%m-%dT12:20:00")
    TODAY_END = NOW.strftime("%Y-%m-%dT13:40:00")
    YDAY = "2026-07-02T10:15:00"
    YDAY_X = "2026-07-02T12:00:00"

    win_pos = {
        "id": "WINSTK-BUY-abc123", "symbol": "WINSTK", "direction": "BUY",
        "entry_price": 100.0, "entry_time": TODAY, "qty_units": 0.0,
        "state": "CLOSED", "stop": 101.1, "target1": 103.5, "target2": 107.0,
        "plan": {"atr": 1.0, "stop_basis": "put_wall 99 -/+ 0.35*atr",
                 "target1_basis": "call_wall 103.5 (oi 480000)"},
        "wall_ref": {"strike": 99.0, "oi": 500000.0},
        "levels": [100.0, 102.0], "hwm": 104.1, "lwm": 99.7,
        "exits": [
            {"time": TODAY_L8R, "price": 103.5, "units": 0.5,
             "reason": "target1", "pnl_per_share": 1.75},
            {"time": TODAY_END, "price": 101.1, "units": 0.5,
             "reason": "stop", "pnl_per_share": 0.55}],
        "stop_history": [
            {"time": TODAY, "from": None, "to": 98.65,
             "reason": "initial: put_wall 99 -/+ 0.35*atr"},
            {"time": TODAY_L8R, "from": 98.65, "to": 100.0,
             "reason": "t1_breakeven (partial +1.75/sh)"},
            {"time": TODAY_MID, "from": 100.0, "to": 101.1,
             "reason": "supertrend 101.10"}],
        "realized_pnl_per_share": 2.30}

    loss_pos = {
        "id": "LOSSTK-SELL-def456", "symbol": "LOSSTK", "direction": "SELL",
        "entry_price": 200.0, "entry_time": YDAY, "qty_units": 0.0,
        "state": "CLOSED", "stop": 201.5, "target1": 197.0, "target2": 194.4,
        "plan": {"atr": 2.0, "stop_basis": "atr_hard entry-/+1.4*atr"},
        "wall_ref": None, "levels": [], "hwm": 201.6, "lwm": 199.2,
        "exits": [{"time": YDAY_X, "price": 201.5, "units": 1.0,
                   "reason": "stop", "pnl_per_share": -1.5}],
        "stop_history": [{"time": YDAY, "from": None, "to": 201.5,
                          "reason": "initial: atr_hard entry-/+1.4*atr"}],
        "realized_pnl_per_share": -1.5}

    open_pos = {
        "id": "OPNSTK-BUY-789xyz", "symbol": "OPNSTK", "direction": "BUY",
        "entry_price": 50.0, "entry_time": TODAY_L8R, "qty_units": 1.0,
        "state": "OPEN", "stop": 49.0, "target1": 51.5, "target2": 52.8,
        "plan": {"atr": 0.5, "stop_basis": "atr_fallback entry-/+1.2*atr"},
        "wall_ref": None, "levels": [], "hwm": 50.4, "lwm": 49.8,
        "exits": [],
        "stop_history": [{"time": TODAY_L8R, "from": None, "to": 49.0,
                          "reason": "initial: atr_fallback entry-/+1.2*atr"}],
        "realized_pnl_per_share": 0.0}

    SIM_ET = NOW.strftime("%Y-%m-%dT12:00:00")
    sim_walk = {
        "exits": [{"time": NOW.strftime("%Y-%m-%dT12:35:00"), "price": 301.5,
                   "units": 0.5, "reason": "target1", "pnl_per_share": 0.75},
                  {"time": NOW.strftime("%Y-%m-%dT15:25:00"), "price": 301.0,
                   "units": 0.5, "reason": "eod", "pnl_per_share": 0.5}],
        "pnl_per_share": 1.25, "bars_held": 41, "mfe": 2.1, "mae": 0.4,
        "stop_moves": [
            {"time": SIM_ET, "from": None, "to": 298.6,
             "reason": "initial: atr_hard entry-/+1.4*atr"},
            {"time": NOW.strftime("%Y-%m-%dT12:35:00"), "from": 298.6,
             "to": 300.0, "reason": "t1_breakeven (partial +0.75/sh)"}],
        "plan": {"atr": 1.0, "stop": 298.6, "target1": 301.5,
                 "target2": 302.8,
                 "stop_basis": "atr_hard entry-/+1.4*atr",
                 "target_basis": "atr_fallback entry+/-1.5/2.8*atr"}}
    sim_trade = {"symbol": "SIMSTK", "direction": "BUY",
                 "entry_time": SIM_ET, "entry": 300.0,
                 "exit_time": NOW.strftime("%Y-%m-%dT15:25:00"),
                 "exit": 301.0, "exit_reason": "eod",
                 "pnl_per_share": 1.25, "score": 66.0}
    sim_signal = {"symbol": "SIMSTK", "direction": "BUY", "price": 300.0,
                  "score": 66.0, "accepted": True, "threshold": 55.0,
                  "sim": True, "sim_trade": sim_walk,
                  "tree": {"key": "root", "name": "Root", "score": 66.0,
                           "children": []},
                  "trigger": {"bar_time": SIM_ET, "symbol": "SIMSTK"}}

    paper_rows = [
        {"type": "paper_entry", "symbol": "WINSTK", "direction": "BUY",
         "price": 100.0, "score": 61.3, "accepted": True, "threshold": 55.0,
         "tree": {"key": "root", "name": "Root", "score": 61.3,
                  "children": [{"key": "smc", "name": "SMC", "score": 70.0,
                                "children": []}]},
         "trigger": {"bar_time": TODAY, "symbol": "WINSTK"}},
        {"type": "paper_exit", "symbol": "WINSTK", "reason": "target1"},
        {"type": "paper_entry", "symbol": "WINSTK", "direction": "BUY",
         "price": 999.0, "score": 40.0,     # different bar — must NOT match
         "tree": {"key": "root"}, "trigger": {"bar_time": "1999-01-01T09:15:00"}},
    ]

    class StubBook:
        def __init__(self, rows):
            self.rows = rows

        def tail(self, n=200):
            return self.rows[-n:]

    class StubExits:
        def __init__(self, positions):
            self._p = positions

        def summary(self):
            return {"open": 1, "positions": [dict(p) for p in self._p]}

    class StubHub:
        def __init__(self, prices):
            self.prices = prices

        def candles_5m(self, sym):
            px = self.prices.get(sym)
            if px is None:
                return None
            return pd.DataFrame(
                {"open": [px], "high": [px], "low": [px], "close": [px]},
                index=[pd.Timestamp(NOW)])

    class StubScanner:
        pass

    sc = StubScanner()
    sc.exits = StubExits([win_pos, loss_pos, open_pos])
    sc.paper = StubBook(paper_rows)
    sc.hub = StubHub({"OPNSTK": 51.25})       # WINSTK/LOSSTK candles gone
    sc.sim_status = {"running": False, "trades": [sim_trade]}
    sc.signals = [sim_signal]

    # ── account math ────────────────────────────────────────────────────
    dash = build_dashboard(sc)
    a = dash["account"]
    assert a["sim_included"] is False
    assert abs(a["realized_total_ps"] - 0.80) < 1e-9, a
    assert abs(a["realized_today_ps"] - 2.30) < 1e-9, a     # loss was y'day
    assert abs(a["unrealized_ps"] - 1.25) < 1e-9, a         # 51.25 - 50.0
    assert a["open_count"] == 1 and a["trades_total"] == 2
    assert a["wins"] == 1 and a["losses"] == 1
    assert abs(a["win_rate"] - 50.0) < 1e-9
    assert abs(a["avg_win_ps"] - 2.30) < 1e-9
    assert abs(a["avg_loss_ps"] - (-1.5)) < 1e-9
    assert abs(a["profit_factor"] - round(2.30 / 1.5, 3)) < 1e-9, a
    assert abs(a["best_ps"] - 2.30) < 1e-9 and abs(a["worst_ps"] + 1.5) < 1e-9

    # ── unified rows, newest first, sim included ────────────────────────
    rows = dash["trades"]
    assert len(rows) == 4, [r["id"] for r in rows]
    ets = [str(r.get("entry_time") or "") for r in rows]
    assert ets == sorted(ets, reverse=True), ets
    sim_rows = [r for r in rows if r["source"] == "sim"]
    assert len(sim_rows) == 1 and sim_rows[0]["id"] == f"sim:SIMSTK:{SIM_ET}"
    assert sim_rows[0]["exit_reason"] == "eod"
    assert {r["source"] for r in rows} == {"paper", "sim"}
    assert dash["generated_at"]

    # ── trade detail: WIN (live) ────────────────────────────────────────
    d = trade_detail(sc, "WINSTK-BUY-abc123")
    assert d is not None and d["symbol"] == "WINSTK"
    assert d["entry_audit"] is not None
    assert d["entry_audit"]["score"] == 61.3          # matched the RIGHT row
    assert d["entry_audit"]["tree"]["children"][0]["key"] == "smc"
    nr = d["narrative"]
    assert nr[0].startswith("ENTRY BUY @ 100"), nr[0]
    assert "score 61.3" in nr[0] and "put_wall 99" in nr[0], nr[0]
    assert any(s.startswith("EXIT target1") for s in nr), nr
    assert any(s.startswith("EXIT stop") and "+0.55/sh" in s for s in nr), nr
    assert any("breakeven after T1" in s for s in nr), nr
    assert any("trail stop -> 101.1 (supertrend)" in s for s in nr), nr
    assert nr[-1].startswith("RESULT: +2.30/sh") and "won because" in nr[-1]
    # story order: ENTRY first, T1 exit before the breakeven move it
    # caused, the trail move before the final stop-out it explains
    i_t1 = next(i for i, s in enumerate(nr) if s.startswith("EXIT target1"))
    i_be = next(i for i, s in enumerate(nr) if "breakeven" in s)
    i_tr = next(i for i, s in enumerate(nr) if s.startswith("trail stop"))
    i_sx = next(i for i, s in enumerate(nr) if s.startswith("EXIT stop"))
    assert 0 < i_t1 < i_be < i_tr < i_sx, nr
    assert d["lifecycle"]["stop_history"] and d["lifecycle"]["stop"] == 101.1

    # ── trade detail: LOSS (live) — MAE from water marks ────────────────
    d = trade_detail(sc, "LOSSTK-SELL-def456")
    assert d is not None and d["narrative"][0].startswith("ENTRY SELL @ 200")
    last = d["narrative"][-1]
    assert last.startswith("RESULT: -1.50/sh") and "lost because stop" in last
    assert "MAE -0.80 ATR" in last, last          # (201.6-200)/2.0

    # ── trade detail: SIM ───────────────────────────────────────────────
    d = trade_detail(sc, f"sim:SIMSTK:{SIM_ET}")
    assert d is not None and d["source"] == "sim"
    assert d["entry_audit"] is sim_signal
    assert d["lifecycle"]["mfe"] == 2.1 and d["lifecycle"]["mae"] == 0.4
    assert d["lifecycle"]["target1"] == 301.5 and d["lifecycle"]["stop"] == 300.0
    nr = d["narrative"]
    assert nr[0].startswith("ENTRY BUY @ 300") and "score 66.0" in nr[0]
    assert any(s.startswith("EXIT eod") for s in nr), nr
    assert nr[-1].startswith("RESULT: +1.25/sh"), nr[-1]

    # ── unknowns + degenerate scanners ──────────────────────────────────
    assert trade_detail(sc, "NOPE-123") is None
    assert trade_detail(sc, "sim:GHOST:2026-01-01T10:00:00") is None
    assert trade_detail(sc, None) is None
    bare = StubScanner()                          # nothing wired at all
    d0 = build_dashboard(bare)
    assert d0["account"]["trades_total"] == 0 and d0["trades"] == []
    assert d0["account"]["win_rate"] is None
    assert d0["account"]["profit_factor"] is None

    print("account:", {k: a[k] for k in ("realized_total_ps",
                                         "realized_today_ps",
                                         "unrealized_ps", "win_rate",
                                         "profit_factor")})
    print("narrative sample:")
    for s in trade_detail(sc, "WINSTK-BUY-abc123")["narrative"]:
        print("  •", s)
    print("ALL PNL SELF-TESTS PASSED")

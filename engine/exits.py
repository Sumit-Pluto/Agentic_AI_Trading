"""ExitEngine — OI-aware position lifecycle manager (paper + optional LIVE).

Turns accepted scanner signals into PaperPositions with option-chain-derived
stops/targets, then walks every OPEN/PARTIAL position bar-by-bar over closed
5-minute candles:

    stop -> target1 (partial + breakeven) -> target2, with an OI-level
    ratchet ladder, a Supertrend trail after breakeven, wall-unwind
    tightening, and a 15:12 IST end-of-day square-off.

Paper is the default: exits are logged to the PaperBook as ``paper_exit``
rows and persisted to state/paper_positions.json.  When a LiveExecutor is
attached (engine.executor) AND core.trade_mode.is_live(), the SAME
lifecycle also routes real Shoonya orders: entries in open_from_signal,
every _exit as an opposite-side MKT order, with a pending-close retry net
in update_all so a failed exit order is never silently dropped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

try:
    from signals.indicators import atr, supertrend
except ModuleNotFoundError:          # run as a script: add repo root to path
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from signals.indicators import atr, supertrend

try:
    from core.activity import activity
except Exception:                      # keep the engine importable in tests
    class _NoopActivity:
        def add(self, *args, **kwargs):
            pass
    activity = _NoopActivity()

log = logging.getLogger("exits")

BUY, SELL = "BUY", "SELL"                 # match quant.base string values
OPEN, PARTIAL, CLOSED = "OPEN", "PARTIAL", "CLOSED"

# ── module constants: every tunable parameter lives here ────────────────
ATR_LEN = 14                    # ATR lookback on 5m bars
WALL_SEARCH_ATR = 2.5           # protective wall must sit within this * ATR
WALL_STOP_BUFFER_ATR = 0.35     # stop buffer beyond a wall / ladder level
STOP_MIN_GAP_ATR = 0.3          # min breathing room between entry and stop
HARD_STOP_ATR = 1.4             # unconditional ATR stop candidate
FALLBACK_STOP_ATR = 1.2         # used when no candidate leaves breathing room
T1_WALL_MIN_ATR = 0.8           # opposing wall usable as target1 if inside
T1_WALL_MAX_ATR = 4.0           #   [entry+0.8*ATR, entry+4*ATR] (BUY)
T1_FALLBACK_ATR = 1.5           # target1 when no usable opposing wall
T2_FALLBACK_ATR = 2.8           # target2 when no usable opposing wall
LADDER_LEVELS = 4               # top-N total-OI strikes between stop..target2
PARTIAL_FRACTION = 0.5          # units booked at target1
WALL_CHECK_MIN = 15             # minutes between chain refetches per position
WALL_UNWIND_RATIO = 0.70        # wall OI below this fraction of entry = unwind
WALL_UNWIND_STOP_ATR = 0.6      # tighten stop to entry -/+ this * ATR on unwind
WALL_UNWIND_PARTIAL_TRAIL_ATR = 1.0   # PARTIAL: tighten to hwm/lwm -/+ this*ATR
EOD_SQUARE_OFF = dtime(15, 12)  # IST square-off time
BAR_MINUTES = 5                 # bar interval; a bar is closed once this old
ATR_FALLBACK_PCT = 0.005        # ATR fallback = max(this * entry, ATR_FLOOR)
ATR_FLOOR = 0.05
SIM_EOD_TIME = dtime(15, 25)    # sim walk: force-close at the day's last
                                # bar at/before this time (reason 'eod')

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "paper_positions.json")


# ── pure historical walk (day simulation) ────────────────────────────────
def simulate_position(df, entry_i: int, direction: str,
                      entry_price: float) -> dict:
    """Pure point-in-time walk of a simulated trade over the REMAINING bars
    of the entry day. No hub, no persistence, no side effects — used by the
    scanner's day replay to grade its simulated entries.

    Uses the engine's own constants:

      stop    = entry -/+ HARD_STOP_ATR * atr   (atr(ATR_LEN) at entry_i,
                point-in-time — Wilder rma is causal, so computing on the
                full df and reading .iloc[entry_i] is prefix-identical)
      target1 = entry +/- T1_FALLBACK_ATR * atr
      target2 = entry +/- T2_FALLBACK_ATR * atr

    target1/target2 are the ATR FALLBACKS only: the live engine prefers
    opposing OI walls, but historical option-chain snapshots cannot be
    reconstructed, so the walls simply don't exist here.

    Rules, mirroring ExitEngine._process_bar:
      * intrabar priority is stop-BEFORE-target on the same bar
        (conservative);
      * target1 books PARTIAL_FRACTION units and ratchets the stop to
        breakeven;
      * after the partial, a supertrend trail (signals.indicators.supertrend
        computed once on the full df — it's causal) ratchets the stop while
        the regime agrees with the trade;
      * whatever is left is force-closed at the day's last bar at/before
        SIM_EOD_TIME (reason 'eod').

    Returns {'exits': [{time, price, units, reason, pnl_per_share}],
             'pnl_per_share': <units-weighted total>, 'bars_held': int,
             'mfe': float, 'mae': float,
             'stop_moves': [{time, 'from': old, 'to': new, reason}],
             'plan': {atr, stop, target1, target2, stop_basis, target_basis}}
    (mfe/mae are per-share favourable/adverse excursions from entry over the
    walked bars, floored at 0; stop_moves is the full stop audit trail,
    starting with the initial set).
    """
    entry_i = int(entry_i)
    entry = float(entry_price)
    buy = direction == BUY
    sign = 1.0 if buy else -1.0

    a = None
    try:
        v = atr(df, ATR_LEN).iloc[entry_i]
        if pd.notna(v) and float(v) > 0:
            a = float(v)
    except Exception:
        pass
    if a is None:
        a = max(ATR_FALLBACK_PCT * entry, ATR_FLOOR)

    stop = entry - sign * HARD_STOP_ATR * a
    target1 = entry + sign * T1_FALLBACK_ATR * a
    target2 = entry + sign * T2_FALLBACK_ATR * a
    plan = {"atr": round(a, 4), "stop": round(stop, 4),
            "target1": round(target1, 4), "target2": round(target2, 4),
            "stop_basis": f"atr_hard entry-/+{HARD_STOP_ATR}*atr",
            "target_basis": (f"atr_fallback entry+/-{T1_FALLBACK_ATR}/"
                             f"{T2_FALLBACK_ATR}*atr")}

    st = None
    try:
        st = supertrend(df)                # causal — full-df compute is fine
    except Exception:
        pass

    entry_ts = df.index[entry_i]
    day = entry_ts.date()
    exits: list[dict] = []
    stop_moves: list[dict] = [{
        "time": entry_ts.isoformat(), "from": None, "to": round(stop, 4),
        "reason": f"initial: {plan['stop_basis']}"}]

    def move_stop(ts, candidate, reason):
        """Ratchet the stop favourably and record the move (no-ops skipped)."""
        nonlocal stop
        new = max(stop, candidate) if buy else min(stop, candidate)
        if abs(new - stop) < 1e-9:
            return
        stop_moves.append({"time": ts.isoformat(),
                           "from": round(stop, 4), "to": round(new, 4),
                           "reason": reason})
        stop = new

    units = 1.0
    partial_done = False
    mfe = mae = 0.0
    bars_held = 0

    def book(ts, price, u, reason):
        nonlocal units
        u = min(float(u), units)
        if u <= 0:
            return
        pnl = ((price - entry) if buy else (entry - price)) * u
        exits.append({"time": ts.isoformat(),
                      "price": round(float(price), 4),
                      "units": round(u, 6), "reason": reason,
                      "pnl_per_share": round(pnl, 6)})
        units = round(units - u, 6)
        if units <= 1e-9:
            units = 0.0

    idx = df.index
    walk = [i for i in range(entry_i + 1, len(df))
            if idx[i].date() == day and idx[i].time() <= SIM_EOD_TIME]
    if not walk:                           # nothing left of the day
        book(entry_ts, float(df["close"].iloc[entry_i]), units, "eod")
    else:
        last_i = walk[-1]
        for i in walk:
            ts = idx[i]
            row = df.iloc[i]
            high, low = float(row["high"]), float(row["low"])
            close = float(row["close"])
            bars_held += 1
            if buy:
                mfe = max(mfe, high - entry)
                mae = max(mae, entry - low)
            else:
                mfe = max(mfe, entry - low)
                mae = max(mae, high - entry)

            # a. stop first — conservative same-bar priority
            if (buy and low <= stop) or (not buy and high >= stop):
                book(ts, stop, units, "stop")
                break

            # b. target1 — partial + breakeven ratchet
            if not partial_done and ((buy and high >= target1) or
                                     (not buy and low <= target1)):
                book(ts, target1, PARTIAL_FRACTION, "target1")
                partial_done = True
                if units <= 0:
                    break
                move_stop(ts, entry,
                          "t1_breakeven (partial "
                          f"{exits[-1]['pnl_per_share']:+.2f}/sh)")

            # c. target2 — close the rest
            if partial_done and units > 0 and (
                    (buy and high >= target2) or
                    (not buy and low <= target2)):
                book(ts, target2, units, "target2")
                break

            # d. supertrend trail — only after the partial
            if partial_done and units > 0 and st is not None:
                try:
                    line = float(st["line"].iloc[i])
                    bull = bool(st["bull"].iloc[i])
                except (KeyError, ValueError, TypeError, IndexError):
                    line = None
                if line is not None and pd.notna(line) and (
                        (buy and bull) or (not buy and not bull)):
                    move_stop(ts, line, f"supertrend {line:.2f}")

            # e. force-close at the day's last usable bar
            if units > 0 and i == last_i:
                book(ts, close, units, "eod")

    total = round(sum(e["pnl_per_share"] for e in exits), 6)
    return {"exits": exits, "pnl_per_share": total,
            "bars_held": bars_held,
            "mfe": round(mfe, 6), "mae": round(mae, 6),
            "stop_moves": stop_moves, "plan": plan}


# ── position ─────────────────────────────────────────────────────────────
@dataclass
class PaperPosition:
    """One paper trade. All fields are plain JSON types (json-serializable)."""
    id: str
    symbol: str
    direction: str                       # BUY / SELL
    entry_price: float
    entry_time: str                      # iso
    qty_units: float = 1.0               # fraction remaining (1.0 -> 0.5 ...)
    strategy: str = "intraday"           # intraday | swing | manual | oi
    segment: str = "FNO"                 # CASH | FNO | MCX
    state: str = OPEN                    # OPEN / PARTIAL / CLOSED
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    plan: dict = field(default_factory=dict)     # how stops/targets derived
    wall_ref: dict | None = None         # {'strike','oi'} entry snapshot | None
    levels: list = field(default_factory=list)   # OI ratchet ladder strikes
    hwm: float | None = None
    lwm: float | None = None
    exits: list = field(default_factory=list)    # [{time,price,units,reason,
                                                 #   pnl_per_share}]
    stop_history: list = field(default_factory=list)  # [{time,'from','to',
                                                 #   reason}] — EVERY stop
                                                 #   change, initial included
    realized_pnl_per_share: float = 0.0
    live: bool = False                   # real orders behind this position
    live_qty: int = 0                    # shares filled at entry
    live_qty_left: int = 0               # shares still open at the broker
    live_entry_fill: float | None = None # actual average entry fill price
    live_pending_close: int = 0          # shares whose exit order FAILED —
                                         # retried every update_all tick
    live_orders: list = field(default_factory=list)  # fill audit trail
    last_update: str = ""
    last_bar: str = ""                   # iso of last processed 5m bar
    last_wall_check: str = ""            # iso of last chain refetch

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PaperPosition":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# ── engine ───────────────────────────────────────────────────────────────
class ExitEngine:
    """Lifecycle manager: hub for data, paper_book for the exit audit trail."""

    def __init__(self, hub, paper_book, state_path: str = STATE_PATH):
        self.hub = hub
        self.book = paper_book
        self.state_path = state_path
        self._lock = threading.RLock()
        self.positions: dict[str, PaperPosition] = {}
        self.executor = None       # LiveExecutor, injected by run_app; None
                                   # or inactive -> pure paper (the default)
        self._load()
        self._sync_live_count()

    def _sync_live_count(self):
        """Tell the executor how many LIVE positions are open (its
        max-positions gate). No-op without an executor."""
        ex = self.executor
        if ex is None:
            return
        try:
            ex.open_live_count = sum(1 for p in self.positions.values()
                                     if p.live and p.state != CLOSED)
        except Exception:
            pass

    # ── persistence (atomic write, corrupt-tolerant load) ───────────────
    def _load(self):
        try:
            with open(self.state_path) as f:
                raw = json.load(f)
            rows = raw.get("positions", []) if isinstance(raw, dict) else raw
            for d in rows:
                try:
                    p = PaperPosition.from_dict(d)
                    self.positions[p.id] = p
                except (TypeError, KeyError) as e:
                    log.warning("skipping corrupt position row: %s", e)
        except FileNotFoundError:
            pass
        except Exception as e:                       # corrupt file — start clean
            log.warning("could not load %s (%s); starting empty",
                        self.state_path, e)

    def _save(self):
        with self._lock:
            payload = {"saved_at": datetime.now().isoformat(timespec="seconds"),
                       "positions": [p.to_dict()
                                     for p in self.positions.values()]}
            d = os.path.dirname(self.state_path)
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{os.path.basename(self.state_path)}."
                                  f"{uuid.uuid4().hex[:8]}.tmp")
            try:
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=1)
                os.replace(tmp, self.state_path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _ratchet(direction: str, stop: float, candidate: float) -> float:
        """Move the stop only in the favourable direction (never loosen)."""
        return max(stop, candidate) if direction == BUY else min(stop, candidate)

    @staticmethod
    def _log_stop(pos: PaperPosition, time_iso: str, old, new, reason: str):
        """Append a stop-history record; no-op moves are skipped."""
        try:
            if old is not None and abs(float(new) - float(old)) < 1e-9:
                return
        except (TypeError, ValueError):
            return
        pos.stop_history.append({
            "time": time_iso,
            "from": None if old is None else round(float(old), 4),
            "to": round(float(new), 4),
            "reason": reason})

    def _atr_now(self, df, entry: float) -> tuple[float, bool]:
        try:
            ser = atr(df, ATR_LEN).dropna()
            if len(ser) and float(ser.iloc[-1]) > 0:
                return float(ser.iloc[-1]), False
        except Exception:
            pass
        return max(ATR_FALLBACK_PCT * entry, ATR_FLOOR), True

    @staticmethod
    def _leg_oi(row, side: str) -> float:
        leg = getattr(row, side, None)
        try:
            return float(leg.oi) if leg is not None and leg.oi else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ── 1. open ──────────────────────────────────────────────────────────
    def open_from_signal(self, signal_result: dict) -> PaperPosition:
        """Create a PaperPosition from an accepted scanner signal dict
        (symbol / direction / price / trigger.bar_time / ...)."""
        with self._lock:
            sym = signal_result["symbol"]
            direction = signal_result["direction"]
            entry = float(signal_result["price"])

            # one live position per symbol+side — repeats manage the original
            for p in self.positions.values():
                if (p.symbol == sym and p.direction == direction
                        and p.state != CLOSED):
                    log.info("exits: %s %s already live (%s) — not stacking",
                             direction, sym, p.id)
                    return p

            buy = direction == BUY
            sign = 1.0 if buy else -1.0

            df = self.hub.candles_5m(sym)
            if df is None or df.empty:
                raise ValueError(f"no candles for {sym}")
            a, a_fb = self._atr_now(df, entry)

            st_line = st_bull = None
            try:
                st = supertrend(df)
                st_line = float(st["line"].iloc[-1])
                st_bull = bool(st["bull"].iloc[-1])
            except Exception as e:
                log.debug("supertrend %s failed: %s", sym, e)

            chain = None
            try:
                chain = self.hub.chain_snapshot(sym)
            except Exception as e:
                log.debug("chain %s failed: %s", sym, e)

            # walls: protective (behind entry, within 2.5*ATR, max OI on the
            # defending side) and opposing (ahead of entry, max OI other side)
            prot = opp = None                       # (strike, oi)
            if chain is not None and getattr(chain, "strikes", None):
                for row in chain.strikes:
                    k = float(row.strike)
                    pe_oi = self._leg_oi(row, "pe")
                    ce_oi = self._leg_oi(row, "ce")
                    behind_oi, ahead_oi = (pe_oi, ce_oi) if buy else (ce_oi, pe_oi)
                    behind = k < entry if buy else k > entry
                    ahead = k > entry if buy else k < entry
                    if (behind and abs(entry - k) <= WALL_SEARCH_ATR * a
                            and behind_oi > 0
                            and (prot is None or behind_oi > prot[1])):
                        prot = (k, behind_oi)
                    if ahead and ahead_oi > 0 and (opp is None
                                                   or ahead_oi > opp[1]):
                        opp = (k, ahead_oi)

            prot_side = "put_wall" if buy else "call_wall"
            opp_side = "call_wall" if buy else "put_wall"

            # stop: highest (BUY) / lowest (SELL) candidate that still leaves
            # STOP_MIN_GAP_ATR of breathing room; else the ATR fallback
            cands: dict[str, float] = {}
            if prot:
                cands[f"{prot_side} {prot[0]:g} -/+ "
                      f"{WALL_STOP_BUFFER_ATR}*atr"] = (
                    prot[0] - sign * WALL_STOP_BUFFER_ATR * a)
            if st_line is not None and ((buy and st_line < entry)
                                        or (not buy and st_line > entry)):
                cands[f"supertrend {st_line:.2f}"] = st_line
            cands[f"atr_hard entry-/+{HARD_STOP_ATR}*atr"] = (
                entry - sign * HARD_STOP_ATR * a)

            gap_edge = entry - sign * STOP_MIN_GAP_ATR * a
            ok = {lbl: v for lbl, v in cands.items()
                  if (v < gap_edge if buy else v > gap_edge)}
            if ok:
                lbl = (max(ok, key=ok.get) if buy else min(ok, key=ok.get))
                stop, stop_basis = ok[lbl], lbl
            else:
                stop = entry - sign * FALLBACK_STOP_ATR * a
                stop_basis = f"atr_fallback entry-/+{FALLBACK_STOP_ATR}*atr"

            # targets
            t1_lo = entry + sign * T1_WALL_MIN_ATR * a
            t1_hi = entry + sign * T1_WALL_MAX_ATR * a
            wall_target = (opp is not None and
                           min(t1_lo, t1_hi) <= opp[0] <= max(t1_lo, t1_hi))
            if wall_target:
                target1 = opp[0]
                target2 = opp[0] + (target1 - entry)     # wall + T1 range
                t1_basis = f"{opp_side} {opp[0]:g} (oi {opp[1]:.0f})"
                t2_basis = (f"{opp_side} {opp[0]:g} + t1_range "
                            f"{target1 - entry:+.2f}")
            else:
                target1 = entry + sign * T1_FALLBACK_ATR * a
                target2 = entry + sign * T2_FALLBACK_ATR * a
                t1_basis = f"atr_fallback entry+/-{T1_FALLBACK_ATR}*atr"
                t2_basis = f"atr_fallback entry+/-{T2_FALLBACK_ATR}*atr"

            # ratchet ladder: top-N total-OI strikes strictly between stop
            # and target2
            levels: list[float] = []
            if chain is not None and getattr(chain, "strikes", None):
                lo, hi = min(stop, target2), max(stop, target2)
                scored = [(float(r.strike),
                           self._leg_oi(r, "ce") + self._leg_oi(r, "pe"))
                          for r in chain.strikes
                          if lo < float(r.strike) < hi]
                scored = [(k, oi) for k, oi in scored if oi > 0]
                top = sorted(scored, key=lambda x: -x[1])[:LADDER_LEVELS]
                levels = sorted(k for k, _ in top)

            trig = signal_result.get("trigger") or {}
            entry_time = trig.get("bar_time") or datetime.now().isoformat(
                timespec="seconds")

            plan = {
                "atr": round(a, 4),
                "atr_source": "fallback_pct" if a_fb else f"atr({ATR_LEN})",
                "st_line": None if st_line is None else round(st_line, 4),
                "st_bull": st_bull,
                "chain": "present" if chain is not None else "missing",
                "protective_wall": (f"{prot_side} {prot[0]:g} "
                                    f"oi={prot[1]:.0f}" if prot else
                                    f"none within {WALL_SEARCH_ATR}*atr"),
                "opposing_wall": (f"{opp_side} {opp[0]:g} oi={opp[1]:.0f}"
                                  if opp else "none"),
                "stop_basis": stop_basis,
                "stop_candidates": {k: round(v, 4) for k, v in cands.items()},
                "breathing_room_edge": round(gap_edge, 4),
                "target1_basis": t1_basis,
                "target2_basis": t2_basis,
                "ladder": levels,
                "notes": [],
            }

            pos = PaperPosition(
                id=f"{sym}-{direction}-{uuid.uuid4().hex[:8]}",
                symbol=sym, direction=direction,
                strategy=signal_result.get("strategy", "intraday"),
                segment=(signal_result.get("segment")
                         or self.hub.segment_of(sym)),
                entry_price=round(entry, 4), entry_time=entry_time,
                stop=round(stop, 4), target1=round(target1, 4),
                target2=round(target2, 4), plan=plan,
                wall_ref=({"strike": prot[0], "oi": prot[1]} if prot else None),
                levels=levels, hwm=entry, lwm=entry,
                stop_history=[{"time": entry_time, "from": None,
                               "to": round(stop, 4),
                               "reason": f"initial: {stop_basis}"}],
                last_update=datetime.now().isoformat(timespec="seconds"),
                last_bar=entry_time, last_wall_check=entry_time)

            # live mode: the REAL order decides whether this trade exists.
            # Refused (limits/window/sizing) or rejected -> no position at
            # all; filled -> track actual shares and the true fill price.
            ex = self.executor
            if ex is not None and ex.active():
                res = ex.enter(symbol=sym, direction=direction, price=entry,
                               stop=float(pos.stop), position_id=pos.id)
                if res is None:
                    log.warning("exits: LIVE entry %s %s refused/failed — "
                                "signal not taken", direction, sym)
                    activity.add(
                        "position",
                        f"LIVE entry {direction} {sym} refused/failed — "
                        f"signal skipped (see live_error/limits)",
                        symbol=sym, direction=direction)
                    return None
                pos.live = True
                pos.live_qty = int(res["qty"])
                pos.live_qty_left = int(res["qty"])
                pos.live_entry_fill = float(res["fill_price"])
                pos.live_orders = [dict(res, leg="entry")]
                pos.entry_price = round(float(res["fill_price"]), 4)

            self.positions[pos.id] = pos
            self._sync_live_count()
            self._save()
            log.info("exits: opened %s %s @ %.2f stop=%.2f t1=%.2f t2=%.2f "
                     "(%s)", direction, sym, entry, pos.stop, pos.target1,
                     pos.target2, stop_basis)
            activity.add(
                "position",
                f"opened {direction} {sym} @ {entry:g} stop {pos.stop:g} "
                f"t1 {pos.target1:g} t2 {pos.target2:g} ({stop_basis})",
                position_id=pos.id, symbol=sym, direction=direction,
                entry=float(entry), stop=pos.stop, target1=pos.target1,
                target2=pos.target2)
            return pos

    # ── 2. update ────────────────────────────────────────────────────────
    def update_all(self):
        """Walk every OPEN/PARTIAL position over its new CLOSED 5m bars."""
        with self._lock:
            changed = False
            now = datetime.now()
            for pos in list(self.positions.values()):
                if pos.state == CLOSED:
                    continue
                try:
                    changed = self._update_position(pos, now) or changed
                except Exception as e:
                    log.warning("exit update %s failed: %s", pos.id, e)
            changed = self._retry_pending_closes() or changed
            if changed:
                self._save()

    def _retry_pending_closes(self) -> bool:
        """Re-fire live exit orders that failed earlier (network blip,
        broker 5xx, session hiccup). Runs every tick until flat."""
        ex = self.executor
        if ex is None or not ex.active():
            return False
        changed = False
        for pos in self.positions.values():
            shares = int(pos.live_pending_close or 0)
            if not (pos.live and shares > 0):
                continue
            try:
                res = ex.close(symbol=pos.symbol, direction=pos.direction,
                               qty=shares,
                               entry_fill=(pos.live_entry_fill
                                           or pos.entry_price),
                               position_id=pos.id, reason="retry_close")
            except Exception as e:
                log.error("retry close %s x%d failed: %s",
                          pos.symbol, shares, e)
                continue
            if res is not None:
                pos.live_pending_close = 0
                pos.live_qty_left = max(pos.live_qty_left - shares, 0)
                pos.live_orders.append(dict(res, leg="exit"))
                changed = True
                self._sync_live_count()
        return changed

    def _update_position(self, pos: PaperPosition, now: datetime) -> bool:
        df = self.hub.candles_5m(pos.symbol)
        if df is None or df.empty:
            return False
        try:
            last_ts = pd.Timestamp(datetime.fromisoformat(pos.last_bar))
        except (ValueError, TypeError):
            last_ts = df.index[0] - pd.Timedelta(minutes=1)
        closed_edge = pd.Timestamp(now) - pd.Timedelta(minutes=BAR_MINUTES)
        new = df[(df.index > last_ts) & (df.index <= closed_edge)]
        if new.empty:
            return False

        a_ser = atr(df, ATR_LEN)
        st = None
        try:
            st = supertrend(df)
        except Exception as e:
            log.debug("supertrend %s failed: %s", pos.symbol, e)

        for ts, row in new.iterrows():
            a_cur = None
            try:
                v = a_ser.loc[ts]
                a_cur = float(v) if pd.notna(v) and float(v) > 0 else None
            except (KeyError, TypeError):
                pass
            if a_cur is None:
                a_cur = float(pos.plan.get("atr") or
                              max(ATR_FALLBACK_PCT * pos.entry_price,
                                  ATR_FLOOR))
            st_line = st_bull = None
            if st is not None:
                try:
                    st_line = float(st["line"].loc[ts])
                    st_bull = bool(st["bull"].loc[ts])
                except (KeyError, ValueError, TypeError):
                    pass
            self._process_bar(pos, ts, row, a_cur, st_line, st_bull)
            pos.last_bar = ts.isoformat()
            if pos.state == CLOSED:
                break
        pos.last_update = now.isoformat(timespec="seconds")
        return True

    def _process_bar(self, pos: PaperPosition, ts, row,
                     a_cur: float, st_line, st_bull):
        buy = pos.direction == BUY
        sign = 1.0 if buy else -1.0
        high, low = float(row["high"]), float(row["low"])
        close = float(row["close"])

        # a. high/low water marks
        pos.hwm = high if pos.hwm is None else max(pos.hwm, high)
        pos.lwm = low if pos.lwm is None else min(pos.lwm, low)

        # b. stop hit — close everything that remains
        if (buy and low <= pos.stop) or (not buy and high >= pos.stop):
            self._exit(pos, ts, pos.stop, pos.qty_units, "stop")
            return

        # c. target1 — book the partial, ratchet stop to breakeven
        if pos.state == OPEN and ((buy and high >= pos.target1) or
                                  (not buy and low <= pos.target1)):
            self._exit(pos, ts, pos.target1, PARTIAL_FRACTION, "target1")
            if pos.state != CLOSED:
                pos.state = PARTIAL
                before = pos.stop
                pos.stop = self._ratchet(pos.direction, pos.stop,
                                         pos.entry_price)
                t1_pnl = (pos.exits[-1].get("pnl_per_share")
                          if pos.exits else None)
                self._log_stop(pos, ts.isoformat(), before, pos.stop,
                               "t1_breakeven (partial "
                               + (f"{t1_pnl:+.2f}/sh" if t1_pnl is not None
                                  else "booked") + ")")

        # d. target2 — close the rest
        if pos.state == PARTIAL and ((buy and high >= pos.target2) or
                                     (not buy and low <= pos.target2)):
            self._exit(pos, ts, pos.target2, pos.qty_units, "target2")
            return

        # e. OI-level ratchet ladder
        for lvl in pos.levels:
            if (buy and close > lvl) or (not buy and close < lvl):
                before = pos.stop
                pos.stop = self._ratchet(
                    pos.direction, pos.stop,
                    lvl - sign * WALL_STOP_BUFFER_ATR * a_cur)
                self._log_stop(pos, ts.isoformat(), before, pos.stop,
                               f"oi_level {lvl:g}")

        # f. supertrend trail — only once breakeven (post-partial)
        if (pos.state == PARTIAL and st_line is not None
                and st_bull is not None
                and ((buy and st_bull) or (not buy and not st_bull))):
            before = pos.stop
            pos.stop = self._ratchet(pos.direction, pos.stop, st_line)
            self._log_stop(pos, ts.isoformat(), before, pos.stop,
                           f"supertrend {st_line:.2f}")

        # g. wall-unwind tighten (throttled chain refetch)
        self._wall_check(pos, ts, a_cur)

        # h. end-of-day square-off
        if pos.state != CLOSED and ts.time() >= EOD_SQUARE_OFF:
            self._exit(pos, ts, close, pos.qty_units, "eod")

    def _wall_check(self, pos: PaperPosition, ts, a_cur: float):
        if not pos.wall_ref or pos.state == CLOSED:
            return
        try:
            last = datetime.fromisoformat(pos.last_wall_check or
                                          pos.entry_time)
        except (ValueError, TypeError):
            last = ts.to_pydatetime()
        if (ts.to_pydatetime() - last) < timedelta(minutes=WALL_CHECK_MIN):
            return
        pos.last_wall_check = ts.isoformat()
        try:
            chain = self.hub.chain_snapshot(pos.symbol)
        except Exception as e:
            log.debug("wall refetch %s failed: %s", pos.symbol, e)
            return
        if chain is None or not getattr(chain, "strikes", None):
            return
        side = "pe" if pos.direction == BUY else "ce"
        cur_oi = None
        for r in chain.strikes:
            if abs(float(r.strike) - float(pos.wall_ref["strike"])) < 1e-6:
                cur_oi = self._leg_oi(r, side)
                break
        if cur_oi is None:
            return
        ref_oi = float(pos.wall_ref.get("oi") or 0)
        if ref_oi <= 0 or cur_oi >= WALL_UNWIND_RATIO * ref_oi:
            return
        buy = pos.direction == BUY
        sign = 1.0 if buy else -1.0
        before = pos.stop
        pos.stop = self._ratchet(pos.direction, pos.stop,
                                 pos.entry_price -
                                 sign * WALL_UNWIND_STOP_ATR * a_cur)
        if pos.state == PARTIAL:
            marker = pos.hwm if buy else pos.lwm
            pos.stop = self._ratchet(
                pos.direction, pos.stop,
                marker - sign * WALL_UNWIND_PARTIAL_TRAIL_ATR * a_cur)
        self._log_stop(pos, ts.isoformat(), before, pos.stop,
                       f"wall_unwind {pos.wall_ref['strike']:g} "
                       f"(oi {cur_oi:.0f} < {WALL_UNWIND_RATIO:.0%} "
                       f"of {ref_oi:.0f})")
        note = (f"{ts.isoformat()} wall_unwind {pos.wall_ref['strike']:g}: "
                f"oi {cur_oi:.0f} < {WALL_UNWIND_RATIO:.0%} of {ref_oi:.0f} "
                f"-> stop {before:.2f} -> {pos.stop:.2f}")
        notes = pos.plan.setdefault("notes", [])
        if not notes or not notes[-1].split(" ", 1)[-1] == note.split(" ", 1)[-1]:
            notes.append(note)
        log.info("exits: %s", note)
        activity.add(
            "position",
            f"wall-tighten {pos.symbol}: stop {before:.2f} -> "
            f"{pos.stop:.2f} (wall {pos.wall_ref['strike']:g} unwinding)",
            position_id=pos.id, symbol=pos.symbol,
            stop_before=float(before), stop_after=float(pos.stop))

    # ── exit booking ─────────────────────────────────────────────────────
    def _exit(self, pos: PaperPosition, ts, price: float, units: float,
              reason: str):
        units = min(float(units), pos.qty_units)
        if units <= 0:
            return
        buy = pos.direction == BUY
        pnl = ((price - pos.entry_price) if buy else
               (pos.entry_price - price)) * units
        rec = {"time": ts.isoformat(), "price": round(float(price), 4),
               "units": round(units, 6), "reason": reason,
               "pnl_per_share": round(pnl, 6)}
        pos.exits.append(rec)
        pos.qty_units = round(pos.qty_units - units, 6)
        pos.realized_pnl_per_share = round(
            pos.realized_pnl_per_share + pnl, 6)
        if pos.qty_units <= 1e-9:
            pos.qty_units = 0.0
            pos.state = CLOSED

        # live position: mirror this exit at the broker. The final exit
        # closes ALL remaining live shares (kills rounding drift); a failed
        # order is parked in live_pending_close and retried every tick.
        if pos.live and pos.live_qty_left > 0:
            if pos.state == CLOSED:
                shares = pos.live_qty_left
            else:
                shares = min(int(round(units * pos.live_qty)),
                             pos.live_qty_left)
            if shares >= 1:
                ex = self.executor
                res = None
                if ex is not None:
                    try:
                        res = ex.close(
                            symbol=pos.symbol, direction=pos.direction,
                            qty=shares,
                            entry_fill=(pos.live_entry_fill
                                        or pos.entry_price),
                            position_id=pos.id, reason=reason)
                    except Exception as e:
                        log.error("LIVE close %s x%d failed: %s",
                                  pos.symbol, shares, e)
                if res is not None:
                    pos.live_qty_left -= shares
                    pos.live_orders.append(dict(res, leg="exit"))
                    rec["live_fill"] = res.get("fill_price")
                    rec["live_pnl_rupees"] = res.get("pnl_rupees")
                    rec["live_order_no"] = res.get("order_no")
                else:
                    pos.live_pending_close += shares
                    activity.add(
                        "risk",
                        f"LIVE EXIT ORDER FAILED {pos.symbol} x{shares} "
                        f"({reason}) — will retry every tick; CHECK THE "
                        f"BROKER TERMINAL", symbol=pos.symbol,
                        qty=int(shares), reason=reason)
            self._sync_live_count()
        try:
            self.book.record({"type": "paper_exit", "position_id": pos.id,
                              "symbol": pos.symbol,
                              "direction": pos.direction,
                              "entry_price": pos.entry_price,
                              "state": pos.state,
                              "qty_left": pos.qty_units, **rec})
        except Exception as e:
            log.warning("paper_exit record failed: %s", e)
        log.info("exits: %s %s %s %.4g units @ %.2f pnl/share %+.2f",
                 reason.upper(), pos.direction, pos.symbol, units,
                 price, pnl)
        activity.add(
            "exit",
            f"{reason.upper()} {pos.direction} {pos.symbol} {units:g}u "
            f"@ {float(price):.2f} pnl/share {pnl:+.2f}"
            + (" — position closed" if pos.state == CLOSED else
               f" — {pos.qty_units:g}u left"),
            position_id=pos.id, symbol=pos.symbol, reason=reason,
            units=float(units), price=float(price),
            pnl_per_share=float(pnl), state=pos.state)

    # ── 3. summary ───────────────────────────────────────────────────────
    def summary(self) -> dict:
        with self._lock:
            today = date.today().isoformat()
            open_n = sum(1 for p in self.positions.values()
                         if p.state == OPEN)
            part_n = sum(1 for p in self.positions.values()
                         if p.state == PARTIAL)
            closed_today = sum(
                1 for p in self.positions.values()
                if p.state == CLOSED and p.exits
                and str(p.exits[-1].get("time", ""))[:10] == today)
            pnl_today = sum(
                float(e.get("pnl_per_share") or 0)
                for p in self.positions.values() for e in p.exits
                if str(e.get("time", ""))[:10] == today)
            live_open = sum(1 for p in self.positions.values()
                            if p.live and p.state != CLOSED)
            return {"open": open_n, "partial": part_n,
                    "live_open": live_open,
                    "closed_today": closed_today,
                    "realized_pnl_today_per_share_weighted":
                        round(pnl_today, 4),
                    "positions": [p.to_dict()
                                  for p in self.positions.values()]}


# ── self-test (no network) ───────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import tempfile
    from types import SimpleNamespace

    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    def leg(oi):
        return SimpleNamespace(oi=float(oi))

    def strike_row(k, ce_oi=0.0, pe_oi=0.0):
        return SimpleNamespace(strike=float(k), ce=leg(ce_oi), pe=leg(pe_oi))

    class FakeChain:
        def __init__(self, rows, lot=500):
            self.strikes = rows
            self.lot = lot

    class FakeHub:
        def __init__(self, df, chain):
            self.df = df
            self.chain = chain
            self.chain_calls = 0

        def candles_5m(self, symbol, days=7):
            return self.df

        def chain_snapshot(self, symbol, span=8):
            self.chain_calls += 1
            return self.chain

        def cash_quote(self, symbol):
            return {"lp": str(float(self.df["close"].iloc[-1]))}

        def segment_of(self, symbol):      # mirror DataHub (segment-aware entries)
            return "FNO"

    class FakeBook:
        def __init__(self):
            self.records = []

        def record(self, entry):
            self.records.append(dict(entry))

    def bars(start, specs):
        """specs: list of (open, high, low, close); 5m spacing from start."""
        recs, t = [], start
        for o, h, l, c in specs:
            recs.append({"time": t, "open": o, "high": h, "low": l,
                         "close": c, "volume": 1000.0, "oi": 0.0})
            t += timedelta(minutes=5)
        return pd.DataFrame(recs).set_index("time")

    def warmup(start, n, c0, step):
        """n bars, close drifts by `step`, TR pinned to 1.0 -> ATR = 1.0."""
        specs, c = [], c0
        for _ in range(n):
            o, cl = c, c + step
            hi, lo = (cl + 0.45, o - 0.45) if step >= 0 else \
                     (o + 0.45, cl - 0.45)
            specs.append((o, hi, lo, cl))
            c = cl
        return specs

    tmpdir = tempfile.mkdtemp(prefix="exits_selftest_")
    day = datetime(2026, 7, 3, 9, 15)          # all bars safely in the past

    # ═════ BUY case: entry -> ladder ratchet -> target1 partial ->
    #        supertrend trail -> profitable stop-out of the remainder ═════
    wu = warmup(day, 40, 96.0, 0.1)            # closes 96.1 .. 100.0, ATR=1
    df = bars(day, wu)
    entry_ts = df.index[-1]
    chain = FakeChain([
        strike_row(96, ce_oi=30_000, pe_oi=120_000),
        strike_row(98, ce_oi=40_000, pe_oi=500_000),    # protective put wall
        strike_row(100, ce_oi=150_000, pe_oi=200_000),
        strike_row(102, ce_oi=210_000, pe_oi=90_000),
        strike_row(103.5, ce_oi=480_000, pe_oi=20_000),  # opposing call wall
        strike_row(108, ce_oi=60_000, pe_oi=5_000),
    ])
    hub = FakeHub(df, chain)
    book = FakeBook()
    state_file = os.path.join(tmpdir, "buy_positions.json")
    eng = ExitEngine(hub, book, state_path=state_file)

    signal = {"symbol": "TESTBUY", "direction": BUY, "price": 100.0,
              "score": 70.0, "tree": {},
              "trigger": {"bar_time": entry_ts.isoformat()}}
    pos = eng.open_from_signal(signal)
    a0 = pos.plan["atr"]
    assert abs(a0 - 1.0) < 0.02, f"warmup ATR should be ~1.0, got {a0}"
    assert pos.wall_ref and pos.wall_ref["strike"] == 98.0, pos.wall_ref
    assert pos.target1 == 103.5 and abs(pos.target2 - 107.0) < 1e-6, \
        (pos.target1, pos.target2)
    # highest qualifying stop candidate is entry - 1.4*atr = 98.6
    assert abs(pos.stop - (100.0 - HARD_STOP_ATR * a0)) < 0.05, pos.stop
    assert pos.levels == [100.0, 102.0, 103.5], pos.levels
    assert pos.stop < 100.0 - STOP_MIN_GAP_ATR * a0 + 1e-9

    stops = [pos.stop]

    def push(o, h, l, c):
        add = bars(hub.df.index[-1] + timedelta(minutes=5), [(o, h, l, c)])
        hub.df = pd.concat([hub.df, add])
        eng.update_all()
        stops.append(pos.stop)

    # F1: crosses level 100 -> ladder ratchet
    push(100.0, 101.15, 99.9, 100.9)
    assert pos.state == OPEN and pos.stop > 98.7, pos.stop
    assert pos.stop < 100.0
    # F2: target1 hit (103.5) -> partial 0.5, breakeven, ladder to 103.5
    push(100.9, 104.1, 100.8, 103.9)
    assert pos.state == PARTIAL and abs(pos.qty_units - 0.5) < 1e-9
    assert pos.exits[0]["reason"] == "target1" and \
        abs(pos.exits[0]["units"] - PARTIAL_FRACTION) < 1e-9
    assert abs(pos.exits[0]["price"] - 103.5) < 1e-9
    assert pos.stop >= 100.0, "breakeven ratchet missing"
    stop_after_t1 = pos.stop
    # F3..: slow grind up, shrinking ranges -> ATR decays, supertrend line
    # climbs above the ladder stop; highs stay below target2 = 107
    grind = [(103.9, 104.35, 103.8, 104.2), (104.2, 104.75, 104.1, 104.6),
             (104.6, 105.15, 104.5, 105.0), (105.0, 105.55, 104.9, 105.4),
             (105.4, 105.95, 105.3, 105.8), (105.8, 106.25, 105.7, 106.1),
             (106.1, 106.45, 106.0, 106.3), (106.3, 106.60, 106.2, 106.5)]
    for o, h, l, c in grind:
        push(o, h, l, c)
    assert pos.state == PARTIAL, pos.state
    st_now = supertrend(hub.df)
    assert bool(st_now["bull"].iloc[-1])
    line_now = float(st_now["line"].iloc[-1])
    assert pos.stop >= line_now - 1e-9, \
        f"supertrend trail not applied: stop {pos.stop} < line {line_now}"
    assert pos.stop > stop_after_t1, "trail should have advanced the stop"
    assert pos.stop > pos.entry_price, "trail stop should be above entry"
    trail_stop = pos.stop
    # reversal bar: stop the remainder out above entry (profitable trail)
    push(106.5, 106.6, trail_stop - 0.8, trail_stop - 0.6)
    assert pos.state == CLOSED and pos.qty_units == 0.0
    assert pos.exits[-1]["reason"] == "stop"
    assert abs(pos.exits[-1]["price"] - trail_stop) < 1e-3   # 4-dp rounding
    assert pos.realized_pnl_per_share > 0, pos.realized_pnl_per_share
    # invariants
    assert all(b >= a - 1e-9 for a, b in zip(stops, stops[1:])), \
        f"BUY stop moved down: {stops}"
    # stop-history audit trail: non-empty, starts with the initial set,
    # and monotonic (BUY stops only ever move UP)
    sh = pos.stop_history
    assert sh, "stop_history must not be empty"
    assert sh[0]["reason"].startswith("initial:") and sh[0]["from"] is None
    assert abs(sh[0]["to"] - stops[0]) < 1e-6, (sh[0], stops[0])
    sh_tos = [h["to"] for h in sh]
    assert all(b >= a - 1e-9 for a, b in zip(sh_tos, sh_tos[1:])), \
        f"stop_history not monotonic: {sh_tos}"
    for h in sh[1:]:
        assert h["from"] is not None and h["to"] >= h["from"] - 1e-9, h
    sh_reasons = " | ".join(h["reason"] for h in sh)
    assert "oi_level" in sh_reasons and "t1_breakeven" in sh_reasons \
        and "supertrend" in sh_reasons, sh_reasons
    assert abs(sh[-1]["to"] - trail_stop) < 1e-3, (sh[-1], trail_stop)
    exits_rows = [r for r in book.records if r["type"] == "paper_exit"]
    assert len(exits_rows) == 2 and \
        {r["reason"] for r in exits_rows} == {"target1", "stop"}
    # json round-trip
    eng2 = ExitEngine(hub, FakeBook(), state_path=state_file)
    assert pos.id in eng2.positions
    assert eng2.positions[pos.id].to_dict() == pos.to_dict(), \
        "round-trip mismatch"
    s = eng.summary()
    assert s["open"] == 0 and s["partial"] == 0
    assert len(s["positions"]) == 1
    print(f"BUY case OK: pnl/share={pos.realized_pnl_per_share:+.3f} "
          f"exits={[ (e['reason'], e['price']) for e in pos.exits ]} "
          f"stop path={['%.2f' % x for x in stops]}")

    # ═════ SELL mirror smoke: entry -> wall-unwind tighten -> stop ═════
    wu = warmup(day, 40, 103.9, -0.1)          # closes 103.8 .. 100.0
    df_s = bars(day, wu)
    entry_ts_s = df_s.index[-1]
    chain_s = FakeChain([
        strike_row(94, ce_oi=5_000, pe_oi=50_000),
        strike_row(96, ce_oi=8_000, pe_oi=150_000),
        strike_row(96.5, ce_oi=10_000, pe_oi=480_000),  # opposing put wall
        strike_row(98, ce_oi=20_000, pe_oi=250_000),
        strike_row(100, ce_oi=140_000, pe_oi=160_000),
        strike_row(102, ce_oi=500_000, pe_oi=30_000),   # protective call wall
    ])
    hub_s = FakeHub(df_s, chain_s)
    book_s = FakeBook()
    eng_s = ExitEngine(hub_s, book_s,
                       state_path=os.path.join(tmpdir, "sell_positions.json"))
    pos_s = eng_s.open_from_signal(
        {"symbol": "TESTSELL", "direction": SELL, "price": 100.0,
         "trigger": {"bar_time": entry_ts_s.isoformat()}})
    assert pos_s.wall_ref and pos_s.wall_ref["strike"] == 102.0
    assert pos_s.target1 == 96.5 and abs(pos_s.target2 - 93.0) < 1e-6
    assert abs(pos_s.stop - 101.4) < 0.05, pos_s.stop      # entry + 1.4*atr
    assert pos_s.stop > 100.0
    sell_stops = [pos_s.stop]

    def push_s(o, h, l, c):
        add = bars(hub_s.df.index[-1] + timedelta(minutes=5), [(o, h, l, c)])
        hub_s.df = pd.concat([hub_s.df, add])
        eng_s.update_all()
        sell_stops.append(pos_s.stop)

    push_s(100.0, 100.45, 99.95, 100.2)        # quiet bar, nothing crossed
    push_s(100.2, 100.40, 100.0, 100.1)        # quiet bar
    # wall unwinds: call OI at 102 collapses to 40% of the entry snapshot
    chain_s.strikes[-1].ce.oi = 200_000.0
    push_s(100.1, 100.35, 99.95, 100.15)       # 15 min mark -> refetch+tighten
    assert pos_s.stop < 101.3, f"unwind tighten missing: {pos_s.stop}"
    assert pos_s.stop > pos_s.entry_price
    assert any("wall_unwind" in n for n in pos_s.plan["notes"]), \
        pos_s.plan["notes"]
    push_s(100.15, pos_s.stop + 0.3, 100.05, pos_s.stop + 0.2)  # stop hit
    assert pos_s.state == CLOSED and pos_s.exits[-1]["reason"] == "stop"
    assert all(b <= a + 1e-9 for a, b in zip(sell_stops, sell_stops[1:])), \
        f"SELL stop moved up: {sell_stops}"
    assert pos_s.realized_pnl_per_share < 0    # tightened stop, small loss
    assert len([r for r in book_s.records
                if r["type"] == "paper_exit"]) == 1
    # stop-history: initial + wall-unwind tighten; SELL stops only move DOWN
    ssh = pos_s.stop_history
    assert ssh and ssh[0]["reason"].startswith("initial:")
    assert any(h["reason"].startswith("wall_unwind") for h in ssh), ssh
    ssh_tos = [h["to"] for h in ssh]
    assert all(b <= a + 1e-9 for a, b in zip(ssh_tos, ssh_tos[1:])), ssh_tos
    print(f"SELL case OK: pnl/share={pos_s.realized_pnl_per_share:+.3f} "
          f"stop path={['%.2f' % x for x in sell_stops]} "
          f"notes={pos_s.plan['notes']}")

    # ═════ simulate_position (pure day-walk): entry -> T1 partial ->
    #        supertrend trail -> profitable stop-out of the remainder ═════
    wu = warmup(day, 40, 96.0, 0.1)            # entry bar 39 @ 100.0, ATR=1
    rally = [(100.0, 101.7, 99.9, 101.6)]      # crosses t1 = 101.5
    grind = [(101.6, 101.9, 101.5, 101.8),     # tight climb under t2 = 102.8
             (101.8, 102.1, 101.7, 102.0),     # -> ATR decays, supertrend
             (102.0, 102.25, 101.9, 102.15),   #    line rises above entry
             (102.15, 102.35, 102.05, 102.25),
             (102.25, 102.45, 102.15, 102.35),
             (102.35, 102.5, 102.25, 102.4),
             (102.4, 102.55, 102.3, 102.45),
             (102.45, 102.6, 102.35, 102.5),
             (102.5, 102.65, 102.4, 102.55),
             (102.55, 102.7, 102.45, 102.6),
             (102.6, 102.7, 102.5, 102.62),
             (102.62, 102.72, 102.52, 102.65)]
    df_pre = bars(day, wu + rally + grind)
    st_pre = supertrend(df_pre)
    assert bool(st_pre["bull"].iloc[-1])
    trail_line = float(st_pre["line"].iloc[-1])
    assert trail_line > 100.0, f"trail line should be above entry: {trail_line}"
    rev = [(102.65, 102.7, trail_line - 0.6, trail_line - 0.4)]
    df_sim = bars(day, wu + rally + grind + rev)

    res = simulate_position(df_sim, 39, BUY, 100.0)
    assert [e["reason"] for e in res["exits"]] == ["target1", "stop"], res
    assert abs(res["exits"][0]["units"] - PARTIAL_FRACTION) < 1e-9
    assert abs(res["exits"][0]["price"] - 101.5) < 0.02       # entry+1.5*atr
    assert res["exits"][1]["price"] > 100.0, \
        f"trail stop should be above entry: {res['exits'][1]}"
    assert abs(res["exits"][1]["price"] - trail_line) < 1e-3
    assert res["pnl_per_share"] > 0.75, res["pnl_per_share"]  # T1 leg + trail
    assert res["bars_held"] == 14, res["bars_held"]
    assert 2.6 < res["mfe"] < 2.8, res["mfe"]                 # 102.72 high
    assert 0.05 <= res["mae"] <= 0.15, res["mae"]             # 99.9 rally low
    # stop_moves audit trail mirrors the live engine's stop_history
    sm = res["stop_moves"]
    assert sm and sm[0]["reason"].startswith("initial:") \
        and sm[0]["from"] is None, sm
    sm_tos = [m["to"] for m in sm]
    assert all(b >= a - 1e-9 for a, b in zip(sm_tos, sm_tos[1:])), sm_tos
    assert any(m["reason"].startswith("t1_breakeven") for m in sm), sm
    assert any(m["reason"].startswith("supertrend") for m in sm), sm
    assert abs(sm_tos[-1] - trail_line) < 1e-3, (sm_tos[-1], trail_line)
    assert res["plan"]["stop"] == sm[0]["to"]
    assert abs(res["plan"]["target1"] - 101.5) < 0.02
    # degenerate: entry on the day's final bar -> immediate flat eod close
    res_eod = simulate_position(df_sim, len(df_sim) - 1, BUY,
                                float(df_sim["close"].iloc[-1]))
    assert [e["reason"] for e in res_eod["exits"]] == ["eod"]
    assert res_eod["pnl_per_share"] == 0.0 and res_eod["bars_held"] == 0
    print(f"simulate_position OK: pnl/share={res['pnl_per_share']:+.3f} "
          f"exits={[(e['reason'], e['price']) for e in res['exits']]} "
          f"mfe={res['mfe']:.2f} mae={res['mae']:.2f} "
          f"bars={res['bars_held']}")

    print("ALL EXIT-ENGINE SELF-TESTS PASSED")

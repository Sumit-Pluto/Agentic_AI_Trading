"""Intraday session loop — the single pipeline (backtest == paper == live).

One step() does, in order (mirrors swing_hyena/runtime/nightly but per-bar):
  1. manage open positions through the I0–I8 exit machine (square-off overrides)
  2. daily-loss kill: flatten + halt if the loss limit is breached
  3. if entries are allowed: scan → size → rule-gate → place → open position
  4. mark intraday equity

Data comes through an IntradayContext; orders through an OrderManager (which
gates via the RuleEngine and stamps <5s latency); everything is journalled. A
bad single position never kills the step (each is try/except-guarded).
"""
from __future__ import annotations

import datetime as dt
import re
import time
from collections import deque

from ..agents._ta import atr, last
from ..brokers import PaperBroker
from ..contracts import Brain, OrderIntent, Position
from ..exits import ExitMarket, manage
from ..intelligence import Scanner
from ..model import ModelFilter
from ..options.models import IST, market_session
from ..orders import OrderManager
from ..risk import Governor
from ..rules import RuleEngine

# NSE option tsym: <UNDERLYING><DD><MON><YY><C|P><STRIKE>, e.g. NIFTY29SEP26C24800
_TSYM_OPT_RE = re.compile(r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})([CP])(\d+(?:\.\d+)?)$")


class SessionLoop:
    def __init__(self, ctx, store, cfg: dict, *, brain: Brain | None = None,
                 broker=None, killswitch=None):
        self.ctx = ctx
        self.store = store
        self.cfg = cfg or {}
        self.brain = brain or Brain.equal()
        self.scanner = Scanner(ctx, self.brain, self.cfg)
        self.governor = Governor(self.cfg)
        self.rules = RuleEngine(self.cfg)
        self.model_filter = ModelFilter.maybe(self.cfg)   # trained-model P(win) gate (optional)
        self.broker = broker or PaperBroker(self.cfg.get("slippage_pct", 0.10))
        self.orders = OrderManager(self.broker, store, self.rules, self.cfg)
        self.killswitch = killswitch          # optional callable -> bool (paused)
        self.positions: list[Position] = []
        self.equity_curve: list[float] = []
        self.halted = False
        self.equity = self.governor.budget()          # capital base = total_budget
        self.funds_provider = None                    # set by the runner in LIVE mode -> /api/funds dict
        self.last_scan: dict = {}      # last scan for the UI (regime, signals, agent rows)
        self.activity: deque = deque(maxlen=200)  # background-processing feed for the UI
        self._last_scan_msg = ""   # dedupe: identical scan lines are logged once…
        self._last_scan_log = 0.0  # …plus a 60 s heartbeat while unchanged
        self._flatten_request = False  # kill-switch / stop-square-off (serviced on the engine thread)
        self._flatten_reason = ""
        self._day: dt.date | None = None       # session rollover tracker
        self._last_scan_at: dt.datetime | None = None   # scan-cadence gate

    def _log(self, stage: str, msg: str) -> None:
        """Append one background-activity event (ring buffer, never raises)."""
        try:
            self.activity.append({"ts": self.ctx.now().isoformat(),
                                  "stage": stage, "msg": msg})
        except Exception:
            pass

    def set_broker(self, broker) -> None:
        """Swap the execution broker (PAPER<->LIVE) at runtime; rebuilds the
        order manager around it. Called by the runner on a mode toggle."""
        self.broker = broker
        self.orders = OrderManager(broker, self.store, self.rules, self.cfg)

    # ---------- helpers ----------
    def _paused(self) -> bool:
        try:
            return bool(self.killswitch()) if self.killswitch else bool(self.cfg.get("paused", False))
        except Exception:
            return False

    def _leg_quote(self, underlying: str, strike: float, is_call: bool):
        try:
            ch = self.ctx.chain(underlying)
        except Exception:
            return None, None
        q = ch.get(strike, is_call) if ch else None
        return q, ch

    def _atr_pts(self, underlying: str, spot: float) -> float:
        try:
            b = self.ctx.bars(underlying)
            a = last(atr(b)) if b is not None else float("nan")
        except Exception:
            a = float("nan")
        mult = float(self.cfg.get("atr_stop_mult", 1.5))
        base = a if a == a and a > 0 else max(spot * 0.002, 1.0)
        return mult * base

    def _entries_allowed(self, now: dt.datetime) -> bool:
        if self.halted or self._paused():
            return False
        return market_session(now)["open"] and not self.ctx.is_square_off(now)

    # ---------- one iteration ----------
    def request_flatten(self, reason: str = "flatten", halt: bool = False) -> None:
        """Thread-safe request to flatten all positions on the next engine tick
        (kill switch / stop-with-square-off). Serviced on the engine thread to
        avoid racing the loop's own position mutations."""
        self._flatten_request = True
        self._flatten_reason = reason
        if halt:
            self.halted = True

    def step(self, now: dt.datetime | None = None) -> dict:
        now = now or self.ctx.now()
        self.equity = self.governor.budget()          # pick up live budget edits
        # session rollover: a long-running server must not carry yesterday's
        # halt/drawdown into today (positions are squared off daily anyway).
        today = now.date()
        if self._day is None:
            self._day = today
        elif today != self._day:
            self._day = today
            self.halted = False
            self.equity_curve = []
            self._log("session", f"new trading day {today.isoformat()} — halt cleared")
        if self._flatten_request:
            self._flatten_all(now, self._flatten_reason or "flatten")
            self._flatten_request = False

        # 1. manage exits
        square_off = self.ctx.is_square_off(now)
        still_open: list[Position] = []
        n_before = len(self.positions)
        for pos in self.positions:
            try:
                if self._manage_one(pos, now, square_off):
                    continue                    # closed fully
                still_open.append(pos)
            except Exception as e:
                pos.log.append(f"manage error: {e}")
                still_open.append(pos)
        self.positions = still_open
        if n_before - len(still_open):
            self._log("exits", f"{n_before - len(still_open)} closed · "
                               f"{len(still_open)} still open")

        # 2. unrealized + daily-loss kill
        unrealized = self._unrealized(now)
        realized = self.store.realized_pnl_today(now.date())
        if not self.halted and self.governor.daily_loss_breached(realized, unrealized):
            self._flatten_all(now, "daily-loss kill")
            self.halted = True
            self._log("halt", "daily-loss kill — flattened everything")

        # 3. scan at cadence (exits/kill run every tick; the full agent tree
        #    is expensive, so live ticks reuse the last scan between cadences).
        #    Sim/backtest clocks jump a bar per step, so they scan every tick.
        do_scan = True
        try:
            gap = float(self.cfg.get("scan_every_seconds", 30) or 0)
        except (TypeError, ValueError):
            gap = 30.0
        if gap > 0 and self._last_scan_at is not None:
            try:
                do_scan = (now - self._last_scan_at).total_seconds() >= gap
            except Exception:
                do_scan = True
        signals: list = []
        regime = ((self.last_scan or {}).get("regime") or {"on": False})
        if do_scan:
            try:
                signals, regime, rows = self.scanner.scan()
                self._last_scan_at = now
                views = [self._signal_view(s) for s in signals]
                self.last_scan = {"ts": now.isoformat(), "regime": regime,
                                  "rows": rows, "signals": views}
                prog = getattr(self.scanner, "progress", []) or []
                n_bars = sum(1 for p in prog if p.get("bars"))
                n_chains = sum(1 for p in prog if p.get("chain"))
                desc = ", ".join(
                    f"{v['symbol']} {v['direction']} "
                    f"{(v.get('instrument') or {}).get('kind', '?')}" for v in views)
                def _n(n: int, word: str) -> str:
                    return f"{n} {word}" if n == 1 else f"{n} {word}s"
                msg = (f"regime {'ON' if regime.get('on') else 'OFF'}"
                       f" ({regime.get('detail') or 'no detail'}) · "
                       f"{_n(len(prog), 'symbol')} ({_n(n_bars, 'bar')}, "
                       f"{_n(n_chains, 'chain')}) · "
                       f"{_n(len(views), 'signal')}" + (f": {desc}" if desc else ""))
                if msg != self._last_scan_msg or time.time() - self._last_scan_log > 60:
                    self._log("scan", msg)
                    self._last_scan_msg, self._last_scan_log = msg, time.time()
            except Exception as e:
                signals, regime = [], ((self.last_scan or {}).get("regime")) or {}
                self._log("scan", f"failed: {type(e).__name__}: {e}")
        fired = 0
        if self._entries_allowed(now):
            for sig in signals:
                try:
                    if self._enter(sig, regime, now):
                        fired += 1
                        inst = sig.instrument or {}
                        self._log("entry",
                                  f"{sig.symbol} {sig.direction} "
                                  f"{inst.get('kind', '?')} {inst.get('strike', '')}")
                except Exception:
                    continue

        # 4. mark equity
        unrealized = self._unrealized(now)
        equity = self.equity + realized + unrealized
        self.equity_curve.append(equity)
        open_risk = sum(p.risk_per_share * p.qty for p in self.positions)
        try:
            reg_on = True
            self.store.mark_equity(now.timestamp(), now.date(), equity, self.equity,
                                   open_risk, realized, unrealized, len(self.positions),
                                   reg_on, 1.0)
        except Exception:
            pass
        return {"ts": now.isoformat(), "positions": len(self.positions),
                "realized": realized, "unrealized": unrealized, "equity": equity,
                "entries": fired, "halted": self.halted, "square_off": square_off}

    # ---------- exit management ----------
    def _manage_one(self, pos: Position, now: dt.datetime, square_off: bool) -> bool:
        if str(getattr(pos, "right", "")).upper() == "FUT":
            mark = self._fut_mark(pos.underlying, pos.entry_px)
            spot = mark
            bid = ask = mid = mark
        else:
            is_call = str(pos.right).upper().startswith("C")
            q, ch = self._leg_quote(pos.underlying, pos.strike, is_call)
            spot = ch.spot if ch else pos.entry_spot
            bid = q.bid if q else 0.0
            ask = q.ask if q else 0.0
            mid = q.mid if q else pos.entry_px
        vwap = None
        try:
            b = self.ctx.bars(pos.underlying)
            vwap = float(b["vwap"].iloc[-1]) if b is not None and "vwap" in b else None
        except Exception:
            vwap = None
        mkt = ExitMarket(now=now, spot=spot, leg_bid=bid, leg_ask=ask, leg_mid=mid,
                         bars=(self.ctx.bars(pos.underlying) if hasattr(self.ctx, "bars") else None),
                         vwap=vwap, is_square_off=square_off,
                         bars_elapsed=self._bars_since_managed(pos, now))
        decision = manage(pos, mkt, self.cfg)
        if decision.action == "HOLD":
            self.store.update_position(pos)
            return False
        exit_px = decision.exit_px or bid or mid or pos.entry_px
        if decision.action == "PARTIAL":
            lot = pos.lot_size or 1
            book_lots = int((pos.qty // lot) * decision.qty_frac)
            book_qty = book_lots * lot
            if book_qty > 0:
                res = self._exit_order(pos, book_qty, exit_px, now, decision.reason)
                if res.status == "FILLED":
                    self.store.book_partial(pos, res.fill_px or exit_px, book_qty,
                                            decision.reason, now, self.brain.version,
                                            cost_per_share=self._cost_per_share(pos))
                else:
                    # Not booked and not confirmed: re-offer the partial next
                    # tick (the stop already sits at breakeven — the safe side).
                    pos.partial_done = False
                    pos.log.append(f"partial failed ({res.status}: {res.reason})")
                    self._log("exits", f"{pos.symbol} partial failed ({res.status}) — retrying")
            self.store.update_position(pos)
            return False
        # full EXIT — journal the close ONLY on a confirmed fill, at the real
        # fill price; otherwise the position stays managed and retries.
        res = self._exit_order(pos, pos.qty, exit_px, now, decision.reason)
        if res.status != "FILLED":
            pos.log.append(f"exit failed ({res.status}: {res.reason}) — holding for retry")
            self._log("exits", f"{pos.symbol} exit failed ({res.status}) — retrying")
            self.store.update_position(pos)
            return False
        pos.exit_px = res.fill_px or exit_px
        pos.exit_ts = now
        pos.exit_reason = decision.reason
        self.store.close_position(pos, self.brain.version,
                                  cost_per_share=self._cost_per_share(pos))
        return True

    def _cost_per_share(self, pos: Position) -> float:
        """Configured round-trip costs (brokerage/STT/...) per share."""
        try:
            per_lot = float(self.cfg.get("roundtrip_cost_per_lot", 0.0) or 0.0)
        except (TypeError, ValueError):
            per_lot = 0.0
        if per_lot <= 0:
            return 0.0
        return per_lot / max(int(getattr(pos, "lot_size", 0) or 0), 1)

    def _exit_order(self, pos: Position, qty: int, px: float, now: dt.datetime, reason: str):
        side = "SELL" if pos.is_long else "BUY"      # close a long by selling
        intent = OrderIntent(symbol=pos.symbol, side=side, qty=qty,
                             order_type="MARKETABLE_LIMIT", limit_px=px,
                             reason=f"exit:{reason}"[:60], signal_ts=time.time(),
                             exch=pos.exch, token=pos.token, underlying=pos.underlying,
                             strike=pos.strike, right=pos.right, expiry=pos.expiry,
                             lot_size=pos.lot_size, strategy=pos.strategy)
        return self.orders.submit(intent, now, self.positions, is_exit=True,
                                  halted=self.halted, paused=False)

    def _flatten_all(self, now: dt.datetime, reason: str):
        still: list[Position] = []
        for pos in list(self.positions):
            if str(getattr(pos, "right", "")).upper() == "FUT":
                px = self._fut_mark(pos.underlying, pos.entry_px) or pos.entry_px
            else:
                is_call = str(pos.right).upper().startswith("C")
                q, ch = self._leg_quote(pos.underlying, pos.strike, is_call)
                px = (q.bid if q and pos.is_long else (q.ask if q else pos.entry_px)) or pos.entry_px
            res = self._exit_order(pos, pos.qty, px, now, reason)
            if res.status != "FILLED":
                pos.log.append(f"flatten failed ({res.status}: {res.reason}) — keeping")
                still.append(pos)
                continue
            pos.exit_px, pos.exit_ts, pos.exit_reason = (res.fill_px or px), now, reason
            self.store.close_position(pos, self.brain.version,
                                      cost_per_share=self._cost_per_share(pos))
        self.positions = still
        if still:
            self._log("exits", f"flatten incomplete: {len(still)} position(s) "
                               "failed to exit — retrying next tick")

    def _unrealized(self, now: dt.datetime) -> float:
        tot = 0.0
        for pos in self.positions:
            if str(getattr(pos, "right", "")).upper() == "FUT":
                mark = self._fut_mark(pos.underlying, pos.entry_px)
                if not mark:
                    continue
                sign = 1.0 if pos.is_long else -1.0
                tot += sign * (mark - pos.entry_px) * pos.qty
                continue
            is_call = str(pos.right).upper().startswith("C")
            q, _ = self._leg_quote(pos.underlying, pos.strike, is_call)
            if not q:
                continue
            mark = q.bid if pos.is_long else q.ask
            sign = 1.0 if pos.is_long else -1.0
            tot += sign * ((mark or q.mid) - pos.entry_px) * pos.qty
        return tot

    # ---------- entries ----------
    @staticmethod
    def _signal_view(s) -> dict:
        return {"ts": s.ts.isoformat(), "symbol": s.symbol, "direction": s.direction,
                "score_buy": round(s.score_buy, 1), "score_sell": round(s.score_sell, 1),
                "family_scores": {k: round(v, 1) for k, v in (s.family_scores or {}).items()},
                "instrument": s.instrument}

    def _bars_since_managed(self, pos: Position, now: dt.datetime) -> float:
        """Wall-clock bars held since entry, minus what is already aged — so
        age_bars is in BARS at any engine cadence (a 2 s tick ages ~0.007 of a
        5 m bar, a sim step ages exactly one). Falls back to one bar when the
        clock is unusable, the historical per-call behaviour."""
        try:
            tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(
                str(self.cfg.get("bar_timeframe", "5m")), 5)
            elapsed = (now - pos.entry_ts).total_seconds() / 60.0 / max(tf_min, 1)
        except Exception:
            return 1.0
        if not (elapsed == elapsed) or elapsed < 0:
            return 1.0
        return max(0.0, elapsed - (pos.age_bars or 0.0))

    # ---------- restart recovery + broker reconciliation ----------
    @staticmethod
    def _position_from_row(r) -> Position:
        ts = r["entry_ts"]
        try:
            entry_ts = dt.datetime.fromisoformat(ts) if ts else dt.datetime.now(IST)
        except (TypeError, ValueError):
            entry_ts = dt.datetime.now(IST)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=IST)
        exp = r["expiry"]
        try:
            expiry = dt.date.fromisoformat(exp) if exp else None
        except (TypeError, ValueError):
            expiry = None
        return Position(
            symbol=r["symbol"], qty=int(r["qty"] or 0), side=r["side"] or "BUY",
            entry_px=float(r["entry_px"] or 0), entry_ts=entry_ts,
            stop=float(r["stop"] or 0),
            risk_per_share=float(r["risk_per_share"] or 0),
            strike=float(r["strike"] or 0), right=r["right"] or "",
            expiry=expiry, lot_size=int(r["lot_size"] or 0),
            exch=r["exch"] or "NFO", token=r["token"] or "",
            underlying=r["underlying"] or "",
            entry_spot=float(r["entry_spot"] or 0),
            delta_at_entry=float(r["delta_at_entry"] or 0),
            age_bars=r["age_bars"] or 0, max_prem=float(r["max_prem"] or 0),
            max_fav_spot=float(r["entry_spot"] or 0),
            breakeven_done=bool(r["breakeven"]), partial_done=bool(r["partial"]),
            strategy=r["strategy"] or "default", position_id=r["id"])

    def restore_open_positions(self) -> list[Position]:
        """Rebuild in-memory tracking from journal OPEN rows (restart recovery).
        Without this a restart mid-day orphans live positions: no exits, no
        stops, and the dedup/exposure gates go blind. Idempotent; never raises."""
        try:
            rows = self.store.open_positions()
        except Exception:
            return []
        have = {p.position_id for p in self.positions if p.position_id}
        out: list[Position] = []
        for r in rows or []:
            try:
                if r["id"] in have:
                    continue
                pos = self._position_from_row(r)
            except Exception:
                continue
            if pos.qty <= 0:
                continue
            self.positions.append(pos)
            out.append(pos)
        if out:
            self._log("restore", f"tracking restored for {len(out)} open position(s)")
        return out

    def _reconcile_mark(self, pos: Position) -> float:
        if str(getattr(pos, "right", "")).upper() == "FUT":
            return self._fut_mark(pos.underlying, pos.entry_px) or pos.entry_px
        try:
            is_call = str(pos.right).upper().startswith("C")
            q, _ch = self._leg_quote(pos.underlying, pos.strike, is_call)
        except Exception:
            q = None
        if q:
            return (q.bid if pos.is_long else q.ask) or q.mid or pos.entry_px
        return pos.entry_px

    def _adopt_broker_row(self, tsym: str, side: str, qty: int, row: dict,
                          now: dt.datetime) -> Position | None:
        """Build a tracked Position for a broker-book row the journal never saw
        (manual trade, or an order the engine placed before a crash). Stops are
        never invented for options (stop=0: I1b premium stop + I0 still apply);
        a FUT stop comes from current ATR. Never raises."""
        try:
            avg = float(row.get("buyavgprc") or 0) if side == "BUY" else \
                float(row.get("sellavgprc") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if avg <= 0:
            try:
                avg = float(row.get("lp") or 0)
            except (TypeError, ValueError):
                avg = 0.0
        underlying, right, strike, expiry = "", "", 0.0, None
        m = _TSYM_OPT_RE.match(tsym.upper())
        if m:
            underlying, exp_s, rc, k = m.group(1), m.group(2), m.group(3), m.group(4)
            right = "CE" if rc == "C" else "PE"
            try:
                strike = float(k)
            except ValueError:
                strike = 0.0
            try:
                expiry = dt.datetime.strptime(exp_s, "%d%b%y").date()
            except ValueError:
                expiry = None
        elif tsym.upper().endswith("-FUT"):
            underlying, right = tsym[:-4], "FUT"
        try:
            lot = int(float(row.get("lotsize") or 0))
        except (TypeError, ValueError):
            lot = 0
        try:
            spot_now = float(self.ctx.spot(underlying)) if underlying else 0.0
        except Exception:
            spot_now = 0.0
        entry = avg if avg > 0 else (self._fut_mark(underlying, 0.0) if right == "FUT"
                                     else (spot_now or 0.0))
        if entry <= 0:
            return None                        # no price anchor — cannot track
        if right == "FUT":
            sl_pts = self._atr_pts(underlying, entry) if underlying else max(entry * 0.002, 1.0)
            rps = max(sl_pts, 0.01)
            stop = entry - sl_pts if side == "BUY" else entry + sl_pts
        else:
            try:
                frac = float(self.cfg.get("max_prem_loss_pct", 40.0)) / 100.0
            except (TypeError, ValueError):
                frac = 0.4
            rps = max(entry * frac, 0.05)
            stop = 0.0                         # never invent an option stop
        pos = Position(symbol=tsym, qty=qty, side=side, entry_px=entry,
                       entry_ts=now, stop=stop, risk_per_share=rps,
                       strike=strike, right=right, expiry=expiry, lot_size=lot,
                       exch=str(row.get("exch") or "NFO"),
                       token=str(row.get("token") or ""), underlying=underlying,
                       entry_spot=(spot_now or entry), delta_at_entry=0.0,
                       max_prem=entry, max_fav_spot=(spot_now or entry),
                       strategy="adopted")
        pos.log.append(f"adopted from broker book at {now.isoformat()}")
        return pos

    def reconcile_with_broker(self, broker_positions: list[dict],
                              now: dt.datetime | None = None) -> dict:
        """LIVE startup reconciliation: journal OPEN rows vs the broker book.

        • journal OPEN but flat/absent at the broker → journal-close as
          externally closed (no exit order: there is nothing to exit — sending
          one would open a reversed position);
        • broker net with no journal row → adopt as tracked (loud log);
        • qty mismatch → the broker is truth (exits must match live size).
        Non-MIS rows are ignored (another product owns them). Never raises."""
        now = now or self.ctx.now()
        summary = {"matched": 0, "qty_adjusted": 0, "closed_ghosts": 0,
                   "adopted": 0, "ignored_non_mis": 0}
        try:
            book: dict[tuple[str, str], tuple[dict, int]] = {}
            for r in broker_positions or []:
                if not isinstance(r, dict):
                    continue
                prd = str(r.get("prd") or r.get("s_prdt_ali") or "").upper()
                if prd and prd != "I":
                    summary["ignored_non_mis"] += 1
                    continue
                try:
                    net = int(float(r.get("netqty") or 0))
                except (TypeError, ValueError):
                    continue
                if net == 0:
                    continue
                tsym = str(r.get("tsym") or "")
                if not tsym:
                    continue
                book[(tsym, "BUY" if net > 0 else "SELL")] = (r, net)
            still: list[Position] = []
            for pos in self.positions:
                key = (pos.symbol, str(pos.side).upper())
                if key not in book:
                    pos.exit_px = self._reconcile_mark(pos)
                    pos.exit_ts, pos.exit_reason = now, "reconcile: flat at broker"
                    self.store.close_position(pos, self.brain.version)
                    summary["closed_ghosts"] += 1
                    self._log("reconcile", f"{pos.symbol} flat at broker — journal-closed")
                    continue
                r, net = book.pop(key)
                if abs(net) != pos.qty:
                    pos.qty = abs(net)
                    self.store.update_position(pos)
                    summary["qty_adjusted"] += 1
                    self._log("reconcile", f"{pos.symbol} qty → {pos.qty} (broker truth)")
                summary["matched"] += 1
                still.append(pos)
            self.positions = still
            for (tsym, side), (r, net) in book.items():
                try:
                    pos = self._adopt_broker_row(tsym, side, abs(net), r, now)
                except Exception:
                    pos = None
                if pos is None:
                    self._log("reconcile", f"{tsym} has broker net {net} but no price "
                                           "anchor — NOT tracked, resolve manually")
                    continue
                pos.position_id = self.store.open_position(pos)
                self.positions.append(pos)
                summary["adopted"] += 1
                self._log("reconcile", f"adopted {tsym} {side} x{abs(net)} from broker book")
        except Exception as e:
            self._log("reconcile", f"failed: {type(e).__name__}: {e}")
        return summary

    def _fut_mark(self, symbol: str, fallback: float = 0.0) -> float:
        """Current futures/underlying mark for FUT legs (live quote → spot)."""
        try:
            fn = getattr(self.ctx, "future", None)
            fq = fn(symbol) if callable(fn) else None
            px = float((fq or {}).get("px") or 0.0)
            if px > 0:
                return px
        except Exception:
            pass
        try:
            px = float(self.ctx.spot(symbol) or 0.0)
            if px > 0:
                return px
        except Exception:
            pass
        return fallback

    def _enter(self, sig, regime: dict, now: dt.datetime) -> bool:
        inst = sig.instrument or {}
        kind = str(inst.get("kind", "OPT")).upper()
        is_call = str(inst.get("right", "")).upper().startswith("C")
        ch = None
        try:
            ch = self.ctx.chain(sig.symbol)
        except Exception:
            ch = None
        if kind != "FUT" and ch is None:
            return False

        # trained-model probability filter (optional): the model can VETO or
        # scale, never invent a trade. Disabled filter / unscorable row → pass.
        if self.model_filter.enabled:
            try:
                idx = getattr(self.ctx, "index_symbol", None) or self.cfg.get("index_symbol", "NIFTY")
                ibars = self.ctx.bars(idx)
            except Exception:
                ibars = None
            try:
                vix = self.ctx.vix()
            except Exception:
                vix = None
            prob = self.model_filter.win_prob(sig, ch, self.ctx.bars(sig.symbol), ibars, vix, now)
            if sig.instrument is not None and prob is not None:
                sig.instrument["win_prob"] = round(prob, 3)
            if not self.model_filter.passes(prob):
                return False

        if kind == "FUT":
            spot = self._fut_mark(sig.symbol, float(inst.get("entry_prem") or 0.0))
            if spot <= 0:
                return False
        else:
            spot = ch.spot
        sl_pts = self._atr_pts(sig.symbol, spot)
        size = self.governor.size(instrument=inst, chain=ch, sl_pts=sl_pts,
                                  equity=self.equity, regime_scalar=regime.get("scalar", 1.0),
                                  open_positions=self.positions,
                                  equity_curve=self.equity_curve)
        if not size.ok:
            return False

        # budget hard-cap: refuse a new entry that would over-deploy the budget
        # (options: premium outlay; futures: 1x stop-risk notional as the cap
        # numerator so a FUT leg cannot bypass the utilisation guard).
        entry_prem = float(inst.get("ask") or inst.get("entry_prem") or 0.0)
        cap_extra = entry_prem * size.qty if kind != "FUT" else size.risk_per_share * size.qty
        ok_cap, _why = self.governor.can_open_new(self.positions, extra_premium=cap_extra)
        if not ok_cap:
            return False
        # shared-account margin gate (LIVE only): never trip a broker square-off
        if str(self.cfg.get("mode", "paper")).lower() == "live" and self.funds_provider:
            try:
                funds = self.funds_provider()
            except Exception:
                funds = None
            safety = float(self.cfg.get("margin_safety_factor", 1.10))
            if funds and not self.governor.margin_ok(funds, cap_extra * safety):
                return False

        if kind == "FUT":
            expiry = ch.expiry if ch is not None else None
            lot = int(inst.get("lot_size") or (ch.lot_size if ch is not None else 0) or 0)
        else:
            expiry, lot = ch.expiry, inst.get("lot_size", ch.lot_size)
        # Options express direction through the leg (a SELL view buys a put),
        # so they are always BUY; futures are linear and take the signal side.
        side = ("BUY" if sig.direction == "BUY" else "SELL") if kind == "FUT" else "BUY"
        sig_ts = getattr(sig, "ts", None)
        signal_ts = (sig_ts.timestamp() if isinstance(sig_ts, dt.datetime)
                     else time.time())
        intent = OrderIntent(symbol=inst["tsym"], side=side, qty=size.qty,
                             order_type="MARKETABLE_LIMIT", limit_px=inst.get("ask") or inst.get("entry_prem"),
                             reason=f"entry:{sig.symbol} {sig.direction}"[:60],
                             signal_id=None, signal_ts=signal_ts, exch=inst.get("exch", "NFO"),
                             token=inst.get("token", ""), underlying=sig.symbol,
                             strike=float(inst.get("strike") or 0.0), right=inst.get("right", ""),
                             expiry=expiry, lot_size=lot,
                             strategy=sig.instrument.get("strategy", "default") if sig.instrument else "default")
        sid = self.store.save_signal(sig)
        intent.signal_id = sid
        res = self.orders.submit(intent, now, self.positions, is_exit=False,
                                 halted=self.halted, paused=self._paused())
        if res.status != "FILLED" or not res.fill_px:
            return False
        # A live partial fill tracks only the filled shares (never ghost qty).
        fill_qty = int(res.filled_qty or 0)
        if fill_qty <= 0:
            fill_qty = size.qty
        if kind == "FUT":
            stop = spot - sl_pts if sig.direction == "BUY" else spot + sl_pts
        else:
            stop = spot - sl_pts if is_call else spot + sl_pts
        pos = Position(symbol=inst["tsym"], qty=fill_qty, side=side, entry_px=res.fill_px,
                       entry_ts=now, stop=stop, risk_per_share=size.risk_per_share,
                       strike=float(inst.get("strike") or 0.0), right=inst.get("right", ""),
                       expiry=expiry,
                       lot_size=lot, exch=inst.get("exch", "NFO"),
                       token=inst.get("token", ""), underlying=sig.symbol, entry_spot=spot,
                       delta_at_entry=float(inst.get("delta") or (1.0 if kind == "FUT" else 0.0)),
                       max_prem=res.fill_px,
                       max_fav_spot=spot, strategy=intent.strategy)
        pos.position_id = self.store.open_position(pos)
        self.store.mark_signal_acted(sid)
        self.positions.append(pos)
        return True

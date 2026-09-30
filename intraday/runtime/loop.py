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
import time

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
        self._flatten_request = False  # kill-switch / stop-square-off (serviced on the engine thread)
        self._flatten_reason = ""

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
        if self._flatten_request:
            self._flatten_all(now, self._flatten_reason or "flatten")
            self._flatten_request = False
        realized_before = self.store.realized_pnl_today(now.date())

        # 1. manage exits
        square_off = self.ctx.is_square_off(now)
        still_open: list[Position] = []
        for pos in self.positions:
            try:
                if self._manage_one(pos, now, square_off):
                    continue                    # closed fully
                still_open.append(pos)
            except Exception as e:
                pos.log.append(f"manage error: {e}")
                still_open.append(pos)
        self.positions = still_open

        # 2. unrealized + daily-loss kill
        unrealized = self._unrealized(now)
        realized = self.store.realized_pnl_today(now.date())
        if not self.halted and self.governor.daily_loss_breached(realized, unrealized):
            self._flatten_all(now, "daily-loss kill")
            self.halted = True

        # 3. scan every tick (for the live UI), act only when entries are allowed
        try:
            signals, regime, rows = self.scanner.scan()
            self.last_scan = {"ts": now.isoformat(), "regime": regime,
                              "rows": rows,
                              "signals": [self._signal_view(s) for s in signals]}
        except Exception:
            signals, regime = [], (self.last_scan.get("regime") if self.last_scan else {}) or {}
        fired = 0
        if self._entries_allowed(now):
            for sig in signals:
                try:
                    if self._enter(sig, regime, now):
                        fired += 1
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
                         vwap=vwap, is_square_off=square_off)
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
                self._exit_order(pos, book_qty, exit_px, now, decision.reason)
                self.store.book_partial(pos, exit_px, book_qty, decision.reason, now,
                                        self.brain.version)
            self.store.update_position(pos)
            return False
        # full EXIT
        self._exit_order(pos, pos.qty, exit_px, now, decision.reason)
        pos.exit_px = exit_px
        pos.exit_ts = now
        pos.exit_reason = decision.reason
        self.store.close_position(pos, self.brain.version)
        return True

    def _exit_order(self, pos: Position, qty: int, px: float, now: dt.datetime, reason: str):
        side = "SELL" if pos.is_long else "BUY"      # close a long by selling
        intent = OrderIntent(symbol=pos.symbol, side=side, qty=qty,
                             order_type="MARKETABLE_LIMIT", limit_px=px,
                             reason=f"exit:{reason}"[:60], signal_ts=time.time(),
                             exch=pos.exch, token=pos.token, underlying=pos.underlying,
                             strike=pos.strike, right=pos.right, expiry=pos.expiry,
                             lot_size=pos.lot_size, strategy=pos.strategy)
        self.orders.submit(intent, now, self.positions, is_exit=True,
                           halted=self.halted, paused=False)

    def _flatten_all(self, now: dt.datetime, reason: str):
        for pos in list(self.positions):
            is_call = str(pos.right).upper().startswith("C")
            q, ch = self._leg_quote(pos.underlying, pos.strike, is_call)
            px = (q.bid if q and pos.is_long else (q.ask if q else pos.entry_px)) or pos.entry_px
            self._exit_order(pos, pos.qty, px, now, reason)
            pos.exit_px, pos.exit_ts, pos.exit_reason = px, now, reason
            self.store.close_position(pos, self.brain.version)
        self.positions = []

    def _unrealized(self, now: dt.datetime) -> float:
        tot = 0.0
        for pos in self.positions:
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

    def _enter(self, sig, regime: dict, now: dt.datetime) -> bool:
        inst = sig.instrument or {}
        is_call = str(inst.get("right", "")).upper().startswith("C")
        ch = None
        try:
            ch = self.ctx.chain(sig.symbol)
        except Exception:
            return False
        if ch is None:
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

        spot = ch.spot
        sl_pts = self._atr_pts(sig.symbol, spot)
        size = self.governor.size(instrument=inst, chain=ch, sl_pts=sl_pts,
                                  equity=self.equity, regime_scalar=regime.get("scalar", 1.0),
                                  open_positions=self.positions,
                                  equity_curve=self.equity_curve)
        if not size.ok:
            return False

        # budget hard-cap: refuse a new entry that would over-deploy the budget
        entry_prem = float(inst.get("ask") or inst.get("entry_prem") or 0.0)
        ok_cap, _why = self.governor.can_open_new(self.positions, extra_premium=entry_prem * size.qty)
        if not ok_cap:
            return False
        # shared-account margin gate (LIVE only): never trip a broker square-off
        if str(self.cfg.get("mode", "paper")).lower() == "live" and self.funds_provider:
            try:
                funds = self.funds_provider()
            except Exception:
                funds = None
            safety = float(self.cfg.get("margin_safety_factor", 1.10))
            if funds and not self.governor.margin_ok(funds, entry_prem * size.qty * safety):
                return False

        intent = OrderIntent(symbol=inst["tsym"], side="BUY", qty=size.qty,
                             order_type="MARKETABLE_LIMIT", limit_px=inst.get("ask") or inst.get("entry_prem"),
                             reason=f"entry:{sig.symbol} {sig.direction}"[:60],
                             signal_id=None, signal_ts=time.time(), exch=inst.get("exch", "NFO"),
                             token=inst.get("token", ""), underlying=sig.symbol,
                             strike=inst["strike"], right=inst["right"],
                             expiry=(ch.expiry), lot_size=inst.get("lot_size", ch.lot_size),
                             strategy=sig.instrument.get("strategy", "default") if sig.instrument else "default")
        sid = self.store.save_signal(sig)
        intent.signal_id = sid
        res = self.orders.submit(intent, now, self.positions, is_exit=False,
                                 halted=self.halted, paused=self._paused())
        if res.status != "FILLED" or not res.fill_px:
            return False
        stop = spot - sl_pts if is_call else spot + sl_pts
        pos = Position(symbol=inst["tsym"], qty=size.qty, side="BUY", entry_px=res.fill_px,
                       entry_ts=now, stop=stop, risk_per_share=size.risk_per_share,
                       strike=inst["strike"], right=inst["right"], expiry=ch.expiry,
                       lot_size=inst.get("lot_size", ch.lot_size), exch=inst.get("exch", "NFO"),
                       token=inst.get("token", ""), underlying=sig.symbol, entry_spot=spot,
                       delta_at_entry=inst.get("delta") or 0.0, max_prem=res.fill_px,
                       max_fav_spot=spot, strategy=intent.strategy)
        pos.position_id = self.store.open_position(pos)
        self.store.mark_signal_acted(sid)
        self.positions.append(pos)
        return True

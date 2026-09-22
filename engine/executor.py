"""LiveExecutor — routes engine entries/exits to real Shoonya orders.

Sits between the ExitEngine and ShoonyaSession.  Every call is guarded by
core.trade_mode.is_live(): in paper mode (the default) the executor is
completely inert, so the rest of the engine can call it unconditionally.

Real-money guardrails (env-tunable via .env, conservative defaults):

    LIVE_RISK_RUPEES     500     rupees risked per trade: qty = risk / |entry-stop|
    LIVE_MAX_QTY         50      hard cap on shares per order
    LIVE_MAX_CAPITAL     50000   per-trade notional cap (qty * price)
    LIVE_MAX_POSITIONS   3       max concurrently open LIVE positions
    LIVE_MAX_DAILY_LOSS  2000    realized ₹ loss for the day that trips the
                                 kill switch (blocks NEW entries; exits keep
                                 routing so open risk still gets managed)

Order mechanics:
    * NSE cash, product I (MIS intraday). Orders go out as MKT; if the
      account's algo guard blocks API market orders (ALGO_CHK), they fall
      back automatically to a marketable LIMIT (core.broker_check).
    * PlaceOrder is never auto-retried after a timeout (double-fire risk,
      see shoonya_client); the MKT->LMT fallback is safe because it only
      fires after a definitive rejection, which never reached the book.
    * Placement ack is not a fill: _await_fill polls order_book() for the
      terminal status and the actual average fill price.
    * Every action lands in the paper book as live_entry / live_exit /
      live_error rows and in the activity journal — one audit trail for
      both modes.
    * Realized ₹ P&L for the day persists in state/live_day.json so a
      restart cannot forget losses and re-arm past the daily cap.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, time as dtime

from core import trade_mode

try:
    from core.activity import activity
except Exception:                      # keep importable in bare tests
    class _NoopActivity:
        def add(self, *args, **kwargs):
            pass
    activity = _NoopActivity()

log = logging.getLogger("executor")

BUY, SELL = "BUY", "SELL"

ENTRY_OPEN = dtime(9, 15)              # no live entries outside this window
ENTRY_CLOSE = dtime(15, 15)
FILL_POLL_SECONDS = 1.2                # order_book poll cadence
FILL_WAIT_SECONDS = 6.0                # max wait for a terminal status
LIMIT_BUFFER_PCT = 0.005              # marketable-limit cushion past the
                                      # touch (0.5%): fills like a market
                                      # order, well inside the price band
REJECTED = {"REJECTED", "CANCELED", "CANCELLED"}
COMPLETE = {"COMPLETE"}

DAY_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "live_day.json")
LIMITS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "live_config.json")

# settable limit -> (attribute, min, max) — bounds stop fat-finger configs
LIMIT_FIELDS = {
    "risk_rupees":    ("risk_rupees",    50.0,  50_000.0),
    "max_qty":        ("max_qty",        1,     10_000),
    "max_capital":    ("max_capital",    1_000.0, 10_000_000.0),
    "max_positions":  ("max_positions",  1,     25),
    "max_daily_loss": ("max_daily_loss", 100.0, 1_000_000.0),
}


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


class LiveExecutor:
    """Real-order router. Inert unless trade_mode.is_live()."""

    def __init__(self, session, hub, book, day_state_path: str = DAY_STATE_PATH):
        self.session = session          # ShoonyaSession (logged in)
        self.hub = hub                  # DataHub — symbol -> NSE tradingsymbol
        self.book = book                # PaperBook — shared audit trail
        self.day_state_path = day_state_path
        self._lock = threading.RLock()
        # limits: env defaults, overridden by the UI-saved state file
        self.risk_rupees = _env_num("LIVE_RISK_RUPEES", 500.0)
        self.max_qty = int(_env_num("LIVE_MAX_QTY", 50))
        self.max_capital = _env_num("LIVE_MAX_CAPITAL", 50_000.0)
        self.max_positions = int(_env_num("LIVE_MAX_POSITIONS", 3))
        self.max_daily_loss = _env_num("LIVE_MAX_DAILY_LOSS", 2_000.0)
        self._load_limits()
        self.open_live_count = 0        # maintained by the ExitEngine
        self._order_updates: dict[str, dict] = {}   # norenordno -> ws msg
        self._day = {"date": "", "realized": 0.0, "orders": 0, "rejects": 0}
        self._load_day()

    # ── websocket order stream (faster than order_book polling) ──────────
    def on_order_update(self, msg: dict):
        """ShoonyaFeed on_order callback. Runs on the socket thread —
        store-and-return only, never block here."""
        try:
            no = str(msg.get("norenordno") or "")
            if no:
                self._order_updates[no] = dict(msg)
                if len(self._order_updates) > 500:      # day-session bound
                    self._order_updates.pop(
                        next(iter(self._order_updates)), None)
        except Exception:
            pass

    # ── risk limits: UI-settable, persisted, clamped to sane bounds ──────
    def limits(self) -> dict:
        return {"risk_rupees": self.risk_rupees, "max_qty": self.max_qty,
                "max_capital": self.max_capital,
                "max_positions": self.max_positions,
                "max_daily_loss": self.max_daily_loss}

    def set_limits(self, new: dict) -> dict:
        """Apply UI-chosen limits (unknown keys ignored, values clamped to
        LIMIT_FIELDS bounds), persist to state/live_config.json, and return
        the resulting limits."""
        changed = {}
        with self._lock:
            for key, (attr, lo, hi) in LIMIT_FIELDS.items():
                if key not in (new or {}):
                    continue
                try:
                    v = float(new[key])
                except (TypeError, ValueError):
                    continue
                v = min(max(v, lo), hi)
                if attr in ("max_qty", "max_positions"):
                    v = int(v)
                if getattr(self, attr) != v:
                    changed[key] = v
                setattr(self, attr, v)
            if changed:
                try:
                    os.makedirs(os.path.dirname(LIMITS_PATH), exist_ok=True)
                    tmp = LIMITS_PATH + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(self.limits(), f, indent=1)
                    os.replace(tmp, LIMITS_PATH)
                except Exception as e:
                    log.warning("live limits save failed: %s", e)
                log.warning("LIVE limits updated: %s", changed)
                activity.add("risk", f"live limits updated: {changed}",
                             **changed)
        return self.limits()

    def _load_limits(self):
        try:
            with open(LIMITS_PATH) as f:
                saved = json.load(f)
        except Exception:
            return
        if not isinstance(saved, dict):
            return
        for key, (attr, lo, hi) in LIMIT_FIELDS.items():
            if key not in saved:
                continue
            try:
                v = min(max(float(saved[key]), lo), hi)
            except (TypeError, ValueError):
                continue
            if attr in ("max_qty", "max_positions"):
                v = int(v)
            setattr(self, attr, v)

    # ── day P&L state (persists across restarts) ─────────────────────────
    def _load_day(self):
        try:
            with open(self.day_state_path) as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("date") == date.today().isoformat():
                self._day.update({k: d[k] for k in
                                  ("date", "realized", "orders", "rejects")
                                  if k in d})
        except Exception:
            pass

    def _save_day(self):
        try:
            os.makedirs(os.path.dirname(self.day_state_path), exist_ok=True)
            tmp = self.day_state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._day, f, indent=1)
            os.replace(tmp, self.day_state_path)
        except Exception as e:
            log.warning("live day-state save failed: %s", e)

    def _roll_day(self):
        today = date.today().isoformat()
        if self._day["date"] != today:
            self._day = {"date": today, "realized": 0.0,
                         "orders": 0, "rejects": 0}
            self._save_day()

    def _add_realized(self, pnl_rupees: float):
        with self._lock:
            self._roll_day()
            self._day["realized"] = round(self._day["realized"] + pnl_rupees, 2)
            self._save_day()
            if (self.max_daily_loss > 0
                    and self._day["realized"] <= -self.max_daily_loss):
                log.error("LIVE daily loss %.0f breached cap %.0f — "
                          "tripping kill switch (new entries blocked)",
                          self._day["realized"], self.max_daily_loss)
                activity.add("risk",
                             f"DAILY LOSS CAP HIT: realized "
                             f"₹{self._day['realized']:.0f} ≤ "
                             f"-₹{self.max_daily_loss:.0f} — new entries "
                             f"paused, exits still live",
                             realized=self._day["realized"])
                try:
                    from core import killswitch
                    killswitch.set_paused(True, source="daily_loss_cap")
                except Exception:
                    pass

    def day_stats(self) -> dict:
        with self._lock:
            self._roll_day()
            return dict(self._day)

    # ── availability ──────────────────────────────────────────────────────
    def active(self) -> bool:
        """Live mode on AND a broker session exists. Never raises."""
        try:
            return trade_mode.is_live() and self.session is not None \
                and bool(getattr(self.session, "access_token", None))
        except Exception:
            return False

    # ── sizing ────────────────────────────────────────────────────────────
    def size(self, price: float, stop: float | None) -> int:
        """Risk-based share count under all three caps; 0 = do not trade."""
        price = float(price)
        if price <= 0:
            return 0
        dist = abs(price - float(stop)) if stop else 0.0
        if dist <= 0:
            dist = 0.01 * price            # degenerate stop: assume 1% risk
        qty = math.floor(self.risk_rupees / dist)
        qty = min(qty, self.max_qty, math.floor(self.max_capital / price))
        return max(int(qty), 0)

    # ── order plumbing ────────────────────────────────────────────────────
    def _tsym(self, symbol: str) -> str | None:
        try:
            row = self.hub.cash_row(symbol)
        except Exception:
            row = None
        if row:
            ts = row.get("TradingSymbol") or row.get("Tsym")
            if ts:
                return str(ts)
        return f"{symbol}-EQ"              # NSE cash convention fallback

    @staticmethod
    def _parse_order_row(r: dict, out: dict) -> str:
        """Merge one order row/update into `out`; returns the status."""
        st = str(r.get("status", "")).upper()
        if st:
            out["status"] = st
        try:
            out["avg_price"] = float(r.get("avgprc") or 0) or out["avg_price"]
        except (TypeError, ValueError):
            pass
        try:
            out["filled_qty"] = int(float(r.get("fillshares") or 0)) \
                or out["filled_qty"]
        except (TypeError, ValueError):
            pass
        out["reject_reason"] = r.get("rejreason") or out["reject_reason"]
        return st

    def _await_fill(self, order_no: str) -> dict:
        """Wait for a terminal order status. Websocket order updates are
        checked first (instant); order_book() polling is the fallback."""
        deadline = time.monotonic() + FILL_WAIT_SECONDS
        out = {"status": "PENDING", "avg_price": None,
               "filled_qty": 0, "reject_reason": None}
        while time.monotonic() < deadline:
            ws_msg = self._order_updates.get(str(order_no))
            if ws_msg is not None:
                st = self._parse_order_row(ws_msg, out)
                if st in COMPLETE or st in REJECTED:
                    return out
                time.sleep(0.2)            # live stream armed — spin lightly
                continue
            try:
                rows = self.session.order_book() or []
            except Exception as e:
                log.warning("order_book poll failed: %s", e)
                rows = []
            for r in rows:
                if str(r.get("norenordno", "")) != str(order_no):
                    continue
                st = self._parse_order_row(r, out)
                if st in COMPLETE or st in REJECTED:
                    return out
                break
            time.sleep(FILL_POLL_SECONDS)
        return out

    def _ticksize(self, symbol: str) -> float:
        try:
            row = self.hub.cash_row(symbol) if self.hub else None
            if row and row.get("TickSize"):
                return float(row["TickSize"]) or 0.05
        except Exception:
            pass
        return 0.05

    def _place(self, *, symbol: str, side: str, qty: int, remarks: str,
               ref_price: float | None = None, exchange: str = "NSE",
               tsym: str | None = None,
               ticksize: float | None = None) -> dict | None:
        """One MIS order + fill wait. Sends a true MKT order and, only if
        this account's algo guard blocks API market orders, falls back to
        a marketable LIMIT (see core.broker_check.place_marketable).
        Returns the fill dict or None.

        ``exchange``/``tsym``/``ticksize`` default to the NSE cash equity for
        ``symbol`` (the intraday flow); manual / MCX orders pass them explicitly.
        """
        tsym = tsym or self._tsym(symbol)
        from core.broker_check import place_marketable

        def _quote():                          # called only on MKT-blocked
            q = None
            try:
                q = self.hub.cash_quote(symbol) if self.hub else None
            except Exception:
                q = None
            if not q and ref_price:
                q = {"lp": float(ref_price)}
            return q

        try:
            resp = place_marketable(
                self.session, side=side, exchange=exchange, tsym=tsym,
                qty=qty, product="I", remarks=remarks[:24],
                get_quote=_quote, ticksize=ticksize or self._ticksize(symbol),
                buffer_pct=LIMIT_BUFFER_PCT)
        except Exception as e:
            log.error("LIVE place_order %s %s x%d FAILED: %s",
                      side, symbol, qty, e)
            self._record_error(symbol, side, qty, remarks, str(e))
            return None
        order_no = str((resp or {}).get("norenordno") or "")
        if not order_no:
            self._record_error(symbol, side, qty, remarks,
                               f"no order number in response: {resp}")
            return None
        with self._lock:
            self._roll_day()
            self._day["orders"] += 1
            self._save_day()
        fill = self._await_fill(order_no)
        fill["order_no"] = order_no
        fill["tsym"] = tsym
        if fill["status"] in REJECTED:
            with self._lock:
                self._day["rejects"] += 1
                self._save_day()
            log.error("LIVE order %s %s x%d REJECTED: %s",
                      side, symbol, qty, fill.get("reject_reason"))
            self._record_error(symbol, side, qty, remarks,
                               f"rejected: {fill.get('reject_reason')}",
                               order_no=order_no)
            return None
        return fill

    def _record_error(self, symbol, side, qty, remarks, error, order_no=None):
        try:
            self.book.record({"type": "live_error", "symbol": symbol,
                              "side": side, "qty": qty, "remarks": remarks,
                              "order_no": order_no, "error": str(error)})
        except Exception:
            pass
        activity.add("live_error",
                     f"LIVE {side} {symbol} x{qty} failed: {error}",
                     symbol=symbol, side=side, qty=int(qty))

    # ── public API: entry / exit ─────────────────────────────────────────
    def enter(self, *, symbol: str, direction: str, price: float,
              stop: float | None, position_id: str) -> dict | None:
        """Place the real entry. Returns {'qty','fill_price','order_no',
        'status'} or None (limits refused / order failed / rejected)."""
        if not self.active():
            return None
        now = datetime.now().time()
        if not (ENTRY_OPEN <= now <= ENTRY_CLOSE):
            log.info("LIVE entry %s refused: outside %s-%s",
                     symbol, ENTRY_OPEN, ENTRY_CLOSE)
            return None
        stats = self.day_stats()
        if (self.max_daily_loss > 0
                and stats["realized"] <= -self.max_daily_loss):
            log.warning("LIVE entry %s refused: daily loss cap", symbol)
            return None
        if self.open_live_count >= self.max_positions:
            log.info("LIVE entry %s refused: %d open >= max %d",
                     symbol, self.open_live_count, self.max_positions)
            return None
        qty = self.size(price, stop)
        if qty < 1:
            log.info("LIVE entry %s refused: sized to 0 shares "
                     "(risk ₹%.0f / stop-dist)", symbol, self.risk_rupees)
            return None

        fill = self._place(symbol=symbol, side=direction, qty=qty,
                           remarks=position_id, ref_price=price)
        if fill is None:
            return None
        fill_price = fill.get("avg_price") or float(price)
        filled_qty = fill.get("filled_qty") or qty
        out = {"qty": int(filled_qty), "fill_price": round(fill_price, 4),
               "order_no": fill["order_no"], "status": fill["status"]}
        try:
            self.book.record({"type": "live_entry", "position_id": position_id,
                              "symbol": symbol, "direction": direction,
                              "signal_price": float(price), **out})
        except Exception:
            pass
        log.warning("LIVE ENTRY %s %s x%d @ %.2f (order %s, %s)",
                    direction, symbol, out["qty"], fill_price,
                    fill["order_no"], fill["status"])
        activity.add("live_order",
                     f"LIVE ENTRY {direction} {symbol} x{out['qty']} @ "
                     f"{fill_price:.2f} ({fill['status']})",
                     symbol=symbol, direction=direction, qty=out["qty"],
                     price=float(fill_price), order_no=fill["order_no"])
        return out

    def close(self, *, symbol: str, direction: str, qty: int,
              entry_fill: float, position_id: str,
              reason: str) -> dict | None:
        """Close `qty` shares of a live position (opposite-side order).
        Exits are always allowed while active() — no entry-window or
        daily-cap refusal: open risk must be closeable."""
        if not self.active() or qty < 1:
            return None
        side = SELL if direction == BUY else BUY
        # ref_price = entry_fill is only a last-resort for the LMT fallback
        # if the live quote is also unavailable; normally the fresh quote
        # inside _place prices the exit.
        fill = self._place(symbol=symbol, side=side, qty=int(qty),
                           remarks=f"x:{reason}"[:24], ref_price=entry_fill)
        if fill is None:
            return None
        fill_price = fill.get("avg_price")
        pnl = None
        if fill_price:
            sign = 1.0 if direction == BUY else -1.0
            pnl = round((float(fill_price) - float(entry_fill))
                        * sign * int(qty), 2)
            self._add_realized(pnl)
        out = {"qty": int(qty), "fill_price": fill_price,
               "order_no": fill["order_no"], "status": fill["status"],
               "pnl_rupees": pnl, "reason": reason}
        try:
            self.book.record({"type": "live_exit", "position_id": position_id,
                              "symbol": symbol, "direction": direction,
                              **out})
        except Exception:
            pass
        log.warning("LIVE EXIT %s %s x%d @ %s (%s) pnl=%s",
                    reason.upper(), symbol, qty, fill_price,
                    fill["status"], pnl)
        activity.add("live_order",
                     f"LIVE EXIT {reason.upper()} {symbol} x{qty} @ "
                     f"{fill_price if fill_price else '?'} "
                     f"pnl {'₹%.0f' % pnl if pnl is not None else 'n/a'}",
                     symbol=symbol, qty=int(qty), reason=reason,
                     order_no=fill["order_no"])
        return out

    # ── startup reconciliation ────────────────────────────────────────────
    def reconcile(self, tracked: list[dict]) -> dict:
        """Compare broker MIS net positions against locally tracked live
        positions. Logs mismatches loudly; returns a report dict.
        `tracked`: [{'symbol', 'direction', 'live_qty_left'}, ...]"""
        report = {"broker": [], "tracked": len(tracked), "mismatches": []}
        if not self.active():
            return report
        try:
            rows = self.session.positions() or []
        except Exception as e:
            report["error"] = str(e)
            log.warning("reconcile: positions() failed: %s", e)
            return report
        broker = {}
        for r in rows:
            if str(r.get("prd", "")) != "I":
                continue
            try:
                net = int(float(r.get("netqty") or 0))
            except (TypeError, ValueError):
                net = 0
            if net == 0:
                continue
            sym = str(r.get("tsym", "")).replace("-EQ", "")
            broker[sym] = net
            report["broker"].append({"symbol": sym, "netqty": net})
        local = {}
        for t in tracked:
            q = int(t.get("live_qty_left") or 0)
            sign = 1 if t.get("direction") == BUY else -1
            local[t["symbol"]] = local.get(t["symbol"], 0) + sign * q
        for sym in sorted(set(broker) | set(local)):
            if broker.get(sym, 0) != local.get(sym, 0):
                m = {"symbol": sym, "broker": broker.get(sym, 0),
                     "local": local.get(sym, 0)}
                report["mismatches"].append(m)
                log.error("RECONCILE MISMATCH %s: broker=%+d local=%+d — "
                          "square off manually or restart after fixing",
                          sym, m["broker"], m["local"])
                activity.add("risk",
                             f"RECONCILE MISMATCH {sym}: broker {m['broker']:+d}"
                             f" vs local {m['local']:+d} — check the broker "
                             f"terminal!", **m)
        if not report["mismatches"] and (broker or local):
            log.info("reconcile OK: %d live position(s) match the broker",
                     len(local))
        return report

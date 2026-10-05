"""Broker implementations (PDF §2.2 execution surface).

Single code path: PaperBroker and GatewayBroker satisfy the same `Broker`
contract, so the engine, risk, rules and exits are identical in paper and live —
only the object wired in changes.

  • PaperBroker  — fills immediately at the intent's marketable price ± slippage,
    tracks positions in memory. Self-contained; the offline/backtest broker.
  • GatewayBroker — routes to the broker Gateway (Shoonya). Placement returns the
    broker ack; real fills arrive asynchronously on the order-update WS (wired in
    Phase 2's session loop), reconciled against the order book.
"""
from __future__ import annotations

import time

from .contracts import Broker, OrderIntent


def _side_sign(side: str) -> int:
    return 1 if str(side).upper().startswith("B") else -1


class PaperBroker(Broker):
    """Simulated broker that bleeds like the live one. Fills at the LIVE touch
    (ask for a buy, bid for a sell — the spread is always paid) re-read at
    execution time through `quote_provider`, after an `exec_delay_s` pause that
    stands in for the submit→exchange→fill round trip, then moved adversely by
    `slippage_pct`. Without a provider it falls back to intent.limit_px (the
    marketable price the strategy computed); a MKT intent must then carry a
    reference price in limit_px. With no provider and no delay it is fully
    deterministic (the backtest behaviour)."""
    name = "paper"

    def __init__(self, slippage_pct: float = 0.10, quote_provider=None,
                 exec_delay_s: float = 0.0):
        self.slippage = slippage_pct / 100.0
        self.quote_provider = quote_provider  # intent -> (bid, ask) | None
        self.exec_delay_s = min(max(float(exec_delay_s or 0.0), 0.0), 30.0)
        self._orders: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}   # symbol -> netted position
        self._seq = 0

    def place(self, intent: OrderIntent) -> dict:
        self._seq += 1
        oid = f"PAPER-{self._seq}"
        if self.exec_delay_s > 0:
            time.sleep(self.exec_delay_s)        # the order travels; the market moves
        ref = intent.limit_px
        if self.quote_provider is not None:      # re-price at the live touch
            try:
                touch = self.quote_provider(intent)
            except Exception:
                touch = None
            if touch:
                try:
                    bid, ask = float(touch[0]), float(touch[1])
                except (TypeError, ValueError, IndexError):
                    bid = ask = 0.0
                live = ask if _side_sign(intent.side) > 0 else bid
                if live and live > 0:
                    ref = live
        if ref is None or ref <= 0:
            return {"broker_order_id": oid, "status": "REJECTED",
                    "reason": "paper fill needs a reference price in limit_px"}
        sign = _side_sign(intent.side)
        # slippage always hurts: pay up to buy, receive less to sell
        fill_px = ref * (1 + self.slippage) if sign > 0 else ref * (1 - self.slippage)
        fill_px = round(fill_px, 2)
        self._orders[oid] = {"broker_order_id": oid, "status": "FILLED",
                             "fill_px": fill_px, "qty": intent.qty,
                             "symbol": intent.symbol, "side": intent.side,
                             "ts": time.time()}
        self._apply_fill(intent.symbol, sign * intent.qty, fill_px)
        return {"broker_order_id": oid, "status": "FILLED", "fill_px": fill_px,
                "filled_qty": intent.qty}

    def _apply_fill(self, symbol: str, signed_qty: int, px: float) -> None:
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = {"symbol": symbol, "net_qty": signed_qty,
                                       "avg_px": px}
            return
        old = pos["net_qty"]
        new = old + signed_qty
        if old == 0 or (old > 0) == (signed_qty > 0):        # opening / adding
            tot = abs(old) + abs(signed_qty)
            pos["avg_px"] = (pos["avg_px"] * abs(old) + px * abs(signed_qty)) / tot if tot else px
        pos["net_qty"] = new
        if new == 0:
            pos["avg_px"] = 0.0

    def cancel(self, broker_order_id: str) -> bool:
        o = self._orders.get(broker_order_id)
        if o and o["status"] not in ("FILLED", "CANCELLED"):
            o["status"] = "CANCELLED"
            return True
        return False       # paper orders fill instantly; nothing to cancel

    def positions(self) -> list[dict]:
        return [dict(p) for p in self._positions.values() if p["net_qty"] != 0]

    def order(self, broker_order_id: str) -> dict | None:
        return self._orders.get(broker_order_id)


_FILLED_STATUSES = {"COMPLETE", "TRADED", "FILLED"}
_REJECTED_STATUSES = {"REJECTED", "CANCELLED", "CANCELED"}


def _book_row(order_book: list, broker_order_id: str) -> dict | None:
    """Find one order-book row by broker id, tolerant to key/case drift."""
    want = str(broker_order_id or "").strip().lower()
    if not want:
        return None
    for r in order_book or []:
        if not isinstance(r, dict):
            continue
        for k in ("norenordno", "order_id", "broker_order_id", "NOrdNo", "id"):
            if str(r.get(k) or "").strip().lower() == want:
                return r
    return None


def _book_fill(row: dict | None) -> tuple[int, float]:
    """(filled shares, average fill price) from an order-book row; zeros when
    the broker reports nothing (unfilled — never assume a fill)."""
    if not row:
        return 0, 0.0
    try:
        q = int(float(row.get("fillshares") or 0))
    except (TypeError, ValueError):
        q = 0
    try:
        px = float(row.get("avgprc") or 0)
    except (TypeError, ValueError):
        px = 0.0
    return max(q, 0), (px if px > 0 else 0.0)


class GatewayBroker(Broker):
    """Routes orders to the broker Gateway (Shoonya). Intraday MIS by default.

    A placement ack is NOT a fill, so place() confirms synchronously against
    the order book (bounded poll): it returns FILLED only for a broker-
    confirmed fill, with the filled share count. An order still working after
    the window is CANCELLED and re-checked once (cancel/fill race); anything
    left over returns TIMEOUT (nothing filled) or WORKING (still live at the
    broker — the engine tracks nothing for it and a human must resolve the
    book). Rejections surface as REJECTED with the broker's reason.
    """
    name = "gateway"

    def __init__(self, client, product_type: str = "I",
                 confirm_timeout_s: float = 12.0, poll_s: float = 1.0):
        self.client = client                 # intraday.gateway_client.GatewayClient
        self.product_type = product_type     # I=MIS (intraday), M=NRML, C=CNC
        self.confirm_timeout_s = max(float(confirm_timeout_s or 0), 0.0)
        self.poll_s = min(max(float(poll_s or 0.5), 0.2), 5.0)

    def place(self, intent: OrderIntent) -> dict:
        bs = "B" if _side_sign(intent.side) > 0 else "S"
        # MARKETABLE_LIMIT / MKT -> MKT (the Gateway converts to a marketable
        # limit through the touch, since Shoonya RMS blocks a plain API MKT).
        if intent.order_type == "LIMIT" and intent.limit_px:
            price_type, price = "LMT", float(intent.limit_px)
        else:
            price_type, price = "MKT", 0.0
        ack = self.client.place_order(
            exchange=intent.exch or "NFO", tradingsymbol=intent.symbol,
            quantity=int(intent.qty), buy_or_sell=bs, price_type=price_type,
            price=price, product_type=self.product_type,
            remarks=(intent.reason or "")[:32])
        if not isinstance(ack, dict):
            return {"broker_order_id": "", "status": "UNKNOWN",
                    "reason": f"gateway answered non-dict: {str(ack)[:80]}",
                    "raw": {"ack": str(ack)[:200]}}
        oid = str(ack.get("norenordno") or ack.get("order_id")
                  or ack.get("broker_order_id") or "")
        if not oid:
            return {"broker_order_id": "", "status": "REJECTED",
                    "reason": f"no broker order id in ack: {str(ack)[:160]}",
                    "raw": ack}
        return self._confirm(intent, oid, ack)

    def _confirm(self, intent: OrderIntent, oid: str, ack: dict) -> dict:
        """Poll the order book until the order reaches a terminal verdict."""
        deadline = time.time() + self.confirm_timeout_s
        row = None
        while True:
            row = self._book_row(oid)
            verdict = self._verdict(intent, oid, row, ack)
            if verdict is not None:
                return verdict
            if time.time() >= deadline:
                break
            time.sleep(self.poll_s)
        # Still working after the window: cancel it so no live order is left
        # untracked, then re-check once (cancel and fill can race).
        try:
            self.cancel(oid)
        except Exception:
            pass
        time.sleep(min(self.poll_s, 1.0))
        row = self._book_row(oid)
        verdict = self._verdict(intent, oid, row, ack)
        if verdict is not None:
            return verdict
        filled, _px = _book_fill(row)
        status_now = str((row or {}).get("status", "")).upper()
        if filled > 0:
            # Partially filled and the rest would not cancel: the filled
            # shares are real and trackable; the remainder is flagged WORKING.
            px = _book_fill(row)[1] or (intent.limit_px or 0.0)
            return {"broker_order_id": oid, "status": "WORKING",
                    "fill_px": float(px) if px else None, "filled_qty": filled,
                    "reason": f"partial {filled}/{intent.qty} filled, rest still "
                              f"{status_now or 'working'} at broker — resolve manually",
                    "raw": {"ack": ack, "book": row}}
        return {"broker_order_id": oid, "status": "TIMEOUT",
                "reason": f"no fill in {self.confirm_timeout_s:.0f}s "
                          f"(book: {status_now or 'no row'}); cancel requested",
                "raw": {"ack": ack, "book": row}}

    def _book_row(self, oid: str) -> dict | None:
        try:
            return _book_row(self.client.order_book(), oid)
        except Exception:
            return None

    def _verdict(self, intent: OrderIntent, oid: str, row: dict | None,
                 ack: dict) -> dict | None:
        """A terminal verdict for this book row, or None to keep polling."""
        if row is None:
            return None                      # not in the book yet — keep polling
        status = str(row.get("status") or "").upper()
        filled, px = _book_fill(row)
        if status in _FILLED_STATUSES and filled > 0:
            return {"broker_order_id": oid, "status": "FILLED",
                    "fill_px": float(px or intent.limit_px or 0.0) or None,
                    "filled_qty": filled, "raw": {"ack": ack, "book": row}}
        if status in _FILLED_STATUSES and filled <= 0:
            return None                      # COMPLETE but no fill qty yet — poll on
        if status in _REJECTED_STATUSES and filled <= 0:
            reason = (row.get("rejreason") or row.get("remarks")
                      or status or "rejected")
            return {"broker_order_id": oid, "status": "REJECTED",
                    "reason": f"broker {status}: {reason}"[:160], "raw": ack}
        if status in _REJECTED_STATUSES and filled > 0:
            # Cancelled/rejected AFTER a partial fill: track the filled shares.
            return {"broker_order_id": oid, "status": "FILLED",
                    "fill_px": float(px or intent.limit_px or 0.0) or None,
                    "filled_qty": filled,
                    "reason": f"partial {filled}/{intent.qty} filled before {status}",
                    "raw": {"ack": ack, "book": row}}
        return None                          # OPEN/PENDING/... — keep polling

    def cancel(self, broker_order_id: str) -> bool:
        try:
            self.client.cancel_order(broker_order_id)
            return True
        except Exception:
            return False

    def positions(self) -> list[dict]:
        try:
            return list(self.client.positions())
        except Exception:
            return []

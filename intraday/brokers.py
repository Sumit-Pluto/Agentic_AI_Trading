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
    """Deterministic simulated broker. Fills at intent.limit_px (the marketable
    price the strategy computed: ask for a buy, bid for a sell) moved adversely
    by `slippage_pct`. A MKT intent must carry a reference price in limit_px."""
    name = "paper"

    def __init__(self, slippage_pct: float = 0.10):
        self.slippage = slippage_pct / 100.0
        self._orders: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}   # symbol -> netted position
        self._seq = 0

    def place(self, intent: OrderIntent) -> dict:
        self._seq += 1
        oid = f"PAPER-{self._seq}"
        ref = intent.limit_px
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
        return {"broker_order_id": oid, "status": "FILLED", "fill_px": fill_px}

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


class GatewayBroker(Broker):
    """Routes orders to the broker Gateway (Shoonya). Intraday MIS by default.

    Placement returns the broker's ack; a placement ack is NOT a fill — the
    order-update WS is the source of truth, reconciled by the session loop
    (Phase 2). `positions()` reads the live book.
    """
    name = "gateway"

    def __init__(self, client, product_type: str = "I"):
        self.client = client                 # intraday.gateway_client.GatewayClient
        self.product_type = product_type     # I=MIS (intraday), M=NRML, C=CNC

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
        oid = (ack.get("norenordno") or ack.get("order_id")
               or ack.get("broker_order_id") or "")
        status = str(ack.get("status") or ("OK" if oid else "UNKNOWN")).upper()
        return {"broker_order_id": oid, "status": status, "raw": ack}

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

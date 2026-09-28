"""Lot helpers — averages from lot records, DB→response serialization."""
from typing import Optional

from app.core import state
from app.schemas import OrderLotResponse
from app.services.pnl import _lot_live_pnl, _lot_mtm_pnl
from brokers.shoonya.scripmaster import get_scripmaster
from db.models import OrderLot


def _compute_avg_from_lots(lots: list[OrderLot]) -> tuple[Optional[float], Optional[float]]:
    """Cost basis of the *currently open* position, from OPEN/PARTIAL lots.

    Weighted by open_qty, so a partially exited lot counts only the shares
    still held (their basis is the lot's entry price). PENDING and CANCELLED
    lots never filled — their avg_entry_price is just the order price — so
    they are excluded, as are fully CLOSED lots from earlier round-trips.

    Returns (None, None) when nothing is open, or when any open lot is
    external — external sync only records the net position, so the true
    buy/sell breakdown is unknown and the caller should fall back to
    broker-reported averages.
    """
    open_lots = [l for l in lots if l.status in ("OPEN", "PARTIAL") and (l.open_qty or 0) > 0]
    if not open_lots:
        return None, None
    if any(l.is_external for l in open_lots):
        return None, None

    buy_qty = 0
    buy_amt = 0.0
    sell_qty = 0
    sell_amt = 0.0
    for lot in open_lots:
        oq = lot.open_qty or 0
        ep = lot.avg_entry_price or 0.0
        if oq <= 0 or ep <= 0:
            continue
        if lot.side == "B":
            buy_qty += oq
            buy_amt += oq * ep
        else:
            sell_qty += oq
            sell_amt += oq * ep

    if buy_qty == 0 and sell_qty == 0:
        return None, None
    buy_avg = (buy_amt / buy_qty) if buy_qty > 0 else 0.0
    sell_avg = (sell_amt / sell_qty) if sell_qty > 0 else 0.0
    return buy_avg, sell_avg


def _serialize_lot(lot: OrderLot) -> OrderLotResponse:
    exit_price = None
    prev_close = None
    if lot.token and lot.open_qty > 0:
        key = f"{lot.exch}|{lot.token}"
        # Display P&L is valued at LTP — matches the Positions card and the
        # broker UI. Bid/ask exit pricing is reserved for target execution
        # (targets.py), where it prices the actual exit order.
        exit_price = state._current_ltps.get(key)
        prev_close = state._current_closes.get(key)
    live = 0.0 if lot.status == "PENDING" else _lot_live_pnl(lot, exit_price)
    # Day MTM: (ltp - prev_close) * qty. Only meaningful for a live open leg.
    mtm = 0.0 if lot.status == "PENDING" else _lot_mtm_pnl(lot, exit_price, prev_close)
    sm = get_scripmaster()
    scrip = sm.master.get(f"{lot.exch}|{lot.tsym}", {})
    prcftr = float(scrip.get("prcftr", "1") or "1")
    pending_exit_orderid = ""
    exit_notional = 0.0
    exit_filled_qty = 0
    for ex in lot.exits:
        if ex.status == "PENDING" and not pending_exit_orderid:
            pending_exit_orderid = ex.broker_exit_orderid or ""
        fq = int(ex.filled_qty or 0)
        if fq > 0 and (ex.avg_exit_price or 0.0) > 0:
            exit_notional += fq * float(ex.avg_exit_price)
            exit_filled_qty += fq
    avg_exit_price = (exit_notional / exit_filled_qty) if exit_filled_qty > 0 else 0.0
    return OrderLotResponse(
        id=lot.id,
        tsym=lot.tsym,
        exch=lot.exch,
        side=lot.side,
        entry_qty=lot.entry_qty,
        open_qty=lot.open_qty,
        avg_entry_price=lot.avg_entry_price or 0.0,
        realized_pnl=lot.realized_pnl or 0.0,
        carried_pnl=lot.carried_pnl or 0.0,
        live_pnl=live,
        mtm_pnl=mtm,
        prev_close=prev_close or 0.0,
        ltp=exit_price or 0.0,
        status=lot.status,
        opened_at=lot.opened_at.isoformat() if lot.opened_at else "",
        closed_at=lot.closed_at.isoformat() if lot.closed_at else "",
        lotsize=lot.lotsize or 1,
        prcftr=prcftr,
        broker_entry_orderid=lot.broker_entry_orderid or "",
        product_type=lot.product_type or "M",
        token=lot.token or "",
        pending_exit_orderid=pending_exit_orderid,
        is_external=bool(lot.is_external),
        is_rollover=bool(lot.is_rollover),
        is_reentry=bool(lot.is_reentry),
        is_temp_exit=bool(lot.is_temp_exit),
        is_persistent=lot.persistent_order_id is not None,
        source_service=lot.source_service,
        avg_exit_price=avg_exit_price,
        exit_filled_qty=exit_filled_qty,
        description=lot.description or "",
        expd=scrip.get("expd", ""),
        sym=scrip.get("sym", ""),
        target_enabled=bool(lot.target_enabled),
        target_value=lot.target_value or 0.0,
    )

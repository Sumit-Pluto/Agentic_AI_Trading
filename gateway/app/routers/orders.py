import asyncio
import logging
import math
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.core import state
from app.core.deps import _require_legacy_auth
from app.core.security import Principal, get_principal
from app.schemas import OrderBookResponse, OrderItem, PlaceOrderRequest
from app.services.execution_coordinator import coordinator
from app.services.reconciliation import (
    _is_exit_remarks,
    _reconcile_lots_from_orderbook,
    _short_poll_reconcile,
)
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import OrderLot, Service

router = APIRouter()
logger = logging.getLogger(__name__)

# §8 ExecutionCoordinator. Opt-in (the intraday multi-strategy deployment sets
# EXECUTION_COORDINATOR=1); off by default so a single-client / human gateway and
# its re-entry flows are unaffected. Applies to NEW ENTRIES only — exits and
# intentional re-entries bypass the dedup.
_COORDINATOR_ON = os.getenv("EXECUTION_COORDINATOR", "0") not in ("0", "false", "False", "")


# Shoonya's RMS blocks plain MARKET orders for API/algo flow (ALGO_CHK). We
# transparently convert MKT → a MARKETABLE LIMIT (a limit priced through the
# touch) which fills like a market order but passes the check. Toggle off with
# SHOONYA_MKT_TO_LIMIT=0 if the account later gets MKT-for-API enabled.
_MKT_TO_LIMIT = os.getenv("SHOONYA_MKT_TO_LIMIT", "1") not in ("0", "false", "False", "")
# How far THROUGH the touch a converted MKT order prices itself.
#
# A marketable limit only has to cross the spread; every point beyond that is
# pure permitted slippage, because the limit lets the order fill all the way
# down to it if the book is thin. A percentage does not express that: 0.3% is
# under a rupee on a ₹250 contract and ~₹47 on a ₹15,500 one, so the same
# setting was a reasonable buffer on natural gas and a 47-point giveaway on
# gold. Cross by a few TICKS, and keep the percentage only as a ceiling for
# instruments whose tick is coarse relative to their price.
_MARKETABLE_BUFFER_PCT = float(os.getenv("SHOONYA_MARKETABLE_BUFFER_PCT", "0.003") or 0.003)
_MARKETABLE_TICKS = max(int(os.getenv("SHOONYA_MARKETABLE_TICKS", "3") or 3), 0)


def _tick_of(exchange: str, tsym: str) -> float:
    """Instrument tick size from the scripmaster."""
    sm = get_scripmaster()
    scrip = sm.master.get(f"{exchange}|{tsym}", {}) if hasattr(sm, "master") else {}
    try:
        return float(scrip.get("ticksize") or 0) or 0.05
    except (TypeError, ValueError):
        return 0.05


def _snap_to_tick(price: float, exchange: str, tsym: str) -> float:
    """Snap a limit price onto the instrument's tick grid.

    Shoonya rejects off-grid limit prices outright, so a hand-typed 412.53 on a
    0.05-tick contract is a rejected order at exactly the moment the user
    wanted to trade. Nearest-tick keeps the user's intent (unlike the
    marketable path, which deliberately rounds through the touch).
    """
    if price <= 0:
        return price
    tick = _tick_of(exchange, tsym)
    return round(round(price / tick) * tick, 2) if tick > 0 else round(price, 2)


async def _marketable_limit_price(broker, token_obj, credentials, exchange: str,
                                  tsym: str, side: str):
    """Compute a marketable LIMIT price for a would-be MKT order: buy at/through
    the ask, sell at/through the bid, plus a small buffer, rounded to the tick.
    Returns (price, tick) or (None, None) if no live price is available."""
    sm = get_scripmaster()
    tok = (sm.get_token(exchange, tsym) or "") if hasattr(sm, "get_token") else ""
    bid = ask = ltp = tick_q = 0.0
    try:
        q = await broker.getQuote(token_obj, credentials, symbol=tok, exchange=exchange)
        bid = float(q.get("bp1") or 0)
        ask = float(q.get("sp1") or 0)
        ltp = float(q.get("lp") or 0)
        tick_q = float(q.get("ti") or 0) or 0.0
    except Exception:
        pass
    need_ask = side == "B"
    ref = (ask or ltp) if need_ask else (bid or ltp)
    if not ref or ref <= 0:
        return None, None
    scrip = sm.master.get(f"{exchange}|{tsym}", {}) if hasattr(sm, "master") else {}
    try:
        tick = float(scrip.get("ticksize") or 0) or tick_q or 0.05
    except (TypeError, ValueError):
        tick = tick_q or 0.05
    # Cross by whole ticks, never further than the percentage ceiling. `min` is
    # the point: on a coarse-tick instrument the ticks win, on a high-priced one
    # the percentage stops a few ticks becoming a large rupee concession.
    tick_buf = tick * _MARKETABLE_TICKS
    pct_buf = ref * _MARKETABLE_BUFFER_PCT
    buf = min(tick_buf, pct_buf) if tick_buf > 0 else pct_buf
    raw = ref + buf if need_ask else max(ref - buf, tick)
    # buy rounds UP to a tick, sell rounds DOWN → stays marketable after rounding
    steps = math.ceil(raw / tick) if need_ask else math.floor(raw / tick)
    return round(max(steps, 1) * tick, 2), tick


@router.post("/api/orders")
async def place_order(req: PlaceOrderRequest, principal: Principal = Depends(get_principal)):
    """Place a new order.

    For entry orders (remarks not flagged as exit), creates an OrderLot row and
    tags the broker order with `lot:<client_ref>` so fills can be reconciled
    back to the originating lot. Entry lots are stamped with `source_service`
    when placed by a machine service (e.g. snowball) so the Gateway UI can badge
    where each order came from; human-placed orders leave it NULL.
    """
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    # §8 conflict management: refuse a duplicate / already-in-flight NEW ENTRY on
    # this (account, contract) across ALL strategy clients. Exits and intentional
    # re-entries bypass (they legitimately repeat a contract). Atomic on the event
    # loop; the reservation auto-expires. Enable with EXECUTION_COORDINATOR=1.
    if (_COORDINATOR_ON and not _is_exit_remarks(req.remarks)
            and req.reentry_source_lot_id is None):
        _ok, _why = coordinator.reserve(auth.user_id, req.exchange, req.tradingsymbol,
                                        req.buy_or_sell, req.quantity, req.price or 0.0)
        if not _ok:
            raise HTTPException(status_code=409, detail=f"execution coordinator: {_why}")
    if req.price_type == "LMT":
        if req.price is None or req.price <= 0:
            raise HTTPException(status_code=400,
                                detail="a LMT order needs price > 0 (use MKT for a market order)")
        snapped = _snap_to_tick(req.price, req.exchange, req.tradingsymbol)
        if snapped != req.price:
            logger.info("tick-snapped LMT price for %s: %.4f → %.2f",
                        req.tradingsymbol, req.price, snapped)
            req.price = snapped

    # MKT is rejected by this account's RMS (ALGO_CHK) → convert to a marketable
    # LIMIT that fills like a market order but is accepted for API orders.
    if _MKT_TO_LIMIT and req.price_type == "MKT":
        px, tick = await _marketable_limit_price(broker, token, credentials,
                                                 req.exchange, req.tradingsymbol, req.buy_or_sell)
        if px and px > 0:
            logger.info("MKT→marketable LMT: %s %s → %.2f (tick %s, +/-%.2g%%)",
                        req.buy_or_sell, req.tradingsymbol, px, tick, _MARKETABLE_BUFFER_PCT * 100)
            req.price_type = "LMT"
            req.price = px
        else:
            logger.warning("MKT→LMT: no live price for %s — sending MKT (may hit ALGO_CHK)",
                           req.tradingsymbol)

    is_entry = not _is_exit_remarks(req.remarks)
    lot: OrderLot | None = None
    db = None
    # Snapshot of a recycled CLOSED lot's fields, so a broker failure can put
    # the row back the way it was instead of leaving a half-reopened lot.
    reentry_snapshot: dict | None = None
    if is_entry:
        sm = get_scripmaster()
        tok = sm.get_token(req.exchange, req.tradingsymbol) or ""
        scrip = sm.master.get(f"{req.exchange}|{req.tradingsymbol}", {}) if hasattr(sm, "master") else {}
        try:
            lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
        except Exception:
            lotsize = 1
        client_ref = f"lot{uuid.uuid4().hex[:12]}"
        db = next(get_db())

        # Stamp which service placed this order (human callers → NULL). The
        # service JWT's subject is its client_id; map it to the Service.name so
        # the UI badges by a stable, readable name ("snowball" → SB).
        source_service: str | None = None
        if principal.kind == "service":
            svc = db.query(Service).filter_by(client_id=principal.client_id).first()
            source_service = svc.name if svc else (principal.client_id or "service")

        carried_pnl = 0.0
        is_reentry = False
        recycle_lot: OrderLot | None = None
        if req.reentry_source_lot_id is not None:
            source_lot = db.query(OrderLot).filter(
                OrderLot.id == req.reentry_source_lot_id,
                OrderLot.owner_uid == auth.user_id,
            ).first()
            if source_lot is not None:
                is_reentry = True
                if source_lot.status == "CLOSED":
                    # Re-entering a CLOSED position: reuse the existing row in
                    # place instead of adding a new one, so no duplicate lot
                    # appears in the UI. Its prior booked result rides forward as
                    # carried_pnl (same "continue from where the last leg left
                    # off" math as a rollover, pnl.py:_lot_live_pnl).
                    carried_pnl = (source_lot.realized_pnl or 0.0) + (source_lot.carried_pnl or 0.0)
                    recycle_lot = source_lot
                # Re-entering an OPEN/PARTIAL lot instead creates a fresh row
                # (that position is still live), and carries no P&L — its own
                # realized_pnl isn't "done" yet.
            else:
                logger.warning(
                    "place_order: reentry_source_lot_id %s not found for user %s; placing without P&L carry",
                    req.reentry_source_lot_id, auth.user_id,
                )

        if recycle_lot is not None:
            # Preserve the closed round-trip's state in case the broker rejects
            # this re-entry and we have to restore the row (see _fail_lot below).
            reentry_snapshot = {
                "exch": recycle_lot.exch, "tsym": recycle_lot.tsym, "token": recycle_lot.token,
                "lotsize": recycle_lot.lotsize, "product_type": recycle_lot.product_type,
                "side": recycle_lot.side, "entry_qty": recycle_lot.entry_qty,
                "open_qty": recycle_lot.open_qty, "avg_entry_price": recycle_lot.avg_entry_price,
                "client_ref": recycle_lot.client_ref, "status": recycle_lot.status,
                "broker_entry_orderid": recycle_lot.broker_entry_orderid,
                "opened_at": recycle_lot.opened_at, "closed_at": recycle_lot.closed_at,
                "description": recycle_lot.description,
                "target_enabled": recycle_lot.target_enabled, "target_value": recycle_lot.target_value,
                "realized_pnl": recycle_lot.realized_pnl, "carried_pnl": recycle_lot.carried_pnl,
                "is_reentry": recycle_lot.is_reentry,
                "is_temp_exit": recycle_lot.is_temp_exit,
                "reentry_source_lot_id": recycle_lot.reentry_source_lot_id,
            }
            # Prior per-exit records belong to the finished round-trip; their net
            # is already folded into carried_pnl. Drop them so the reopened leg
            # starts with a clean Exit column and no stale exit average.
            for ex in list(recycle_lot.exits):
                db.delete(ex)
            recycle_lot.exch = req.exchange
            recycle_lot.tsym = req.tradingsymbol
            recycle_lot.token = tok
            recycle_lot.lotsize = lotsize
            recycle_lot.product_type = req.product_type
            recycle_lot.side = req.buy_or_sell
            recycle_lot.entry_qty = req.quantity
            recycle_lot.open_qty = req.quantity
            recycle_lot.avg_entry_price = req.price if req.price_type == "LMT" else 0.0
            recycle_lot.client_ref = client_ref
            recycle_lot.status = "PENDING"
            recycle_lot.broker_entry_orderid = ""
            recycle_lot.opened_at = datetime.utcnow()
            recycle_lot.closed_at = None
            recycle_lot.description = (req.description or "").strip()
            recycle_lot.target_enabled = bool(req.target_enabled and req.target_value > 0)
            recycle_lot.target_value = req.target_value if req.target_enabled else 0.0
            recycle_lot.realized_pnl = 0.0
            recycle_lot.carried_pnl = carried_pnl
            recycle_lot.is_reentry = True
            # Re-entering consumes the TE tag: the row is live again, so it no
            # longer needs the closed-today-filter exemption (the tag "changes").
            recycle_lot.is_temp_exit = False
            recycle_lot.reentry_source_lot_id = recycle_lot.id
            recycle_lot.source_service = source_service
            lot = recycle_lot
        else:
            lot = OrderLot(
                owner_uid=auth.user_id,
                exch=req.exchange,
                tsym=req.tradingsymbol,
                token=tok,
                lotsize=lotsize,
                product_type=req.product_type,
                side=req.buy_or_sell,
                entry_qty=req.quantity,
                open_qty=req.quantity,
                avg_entry_price=req.price if req.price_type == "LMT" else 0.0,
                client_ref=client_ref,
                status="PENDING",
                description=(req.description or "").strip(),
                # Per-order target rides with this lot; it goes live once the lot
                # fills (a PENDING lot can't auto-exit — there's nothing open yet).
                target_enabled=bool(req.target_enabled and req.target_value > 0),
                target_value=req.target_value if req.target_enabled else 0.0,
                carried_pnl=carried_pnl,
                is_reentry=is_reentry,
                reentry_source_lot_id=req.reentry_source_lot_id if is_reentry else None,
                source_service=source_service,
            )
            db.add(lot)
        db.commit()
        db.refresh(lot)

    payload = req.dict()
    payload.pop("description", None)       # broker payload doesn't take these
    payload.pop("target_enabled", None)
    payload.pop("target_value", None)
    payload.pop("reentry_source_lot_id", None)
    if lot is not None:
        payload["remarks"] = lot.client_ref  # broker echoes this back in order book

    def _fail_lot():
        """Undo the DB row after a broker REJECTION — the broker answered, and
        said no, so no order exists. A recycled CLOSED lot is restored to its
        finished state (carried_pnl keeps the net P&L); a fresh lot never
        represented a real position, so it's just marked CANCELLED.

        Only for a definite rejection. See _unresolved_lot for the case where we
        never heard back.
        """
        if lot is None or db is None:
            return
        if reentry_snapshot is not None:
            for field, value in reentry_snapshot.items():
                setattr(lot, field, value)
        else:
            lot.status = "CANCELLED"
        db.commit()

    def _unresolved_lot(why: str):
        """We did not hear back — the order may be resting or filled.

        Marking this CANCELLED is a lie the system cannot take back: the row is
        written off while the broker holds a real position, and because no order
        id was ever saved nothing links the two afterwards. Leave it PENDING and
        say so, so reconciliation and a human both still have something to find.
        """
        if lot is None or db is None:
            return
        lot.status = "PENDING"
        lot.description = ((lot.description or "") +
                           f" [unresolved placement: {why}. The order may be live at the broker —"
                           " verify before re-placing.]")[:2000]
        db.commit()

    try:
        result = await broker.placeOrder(token, credentials, payload)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            _fail_lot()
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order placement failed"))
        order_id = result.get("norenordno", "") if isinstance(result, dict) else str(result)

        if lot is not None and db is not None:
            lot.broker_entry_orderid = order_id
            db.commit()

        sm = get_scripmaster()
        tok = sm.get_token(req.exchange, req.tradingsymbol)
        if tok:
            state._auto_exited_tokens.discard(f"{req.exchange}|{tok}")

        asyncio.create_task(_short_poll_reconcile(auth.user_id))
        return {"status": "success", "order_id": order_id, "lot_id": (lot.id if lot else None)}
    except HTTPException:
        raise
    except BrokerError as e:
        # `raw` is set only when the broker itself answered and refused. Without
        # it we never reached a verdict — the request may have been received.
        if e.raw:
            _fail_lot()
            logger.error("Broker rejected order: %s | payload: %s", e, payload)
            raise HTTPException(status_code=400, detail=f"Shoonya rejected the order: {e}")
        _unresolved_lot(str(e))
        logger.error("Order placement UNRESOLVED (no verdict from broker): %s | payload: %s", e, payload)
        raise HTTPException(status_code=502,
                            detail=f"No response from the broker ({e}). The order may already be live — "
                                   "check the order book before placing it again.")
    except Exception as e:
        _unresolved_lot(str(e))
        logger.exception("Order placement UNRESOLVED (transport failure) | payload: %s", payload)
        raise HTTPException(status_code=502,
                            detail=f"Could not confirm the order ({e}). It may already be live — "
                                   "check the order book before placing it again.")
    finally:
        if db is not None:
            db.close()


@router.get("/api/orders")
async def get_orders():
    """Get all orders (order book)."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    try:
        raw_orders = await broker.getOrderBook(token, credentials)
        await _reconcile_lots_from_orderbook(raw_orders, auth.user_id)
        orders = [
            OrderItem(
                norenordno=o.get("norenordno", ""),
                tsym=o.get("tsym", ""),
                exch=o.get("exch", ""),
                prd=o.get("prd", ""),
                trantype=o.get("trantype", ""),
                qty=o.get("qty", "0"),
                price=o.get("prc", o.get("price", "0")),
                pricetype=o.get("prctyp", o.get("pricetype", "")),
                status=o.get("status", ""),
                orderid=o.get("norenordno", ""),
                pytime=o.get("exch_tm", o.get("pytime", "")),
                exch_orderid=o.get("exchordid", o.get("exch_orderid", "")),
                # Execution truth — see OrderItem. Without these a caller cannot
                # tell a filled order from an untouched one and re-places exits
                # that have already executed.
                fillshares=str(o.get("fillshares", "") or "0"),
                avgprc=str(o.get("avgprc", "") or "0"),
                prc=str(o.get("prc", o.get("price", "")) or ""),
                rejreason=str(o.get("rejreason", "") or o.get("emsg", "") or ""),
                remarks=str(o.get("remarks", "") or ""),
            )
            for o in raw_orders
        ]
        return OrderBookResponse(orders=orders)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching orders: {e}")


@router.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    """Cancel an order."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    try:
        result = await broker.cancelOrder(token, credentials, order_id)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order cancellation failed"))
        return {"status": "cancelled", "order_id": order_id}
    except HTTPException:
        raise
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error cancelling order: {e}")


@router.put("/api/orders/{order_id}")
async def modify_order(order_id: str, req: dict):
    """Modify an existing order."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    try:
        result = await broker.modifyOrder(token, credentials, order_id, req)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order modification failed"))

        new_price = req.get("newprice")
        if new_price is not None:
            try:
                price_val = float(new_price)
                db = next(get_db())
                try:
                    lot = db.query(OrderLot).filter(
                        OrderLot.broker_entry_orderid == order_id,
                        OrderLot.status == "PENDING",
                    ).first()
                    if lot:
                        lot.avg_entry_price = price_val
                        db.commit()
                finally:
                    db.close()
            except Exception:
                logger.warning("modify_order: failed to sync price to lot for order %s", order_id)

        return {"status": "modified", "order_id": order_id}
    except HTTPException:
        raise
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error modifying order: {e}")

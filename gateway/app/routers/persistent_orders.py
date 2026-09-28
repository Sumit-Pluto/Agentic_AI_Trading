import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import _require_legacy_auth
from app.schemas import PersistentOrderCreate, PersistentOrderResponse
from app.services.persistent_orders import (
    _serialize_persistent_order,
    _submit_persistent_attempt,
)
from brokers.base import SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import OrderLot, PersistentOrder
from session_windows import is_session_open

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/persistent-orders", response_model=PersistentOrderResponse, status_code=201)
async def create_persistent_order(req: PersistentOrderCreate, db: Session = Depends(get_db)):
    """Create a hold-until-filled entry order. If the venue is currently open,
    a DAY order is submitted immediately; otherwise the sweeper picks it up
    at the next session open."""
    auth = _require_legacy_auth()
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    if req.buy_or_sell not in ("B", "S"):
        raise HTTPException(status_code=400, detail="buy_or_sell must be B or S")

    sm = get_scripmaster()
    tok = sm.get_token(req.exchange, req.tradingsymbol) or ""
    scrip = sm.master.get(f"{req.exchange}|{req.tradingsymbol}", {}) if hasattr(sm, "master") else {}
    try:
        lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
    except Exception:
        lotsize = 1

    po = PersistentOrder(
        owner_uid=auth.user_id,
        exch=req.exchange,
        tsym=req.tradingsymbol,
        token=tok,
        lotsize=lotsize,
        side=req.buy_or_sell,
        product_type=req.product_type,
        price_type=req.price_type,
        price=req.price,
        trigger_price=req.trigger_price,
        quantity=req.quantity,
        target_enabled=req.target_enabled,
        target_value=req.target_value,
        status="ACTIVE",
        description=(req.description or "").strip(),
    )
    db.add(po)
    db.commit()
    db.refresh(po)

    # Try to submit right away if the venue is open.
    if is_session_open(req.exchange):
        await _submit_persistent_attempt(po)
        db.refresh(po)

    return _serialize_persistent_order(po)


@router.get("/api/persistent-orders", response_model=list[PersistentOrderResponse])
async def list_persistent_orders(db: Session = Depends(get_db)):
    auth = _require_legacy_auth()
    rows = (
        db.query(PersistentOrder)
        .filter(PersistentOrder.owner_uid == auth.user_id)
        .order_by(PersistentOrder.created_at.desc())
        .all()
    )
    return [_serialize_persistent_order(p) for p in rows]


@router.delete("/api/persistent-orders/{po_id}", response_model=PersistentOrderResponse)
async def cancel_persistent_order(po_id: int, db: Session = Depends(get_db)):
    """User-cancel a persistent order. Also cancels today's pending broker order
    if one is still live, so no stray fill sneaks in after the intent is retired."""
    auth = _require_legacy_auth()
    po = db.query(PersistentOrder).filter_by(id=po_id, owner_uid=auth.user_id).first()
    if po is None:
        raise HTTPException(status_code=404, detail="Persistent order not found")
    if po.status != "ACTIVE":
        raise HTTPException(status_code=400, detail=f"Cannot cancel; status is {po.status}")

    # Cancel any currently pending PENDING lot tied to this persistent order.
    pending_lot = (
        db.query(OrderLot)
        .filter(OrderLot.persistent_order_id == po.id, OrderLot.status == "PENDING")
        .filter(OrderLot.broker_entry_orderid != "")
        .first()
    )
    if pending_lot is not None:
        broker = get_broker("shoonya")
        token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id,
                             issued_at="", broker_name="shoonya")
        credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
        try:
            await broker.cancelOrder(token, credentials, pending_lot.broker_entry_orderid)
        except Exception as e:
            logger.warning("Persistent cancel: broker cancel failed for lot=%d (po=%d): %s",
                           pending_lot.id, po.id, e)
            # Continue anyway — mark the persistent intent as retired.

    po.status = "USER_CANCELLED"
    db.commit()
    db.refresh(po)
    return _serialize_persistent_order(po)

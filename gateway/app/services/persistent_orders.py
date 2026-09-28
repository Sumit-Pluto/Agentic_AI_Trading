"""Persistent-order helpers: today's submission attempt, day-boundary sweep,
DB-row → response DTO serialization."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from app.core import auth as _auth_module
from app.schemas import PersistentOrderResponse
from brokers.base import SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import OrderLot, PersistentOrder
from session_windows import is_session_open, today_ist, IST

logger = logging.getLogger(__name__)


def _serialize_persistent_order(po: PersistentOrder) -> PersistentOrderResponse:
    return PersistentOrderResponse(
        id=po.id,
        exch=po.exch,
        tsym=po.tsym,
        side=po.side,
        product_type=po.product_type or "M",
        price_type=po.price_type or "LMT",
        price=po.price or 0.0,
        trigger_price=po.trigger_price or 0.0,
        quantity=po.quantity,
        target_enabled=bool(po.target_enabled),
        target_value=po.target_value or 0.0,
        status=po.status,
        last_broker_orderid=po.last_broker_orderid or "",
        last_submitted_at=po.last_submitted_at.isoformat() if po.last_submitted_at else "",
        filled_lot_id=po.filled_lot_id,
        created_at=po.created_at.isoformat() if po.created_at else "",
    )


async def _submit_persistent_attempt(po: PersistentOrder) -> bool:
    """Place today's DAY order for one persistent-order intent.

    Creates a fresh PENDING lot tagged with a unique client_ref and links it
    back via `persistent_order_id`. Reuses the standard lot-reconcile pipeline
    — so on COMPLETE the lot becomes OPEN and inherits the target settings;
    on CANCELLED the lot becomes CANCELLED but the persistent row stays ACTIVE
    so the next day's sweep tries again.

    Returns True if the broker accepted the order.
    """
    from app.services.reconciliation import _short_poll_reconcile

    if _auth_module._auth is None or not _auth_module._auth.is_authenticated():
        logger.warning("Persistent submit skipped: not authenticated (po id=%d)", po.id)
        return False

    broker = get_broker("shoonya")
    session_token = SessionToken(token=_auth_module._auth.auth_token, broker_uid=_auth_module._auth.user_id,
                                 issued_at="", broker_name="shoonya")
    credentials = {"user_id": _auth_module._auth.user_id, "account_id": _auth_module._auth.user_id}

    sm = get_scripmaster()
    tok = sm.get_token(po.exch, po.tsym) or po.token or ""
    scrip = sm.master.get(f"{po.exch}|{po.tsym}", {}) if hasattr(sm, "master") else {}
    try:
        lotsize = max(int(scrip.get("lotsize", po.lotsize or 1) or 1), 1)
    except Exception:
        lotsize = po.lotsize or 1

    db = next(get_db())
    try:
        client_ref = f"lot{uuid.uuid4().hex[:12]}"
        lot = OrderLot(
            owner_uid=po.owner_uid,
            exch=po.exch,
            tsym=po.tsym,
            token=tok,
            lotsize=lotsize,
            product_type=po.product_type or "M",
            side=po.side,
            entry_qty=po.quantity,
            open_qty=po.quantity,
            avg_entry_price=po.price if po.price_type == "LMT" else 0.0,
            client_ref=client_ref,
            status="PENDING",
            persistent_order_id=po.id,
            description=po.description or "",
        )
        db.add(lot)
        db.commit()
        db.refresh(lot)

        payload = {
            "buy_or_sell": po.side,
            "product_type": po.product_type or "M",
            "exchange": po.exch,
            "tradingsymbol": po.tsym,
            "quantity": po.quantity,
            "discloseqty": 0,
            "price_type": po.price_type or "LMT",
            "price": po.price if po.price_type == "LMT" else 0,
            "trigger_price": po.trigger_price or 0,
            "retention": "DAY",
            "remarks": client_ref,
        }
        try:
            result = await broker.placeOrder(session_token, credentials, payload)
        except Exception as e:
            logger.error("Persistent submit failed for po=%d: %s", po.id, e)
            lot.status = "CANCELLED"
            db.commit()
            return False
        if isinstance(result, dict) and result.get("stat") != "Ok":
            logger.error("Persistent submit rejected for po=%d: %s", po.id, result.get("emsg", ""))
            lot.status = "CANCELLED"
            db.commit()
            return False
        order_id = result.get("norenordno", "") if isinstance(result, dict) else str(result)
        lot.broker_entry_orderid = order_id
        # Refresh the persistent row on this same session so the updated
        # fields are committed atomically with the lot.
        po_live = db.query(PersistentOrder).filter_by(id=po.id).first()
        if po_live is not None:
            po_live.last_broker_orderid = order_id
            po_live.last_submitted_at = datetime.utcnow()
        db.commit()
        logger.info("Persistent po=%d submitted: order_id=%s qty=%d price=%.2f",
                    po.id, order_id, po.quantity, po.price or 0.0)
        asyncio.create_task(_short_poll_reconcile(_auth_module._auth.user_id))
        return True
    finally:
        db.close()


async def _sweep_persistent_orders() -> None:
    """Runs on a 60s cadence. Submits a fresh DAY order for every ACTIVE
    persistent row whose venue is currently open and that hasn't been submitted
    today (IST)."""
    if _auth_module._auth is None or not _auth_module._auth.is_authenticated():
        return
    db = next(get_db())
    try:
        rows = (
            db.query(PersistentOrder)
            .filter(PersistentOrder.owner_uid == _auth_module._auth.user_id)
            .filter(PersistentOrder.status == "ACTIVE")
            .all()
        )
    finally:
        db.close()

    today = today_ist()
    for po in rows:
        if not is_session_open(po.exch):
            continue
        # Already submitted today? Skip until next session.
        if po.last_submitted_at is not None:
            # Cheap "same trading day" check: last-attempt IST date == today IST.
            last_utc = po.last_submitted_at.replace(tzinfo=timezone.utc)
            if last_utc.astimezone(IST).date() == today:
                continue
        await _submit_persistent_attempt(po)

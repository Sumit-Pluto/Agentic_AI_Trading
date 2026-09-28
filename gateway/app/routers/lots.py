import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, time as _time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import Session

from app.core import state
from app.core.deps import _require_legacy_auth
from app.schemas import (
    LotExitRequest,
    LotRolloverRequest,
    LotTargetUpdate,
    LotTempExitUpdate,
    OrderLotResponse,
    RolloverFailure,
    ScripSearchResult,
)
from app.services.lots import _serialize_lot
from app.services.reconciliation import _norm_prd, _short_poll_reconcile
from app.services.targets import _load_lot_targets
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import LotExit, OrderLot, PersistentOrder, RolloverIntent
from ticker_manager import ticker_manager

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/lots/cleanup-external")
async def cleanup_external_lots():
    """Cancel is_external lots that don't correspond to an actual open broker position.

    Uses broker positions as ground truth. For each symbol:
      - allowed_ext_net = broker_netqty - non_external_lot_net
      - External lots are kept (oldest first) up to that allowed qty.
      - Any external lot beyond the allowed qty is deleted from the DB.

    Returns the number of lots cancelled.
    """
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    try:
        raw_positions = await broker.getPositions(token, credentials)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch positions: {e}")

    # Build broker net-qty map: (exch, tsym, prd) -> int. Broker rows are
    # per-product; keying by symbol alone would let one product's row
    # overwrite another's for the same symbol.
    positions_net: dict[tuple, int] = {}
    for pos in (raw_positions or []):
        exch = (pos.get("exch") or "").strip()
        tsym = (pos.get("tsym") or "").strip()
        if not exch or not tsym:
            continue
        try:
            netqty = int(float(pos.get("netqty", 0) or 0))
        except (TypeError, ValueError):
            netqty = 0
        positions_net[(exch, tsym, _norm_prd(pos.get("prd")))] = netqty

    db = next(get_db())
    cancelled_count = 0
    try:
        open_statuses = ["OPEN", "PARTIAL"]
        all_open_lots = (
            db.query(OrderLot)
            .filter(OrderLot.owner_uid == auth.user_id, OrderLot.status.in_(open_statuses))
            .all()
        )

        # Group by symbol + product — each product's lots are judged against
        # that product's broker net, not the whole symbol's.
        by_sym: dict[tuple, list] = defaultdict(list)
        for lot in all_open_lots:
            by_sym[(lot.exch, lot.tsym, _norm_prd(lot.product_type))].append(lot)

        for (exch, tsym, prd), lots in by_sym.items():
            broker_net = positions_net.get((exch, tsym, prd), 0)

            # Net contribution of non-external lots
            non_ext_net = sum(
                (1 if l.side == "B" else -1) * (l.open_qty or 0)
                for l in lots if not l.is_external
            )

            # How much net position is left for external lots to account for
            allowed_ext_net = broker_net - non_ext_net

            ext_lots = sorted(
                [l for l in lots if l.is_external],
                key=lambda l: l.opened_at or datetime.min,
            )

            accumulated = 0
            for lot in ext_lots:
                sign = 1 if lot.side == "B" else -1
                contribution = sign * (lot.open_qty or 0)

                # Keep this lot if it moves accumulated toward allowed_ext_net
                # and doesn't overshoot in the wrong direction.
                fits = False
                if allowed_ext_net == 0:
                    fits = False  # broker has no room for any external lot here
                elif sign == (1 if allowed_ext_net > 0 else -1):
                    # Same direction as allowed → keep if within limit
                    fits = abs(accumulated + contribution) <= abs(allowed_ext_net)
                # opposite sign → definitely phantom, fits = False

                if fits:
                    accumulated += contribution
                else:
                    db.delete(lot)
                    cancelled_count += 1
                    logger.info(
                        "Deleted phantom external lot id=%d %s %s qty=%d "
                        "(broker_net=%d non_ext_net=%d allowed=%d)",
                        lot.id, lot.side, tsym, lot.open_qty,
                        broker_net, non_ext_net, allowed_ext_net,
                    )

        db.commit()
    except Exception:
        logger.exception("cleanup_external_lots failed")
        db.rollback()
        raise HTTPException(status_code=500, detail="Cleanup failed")
    finally:
        db.close()

    return {"status": "ok", "cancelled": cancelled_count}


@router.get("/api/lots", response_model=list[OrderLotResponse])
async def get_lots(db: Session = Depends(get_db)):
    """Return open lots + lots closed today + TE-tagged lots, for the active broker user."""
    auth = _require_legacy_auth()
    today_start = datetime.combine(datetime.utcnow().date(), _time.min)
    rows = (
        db.query(OrderLot)
        .filter(OrderLot.owner_uid == auth.user_id)
        .filter(
            (OrderLot.status.in_(["PENDING", "OPEN", "PARTIAL"]))
            | (OrderLot.closed_at >= today_start)
            # TE ("temporary exit") lots stay visible across days so the user can
            # re-enter / roll them the next session — cleared on re-entry.
            | (OrderLot.is_temp_exit.is_(True))
        )
        .order_by(OrderLot.opened_at.desc())
        .all()
    )
    # Suppress internal cancelled shells — persistent-order EOD attempts and
    # phantom external lots cancelled by the cleanup endpoint.
    rows = [
        l for l in rows
        if not (l.persistent_order_id and l.status == "CANCELLED")
        and not (l.is_external and l.status == "CANCELLED")
    ]
    # Ensure bid/ask streams for any open lot, so live_pnl isn't stuck on stale
    # data when the user opens the Orders card before /api/positions has run.
    lot_keys = [f"{l.exch}|{l.token}" for l in rows
                if l.token and l.status in ("OPEN", "PARTIAL")]
    if lot_keys:
        ticker_manager.subscribe(list(set(lot_keys)))
    return [_serialize_lot(l) for l in rows]


@router.get("/api/lots/history", response_model=list[OrderLotResponse])
async def get_lots_history(
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Return all-time order history (every lot ever recorded), newest first.

    Unlike `/api/lots` (open + closed-today only), this is not filtered by
    date — lots are never deleted from the DB once closed, so this reflects
    the complete execution history for the active broker user.
    """
    auth = _require_legacy_auth()
    limit = max(1, min(limit, 1000))
    rows = (
        db.query(OrderLot)
        .filter(OrderLot.owner_uid == auth.user_id)
        .filter(
            not_(
                and_(OrderLot.status == "CANCELLED", or_(
                    OrderLot.persistent_order_id.isnot(None),
                    OrderLot.is_external.is_(True),
                ))
            )
        )
        .order_by(OrderLot.opened_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_serialize_lot(l) for l in rows]


@router.delete("/api/lots/{lot_id}")
async def delete_lot(lot_id: int, db: Session = Depends(get_db)):
    """Permanently delete a single lot (and its exits) from the DB.

    For clearing wrongly-synced / stale lots the corrected sync can't
    self-heal — a wrong-priced or phantom lot already in the DB makes every
    later sync compute delta=0 and leave it untouched. This removes only the
    local record; it never touches the broker. If the lot reflects a real
    open broker position, the next "Sync Positions" re-imports it with the
    broker's correct side/qty/avg price.
    """
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")

    # Capture identity for the audit log before the row is expired by delete.
    lot_desc = (f"{lot.side} {lot.tsym} qty={lot.open_qty}/{lot.entry_qty} "
                f"status={lot.status} ext={lot.is_external}")

    # A persistent order may point back at this lot via filled_lot_id — clear
    # it so the delete doesn't leave a dangling reference.
    for po in db.query(PersistentOrder).filter_by(filled_lot_id=lot_id).all():
        po.filled_lot_id = None

    try:
        db.delete(lot)  # cascade="all, delete-orphan" removes this lot's exits
        db.commit()
    except Exception:
        logger.exception("delete_lot failed for id=%d", lot_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Delete failed")

    logger.info("Deleted lot id=%d %s (manual UI delete)", lot_id, lot_desc)
    return {"status": "ok", "deleted": lot_id}


@router.post("/api/lots/{lot_id}/exit", response_model=OrderLotResponse)
async def exit_lot(lot_id: int, req: LotExitRequest, db: Session = Depends(get_db)):
    """Place a partial/full exit order against a specific lot."""
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    if lot.status not in ("OPEN", "PARTIAL"):
        raise HTTPException(status_code=400, detail=f"Cannot exit lot in status {lot.status}")
    if req.qty <= 0 or req.qty > lot.open_qty:
        raise HTTPException(status_code=400, detail=f"qty must be between 1 and {lot.open_qty}")

    # Tag TE up front — stamped on the lot regardless of when it actually closes,
    # so a partial exit that later fully closes still keeps the row alive.
    if req.temp_exit:
        lot.is_temp_exit = True

    exit_side = "S" if lot.side == "B" else "B"
    price = req.price
    if req.price_type == "LMT" and price <= 0 and lot.token:
        key = f"{lot.exch}|{lot.token}"
        # Long lots exit on the bid; short lots exit on the ask
        price = (state._current_bids.get(key) if lot.side == "B" else state._current_asks.get(key)) or state._current_ltps.get(key, 0.0)

    client_ref = f"lotexit{uuid.uuid4().hex[:10]}"
    ex = LotExit(lot_id=lot.id, exit_qty=req.qty, client_ref=client_ref, status="PENDING")
    db.add(ex)
    db.commit()
    db.refresh(ex)

    broker = get_broker("shoonya")
    session_token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    order_payload = {
        "buy_or_sell": exit_side,
        "product_type": lot.product_type or "M",
        "exchange": lot.exch,
        "tradingsymbol": lot.tsym,
        "quantity": req.qty,
        "discloseqty": 0,
        "price_type": req.price_type,
        "price": price if req.price_type == "LMT" else 0,
        "trigger_price": 0,
        "retention": "DAY",
        "remarks": client_ref,
    }
    try:
        result = await broker.placeOrder(session_token, credentials, order_payload)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            ex.status = "REJECTED"
            db.commit()
            raise HTTPException(status_code=400, detail=result.get("emsg", "Exit order rejected"))
        ex.broker_exit_orderid = result.get("norenordno", "") if isinstance(result, dict) else str(result)
        db.commit()
    except HTTPException:
        raise
    except BrokerError as e:
        ex.status = "REJECTED"
        db.commit()
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        ex.status = "REJECTED"
        db.commit()
        raise HTTPException(status_code=502, detail=f"Error placing exit: {e}")

    asyncio.create_task(_short_poll_reconcile(auth.user_id))
    db.refresh(lot)
    return _serialize_lot(lot)


@router.delete("/api/lots/{lot_id}/exit", response_model=OrderLotResponse)
async def cancel_lot_exit(lot_id: int, db: Session = Depends(get_db)):
    """Cancel the pending exit order for a lot."""
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")

    pending_exit = next((ex for ex in lot.exits if ex.status == "PENDING"), None)
    if pending_exit is None:
        raise HTTPException(status_code=404, detail="No pending exit found for this lot")

    if pending_exit.broker_exit_orderid:
        broker = get_broker("shoonya")
        token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
        credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
        try:
            result = await broker.cancelOrder(token, credentials, pending_exit.broker_exit_orderid)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise HTTPException(status_code=400, detail=result.get("emsg", "Exit cancellation failed"))
        except HTTPException:
            raise
        except BrokerError as e:
            raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Error cancelling exit: {e}")

    pending_exit.status = "CANCELLED"
    db.commit()
    db.refresh(lot)
    return _serialize_lot(lot)


@router.put("/api/lots/{lot_id}/target", response_model=OrderLotResponse)
async def update_lot_target(lot_id: int, req: LotTargetUpdate, db: Session = Depends(get_db)):
    """Set/clear this lot's own per-order auto-exit target.

    Independent of the symbol/exchange targets — when THIS lot's live P&L
    (entry → touch) reaches target_value, only this lot's quantity is exited,
    so two batches of the same symbol can carry different targets.
    """
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    if req.enabled is not None:
        lot.target_enabled = req.enabled
    if req.target_value is not None:
        lot.target_value = req.target_value
    # A ₹0 (or negative) target would fire the instant P&L touches breakeven.
    if lot.target_enabled and (lot.target_value or 0) <= 0:
        db.rollback()
        raise HTTPException(status_code=400, detail="Target value must be positive to enable")
    db.commit()
    db.refresh(lot)
    # Re-arm this lot (a prior-session hit may have latched it) and rebuild the
    # per-order cache so the tick loop prices it right away.
    state._lot_target_exited.discard(lot.id)
    _load_lot_targets()
    return _serialize_lot(lot)


@router.put("/api/lots/{lot_id}/temp-exit", response_model=OrderLotResponse)
async def set_temp_exit(lot_id: int, req: LotTempExitUpdate, db: Session = Depends(get_db)):
    """Set/clear the TE (temporary-exit) tag on a lot after the fact.

    A TE-tagged lot stays visible on the Orders card across days (it's exempt
    from the closed-today filter in `/api/lots`), so a position closed to be
    re-entered/rolled the next session doesn't drop off the list. Re-entry
    clears the tag automatically.
    """
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    lot.is_temp_exit = bool(req.enabled)
    db.commit()
    db.refresh(lot)
    return _serialize_lot(lot)


@router.get("/api/lots/{lot_id}/roll-targets", response_model=list[ScripSearchResult])
async def roll_targets(lot_id: int, db: Session = Depends(get_db)):
    """Future contracts this lot could roll into: same underlying, later
    expiries, nearest-first. Empty when the lot is already the last listed
    expiry (or the contract is unknown)."""
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    rows = get_scripmaster().next_expiries(lot.exch, lot.tsym)
    return [
        ScripSearchResult(
            tsym=r.get("tsym", ""),
            exch=r.get("exch", ""),
            token=r.get("token", ""),
            instrumenttype=r.get("instrumenttype", ""),
            expd=r.get("expd", ""),
            opttype=r.get("opttype", ""),
            strikeprice=r.get("strikeprice", ""),
            lotsize=r.get("lotsize", ""),
            sym=r.get("sym", ""),
        )
        for r in rows
    ]


@router.post("/api/lots/{lot_id}/rollover")
async def rollover_lot(lot_id: int, req: LotRolloverRequest, db: Session = Depends(get_db)):
    """Roll a futures position to a later expiry — hands-off.

    Places the exit order on the near contract, then records a RolloverIntent.
    The far (entry) leg is placed AUTOMATICALLY by the reconciliation path
    (_fire_ready_rollovers) the moment the near exit fills — however long that
    takes — for the actually-filled quantity, carrying an enabled symbol target
    across. This endpoint returns as soon as the exit is placed; it does not
    block waiting for the fill.
    """
    auth = _require_legacy_auth()
    lot = db.query(OrderLot).filter_by(id=lot_id, owner_uid=auth.user_id).first()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    if lot.status not in ("OPEN", "PARTIAL"):
        raise HTTPException(status_code=400, detail=f"Cannot roll lot in status {lot.status}")

    roll_qty = req.qty or lot.open_qty
    if roll_qty <= 0 or roll_qty > lot.open_qty:
        raise HTTPException(status_code=400, detail=f"qty must be between 1 and {lot.open_qty}")

    # A lot mid-exit must not be rolled — its close is already in flight.
    if any(ex.status == "PENDING" for ex in lot.exits):
        raise HTTPException(status_code=400, detail="Lot already has a pending exit; cancel it before rolling")

    sm = get_scripmaster()
    # Resolve the far contract BEFORE placing the exit — if there's nowhere to
    # roll, don't touch the near position.
    if req.target_tsym:
        far = sm.master.get(f"{lot.exch}|{req.target_tsym}")
        if not far:
            raise HTTPException(status_code=400, detail=f"Unknown target contract {req.target_tsym}")
    else:
        candidates = sm.next_expiries(lot.exch, lot.tsym)
        if not candidates:
            raise HTTPException(status_code=400, detail="No later expiry available to roll into")
        far = candidates[0]
    far_tsym = far.get("tsym", "")
    far_token = far.get("token", "")
    far_expd = far.get("expd", "")

    price_type = "MKT" if (req.price_type or "").upper() == "MKT" else "LMT"
    partial_roll = roll_qty < lot.open_qty

    # Capture scalars up front (the objects may expire across the commit below).
    lot_side = lot.side
    lot_product = lot.product_type or "M"
    lot_exch = lot.exch
    lot_tsym = lot.tsym

    # Snapshot THIS lot's own per-order target so the far leg can inherit it on
    # fire — targets ride with the individual lot, not the symbol.
    near_target_enabled = bool(lot.target_enabled)
    near_target_value = lot.target_value or 0.0

    # --- Place the near exit (reuse exit_lot). Raises HTTPException on reject
    # → no intent is created, nothing else happens. ---
    pre_ids = {ex.id for ex in lot.exits}
    await exit_lot(
        lot_id,
        LotExitRequest(qty=roll_qty, price_type=price_type, price=req.exit_price),
        db,
    )
    exit_q = db.query(LotExit).filter(LotExit.lot_id == lot_id)
    if pre_ids:
        exit_q = exit_q.filter(LotExit.id.notin_(pre_ids))
    my_exit = exit_q.order_by(LotExit.id.desc()).first()
    if my_exit is None:
        raise HTTPException(status_code=500, detail="Rollover: exit order placed but could not be tracked")

    # --- Record the intent: the far leg fires automatically on exit fill. ---
    intent = RolloverIntent(
        owner_uid=auth.user_id,
        lot_id=lot_id,
        exit_id=my_exit.id,
        exch=lot_exch,
        near_tsym=lot_tsym,
        far_tsym=far_tsym,
        far_token=far_token,
        side=lot_side,
        product_type=lot_product,
        price_type=price_type,
        entry_price=(req.entry_price if price_type == "LMT" else 0.0),
        carry_target=req.carry_target,
        carry_pnl=req.carry_pnl,
        near_target_enabled=near_target_enabled,
        near_target_value=near_target_value,
        full_roll=(not partial_roll),
        status="PENDING",
    )
    db.add(intent)
    db.commit()

    # exit_lot already scheduled a short-poll reconcile; when the exit fills,
    # _fire_ready_rollovers places the far leg (fast for a market exit, whenever
    # it fills for a limit exit).
    return {
        "status": "rolling",
        "near": {"lot_id": lot_id, "tsym": lot_tsym, "exit_qty": roll_qty},
        "far": {"tsym": far_tsym, "expd": far_expd},
        "message": f"Exit placed for {lot_tsym}. {far_tsym} will be placed automatically the moment the exit fills.",
    }


@router.get("/api/rollover-intents/failures", response_model=list[RolloverFailure])
async def rollover_failures(db: Session = Depends(get_db)):
    """Rollovers whose far leg auto-failed after the near leg closed and that
    the user hasn't dismissed. Surfaced as banners so a failure the user missed
    (e.g. a slow limit exit that filled while they were away) is still seen."""
    auth = _require_legacy_auth()
    rows = (
        db.query(RolloverIntent)
        .filter_by(owner_uid=auth.user_id, status="FAILED", acknowledged=False)
        .order_by(RolloverIntent.id.desc())
        .limit(20)
        .all()
    )
    return [
        RolloverFailure(
            id=r.id,
            near_tsym=r.near_tsym,
            far_tsym=r.far_tsym,
            error=r.error or "",
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.post("/api/rollover-intents/{intent_id}/ack")
async def ack_rollover_intent(intent_id: int, db: Session = Depends(get_db)):
    """Dismiss a failed-rollover banner."""
    auth = _require_legacy_auth()
    it = db.query(RolloverIntent).filter_by(id=intent_id, owner_uid=auth.user_id).first()
    if it is None:
        raise HTTPException(status_code=404, detail="Rollover intent not found")
    it.acknowledged = True
    db.commit()
    return {"status": "ok", "id": intent_id}



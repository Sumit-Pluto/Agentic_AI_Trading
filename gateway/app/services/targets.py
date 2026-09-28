"""Target P&L services: rebuild caches from broker/DB, fire auto-exit orders,
handle every WebSocket tick for per-symbol / per-exchange total-P&L triggers."""
import asyncio
import logging
import uuid

from fastapi import HTTPException

from app.core import state
from app.core.deps import _require_legacy_auth
from app.services.pnl import (
    _calc_exchange_pnls,
    _compute_position_pnl,
    _exit_price,
    _normalize_broker_position,
)
from brokers.base import SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import LotExit, OrderLot, SymbolTarget
from session_windows import is_market_open_for_orders
from ticker_manager import ticker_manager

logger = logging.getLogger(__name__)


def _norm_prd(value) -> str:
    """Mirror of reconciliation._norm_prd (module-level import would be circular)."""
    return (value or "").strip().upper() or "M"


# Auto-exit watchdog tuning (module-level so tests can shrink them).
_WATCH_DELAY_S = 3.0     # seconds between order-status checks
_WATCH_MAX_CHECKS = 5    # status checks (≈ price chases) before pulling the order


async def _load_target_positions() -> bool:
    """Fetch open positions and rebuild token-keyed maps for target checking.

    Returns True when the caches were rebuilt from a fresh broker snapshot,
    False when they were left untouched (not authenticated / fetch failed) —
    callers that re-arm a trigger afterwards must not do so on False, or the
    next trigger sizes its exit off a stale netqty.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        return False
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    try:
        raw = await broker.getPositions(token, credentials)
    except Exception as e:
        logger.warning("_load_target_positions: failed to fetch positions: %s", e)
        return False

    sm = get_scripmaster()
    db = next(get_db())
    try:
        db_targets = {(st.exch, st.tsym): st for st in db.query(SymbolTarget).all()}
    finally:
        db.close()

    # Keep flat symbols (netqty==0) too — their day realized P&L is needed for
    # the global cap to include intraday round-trips that have been closed out.
    # Subscribing flat symbols to the ticker is harmless (extra ticks, no auto-
    # exit because netqty==0 short-circuits the per-symbol check and
    # `_place_exit_orders` skips zero-qty rows).
    new_positions: dict[str, list[dict]] = {}
    new_targets: dict[str, dict] = {}
    subscribe_keys: list[str] = []
    for pos in raw:
        tsym = pos.get("tsym", "")
        exch = pos.get("exch", "")
        tok = sm.get_token(exch, tsym)
        if not tok:
            continue
        key = f"{exch}|{tok}"
        scrip = sm.master.get(f"{exch}|{tsym}", {})
        lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
        entry = _normalize_broker_position(pos)
        entry["lotsize"] = lotsize
        entry["prcftr"] = float(scrip.get("prcftr", "1") or "1")
        new_positions.setdefault(key, []).append(entry)
        if entry["netqty"] != 0:
            subscribe_keys.append(key)
        st = db_targets.get((exch, tsym))
        if st:
            new_targets[key] = {"enabled": st.enabled, "target_value": st.target_value, "carried_pnl": st.carried_pnl or 0.0}

    state._target_positions.clear()
    state._target_positions.update(new_positions)
    state._symbol_targets.clear()
    state._symbol_targets.update(new_targets)
    if subscribe_keys:
        ticker_manager.subscribe(subscribe_keys)
    logger.info("_load_target_positions: loaded %d symbol(s)", len(new_positions))
    return True


def _load_lot_targets() -> None:
    """Rebuild the per-order (per-lot) target cache from the DB.

    Every OPEN/PARTIAL lot with target_enabled and a positive target gets an
    entry keyed by its EXCH|TOKEN, so the tick loop can value it off that
    symbol's price feed and fire an exit sized to that single lot when its OWN
    live P&L hits its target. This is independent of the per-symbol / per-
    exchange targets, which act on the aggregate broker (net) position.

    Cheap and synchronous (DB + scripmaster + a ticker subscribe, no broker
    round-trip) so it can run inline after every reconcile.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        state._lot_targets.clear()
        state._lot_target_exited.clear()
        return

    sm = get_scripmaster()
    db = next(get_db())
    try:
        lots = (
            db.query(OrderLot)
            .filter(OrderLot.owner_uid == auth.user_id)
            .filter(OrderLot.status.in_(("OPEN", "PARTIAL")))
            .filter(OrderLot.target_enabled.is_(True))
            .filter(OrderLot.target_value > 0)
            .all()
        )
        new_map: dict[str, list[dict]] = {}
        subscribe_keys: list[str] = []
        live_ids: set[int] = set()
        for lot in lots:
            if not lot.token or (lot.open_qty or 0) <= 0:
                continue
            key = f"{lot.exch}|{lot.token}"
            scrip = sm.master.get(f"{lot.exch}|{lot.tsym}", {})
            prcftr = float(scrip.get("prcftr", "1") or "1")
            new_map.setdefault(key, []).append({
                "lot_id": lot.id,
                "exch": lot.exch,
                "tsym": lot.tsym,
                "token": lot.token,
                "side": lot.side,
                "product_type": lot.product_type or "M",
                "avg_entry_price": lot.avg_entry_price or 0.0,
                "open_qty": lot.open_qty or 0,
                "prcftr": prcftr,
                # realized+carried P&L already banked on this lot counts toward
                # its target. Carry is counted exactly once, matching the Orders
                # card "Live P&L" display (live_pnl = realized + carried + unrealized).
                "realized": (lot.realized_pnl or 0.0) + (lot.carried_pnl or 0.0),
                "target_value": lot.target_value or 0.0,
            })
            subscribe_keys.append(key)
            live_ids.add(lot.id)
    finally:
        db.close()

    state._lot_targets.clear()
    state._lot_targets.update(new_map)
    # Drop latches for lots no longer armed/open, so a fired-then-closed lot
    # doesn't leave its id stuck exited (ids are never reused, but this keeps
    # the set from growing unbounded across the session).
    state._lot_target_exited.intersection_update(live_ids)
    if subscribe_keys:
        ticker_manager.subscribe(list(set(subscribe_keys)))


def _mark_lot_exit_rejected(client_ref: str) -> None:
    """Flip a just-created PENDING LotExit to REJECTED when its order never
    reached the broker — so it isn't counted as an in-flight exit."""
    db = next(get_db())
    try:
        ex = db.query(LotExit).filter_by(client_ref=client_ref).first()
        if ex and ex.status == "PENDING":
            ex.status = "REJECTED"
            db.commit()
    except Exception:
        logger.exception("Failed to mark lot exit %s rejected", client_ref)
    finally:
        db.close()


async def _auto_exit_lot(lt: dict, price: float) -> bool:
    """Place a touch-priced exit for one lot when its per-order target fires.

    Reuses the manual lot-exit machinery so the fill is attributed correctly:
    a PENDING LotExit tagged with a client_ref, a LMT order at the touch (best
    bid for a long exit, best ask for a short), then a short-poll reconcile.
    Sized to the lot's own open_qty — a partial exit of the net broker
    position. Returns True when the exit order was accepted by the broker.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        logger.error("Per-order auto-exit: not authenticated, skipping lot %d", lt["lot_id"])
        return False

    db = next(get_db())
    try:
        lot = db.query(OrderLot).filter_by(id=lt["lot_id"], owner_uid=auth.user_id).first()
        if lot is None or lot.status not in ("OPEN", "PARTIAL") or (lot.open_qty or 0) <= 0:
            return False
        if any(ex.status == "PENDING" for ex in lot.exits):
            return False  # an exit is already resting for this lot
        qty = lot.open_qty
        exit_side = "S" if lot.side == "B" else "B"
        product = lot.product_type or "M"
        exch, tsym = lot.exch, lot.tsym
        client_ref = f"lotexit{uuid.uuid4().hex[:10]}"
        ex = LotExit(lot_id=lot.id, exit_qty=qty, client_ref=client_ref, status="PENDING")
        db.add(ex)
        db.commit()
    finally:
        db.close()

    broker = get_broker("shoonya")
    session_token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    order = {
        "buy_or_sell": exit_side,
        "product_type": "I" if product == "I" else "M",
        "exchange": exch,
        "tradingsymbol": tsym,
        "quantity": qty,
        "discloseqty": 0,
        "price_type": "LMT",
        "price": price,
        "trigger_price": 0,
        "retention": "DAY",
        "remarks": client_ref,
    }
    try:
        result = await broker.placeOrder(session_token, credentials, order)
    except Exception as e:
        logger.error("Per-order auto-exit order failed for lot %d (%s): %s", lt["lot_id"], tsym, e)
        _mark_lot_exit_rejected(client_ref)
        return False
    if isinstance(result, dict) and result.get("stat") != "Ok":
        logger.error("Per-order auto-exit rejected for lot %d (%s): %s",
                     lt["lot_id"], tsym, result.get("emsg", ""))
        _mark_lot_exit_rejected(client_ref)
        return False

    order_id = result.get("norenordno", "") if isinstance(result, dict) else ""
    if order_id:
        db2 = next(get_db())
        try:
            ex2 = db2.query(LotExit).filter_by(client_ref=client_ref).first()
            if ex2:
                ex2.broker_exit_orderid = order_id
                db2.commit()
        finally:
            db2.close()
    # Lazy import to avoid the reconciliation ↔ targets import cycle.
    from app.services.reconciliation import _short_poll_reconcile
    asyncio.create_task(_short_poll_reconcile(auth.user_id))
    logger.info("Per-order target hit: lot %d %s — placed exit %s for qty %d @ ₹%.2f",
                lt["lot_id"], tsym, order_id, qty, price)
    return True


async def _place_exit_orders(positions_map: dict[str, list[dict]], remarks: str) -> dict[str, bool]:
    """Place exit orders for all positions in the map.

    Returns per-key success: key → True when every exit order for that key was
    accepted by the broker (a key with nothing left to exit counts as success).

    Priced at the touch — best bid for long exits, best ask for short exits,
    LTP fallback — the same rule manual lot exits use. A key with no cached
    price at all is failed (not sent at ₹0) so the caller re-arms and retries
    on a later tick.

    The exit quantity is reduced by the unfilled qty of PENDING DB exits on
    the same symbol+product (manual lot exits) — a full-position exit must not
    re-sell shares an in-flight exit order already covers. Once those exits
    fill, reconcile refreshes `_target_positions`, so the two views hand off
    without a gap.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        logger.error("Auto-exit: not authenticated, skipping")
        return {key: False for key in positions_map}

    # (exch, tsym, prd) → unfilled qty of exits already resting at the broker.
    pending_exit_qty: dict[tuple[str, str, str], int] = {}
    db = next(get_db())
    try:
        rows = (
            db.query(LotExit, OrderLot)
            .join(OrderLot, LotExit.lot_id == OrderLot.id)
            .filter(OrderLot.owner_uid == auth.user_id)
            .filter(LotExit.status == "PENDING")
            .all()
        )
        for ex, lot in rows:
            pkey = (lot.exch, lot.tsym, _norm_prd(lot.product_type))
            pending_exit_qty[pkey] = pending_exit_qty.get(pkey, 0) + max((ex.exit_qty or 0) - (ex.filled_qty or 0), 0)
    finally:
        db.close()

    broker = get_broker("shoonya")
    session_token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    results: dict[str, bool] = {}
    for key, positions in positions_map.items():
        results[key] = True
        for pos in positions:
            if pos["netqty"] == 0:
                continue
            # Long → sell at best bid; short → buy back at best ask; LTP
            # fallback (_exit_price).
            price = _exit_price(key, pos["netqty"])
            if not price or price <= 0:
                logger.warning("Auto-exit %s %s: no cached exit price for %s — skipping, will retry",
                               pos["exch"], pos["tsym"], key)
                results[key] = False
                continue
            qty = abs(pos["netqty"])
            pkey = (pos["exch"], pos["tsym"], _norm_prd(pos["prd"]))
            outstanding = pending_exit_qty.get(pkey, 0)
            if outstanding > 0:
                covered = min(qty, outstanding)
                qty -= covered
                pending_exit_qty[pkey] = outstanding - covered
                if qty == 0:
                    logger.info("Auto-exit %s %s: full qty covered by pending exit orders — skipping",
                                pos["exch"], pos["tsym"])
                    continue
            order = {
                "buy_or_sell": "S" if pos["netqty"] > 0 else "B",
                "product_type": "I" if pos["prd"] == "I" else "M",
                "exchange": pos["exch"],
                "tradingsymbol": pos["tsym"],
                "quantity": qty,
                "discloseqty": 0,
                "price_type": "LMT",
                "price": price,
                "trigger_price": 0,
                "retention": "DAY",
                "remarks": remarks,
            }
            try:
                result = await broker.placeOrder(session_token, credentials, order)
            except Exception as e:
                logger.error("Auto-exit order failed for %s %s: %s", pos["exch"], pos["tsym"], e)
                results[key] = False
                continue
            if isinstance(result, dict) and result.get("stat") != "Ok":
                logger.error("Auto-exit order rejected for %s %s: %s",
                             pos["exch"], pos["tsym"], result.get("emsg", ""))
                results[key] = False
                continue
            order_id = result.get("norenordno", "") if isinstance(result, dict) else ""
            if order_id:
                asyncio.create_task(_watch_auto_exit_order(order_id, key, pos, qty))
    return results


def _clear_symbol_target_carried(exch: str, tsym: str, key: str) -> None:
    """Zero a symbol target's rollover carry (DB + live cache).

    Called when the target auto-exits: the carried position is now closed, so a
    fresh position on this symbol must not inherit the old carry.
    """
    db = next(get_db())
    try:
        st = db.query(SymbolTarget).filter_by(exch=exch, tsym=tsym).first()
        if st and (st.carried_pnl or 0.0):
            st.carried_pnl = 0.0
            db.commit()
    except Exception:
        logger.exception("Failed to clear carried_pnl for %s|%s", exch, tsym)
    finally:
        db.close()
    if key in state._symbol_targets:
        state._symbol_targets[key]["carried_pnl"] = 0.0


def _rearm_key(key: str, exch: str, reason: str) -> None:
    """Undo the trigger latches for one key so the next tick can re-fire.

    Only safe once the outstanding exit order is definitively dead — re-arming
    with a live order resting would let the trigger place a second exit.
    """
    state._auto_exited_tokens.discard(key)
    state._exchange_target_exited.discard(exch)
    logger.warning("Auto-exit watchdog: %s — re-armed %s", reason, key)


async def _refresh_then_rearm(key: str, exch: str, reason: str) -> None:
    """Refresh the cached positions from the broker, then re-arm one key.

    A cancelled exit order may have partially filled first. Re-arming against
    the pre-fill netqty makes the next trigger exit shares that are already
    gone — flipping the account short. So the latch only comes off once a
    fresh snapshot has landed; if the refresh fails, staying latched is the
    safe side (a dead target is recoverable by re-enabling it, an oversell
    is not).
    """
    if await _load_target_positions():
        _rearm_key(key, exch, reason)
    else:
        logger.error("Auto-exit watchdog: position refresh failed — NOT re-arming %s (%s)", key, reason)


async def _watch_auto_exit_order(order_id: str, key: str, pos: dict, qty: int) -> None:
    """Babysit one auto-exit LMT order until it fills.

    Every _WATCH_DELAY_S: if the order is still resting, chase the market by
    modifying its price to the current touch (bid for sells, ask for buys —
    same rule as placement). After _WATCH_MAX_CHECKS checks without a fill,
    cancel the order, refresh the position cache (the order may have partially
    filled), and re-arm the trigger latches so the target fires fresh off the
    next tick against the real remaining position. If the order dies on its
    own: rejected → re-arm immediately (nothing filled); cancelled at the
    broker → refresh first, same as the timeout path.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        return
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    exch, tsym = pos["exch"], pos["tsym"]

    async def _status() -> str:
        row = await broker.getOrderStatus(token, credentials, order_id)
        return (row.get("status") or "").upper() if isinstance(row, dict) else ""

    for attempt in range(1, _WATCH_MAX_CHECKS + 1):
        await asyncio.sleep(_WATCH_DELAY_S)
        try:
            status = await _status()
        except Exception:
            logger.exception("Auto-exit watchdog: status fetch failed for %s (%s)", order_id, tsym)
            continue
        if status == "COMPLETE":
            return
        if status == "REJECTED":
            # A rejection can't have filled anything — the cache is still
            # accurate, re-arm without the refresh round-trip.
            _rearm_key(key, exch, f"exit order {order_id} for {tsym} died (REJECTED)")
            return
        if status in ("CANCELED", "CANCELLED"):
            # Cancelled at the broker (user / RMS) — may have partially
            # filled first, so refresh netqty before re-arming.
            await _refresh_then_rearm(key, exch, f"exit order {order_id} for {tsym} died ({status})")
            return
        if attempt == _WATCH_MAX_CHECKS:
            break
        # Still resting — chase the touch.
        price = _exit_price(key, pos["netqty"])
        if not price or price <= 0:
            continue
        try:
            await broker.modifyOrder(token, credentials, order_id, {
                "exchange": exch,
                "tradingsymbol": tsym,
                "newquantity": qty,
                "newprice_type": "LMT",
                "newprice": price,
            })
            logger.info("Auto-exit watchdog: chased %s %s to ₹%.2f (check %d/%d)",
                        tsym, order_id, price, attempt, _WATCH_MAX_CHECKS)
        except Exception as e:
            # Most common cause: the order filled between the status check and
            # the modify. The next loop pass sees COMPLETE and returns.
            logger.info("Auto-exit watchdog: modify failed for %s (%s): %s", order_id, tsym, e)

    # Out of patience — pull the order, then re-arm for a fresh attempt.
    try:
        await broker.cancelOrder(token, credentials, order_id)
    except Exception:
        # Cancel usually fails because the order just filled — verify.
        try:
            if await _status() == "COMPLETE":
                return
        except Exception:
            pass
        # Unknown state: the order may still be live. Do NOT re-arm.
        logger.error("Auto-exit watchdog: cancel failed and state unknown for %s (%s) — NOT re-arming",
                     order_id, tsym)
        return
    await _refresh_then_rearm(key, exch, f"exit order {order_id} for {tsym} unfilled after {_WATCH_MAX_CHECKS} checks, cancelled")


async def _on_target_tick(tick: dict) -> None:
    """Called on every WebSocket tick. Checks per-symbol and global P&L targets."""
    exchange = tick.get("e", "")
    tok = tick.get("tk", "")
    key = f"{exchange}|{tok}"
    ltp_str = tick.get("lp")
    bp1_str = tick.get("bp1")
    sp1_str = tick.get("sp1")
    close_str = tick.get("c")

    if ltp_str:
        state._current_ltps[key] = float(ltp_str)
    if bp1_str:
        state._current_bids[key] = float(bp1_str)
    if sp1_str:
        state._current_asks[key] = float(sp1_str)
    if close_str:
        state._current_closes[key] = float(close_str)

    # Prices flow in during the pre-open feed (e.g. NFO ~09:00, market opens
    # 09:15) — the caches above are kept fresh so live P&L displays, but an exit
    # placed now would be rejected "market not open". Gate all order placement
    # on the exchange's session window; each exchange has its own timing.
    market_open = is_market_open_for_orders(exchange)

    # --- per-symbol target ---
    # Flat total-P&L (realized + unrealized) threshold — not scaled by lot
    # count. Open legs are valued via _exit_price (best bid for longs, best
    # ask for shorts, LTP fallback); flat legs still contribute their
    # already-realized closed_pnl so intraday round-trips count too.
    sym_target = state._symbol_targets.get(key)
    has_price = ltp_str or bp1_str or sp1_str
    if market_open and sym_target and sym_target["enabled"] and has_price and key in state._target_positions and key not in state._auto_exited_tokens:
        positions = state._target_positions[key]
        symbol_pnl = 0.0
        has_open = False
        for pos in positions:
            if pos["netqty"] == 0:
                symbol_pnl += _compute_position_pnl(pos, 0.0)[0]
                continue
            price = _exit_price(key, pos["netqty"])
            if price is None:
                continue
            total_pnl, _ = _compute_position_pnl(pos, price)
            symbol_pnl += total_pnl
            has_open = True

        # Rollover carry: P&L banked on this position's prior contracts counts
        # toward the target too, so a rolled position keeps its running total.
        carried = sym_target.get("carried_pnl", 0.0)
        if has_open and (symbol_pnl + carried) >= sym_target["target_value"]:
            state._auto_exited_tokens.add(key)
            logger.info("Symbol target hit for %s: total_pnl=%.2f (+%.2f carried) >= target=%.2f — placing exit orders",
                        key, symbol_pnl, carried, sym_target["target_value"])
            try:
                results = await _place_exit_orders({key: positions}, "auto_exit_target")
            except Exception:
                # Must not escape: this coroutine runs in a fire-and-forget
                # future, so an unhandled exception would leave the key latched
                # with no exit resting at the broker — the target would be
                # silently dead for the rest of the session.
                logger.exception("Symbol target exit placement crashed for %s", key)
                results = {}
            if not results.get(key, False):
                state._auto_exited_tokens.discard(key)
            else:
                # Position is being exited — clear the carry so a future
                # position on this symbol starts its target fresh.
                _clear_symbol_target_carried(positions[0]["exch"], positions[0]["tsym"], key)

    # --- per-order (per-lot) targets ---
    # Each armed lot exits on its OWN live P&L (entry → touch), sized to just
    # that lot — so two batches of the same symbol carry independent targets.
    # Skipped entirely while the symbol/exchange target is already auto-exiting
    # this key (latched in _auto_exited_tokens): that exit covers these lots and
    # a second order here would oversell the net position.
    lot_targets = state._lot_targets.get(key)
    if market_open and lot_targets and has_price and key not in state._auto_exited_tokens:
        for lt in list(lot_targets):
            if lt["lot_id"] in state._lot_target_exited:
                continue
            # Long lots exit on the bid, short lots on the ask (LTP fallback) —
            # the same touch rule the manual exit and symbol target use.
            sign = 1 if lt["side"] == "B" else -1
            exit_px = _exit_price(key, sign)
            if not exit_px or exit_px <= 0:
                continue
            live = lt["realized"] + (exit_px - lt["avg_entry_price"]) * lt["open_qty"] * sign * lt["prcftr"]
            if live < lt["target_value"]:
                continue
            # Latch before the await so a burst of ticks can't double-fire.
            state._lot_target_exited.add(lt["lot_id"])
            logger.info("Per-order target hit for lot %d (%s): live=%.2f >= target=%.2f — placing exit",
                        lt["lot_id"], lt["tsym"], live, lt["target_value"])
            try:
                ok = await _auto_exit_lot(lt, exit_px)
            except Exception:
                logger.exception("Per-order auto-exit crashed for lot %d", lt["lot_id"])
                ok = False
            if not ok:
                state._lot_target_exited.discard(lt["lot_id"])

    # --- per-exchange total P&L targets ---
    # Each exchange has its own session timing, so each gets an independent
    # total-P&L cap (realized + unrealized across that exchange's positions).
    # When one hits its target, only that exchange's positions are auto-exited;
    # the others keep running until their own caps trigger.
    if state._exchange_targets and state._target_positions:
        exch_pnls = _calc_exchange_pnls()
        for exch, tconf in list(state._exchange_targets.items()):
            if not tconf["enabled"] or exch in state._exchange_target_exited:
                continue
            # Each exchange is gated on its own session — a pre-open tick on one
            # venue must not fire an exit on another that's still closed.
            if not is_market_open_for_orders(exch):
                continue
            pnl = exch_pnls.get(exch)
            if pnl is None:
                continue
            if pnl >= tconf["target_value"]:
                state._exchange_target_exited.add(exch)
                # Skip keys the symbol target already exited — their exit
                # orders are at the broker; re-selling them flips the account
                # short.
                exch_positions = {
                    k: v for k, v in state._target_positions.items()
                    if k.split("|", 1)[0] == exch and k not in state._auto_exited_tokens
                }
                logger.info("Exchange target hit for %s: total_pnl=%.2f >= target=%.2f — exiting %d symbol(s)",
                            exch, pnl, tconf["target_value"], len(exch_positions))
                # Latch every key before the await so concurrent ticks
                # (symbol / lot paths) skip them while these exits are in
                # flight.
                for k in exch_positions:
                    state._auto_exited_tokens.add(k)
                try:
                    results = await _place_exit_orders(exch_positions, f"auto_exit_target_{exch}")
                except Exception:
                    # Same rule as the symbol path: a crash after latching must
                    # not leave keys (and the exchange) stuck with no exits at
                    # the broker. Empty results fails every key below, which
                    # un-latches them and re-arms the exchange for a retry.
                    logger.exception("Exchange target exit placement crashed for %s", exch)
                    results = {}
                failed = [k for k in exch_positions if not results.get(k, False)]
                for k in failed:
                    state._auto_exited_tokens.discard(k)
                if failed:
                    # Re-arm the exchange for the failed keys only — the
                    # succeeded keys stay latched, so a retry can't place a
                    # second exit for them.
                    state._exchange_target_exited.discard(exch)

"""Order reconciliation: match broker fills to OrderLots/LotExits, import
external orders/positions, offset opposing lots, and cancel sibling exits."""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core import auth as _auth_module, state
from app.core.deps import _require_legacy_auth
from app.services.pnl import _compute_position_pnl, _normalize_broker_position
from app.services.targets import _load_lot_targets, _load_target_positions
from brokers.base import SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import LotExit, OrderLot, PersistentOrder, RolloverIntent
from ticker_manager import ticker_manager

logger = logging.getLogger(__name__)


# WHITELIST on purpose: anything unrecognised falls through to ENTRY, so a tag
# missing from this list costs a phantom lot — visible and fixable — rather than
# an untracked live position, which is neither. Adding a tag here is therefore a
# trading-safety change, not a tidy-up.
#
#   "exit*"        gateway UI exit, and snowball's exit_snowball_l* / exit_roll_l*
#   "auto*"        gateway symbol/exchange auto-exit
#   "lotexit*"     gateway lot-exit client_ref
#   "tempexit*"    snowball's temp-exit SELL (core/engine.py) — an immediate
#                  marketable sell that leaves no resting order behind. It was
#                  absent, so every paused rung booked a phantom SHORT lot.
#
# NOT here, deliberately:
#   "reenter_l*"   a BUY that reopens a paused rung — a genuine new position
#                  that MUST get a lot.
#   "target_rest*" a resting target SELL. Reclassifying it needs the
#                  terminal-with-fill handling in _reconcile_untagged_fills
#                  first, or a partial-fill-then-cancel has no handler at all
#                  and the ledger silently overstates the long.
_EXIT_REMARK_PREFIXES = ("exit", "auto", "lotexit", "tempexit")


def _is_exit_remarks(remarks: str) -> bool:
    """Order-placement remarks that signal 'this is an exit, do not create a new lot'."""
    return (remarks or "").startswith(_EXIT_REMARK_PREFIXES)


def _norm_prd(value) -> str:
    """Normalize a broker product code (M=NRML, I=MIS, C=CNC, H=cover).

    Broker fields can arrive with stray whitespace or missing entirely;
    without normalizing, "M" and "m " would count as different products
    wherever product participates in a grouping key.
    """
    return (value or "").strip().upper() or "M"


def _to_int(value, default: int = 0) -> int:
    """Parse a broker numeric field ("40", 40, 40.0, None, garbage) to int."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    """Parse a broker numeric field to float, tolerating None/garbage."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_broker_order_time(o: dict) -> datetime | None:
    """Parse a broker order timestamp ("HH:MM:SS DD-MM-YYYY", IST wall-clock)
    to a naive datetime, or None if missing/unparseable.
    """
    try:
        raw = str(o.get("norentm") or o.get("pytime") or "")
        return datetime.strptime(raw, "%H:%M:%S %d-%m-%Y")
    except (AttributeError, ValueError):
        return None


def _order_time_key(o: dict) -> datetime:
    """Sort key for broker order timestamps.

    Parsed to a real datetime so ordering doesn't silently depend on the
    order book being single-day (lexical sort ignores the trailing date).
    Unparseable values sort first.
    """
    return _parse_broker_order_time(o) or datetime.min


def _broker_order_opened_at(o: dict) -> datetime:
    """The broker's order time converted to UTC, for OrderLot.opened_at —
    falls back to now (UTC) when the order carries no parseable timestamp.
    IST is a fixed UTC+5:30 offset with no DST, so a plain subtraction is exact.
    """
    ist = _parse_broker_order_time(o)
    if ist is None:
        return datetime.utcnow()
    return ist - timedelta(hours=5, minutes=30)


async def _short_poll_reconcile(owner_uid: str) -> None:
    """After an order is placed, poll the broker order book a few times in
    quick succession so fills are reflected without waiting for the 30s REST
    refresh — this catches the case where Shoonya's order-update WS doesn't
    push a COMPLETE event for instantly-offsetting trades.
    """
    if _auth_module._auth is None or not _auth_module._auth.is_authenticated():
        return
    broker = get_broker("shoonya")
    token = SessionToken(token=_auth_module._auth.auth_token, broker_uid=_auth_module._auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": _auth_module._auth.user_id, "account_id": _auth_module._auth.user_id}
    for delay in (0.3, 0.7, 1.5, 3.0):
        await asyncio.sleep(delay)
        if _auth_module._auth is None or _auth_module._auth.user_id != owner_uid:
            return
        try:
            raw = await broker.getOrderBook(token, credentials)
            await _reconcile_lots_from_orderbook(raw, owner_uid)
            # Nudge the frontend to refetch /api/lots immediately
            await ticker_manager._broadcast_order({
                "norenordno": "", "status": "POLL", "remarks": "", "tsym": "", "exch": "",
            })
        except Exception:
            logger.exception("short-poll reconcile iteration failed")
    # One position-cache refresh after the poll burst — same reason as in
    # _on_order_update_event, for fills the order-update WS didn't push.
    try:
        await _load_target_positions()
        _load_lot_targets()
    except Exception:
        logger.exception("short-poll target-position refresh failed")


async def _on_order_update_event(order: dict) -> None:
    """Event-driven lot reconciliation: fires on every broker order update
    pushed over the Shoonya WebSocket, so fills are reflected without waiting
    for the 30s REST poll.
    """
    if _auth_module._auth is None or not _auth_module._auth.is_authenticated():
        return
    try:
        await _reconcile_lots_from_orderbook([order], _auth_module._auth.user_id)
    except Exception:
        logger.exception("Event-driven lot reconciliation failed")
    # A fill changes broker netqty — refresh the symbol/exchange target
    # position cache so a full-position auto-exit sizes itself off the real
    # position, not the pre-fill snapshot. (A lot exit stops being subtracted
    # by _place_exit_orders the moment it's marked FILLED above; this refresh
    # is what makes netqty correct at that same moment.)
    if (order.get("status") or "").upper() == "COMPLETE" or _to_int(order.get("fillshares")) > 0:
        try:
            await _load_target_positions()
            _load_lot_targets()
        except Exception:
            logger.exception("Refreshing target-position cache failed")


def _offset_opposing_lots(db, owner_uid: str,
                          siblings_to_cancel: list[tuple[int, str]]) -> bool:
    """When the user opens a new lot on the opposite side of an existing open lot
    for the same symbol, the broker nets them out (no explicit exit order is
    placed). This walks our open lots FIFO and offsets them: the older lot is
    treated as the closed leg and is credited with realized PnL using the newer
    lot's entry price as the effective exit price.
    """
    lots = (
        db.query(OrderLot)
        .filter(OrderLot.owner_uid == owner_uid, OrderLot.status.in_(["OPEN", "PARTIAL"]))
        .order_by(OrderLot.opened_at.asc())
        .all()
    )
    # The broker only nets trades within one product (MIS/NRML/CNC are
    # separate positions), so offsets must stay within one product too.
    by_sym: dict[tuple[str, str, str], list[OrderLot]] = {}
    for l in lots:
        by_sym.setdefault((l.exch, l.tsym, _norm_prd(l.product_type)), []).append(l)

    sm = get_scripmaster()
    dirty = False
    for (exch, tsym, _prd), grp in by_sym.items():
        scrip = sm.master.get(f"{exch}|{tsym}", {})
        prcftr = float(scrip.get("prcftr", "1") or "1")
        # Two-pointer FIFO: walk older→newer; when an open lot meets a newer
        # opposite-side open lot, offset min(open_qty) between them.
        for i, older in enumerate(grp):
            if older.open_qty <= 0:
                continue
            for newer in grp[i + 1:]:
                if newer.open_qty <= 0 or newer.side == older.side:
                    continue
                match = min(older.open_qty, newer.open_qty)
                side_sign = 1 if older.side == "B" else -1
                older.realized_pnl = (older.realized_pnl or 0.0) + \
                    (newer.avg_entry_price - older.avg_entry_price) * match * side_sign * prcftr
                older.open_qty -= match
                newer.open_qty -= match
                if older.open_qty == 0:
                    older.status = "CLOSED"
                    older.closed_at = datetime.utcnow()
                    _collect_pending_exits(older, siblings_to_cancel)
                else:
                    older.status = "PARTIAL"
                if newer.open_qty == 0:
                    newer.status = "CLOSED"
                    newer.closed_at = datetime.utcnow()
                    _collect_pending_exits(newer, siblings_to_cancel)
                else:
                    newer.status = "PARTIAL"
                dirty = True
                if older.open_qty == 0:
                    break
    return dirty


def _cancel_stale_pending_lots(db, owner_uid: str) -> bool:
    """Write off PENDING lots that never got a broker order id.

    `place_order` commits the lot before calling the broker; a crash between
    the two leaves a PENDING lot no order book will ever reference. 10
    minutes is far beyond the normal insert→broker-ack gap (seconds), so
    only true ghosts qualify.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    stale = (
        db.query(OrderLot)
        .filter(
            OrderLot.owner_uid == owner_uid,
            OrderLot.status == "PENDING",
            OrderLot.broker_entry_orderid == "",
            OrderLot.is_external.is_(False),
            OrderLot.opened_at < cutoff,
        )
        .all()
    )
    for lot in stale:
        lot.status = "CANCELLED"
        logger.info("Cancelled stale PENDING lot id=%d %s %s qty=%d (no broker order id)",
                    lot.id, lot.side, lot.tsym, lot.entry_qty)
    return bool(stale)


def _reconcile_untagged_fills(
    db, raw_orders: list[dict], owner_uid: str, lots_by_ref: dict[str, OrderLot],
    siblings_to_cancel: list[tuple[int, str]],
) -> bool:
    """Reconcile COMPLETE broker fills that aren't tagged with one of our client_refs.

    Covers:
      - Positions card "Exit" (remarks='exit', no lot id).
      - Orders placed in the broker UI (empty remarks).
      - Reversals: a single fill that closes an open position and opens a new one
        on the opposite side (e.g. long 5 → sell 10 = flat 5 + short 5).

    Per fill, in order:
      1. FIFO-close opposite-side open lots (up to fillshares).
      2. Import any remainder as a new external OPEN lot on the trade's side.

    Emits one synthetic LotExit per closed lot for a correct audit trail. Dedup
    uses `broker_exit_orderid` on LotExit + `broker_entry_orderid` on OrderLot,
    so repeat polls skip fills we've already processed.
    """
    if not raw_orders:
        return False

    sm = get_scripmaster()

    processed_exit_brokerids = {
        e.broker_exit_orderid
        for e in db.query(LotExit).join(OrderLot)
        .filter(OrderLot.owner_uid == owner_uid)
        .filter(LotExit.broker_exit_orderid != "").all()
    }
    known_entry_brokerids = {
        l.broker_entry_orderid for l in lots_by_ref.values() if l.broker_entry_orderid
    }
    known_exit_refs = {
        e.client_ref
        for e in db.query(LotExit).join(OrderLot).filter(OrderLot.owner_uid == owner_uid).all()
    }
    all_known_refs = set(lots_by_ref.keys()) | known_exit_refs

    open_lots_by_sym: dict[tuple[str, str, str], list[OrderLot]] = {}
    for l in (
        db.query(OrderLot)
        .filter(OrderLot.owner_uid == owner_uid, OrderLot.status.in_(["OPEN", "PARTIAL"]))
        .order_by(OrderLot.opened_at.asc())
        .all()
    ):
        open_lots_by_sym.setdefault((l.exch, l.tsym, _norm_prd(l.product_type)), []).append(l)

    sm = get_scripmaster()
    dirty = False

    # Oldest first so same-batch buy-then-sell pairs reconcile in order.
    raw_orders_sorted = sorted(raw_orders, key=_order_time_key)

    for o in raw_orders_sorted:
        norenordno = (o.get("norenordno") or "").strip()
        status = (o.get("status") or "").upper()
        if not norenordno or status != "COMPLETE":
            continue
        if norenordno in processed_exit_brokerids or norenordno in known_entry_brokerids:
            continue

        remarks = (o.get("remarks") or "").strip()
        # Tagged orders were handled by the primary loop.
        if remarks and remarks in all_known_refs:
            continue

        # Intentional-exit remarks come from the Positions card "Exit" button
        # (remarks='exit') and auto-exit paths (remarks='auto_...'). These
        # close a broker-side position that may not be tracked in our DB.
        # FIFO-close what matches; drop any remainder — do NOT open a new
        # external lot on the remainder. The user's intent was to reduce
        # exposure, not open a fresh position, so importing the leftover
        # would create a phantom that survives across daily polls.
        is_intentional_exit = bool(remarks) and _is_exit_remarks(remarks)

        trantype = (o.get("trantype") or "").upper()
        if trantype not in ("B", "S"):
            continue

        fillshares = _to_int(o.get("fillshares"))
        avgprc = _to_float(o.get("avgprc"))
        if fillshares <= 0 or avgprc <= 0:
            continue

        exch = (o.get("exch") or "").strip()
        tsym = (o.get("tsym") or "").strip()
        if not exch or not tsym:
            continue
        prd = _norm_prd(o.get("prd"))

        scrip = sm.master.get(f"{exch}|{tsym}", {})
        prcftr = float(scrip.get("prcftr", "1") or "1")

        # ── Phase 1: FIFO-close opposite-side open lots (same product) ──────
        # An MIS fill doesn't touch an NRML position at the broker, so
        # closing must stay within the fill's own product.
        closing_side = "B" if trantype == "S" else "S"
        remaining = fillshares
        for lot in open_lots_by_sym.get((exch, tsym, prd), []):
            if remaining <= 0:
                break
            if lot.side != closing_side or lot.open_qty <= 0:
                continue
            match_qty = min(lot.open_qty, remaining)
            side_sign = 1 if lot.side == "B" else -1
            lot.realized_pnl = (lot.realized_pnl or 0.0) + \
                (avgprc - lot.avg_entry_price) * match_qty * side_sign * prcftr
            lot.open_qty -= match_qty
            if lot.open_qty == 0:
                lot.status = "CLOSED"
                lot.closed_at = datetime.utcnow()
                # An untagged fill (symbol/exchange auto-exit, broker-UI exit)
                # closed this lot — pull any exits still resting on it.
                _collect_pending_exits(lot, siblings_to_cancel)
            else:
                lot.status = "PARTIAL"
            # One synthetic LotExit per closed lot — matches lot.open_qty delta.
            synth = LotExit(
                lot_id=lot.id,
                exit_qty=match_qty,
                filled_qty=match_qty,
                avg_exit_price=avgprc,
                broker_exit_orderid=norenordno,
                client_ref=f"synth_{norenordno}_{lot.id}",
                status="FILLED",
                filled_at=datetime.utcnow(),
            )
            db.add(synth)
            remaining -= match_qty
            dirty = True

        if remaining < fillshares:
            processed_exit_brokerids.add(norenordno)

        # ── Phase 2: Any remainder opens a new external lot on this side ────
        # Skip for intentional exits — see comment above.
        if remaining > 0 and is_intentional_exit:
            logger.info(
                "Intentional exit %s over-closed by %d share(s) — untracked "
                "opposing position; not importing.",
                norenordno, remaining,
            )
        elif remaining > 0:
            tok = sm.get_token(exch, tsym) or ""
            scrip = sm.master.get(f"{exch}|{tsym}", {}) if hasattr(sm, "master") else {}
            try:
                lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
            except Exception:
                lotsize = 1

            client_ref = f"ext_{uuid.uuid4().hex[:12]}"
            new_lot = OrderLot(
                owner_uid=owner_uid,
                exch=exch, tsym=tsym, token=tok, lotsize=lotsize,
                product_type=prd,
                side=trantype,
                entry_qty=remaining, open_qty=remaining,
                avg_entry_price=avgprc, realized_pnl=0.0,
                status="OPEN",
                broker_entry_orderid=norenordno,
                client_ref=client_ref,
                is_external=True,
                opened_at=_broker_order_opened_at(o),
            )
            db.add(new_lot)
            db.flush()
            lots_by_ref[client_ref] = new_lot
            known_entry_brokerids.add(norenordno)
            open_lots_by_sym.setdefault((exch, tsym, prd), []).append(new_lot)
            closed_qty = fillshares - remaining
            if closed_qty > 0:
                logger.info("Imported external OPEN via reversal: %s %s %s open=%d closed=%d @ %.2f",
                            norenordno, trantype, tsym, remaining, closed_qty, avgprc)
            else:
                logger.info("Imported external OPEN: %s %s %s qty=%d @ %.2f",
                            norenordno, trantype, tsym, remaining, avgprc)
            dirty = True

    return dirty


def _import_external_pending_orders(db, raw_orders: list[dict], owner_uid: str, lots_by_ref: dict) -> bool:
    """Import broker OPEN / TRIGGER_PENDING (pending) orders that weren't placed via Gateway.

    Skips pending orders that fit entirely within an existing opposite-side
    open position (treated as an untagged pending exit — closing will be
    handled by `_reconcile_untagged_fills` once they fill). Anything else is
    imported as a PENDING external lot so the UI can show and manage it.

    COMPLETE (filled) untagged orders are handled by `_reconcile_untagged_fills`.
    """
    if not raw_orders:
        return False

    known_entry_brokerids = {
        l.broker_entry_orderid for l in lots_by_ref.values() if l.broker_entry_orderid
    }
    known_exit_refs = {
        e.client_ref
        for e in db.query(LotExit).join(OrderLot).filter(OrderLot.owner_uid == owner_uid).all()
    }
    all_known_refs = set(lots_by_ref.keys()) | known_exit_refs

    # Per (exch, tsym, prd, side) open qty from OPEN/PARTIAL lots — used to
    # decide whether an opposing pending order is a pure exit. Product is part
    # of the key: a pending MIS sell is not an exit of an NRML long.
    open_by_sym_side: dict[tuple, int] = {}
    for l in lots_by_ref.values():
        if l.status not in ("OPEN", "PARTIAL"):
            continue
        key = (l.exch, l.tsym, _norm_prd(l.product_type), l.side)
        open_by_sym_side[key] = open_by_sym_side.get(key, 0) + (l.open_qty or 0)

    sm = get_scripmaster()
    dirty = False

    for o in raw_orders:
        norenordno = (o.get("norenordno") or "").strip()
        broker_status = (o.get("status") or "").upper()

        # TRIGGER_PENDING = a stop-loss waiting for its trigger — still a
        # live pending order the user should see.
        if not norenordno or broker_status not in ("OPEN", "TRIGGER_PENDING"):
            continue
        if norenordno in known_entry_brokerids:
            continue

        trantype = (o.get("trantype") or "").upper()
        if trantype not in ("B", "S"):
            continue

        remarks = (o.get("remarks") or "").strip()
        if remarks in all_known_refs or _is_exit_remarks(remarks):
            continue

        exch = (o.get("exch") or "").strip()
        tsym = (o.get("tsym") or "").strip()
        if not exch or not tsym:
            continue
        prd = _norm_prd(o.get("prd"))

        lot_qty = _to_int(o.get("qty"))
        lot_price = _to_float(o.get("prc", o.get("price", "0")) or "0")
        if lot_price <= 0:
            # SL-market orders carry no limit price — fall back to trigger.
            lot_price = _to_float(o.get("trgprc"))
        if lot_qty <= 0 or lot_price <= 0:
            continue

        # If pending qty fits within existing opposite-side open qty, this is
        # very likely an untagged exit — skip so we don't create a phantom lot.
        opp_side = "S" if trantype == "B" else "B"
        opp_open = open_by_sym_side.get((exch, tsym, prd, opp_side), 0)
        if opp_open >= lot_qty:
            logger.debug("Skipping likely pending exit: %s %s %s qty=%d (opp_open=%d)",
                         norenordno, trantype, tsym, lot_qty, opp_open)
            continue

        tok = sm.get_token(exch, tsym) or ""
        scrip = sm.master.get(f"{exch}|{tsym}", {}) if hasattr(sm, "master") else {}
        try:
            lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
        except Exception:
            lotsize = 1

        client_ref = f"ext_{uuid.uuid4().hex[:12]}"
        lot = OrderLot(
            owner_uid=owner_uid,
            exch=exch, tsym=tsym, token=tok, lotsize=lotsize,
            product_type=(o.get("prd") or "M").strip(),
            side=trantype,
            entry_qty=lot_qty, open_qty=lot_qty,
            avg_entry_price=lot_price, realized_pnl=0.0,
            status="PENDING",
            broker_entry_orderid=norenordno,
            client_ref=client_ref,
            is_external=True,
            opened_at=_broker_order_opened_at(o),
        )
        db.add(lot)
        db.flush()
        lots_by_ref[client_ref] = lot
        known_entry_brokerids.add(norenordno)
        logger.info("Imported external PENDING: %s %s %s qty=%d @ %.2f",
                    norenordno, trantype, tsym, lot_qty, lot_price)
        dirty = True

    return dirty


async def _sync_positions_to_lots(owner_uid: str) -> None:
    """Import broker positions whose net qty exceeds what DB lots track.

    Uses a **signed** net delta: broker_netqty (signed) − db_net (signed).
    This is essential when the broker's net position is on the opposite side
    of what the DB tracks (e.g. DB says long 5, broker says short 3). The
    old per-side comparison would import a full-size short in that case, and
    then `_offset_opposing_lots` would close it against the older long,
    leaving no OPEN short — so the next sync would re-import the same phantom
    lot forever.

    Handles the gateway-was-down case: broker order books are daily-only, but
    positions persist across days.

    Runs under `state._reconcile_lock`, and reconciles today's order book
    *before* comparing net quantities. The book pass matches fills by broker
    order id / client_ref, so everything the book can explain is already in
    the DB when the blunt netqty comparison runs. Without this ordering, a
    fill imported here (which carries no broker order id) gets re-imported by
    the next book pass as a duplicate lot.
    """
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        return
    broker = get_broker("shoonya")
    token = SessionToken(token=auth.auth_token, broker_uid=auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    # Network fetches stay outside the lock. Book first, positions second, so
    # the positions snapshot reflects every fill the book snapshot has.
    try:
        raw_orders = await broker.getOrderBook(token, credentials)
    except Exception as e:
        # Book first, positions second, is not a preference — it is the whole
        # reason this ordering is documented above. Importing positions against
        # an assumed-empty book books every fill as a fresh external lot with no
        # order id, and the next readable book re-imports the same fills as a
        # second lot for the same position. Skip the pass; it runs again shortly.
        logger.warning("_sync_positions_to_lots: order book unreadable (%s) — skipping this pass rather "
                       "than importing positions that the book would have explained", e)
        return

    try:
        raw = await broker.getPositions(token, credentials)
    except Exception as e:
        logger.warning("_sync_positions_to_lots: failed to fetch positions: %s", e)
        return

    if not raw:
        return

    loop = asyncio.get_running_loop()
    async with state._reconcile_lock:
        siblings_to_cancel = await loop.run_in_executor(
            None, _reconcile_lots_from_orderbook_impl, raw_orders, owner_uid
        )
        await loop.run_in_executor(None, _import_position_deltas_impl, raw, owner_uid)
    if siblings_to_cancel:
        await _cancel_sibling_exits(siblings_to_cancel, owner_uid)


def _import_position_deltas_impl(raw: list[dict], owner_uid: str) -> None:
    """DB half of `_sync_positions_to_lots`: import the signed net delta per
    symbol as an external lot. Must run while `state._reconcile_lock` is held
    so it can't interleave with an order-book reconcile pass.
    """
    sm = get_scripmaster()
    db = next(get_db())
    try:
        for pos in raw:
            if not isinstance(pos, dict):
                continue
            netqty = _to_int(pos.get("netqty"))
            if netqty == 0:
                continue

            tsym = (pos.get("tsym") or "").strip()
            exch = (pos.get("exch") or "").strip()
            if not tsym or not exch:
                continue

            netavgprc = _to_float(pos.get("netavgprc"))
            prd = _norm_prd(pos.get("prd"))

            tok = sm.get_token(exch, tsym) or ""
            scrip = sm.master.get(f"{exch}|{tsym}", {}) if hasattr(sm, "master") else {}
            try:
                lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
            except Exception:
                lotsize = 1
            try:
                prcftr = float(scrip.get("prcftr", "1") or "1")
            except Exception:
                prcftr = 1.0

            # Entry price for the imported lot. For a position opened today the
            # broker's netavgprc is the true entry. For carry-forward positions
            # it's the prior settlement/close price, not what the trader paid —
            # so back the real basis out of the position's total P&L at LTP
            # (the same figure the Positions card shows):
            #     entry = ltp - total_pnl / (netqty * prcftr)
            entry_price = netavgprc
            cf_qty = _to_int(pos.get("cfbuyqty")) + _to_int(pos.get("cfsellqty"))
            if cf_qty > 0:
                norm = _normalize_broker_position(pos)
                norm["prcftr"] = prcftr
                ltp = state._current_ltps.get(f"{exch}|{tok}") or norm["lp"]
                if ltp and ltp > 0:
                    total_pnl, _ = _compute_position_pnl(norm, ltp)
                    derived = ltp - total_pnl / (netqty * prcftr)
                    if derived > 0:
                        entry_price = derived

            if entry_price <= 0:
                logger.warning(
                    "_sync_positions_to_lots: skipping %s/%s netqty=%d — no usable "
                    "entry price (netavgprc=%.2f)",
                    exch, tsym, netqty, netavgprc,
                )
                continue

            # Signed DB net across OPEN/PARTIAL lots for this symbol in the
            # same product. Broker position rows are per-product (MIS/NRML/
            # CNC are separate positions), so only same-product lots can
            # explain this row's net qty — comparing against the whole-symbol
            # net imports phantom lots whenever products are mixed. Product is
            # matched in Python because stored codes may be unnormalized.
            db_net = 0
            for l in (
                db.query(OrderLot)
                .filter(
                    OrderLot.owner_uid == owner_uid,
                    OrderLot.tsym == tsym,
                    OrderLot.exch == exch,
                    OrderLot.status.in_(["OPEN", "PARTIAL"]),
                )
                .all()
            ):
                if _norm_prd(l.product_type) != prd:
                    continue
                sign = 1 if l.side == "B" else -1
                db_net += sign * (l.open_qty or 0)

            delta = netqty - db_net
            if delta == 0:
                continue

            side = "B" if delta > 0 else "S"
            abs_qty = abs(delta)

            client_ref = f"ext_{uuid.uuid4().hex[:12]}"
            lot = OrderLot(
                owner_uid=owner_uid,
                exch=exch, tsym=tsym, token=tok, lotsize=lotsize,
                product_type=prd,
                side=side,
                entry_qty=abs_qty, open_qty=abs_qty,
                avg_entry_price=entry_price, realized_pnl=0.0,
                status="OPEN",
                broker_entry_orderid="",
                client_ref=client_ref,
                is_external=True,
            )
            db.add(lot)
            logger.info(
                "Synced position to lot: %s %s [%s] qty=%d @ %.2f (netavgprc=%.2f broker_net=%d db_net=%d)",
                side, tsym, prd, abs_qty, entry_price, netavgprc, netqty, db_net,
            )

        db.commit()
    except Exception:
        logger.exception("_import_position_deltas_impl failed")
        db.rollback()
    finally:
        db.close()


async def _reconcile_lots_from_orderbook(raw_orders: list[dict], owner_uid: str) -> None:
    """Serialized wrapper. The event-driven, short-poll, and REST paths all
    call this concurrently; without the lock, two overlapping runs can both
    reach `_import_...` before either commits, producing duplicate lots.
    """
    async with state._reconcile_lock:
        siblings_to_cancel = await asyncio.get_running_loop().run_in_executor(
            None, _reconcile_lots_from_orderbook_impl, raw_orders, owner_uid
        )
    if siblings_to_cancel:
        await _cancel_sibling_exits(siblings_to_cancel, owner_uid)
    # Any near exit that just filled may have a rollover waiting on it — place
    # the far leg now. Runs outside the reconcile lock (it makes broker calls)
    # and claims each intent atomically, so it never double-fires.
    await _fire_ready_rollovers(owner_uid)


def _finalize_rollover_intent(intent_id: int, status: str, *,
                              far_order_id: str = "", far_lot_id=None, error: str = "") -> None:
    """Record the terminal outcome of a rollover intent (DONE or FAILED)."""
    db = next(get_db())
    try:
        it = db.query(RolloverIntent).filter_by(id=intent_id).first()
        if it is None:
            return
        it.status = status
        it.fired_at = datetime.utcnow()
        if far_order_id:
            it.far_order_id = far_order_id
        if far_lot_id is not None:
            it.far_lot_id = far_lot_id
        if error:
            it.error = error[:500]
        db.commit()
    finally:
        db.close()


def _restore_near_carry(lot_id: int, prior: float) -> None:
    """Put the carry share back on the near lot after the far leg failed to
    place. Phase 1 removed it in anticipation of moving it to the far lot; with
    no far lot created, restoring it keeps that running P&L visible on the near
    lot (still open on a partial roll, closed-today on a full roll) instead of
    vanishing from both.
    """
    db = next(get_db())
    try:
        lot = db.query(OrderLot).filter_by(id=lot_id).first()
        if lot is not None:
            lot.carried_pnl = prior
            db.commit()
    except Exception:
        logger.exception("Auto-rollover carry restore failed for near lot %s", lot_id)
    finally:
        db.close()


async def _fire_ready_rollovers(owner_uid: str) -> None:
    """Place the far leg for any PENDING rollover whose near exit has filled.

    Called at the tail of every reconcile pass, so a fill detected by the WS
    order-update, the post-order short-poll, or the 30s REST refresh triggers
    the far entry automatically — no matter how long the exit took to fill.
    Each intent is claimed atomically (PENDING→FIRING) before the async
    place_order, so overlapping reconcile passes can't place it twice.
    """
    if (_auth_module._auth is None or not _auth_module._auth.is_authenticated()
            or _auth_module._auth.user_id != owner_uid):
        return

    # Phase 1 (sync DB): find ready intents and claim them.
    db = next(get_db())
    ready: list[dict] = []
    try:
        intents = db.query(RolloverIntent).filter_by(owner_uid=owner_uid, status="PENDING").all()
        for it in intents:
            ex = db.query(LotExit).filter_by(id=it.exit_id).first()
            if ex is None:
                it.status = "FAILED"
                it.error = "near exit not found"
                continue
            if ex.status == "FILLED":
                filled = int(ex.filled_qty or 0)
                if filled <= 0:
                    it.status = "FAILED"
                    it.error = "near exit filled with zero qty"
                    continue
                claimed = (
                    db.query(RolloverIntent)
                    .filter_by(id=it.id, status="PENDING")
                    .update({"status": "FIRING"})
                )
                db.commit()
                if claimed == 1:
                    # Carry the running P&L: this leg's realized result on the
                    # rolled qty = (exit − entry) × qty × prcftr (signed by side),
                    # plus the near lot's own already-carried P&L (from a prior
                    # roll), proportioned to the rolled fraction.
                    carried = 0.0
                    # Track the near lot's carry share we optimistically move to
                    # the far leg below, so we can restore it if the far entry
                    # fails to place (Phase 2) — otherwise that P&L would end up
                    # shown on neither lot.
                    near_prior_carried = 0.0
                    near_carry_reduced = False
                    if it.carry_pnl:
                        near_lot = db.query(OrderLot).filter_by(id=it.lot_id).first()
                        if near_lot is not None:
                            scrip = get_scripmaster().master.get(f"{it.exch}|{it.near_tsym}", {})
                            prcftr = float(scrip.get("prcftr", "1") or "1")
                            side_sign = 1 if near_lot.side == "B" else -1
                            leg_realized = (float(ex.avg_exit_price or 0.0) - float(near_lot.avg_entry_price or 0.0)) \
                                * filled * side_sign * prcftr
                            prior = float(near_lot.carried_pnl or 0.0)
                            qty_before = (near_lot.open_qty or 0) + filled  # open_qty already reduced by this fill
                            frac = (filled / qty_before) if qty_before > 0 else 1.0
                            carried = prior * frac + leg_realized
                            # Leave the un-rolled remainder's carry share behind.
                            remaining = prior - prior * frac
                            if abs(prior - remaining) > 1e-9:
                                near_lot.carried_pnl = remaining
                                db.commit()
                                near_prior_carried = prior
                                near_carry_reduced = True
                    ready.append({
                        "id": it.id, "exch": it.exch, "near_tsym": it.near_tsym,
                        "far_tsym": it.far_tsym, "far_token": it.far_token, "side": it.side,
                        "product_type": it.product_type, "price_type": it.price_type,
                        "entry_price": it.entry_price, "qty": filled, "lot_id": it.lot_id,
                        "carry_target": it.carry_target,
                        "near_target_enabled": it.near_target_enabled,
                        "near_target_value": it.near_target_value, "full_roll": it.full_roll,
                        "carry_pnl": it.carry_pnl, "carried_pnl": carried,
                        "near_prior_carried": near_prior_carried,
                        "near_carry_reduced": near_carry_reduced,
                    })
            elif ex.status in ("REJECTED", "CANCELLED"):
                it.status = "FAILED"
                it.error = f"near exit {ex.status.lower()}"
        db.commit()
    finally:
        db.close()

    if not ready:
        return

    # Phase 2 (async broker): place each far leg. Lazy imports avoid a
    # router↔service import cycle at module load.
    from app.routers.orders import place_order
    from app.schemas import PlaceOrderRequest

    for r in ready:
        entry_price = 0.0
        if r["price_type"] == "LMT":
            entry_price = r["entry_price"]
            if entry_price <= 0 and r["far_token"]:
                key = f"{r['exch']}|{r['far_token']}"
                book = state._current_asks if r["side"] == "B" else state._current_bids
                entry_price = book.get(key) or state._current_ltps.get(key, 0.0)
        try:
            result = await place_order(PlaceOrderRequest(
                buy_or_sell=r["side"], product_type=r["product_type"], exchange=r["exch"],
                tradingsymbol=r["far_tsym"], quantity=r["qty"], discloseqty=0,
                price_type=r["price_type"], price=(entry_price if r["price_type"] == "LMT" else 0),
                trigger_price=0, retention="DAY", remarks="",
                description=f"Rollover from {r['near_tsym']}",
            ))
            far_order_id = result.get("order_id", "") if isinstance(result, dict) else ""
            far_lot_id = result.get("lot_id") if isinstance(result, dict) else None
            _finalize_rollover_intent(r["id"], "DONE", far_order_id=far_order_id, far_lot_id=far_lot_id)
            # Tag the far lot as rollover-placed (ROLL badge) and carry the near
            # leg's realized P&L onto it so its P&L continues from where the near
            # contract left off.
            if far_lot_id is not None:
                fdb = next(get_db())
                try:
                    fl = fdb.query(OrderLot).filter_by(id=far_lot_id).first()
                    if fl is not None:
                        fl.is_rollover = True
                        if r["carry_pnl"] and r["carried_pnl"]:
                            fl.carried_pnl = r["carried_pnl"]
                        # Carry the near lot's per-order target onto the far lot.
                        # Its carried_pnl (set above) already counts toward the
                        # target via _load_lot_targets, so the rolled position
                        # keeps its running total toward the same exit level.
                        if r["carry_target"] and r["near_target_enabled"]:
                            fl.target_enabled = True
                            fl.target_value = r["near_target_value"]
                            state._lot_target_exited.discard(fl.id)
                        fdb.commit()
                except Exception:
                    logger.exception("Auto-rollover far-lot tag/carry set failed for far lot %s", far_lot_id)
                finally:
                    fdb.close()
            logger.info("Auto-rollover: placed %s x%d (order %s) for near %s; carried_pnl=%.2f",
                        r["far_tsym"], r["qty"], far_order_id, r["near_tsym"], r["carried_pnl"])
            await ticker_manager.broadcast_alert(
                "success", f"Rolled {r['near_tsym']} → {r['far_tsym']} · {r['qty']} qty placed")
        # Telling a human to place the far leg by hand is only safe when we KNOW
        # it was never accepted. A 400 is the broker's own refusal; a 502 means
        # we never got a verdict and the order may be resting or filled — acting
        # on that advice would double the position, so it must not be given.
        except HTTPException as e:
            rejected = e.status_code == 400
            logger.warning("Auto-rollover entry %s for %s: %s",
                           "rejected" if rejected else "UNRESOLVED", r["far_tsym"], e.detail)
            _finalize_rollover_intent(r["id"], "FAILED" if rejected else "UNRESOLVED", error=str(e.detail))
            if r["near_carry_reduced"]:
                _restore_near_carry(r["lot_id"], r["near_prior_carried"])
            await ticker_manager.broadcast_alert(
                "error",
                (f"Rollover failed: {r['far_tsym']} entry rejected — {e.detail}. "
                 f"You are flat on {r['near_tsym']} (it closed). Place {r['far_tsym']} manually.")
                if rejected else
                (f"Rollover UNRESOLVED: no verdict on the {r['far_tsym']} entry — {e.detail}. "
                 f"You are flat on {r['near_tsym']}, but the {r['far_tsym']} order MAY be live. "
                 "Check the order book before placing anything."))
            continue
        except Exception as e:
            logger.exception("Auto-rollover entry crashed for %s", r["far_tsym"])
            _finalize_rollover_intent(r["id"], "UNRESOLVED", error=str(e))
            if r["near_carry_reduced"]:
                _restore_near_carry(r["lot_id"], r["near_prior_carried"])
            await ticker_manager.broadcast_alert(
                "error", f"Rollover UNRESOLVED: could not confirm the {r['far_tsym']} entry — {e}. "
                         f"You are flat on {r['near_tsym']}, but that order MAY be live. "
                         "Check the order book before placing anything.")
            continue

    # Nudge the frontend to refetch lots/orders so the new far leg shows up.
    try:
        await ticker_manager._broadcast_order({
            "norenordno": "", "status": "POLL", "remarks": "", "tsym": "", "exch": "",
        })
    except Exception:
        pass


async def _cancel_sibling_exits(exits: list[tuple[int, str]], owner_uid: str) -> None:
    """After a fill closes a lot, cancel any remaining PENDING exits on it.

    `exits` is a list of `(lot_exit_id, broker_order_id)` collected inside the
    reconcile pass. Best-effort: broker errors are logged; DB is only marked
    CANCELLED after a successful broker cancel so a failed call doesn't lie
    about live orders.
    """
    if _auth_module._auth is None or _auth_module._auth.user_id != owner_uid or not _auth_module._auth.is_authenticated():
        return
    broker = get_broker("shoonya")
    token = SessionToken(token=_auth_module._auth.auth_token, broker_uid=_auth_module._auth.user_id, issued_at="", broker_name="shoonya")
    credentials = {"user_id": _auth_module._auth.user_id, "account_id": _auth_module._auth.user_id}
    cancelled_ids: list[int] = []
    for exit_id, broker_orderid in exits:
        try:
            result = await broker.cancelOrder(token, credentials, broker_orderid)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                logger.warning("Sibling exit cancel refused for %s: %s",
                               broker_orderid, result.get("emsg"))
                continue
            cancelled_ids.append(exit_id)
        except Exception:
            logger.exception("Sibling exit cancel failed for broker order %s", broker_orderid)
    if not cancelled_ids:
        return
    db = next(get_db())
    try:
        for row in db.query(LotExit).filter(LotExit.id.in_(cancelled_ids)).all():
            if row.status == "PENDING":
                row.status = "CANCELLED"
        db.commit()
    finally:
        db.close()


def _arm_symbol_target_from_persistent(db, sm, po_row: PersistentOrder, lot: OrderLot) -> None:
    """A persistent order carrying a target intent just (partially) filled —
    arm it as THIS lot's own per-order target. Per-order (not per-symbol): when
    this specific lot's live P&L reaches target_value, only this lot is exited,
    so a persistent order can't disturb an unrelated batch of the same symbol.
    """
    if not po_row.target_enabled or (po_row.target_value or 0) <= 0:
        return
    lot.target_enabled = True
    lot.target_value = po_row.target_value
    # Fresh lot — drop any stale latch so its target can fire this session.
    state._lot_target_exited.discard(lot.id)
    logger.info("Armed per-order target from persistent order %d: lot %s %s ₹%.2f",
                po_row.id, lot.exch, lot.tsym, po_row.target_value)


def _collect_pending_exits(lot: OrderLot, siblings_to_cancel: list[tuple[int, str]]) -> None:
    """Queue a just-closed lot's remaining PENDING broker exits for cancellation.

    Every path that zeroes a lot's open_qty must call this — a resting exit
    order left live at the broker after its lot closed fills against nothing
    and flips the account short.
    """
    for sib in lot.exits:
        if sib.status == "PENDING" and sib.broker_exit_orderid:
            siblings_to_cancel.append((sib.id, sib.broker_exit_orderid))


def _apply_exit_fill(sm, ex: LotExit, fillshares: int, avgprc: float,
                     siblings_to_cancel: list[tuple[int, str]]) -> None:
    """Book a broker fill against a LotExit: mark it FILLED, reduce the parent
    lot's open qty, realize PnL, and collect sibling PENDING exits for cancel
    when the parent closes. Shared by the COMPLETE branch and the
    partial-fill-then-cancel branch.
    """
    ex.filled_qty = fillshares
    ex.avg_exit_price = avgprc
    ex.status = "FILLED"
    ex.filled_at = datetime.utcnow()
    parent = ex.lot
    if parent is None:
        return
    # Clamp to what the lot actually has open: if a double-exit ever slips
    # past the sibling-cancel guard, PnL must not be booked on shares the
    # lot didn't have.
    closable = min(fillshares, parent.open_qty)
    if closable < fillshares:
        logger.warning("Exit fill %s on lot %d: fill qty %d exceeds open qty %d — booking PnL on %d",
                       ex.client_ref, parent.id, fillshares, parent.open_qty, closable)
    parent.open_qty -= closable
    side_sign = 1 if parent.side == "B" else -1
    scrip = sm.master.get(f"{parent.exch}|{parent.tsym}", {})
    prcftr = float(scrip.get("prcftr", "1") or "1")
    parent.realized_pnl = (parent.realized_pnl or 0.0) + \
        (avgprc - parent.avg_entry_price) * closable * side_sign * prcftr
    if parent.open_qty == 0:
        parent.status = "CLOSED"
        parent.closed_at = datetime.utcnow()
        # Prevent over-exit: a closed lot can still have sibling PENDING
        # exits sitting at the broker (e.g. a manual DB exit order plus a
        # target auto-exit). `ex` itself was marked FILLED above, so the
        # PENDING filter naturally excludes it.
        _collect_pending_exits(parent, siblings_to_cancel)
    else:
        parent.status = "PARTIAL"


def _reconcile_lots_from_orderbook_impl(raw_orders: list[dict], owner_uid: str) -> list[tuple[int, str]]:
    """Match broker fills back to OrderLots/LotExits via the client_ref tag in `remarks`.

    Returns a list of `(lot_exit_id, broker_order_id)` for sibling PENDING exits
    on lots that were closed by a fill in this pass — the async wrapper cancels
    them at the broker to prevent over-exit (e.g. a lot with a target-exit *and*
    a manual DB exit order both firing on the same 1-qty position).
    """
    raw_orders = [o for o in raw_orders if isinstance(o, dict)]
    if not raw_orders:
        return []
    sm = get_scripmaster()
    db = next(get_db())
    try:
        # Build ref → row maps once for O(1) lookup
        lots_by_ref: dict[str, OrderLot] = {
            l.client_ref: l for l in db.query(OrderLot).filter(OrderLot.owner_uid == owner_uid).all()
        }
        exits_by_ref: dict[str, LotExit] = {
            e.client_ref: e for e in db.query(LotExit).join(OrderLot).filter(OrderLot.owner_uid == owner_uid).all()
        }
        siblings_to_cancel: list[tuple[int, str]] = []
        # Secondary index: broker order ID → external PENDING lot (for cancel
        # tracking). CANCELLED lots stay in the index so a partial fill seen in
        # a later, more complete order snapshot can revive them.
        external_pending_by_brokerid: dict[str, OrderLot] = {
            l.broker_entry_orderid: l
            for l in lots_by_ref.values()
            if l.is_external and l.status in ("PENDING", "CANCELLED") and l.broker_entry_orderid
        }
        # Fallback index: broker order id → our lot / exit.
        #
        # `remarks` is the designed matching key, but Shoonya returns it EMPTY on
        # REST order-book rows — only the live order-update websocket carries it.
        # So on every REST pass nothing matched: entries never left PENDING (and
        # were then re-imported as a second external lot for the same position),
        # and filled exits never applied, leaving lots OPEN at full size against
        # a flat account. The broker order id is on both sides of the wire and
        # the broker never discards it, so match on that when the tag is missing.
        lots_by_brokerid: dict[str, OrderLot] = {
            l.broker_entry_orderid: l
            for l in lots_by_ref.values()
            if l.broker_entry_orderid and not l.is_external
        }
        exits_by_brokerid: dict[str, LotExit] = {
            e.broker_exit_orderid: e
            for e in exits_by_ref.values()
            if e.broker_exit_orderid
        }
        dirty = False
        for o in raw_orders:
            # One malformed order must not poison the whole pass — log and
            # skip it; the rest still reconcile and commit.
            try:
                norenordno = (o.get("norenordno") or "").strip()
                remarks = (o.get("remarks") or "").strip()
                status = (o.get("status") or "").upper()
                fillshares = _to_int(o.get("fillshares"))
                avgprc = _to_float(o.get("avgprc"))

                # External PENDING lots are tracked by broker order ID, not remarks.
                if norenordno and norenordno in external_pending_by_brokerid:
                    ext_lot = external_pending_by_brokerid[norenordno]
                    if status in ("REJECTED", "CANCELED", "CANCELLED"):
                        if fillshares > 0:
                            # Partially filled before the cancel — keep the filled
                            # part as a live lot; only the remainder was cancelled.
                            ext_lot.entry_qty = fillshares
                            ext_lot.open_qty = fillshares
                            if avgprc > 0:
                                ext_lot.avg_entry_price = avgprc
                            ext_lot.status = "OPEN"
                            dirty = True
                            logger.info("External PENDING %s %s cancelled after partial fill — keeping qty=%d @ %.2f",
                                        norenordno, ext_lot.tsym, fillshares, avgprc)
                        elif ext_lot.status == "PENDING":
                            ext_lot.status = "CANCELLED"
                            dirty = True
                            logger.info("Cancelled external PENDING lot: %s %s", norenordno, ext_lot.tsym)
                    elif status == "COMPLETE" and fillshares > 0:
                        ext_lot.status = "OPEN"
                        if avgprc > 0:
                            ext_lot.avg_entry_price = avgprc
                        # Reconcile qty against the actual fill: on partial-fill-
                        # then-cancel, fillshares can be smaller than the ordered
                        # qty stored at import time.
                        if fillshares != ext_lot.entry_qty:
                            logger.info("External PENDING %s: reconciling qty %d → %d",
                                        norenordno, ext_lot.entry_qty, fillshares)
                            ext_lot.entry_qty = fillshares
                            ext_lot.open_qty = fillshares
                        dirty = True
                        logger.info("Filled external PENDING lot: %s %s qty=%d @ %.2f", norenordno, ext_lot.tsym, fillshares, avgprc)
                    continue

                # Prefer the tag; fall back to the broker order id, which is the
                # only key that survives a REST round trip (see the index above).
                if not remarks and not norenordno:
                    continue
                # Entry leg
                lot = lots_by_ref.get(remarks) if remarks else None
                if lot is None and norenordno:
                    lot = lots_by_brokerid.get(norenordno)
                if lot is not None:
                    if status == "REJECTED" or status == "CANCELED" or status == "CANCELLED":
                        if fillshares > 0 and lot.status in ("PENDING", "CANCELLED"):
                            # Partially filled before the cancel — keep the filled
                            # part as a live lot; only the remainder was cancelled.
                            # CANCELLED is revivable here: a WS cancel event that
                            # lacked fill fields can land before the REST poll
                            # that carries them.
                            lot.entry_qty = fillshares
                            lot.open_qty = fillshares
                            if avgprc > 0:
                                lot.avg_entry_price = avgprc
                            lot.status = "OPEN"
                            dirty = True
                            logger.info("Entry %s %s cancelled after partial fill — keeping qty=%d @ %.2f",
                                        norenordno, lot.tsym, fillshares, avgprc)
                            if lot.persistent_order_id:
                                po_row = db.query(PersistentOrder).filter_by(id=lot.persistent_order_id).first()
                                if po_row is not None and po_row.status == "ACTIVE":
                                    # Tomorrow's sweep must re-place only the
                                    # unfilled remainder, or the user ends up
                                    # oversized.
                                    po_row.quantity = max(po_row.quantity - fillshares, 0)
                                    if po_row.quantity == 0:
                                        po_row.status = "FILLED"
                                        po_row.filled_lot_id = lot.id
                                    elif status == "REJECTED":
                                        po_row.status = "FAILED"
                                    # Part of the position is live — the
                                    # target intent applies from now.
                                    _arm_symbol_target_from_persistent(db, sm, po_row, lot)
                        elif lot.status == "PENDING":
                            lot.status = "CANCELLED"
                            dirty = True
                            # Persistent orders: REJECTED means broker refused (bad
                            # params, no funds, etc.) — mark FAILED so we stop
                            # retrying. Plain CANCELLED (exchange EOD) leaves the
                            # persistent row ACTIVE for tomorrow's sweep.
                            if lot.persistent_order_id and status == "REJECTED":
                                po_row = db.query(PersistentOrder).filter_by(id=lot.persistent_order_id).first()
                                if po_row is not None and po_row.status == "ACTIVE":
                                    po_row.status = "FAILED"
                    elif status == "COMPLETE" and fillshares > 0:
                        if avgprc > 0 and abs(avgprc - lot.avg_entry_price) > 1e-9:
                            lot.avg_entry_price = avgprc
                            dirty = True
                        if lot.status in ("PENDING", "CANCELLED"):
                            # CANCELLED → OPEN: the lot was written off (e.g. the
                            # place-order response was lost) but the broker filled
                            # it — the order book is the truth. Adopt the broker's
                            # fill qty too: the order may have been modified at
                            # the broker before filling. Safe here: PENDING and
                            # CANCELLED lots cannot have exits yet.
                            if fillshares != lot.entry_qty:
                                logger.info("Entry %s %s: reconciling qty %d → %d",
                                            norenordno, lot.tsym, lot.entry_qty, fillshares)
                            lot.entry_qty = fillshares
                            lot.open_qty = fillshares
                            lot.status = "OPEN"
                            dirty = True
                        # Persistent order fully filled — retire it and remember
                        # which lot it produced.
                        if lot.persistent_order_id:
                            po_row = db.query(PersistentOrder).filter_by(id=lot.persistent_order_id).first()
                            if po_row is not None and po_row.status == "ACTIVE":
                                po_row.status = "FILLED"
                                po_row.filled_lot_id = lot.id
                                _arm_symbol_target_from_persistent(db, sm, po_row, lot)
                    continue
                # Exit leg
                ex = exits_by_ref.get(remarks) if remarks else None
                if ex is None and norenordno:
                    ex = exits_by_brokerid.get(norenordno)
                if ex is not None:
                    if status in ("REJECTED", "CANCELED", "CANCELLED"):
                        if fillshares > 0 and avgprc > 0 and (ex.filled_qty or 0) == 0:
                            # Partially filled before the cancel — book the filled
                            # part; only the remainder was cancelled. Also corrects
                            # an exit a WS cancel event (without fill fields)
                            # already marked CANCELLED.
                            _apply_exit_fill(sm, ex, fillshares, avgprc, siblings_to_cancel)
                            dirty = True
                            logger.info("Exit %s %s cancelled after partial fill — booked qty=%d @ %.2f",
                                        norenordno, (ex.lot.tsym if ex.lot else ""), fillshares, avgprc)
                        elif ex.status == "PENDING":
                            ex.status = "CANCELLED" if status != "REJECTED" else "REJECTED"
                            dirty = True
                    elif status == "COMPLETE" and fillshares > 0 and (
                        ex.status == "PENDING"
                        or ((ex.filled_qty or 0) == 0 and ex.status in ("CANCELLED", "REJECTED"))
                    ):
                        # PENDING is the normal path. CANCELLED/REJECTED with no
                        # fill booked is an orphaned fill — the cancel raced the
                        # fill, or the placement response was lost.
                        _apply_exit_fill(sm, ex, fillshares, avgprc, siblings_to_cancel)
                        dirty = True
            except Exception:
                logger.exception("Skipping malformed order during reconcile: %s", o)
        if _import_external_pending_orders(db, raw_orders, owner_uid, lots_by_ref):
            dirty = True
        if _reconcile_untagged_fills(db, raw_orders, owner_uid, lots_by_ref, siblings_to_cancel):
            dirty = True
        if _offset_opposing_lots(db, owner_uid, siblings_to_cancel):
            dirty = True
        if _cancel_stale_pending_lots(db, owner_uid):
            dirty = True
        if dirty:
            db.commit()
        return siblings_to_cancel
    except Exception:
        logger.exception("Lot reconciliation failed")
        return []
    finally:
        db.close()

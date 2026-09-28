from fastapi import APIRouter, HTTPException

from app.core import state
from app.core.deps import _require_legacy_auth
from app.schemas import (
    PositionItem,
    PositionsSummaryResponse,
    SymbolGroup,
)
from app.services.lots import _compute_avg_from_lots
from app.services.pnl import (
    _compute_position_pnl,
    _normalize_broker_position,
)
from app.services.reconciliation import _sync_positions_to_lots
from app.services.targets import _load_target_positions
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import OrderLot, SymbolTarget
from session_windows import today_ist
from ticker_manager import ticker_manager

router = APIRouter()


@router.get("/api/positions")
async def positions_legacy():
    """Get positions grouped by symbol with per-symbol and total PnL."""
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
        raw_positions = await broker.getPositions(token, credentials)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching positions: {e}")

    # One DB session for the whole handler — used both to compute per-symbol
    # buy/sell averages from our lots and to pull symbol-target rows below.
    _db = next(get_db())
    try:
        # Only OPEN/PARTIAL lots — the card shows the open position's cost
        # basis, so closed round-trips and never-filled orders don't belong.
        all_lots = (
            _db.query(OrderLot)
            .filter(OrderLot.owner_uid == auth.user_id)
            .filter(OrderLot.status.in_(["OPEN", "PARTIAL"]))
            .all()
        )
        lots_by_sym: dict[tuple[str, str], list[OrderLot]] = {}
        for l in all_lots:
            lots_by_sym.setdefault((l.exch, l.tsym), []).append(l)
        db_targets = {(st.exch, st.tsym): st for st in _db.query(SymbolTarget).all()}
    finally:
        _db.close()

    sm = get_scripmaster()
    # "MTM at close" cache belongs to a single trading day — drop it when the
    # session date rolls over so yesterday's frozen marks never leak into today.
    _today = today_ist().isoformat()
    if state._last_position_mtm_day != _today:
        state._last_position_mtm.clear()
        state._last_position_mtm_day = _today
    grouped: dict[str, list[PositionItem]] = {}
    position_ticker_keys: list[str] = []
    for pos in raw_positions:
        tsym = pos.get("tsym", "UNKNOWN")
        exch = pos.get("exch", "")
        upldprc = float(pos.get("upldprc", "0") or "0")
        netqty_int = int(pos.get("netqty", "0") or "0")
        lp = float(pos.get("lp", "0") or "0")
        scrip = sm.master.get(f"{exch}|{tsym}", {})
        lotsize = scrip.get("lotsize", "1") or "1"
        prcftr = float(scrip.get("prcftr", "1") or "1")
        tok = sm.get_token(exch, tsym) or ""
        # Positions card values P&L at LTP — matches the broker UI and the
        # lots card. Only the target auto-exit logic keeps bid/ask exit
        # pricing, since it prices an actual exit order.
        if netqty_int != 0 and tok:
            exit_price = state._current_ltps.get(f"{exch}|{tok}") or lp
        else:
            exit_price = lp
        norm_pos = _normalize_broker_position(pos)
        norm_pos["prcftr"] = prcftr
        total_pnl, urmtom = _compute_position_pnl(norm_pos, exit_price)
        # Derive realized as (total - urmtom) so the two display fields always
        # sum to total. Broker's `rpnl` can't be trusted: it omits CF-closure
        # realized P&L (rolled positions report rpnl≈0 even when actual booked
        # P&L is large), so deriving keeps the card internally consistent.
        rpnl = total_pnl - urmtom
        # Freeze the live MTM while open, so once this symbol goes flat we can
        # still show the mark it carried at the moment of exit instead of 0.
        mkey = f"{exch}|{tsym}"
        if netqty_int != 0:
            if urmtom:
                state._last_position_mtm[mkey] = urmtom
            mtm_at_close = urmtom
        else:
            mtm_at_close = state._last_position_mtm.get(mkey, 0.0)
        computed_buy, computed_sell = _compute_avg_from_lots(lots_by_sym.get((exch, tsym), []))
        if computed_buy is not None:
            buyavgprc_str = f"{computed_buy:.2f}"
            sellavgprc_str = f"{computed_sell:.2f}"
        else:
            buyavgprc_str = pos.get("buyavgprc") or pos.get("netavgprc") or pos.get("cfbuyavgprc") or pos.get("totbuyavgprc") or "0"
            sellavgprc_str = pos.get("sellavgprc") or pos.get("netavgprc") or pos.get("cfsellavgprc") or pos.get("totsellavgprc") or "0"
        # Open-basis display: zero out the side that's already closed against
        # the open qty (running-total avg for the closed side just clutters
        # the card and doesn't help the trader — realized P&L already reflects it).
        if netqty_int > 0:
            sellavgprc_str = "0"
        elif netqty_int < 0:
            buyavgprc_str = "0"
        item = PositionItem(
            tsym=tsym,
            exch=exch,
            prd=pos.get("prd", ""),
            s_prdt_ali=pos.get("s_prdt_ali", ""),
            netqty=pos.get("netqty", "0"),
            rpnl=rpnl,
            urmtom=urmtom,
            mtm_at_close=mtm_at_close,
            total_pnl=total_pnl,
            buyavgprc=buyavgprc_str,
            sellavgprc=sellavgprc_str,
            lp=pos.get("lp", "0"),
            upldprc=upldprc,
            lotsize=lotsize,
            prcftr=prcftr,
            token=tok,
            exit_price=exit_price,
            expiry=scrip.get("expd", ""),
        )
        grouped.setdefault(tsym, []).append(item)
        if tok and f"{exch}|{tok}" not in position_ticker_keys:
            position_ticker_keys.append(f"{exch}|{tok}")

    if position_ticker_keys:
        ticker_manager.subscribe(position_ticker_keys)

    new_pos_map: dict[str, list[dict]] = {}
    new_sym_targets: dict[str, dict] = {}
    # Retain flat rows too — global target needs their closed_pnl for round-trips.
    for pos in raw_positions:
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
        new_pos_map.setdefault(key, []).append(entry)
        st = db_targets.get((exch, tsym))
        if st:
            new_sym_targets[key] = {"enabled": st.enabled, "target_value": st.target_value}
    state._target_positions.clear()
    state._target_positions.update(new_pos_map)
    state._symbol_targets.clear()
    state._symbol_targets.update(new_sym_targets)

    symbol_groups = []
    for tsym, items in grouped.items():
        sym_pnl = sum(i.total_pnl for i in items)
        symbol_groups.append(SymbolGroup(
            symbol=tsym,
            exchange=items[0].exch,
            symbol_pnl=sym_pnl,
            positions=items,
        ))

    total_pnl = sum(g.symbol_pnl for g in symbol_groups)
    return PositionsSummaryResponse(symbol_groups=symbol_groups, total_pnl=total_pnl)


@router.post("/api/sync-positions")
async def sync_positions_endpoint():
    """Re-import any broker positions not yet tracked as lots."""
    auth = _require_legacy_auth()
    await _sync_positions_to_lots(auth.user_id)
    await _load_target_positions()
    return {"status": "ok"}

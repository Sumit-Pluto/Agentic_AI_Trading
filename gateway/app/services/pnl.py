"""P&L computation helpers.

Reads state via `state._current_bids` / `state._current_asks` / `state._current_ltps`
and `state._target_positions` — always via attribute access to survive rebinding.
"""
from datetime import timezone

from app.core import state
from brokers.shoonya.scripmaster import get_scripmaster
from db.models import OrderLot
from session_windows import IST, today_ist


def _opened_today(lot: OrderLot) -> bool:
    """True if the lot was entered in the current IST session date.

    opened_at is stored naive-UTC (models default = datetime.utcnow), so it
    must be tagged UTC and shifted to IST before comparing trading dates —
    otherwise a late-evening MCX entry can read as the wrong calendar day.
    """
    if not lot.opened_at:
        return False
    ist_date = lot.opened_at.replace(tzinfo=timezone.utc).astimezone(IST).date()
    return ist_date == today_ist()


def _exit_price(key: str, netqty: int) -> float | None:
    """Best bid for long positions, best ask for short; falls back to LTP."""
    if netqty > 0:
        return state._current_bids.get(key) or state._current_ltps.get(key)
    else:
        return state._current_asks.get(key) or state._current_ltps.get(key)


def _compute_position_pnl(pos: dict, exit_price: float) -> tuple[float, float]:
    """Returns (total_pnl, urmtom) for one broker position at exit_price.

    Universal cash-flow formula — matches Shoonya web UI's "P&L" exactly when
    exit_price == LTP, and works across pure intraday, pure carry-forward, and
    mixed (rollover) cases:

        total_pnl = ((daysellamt - daybuyamt)
                  - (cfbuyqty - cfsellqty) * upldprc
                  + netqty * exit_price) * prcftr

    Rationale: cash collected from today's sells, minus cash spent on today's
    buys, minus the basis of carried-forward qty at yesterday's settlement,
    plus current value of the open position. Doesn't trust `rpnl`/`urmtom`
    fields because Shoonya's `rpnl` omits CF-closure realized profits (visible
    bug: a rolled-over 9-lot position closed today reports rpnl≈0 even though
    the actual realized P&L is hundreds of rupees).

    `prcftr` (GNGD from scripmaster) converts (qty × price) into actual rupees.
    For NSE/NFO and most MCX contracts, prcftr=1. For MCX metals like LEAD /
    ZINC / ALUMINIUM (qty in MT, price in Rs/kg), prcftr=1000. For GOLD (qty
    in kg, price in Rs/10g), prcftr=100. Without it, e.g. LEAD reports P&L
    off by 1000× vs the broker.

    urmtom is the open-position MTM portion at exit_price; realized = total - urmtom.
    """
    netqty = pos["netqty"]
    upldprc = pos.get("upldprc", 0.0) or 0.0
    daybuyamt = pos.get("daybuyamt", 0.0) or 0.0
    daysellamt = pos.get("daysellamt", 0.0) or 0.0
    cfbuyqty = pos.get("cfbuyqty", 0) or 0
    cfsellqty = pos.get("cfsellqty", 0) or 0
    cfnet = cfbuyqty - cfsellqty
    prcftr = pos.get("prcftr", 1.0) or 1.0

    closed_pnl = (daysellamt - daybuyamt) - cfnet * upldprc
    open_value = netqty * exit_price if exit_price > 0 else 0.0
    total_pnl = (closed_pnl + open_value) * prcftr

    if netqty == 0 or exit_price <= 0:
        return total_pnl, 0.0

    # Open-position basis for the urmtom split: prefer broker-reported net avg,
    # fall back to total buy/sell avg, then to today's avg.
    avg = pos.get("netavgprc", 0.0) or 0.0
    if avg <= 0:
        avg = (pos.get("totbuyavgprc", 0.0) if netqty > 0 else pos.get("totsellavgprc", 0.0)) or 0.0
    if avg <= 0:
        avg = (pos.get("buyavgprc", 0.0) if netqty > 0 else pos.get("sellavgprc", 0.0)) or 0.0
    if avg <= 0:
        return total_pnl, 0.0
    urmtom = (exit_price - avg) * netqty * prcftr
    return total_pnl, urmtom


def _normalize_broker_position(pos: dict) -> dict:
    """Cast broker's string-typed position dict to a numeric dict for P&L math."""
    def _f(k: str) -> float:
        v = pos.get(k, 0)
        try:
            return float(v) if v not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0
    def _i(k: str) -> int:
        v = pos.get(k, 0)
        try:
            return int(float(v)) if v not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    daybuyqty = _i("daybuyqty")
    daysellqty = _i("daysellqty")
    daybuyavgprc = _f("daybuyavgprc")
    daysellavgprc = _f("daysellavgprc")
    # Always derive amounts as qty × raw avg price — never use the broker's
    # daybuyamt/daysellamt. Those are already rupee-converted (they include
    # prcftr and mult, per Noren's ActualBuyAvgPrice formula), so feeding them
    # into _compute_position_pnl's single ×prcftr would double-apply the
    # factor on MCX contracts like LEAD (1000×). Deriving keeps every term of
    # the P&L formula in raw price units, converted to rupees exactly once.
    daybuyamt = daybuyqty * daybuyavgprc
    daysellamt = daysellqty * daysellavgprc

    return {
        "netqty":        _i("netqty"),
        "netavgprc":     _f("netavgprc"),
        "buyavgprc":     _f("buyavgprc"),
        "sellavgprc":    _f("sellavgprc"),
        "totbuyavgprc":  _f("totbuyavgprc"),
        "totsellavgprc": _f("totsellavgprc"),
        "rpnl":          _f("rpnl"),
        "urmtom":        _f("urmtom"),
        "lp":            _f("lp"),
        "upldprc":       _f("upldprc"),
        "daybuyqty":     daybuyqty,
        "daysellqty":    daysellqty,
        "daybuyavgprc":  daybuyavgprc,
        "daysellavgprc": daysellavgprc,
        "daybuyamt":     daybuyamt,
        "daysellamt":    daysellamt,
        "cfbuyqty":      _i("cfbuyqty"),
        "cfsellqty":     _i("cfsellqty"),
        "tsym":          pos.get("tsym", ""),
        "exch":          pos.get("exch", ""),
        "prd":           pos.get("prd", "M"),
    }


def _calc_exchange_pnls() -> dict[str, float]:
    """Total live P&L per exchange, keyed by exchange code (NFO, MCX, …).

    Each cached position contributes to its exchange's running total using the
    same valuation rules as the whole-book total:

    Flat symbols (netqty==0) still contribute their `closed_pnl` — day realized
    minus CF basis — so an intraday round-trip that has been flattened keeps
    counting toward that exchange's cap. `_compute_position_pnl` with
    exit_price=0 returns just the closed_pnl for a flat position.

    Open positions with no live tick yet (e.g. right after startup) fall back
    to the REST-time lp, then to the position's own basis price (upldprc for
    CF, netavgprc for intraday). Valuing the open leg at its basis makes the
    unrealized part exactly zero while the already-realized day component
    still counts — skipping the row entirely would understate the total.
    """
    totals: dict[str, float] = {}
    for key, positions in state._target_positions.items():
        exch = key.split("|", 1)[0]
        for pos in positions:
            if pos["netqty"] == 0:
                totals[exch] = totals.get(exch, 0.0) + _compute_position_pnl(pos, 0.0)[0]
                continue
            price = (_exit_price(key, pos["netqty"])
                     or pos.get("lp", 0.0)
                     or pos.get("upldprc", 0.0)
                     or pos.get("netavgprc", 0.0))
            if not price or price <= 0:
                continue
            totals[exch] = totals.get(exch, 0.0) + _compute_position_pnl(pos, price)[0]
    return totals


def _lot_live_pnl(lot: OrderLot, exit_price: float | None) -> float:
    """Per-lot trader's view: realized + (exit_price - entry_price) * open_qty * prcftr.

    Always valued against the lot's own entry price — this is "what has this
    entry made me since I opened it." The settlement-basis (broker accounting)
    view lives on the Positions card.

    `prcftr` converts the (qty × price) product to actual rupees for contracts
    where the qty unit differs from the price unit (e.g. MCX LEAD: qty in MT,
    price in Rs/kg → prcftr=1000).
    """
    # carried_pnl is P&L rolled in from a prior contract (futures rollover): it
    # rides with the position so the new leg's P&L continues from where the old
    # one left off, but it stays out of this lot's own realized_pnl (history).
    realized = (lot.realized_pnl or 0.0) + (lot.carried_pnl or 0.0)
    if exit_price is None or lot.open_qty <= 0:
        return realized
    side_sign = 1 if lot.side == "B" else -1
    sm = get_scripmaster()
    scrip = sm.master.get(f"{lot.exch}|{lot.tsym}", {})
    prcftr = float(scrip.get("prcftr", "1") or "1")
    return realized + (exit_price - lot.avg_entry_price) * lot.open_qty * side_sign * prcftr


def _lot_mtm_pnl(lot: OrderLot, ltp: float | None, prev_close: float | None) -> float:
    """Day mark-to-market P&L for the open leg: (ltp - prev_close) * qty.

    How much the still-open quantity has moved *today* — anchored to yesterday's
    settlement (the tick's "c" field), not the lot's own entry price. Signed by
    side (a short gains when price falls below prev close) and scaled by prcftr
    so the result is in rupees, mirroring `_lot_live_pnl`.

    Returns 0.0 when there's no open qty or either price is missing (e.g. before
    the first tick, or for a symbol not currently streaming a close).

    Also 0.0 for a lot opened in the current session: prev_close is yesterday's
    settlement, but a same-day entry was never held at that close, so anchoring
    day-MTM to it would be wrong. Day-MTM only becomes meaningful once the
    position is carried overnight — the entry-anchored live P&L still shows
    today's move on a fresh position.
    """
    if ltp is None or prev_close is None or ltp <= 0 or prev_close <= 0 or lot.open_qty <= 0:
        return 0.0
    if _opened_today(lot):
        return 0.0
    side_sign = 1 if lot.side == "B" else -1
    sm = get_scripmaster()
    scrip = sm.master.get(f"{lot.exch}|{lot.tsym}", {})
    prcftr = float(scrip.get("prcftr", "1") or "1")
    return (ltp - prev_close) * lot.open_qty * side_sign * prcftr

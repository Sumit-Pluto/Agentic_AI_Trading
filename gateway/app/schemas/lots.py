from typing import Optional

from pydantic import BaseModel


class OrderLotResponse(BaseModel):
    id: int
    tsym: str
    exch: str
    side: str
    entry_qty: int
    open_qty: int
    avg_entry_price: float
    realized_pnl: float
    carried_pnl: float = 0.0
    live_pnl: float = 0.0
    mtm_pnl: float = 0.0      # day MTM on the open leg: (ltp - prev_close) * qty, side- & prcftr-adjusted
    prev_close: float = 0.0  # previous-day close (tick "c") used for mtm_pnl
    ltp: float = 0.0
    status: str
    opened_at: str
    closed_at: str = ""
    lotsize: int = 1
    prcftr: float = 1.0
    broker_entry_orderid: str = ""
    product_type: str = "M"
    token: str = ""
    pending_exit_orderid: str = ""
    is_external: bool = False
    is_rollover: bool = False
    is_reentry: bool = False
    is_temp_exit: bool = False  # TE tag: CLOSED lot kept visible across days for re-entry/roll
    is_persistent: bool = False
    source_service: Optional[str] = None  # name of the service that placed this lot (e.g. "snowball" → SB badge); None = human/Gateway UI
    avg_exit_price: float = 0.0
    exit_filled_qty: int = 0
    description: str = ""
    expd: str = ""           # contract expiry (from scripmaster), e.g. "31-JUL-2026"
    sym: str = ""            # underlying (from scripmaster), e.g. "LEAD"
    target_enabled: bool = False  # per-order auto-exit target armed on THIS lot
    target_value: float = 0.0     # this lot's own live-P&L threshold in ₹


class LotTargetUpdate(BaseModel):
    """Set/clear a per-order (per-lot) auto-exit target on one lot."""
    enabled: Optional[bool] = None
    target_value: Optional[float] = None


class LotExitRequest(BaseModel):
    qty: int
    price: float = 0.0       # 0 → use current bid/ask
    price_type: str = "LMT"  # "LMT" or "MKT"
    temp_exit: bool = False  # tag the lot TE so its row survives past today for re-entry/roll


class LotTempExitUpdate(BaseModel):
    """Set/clear the TE (temporary-exit) tag on a lot after the fact."""
    enabled: bool


class RolloverFailure(BaseModel):
    """A rollover whose far (entry) leg auto-failed after the near leg closed."""
    id: int
    near_tsym: str
    far_tsym: str
    error: str = ""
    created_at: str = ""


class LotRolloverRequest(BaseModel):
    """Roll a futures lot to a later expiry. Both legs (near exit + far entry)
    share one price_type; LMT prices default from cached bid/ask when 0."""
    target_tsym: str = ""     # "" → auto-resolve nearest next expiry
    qty: int = 0              # 0 → full open_qty
    price_type: str = "LMT"   # "LMT" (roll-cost control) or "MKT" (guaranteed roll)
    exit_price: float = 0.0   # near-leg LMT price; 0 → cached bid/ask
    entry_price: float = 0.0  # far-leg LMT price;  0 → cached ask/bid
    carry_target: bool = True
    carry_pnl: bool = True    # carry the near leg's realized P&L onto the far lot

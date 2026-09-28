from pydantic import BaseModel


class PositionItem(BaseModel):
    tsym: str
    exch: str
    prd: str
    s_prdt_ali: str
    netqty: str
    rpnl: float
    urmtom: float
    mtm_at_close: float = 0.0   # frozen urmtom from just before square-off; shown in place of 0 for flat rows
    total_pnl: float
    buyavgprc: str
    sellavgprc: str
    lp: str
    upldprc: float = 0.0
    lotsize: str = "1"
    prcftr: float = 1.0
    token: str = ""
    exit_price: float = 0.0
    expiry: str = ""


class SymbolGroup(BaseModel):
    symbol: str
    exchange: str
    symbol_pnl: float
    positions: list[PositionItem]


class PositionsSummaryResponse(BaseModel):
    symbol_groups: list[SymbolGroup]
    total_pnl: float


class FundsResponse(BaseModel):
    cash: float          # available cash / balance
    margin_used: float   # margin consumed
    payin: float         # today's deposit
    collateral: float    # collateral value

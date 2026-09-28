from typing import Optional
from pydantic import BaseModel


class ExchangeTargetResponse(BaseModel):
    exch: str
    enabled: bool
    target_value: float


class ExchangeTargetUpdate(BaseModel):
    enabled: Optional[bool] = None
    target_value: Optional[float] = None


class SymbolTargetResponse(BaseModel):
    exch: str
    tsym: str
    enabled: bool
    target_value: float
    carried_pnl: float = 0.0


class SymbolTargetUpdate(BaseModel):
    enabled: Optional[bool] = None
    target_value: Optional[float] = None
    carried_pnl: Optional[float] = None  # rollover carry; when omitted but target_value is set, resets to 0 (fresh manual target)

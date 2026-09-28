from typing import Optional
from pydantic import BaseModel


class PersistentOrderCreate(BaseModel):
    exchange: str
    tradingsymbol: str
    buy_or_sell: str          # "B" or "S"
    product_type: str = "M"
    price_type: str = "LMT"
    price: float = 0.0
    trigger_price: float = 0.0
    quantity: int
    target_enabled: bool = False
    target_value: float = 0.0
    description: str = ""


class PersistentOrderResponse(BaseModel):
    id: int
    exch: str
    tsym: str
    side: str
    product_type: str
    price_type: str
    price: float
    trigger_price: float
    quantity: int
    target_enabled: bool
    target_value: float
    status: str
    last_broker_orderid: str
    last_submitted_at: str = ""
    filled_lot_id: Optional[int] = None
    created_at: str

from pydantic import BaseModel


class PlaceOrderRequest(BaseModel):
    buy_or_sell: str  # "B" or "S"
    product_type: str  # "C" (CNC), "M" (NRML), "I" (MIS)
    exchange: str  # "NSE", "MCX", etc.
    tradingsymbol: str
    quantity: int
    discloseqty: int = 0
    price_type: str  # "LMT", "MKT"
    price: float = 0
    trigger_price: float = 0
    retention: str = "DAY"  # "DAY" or "IOC"
    remarks: str = ""
    description: str = ""   # user-supplied note, stored on OrderLot
    target_enabled: bool = False  # arm a per-order auto-exit target on the lot
    target_value: float = 0.0     # this lot's own live-P&L threshold in ₹
    reentry_source_lot_id: int | None = None  # re-entry: prior lot to carry realized+carried P&L forward from


class OrderMarginRequest(BaseModel):
    exchange: str
    tradingsymbol: str
    quantity: int
    buy_or_sell: str = "B"  # "B" or "S"
    product_type: str = "M"  # "C" (CNC), "M" (NRML), "I" (MIS)


class OrderItem(BaseModel):
    """One row of the broker order book, as served to API clients.

    The fill fields below are NOT decoration. This response model is what an
    engine reads to answer "did my order fill, how much, and at what price" —
    the order book is the only ground truth for that, because a broker
    acknowledgement of a placement says nothing about execution. They were
    absent from this model, so pydantic dropped them on the way out and every
    filled order came back to the caller as `fillshares=0, avgprc=0`.

    What that cost, concretely: a caller cannot distinguish COMPLETE-and-filled
    from COMPLETE-and-nothing-happened, so a resting exit order that had already
    executed read as unfilled and got placed AGAIN — repeatedly, once per
    reconcile pass — turning one long position into a growing real short. Entry
    verification degraded the same way, leaving every filled entry unconfirmed.

    So: anything the broker reports about EXECUTION belongs in this model, and
    removing a field from it is a trading-safety change, not a cleanup.
    """
    norenordno: str
    tsym: str
    exch: str
    prd: str
    trantype: str
    qty: str
    price: str
    pricetype: str
    status: str
    orderid: str
    pytime: str
    exch_orderid: str

    # --- execution truth ---
    fillshares: str = "0"   # units actually filled so far
    avgprc: str = "0"       # average fill price ("0" = broker reported none)
    prc: str = ""           # the order's own limit price, under the broker's key
    rejreason: str = ""     # why the broker refused it — otherwise the caller
                            # reports a rejection with no reason attached
    remarks: str = ""       # client tag; Shoonya blanks this on REST rows, but
                            # other brokers echo it and consumers match on it


class OrderBookResponse(BaseModel):
    orders: list[OrderItem]

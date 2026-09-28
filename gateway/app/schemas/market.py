from typing import Optional
from pydantic import BaseModel


class ScripSearchResult(BaseModel):
    tsym: str
    exch: str
    token: str
    instrumenttype: str = ""
    expd: str = ""
    opttype: str = ""
    strikeprice: str = ""
    lotsize: str = ""
    sym: str = ""


class ScripSearchResponse(BaseModel):
    results: list[ScripSearchResult]


class QuoteResponse(BaseModel):
    lp: str = ""   # last traded price
    bp1: str = ""  # best bid price
    sp1: str = ""  # best ask price
    bq1: str = ""  # best bid qty
    sq1: str = ""  # best ask qty
    o: str = ""
    h: str = ""
    l: str = ""
    c: str = ""
    v: str = ""
    ti: str = ""
    lot: str = ""
    oi: str = ""   # open interest (F&O only) — the broker sends it, this
                   # schema used to drop it, so no consumer could see OI
    ls: str = ""   # lot size as the broker reports it for this contract
    tsym: str = ""
    exch: str = ""
    token: str = ""


class OptionChainRequest(BaseModel):
    symbol: str           # underlying e.g. "NATURALGAS", "BANKNIFTY"
    exchange: str         # "MCX", "NFO", "NSE" (NSE auto-maps to NFO)
    expiry: str = ""      # "29-May-2025" — empty = nearest
    atm: float = 0.0      # ATM centre; 0 = resolve from the underlying's quote
    count: int = 10       # strikes above and below ATM
    use_cache: bool = True  # False forces a live rebuild, bypassing the TTL cache


class ChainLeg(BaseModel):
    """One side of one strike.

    The `lp`/`oi`/`v` strings are the original display fields and are kept as-is
    for the gateway UI. Everything below them is the same data typed, plus the
    book and lot size — a strategy priced off `lp` rather than the executable
    bid/ask reports edge that no order could have captured.
    """
    token: str
    tsym: str
    exch: str
    lp: str = ""
    oi: str = ""
    v: str = ""

    strike: float = 0.0
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    oi_num: int = 0
    prev_oi: int = 0      # 0 = unknown (see OptionOISnapshot), not "unchanged"
    volume: int = 0
    prev_close: float = 0.0
    lot_size: int = 1
    tick_size: float = 0.0
    quoted: bool = False  # False = no live quote; treat prices as absent
    # When THIS leg's quote was received (epoch seconds), or None if unquoted.
    # Shoonya's quote carries no time of its own and a chain is assembled over
    # several seconds at bounded concurrency, so the legs of one payload are
    # not contemporaneous — a consumer comparing legs against each other needs
    # to know how far apart in time they were taken.
    fetched_at: Optional[float] = None


class ChainRow(BaseModel):
    strike: float
    CE: Optional[ChainLeg] = None
    PE: Optional[ChainLeg] = None


class ChainQuality(BaseModel):
    """How complete this build actually is. Illiquid strikes routinely have no
    quote at all, so `quoted < legs` is normal, not an error — but a consumer
    that silently treats an unquoted leg as tradeable would be wrong."""
    legs: int = 0
    quoted: int = 0
    prev_oi_known: int = 0
    # The window this chain's quotes were taken over. Measured on live
    # RELIANCE: 5-8s for 34 legs, 8-9s for 62. A wide span means the payload is
    # not a simultaneous snapshot, which is exactly when cross-leg arbitrage
    # checks throw false positives.
    built_at: Optional[float] = None
    quote_span_s: float = 0.0


class OptionChainResponse(BaseModel):
    symbol: str
    exchange: str
    expiry: str = ""              # broker format, "25-AUG-2026"
    expiry_iso: str = ""          # "2026-08-25"
    expiries: list[str] = []
    expiries_iso: list[str] = []
    spot: float = 0.0             # underlying LTP (0 = could not resolve)
    lot_size: int = 1
    underlying_exchange: str = ""
    underlying_token: str = ""
    asof: str = ""
    cached: bool = False
    truncated: bool = False       # window capped without a resolvable ATM
    quality: Optional[ChainQuality] = None
    chain: list[ChainRow]
    error: str = ""

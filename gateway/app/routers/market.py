import asyncio
import time as _time
from datetime import datetime as _dt

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.core.deps import _require_legacy_auth
from app.schemas import (
    ChainLeg,
    ChainQuality,
    ChainRow,
    OptionChainRequest,
    OptionChainResponse,
    QuoteResponse,
    ScripSearchResponse,
    ScripSearchResult,
)
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.download_scripmaster import download_and_save
from brokers.shoonya.option_chain import build_option_chain
from brokers.shoonya.scripmaster import get_scripmaster, reload_scripmaster

router = APIRouter()


@router.post("/api/scripmaster/refresh")
async def refresh_scripmaster():
    """Re-download the Shoonya scripmaster from the CDN and reload it into memory.

    The CDN republishes the full contract universe every trading morning, so this
    single refresh drops expired contracts and picks up newly-listed ones — no
    per-expiry bookkeeping needed. Runs the blocking download in a threadpool so
    the event loop stays responsive.
    """
    try:
        stats = await run_in_threadpool(download_and_save)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scripmaster download failed: {e}")

    sm = await run_in_threadpool(reload_scripmaster)
    return {
        "total": stats["total"],
        "symbols_loaded": len(sm.master),
        "per_exchange": stats["per_exchange"],
        "updated_at": _dt.now().strftime("%H:%M:%S %d-%m-%Y"),
    }


@router.get("/api/search", response_model=ScripSearchResponse)
async def search_symbols(exchange: str = "", q: str = ""):
    """Search trading symbols — local scripmaster first, live Shoonya API fallback."""
    if len(q) < 2:
        return ScripSearchResponse(results=[])

    EXCH_PRIORITY = {"NSE": 0, "BSE": 1, "MCX": 2, "NFO": 3}

    def _sort_key(row: dict) -> tuple:
        exch = row.get("exch", "")
        instr = row.get("instrumenttype", "")
        tsym = row.get("tsym", "").lower()
        q_lower = q.lower()
        fuzzy_tier = 1 if row.get("_fuzzy") else 0
        exact = 0 if tsym == q_lower else 1
        is_option = 1 if "OPT" in instr else 0
        exch_rank = EXCH_PRIORITY.get(exch, 9)
        expd = row.get("expd", "")
        try:
            expiry = _dt.strptime(expd, "%d-%b-%Y")
        except Exception:
            expiry = _dt(9999, 1, 1)
        return (fuzzy_tier, exact, exch_rank, is_option, expiry)

    # Local scripmaster is fastest — use it when loaded
    sm = get_scripmaster()
    if sm.is_loaded():
        raw = sm.search(exchange, q)
        if raw:
            raw.sort(key=_sort_key)
            return ScripSearchResponse(results=[
                ScripSearchResult(
                    tsym=r.get("tsym", ""),
                    exch=r.get("exch", exchange),
                    token=r.get("token", ""),
                    instrumenttype=r.get("instrumenttype", ""),
                    expd=r.get("expd", ""),
                    opttype=r.get("opttype", ""),
                    strikeprice=r.get("strikeprice", ""),
                    lotsize=r.get("lotsize", ""),
                    sym=r.get("sym", ""),
                )
                for r in raw[:30]
            ])

    # Fallback: live Shoonya searchscrip API (requires auth)
    try:
        auth = _require_legacy_auth()
    except HTTPException:
        return ScripSearchResponse(results=[])

    broker = get_broker("shoonya")
    session = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    try:
        raw_live = await broker.searchSymbols(session, credentials, exchange, q)
        return ScripSearchResponse(results=[
            ScripSearchResult(
                tsym=r.get("tsym", ""),
                exch=r.get("exch", exchange),
                token=r.get("token", ""),
                # live API uses instname/optt/strprc/ls/symname instead of CSV column names
                instrumenttype=r.get("instname", r.get("instrumenttype", "")),
                expd=r.get("expd", ""),
                opttype=r.get("optt", r.get("opttype", "")),
                strikeprice=r.get("strprc", r.get("strikeprice", "")),
                lotsize=r.get("ls", r.get("lotsize", "")),
                sym=r.get("symname", r.get("sym", "")),
            )
            for r in raw_live[:20]
        ])
    except Exception:
        return ScripSearchResponse(results=[])


@router.get("/api/quote", response_model=QuoteResponse)
async def get_quote(exchange: str, token: str):
    """Fetch live quote for a symbol using its numeric token."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    session = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    try:
        raw = await broker.getQuote(session, credentials, symbol=token, exchange=exchange)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return QuoteResponse(
        lp=str(raw.get("lp", "")),
        bp1=str(raw.get("bp1", "")),
        sp1=str(raw.get("sp1", "")),
        bq1=str(raw.get("bq1", "")),
        sq1=str(raw.get("sq1", "")),
        o=str(raw.get("o", "")),
        h=str(raw.get("h", "")),
        l=str(raw.get("l", "")),
        c=str(raw.get("c", "")),
        v=str(raw.get("v", "")),
        ti=str(raw.get("ti", "")),
        lot=str(raw.get("lot", "")),
        oi=str(raw.get("oi", "")),
        ls=str(raw.get("ls", "")),
        tsym=str(raw.get("tsym", "")),
        exch=str(raw.get("exch", exchange)),
        token=str(raw.get("token", token)),
    )


def _normalize_candles(raw) -> list[dict]:
    """NorenApi returns keys like ssboe/into/inth/intl/intc/intv/time, newest
    first. Normalise to plain OHLCV dicts, oldest-first for indicator math."""
    candles: list[dict] = []
    for c in (raw or []):
        if not isinstance(c, dict):
            continue
        try:
            candles.append({
                "time": c.get("time") or c.get("ssboe", ""),
                "open": float(c.get("into") or c.get("o") or 0),
                "high": float(c.get("inth") or c.get("h") or 0),
                "low": float(c.get("intl") or c.get("l") or 0),
                "close": float(c.get("intc") or c.get("c") or 0),
                "volume": float(c.get("v") or c.get("intv") or 0),
            })
        except (ValueError, TypeError):
            continue
    candles.reverse()
    return candles


async def _fetch_one_candles(broker, session, credentials, exchange: str, token: str,
                             interval: int = 15, lookback_minutes: int = 0,
                             daily: bool = False, tradingsymbol: str = "",
                             days: int = 30) -> dict:
    """Fetch + normalise one instrument's candles. Shared by the single GET and
    the batch POST so both paths behave identically. Raises BrokerError."""
    now = _time.time()
    if daily:
        raw = await broker.getCandles(
            session, credentials,
            exchange=exchange, symbol_token=token,
            tradingsymbol=tradingsymbol, daily=True,
            starttime=now - (days * 86400), endtime=now,
        )
    else:
        lb = lookback_minutes if lookback_minutes > 0 else interval * 200
        raw = await broker.getCandles(
            session, credentials,
            exchange=exchange, symbol_token=token,
            interval=interval, starttime=now - (lb * 60), endtime=now,
        )
    return {"exchange": exchange, "token": token,
            "interval": ("1D" if daily else interval),
            "candles": _normalize_candles(raw)}


def _candles_session(auth):
    return SessionToken(token=auth.auth_token, broker_uid=auth.user_id,
                        issued_at="", broker_name="shoonya")


@router.get("/api/candles")
async def get_candles(
    exchange: str,
    token: str,
    interval: int = 15,
    lookback_minutes: int = 0,
    daily: bool = False,
    tradingsymbol: str = "",
    days: int = 30,
):
    """Fetch historical OHLC candles for indicators.

    Intraday: lookback_minutes (default = interval * 200 if 0).
    Daily: pass daily=true and tradingsymbol; days = lookback days.
    """
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    session = _candles_session(auth)
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    try:
        return await _fetch_one_candles(
            broker, session, credentials, exchange, token,
            interval=interval, lookback_minutes=lookback_minutes,
            daily=daily, tradingsymbol=tradingsymbol, days=days,
        )
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e))


class CandleQuery(BaseModel):
    exchange: str
    token: str
    interval: int = 15
    lookback_minutes: int = 0
    daily: bool = False
    tradingsymbol: str = ""
    days: int = 30


class CandlesBatchRequest(BaseModel):
    items: list[CandleQuery]
    concurrency: int = 8


@router.post("/api/candles/batch")
async def get_candles_batch(req: CandlesBatchRequest):
    """Fetch candles for many instruments in one call, concurrently.

    An intraday engine warms N underlyings + option legs at session start; doing
    that as N separate /api/candles round-trips is slow, so this batches them
    under a bounded semaphore. One instrument failing yields an ``error`` field
    for that item instead of failing the whole batch — the caller warms what it
    can. Concurrency is capped (Shoonya misroutes quotes under burst; candles
    are gentler but the same caution applies)."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    session = _candles_session(auth)
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}
    sem = asyncio.Semaphore(max(1, min(req.concurrency, 16)))

    async def _one(q: CandleQuery) -> dict:
        async with sem:
            try:
                return await _fetch_one_candles(
                    broker, session, credentials, q.exchange, q.token,
                    interval=q.interval, lookback_minutes=q.lookback_minutes,
                    daily=q.daily, tradingsymbol=q.tradingsymbol, days=q.days,
                )
            except BrokerError as e:
                return {"exchange": q.exchange, "token": q.token,
                        "error": str(e), "candles": []}
            except Exception as e:  # never let one bad leg kill the batch
                return {"exchange": q.exchange, "token": q.token,
                        "error": f"{type(e).__name__}: {e}", "candles": []}

    results = await asyncio.gather(*[_one(q) for q in req.items])
    return {"results": list(results)}


@router.post("/api/option-chain", response_model=OptionChainResponse)
async def option_chain_endpoint(req: OptionChainRequest):
    """Build option chain for a symbol using local scripmaster + live quotes."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    session = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    result = await build_option_chain(
        broker, session, credentials,
        symbol=req.symbol,
        exchange=req.exchange,
        expiry=req.expiry or None,
        atm=req.atm or None,
        count=req.count,
        use_cache=req.use_cache,
    )

    chain_rows = []
    for row in result.get("chain", []):
        ce = row.get("CE")
        pe = row.get("PE")
        chain_rows.append(ChainRow(
            strike=row["strike"],
            CE=ChainLeg(**ce) if ce else None,
            PE=ChainLeg(**pe) if pe else None,
        ))

    q = result.get("quality")
    return OptionChainResponse(
        symbol=result.get("symbol", req.symbol),
        exchange=result.get("exchange", req.exchange),
        expiry=result.get("expiry", ""),
        expiry_iso=result.get("expiry_iso", ""),
        expiries=result.get("expiries", []),
        expiries_iso=result.get("expiries_iso", []),
        spot=result.get("spot", 0.0),
        lot_size=result.get("lot_size", 1),
        underlying_exchange=result.get("underlying_exchange", ""),
        underlying_token=result.get("underlying_token", ""),
        asof=result.get("asof", ""),
        cached=result.get("cached", False),
        truncated=result.get("truncated", False),
        quality=ChainQuality(**q) if q else None,
        chain=chain_rows,
        error=result.get("error", ""),
    )

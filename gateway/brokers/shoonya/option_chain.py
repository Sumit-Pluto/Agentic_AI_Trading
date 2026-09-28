"""Option chain builder: local scripmaster for the contract universe, live
broker quotes for prices.

What this returns is the input to a pricing model (see the OptionSmith advisor),
not just a display grid, so it carries the whole quote — bid/ask, OI, volume,
previous close, lot size — rather than the last price alone. A strategy priced
off LTP instead of the executable bid/ask shows edge that does not exist.

Three things here are not obvious:

* **ATM is resolved from the underlying's own quote** when the caller doesn't
  supply it. Without a centre the `count` window cannot apply, and the old
  behaviour — return every listed strike — meant ~200 quote calls and a 20s
  response for a chain of which 90% was untradeable.
* **Quote fetches are bounded and verified.** The verification lives in the
  adapter (Shoonya misroutes quotes); the bound lives here because a chain is
  the only caller that asks for a hundred quotes at once.
* **prev_oi comes from the gateway's own snapshots** (`oi_history`), because no
  broker endpoint returns it.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from brokers.shoonya import oi_history
from brokers.shoonya.scripmaster import get_scripmaster

logger = logging.getLogger(__name__)

# Shoonya has no bulk-quote endpoint, so a chain is N sequential-ish calls.
# 8 keeps a 40-leg window near 5s without pushing the broker into the misrouting
# behaviour that gets worse with burst size.
_MAX_CONCURRENT_QUOTES = int(os.getenv("OPTION_CHAIN_QUOTE_CONCURRENCY", "8"))

# Repeat requests inside this window are served from the last build. A chain
# costs seconds of broker time; an advisor UI polling it, plus the gateway's own
# chain modal, would otherwise queue fetches faster than they complete.
_CACHE_TTL = float(os.getenv("OPTION_CHAIN_CACHE_TTL", "4.0"))

# Index options carry `sym` = NIFTY / BANKNIFTY, but the spot lives on NSE under
# a different tradingsymbol entirely ("NIFTY BANK"), so it cannot be derived by
# string manipulation. Equities resolve as {SYM}-EQ and need no entry here.
_INDEX_SPOT_TSYM = {
    "NIFTY": "NIFTY INDEX",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "NIFTYNXT50": "NIFTYNXT50",
}

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_locks: dict[tuple, asyncio.Lock] = {}


def _parse_expd(s: str) -> datetime:
    """'25-AUG-2026' -> datetime. Unparseable sorts last rather than raising,
    so one malformed scripmaster row cannot empty the expiry list."""
    try:
        return datetime.strptime(s, "%d-%b-%Y")
    except (ValueError, TypeError):
        return datetime(9999, 1, 1)


def _iso_expiry(s: str) -> str:
    """'25-AUG-2026' -> '2026-08-25'. Every consumer that does date maths wants
    ISO; the broker's own format is kept alongside for round-tripping back."""
    try:
        d, mon, y = s.split("-")
        return f"{int(y):04d}-{_MONTHS[mon.upper()[:3]]:02d}-{int(d):02d}"
    except Exception:
        return ""


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _underlying_ref(sm, symbol: str, exchange: str, options_exch: str,
                    expiry: str) -> Optional[tuple[str, str]]:
    """(exchange, token) for the instrument whose price is this chain's spot.

    Indices map through the table above; NSE/BSE equities are {SYM}-EQ; MCX
    options are on a future, so the spot is that future's own contract at the
    same expiry.
    """
    sym = symbol.upper()

    if options_exch == "NFO":
        idx = _INDEX_SPOT_TSYM.get(sym)
        if idx:
            row = sm.master.get(f"NSE|{idx}")
            if row:
                return "NSE", row["token"]
        eq_exch = exchange.upper() if exchange.upper() in ("NSE", "BSE") else "NSE"
        row = sm.master.get(f"{eq_exch}|{sym}-EQ")
        if row:
            return eq_exch, row["token"]
        return None

    if options_exch == "MCX":
        futures = [r for r in sm.sym_index.get(sym.lower(), [])
                   if r.get("exch") == "MCX"
                   and r.get("instrumenttype", "").startswith("FUT")]
        if not futures:
            return None
        exact = [r for r in futures if r.get("expd") == expiry]
        pick = min(exact or futures, key=lambda r: _parse_expd(r.get("expd", "")))
        return "MCX", pick["token"]

    return None


async def _fetch_quotes(broker, session_token, credentials,
                        targets: list[tuple[str, str]]) -> dict[str, dict]:
    """{token: quote} for (exchange, token) pairs, at bounded concurrency.

    A leg whose quote cannot be fetched — or which the adapter rejects as
    misrouted — is simply absent from the result. The caller emits that leg with
    empty prices, which every consumer already treats as untradeable; inventing
    a price would be the only worse option.
    """
    sem = asyncio.Semaphore(_MAX_CONCURRENT_QUOTES)
    failures: list[str] = []

    async def one(exch: str, tok: str):
        async with sem:
            try:
                q = await broker.getQuote(session_token, credentials,
                                          symbol=tok, exchange=exch)
                # ADDITIVE: stamp when this leg was received. Shoonya's quote
                # carries no time of its own, and a chain is built over several
                # seconds at bounded concurrency — measured 5-9s for 34-62 legs
                # — so legs in one payload are NOT contemporaneous. Downstream
                # no-arbitrage checks compare legs against each other and need
                # to know how far apart in time they were taken.
                if isinstance(q, dict):
                    q["fetched_at"] = time.time()
                return tok, q
            except Exception as e:
                failures.append(f"{exch}|{tok}: {e}")
                return tok, None

    results = await asyncio.gather(*[one(e, t) for e, t in targets])
    if failures:
        logger.warning("option chain: %d/%d quotes unavailable (first: %s)",
                       len(failures), len(targets), failures[0])
    return {tok: q for tok, q in results if isinstance(q, dict) and q}


def _leg(src: dict, exch: str, quote: dict, prev_oi: int) -> dict:
    """One side of one strike, carrying everything a pricing model needs."""
    oi = _i(quote.get("oi"))
    return {
        "token": src.get("token", ""),
        "tsym": src.get("tsym", ""),
        "exch": exch,
        # Legacy string fields — the gateway UI reads these; keep the shape.
        "lp": str(quote.get("lp", "") or ""),
        "oi": str(oi or ""),
        "v": str(quote.get("v", "0") or "0"),
        # Numeric fields for pricing. bid/ask are what an order actually fills
        # at; `lp` is where someone else traded, possibly hours ago.
        "ltp": _f(quote.get("lp")),
        "bid": _f(quote.get("bp1")),
        "ask": _f(quote.get("sp1")),
        "bid_qty": _i(quote.get("bq1")),
        "ask_qty": _i(quote.get("sq1")),
        "oi_num": oi,
        "prev_oi": prev_oi,
        "volume": _i(quote.get("v")),
        "prev_close": _f(quote.get("c")),
        "lot_size": _i(src.get("lotsize") or quote.get("ls"), 1),
        "tick_size": _f(src.get("ticksize") or quote.get("ti")),
        "strike": _f(src.get("strikeprice")),
        "quoted": bool(quote),
        "fetched_at": quote.get("fetched_at"),      # additive; None when unquoted
    }


async def build_option_chain(
    broker,
    session_token,
    credentials: dict,
    symbol: str,
    exchange: str,
    expiry: Optional[str] = None,
    atm: Optional[float] = None,
    count: int = 10,
    use_cache: bool = True,
) -> dict:
    """Build a structured option chain for a symbol using the local scripmaster.

    NSE/BSE equity symbols automatically map to NFO for their options.
    MCX futures map to MCX options (OPTFUT).

    Returns:
        {symbol, exchange, expiry, expiry_iso, expiries, expiries_iso, spot,
         lot_size, underlying_token, underlying_exchange, asof, quality,
         chain: [{strike, CE, PE}]}
    """
    key = (symbol.upper(), exchange.upper(), expiry or "", count,
           round(atm or 0.0, 2))
    if use_cache and _CACHE_TTL > 0:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return {**hit[1], "cached": True}
        # Serialise identical concurrent builds so the second caller waits for
        # the first's result instead of launching a duplicate 40-quote fetch.
        lock = _cache_locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = _cache.get(key)
            if hit and (time.time() - hit[0]) < _CACHE_TTL:
                return {**hit[1], "cached": True}
            result = await _build(broker, session_token, credentials, symbol,
                                  exchange, expiry, atm, count)
            _cache[key] = (time.time(), result)
            return result

    return await _build(broker, session_token, credentials, symbol, exchange,
                        expiry, atm, count)


async def _build(broker, session_token, credentials, symbol, exchange,
                 expiry, atm, count) -> dict:
    sm = get_scripmaster()
    if not sm.is_loaded():
        return {"symbol": symbol, "exchange": exchange, "expiries": [], "chain": [],
                "error": "Scripmaster not loaded — run download_scripmaster.py"}

    # NSE/BSE equities have options on NFO
    options_exch = exchange
    if exchange.upper() in ("NSE", "BSE"):
        options_exch = "NFO"

    sym_upper = symbol.upper()

    options = [row for row in sm.sym_index.get(sym_upper.lower(), [])
               if row.get("exch") == options_exch
               and row.get("opttype") in ("CE", "PE")]

    if not options:
        return {
            "symbol": symbol, "exchange": options_exch, "expiries": [], "chain": [],
            "error": f"No options found for {symbol} on {options_exch}",
        }

    expiries = sorted({r["expd"] for r in options if r.get("expd")}, key=_parse_expd)
    selected = expiry if (expiry and expiry in expiries) else (expiries[0] if expiries else "")
    exp_opts = [r for r in options if r.get("expd") == selected]

    strikes_map: dict[float, dict] = {}
    for row in exp_opts:
        strike = _f(row.get("strikeprice"), -1.0)
        if strike < 0:
            continue
        opt_type = row.get("opttype")
        if opt_type in ("CE", "PE"):
            strikes_map.setdefault(strike, {})[opt_type] = row

    sorted_strikes = sorted(strikes_map)

    base = {
        "symbol": symbol,
        "exchange": options_exch,
        "expiry": selected,
        "expiry_iso": _iso_expiry(selected),
        "expiries": expiries,
        "expiries_iso": [_iso_expiry(e) for e in expiries],
        "underlying_exchange": "",
        "underlying_token": "",
        "spot": 0.0,
        "lot_size": _i((exp_opts[0].get("lotsize") if exp_opts else 1), 1),
        "asof": datetime.now().isoformat(timespec="seconds"),
    }

    if not sorted_strikes:
        return {**base, "chain": []}

    # --- spot -----------------------------------------------------------
    # Needed both as the ATM centre and as a model input downstream, so it is
    # fetched even when the caller supplied `atm` explicitly.
    spot = 0.0
    ref = _underlying_ref(sm, sym_upper, exchange, options_exch, selected)
    if ref:
        base["underlying_exchange"], base["underlying_token"] = ref
        try:
            uq = await broker.getQuote(session_token, credentials,
                                       symbol=ref[1], exchange=ref[0])
            spot = _f((uq or {}).get("lp"))
        except Exception as e:
            logger.warning("option chain: underlying quote failed for %s (%s|%s): %s",
                           sym_upper, ref[0], ref[1], e)
    base["spot"] = spot

    centre = atm if (atm and atm > 0) else spot
    if centre > 0:
        atm_strike = min(sorted_strikes, key=lambda s: abs(s - centre))
        idx = sorted_strikes.index(atm_strike)
        lo = max(0, idx - count)
        hi = min(len(sorted_strikes), idx + count + 1)
        sorted_strikes = sorted_strikes[lo:hi]
    else:
        # No centre available: cap the window rather than fetching the entire
        # listed universe, which is a ~200-quote request the caller did not ask
        # for. The truncation is reported so it is never mistaken for the
        # complete chain.
        limit = count * 2 + 1
        if len(sorted_strikes) > limit:
            mid = len(sorted_strikes) // 2
            sorted_strikes = sorted_strikes[max(0, mid - count): mid + count + 1]
            base["truncated"] = True

    targets = [(options_exch, strikes_map[s][t]["token"])
               for s in sorted_strikes
               for t in ("CE", "PE")
               if strikes_map[s].get(t, {}).get("token")]

    quote_map = await _fetch_quotes(broker, session_token, credentials, targets)

    tokens = [t for _, t in targets]
    prev_oi_map = oi_history.previous(options_exch, tokens)

    chain = []
    for strike in sorted_strikes:
        row_dict: dict = {"strike": strike}
        for opt_type in ("CE", "PE"):
            src = strikes_map[strike].get(opt_type)
            if src:
                tok = src.get("token", "")
                row_dict[opt_type] = _leg(src, options_exch,
                                          quote_map.get(tok, {}),
                                          prev_oi_map.get(tok, 0))
            else:
                row_dict[opt_type] = None
        chain.append(row_dict)

    oi_history.record(options_exch,
                      {tok: _i(q.get("oi")) for tok, q in quote_map.items()})

    stamps = [q["fetched_at"] for q in quote_map.values()
              if isinstance(q.get("fetched_at"), (int, float))]
    return {
        **base,
        "chain": chain,
        "quality": {
            "legs": len(targets),
            "quoted": len(quote_map),
            "prev_oi_known": len(prev_oi_map),
            # ADDITIVE: the window this chain's quotes were taken over. A large
            # span means the legs are not a simultaneous snapshot, which is
            # exactly when cross-leg arbitrage checks produce false positives.
            "built_at": max(stamps) if stamps else None,
            "quote_span_s": (round(max(stamps) - min(stamps), 2)
                             if len(stamps) > 1 else 0.0),
        },
    }

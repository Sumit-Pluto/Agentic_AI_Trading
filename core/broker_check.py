"""Broker readiness checks — can we actually reach Shoonya and send orders?

run_checks(session, ...) performs READ-ONLY diagnostics:
    token      a session token is loaded
    order_book OrderBook reachable  -> proves auth + IP whitelist + OMS
    margin     Limits reachable     -> available cash for MIS
    positions  PositionBook reachable
    scrip      symbol resolves to an NSE tradingsymbol + token
    feed       websocket connected (when a hub with feed_status is given)

order_path_test(session, hub) goes one step further ON REQUEST ONLY:
it submits a REAL 1-share MIS BUY limit order priced far below LTP
(unfillable) and cancels it immediately.  Any structured OMS response —
acceptance+cancel, or a business rejection like "market closed" /
"price out of band" — proves the buy/sell request pipeline works
end-to-end.  Only auth/transport failures mean it does not.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("broker_check")

TEST_SYMBOL = "YESBANK"        # cheap + liquid — 1 share is a few rupees
TEST_DISCOUNT = 0.80           # limit at 20% below LTP: unfillable, and a
                               # band rejection also proves the order path

# emsg fragments that are BUSINESS rejections (order pipeline works) as
# opposed to auth/session/transport failures (it does not)
_BUSINESS_REJECTS = ("market", "closed", "band", "range", "price", "freeze",
                     "rms", "margin", "fund", "block", "not allowed",
                     "square off", "amo")


def resolve_front_month(exchange: str, underlying: str) -> dict | None:
    """Nearest non-expired futures contract for `underlying` on `exchange`
    (e.g. MCX/GOLDPETAL -> the front-month GOLDPETALddMONyy). Returns
    {'tsym','token','lotsize','expiry'} or None. Uses the daily-cached
    scrip master — no broker login needed."""
    from datetime import datetime
    try:
        from shoonya_client import load_scripmaster
        master = load_scripmaster(exchange)
    except Exception as e:
        log.warning("scrip master %s failed: %s", exchange, e)
        return None
    up = underlying.upper().strip()
    best = None                              # (expiry_epoch, row, tsym)
    for tsym, row in master.items():
        sym = (row.get("Symbol") or "").upper().strip()
        inst = (row.get("Instrument") or "").upper()
        if sym != up or not inst.startswith("FUT"):
            continue
        raw = (row.get("Expiry") or "").strip()
        try:
            exp = datetime.strptime(raw.title(), "%d-%b-%Y").timestamp()
        except ValueError:
            continue
        if exp < time.time():                # already expired
            continue
        if best is None or exp < best[0]:
            best = (exp, row, tsym)
    if best is None:
        return None
    _, row, tsym = best
    try:
        tick = float(row.get("TickSize") or 0) or 0.05
    except (TypeError, ValueError):
        tick = 0.05
    return {"tsym": tsym, "token": row.get("Token"),
            "lotsize": int(float(row.get("LotSize") or 1)),
            "ticksize": tick, "expiry": row.get("Expiry")}


def marketable_price(quote: dict, side: str, ticksize: float = 0.05,
                     buffer_pct: float = 0.005) -> float | None:
    """A LIMIT price that fills like a MARKET order — priced through the
    opposite side of the book (this account rejects API MKT orders).

    BUY  -> best ask (sp1, else LTP) * (1+buffer), rounded UP to the tick;
    SELL -> best bid (bp1, else LTP) * (1-buffer), rounded DOWN to the tick.
    Returns None if no usable price. The limit caps the worst fill, so a
    modest buffer only risks a marginally worse price, never a runaway."""
    import math
    s = str(side).upper()[:1]

    def _f(key):
        try:
            return float(quote.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    lp = _f("lp")
    t = ticksize if ticksize and ticksize > 0 else 0.05
    if s == "B":
        base = _f("sp1") or lp
        if base <= 0:
            return None
        return round(math.ceil(base * (1 + buffer_pct) / t) * t, 2)
    base = _f("bp1") or lp
    if base <= 0:
        return None
    return round(math.floor(base * (1 - buffer_pct) / t) * t, 2)


def place_marketable(session, *, side: str, exchange: str, tsym: str,
                     qty: int, product: str = "I", remarks: str = "api",
                     get_quote=None, ticksize: float = 0.05,
                     buffer_pct: float = 0.005) -> dict:
    """Place an order that behaves like a MARKET order, robust to accounts
    that block API market orders.

    Tries a true MKT order first (price 0). If the account rejects it with
    the algo guard ("ALGO_CHK: MKT Order type not allowed for API order"),
    falls back ONCE to a marketable LIMIT priced through the book — safe,
    because a rejected order never reached the book (no double-fire).
    Any other failure is re-raised unchanged. Returns the raw place_order
    response dict. `get_quote` is a no-arg callable returning a quote dict;
    it is called ONLY on the fallback path, so the happy path stays fast."""
    B = "B" if str(side).upper()[:1] == "B" else "S"
    try:
        return session.place_order(
            buy_or_sell=B, exchange=exchange, tradingsymbol=tsym,
            quantity=qty, price_type="MKT", price=0.0, product=product,
            remarks=remarks)
    except Exception as e:
        msg = str(e).lower()
        mkt_blocked = ("mkt order type not allowed" in msg
                       or "algo_chk" in msg
                       or "market order" in msg and "not allow" in msg)
        if not mkt_blocked:
            raise                             # a real failure — surface it
        quote = None
        if get_quote is not None:
            try:
                quote = get_quote()
            except Exception:
                quote = None
        px = marketable_price(quote or {}, B, ticksize, buffer_pct)
        if px is None:
            raise RuntimeError(
                f"account blocks API MKT orders and no quote for a "
                f"marketable-limit fallback ({tsym}); original: {e}")
        log.warning("%s: account blocks API MKT — retrying as marketable "
                    "LIMIT @ %.2f", tsym, px)
        return session.place_order(
            buy_or_sell=B, exchange=exchange, tradingsymbol=tsym,
            quantity=qty, price_type="LMT", price=px, product=product,
            remarks=remarks)


def _step(name: str, ok: bool, detail: str = "", **extra) -> dict:
    d = {"name": name, "ok": bool(ok), "detail": detail}
    d.update(extra)
    return d


def run_checks(session, hub=None) -> list[dict]:
    """Read-only diagnostics. Never places an order. Never raises."""
    out: list[dict] = []

    tok = bool(getattr(session, "access_token", None))
    out.append(_step("token", tok,
                     "session token loaded" if tok
                     else "not logged in — run the daily login"))
    if not tok:
        return out

    try:
        rows = session.order_book()
        out.append(_step("order_book", True,
                         f"OrderBook reachable ({len(rows or [])} order(s) "
                         f"today) — auth + IP whitelist OK"))
    except Exception as e:
        out.append(_step("order_book", False, f"OrderBook failed: {e}"))

    try:
        lim = session.limits()
        lim = lim[0] if isinstance(lim, list) and lim else lim
        cash = None
        if isinstance(lim, dict):
            for k in ("cash", "payin", "brkcollamt"):
                try:
                    cash = float(lim.get(k))
                    break
                except (TypeError, ValueError):
                    continue
        out.append(_step("margin", True,
                         f"Limits reachable — available cash: "
                         f"{('₹%.2f' % cash) if cash is not None else 'n/a'}",
                         cash=cash))
    except Exception as e:
        out.append(_step("margin", False, f"Limits failed: {e}"))

    try:
        rows = session.positions()
        n = len(rows or [])
        out.append(_step("positions", True,
                         f"PositionBook reachable ({n} row(s))"))
    except Exception as e:
        out.append(_step("positions", False, f"PositionBook failed: {e}"))

    if hub is not None:
        try:
            row = hub.cash_row(TEST_SYMBOL)
            ok = bool(row and row.get("Token"))
            out.append(_step(
                "scrip", ok,
                f"{TEST_SYMBOL} -> "
                f"{(row or {}).get('TradingSymbol') or TEST_SYMBOL + '-EQ'} "
                f"token {(row or {}).get('Token')}" if ok
                else f"scrip master cannot resolve {TEST_SYMBOL}"))
        except Exception as e:
            out.append(_step("scrip", False, f"scrip master failed: {e}"))
        try:
            fs = hub.feed_status() if hasattr(hub, "feed_status") else None
            if fs is not None:
                out.append(_step(
                    "feed", bool(fs.get("connected")),
                    f"websocket {'connected' if fs.get('connected') else 'DOWN'}"
                    f" — {fs.get('instruments', 0)} live instrument(s)"
                    + ("" if fs.get("attached") else " (not attached)")))
        except Exception:
            pass
    return out


def order_path_test(session, hub=None, symbol: str = TEST_SYMBOL) -> dict:
    """PLACES A REAL (unfillable) ORDER and cancels it. Call only with the
    user's explicit confirmation. Returns {'ok', 'proved', 'steps': [...]}
    where proved means the buy/sell request pipeline demonstrably works."""
    steps: list[dict] = []
    tsym, ltp = f"{symbol}-EQ", None
    if hub is not None:
        try:
            row = hub.cash_row(symbol)
            if row:
                tsym = row.get("TradingSymbol") or tsym
                q = hub.cash_quote(symbol)
                if q and q.get("lp"):
                    ltp = float(q["lp"])
        except Exception:
            pass
    if ltp is None:
        try:                     # quote directly if the hub couldn't
            row = hub.cash_row(symbol) if hub else None
            if row and row.get("Token"):
                q = session.get_quote("NSE", row["Token"])
                if q and q.get("lp"):
                    ltp = float(q["lp"])
        except Exception:
            pass
    if ltp:
        price = max(round(ltp * TEST_DISCOUNT / 0.05) * 0.05, 0.05)
        steps.append(_step("quote", True, f"{symbol} LTP ₹{ltp:.2f} — test "
                                          f"limit ₹{price:.2f} (unfillable)"))
    else:
        price = 0.05             # floor price: never fills, still hits OMS
        steps.append(_step("quote", False,
                           f"no LTP for {symbol} — using ₹{price:.2f}"))

    try:
        resp = session.place_order(
            buy_or_sell="B", exchange="NSE", tradingsymbol=tsym,
            quantity=1, price_type="LMT", price=round(price, 2),
            product="I", remarks="orderpathtest")
        order_no = str((resp or {}).get("norenordno") or "")
        steps.append(_step("place", True,
                           f"OMS ACCEPTED test order (no {order_no}) — "
                           f"buy/sell pipeline works", order_no=order_no))
        proved = True
        time.sleep(0.5)
        try:
            session.cancel_order(order_no)
            steps.append(_step("cancel", True, "test order cancelled"))
        except Exception as e:
            steps.append(_step("cancel", False,
                               f"CANCEL FAILED — cancel order {order_no} "
                               f"manually in the broker terminal! ({e})"))
    except Exception as e:
        msg = str(e)
        business = any(w in msg.lower() for w in _BUSINESS_REJECTS)
        proved = business
        steps.append(_step(
            "place", business,
            (f"OMS rejected the test order as expected ({msg}) — the "
             f"request REACHED Shoonya, pipeline works") if business else
            f"order request FAILED before the OMS ({msg}) — pipeline NOT "
            f"proven (auth/IP/transport problem)"))
    ok = all(s["ok"] for s in steps if s["name"] != "quote")
    return {"ok": ok, "proved": proved, "steps": steps}

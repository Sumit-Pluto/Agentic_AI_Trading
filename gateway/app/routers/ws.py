import datetime as _dt
import re as _re

from fastapi import (APIRouter, Depends, HTTPException, WebSocket,
                     WebSocketDisconnect)

from app.core.security import principal_from_token, require_scope
from db.engine import SessionLocal
from ticker_manager import ticker_manager

# NFO trading symbols encode expiry, right and strike: WIPRO29SEP26P260.
# The scripmaster carries only tsym + token, so they are parsed back out.
_TSYM_PAT = _re.compile(r"^(.+?)(\d{2}[A-Z]{3}\d{2})([CP])([0-9.]+)$")

# Parsing the scripmaster means walking ~100k rows and regex-matching each
# tsym. A universe sweep chunks into ~36 calls, so doing it per call was 36 full
# scans per pass — measured as the dominant cost, well above the feed settle
# time it was meant to be measuring. Built once, reused, rebuilt when the
# scripmaster itself changes.
_IDX_CACHE: dict = {}


def _contract_index(sm, exch: str) -> dict:
    """{symbol: [{tok, tsym, exp, right, strike}, ...]} for one exchange."""
    key = (exch, id(sm.master), len(sm.master))
    hit = _IDX_CACHE.get("key")
    if hit == key and exch in _IDX_CACHE.get("data", {}):
        return _IDX_CACHE["data"][exch]
    if hit != key:
        _IDX_CACHE.clear()
        _IDX_CACHE["key"], _IDX_CACHE["data"] = key, {}

    out: dict = {}
    for k, row in sm.master.items():
        if not k.startswith(f"{exch}|"):
            continue
        tsym = (row.get("tsym") or "").strip().upper()
        tok = (row.get("token") or "").strip()
        m = _TSYM_PAT.match(tsym)
        if not (m and tok):
            continue
        sym, exp_s, right, strike = m.groups()
        try:
            exp = _dt.datetime.strptime(exp_s, "%d%b%y").date()
        except ValueError:
            continue
        try:
            lot = int(row.get("lotsize") or 0)
        except (TypeError, ValueError):
            lot = 0
        out.setdefault(sym, []).append(
            {"tok": tok, "tsym": tsym, "exp": exp, "right": right,
             "strike": float(strike), "lot": lot})
    _IDX_CACHE["data"][exch] = out
    return out


def _require_live_feed(tm) -> None:
    """Refuse to drive the socket unless it is actually open.

    `_broker` stays a live object after the session drops, so checking it is not
    enough: every subscribe against the dead socket triggers a reconnect attempt
    and a disconnect callback. Measured, one universe sweep against a down
    session produced 2,683 disconnect callbacks and wedged the event loop —
    taking the Gateway down for every other consumer, not just the caller.
    """
    if tm is None or getattr(tm, "_broker", None) is None \
            or not getattr(tm, "_ws_open", False):
        raise HTTPException(
            503, "broker feed is not open — connect the broker before probing")


router = APIRouter()

# Diagnostics live on their own router so they can carry an auth dependency.
# The WebSocket route above cannot: browsers cannot set an Authorization header
# on a WS handshake, so it authenticates from a ?token= query param itself.
# These probes drive the LIVE broker socket (subscribe/unsubscribe), so they
# require the same "market" scope as any other market-data route.
probe_router = APIRouter(dependencies=[Depends(require_scope("market"))])


@router.websocket("/api/ws/ticker")
async def ws_ticker(websocket: WebSocket):
    # Browsers can't set Authorization headers on a WebSocket, so the JWT is
    # passed as a ?token= query param. Accept a human user OR a service that holds
    # the "market" scope; reject before accepting otherwise.
    token = websocket.query_params.get("token") or ""
    db = SessionLocal()
    try:
        principal = principal_from_token(token, db)
    finally:
        db.close()
    allowed = principal is not None and (
        principal.kind == "user" or "market" in principal.scopes
    )
    if not allowed:
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    await ticker_manager.add_client(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # symbols are already "EXCHANGE|TOKEN" format from the frontend
            symbols = data.get("symbols", [])
            action = data.get("action")
            # feed: "touchline" (default) or "depth" (SNAPQUOTE — carries OI +
            # the bid/ask ladder). Depth frames arrive tagged {"type":"depth"}.
            feed = (data.get("feed") or "touchline")
            if action == "subscribe":
                ticker_manager.subscribe(symbols, feed=feed)
            elif action == "unsubscribe":
                ticker_manager.unsubscribe(symbols)
    except WebSocketDisconnect:
        ticker_manager.remove_client(websocket)


# ── TEMPORARY DIAGNOSTIC ────────────────────────────────────────────────
# Answers one question: does the Shoonya DEPTH feed carry open interest?
# It matters because OI is the only field the touchline feed lacks, and
# obtaining it over REST costs one round-trip per leg — measured 6.9 quotes/s,
# i.e. ~15 minutes for a 180-symbol universe. If depth carries OI, the entire
# OI scan becomes a push subscription instead of a poll.
# Remove once answered.
@probe_router.get("/api/_probe/depth")
async def probe_depth(exch: str = "NFO", token: str = "", seconds: float = 6.0):
    import asyncio as _a
    from NorenRestApiPy.NorenApi import FeedType
    from ticker_manager import ticker_manager as _tm

    if not token:
        return {"error": "pass ?token=<option token>&exch=NFO"}
    seen: dict[str, dict] = {}

    def _probe(msg: dict) -> None:
        t = str(msg.get("t", "?"))
        if t not in seen:
            seen[t] = dict(msg)

    sym = f"{exch}|{token}"
    _require_live_feed(_tm)
    _tm.raw_probe = _probe
    try:
        _tm._broker.ws_subscribe(sym, FeedType.SNAPQUOTE)
        await _a.sleep(seconds)
    finally:
        try:
            _tm._broker.ws_unsubscribe(sym, FeedType.SNAPQUOTE)
        except Exception:
            pass
        _tm.raw_probe = None
    return {
        "subscribed": sym,
        "message_types_seen": sorted(seen),
        "fields_by_type": {t: sorted(m.keys()) for t, m in seen.items()},
        "has_oi": {t: [k for k in m if "oi" in k.lower()] for t, m in seen.items()},
        "sample": {t: {k: v for k, v in list(m.items())[:14]} for t, m in seen.items()},
    }


@probe_router.get("/api/_probe/bulk")
async def probe_bulk(symbols: str = "RELIANCE,INFY,TCS,SBIN,ITC,HDFCBANK",
                     per_symbol: int = 40, seconds: float = 20.0):
    """Can one connection carry the whole universe, and how heavy is the stream?

    Enumerates option tokens straight from the scripmaster (no REST at all),
    subscribes them as depth in one message, and measures what comes back:
    how many distinct tokens actually report, the message rate, and how many
    OI changes land per second. Those three numbers decide whether a pushed
    OI scan over 180 symbols is viable.
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    sm = get_scripmaster()
    if not sm.is_loaded():
        return {"error": "scripmaster not loaded"}

    wanted = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    tokens: list[str] = []
    per: dict[str, int] = {}
    for key, row in sm.master.items():
        if not key.startswith("NFO|"):
            continue
        sym = (row.get("sym") or "").strip().upper()
        if sym not in wanted or row.get("instname", "").startswith("FUT"):
            continue
        if per.get(sym, 0) >= per_symbol:
            continue
        tok = (row.get("token") or "").strip()
        if tok:
            tokens.append(f"NFO|{tok}")
            per[sym] = per.get(sym, 0) + 1
    if not tokens:
        return {"error": "no option tokens found", "looked_for": wanted}

    stats = {"msgs": 0, "oi_msgs": 0, "types": {}, "tokens_seen": set(),
             "oi_changes": 0}
    last_oi: dict[str, str] = {}

    def _probe(msg: dict) -> None:
        t = str(msg.get("t", "?"))
        stats["types"][t] = stats["types"].get(t, 0) + 1
        stats["msgs"] += 1
        tk = msg.get("tk")
        if tk:
            stats["tokens_seen"].add(tk)
        if "oi" in msg:
            stats["oi_msgs"] += 1
            if tk and last_oi.get(tk) not in (None, msg["oi"]):
                stats["oi_changes"] += 1
            if tk:
                last_oi[tk] = msg["oi"]

    _require_live_feed(_tm)
    _tm.raw_probe = _probe
    t0 = _t.time()
    try:
        # one message, all tokens — the library joins the list with '#'
        _tm._broker.ws_subscribe(tokens, FeedType.SNAPQUOTE)
        await _a.sleep(seconds)
    finally:
        try:
            _tm._broker.ws_unsubscribe(tokens, FeedType.SNAPQUOTE)
        except Exception:
            pass
        _tm.raw_probe = None
    el = _t.time() - t0
    return {
        "symbols": wanted,
        "subscribed_tokens": len(tokens),
        "elapsed_s": round(el, 1),
        "distinct_tokens_reporting": len(stats["tokens_seen"]),
        "coverage_pct": round(100 * len(stats["tokens_seen"]) / len(tokens), 1),
        "messages": stats["msgs"],
        "msgs_per_sec": round(stats["msgs"] / el, 1),
        "messages_with_oi": stats["oi_msgs"],
        "oi_changes_observed": stats["oi_changes"],
        "by_type": stats["types"],
    }


@probe_router.get("/api/_probe/rotate")
async def probe_rotate(batch: int = 100, batches: int = 4, settle: float = 2.0):
    """Depth caps at ~100 concurrent subs. Can we ROTATE through the universe?

    Subscribes a batch, waits for its acks (each carries oi/poi), unsubscribes,
    moves on. The number that matters is seconds-per-batch: at 100 tokens a
    batch, a 6,120-leg universe is 62 batches, so a 2s batch is a ~2-minute
    full OI sweep against ~15 minutes over REST.
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)

    sm = get_scripmaster()
    toks = [f"NFO|{(r.get('token') or '').strip()}"
            for k, r in sm.master.items()
            if k.startswith("NFO|") and (r.get("token") or "").strip()
            and not (r.get("instname", "") or "").startswith("FUT")]
    toks = toks[:batch * batches]
    if not toks:
        return {"error": "no tokens"}

    results, t_all = [], _t.time()
    for i in range(0, len(toks), batch):
        chunk = toks[i:i + batch]
        seen: set = set()
        oi_seen = 0

        def _probe(msg: dict, _s=seen):
            if msg.get("t") in ("dk", "df") and msg.get("tk"):
                _s.add(msg["tk"])

        _tm.raw_probe = _probe
        t0 = _t.time()
        try:
            _tm._broker.ws_subscribe(chunk, FeedType.SNAPQUOTE)
            await _a.sleep(settle)
        finally:
            try:
                _tm._broker.ws_unsubscribe(chunk, FeedType.SNAPQUOTE)
            except Exception:
                pass
            _tm.raw_probe = None
        results.append({"batch": i // batch + 1, "sent": len(chunk),
                        "acked": len(seen),
                        "secs": round(_t.time() - t0, 2)})
    total = _t.time() - t_all
    acked = sum(r["acked"] for r in results)
    per_batch = total / max(len(results), 1)
    return {
        "batch_size": batch, "batches": len(results),
        "tokens_attempted": len(toks), "tokens_acked": acked,
        "total_s": round(total, 1),
        "sec_per_batch": round(per_batch, 2),
        "projected_universe_sweep_s": round(per_batch * (6120 / batch), 1),
        "detail": results,
    }


@probe_router.get("/api/_probe/oiscan")
async def probe_oiscan(symbols: str = "", per_symbol: int = 34,
                       batch: int = 100, settle: float = 0.4,
                       rounds: int = 2, verify: int = 6, gap: float = 15.0,
                       exch: str = "NFO"):
    """Full OI scan over N symbols via rotating depth subs. Answers three
    questions in one run:

      SPEED    seconds for a whole sweep of the requested universe
      LIVENESS re-runs the sweep and reports how many legs' OI actually moved
      CORRECT  cross-checks a sample of depth OI against the REST quote for the
               same token — the two must agree, or one of them is lying
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from app.core import auth as _auth_module
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)

    sm = get_scripmaster()
    wanted = [x.strip().upper() for x in symbols.split(",") if x.strip()]

    # The scripmaster only carries tsym + token, so expiry, right and strike
    # have to come out of the symbol string: WIPRO29SEP26P260. Selecting
    # blindly grabs far-dated illiquid strikes whose OI never moves — which is
    # exactly what made an earlier run show 53% coverage and zero OI changes.
    import re as _re
    from datetime import datetime as _dtm
    PAT = _re.compile(r"^(.+?)(\d{2}[A-Z]{3}\d{2})([CP])([0-9.]+)$")

    by_sym: dict = {}
    EX = exch.strip().upper()
    for key, row in sm.master.items():
        if not key.startswith(f"{EX}|"):
            continue
        tsym = (row.get("tsym") or "").strip().upper()
        tok = (row.get("token") or "").strip()
        m = PAT.match(tsym)
        if not (m and tok):
            continue
        sym, exp_s, right, strike = m.groups()
        if wanted and sym not in wanted:
            continue
        try:
            exp = _dtm.strptime(exp_s, "%d%b%y").date()
        except ValueError:
            continue
        by_sym.setdefault(sym, []).append(
            {"tok": tok, "tsym": tsym, "exp": exp, "right": right,
             "strike": float(strike)})

    toks, per, meta = [], {}, {}
    for sym, rows in by_sym.items():
        nearest = min(r["exp"] for r in rows)          # front expiry only
        front = [r for r in rows if r["exp"] == nearest]
        strikes = sorted({r["strike"] for r in front})
        if not strikes:
            continue
        atm = strikes[len(strikes) // 2]               # median strike ~ ATM
        front.sort(key=lambda r: (abs(r["strike"] - atm), r["right"]))
        for r in front[:per_symbol]:
            toks.append(f"{EX}|{r['tok']}")
            meta[r["tok"]] = {"sym": sym, "tsym": r["tsym"]}
        per[sym] = min(len(front), per_symbol)

    if not toks:
        return {"error": "no tokens", "wanted": wanted}

    async def sweep() -> dict:
        book: dict = {}

        def _probe(msg: dict):
            if msg.get("t") in ("dk", "df") and msg.get("tk"):
                cur = book.setdefault(msg["tk"], {})
                for f in ("oi", "poi", "lp", "bp1", "sp1", "ft", "v"):
                    if f in msg:
                        cur[f] = msg[f]

        _tm.raw_probe = _probe
        t0 = _t.time()
        try:
            for i in range(0, len(toks), batch):
                chunk = toks[i:i + batch]
                _tm._broker.ws_subscribe(chunk, FeedType.SNAPQUOTE)
                await _a.sleep(settle)
                try:
                    _tm._broker.ws_unsubscribe(chunk, FeedType.SNAPQUOTE)
                except Exception:
                    pass
        finally:
            _tm.raw_probe = None
        return {"secs": round(_t.time() - t0, 2), "book": book}

    sweeps = []
    for n in range(max(rounds, 1)):
        if n:
            await _a.sleep(gap)      # OI needs time to actually move
        sweeps.append(await sweep())

    first, last = sweeps[0], sweeps[-1]
    with_oi = {k: v for k, v in last["book"].items() if "oi" in v}
    moved = sum(1 for k, v in with_oi.items()
                if k in first["book"] and "oi" in first["book"][k]
                and first["book"][k]["oi"] != v["oi"])

    # correctness: depth OI vs REST OI for the same tokens
    checks, mism = [], 0
    api = getattr(_tm._broker, "_api", None)
    sample = [k for k in with_oi][:max(verify, 0)]
    for tk in sample:
        try:
            q = api.get_quotes(exchange=EX, token=tk) or {}
            rest_oi = str(q.get("oi", ""))
            ws_oi = str(with_oi[tk].get("oi", ""))
            ok = rest_oi == ws_oi
            if not ok:
                mism += 1
            checks.append({"token": tk, "tsym": meta.get(tk, {}).get("tsym"),
                           "ws_oi": ws_oi, "rest_oi": rest_oi, "match": ok})
        except Exception as e:
            checks.append({"token": tk, "error": f"{type(e).__name__}: {e}"})

    return {
        "symbols_scanned": len(per), "legs_requested": len(toks),
        "sweep_seconds": [s["secs"] for s in sweeps],
        "legs_with_oi": len(with_oi),
        "coverage_pct": round(100 * len(with_oi) / len(toks), 1),
        "legs_oi_changed_between_sweeps": moved,
        "has_prev_day_oi": sum(1 for v in with_oi.values() if "poi" in v),
        "has_feed_time": sum(1 for v in with_oi.values() if "ft" in v),
        "rest_crosscheck": {"sampled": len(checks), "mismatches": mism,
                            "detail": checks},
    }


@probe_router.get("/api/_probe/raw")
async def probe_raw(exch: str = "NFO", symbol: str = "RELIANCE",
                    per_symbol: int = 30, settle: float = 0.6):
    """Capture RAW depth messages plus the token->tsym map we subscribed under.

    Exists so the consumer (OptionSmith's DepthBook) can be exercised on real
    broker output rather than reconstructed fixtures — including the identity
    check, which needs the same expectation map the subscription used.
    """
    import asyncio as _a
    import re as _re
    from datetime import datetime as _dtm
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    EX = exch.strip().upper()
    SY = symbol.strip().upper()
    PAT = _re.compile(r"^(.+?)(\d{2}[A-Z]{3}\d{2})([CP])([0-9.]+)$")
    rows = []
    for key, row in get_scripmaster().master.items():
        if not key.startswith(f"{EX}|"):
            continue
        ts = (row.get("tsym") or "").strip().upper()
        tok = (row.get("token") or "").strip()
        m = PAT.match(ts)
        if not (m and tok and m.group(1) == SY):
            continue
        try:
            exp = _dtm.strptime(m.group(2), "%d%b%y").date()
        except ValueError:
            continue
        rows.append({"tok": tok, "ts": ts, "exp": exp,
                     "strike": float(m.group(4))})
    if not rows:
        return {"error": f"no options for {SY} on {EX}"}
    front = min(r["exp"] for r in rows)
    rows = [r for r in rows if r["exp"] == front]
    strikes = sorted({r["strike"] for r in rows})
    atm = strikes[len(strikes) // 2]
    rows.sort(key=lambda r: abs(r["strike"] - atm))
    rows = rows[:per_symbol]

    expect = {r["tok"]: r["ts"] for r in rows}
    toks = [f"{EX}|{r['tok']}" for r in rows]
    msgs: list = []
    _require_live_feed(_tm)
    _tm.raw_probe = lambda m: msgs.append(dict(m))
    try:
        _tm._broker.ws_subscribe(toks, FeedType.SNAPQUOTE)
        await _a.sleep(settle)
    finally:
        try:
            _tm._broker.ws_unsubscribe(toks, FeedType.SNAPQUOTE)
        except Exception:
            pass
        _tm.raw_probe = None
    return {"exchange": EX, "symbol": SY, "expiry": str(front),
            "subscribed": len(toks), "expect": expect,
            "messages": msgs, "message_count": len(msgs)}


@probe_router.get("/api/_probe/oisweep")
async def probe_oisweep(symbols: str = "", strikes: int = 10,
                        per_symbol: int = 0, spots: str = "",
                        stale_after: float = 900.0, legs: int = 0,
                        batch: int = 100, settle: float = 0.6,
                        expiry_index: int = 0, exch: str = "NFO"):
    """One rotating depth sweep, aggregated PER SYMBOL.

    /api/_probe/oiscan answers "is the transport sound?" with universe-wide
    totals. This answers "what is each symbol's open interest right now?", which
    is what a per-symbol view needs.

    `settle` is how long to wait before moving to the next batch, and it IS the
    cost of a sweep: wall-clock is settle x batch-count.

    It has to cover the UNSUBSCRIBE, not just the acks, and those are different
    numbers. A single 84-leg batch in isolation is fully acked in 0.15s — but
    rotating batches back to back at that speed leaves the previous
    subscriptions alive, so they eat the 100-slot ceiling and every later batch
    is refused. Measured over 8 sequential batches, average coverage of the
    late ones:

        0.2s -> 19.1%   (collapses: 84 acked, then 65, 41, 22, 0)
        0.4s -> 82.4%   (still degrading)
        0.6s -> 98.8%   holds
        1.0s -> 98.8%   no better

    0.6s is the floor for SUSTAINED rotation. Tuning it on a single batch is how
    it ended up at 0.2s and quietly returned 1% coverage across a universe.

    expiry_index selects among the symbol's own sorted expiries: 0 = nearest
    (current month), 1 = the one after (next month). It is resolved per symbol
    rather than globally, because F&O names do not all share an expiry date.
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)

    sm = get_scripmaster()
    wanted = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    EX = exch.strip().upper()

    idx = _contract_index(sm, EX)
    by_sym = {k: v for k, v in idx.items() if not wanted or k in wanted}

    # The scripmaster keeps recently-expired rows. Sorting by date and taking
    # the first therefore picks a DEAD contract for anything with weeklies —
    # NIFTY resolved to an expiry a week in the past and returned no legs at
    # all, because there is nothing left to subscribe to.
    today = _dt.date.today()
    spot_of = {}
    for part in (spots or "").split(","):
        if ":" in part:
            k, _, v = part.partition(":")
            try:
                spot_of[k.strip().upper()] = float(v)
            except ValueError:
                pass

    toks, meta, chosen = [], {}, {}
    for sym, rows in by_sym.items():
        exps = sorted({r["exp"] for r in rows if r["exp"] >= today})
        if not exps:
            continue
        # clamp rather than fail: a name may not have a far month listed yet
        idx = min(max(expiry_index, 0), len(exps) - 1)
        exp = exps[idx]
        legs = [r for r in rows if r["exp"] == exp]
        strikes_avail = sorted({r["strike"] for r in legs})
        if not strikes_avail:
            continue
        # The median LISTED strike is only a proxy for at-the-money, and a poor
        # one wherever the listed range is wide relative to price: IDEA's median
        # is 14.0, so a +/-10-strike window around it spent half its legs on
        # strikes that never trade. A caller-supplied spot fixes that.
        ref = spot_of.get(sym)
        atm = (min(strikes_avail, key=lambda k: abs(k - ref)) if ref
               else strikes_avail[len(strikes_avail) // 2])
        if per_symbol:
            # legacy: a flat contract budget, nearest-ATM first
            legs.sort(key=lambda r: (abs(r["strike"] - atm), r["right"]))
            picked = legs[:per_symbol]
        else:
            # ATM +/- `strikes`, BOTH rights on each — a strike is only useful
            # for wall/PCR work if its call and put are both present
            near = set(sorted(strikes_avail,
                              key=lambda k: abs(k - atm))[:2 * strikes + 1])
            picked = [r for r in legs if r["strike"] in near]
            picked.sort(key=lambda r: (abs(r["strike"] - atm), r["right"]))
        chosen[sym] = {"expiry": exp.isoformat(), "atm_strike": atm,
                       "lot_size": (picked[0]["lot"] if picked else 0),
                       "strikes_covered": len({r["strike"] for r in picked}),
                       "spot": ref, "anchored_on": "spot" if ref else "median_strike",
                       "expiry_index_used": idx,
                       "expiries_available": len(exps),
                       "legs_requested": len(picked)}
        for r in picked:
            toks.append(f"{EX}|{r['tok']}")
            meta[r["tok"]] = {"sym": sym, "right": r["right"],
                              "strike": r["strike"], "tsym": r["tsym"],
                              "lot": r["lot"]}

    if not toks:
        return {"error": "no tokens for the requested symbols",
                "wanted": wanted, "expiry_index": expiry_index}

    book: dict = {}
    acked: set = set()
    mismatch_samples: list = []
    rejects = {"unknown_token": 0, "symbol_mismatch": 0, "delta_before_ack": 0,
               "oi_negative": 0, "oi_not_lot_multiple": 0, "oi_unparseable": 0,
               "stale_leg": 0}

    def _probe(msg: dict):
        """Merge one message, or reject it and say why.

        A stream needs the checks a REST payload gets for free. The token is not
        enough on its own: ~17% of REST option-quote requests came back as a
        DIFFERENT instrument, and the price was well-formed every time — so the
        only defence is the identity the message carries about itself.
        """
        t, tk = msg.get("t"), msg.get("tk")
        if t not in ("dk", "df") or not tk:
            return
        want = meta.get(tk)
        if want is None:
            rejects["unknown_token"] += 1        # we never subscribed to this
            return
        ts = (msg.get("ts") or "").strip().upper()
        if ts:
            if ts != want["tsym"]:
                rejects["symbol_mismatch"] += 1  # broker sent the wrong contract
                if len(mismatch_samples) < 6:
                    mismatch_samples.append(
                        {"token": tk, "expected": want["tsym"], "got": ts,
                         "exch": msg.get("e")})
                return
            acked.add(tk)
        elif tk not in acked:
            # `df` names no symbol, so a delta before its full ack is a leg
            # whose identity was never verified. Deltas MERGE; they may not
            # create.
            rejects["delta_before_ack"] += 1
            return
        cur = book.setdefault(tk, {})
        for f in ("oi", "poi", "lp", "bp1", "sp1", "ft", "v"):
            if f in msg:
                cur[f] = msg[f]

    _tm.raw_probe = _probe
    t0 = _t.time()
    batches = 0
    try:
        for i in range(0, len(toks), max(batch, 1)):
            chunk = toks[i:i + max(batch, 1)]
            _tm._broker.ws_subscribe(chunk, FeedType.SNAPQUOTE)
            await _a.sleep(settle)
            try:
                _tm._broker.ws_unsubscribe(chunk, FeedType.SNAPQUOTE)
            except Exception:
                pass
            batches += 1
    finally:
        _tm.raw_probe = None
    elapsed = round(_t.time() - t0, 2)

    now_epoch = _t.time()
    out: dict = {}
    for tk, v in book.items():
        m = meta.get(tk)
        if not m or "oi" not in v:
            continue
        rec = out.setdefault(m["sym"], {"call_oi": 0, "put_oi": 0,
                                        "legs_with_oi": 0, "ltp_sum": 0.0,
                                        "feed_times": [], "defects": 0})
        try:
            oi = int(float(v["oi"]))
        except (TypeError, ValueError):
            rejects["oi_unparseable"] += 1
            rec["defects"] += 1
            continue
        if oi < 0:
            rejects["oi_negative"] += 1        # open interest cannot be short
            rec["defects"] += 1
            continue
        # Open interest is a quantity and contracts trade in lots, so it is
        # always a whole number of lots. Verified 158/158 across lot sizes from
        # 50 to 71,475. A value that fails this is corrupt, or belongs to a
        # different contract whose lot size happens not to divide it.
        lot = m.get("lot") or 0
        if lot and oi % lot:
            rejects["oi_not_lot_multiple"] += 1
            rec["defects"] += 1
            continue
        ft = v.get("ft")
        if ft:
            try:
                if now_epoch - float(ft) > stale_after:
                    rejects["stale_leg"] += 1
                    rec["defects"] += 1
                    continue
            except (TypeError, ValueError):
                pass
        rec["call_oi" if m["right"] == "C" else "put_oi"] += oi
        rec["legs_with_oi"] += 1
        if v.get("ft"):
            rec["feed_times"].append(v["ft"])
        if legs:
            # These legs already passed identity, lot-multiple and staleness.
            # Returning them costs nothing extra on the wire we already opened,
            # and it is the difference between a number per symbol and a chain
            # the strategy layer can actually price.
            def _f(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0
            rec.setdefault("legs", []).append({
                "strike": m["strike"], "right": m["right"], "oi": oi,
                "poi": int(_f(v.get("poi"))), "ltp": _f(v.get("lp")),
                "bid": _f(v.get("bp1")), "ask": _f(v.get("sp1")),
                "ft": _f(v.get("ft")) or None, "tsym": m["tsym"],
                # volume is a liquidity signal the chain store had no way to
                # carry, because the sweep never captured it
                "volume": int(_f(v.get("v"))),
            })

    symbols_out = {}
    for sym, info in chosen.items():
        got = out.get(sym)
        rec = dict(info)
        if got:
            c, p = got["call_oi"], got["put_oi"]
            rec.update({
                "defects": got.get("defects", 0),
                "call_oi": c, "put_oi": p, "total_oi": c + p,
                "pcr": round(p / c, 3) if c else None,
                "legs_with_oi": got["legs_with_oi"],
                "coverage_pct": round(
                    100 * got["legs_with_oi"] / max(info["legs_requested"], 1), 1),
                "feed_time": max(got["feed_times"]) if got["feed_times"] else None,
            })
            if legs and got.get("legs"):
                rec["legs"] = sorted(got["legs"],
                                     key=lambda r: (r["strike"], r["right"]))
                fts = [l["ft"] for l in rec["legs"] if l["ft"]]
                # the window this symbol's legs were quoted over, from the
                # broker's own clock — what the gate needs to forgive skew
                rec["quote_span_s"] = round(max(fts) - min(fts), 2) if len(fts) > 1 else 0.0
                rec["built_at"] = max(fts) if fts else None
        else:
            rec.update({"call_oi": 0, "put_oi": 0, "total_oi": 0, "pcr": None,
                        "legs_with_oi": 0, "coverage_pct": 0.0,
                        "feed_time": None})
        symbols_out[sym] = rec

    legs_with_oi = sum(s["legs_with_oi"] for s in symbols_out.values())
    return {
        "sweep_seconds": elapsed,
        "batches": batches,
        "symbols_scanned": len(symbols_out),
        "legs_requested": len(toks),
        "legs_with_oi": legs_with_oi,
        "coverage_pct": round(100 * legs_with_oi / max(len(toks), 1), 1),
        "expiry_index": expiry_index,
        "quality": {
            "rejects": rejects,
            "total_rejected": sum(rejects.values()),
            "legs_acked": len(acked),
            "clean": sum(rejects.values()) == 0,
            "mismatch_samples": mismatch_samples,
        },
        "symbols": symbols_out,
    }


@probe_router.get("/api/_probe/fnolist")
def probe_fnolist(exch: str = "NFO"):
    """Every underlying that currently has option contracts listed.

    The scripmaster is the live truth about what is in F&O; a bundled list goes
    stale at every NSE eligibility revision (renames included — GMRINFRA became
    GMRAIRPORT), and a stale name is a silently missing row, not an error.
    """
    from brokers.shoonya.scripmaster import get_scripmaster
    EX = exch.strip().upper()
    idx = _contract_index(get_scripmaster(), EX)
    out = []
    for sym, rows in idx.items():
        exps = sorted({r["exp"] for r in rows})
        out.append({"symbol": sym, "contracts": len(rows),
                    "expiries": [e.isoformat() for e in exps[:4]]})
    out.sort(key=lambda r: r["symbol"])
    return {"exchange": EX, "count": len(out), "underlyings": out}


@probe_router.get("/api/_probe/spots")
async def probe_spots(symbols: str = "", batch: int = 100, settle: float = 0.25,
                      exch: str = "NSE"):
    """Last traded price for many underlyings, over the depth feed.

    214 REST quotes would take ~30s at the measured 6.9/sec. The same names go
    up the depth socket in three batches for well under a second, and the option
    sweep needs the number only to centre its strike window — so this runs once
    per pass, not once per chunk.
    """
    import asyncio as _a
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)
    sm = get_scripmaster()
    wanted = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    EX = exch.strip().upper()

    # cash-segment rows carry the plain trading symbol, options do not
    tok_of = {}
    for k, row in sm.master.items():
        if not k.startswith(f"{EX}|"):
            continue
        tsym = (row.get("tsym") or "").strip().upper()
        base = tsym[:-3] if tsym.endswith("-EQ") else tsym
        if _TSYM_PAT.match(tsym):
            continue                      # an option, not the underlying
        if wanted and base not in wanted:
            continue
        tok_of.setdefault(base, (row.get("token") or "").strip())

    toks = [f"{EX}|{t}" for t in tok_of.values() if t]
    if not toks:
        return {"error": "no underlying tokens", "wanted": wanted}

    seen: dict = {}

    def _probe(msg: dict):
        if msg.get("tk") and msg.get("lp"):
            seen[msg["tk"]] = msg["lp"]

    _tm.raw_probe = _probe
    import time as _t
    t0 = _t.time()
    try:
        for i in range(0, len(toks), max(batch, 1)):
            chunk = toks[i:i + max(batch, 1)]
            _tm._broker.ws_subscribe(chunk, FeedType.TOUCHLINE)
            await _a.sleep(settle)
            try:
                _tm._broker.ws_unsubscribe(chunk, FeedType.TOUCHLINE)
            except Exception:
                pass
    finally:
        _tm.raw_probe = None

    out = {}
    for sym, tok in tok_of.items():
        lp = seen.get(tok)
        if lp:
            try:
                out[sym] = float(lp)
            except (TypeError, ValueError):
                pass
    return {"elapsed_s": round(_t.time() - t0, 2), "requested": len(toks),
            "resolved": len(out), "spots": out}


@probe_router.get("/api/_probe/oicadence")
async def probe_oicadence(symbols: str = "RELIANCE", strikes: int = 10,
                          samples: int = 6, gap: float = 10.0,
                          settle: float = 0.2, exch: str = "NFO"):
    """How often does open interest ACTUALLY change?

    The scan's whole economics rest on an assumption nobody had measured: that
    OI refreshes slowly enough for tiering to skip most symbols most of the
    time. If OI updates every few seconds, tiering is throwing away real
    information; if it updates twice a session, sweeping every 24s is burning
    the broker connection for nothing.

    Samples the same legs repeatedly and reports, per leg, how many samples
    changed and the shortest observed gap between two different values. The
    answer is a property of the exchange feed, not of our code, so it has to be
    measured rather than assumed.
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)
    sm = get_scripmaster()
    EX = exch.strip().upper()
    wanted = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    idx = _contract_index(sm, EX)
    today = _dt.date.today()

    toks, meta = [], {}
    for sym in wanted:
        rows = idx.get(sym) or []
        exps = sorted({r["exp"] for r in rows if r["exp"] >= today})
        if not exps:
            continue
        legs = [r for r in rows if r["exp"] == exps[0]]
        avail = sorted({r["strike"] for r in legs})
        if not avail:
            continue
        atm = avail[len(avail) // 2]
        near = set(sorted(avail, key=lambda k: abs(k - atm))[:2 * strikes + 1])
        for r in [x for x in legs if x["strike"] in near]:
            toks.append(f"{EX}|{r['tok']}")
            meta[r["tok"]] = {"sym": sym, "tsym": r["tsym"]}
    if not toks:
        return {"error": "no tokens", "wanted": wanted}

    # token -> [(t, oi), ...]
    series: dict = {}

    def _probe(msg: dict):
        tk = msg.get("tk")
        if msg.get("t") in ("dk", "df") and tk in meta and "oi" in msg:
            try:
                series.setdefault(tk, []).append((_t.time(), int(float(msg["oi"]))))
            except (TypeError, ValueError):
                pass

    t0 = _t.time()
    _tm.raw_probe = _probe
    try:
        for n in range(max(samples, 2)):
            if n:
                await _a.sleep(gap)
            for i in range(0, len(toks), 100):
                chunk = toks[i:i + 100]
                _tm._broker.ws_subscribe(chunk, FeedType.SNAPQUOTE)
                await _a.sleep(settle)
                try:
                    _tm._broker.ws_unsubscribe(chunk, FeedType.SNAPQUOTE)
                except Exception:
                    pass
    finally:
        _tm.raw_probe = None

    per_leg, changed_legs, min_gaps = [], 0, []
    for tk, pts in series.items():
        vals = [v for _, v in pts]
        distinct = len(set(vals))
        changes, last_t, last_v, gaps = 0, None, None, []
        for t, v in pts:
            if last_v is not None and v != last_v:
                changes += 1
                if last_t:
                    gaps.append(t - last_t)
            if last_v is None or v != last_v:
                last_t, last_v = t, v
        if changes:
            changed_legs += 1
            min_gaps.append(min(gaps) if gaps else None)
        per_leg.append({"tsym": meta[tk]["tsym"], "samples": len(pts),
                        "distinct_values": distinct, "changes": changes,
                        "min_change_gap_s": round(min(gaps), 1) if gaps else None})

    per_leg.sort(key=lambda r: -r["changes"])
    gaps = [g for g in min_gaps if g]
    gaps.sort()
    return {
        "symbols": wanted, "legs_tracked": len(series),
        "samples_per_leg": samples, "sample_gap_s": gap,
        "window_s": round(_t.time() - t0, 1),
        "legs_that_changed": changed_legs,
        "legs_static": len(series) - changed_legs,
        "pct_changed": round(100.0 * changed_legs / max(len(series), 1), 1),
        "median_change_gap_s": round(gaps[len(gaps) // 2], 1) if gaps else None,
        "fastest_change_gap_s": round(gaps[0], 1) if gaps else None,
        "detail": per_leg[:25],
    }


@probe_router.get("/api/_probe/poolshare")
async def probe_poolshare(hold: int = 60, then: int = 100, settle: float = 1.5,
                          exch: str = "NFO"):
    """Is the 100-subscription ceiling per FEED TYPE, or shared across them?

    It decides whether open positions can hold a standing TOUCHLINE subscription
    without stealing slots from the scanner's rotating DEPTH sweep. If the pool
    is shared, every held position makes every sweep slower; if it is per type,
    they are independent budgets and the position book is free.

    Holds `hold` touchline subscriptions open, then asks for `then` depth
    subscriptions and counts how many are acked.
    """
    import asyncio as _a
    import time as _t
    from NorenRestApiPy.NorenApi import FeedType
    from brokers.shoonya.scripmaster import get_scripmaster
    from ticker_manager import ticker_manager as _tm

    _require_live_feed(_tm)
    EX = exch.strip().upper()
    idx = _contract_index(get_scripmaster(), EX)
    today = _dt.date.today()

    toks = []
    for sym, rows in sorted(idx.items()):
        exps = sorted({r["exp"] for r in rows if r["exp"] >= today})
        if not exps:
            continue
        for r in [x for x in rows if x["exp"] == exps[0]][:40]:
            toks.append(f"{EX}|{r['tok']}")
        if len(toks) > hold + then + 60:
            break
    held, probe = toks[:hold], toks[hold:hold + then]
    if not probe:
        return {"error": "not enough tokens"}

    seen_t, seen_d = set(), set()

    def _p(msg):
        tk = msg.get("tk")
        if not tk:
            return
        if msg.get("t") in ("tk", "tf"):
            seen_t.add(tk)
        elif msg.get("t") in ("dk", "df"):
            seen_d.add(tk)

    _tm.raw_probe = _p
    try:
        # 1. hold touchline open, deliberately NOT unsubscribed
        _tm._broker.ws_subscribe(held, FeedType.TOUCHLINE)
        await _a.sleep(settle)
        touch_acked = len(seen_t)

        # 2. now ask for depth while those are still held
        _tm._broker.ws_subscribe(probe, FeedType.SNAPQUOTE)
        await _a.sleep(settle)
        depth_acked = len(seen_d)
    finally:
        for chunk, ft in ((held, FeedType.TOUCHLINE), (probe, FeedType.SNAPQUOTE)):
            try:
                _tm._broker.ws_unsubscribe(chunk, FeedType.TOUCHLINE if ft == FeedType.TOUCHLINE else FeedType.SNAPQUOTE)
            except Exception:
                pass
        await _a.sleep(settle)
        _tm.raw_probe = None

    return {
        "touchline_held": len(held), "touchline_acked": touch_acked,
        "depth_requested": len(probe), "depth_acked": depth_acked,
        "depth_pct": round(100.0 * depth_acked / max(len(probe), 1), 1),
        "verdict": ("pools look SEPARATE — depth was unaffected by held touchline"
                    if depth_acked >= 0.9 * len(probe) else
                    "pools look SHARED — held touchline reduced depth acks"),
    }

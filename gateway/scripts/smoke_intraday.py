#!/usr/bin/env python3
"""Phase-0 acceptance smoke test for the intraday broker Gateway.

Exercises everything the intraday engine will depend on, against a RUNNING
gateway with a LIVE (or cached) Shoonya session:

    • /health
    • /api/auth/service-token         (mint a market-scoped service JWT)
    • /api/option-chain               (NIFTY chain w/ spot + legs)
    • /api/candles  +  /api/candles/batch   (underlying warm-up bars)   [NEW]
    • /api/_probe/oisweep             (per-strike OI + PCR)
    • WS /api/ws/ticker  feed=touchline  and  feed=depth (OI frames)    [NEW]

Run this ON THE VPS: Shoonya login is IP-whitelisted, so a live session only
exists where the registered static IP is. Off-VPS you can still run it against
a gateway that has a cached token, but a fresh login will fail with "invalid IP".

Usage:
    # register a service client once (prints CLIENT_ID / CLIENT_SECRET):
    #   python -m scripts.register_service --name intraday-engine
    GATEWAY_BASE_URL=http://127.0.0.1:8000 \
    GATEWAY_CLIENT_ID=... GATEWAY_CLIENT_SECRET=... \
    python scripts/smoke_intraday.py --symbol NIFTY

Exit code is non-zero if any REQUIRED check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WS_BASE = os.getenv("GATEWAY_WS_URL", BASE.replace("http", "ws") + "/api/ws/ticker")
CLIENT_ID = os.getenv("GATEWAY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GATEWAY_CLIENT_SECRET", "")

_OK, _FAIL, _SKIP = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m", "\033[33mSKIP\033[0m"
_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "", required: bool = True) -> bool:
    tag = _OK if ok else (_FAIL if required else _SKIP)
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")
    _results.append((name, ok or not required, detail))
    return ok


def _req(method: str, path: str, token: str = "", body: dict | None = None,
         timeout: float = 20.0) -> tuple[int, dict]:
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--ws-seconds", type=float, default=8.0)
    args = ap.parse_args()

    print(f"Gateway smoke test → {BASE}  (symbol={args.symbol})\n")

    # 1. health
    st, body = _req("GET", "/health")
    _check("health", st == 200 and body.get("status") == "ok", f"status={st}")

    # 2. service token
    token = ""
    if CLIENT_ID and CLIENT_SECRET:
        st, body = _req("POST", "/api/auth/service-token",
                        body={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        token = body.get("access_token") or body.get("token") or ""
        _check("service-token", bool(token), f"status={st}")
    else:
        _check("service-token", False, "set GATEWAY_CLIENT_ID/SECRET", required=False)

    # 3. option chain
    st, body = _req("POST", "/api/option-chain", token=token,
                    body={"symbol": args.symbol, "exchange": args.exchange, "count": 10})
    spot = body.get("spot")
    chain = body.get("chain") or []
    _check("option-chain", st == 200 and bool(spot) and len(chain) > 0,
           f"spot={spot} legs={len(chain)} status={st}")

    # 4. candles (single) + batch  [NEW]
    idx_token = str(body.get("underlying_token") or "")
    idx_exch = body.get("underlying_exchange") or args.exchange
    if idx_token:
        st, cb = _req("GET", f"/api/candles?exchange={idx_exch}&token={idx_token}&interval=5",
                      token=token)
        _check("candles(single)", st == 200 and len(cb.get("candles", [])) > 0,
               f"n={len(cb.get('candles', []))} status={st}")
        st, bb = _req("POST", "/api/candles/batch", token=token, body={
            "items": [
                {"exchange": idx_exch, "token": idx_token, "interval": 5},
                {"exchange": idx_exch, "token": idx_token, "interval": 15},
            ], "concurrency": 4})
        res = bb.get("results", [])
        _check("candles/batch", st == 200 and len(res) == 2
               and all(len(r.get("candles", [])) > 0 for r in res),
               f"items={len(res)} status={st}")
    else:
        _check("candles", False, "no underlying_token from chain", required=False)

    # 5. OI sweep
    st, sweep = _req("GET", f"/api/_probe/oisweep?symbols={args.symbol}&strikes=5",
                     token=token, timeout=40)
    rows = sweep if isinstance(sweep, list) else sweep.get("results", sweep.get("data", []))
    _check("oisweep", st == 200 and bool(rows), f"status={st}", required=False)

    # 6. WS touchline + depth  [depth is NEW]
    _ws_check(idx_exch, idx_token, args, token)

    print()
    required_failed = [n for n, ok, _ in _results if not ok]
    if required_failed:
        print(f"RESULT: FAIL — {len(required_failed)} required check(s) failed: {required_failed}")
        return 1
    print("RESULT: PASS — gateway is ready for the intraday engine")
    return 0


def _ws_check(exch: str, token_id: str, args, jwt: str) -> None:
    """Subscribe touchline + depth for the index and confirm frames arrive."""
    try:
        from websocket import create_connection  # websocket-client
    except Exception:
        _check("ws(touchline/depth)", False, "websocket-client not installed", required=False)
        return
    if not (jwt and token_id):
        _check("ws(touchline/depth)", False, "need token + underlying", required=False)
        return
    try:
        ws = create_connection(f"{WS_BASE}?token={jwt}", timeout=args.ws_seconds + 5)
        sym = f"{exch}|{token_id}"
        ws.send(json.dumps({"action": "subscribe", "symbols": [sym], "feed": "touchline"}))
        ws.send(json.dumps({"action": "subscribe", "symbols": [sym], "feed": "depth"}))
        deadline = time.time() + args.ws_seconds
        saw_touch = saw_depth = False
        ws.settimeout(1.0)
        while time.time() < deadline and not (saw_touch and saw_depth):
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            if msg.get("type") == "depth":
                saw_depth = True
            elif "lp" in msg and msg.get("type") != "order_update":
                saw_touch = True
        ws.close()
        _check("ws(touchline)", saw_touch, "no touchline frame", required=False)
        _check("ws(depth+OI)", saw_depth, "no depth frame — check depth-forwarding", required=False)
    except Exception as e:
        _check("ws(touchline/depth)", False, f"{type(e).__name__}: {e}", required=False)


if __name__ == "__main__":
    sys.exit(main())

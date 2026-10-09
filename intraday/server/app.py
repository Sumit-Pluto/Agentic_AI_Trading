"""FastAPI backend for the intraday cockpit (PDF §4 reporting + live push).

  WS  /ws              → the live engine snapshot every tick (state, positions,
                          signals, agent rows, option chains, equity point)
  GET /api/state        → current engine state
      /api/positions     /api/orders  /api/trades  /api/signals   (from journal)
      /api/agents        → last scan's per-agent rows
      /api/chain?symbol   → latest built chain (strikes, greeks, OI)
      /api/equity         → intraday equity curve
      /api/reporting      → daily/summary metrics (win rate, P&L, drawdown, latency)
      /api/config (GET/POST)   /api/pause (POST)   /api/mode (GET/POST)
  /                     → the built React cockpit (frontend/dist)

Run:  uvicorn intraday.server.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config as config_mod
from .runner import EngineRunner

_ROOT = Path(__file__).resolve().parents[2]
_DIST = _ROOT / "frontend" / "dist"

# load intraday/.env (GATEWAY_*, OPTIONSMITH_CHAIN_DB, INTRADAY_*) if present, so
# manual runs work without sourcing; systemd also sets these via EnvironmentFile.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "intraday" / ".env")
except Exception:
    pass

runner: EngineRunner | None = None


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    global runner
    cfg = config_mod.load()
    runner = EngineRunner(cfg)
    runner.start(asyncio.get_running_loop())
    yield
    runner.stop()


app = FastAPI(title="Intraday Agentic Cockpit", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


def _store():
    return runner.store


def _rows(sql: str, args=()):
    return [dict(r) for r in _store().q(sql, args)]


def _guard_mutation(request: Request):
    """Bearer-token guard for state-changing endpoints. Active only when
    INTRADAY_API_TOKEN is set (VPS deployments); local runs stay open so the
    cockpit works with zero setup. Clients send
    `Authorization: Bearer <token>`."""
    want = (os.environ.get("INTRADAY_API_TOKEN") or "").strip()
    if not want:
        return
    got = (request.headers.get("authorization") or "").strip()
    if got != f"Bearer {want}":
        raise HTTPException(status_code=401, detail="bad or missing API token")


# ---------------- live stream ----------------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q = runner.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", **runner.snapshot()})
        while True:
            snap = await q.get()
            await websocket.send_json({"type": "snapshot", **snap})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runner.unsubscribe(q)


# ---------------- REST ----------------
@app.get("/api/state")
def state():
    return runner.snapshot().get("state", {})


@app.get("/api/positions")
def positions():
    return {"positions": runner.snapshot().get("positions", [])}


@app.get("/api/orders")
def orders(limit: int = 100):
    return {"orders": _rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))}


@app.get("/api/trades")
def trades(limit: int = 200):
    return {"trades": _rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))}


@app.get("/api/signals")
def signals(limit: int = 100):
    return {"signals": _rows("SELECT id,ts,date,symbol,direction,score_buy,score_sell,"
                             "regime,instrument,acted FROM signals ORDER BY id DESC LIMIT ?",
                             (limit,))}


@app.get("/api/agents")
def agents():
    return {"rows": runner.snapshot().get("agent_rows", []),
            "regime": runner.snapshot().get("state", {}).get("regime", {})}


@app.get("/api/chain")
def chain(symbol: str = ""):
    chains = runner.snapshot().get("chains", {})
    if symbol:
        return chains.get(symbol) or {}
    return chains


@app.get("/api/activity")
def activity(limit: int = 200):
    snap = runner.snapshot()
    acts = snap.get("activity", [])
    return {"events": acts[-limit:] if limit > 0 else acts,
            "scan": snap.get("scan", [])}


@app.get("/api/equity")
def equity(limit: int = 500):
    rows = _rows("SELECT ts,date,equity,realized_pnl,unrealized_pnl,n_positions "
                 "FROM equity ORDER BY ts DESC LIMIT ?", (limit,))
    rows.reverse()
    return {"curve": rows}


@app.get("/api/reporting")
def reporting():
    """Summary metrics from the journal (PDF §4)."""
    t = _rows("SELECT pnl,r,side,strategy,underlying,exit_reason,latency,date FROM ("
              "SELECT tr.pnl,tr.r,tr.side,tr.strategy,tr.underlying,tr.exit_reason,"
              "NULL latency,tr.date FROM trades tr) ORDER BY date")
    n = len(t)
    wins = [x for x in t if (x["pnl"] or 0) > 0]
    total = sum((x["pnl"] or 0) for x in t)
    lat = _rows("SELECT latency_ms FROM orders WHERE latency_ms IS NOT NULL")
    lat_vals = [x["latency_ms"] for x in lat if x["latency_ms"] is not None]

    def _group(field):
        g: dict = {}
        for x in t:
            k = x.get(field) or "?"
            gg = g.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
            gg["n"] += 1; gg["pnl"] += (x["pnl"] or 0)
            gg["wins"] += 1 if (x["pnl"] or 0) > 0 else 0
        return g

    # max drawdown from the equity curve
    eq = [r["equity"] for r in _rows("SELECT equity FROM equity ORDER BY ts")]
    peak = dd = 0.0
    for e in eq:
        peak = max(peak, e); dd = max(dd, peak - e)

    return {
        "trades": n, "wins": len(wins),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "total_pnl": round(total, 0),
        "avg_win": round(sum(x["pnl"] for x in wins) / len(wins), 0) if wins else 0.0,
        "avg_loss": round(sum(x["pnl"] for x in t if (x["pnl"] or 0) <= 0)
                          / max(1, n - len(wins)), 0),
        "max_drawdown": round(dd, 0),
        "avg_latency_ms": round(sum(lat_vals) / len(lat_vals), 1) if lat_vals else None,
        "execution": _store().execution_quality(),
        "by_strategy": _group("strategy"), "by_symbol": _group("underlying"),
        "by_exit": _group("exit_reason"),
    }


@app.get("/api/config")
def get_config():
    return runner.cfg


@app.post("/api/config")
async def set_config(body: dict, request: Request):
    _guard_mutation(request)
    # mutate in place so the loop/governor/rules (which hold the same dict) see it
    for k, v in (body or {}).items():
        if k in config_mod.DEFAULTS:
            runner.cfg[k] = v
    config_mod.save(runner.cfg)
    return {"ok": True, "config": runner.cfg}


@app.post("/api/pause")
async def pause(body: dict, request: Request):
    """Start/Stop the scanner. Stop (paused=True) halts NEW entries; open
    positions keep running under the exit engine unless square_off is set."""
    _guard_mutation(request)
    runner.cfg["paused"] = bool(body.get("paused", True))
    config_mod.save(runner.cfg)          # a restart must not silently resume
    if runner.cfg["paused"] and body.get("square_off"):
        runner.loop.request_flatten("stop: square-off")
    return {"paused": runner.cfg["paused"]}


@app.post("/api/kill")
async def kill_switch(request: Request):
    """Live-only kill: halt trading for the day AND flatten all open positions."""
    _guard_mutation(request)
    runner.cfg["paused"] = True
    config_mod.save(runner.cfg)
    runner.loop.request_flatten("kill switch", halt=True)
    return {"halted": True, "paused": True}


@app.post("/api/resume")
async def resume(body: dict, request: Request):
    """Clear a daily-loss / kill-switch halt. Requires {"confirm":"RESUME"} —
    the halt persists across restarts by design, so only a typed human
    override lifts it (also clears the pause the kill switch sets)."""
    _guard_mutation(request)
    if str(body.get("confirm", "")) != "RESUME":
        return JSONResponse(status_code=400, content={"error": 'type "RESUME" to confirm'})
    runner.cfg["paused"] = False
    config_mod.save(runner.cfg)
    runner.loop.resume()
    return {"halted": False, "paused": False}


@app.get("/api/budget")
def budget():
    s = runner.snapshot()
    return {"budget": s.get("budget", {}), "funds": s.get("funds")}


def _num_or_none(x):
    try:
        f = float(x)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _int_or_zero(x) -> int:
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def _norm_broker_order(o: dict) -> dict:
    side = "BUY" if str(o.get("trantype", "")).upper().startswith("B") else "SELL"
    return {"id": str(o.get("norenordno") or o.get("orderid") or ""),
            "tsym": str(o.get("tsym") or ""), "exch": str(o.get("exch") or ""),
            "prd": str(o.get("prd") or ""), "side": side,
            "qty": _int_or_zero(o.get("qty")),
            "filled": _int_or_zero(o.get("fillshares")),
            "status": str(o.get("status") or "").upper(),
            "price": _num_or_none(o.get("prc") or o.get("price")),
            "avg": _num_or_none(o.get("avgprc")),
            "time": str(o.get("pytime") or o.get("exch_tm") or ""),
            "rejreason": str(o.get("rejreason") or ""),
            "remarks": str(o.get("remarks") or "")}


def _norm_broker_position(p: dict) -> dict:
    net = _int_or_zero(p.get("netqty"))
    side = "BUY" if net >= 0 else "SELL"
    avg = p.get("buyavgprc") if side == "BUY" else p.get("sellavgprc")
    return {"tsym": str(p.get("tsym") or ""), "exch": str(p.get("exch") or ""),
            "prd": str(p.get("prd") or p.get("s_prdt_ali") or ""),
            "side": side, "qty": abs(net),
            "avg": _num_or_none(avg), "ltp": _num_or_none(p.get("lp")),
            "pnl": _num_or_none(p.get("total_pnl")),
            "mtm": _num_or_none(p.get("urmtom")),
            "realized": _num_or_none(p.get("rpnl")),
            "lot_size": _int_or_zero(p.get("lotsize")) or 0}


@app.get("/api/orderbook")
def orderbook():
    """Live broker order book (Shoonya truth, latest first). connected=False
    when this server has no Gateway client (sim/offline)."""
    raw = runner.order_book()
    if raw is None:
        return {"connected": False, "orders": []}
    rows = raw if isinstance(raw, list) else []
    orders = [_norm_broker_order(o) for o in rows if isinstance(o, dict)]
    orders.reverse()
    return {"connected": True, "orders": orders}


@app.get("/api/broker-positions")
def broker_positions():
    """Live broker net positions (all products/strategies on the account).
    connected=False when this server has no Gateway client."""
    raw = runner.broker_positions()
    if raw is None:
        return {"connected": False, "positions": []}
    rows = raw if isinstance(raw, list) else []
    out = [_norm_broker_position(p) for p in rows if isinstance(p, dict)]
    return {"connected": True, "positions": [p for p in out if p["qty"] != 0]}


@app.get("/api/funds")
def funds():
    return {"funds": runner._funds()}


@app.get("/api/mode")
def get_mode():
    return {"mode": runner.cfg.get("mode", "paper"), "engine": runner.engine_mode,
            "broker": runner.loop.broker.name}


@app.post("/api/mode")
async def set_mode(body: dict, request: Request):
    """Toggle PAPER<->LIVE. LIVE requires {"mode":"live","confirm":"LIVE"} and a
    connected Gateway; swaps the broker at runtime."""
    _guard_mutation(request)
    res = runner.set_mode(str(body.get("mode", "paper")), str(body.get("confirm", "")))
    if "error" in res:
        return JSONResponse(status_code=400, content=res)
    return res


# ---------------- static (the built cockpit) — mounted last ----------------
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="app")

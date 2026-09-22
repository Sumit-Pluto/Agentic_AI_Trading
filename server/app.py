"""FastAPI app for the NSE F&O quant scanner.

create_app(scanner, config, root_agent) wires the running Scanner, the
QuantConfig store and the root QuantAgent into a small JSON API, and serves
the built React/Tailwind frontend (frontend/, built to frontend/dist).

All handlers are plain sync ``def`` functions: FastAPI executes them in its
threadpool, so blocking hub/broker calls never stall the event loop.
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# React/Vite frontend (frontend/) — built to frontend/dist and served below,
# after every /api/* route, so the SPA mount's catch-all never shadows them.
FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")

STRATEGIST_CACHE_TTL = 90.0     # seconds; per-symbol advisor result cache


def create_app(scanner, config, root_agent) -> FastAPI:
    app = FastAPI(title="Quant Scanner", docs_url=None, redoc_url=None)

    # ── read state ──────────────────────────────────────────────────────
    @app.get("/api/state")
    def state():
        try:
            from core import trade_mode
            _mode = trade_mode.mode()
        except Exception:
            _mode = "paper"
        return {
            "mode": _mode,
            "universe": len(scanner.universe),
            "last_sweep": scanner.last_sweep,
            "sweep_seconds": scanner.sweep_seconds,
            "signal_threshold": config.signal_threshold,
            "paper_count": len(scanner.paper.tail(10000)),
            "signals": scanner.signals[:100],
        }

    @app.get("/api/tree")
    def tree():
        return {"describe": root_agent.describe(), "config": config.data}

    @app.get("/api/paper")
    def paper():
        return scanner.paper.tail(200)

    @app.get("/api/swing")
    def swing_signals():
        """Current Rbknox + Order-Block swing alerts across Cash/F&O/MCX
        (daily + intraday). Populated by the background SwingScanner."""
        sw = getattr(scanner, "swing", None)
        if sw is None:
            return {"last_scan": None, "count": 0, "signals": []}
        return sw.snapshot()

    @app.get("/api/swing/params")
    def swing_params_get():
        """Current Rbknox (Knoxville Divergence) params + editable-field spec."""
        sw = getattr(scanner, "swing", None)
        if sw is None:
            return JSONResponse(status_code=503,
                                content={"error": "swing scanner not running"})
        return sw.get_params()

    @app.post("/api/swing/params")
    def swing_params_set(payload: dict = Body(...)):
        """Edit the Rbknox params, then re-scan in the background so the new
        settings take effect without blocking the request."""
        sw = getattr(scanner, "swing", None)
        if sw is None:
            return JSONResponse(status_code=503,
                                content={"error": "swing scanner not running"})
        params = sw.set_params(payload or {})
        threading.Thread(target=sw.sweep, name="swing-reparam",
                         daemon=True).start()
        return {"ok": True, "params": params}

    @app.get("/api/oi-scan")
    def oi_scan():
        """Current OI-buildup + wall-confirmed hits (the OI strategy)."""
        sc = getattr(scanner, "oi", None)
        if sc is None:
            return {"last_scan": None, "count": 0, "signals": []}
        return sc.snapshot()

    @app.get("/api/agent-signals")
    def agent_signals():
        """Agent-PRIMARY signals: the tree fires without any indicator
        trigger (score + margin + rising-edge). Paper-tagged strategy=agents."""
        sc = getattr(scanner, "agents", None)
        if sc is None:
            return {"last_scan": None, "count": 0, "signals": []}
        return sc.snapshot()

    # ── watchlists (per-strategy P&L-since-add) ──────────────────────────
    @app.get("/api/watchlist")
    def watchlist_list(strategy: str = Query("")):
        wl = getattr(scanner, "watchlist", None)
        if wl is None:
            return {"strategies": []}
        s = strategy.strip() or None
        return wl.strategy_pnl(s) if s else wl.overview()

    @app.post("/api/watchlist")
    def watchlist_add(payload: dict = Body(...)):
        wl = getattr(scanner, "watchlist", None)
        if wl is None:
            return JSONResponse(status_code=503, content={"error": "watchlist off"})
        sym = str(payload.get("symbol", "")).strip().upper()
        strat = str(payload.get("strategy", "")).strip()
        if not sym or not strat:
            return JSONResponse(status_code=400,
                                content={"error": "symbol and strategy required"})
        return wl.add(symbol=sym, strategy=strat,
                      direction=str(payload.get("direction", "BUY")),
                      segment=payload.get("segment"),
                      note=str(payload.get("note", "")))

    @app.post("/api/watchlist/remove")
    def watchlist_remove(payload: dict = Body(...)):
        wl = getattr(scanner, "watchlist", None)
        if wl is None:
            return JSONResponse(status_code=503, content={"error": "watchlist off"})
        return {"ok": wl.remove(str(payload.get("id", "")))}

    # ── saved option-strategy structures (live mark-to-market P&L) ────────
    @app.get("/api/strategist/saved")
    def saved_strategies_list():
        ss = getattr(scanner, "saved_strategies", None)
        if ss is None:
            return {"count": 0, "priced": 0, "items": []}
        return ss.overview()

    @app.post("/api/strategist/save")
    def saved_strategies_add(payload: dict = Body(...)):
        ss = getattr(scanner, "saved_strategies", None)
        if ss is None:
            return JSONResponse(status_code=503,
                                content={"error": "saved strategies off"})
        sym = str(payload.get("symbol", "")).strip().upper()
        legs = payload.get("legs") or []
        if not sym or not legs:
            return JSONResponse(status_code=400,
                                content={"error": "symbol and legs required"})
        item = ss.add(symbol=sym, label=str(payload.get("label", "structure")),
                      legs=legs,
                      entry_net_per_share=payload.get("net_premium_per_share"),
                      lot=payload.get("lot"),
                      metrics=payload.get("metrics") or {})
        return {"ok": True, "item": item}

    @app.post("/api/strategist/save/remove")
    def saved_strategies_remove(payload: dict = Body(...)):
        ss = getattr(scanner, "saved_strategies", None)
        if ss is None:
            return JSONResponse(status_code=503,
                                content={"error": "saved strategies off"})
        return {"ok": ss.remove(str(payload.get("id", "")))}

    # ── LLM assistant (Qwen3.5-9B) ───────────────────────────────────────
    @app.post("/api/assistant/chat")
    def assistant_chat(payload: dict = Body(...)):
        asst = getattr(scanner, "assistant", None)
        if asst is None:
            return JSONResponse(status_code=503, content={"error": "assistant not wired"})
        msg = str(payload.get("message", "")).strip()
        if not msg:
            return JSONResponse(status_code=400, content={"error": "message required"})
        if not asst.client.available():
            return {"reply": "The LLM endpoint isn't configured yet — set "
                    "LLM_BASE_URL and LLM_API_KEY in .env (RunPod Qwen3.5-9B).",
                    "mode": "unconfigured", "tools_used": [], "validator": "n/a",
                    "disclaimer": ""}
        try:
            return asst.chat(str(payload.get("session_id") or "default"), msg)
        except Exception as exc:
            return JSONResponse(status_code=502,
                                content={"error": f"assistant error: {exc}"})

    @app.get("/api/assistant/health")
    def assistant_health():
        asst = getattr(scanner, "assistant", None)
        return {"configured": bool(asst and asst.client.available()),
                "model": getattr(getattr(asst, "client", None), "model", None)}

    @app.get("/api/assistant/audit")
    def assistant_audit(n: int = Query(20)):
        asst = getattr(scanner, "assistant", None)
        if asst is None or getattr(asst, "audit", None) is None:
            return {"records": []}
        return {"records": asst.audit.tail(n)}

    # ── config mutations ────────────────────────────────────────────────
    @app.post("/api/agents/toggle")
    def toggle(payload: dict = Body(...)):
        config.set_enabled(str(payload["key"]), bool(payload["enabled"]))
        return {"ok": True}

    @app.post("/api/agents/weight")
    def weight(payload: dict = Body(...)):
        config.set_weight(str(payload["key"]),
                          str(payload["direction"]).upper(),
                          float(payload["value"]),
                          payload.get("symbol") or None)
        return {"ok": True}

    @app.post("/api/agents/threshold")
    def agent_threshold(payload: dict = Body(...)):
        config.set_threshold(str(payload["key"]), float(payload["value"]))
        return {"ok": True}

    @app.post("/api/threshold")
    def signal_threshold(payload: dict = Body(...)):
        config.data["signal_threshold"] = float(payload["value"])
        config.save()
        return {"ok": True, "signal_threshold": config.signal_threshold}

    # ── on-demand evaluation ────────────────────────────────────────────
    @app.get("/api/evaluate")
    def evaluate(symbol: str = Query(...), direction: str = Query("BUY"),
                 segment: str = Query("")):
        direction = direction.upper()
        if direction not in ("BUY", "SELL"):
            return JSONResponse(status_code=400,
                                content={"error": "direction must be BUY or SELL"})
        seg = segment.upper().strip() or None
        result = scanner.evaluate(symbol.upper().strip(), direction, segment=seg)
        if result is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"no data for {symbol.upper().strip()}"})
        return result

    # ── strategy advisor ────────────────────────────────────────────────
    app.state.strategist_advisor = None      # built lazily on first call
    app.state.strategist_cache = {}          # SYMBOL -> (epoch, result dict)
    app.state.strategist_lock = threading.Lock()

    @app.get("/api/strategist")
    def strategist(symbol: str = Query(...),
                   profile: str = Query("balanced")):
        """OI-footprint strategy advisor. Sync def -> FastAPI threadpool,
        so the (slow: chain + bhavcopy history) analysis never blocks the
        event loop. Results cached per (symbol, profile)."""
        sym = symbol.upper().strip()
        prof = profile.lower().strip()
        if prof not in ("conservative", "balanced", "aggressive"):
            prof = "balanced"
        if not sym:
            return JSONResponse(status_code=400,
                                content={"error": "symbol required"})
        if getattr(scanner, "hub", None) is None:
            return JSONResponse(status_code=503,
                                content={"error": "no session"})

        now = time.time()
        cache_key = f"{sym}|{prof}"
        with app.state.strategist_lock:
            hit = app.state.strategist_cache.get(cache_key)
            if hit and now - hit[0] < STRATEGIST_CACHE_TTL:
                return hit[1]
            advisor = app.state.strategist_advisor

        if advisor is None:
            try:
                from strategist.service import StrategyAdvisor
            except Exception as exc:          # module missing / import error
                return JSONResponse(
                    status_code=503,
                    content={"error": f"strategist unavailable: {exc}"})
            with app.state.strategist_lock:
                if app.state.strategist_advisor is None:
                    app.state.strategist_advisor = StrategyAdvisor(scanner.hub)
                advisor = app.state.strategist_advisor

        entry = None
        for name in ("analyze", "analyse", "advise", "recommend", "report",
                     "run"):
            fn = getattr(advisor, name, None)
            if callable(fn):
                entry = fn
                break
        if entry is None and callable(advisor):
            entry = advisor
        if entry is None:
            return JSONResponse(
                status_code=500,
                content={"error": "StrategyAdvisor exposes no known "
                                  "entrypoint (analyze/analyse/advise/...)"})
        try:
            try:
                result = entry(sym, profile=prof)
            except TypeError:                 # entrypoint without profile
                result = entry(sym)
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"strategist failed: {exc}"})
        if result is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"no usable option chain for {sym}"})
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        with app.state.strategist_lock:
            app.state.strategist_cache[cache_key] = (time.time(), result)
        return result

    # ── market news ─────────────────────────────────────────────────────
    app.state.newshub = None                 # built lazily on first call
    app.state.newshub_lock = threading.Lock()

    @app.get("/api/news")
    def news():
        """Market-news hub state. Works WITHOUT a broker login — RSS +
        Twelve Data need no session. Lazy singleton hub with its own daemon
        refresh thread (same pattern as the strategist advisor)."""
        hub = app.state.newshub
        if hub is None:
            try:
                from news.service import NewsHub
            except Exception as exc:          # module missing / import error
                return JSONResponse(
                    status_code=503,
                    content={"error": f"news module unavailable: {exc}"})
            with app.state.newshub_lock:
                if app.state.newshub is None:
                    app.state.newshub = NewsHub(start=True)
                hub = app.state.newshub
        try:
            return hub.get_state()
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"news state failed: {exc}"})

    # ── zero-loss strategy scan ─────────────────────────────────────────
    app.state.zeroloss = None                # built lazily on first scan
    app.state.zeroloss_lock = threading.Lock()

    def _zeroloss_get():
        """Lazy ZeroLossScan singleton. Returns (scan, error_response)."""
        if getattr(scanner, "hub", None) is None:
            return None, JSONResponse(status_code=503,
                                      content={"error": "no session"})
        scan = app.state.zeroloss
        if scan is None:
            try:
                from strategist.zeroloss import ZeroLossScan
            except Exception as exc:      # module missing / import error
                return None, JSONResponse(
                    status_code=503,
                    content={"error": f"zeroloss unavailable: {exc}"})
            with app.state.zeroloss_lock:
                if app.state.zeroloss is None:
                    try:
                        app.state.zeroloss = ZeroLossScan(scanner.hub)
                    except Exception as exc:
                        return None, JSONResponse(
                            status_code=503,
                            content={"error": f"zeroloss init failed: {exc}"})
                scan = app.state.zeroloss
        return scan, None

    def _zeroloss_probe(scan, names):
        """First usable attribute among `names`; called if callable."""
        for name in names:
            v = getattr(scan, name, None)
            if v is None:
                continue
            if callable(v):
                try:
                    v = v()
                except Exception:
                    continue
            return v
        return None

    def _zeroloss_state(scan) -> dict:
        status = _zeroloss_probe(scan, ("status", "get_status", "state"))
        if not isinstance(status, dict):
            status = {} if status is None else {"state": str(status)}
        results = _zeroloss_probe(scan, ("results", "get_results", "hits"))
        if not isinstance(results, (list, tuple)):
            results = []
        out = []
        for r in results:
            try:
                out.append(r.to_dict() if hasattr(r, "to_dict") else r)
            except Exception:
                continue
        return {"status": status, "results": out}

    @app.post("/api/zeroloss")
    def zeroloss_start(payload: dict = Body(default={})):
        """Kick off (or join) a background zero-loss scan of the F&O
        universe. Floors are computed at EXECUTABLE bid/ask prices minus
        friction inside strategist.zeroloss — this endpoint only
        orchestrates and degrades gracefully when the module or the
        broker session is missing."""
        scan, err = _zeroloss_get()
        if err is not None:
            return err
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        entry = None
        for name in ("start", "run"):
            fn = getattr(scan, name, None)
            if callable(fn):
                entry = fn
                break
        if entry is None:
            return JSONResponse(
                status_code=500,
                content={"error": "ZeroLossScan exposes no start/run "
                                  "entrypoint"})
        try:
            try:
                started = entry(limit)
            except TypeError:             # entrypoint without a limit arg
                started = entry()
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"error": f"zeroloss start failed: {exc}"})
        if started is None:               # start() that returns nothing
            started = True
        return {"started": bool(started),
                "status": _zeroloss_state(scan)["status"]}

    @app.get("/api/zeroloss")
    def zeroloss_status():
        """Scan progress + hits. Idle payload before the first scan so
        the UI can poll freely without triggering imports or broker
        access."""
        if app.state.zeroloss is None:
            return {"status": {"running": False}, "results": []}
        return _zeroloss_state(app.state.zeroloss)

    # ── symbol list (autocomplete) ──────────────────────────────────────
    @app.get("/api/symbols")
    def symbols():
        return scanner.universe

    # ── day simulation ──────────────────────────────────────────────────
    @app.post("/api/simulate")
    def simulate(payload: dict = Body(default={})):
        """Replay the last session through the live pipeline (background)."""
        limit = int(payload.get("limit") or 0)
        started = scanner.start_simulation(limit=limit)
        return {"started": started, "status": scanner.sim_status}

    @app.get("/api/simulate")
    def simulate_status():
        return scanner.sim_status

    # ── paper positions (exit engine) ───────────────────────────────────
    _POS_EMPTY = {"open": 0, "partial": 0, "closed_today": 0,
                  "realized_pnl_today_per_share_weighted": 0.0,
                  "positions": []}

    @app.get("/api/positions")
    def positions():
        """ExitEngine summary + cheap last-price / unrealized augmentation.
        Degrades to an empty summary when the engine is missing."""
        exits = getattr(scanner, "exits", None)
        if exits is None:
            return dict(_POS_EMPTY)
        try:
            summ = exits.summary()
        except Exception as exc:
            out = dict(_POS_EMPTY)
            out["error"] = f"summary failed: {exc}"
            return out
        if not isinstance(summ, dict):
            return dict(_POS_EMPTY)
        hub = getattr(scanner, "hub", None)
        price_cache: dict = {}                # SYMBOL -> float | None

        def _last_price(sym):
            if sym in price_cache:
                return price_cache[sym]
            px = None
            if hub is not None:
                try:                          # candles_5m serves from cache
                    df = hub.candles_5m(sym)
                    if df is not None and not getattr(df, "empty", True):
                        px = float(df["close"].iloc[-1])
                except Exception:
                    px = None
            price_cache[sym] = px
            return px

        for p in summ.get("positions") or []:
            if not isinstance(p, dict):
                continue
            try:
                if p.get("state") == "CLOSED":
                    continue
                px = _last_price(str(p.get("symbol") or ""))
                if px is None:
                    continue
                p["last_price"] = round(px, 4)
                entry = float(p.get("entry_price") or 0)
                qty = float(p.get("qty_units") or 0)
                sign = 1.0 if p.get("direction") == "BUY" else -1.0
                p["unrealized_per_share"] = round(
                    (px - entry) * sign * qty, 4)
            except Exception:
                continue
        return summ

    # ── P&L dashboard ───────────────────────────────────────────────────
    @app.get("/api/pnl")
    def pnl_dashboard():
        """Account tiles (live paper only) + unified live/sim trade rows."""
        try:
            from engine.pnl import build_dashboard
        except Exception as exc:              # module not landed yet
            return JSONResponse(
                status_code=503,
                content={"error": f"pnl module unavailable: {exc}"})
        try:
            return build_dashboard(scanner)
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"pnl failed: {exc}"})

    @app.get("/api/pnl/trade")
    def pnl_trade(id: str = Query(...)):
        """Lifecycle + entry audit + narrative for one trade (live or sim)."""
        try:
            from engine.pnl import trade_detail
        except Exception as exc:              # module not landed yet
            return JSONResponse(
                status_code=503,
                content={"error": f"pnl module unavailable: {exc}"})
        try:
            detail = trade_detail(scanner, id)
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"trade detail failed: "
                                                  f"{exc}"})
        if detail is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown trade id: {id}"})
        return detail

    # ── activity journal ────────────────────────────────────────────────
    @app.get("/api/activity")
    def activity_feed(n: int = Query(100)):
        """Recent activity events, newest first. Empty list on any failure
        so the UI ticker can poll unconditionally."""
        try:
            n = max(1, min(int(n), 300))
        except (TypeError, ValueError):
            n = 100
        try:
            from core.activity import activity as _activity
            items = _activity.recent(n)
            return items if isinstance(items, list) else []
        except Exception:
            return []

    # ── trade mode: PAPER (default) vs LIVE real-money ──────────────────
    def _mode_payload() -> dict:
        try:
            from core import trade_mode
            live = bool(trade_mode.is_live())
        except Exception:
            live = False
        out = {"mode": "live" if live else "paper", "live": live}
        try:
            exits = getattr(scanner, "exits", None)
            ex = getattr(exits, "executor", None) if exits else None
            if ex is not None:
                out["executor"] = {
                    "ready": bool(ex.active()) if live else False,
                    "day": ex.day_stats(),
                    "limits": {"risk_rupees": ex.risk_rupees,
                               "max_qty": ex.max_qty,
                               "max_capital": ex.max_capital,
                               "max_positions": ex.max_positions,
                               "max_daily_loss": ex.max_daily_loss},
                    "open_live": ex.open_live_count}
            else:
                out["executor"] = None
            hub = getattr(scanner, "hub", None)
            if hub is not None and hasattr(hub, "feed_status"):
                out["feed"] = hub.feed_status()
        except Exception:
            pass
        return out

    @app.get("/api/mode")
    def mode_state():
        return _mode_payload()

    @app.post("/api/mode")
    def mode_set(payload: dict = Body(...)):
        """Switch paper <-> live. Going LIVE requires confirm == "LIVE"
        (the UI makes the user type it). Going back to paper is always
        allowed but reports open live positions so the UI can warn —
        their exits keep routing only while mode is LIVE."""
        want = str(payload.get("mode", "")).lower()
        if want not in ("paper", "live"):
            return JSONResponse(status_code=400,
                                content={"error": "mode must be paper|live"})
        try:
            from core import trade_mode
        except Exception as exc:
            return JSONResponse(status_code=503,
                                content={"error": f"trade_mode unavailable: "
                                                  f"{exc}"})
        if want == "live":
            if str(payload.get("confirm", "")) != "LIVE":
                return JSONResponse(
                    status_code=400,
                    content={"error": "confirm must be the string \"LIVE\""})
            exits = getattr(scanner, "exits", None)
            ex = getattr(exits, "executor", None) if exits else None
            if ex is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "no LiveExecutor wired — restart "
                                      "run_app.py with a broker session"})
            if isinstance(payload.get("limits"), dict):
                ex.set_limits(payload["limits"])   # user-chosen risk caps
            trade_mode.set_live(True, source="ui")
            if not ex.active():
                trade_mode.set_live(False, source="ui-rollback")
                return JSONResponse(
                    status_code=503,
                    content={"error": "broker session not ready — daily "
                                      "login missing/expired; staying PAPER"})
        else:
            trade_mode.set_live(False, source="ui")
        out = _mode_payload()
        try:                       # open live positions -> UI warning banner
            exits = getattr(scanner, "exits", None)
            if exits is not None:
                out["live_open"] = int(exits.summary().get("live_open") or 0)
        except Exception:
            pass
        return out

    @app.post("/api/live/limits")
    def live_limits(payload: dict = Body(...)):
        """Update the live risk caps (works in either mode; persisted)."""
        exits = getattr(scanner, "exits", None)
        ex = getattr(exits, "executor", None) if exits else None
        if ex is None:
            return JSONResponse(status_code=503,
                                content={"error": "no LiveExecutor wired"})
        return {"ok": True, "limits": ex.set_limits(payload or {})}

    # ── broker readiness / order-path checks ────────────────────────────
    def _executor():
        exits = getattr(scanner, "exits", None)
        return getattr(exits, "executor", None) if exits else None

    @app.get("/api/broker/check")
    def broker_check():
        """Read-only: session/auth, order book, margin, positions, scrip
        master, websocket. Never places an order."""
        ex = _executor()
        if ex is None or ex.session is None:
            return JSONResponse(status_code=503,
                                content={"error": "no broker session wired"})
        try:
            from core.broker_check import run_checks
            checks = run_checks(ex.session, getattr(scanner, "hub", None))
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"check failed: {exc}"})
        ok = all(c["ok"] for c in checks if c["name"] != "feed")
        return {"ok": ok, "checks": checks}

    @app.post("/api/broker/ordertest")
    def broker_ordertest(payload: dict = Body(default={})):
        """PLACES A REAL unfillable 1-share limit order and cancels it, to
        prove the buy/sell pipeline. Requires confirm == "TEST"."""
        if str(payload.get("confirm", "")) != "TEST":
            return JSONResponse(
                status_code=400,
                content={"error": "confirm must be the string \"TEST\""})
        ex = _executor()
        if ex is None or ex.session is None:
            return JSONResponse(status_code=503,
                                content={"error": "no broker session wired"})
        try:
            from core.broker_check import order_path_test
            return order_path_test(ex.session, getattr(scanner, "hub", None))
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"order test failed: {exc}"})

    # ── manual order across Cash / F&O / MCX (decision #4) ──────────────
    @app.post("/api/manual/order")
    def manual_order(payload: dict = Body(...)):
        """Place ONE real marketable intraday order across Cash / F&O / MCX.
        Requires confirm == direction. Kill-switch respected. Fires a REAL
        order regardless of paper/LIVE mode (it is an explicit manual action).

        segment CASH -> NSE equity (qty = shares); FNO -> NFO front-month
        future; MCX -> MCX front-month future (qty = LOTS for futures)."""
        symbol = str(payload.get("symbol", "")).strip().upper()
        direction = str(payload.get("direction", "")).strip().upper()
        if not symbol or direction not in ("BUY", "SELL"):
            return JSONResponse(status_code=400,
                                content={"error": "symbol + direction (BUY/SELL) required"})
        if str(payload.get("confirm", "")) != direction:
            return JSONResponse(status_code=400,
                                content={"error": f'confirm must equal "{direction}"'})
        try:
            qty = int(payload.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            return JSONResponse(status_code=400, content={"error": "qty must be > 0"})

        from core import killswitch
        if killswitch.is_paused():
            return JSONResponse(status_code=423,
                                content={"error": "kill switch is ON (paused)"})
        ex = _executor()
        hub = getattr(scanner, "hub", None)
        if ex is None or getattr(ex, "session", None) is None or hub is None:
            return JSONResponse(status_code=503,
                                content={"error": "no broker session — run the daily login"})

        seg = str(payload.get("segment", "") or hub.segment_of(symbol)).upper()
        side = "B" if direction == "BUY" else "S"
        try:
            if seg == "CASH":
                row = hub.cash_row(symbol)
                if not row:
                    return JSONResponse(status_code=404,
                                        content={"error": f"no NSE equity {symbol}"})
                exch = "NSE"
                tsym = row.get("TradingSymbol") or f"{symbol}-EQ"
                tick = float(row.get("TickSize") or 0.05)
                units = qty                                   # shares
            else:
                from core.broker_check import resolve_front_month
                exch = "MCX" if seg == "MCX" else "NFO"
                con = resolve_front_month(exch, symbol)
                if not con:
                    return JSONResponse(status_code=404,
                                        content={"error": f"no front-month {symbol} on {exch}"})
                tsym = con["tsym"]
                tick = con.get("ticksize") or 0.05
                units = qty * int(con.get("lotsize") or 1)    # qty = lots
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"contract resolve failed: {exc}"})

        try:
            ref = hub.price(symbol, seg)
        except Exception:
            ref = None
        try:
            fill = ex._place(symbol=symbol, side=side, qty=units, remarks="manual",
                             ref_price=ref, exchange=exch, tsym=tsym, ticksize=tick)
        except Exception as exc:
            return JSONResponse(status_code=502,
                                content={"error": f"order failed: {exc}", "tsym": tsym})
        out = {"ok": bool(fill), "symbol": symbol, "segment": seg,
               "exchange": exch, "tsym": tsym, "direction": direction,
               "qty": units, "status": (fill or {}).get("status") or "FAILED/REJECTED",
               "fill_price": (fill or {}).get("avg_price"),
               "reject_reason": (fill or {}).get("reject_reason")}
        try:
            scanner.paper.record({"type": "live_manual", "strategy": "manual", **out})
        except Exception:
            pass
        return out

    # ── kill switch (pause new entries) ─────────────────────────────────
    @app.get("/api/pause")
    def pause_state():
        try:
            from core import killswitch
            paused = bool(killswitch.is_paused())
        except Exception:                     # module not landed yet
            paused = bool(getattr(scanner, "paused", False))
        return {"paused": paused}

    @app.post("/api/pause")
    def pause_set(payload: dict = Body(...)):
        want = bool(payload.get("paused"))
        paused = want
        try:
            from core import killswitch
            paused = bool(killswitch.set_paused(want, source="ui"))
        except Exception:                     # degrade to in-process flag
            paused = want
        try:
            scanner.paused = paused           # immediate effect this sweep
        except Exception:
            pass
        return {"paused": paused}

    # ── swing-structure overlays for the chart ──────────────────────────
    @app.get("/api/overlays")
    def overlays(symbol: str = Query(...)):
        """Major swing pivots + trend on the SAME tail-400 slice as
        /api/candles, so marker times align with charted bars."""
        sym = symbol.upper().strip()
        hub = getattr(scanner, "hub", None)
        df = None
        if hub is not None:
            try:
                df = hub.candles_5m(sym)
            except Exception:
                df = None
        if df is None or getattr(df, "empty", True):
            return JSONResponse(status_code=404,
                                content={"error": f"no candles for {sym}"})
        try:
            from signals.market_structure import analyze
        except Exception as exc:              # module not landed yet
            return {"pivots": [], "trend": "RANGE",
                    "error": f"market_structure unavailable: {exc}"}
        tail = df.tail(400)
        try:
            res = analyze(tail)
        except Exception as exc:
            return {"pivots": [], "trend": "RANGE",
                    "error": f"analyze failed: {exc}"}
        if not isinstance(res, dict):
            return {"pivots": [], "trend": "RANGE"}
        pivots = []
        for p in res.get("pivots") or []:
            try:
                q = dict(p)
                # epoch seconds — the exact time key /api/candles emits
                q["time"] = int(tail.index[int(q["i"])].timestamp())
                pivots.append(q)
            except Exception:
                continue
        out = {"pivots": pivots, "trend": str(res.get("trend") or "RANGE")}

        # ── TradingView-style extras: supertrend, OI walls, position levels ─
        try:
            from signals.indicators import supertrend as st_calc
            st = st_calc(tail)
            out["supertrend"] = [
                {"time": int(ts.timestamp()),
                 "value": (None if line != line else round(float(line), 2)),
                 "bull": bool(bull)}
                for ts, line, bull in zip(tail.index, st["line"], st["bull"])
                if line == line]                     # NaN warm-up dropped
        except Exception:
            out["supertrend"] = []

        try:
            chain = hub.chain_snapshot(sym) if hub else None
            if chain and chain.strikes:
                def ladder(side):
                    rows = []
                    for r in chain.strikes:
                        leg = getattr(r, side, None)
                        if leg and (leg.oi or 0) > 0:
                            rows.append((float(leg.oi), float(r.strike)))
                    rows.sort(reverse=True)
                    return rows
                pe, ce = ladder("pe"), ladder("ce")
                out["walls"] = {"put_wall": pe[0][1] if pe else None,
                                "call_wall": ce[0][1] if ce else None}
                # secondary OI levels: next-biggest strikes per side — the
                # full defense ladder, same rungs the exit engine trails over
                out["oi_levels"] = (
                    [{"side": "PE", "strike": k, "oi": oi}
                     for oi, k in pe[1:4]] +
                    [{"side": "CE", "strike": k, "oi": oi}
                     for oi, k in ce[1:4]])
        except Exception:
            pass

        try:
            exits = getattr(scanner, "exits", None)
            if exits is not None:
                summ = exits.summary() or {}
                for p in summ.get("positions") or []:
                    if (str(p.get("symbol", "")).upper() == sym
                            and p.get("state") in ("OPEN", "PARTIAL")):
                        out["position"] = {
                            "direction": p.get("direction"),
                            "entry": p.get("entry_price"),
                            "stop": p.get("stop"),
                            "target1": p.get("target1"),
                            "target2": p.get("target2"),
                            "state": p.get("state")}
                        break
        except Exception:
            pass
        return out

    # ── candles for the chart ───────────────────────────────────────────
    @app.get("/api/candles")
    def candles(symbol: str = Query(...)):
        df = scanner.hub.candles_5m(symbol.upper().strip())
        if df is None or getattr(df, "empty", True):
            return JSONResponse(
                status_code=404,
                content={"error": f"no candles for {symbol.upper().strip()}"})
        out = []
        for ts, row in df.tail(400).iterrows():
            out.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            })
        return out

    # ── frontend (must be mounted last: a "/" mount matches every path not
    # already claimed by an /api/* route above it) ────────────────────────
    if os.path.isdir(FRONTEND_DIST):
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app

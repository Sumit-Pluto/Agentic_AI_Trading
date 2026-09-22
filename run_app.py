#!/usr/bin/env python3
"""Quant scanner app — signals + scoring + web UI + paper/LIVE execution.

Modes (UI toggle button or POST /api/mode):
    PAPER (default)  accepted signals are logged to paper_trades.jsonl,
                     no orders are placed.
    LIVE             the same signals/exits also place REAL intraday (MIS)
                     orders on Shoonya via engine.executor.LiveExecutor —
                     risk-capped (LIVE_* env knobs), daily-loss kill switch,
                     broker reconciliation at startup.

Live market data: one Shoonya websocket (ticks for the whole universe +
order updates) feeds DataHub.cash_quote and the executor's fill tracking;
REST remains the fallback when the socket is down.

Run:
    conda activate sm_agent
    python run_app.py            # walks you through the daily Shoonya login
    # then open http://127.0.0.1:8000

Env knobs (optional, via .env):
    UNIVERSE_LIMIT=25     scan only the first N F&O symbols (0 = all)
    APP_PORT=8000
    SCAN_POLL_SECONDS=60
    LIVE_RISK_RUPEES / LIVE_MAX_QTY / LIVE_MAX_CAPITAL /
    LIVE_MAX_POSITIONS / LIVE_MAX_DAILY_LOSS   (see engine/executor.py)
"""

import logging
import os

import uvicorn
from dotenv import load_dotenv

from core import trade_mode
from engine.executor import LiveExecutor
from engine.exits import ExitEngine
from engine.orchestrator import Orchestrator
from engine.scanner import Scanner
from feed.cache import TickCache
from feed.pump import FeedPump
from quant.config import QuantConfig
from quant.datahub import DataHub
from quant.registry import build_root
from server.app import create_app
from shoonya_client import ShoonyaFeed, build_session
from shoonya_login import check_registered_ip, daily_login

log = logging.getLogger("run_app")


def start_feed(session, hub, scanner, executor):
    """One websocket for the whole app: universe ticks -> freshness-aware
    push cache (feed.pump/feed.cache), order updates -> executor fill tracking.
    Self-healing; REST remains the on-demand fallback whenever a tick is stale
    or the socket is down."""
    try:
        cache = TickCache()
        # FeedPump.on_tick merges every wire frame into the cache. The REST
        # keeper is available (quote_fn wired) but left OFF: DataHub already
        # REST-falls-back on demand when a cached tick is stale, so proactive
        # polling is redundant until we tune it live on the VPS.
        pump = FeedPump(cache, quote_fn=hub._quote)
        feed = ShoonyaFeed(session, on_tick=pump.on_tick,
                           on_order=executor.on_order_update)
        feed.start()
        feed.subscribe_orders()
        keys = []
        for sym in scanner.universe:
            token = hub.cash_token(sym)
            if token:
                keys.append(f"NSE|{token}")
        # MCX commodity front-month futures — push commodity ticks into the
        # same cache so the swing / OI scanners and manual orders see live LTP.
        for sym in hub.mcx_universe():
            _exch, mtoken = hub.candle_ref(sym, "MCX")
            if mtoken:
                keys.append(f"MCX|{mtoken}")
        if keys:
            feed.subscribe(keys)
            pump.track(keys)
        hub.attach_feed(feed, cache)
        hub.feed_pump = pump          # keep alive + reachable for shutdown
        if feed.wait_connected(10):
            log.info("websocket up — %d instruments subscribed + order "
                     "stream (push cache active)", len(keys))
        else:
            log.warning("websocket not connected yet — it keeps retrying; "
                        "REST fallback active meanwhile")
        return feed
    except Exception as e:
        log.warning("websocket feed unavailable (%s) — running on REST "
                    "fallback", e)
        return None


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv()

    session = build_session()
    check_registered_ip()
    daily_login(session)

    hub = DataHub(session)
    config = QuantConfig()
    root = build_root()
    scanner = Scanner(hub, root, config,
                      universe_limit=int(os.getenv("UNIVERSE_LIMIT", "0")),
                      poll_seconds=int(os.getenv("SCAN_POLL_SECONDS", "60")))
    exits = ExitEngine(hub, scanner.paper)          # OI-aware exits
    executor = LiveExecutor(session, hub, scanner.paper)
    exits.executor = executor
    exits._sync_live_count()
    scanner.exits = exits

    start_feed(session, hub, scanner, executor)     # live ticks + orders

    if trade_mode.is_live():                        # crash-restart mid-day?
        tracked = [{"symbol": p.symbol, "direction": p.direction,
                    "live_qty_left": p.live_qty_left}
                   for p in exits.positions.values()
                   if p.live and p.state != "CLOSED"]
        report = executor.reconcile(tracked)
        if report.get("mismatches"):
            log.error("LIVE reconcile found %d mismatch(es) — check the "
                      "broker terminal before trusting the engine",
                      len(report["mismatches"]))

    scanner.start()
    Orchestrator(scanner, hub).start()   # session phases, warmup, EOD summary

    from engine.swing_scanner import SwingScanner   # Rbknox+OB swing (own cadence)
    from engine.oi_scanner import OIScanner          # OI-buildup + wall scanner
    from engine.watchlist import WatchlistStore      # per-strategy P&L-since-add
    from engine.saved_strategies import SavedStrategies  # saved option structures
    scanner.swing = SwingScanner(hub)
    scanner.oi = OIScanner(hub)
    scanner.watchlist = WatchlistStore(hub.price)
    scanner.saved_strategies = SavedStrategies(hub.chain_snapshot)
    scanner.swing.start()
    # OI-buildup sweep is OFF by default: its 239-symbol × ~34-option-leg
    # GetQuotes sweep every few minutes was tripping the REST circuit breaker
    # (stale option tokens -> HTTP 400), which then made on-demand evaluate/
    # candles return "no data" for good symbols. Re-enable with OI_SCANNER=1.
    if os.getenv("OI_SCANNER", "0") == "1":
        scanner.oi.start()
    from engine.agent_scanner import AgentScanner   # agent-PRIMARY signals
    scanner.agents = AgentScanner(hub, scanner)     # (tree is the trigger;
    if os.getenv("AGENT_SCANNER", "1") == "1":      # paper-only until proven)
        scanner.agents.start()

    from assistant import build_assistant            # LLM assistant (Qwen3.5-9B)
    scanner.assistant = build_assistant(hub, scanner)
    from assistant.macro_job import MacroJob         # feeds the MacroLLM leaf
    MacroJob(scanner.assistant.client).start()       # no-op until endpoint configured

    app = create_app(scanner, config, root)
    port = int(os.getenv("APP_PORT", "8000"))
    mode = trade_mode.mode().upper()
    banner = ("🔴 LIVE — REAL MONEY" if mode == "LIVE"
              else "paper mode (toggle LIVE in the UI)")
    print(f"\n  ── Scanner UI → http://127.0.0.1:{port} ──"
          f"\n  universe: {len(scanner.universe)} F&O stocks · {banner}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

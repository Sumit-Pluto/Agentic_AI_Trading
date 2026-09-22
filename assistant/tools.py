"""assistant/tools.py — the assistant's read-only tool layer.

Every number the assistant may state comes from one of these tools (guardrail
#1). Each tool is a JSON schema (sent to the model) + a Python executor that
reads the live system: DataHub (quotes/score/chain/vix/candles), the scanners
(swing/OI), the watchlist (per-strategy P&L), and persisted state files
(positions / news-macro). All tools are READ-ONLY — the assistant never trades.

A ``ToolContext`` wraps the live objects (hub, scanner, rag). Executors are
defensive: any failure returns ``{"error": ...}`` rather than raising, so a
missing feed / cold cache degrades gracefully instead of breaking the chat.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class ToolContext:
    hub: Any = None            # quant.datahub.DataHub
    scanner: Any = None        # engine.scanner.Scanner (has .evaluate/.paper/.exits/.watchlist)
    rag: Any = None            # assistant.rag.DocIndex
    state_dir: str = "state"

    # ── thin, defensive accessors ────────────────────────────────────────
    def price(self, symbol: str, segment: str | None = None) -> Optional[float]:
        try:
            return self.hub.price(symbol, segment) if self.hub else None
        except Exception:
            return None

    def read_state(self, name: str) -> dict:
        try:
            with open(os.path.join(self.state_dir, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


# ── registry ─────────────────────────────────────────────────────────────
_TOOLS: list[dict] = []


def _tool(name: str, description: str, properties: dict, required: list | None = None):
    def deco(fn: Callable):
        _TOOLS.append({"name": name, "description": description, "fn": fn,
                       "parameters": {"type": "object", "properties": properties,
                                      "required": required or []}})
        return fn
    return deco


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── trade / account ────────────────────────────────────────────────────
@_tool("get_positions", "Current open positions with live unrealized P&L. "
       "Optionally filter by strategy (intraday/swing/manual/oi) or segment "
       "(CASH/FNO/MCX).",
       {"strategy": {"type": "string"}, "segment": {"type": "string"}})
def get_positions(args, ctx: ToolContext):
    sc = ctx.scanner
    exits = getattr(sc, "exits", None)
    if exits is not None and getattr(exits, "positions", None):
        rows = [p.to_dict() for p in exits.positions.values()]
    else:
        rows = ctx.read_state("paper_positions.json").get("positions", [])
    out = []
    for p in rows:
        if args.get("strategy") and p.get("strategy") != args["strategy"]:
            continue
        if args.get("segment") and p.get("segment") != args["segment"]:
            continue
        if p.get("state") == "CLOSED":
            continue
        entry = _num(p.get("entry_price"))
        cur = ctx.price(p.get("symbol"), p.get("segment"))
        pnl = None
        if entry and cur:
            sign = 1.0 if p.get("direction") == "BUY" else -1.0
            pnl = {"current": round(cur, 2),
                   "unrealized_per_share": round((cur - entry) * sign, 2),
                   "unrealized_pct": round((cur - entry) * sign / entry * 100, 2)}
        out.append({"symbol": p.get("symbol"), "direction": p.get("direction"),
                    "strategy": p.get("strategy"), "segment": p.get("segment"),
                    "entry_price": entry, "stop": p.get("stop"),
                    "target1": p.get("target1"), "pnl": pnl})
    return {"count": len(out), "positions": out}


@_tool("get_recent_trades", "Recent trades across ALL books — paper, SIMULATED "
       "(from 'Simulate last day'), and live-money — each tagged with a "
       "trade_type so you can tell them apart. Use this for any question about "
       "the user's trades; simulated trades ARE included here. Newest last.",
       {"limit": {"type": "integer", "description": "how many (default 20)"}})
def get_recent_trades(args, ctx: ToolContext):
    sc = ctx.scanner
    n = int(args.get("limit") or 20)
    trades: list = []
    # Unified live+sim trade rows (same source the P&L dashboard shows), each
    # carrying source="paper"|"sim". Tag them so the model can identify the book.
    try:
        from engine.pnl import build_dashboard
        dash = build_dashboard(sc) if sc is not None else {}
        for t in dash.get("trades", []):
            row = dict(t)
            src = t.get("source")
            row["trade_type"] = ("simulated" if src == "sim"
                                 else "live_money" if (t.get("live") or t.get("real"))
                                 else "paper")
            trades.append(row)
    except Exception:
        trades = []
    # fall back to the raw paper/live log if the dashboard isn't available
    if not trades:
        try:
            for r in (sc.paper.tail(n) if getattr(sc, "paper", None) else []):
                row = dict(r) if isinstance(r, dict) else {"raw": r}
                row.setdefault("trade_type", "paper")
                trades.append(row)
        except Exception:
            trades = []
    by_type: dict = {}
    for t in trades:
        k = t.get("trade_type", "paper")
        by_type[k] = by_type.get(k, 0) + 1
    return {"count": len(trades), "by_type": by_type, "trades": trades[-n:],
            "legend": ("trade_type: simulated = replayed last-day walk (NOT real "
                       "money, excluded from account P&L); paper = paper-mode "
                       "entry; live_money = real Shoonya order.")}


# ── scoring ──────────────────────────────────────────────────────────────
@_tool("get_score", "Run the quant agent tree for a symbol+direction and "
       "return the composite score, accept/veto, and each agent family's "
       "score. Works for Cash/F&O/MCX.",
       {"symbol": {"type": "string"}, "direction": {"type": "string", "enum": ["BUY", "SELL"]},
        "segment": {"type": "string"}},
       required=["symbol", "direction"])
def get_score(args, ctx: ToolContext):
    sc = ctx.scanner
    if sc is None:
        return {"error": "scanner unavailable"}
    try:
        res = sc.evaluate(str(args["symbol"]).upper(),
                          str(args["direction"]).upper(),
                          segment=args.get("segment"))
    except Exception as e:
        return {"error": f"evaluate failed: {e}"}
    if not res:
        return {"error": f"no data for {args.get('symbol')}"}
    tree = res.get("tree", {})
    families = [{"key": c.get("key"), "score": c.get("score")}
                for c in tree.get("children", [])]
    return {"symbol": res.get("symbol"), "direction": res.get("direction"),
            "segment": res.get("segment"), "price": res.get("price"),
            "score": res.get("score"), "threshold": res.get("threshold"),
            "accepted": res.get("accepted"), "vetoed_by": res.get("vetoed_by"),
            "families": families}


# ── market data ──────────────────────────────────────────────────────────
@_tool("get_quote", "Last traded price for a symbol (Cash/F&O spot or MCX "
       "front-month future).",
       {"symbol": {"type": "string"}, "segment": {"type": "string"}},
       required=["symbol"])
def get_quote(args, ctx: ToolContext):
    px = ctx.price(str(args["symbol"]).upper(), args.get("segment"))
    if px is None:
        return {"error": "no quote"}
    return {"symbol": str(args["symbol"]).upper(), "price": round(px, 2)}


@_tool("get_vix", "India VIX (volatility index) last value.", {})
def get_vix(args, ctx: ToolContext):
    try:
        v = ctx.hub.india_vix() if ctx.hub else None
    except Exception:
        v = None
    return {"india_vix": v} if v is not None else {"error": "vix unavailable"}


@_tool("get_option_chain", "Option-chain summary for an F&O symbol: spot, the "
       "put wall (max PE OI) and call wall (max CE OI).",
       {"symbol": {"type": "string"}}, required=["symbol"])
def get_option_chain(args, ctx: ToolContext):
    try:
        ch = ctx.hub.chain_snapshot(str(args["symbol"]).upper()) if ctx.hub else None
    except Exception as e:
        return {"error": f"chain failed: {e}"}
    if not ch or not getattr(ch, "strikes", None):
        return {"error": "no chain"}
    put_wall = call_wall = None
    put_oi = call_oi = 0.0
    for r in ch.strikes:
        if r.pe and (r.pe.oi or 0) > put_oi:
            put_oi, put_wall = r.pe.oi, float(r.strike)
        if r.ce and (r.ce.oi or 0) > call_oi:
            call_oi, call_wall = r.ce.oi, float(r.strike)
    return {"symbol": str(args["symbol"]).upper(), "spot": getattr(ch, "spot", None),
            "put_wall": put_wall, "call_wall": call_wall}


@_tool("get_candles", "Recent OHLC summary for a symbol (last close, session "
       "high/low, % change). interval minutes (5/15/60) or '1d'.",
       {"symbol": {"type": "string"}, "interval": {"type": "string"},
        "segment": {"type": "string"}}, required=["symbol"])
def get_candles(args, ctx: ToolContext):
    if ctx.hub is None:
        return {"error": "no hub"}
    sym = str(args["symbol"]).upper()
    interval = str(args.get("interval") or "5")
    seg = args.get("segment")
    try:
        df = (ctx.hub.daily_candles(sym, segment=seg) if interval == "1d"
              else ctx.hub.candles(sym, interval=interval, segment=seg))
    except Exception as e:
        return {"error": f"candles failed: {e}"}
    if df is None or len(df) == 0:
        return {"error": "no candles"}
    last = float(df["close"].iloc[-1])
    first = float(df["close"].iloc[0])
    return {"symbol": sym, "interval": interval, "bars": int(len(df)),
            "last_close": round(last, 2), "high": round(float(df["high"].max()), 2),
            "low": round(float(df["low"].min()), 2),
            "change_pct": round((last - first) / first * 100, 2) if first else None}


# ── news / macro ─────────────────────────────────────────────────────────
@_tool("get_news_macro", "Macro + news snapshot from the news hub: risk score, "
       "the global market tape (indices/FX/commodities), FII/DII flows (+ net), "
       "and the top weighted headlines WITH the F&O symbols and sectors each "
       "one impacts. Use this to summarise the news OR to find which stocks the "
       "current news affects.",
       {"limit": {"type": "integer", "description": "how many headlines (default 12)"}})
def get_news_macro(args, ctx: ToolContext):
    s = ctx.read_state("news_state.json")
    if not s:
        return {"error": "no news state yet"}
    n = int(args.get("limit") or 12)
    weighted = s.get("weighted", [])[:n]
    fii = s.get("fii_dii") or {}
    net = None
    try:
        net = round(_num(fii.get("fii_net_cr")) + _num(fii.get("dii_net_cr")), 1) \
            if fii.get("fii_net_cr") is not None else None
    except (TypeError, ValueError):
        net = None
    return {"generated_at": s.get("generated_at"), "risk_score": s.get("risk_score"),
            "market_tape": s.get("market_tape"), "fii_dii": fii,
            "net_institutional_cr": net,
            "news_counts": {"indian": len(s.get("indian_news") or []),
                            "global": len(s.get("global_news") or [])},
            "top_news": [{"title": w.get("title"), "weight": w.get("weight"),
                          "symbols": w.get("symbols"), "sectors": w.get("sectors"),
                          "reasons": w.get("reasons")} for w in weighted]}


# ── strategy scanners ────────────────────────────────────────────────────
@_tool("get_swing_signals", "Current Rbknox + Order-Block SWING signals across "
       "Cash/F&O/MCX (daily + intraday).",
       {"limit": {"type": "integer"}})
def get_swing_signals(args, ctx: ToolContext):
    s = ctx.read_state("swing_signals.json")
    sigs = s.get("signals", [])[: int(args.get("limit") or 15)]
    return {"last_scan": s.get("last_scan"), "count": len(sigs), "signals": sigs}


@_tool("get_oi_signals", "Current OI-strategy hits (price×OI buildup confirmed "
       "by option walls) across F&O/MCX.",
       {"limit": {"type": "integer"}})
def get_oi_signals(args, ctx: ToolContext):
    s = ctx.read_state("oi_signals.json")
    sigs = s.get("signals", [])[: int(args.get("limit") or 15)]
    return {"last_scan": s.get("last_scan"), "count": len(sigs), "signals": sigs}


@_tool("get_watchlist", "Saved watchlist items with P&L SINCE you added them, "
       "grouped/aggregated by strategy (so you can judge if a strategy works). "
       "Optionally one strategy.", {"strategy": {"type": "string"}})
def get_watchlist(args, ctx: ToolContext):
    wl = getattr(ctx.scanner, "watchlist", None)
    if wl is not None:
        try:
            return (wl.strategy_pnl(args["strategy"]) if args.get("strategy")
                    else wl.overview())
        except Exception:
            pass
    return ctx.read_state("watchlists.json") or {"strategies": []}


# ── docs / RAG ───────────────────────────────────────────────────────────
@_tool("search_docs", "Search the project's own documentation and agent specs "
       "(docs/) for definitions, methodology and terminology. Use this to "
       "explain HOW the system works or what a term means.",
       {"query": {"type": "string"}, "k": {"type": "integer"}},
       required=["query"])
def search_docs(args, ctx: ToolContext):
    if ctx.rag is None:
        return {"error": "docs index unavailable"}
    try:
        hits = ctx.rag.search(str(args["query"]), k=int(args.get("k") or 4))
    except Exception as e:
        return {"error": f"search failed: {e}"}
    return {"results": hits}


# ── exported wiring ──────────────────────────────────────────────────────
def tool_schemas() -> list[dict]:
    """OpenAI-format tool schemas for the chat request."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}} for t in _TOOLS]


def tool_fns() -> dict:
    return {t["name"]: t["fn"] for t in _TOOLS}


def execute(name: str, args: dict, ctx: ToolContext) -> dict:
    fn = tool_fns().get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(args or {}, ctx)
    except Exception as e:            # never let a tool crash the turn
        return {"error": f"{type(e).__name__}: {e}"}

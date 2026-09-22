"""Live end-to-end test of the assistant against the REAL Qwen3.5-9B endpoint.

Seeds fake state (positions, trades, news/macro, swing + OI signals, watchlist),
wires a mock DataHub/scanner + the real docs RAG, and runs a battery of user
questions through the live LLM (via .env LLM_BASE_URL / tunnel). For each it
prints the tools the model called, the answer, and the guardrail verdict.

Run:  python -m assistant.live_test
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from assistant.agent import Assistant
from assistant.audit import AuditLog
from assistant.client import LLMClient
from assistant.rag import DocIndex
from assistant.tools import ToolContext
from engine.watchlist import WatchlistStore

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST).isoformat(timespec="seconds")

PRICES = {"RELIANCE": 2954.30, "TCS": 4120.50, "CRUDEOIL": 6850.0,
          "TATASTEEL": 145.20, "NIFTY": 24180.5}


class MockHub:
    def price(self, s, seg=None):
        return PRICES.get(s)

    def india_vix(self):
        return 13.8

    def segment_of(self, s):
        if s == "CRUDEOIL":
            return "MCX"
        return "FNO" if s in ("RELIANCE", "TCS", "TATASTEEL") else "CASH"

    def cash_quote(self, s, seg=None):
        p = PRICES.get(s)
        return {"lp": p} if p else None

    def chain_snapshot(self, s):
        return NS(spot=PRICES.get(s, 0.0), strikes=[
            NS(strike=2900.0, pe=NS(oi=52000), ce=NS(oi=1200)),
            NS(strike=2950.0, pe=NS(oi=8000), ce=NS(oi=9000)),
            NS(strike=3000.0, pe=NS(oi=900), ce=NS(oi=61000)),
        ])

    def candles(self, s, interval="5", days=7, segment=None):
        import numpy as np
        import pandas as pd
        n = 40
        idx = pd.date_range("2026-07-10 09:15", periods=n, freq="5min")
        base = PRICES.get(s, 100.0)
        close = np.linspace(base * 0.99, base, n)
        return pd.DataFrame({"open": close, "high": close + 2, "low": close - 2,
                             "close": close, "volume": np.full(n, 1000.0),
                             "oi": np.zeros(n)}, index=idx)

    def daily_candles(self, s, days=40, segment=None):
        return self.candles(s)


def _seed(state_dir):
    os.makedirs(state_dir, exist_ok=True)

    def w(name, obj):
        with open(os.path.join(state_dir, name), "w") as f:
            json.dump(obj, f)

    w("paper_positions.json", {"positions": [
        {"id": "RELIANCE-BUY-a1", "symbol": "RELIANCE", "direction": "BUY",
         "strategy": "intraday", "segment": "FNO", "entry_price": 2900.0,
         "stop": 2870.0, "target1": 2960.0, "state": "OPEN", "qty_units": 1.0},
        {"id": "CRUDEOIL-SELL-b2", "symbol": "CRUDEOIL", "direction": "SELL",
         "strategy": "swing", "segment": "MCX", "entry_price": 6900.0,
         "stop": 6950.0, "target1": 6800.0, "state": "OPEN", "qty_units": 1.0},
    ]})
    w("news_state.json", {
        "generated_at": NOW, "risk_score": 0.35,
        "market_tape": {"spx": {"price": 6100.0, "chg_pct": 0.8},
                        "usdinr": {"price": 83.5, "chg_pct": 0.1},
                        "gold": {"price": 2400.0, "chg_pct": -0.4}},
        "fii_dii": {"date": NOW[:10], "fii_net_cr": 1500.0, "dii_net_cr": -300.0},
        "weighted": [
            {"title": "RBI holds repo rate at 6.5%", "weight": 8, "symbols": ["BANKNIFTY"]},
            {"title": "Crude jumps on OPEC supply cut", "weight": 6, "symbols": ["ONGC"]}]})
    w("swing_signals.json", {"last_scan": NOW, "signals": [
        {"symbol": "TATASTEEL", "segment": "FNO", "interval": "1d",
         "direction": "BUY", "price": 145.20, "entry": 145.0, "stop": 140.0,
         "target": 160.0, "rr": 3.0, "ob_bottom": 142.0, "ob_top": 146.0,
         "knox_rsi": 28.0}]})
    w("oi_signals.json", {"last_scan": NOW, "signals": [
        {"symbol": "RELIANCE", "segment": "FNO", "kind": "long_buildup",
         "direction": "BUY", "price": 2954.30, "px_chg_pct": 1.2,
         "oi_chg_pct": 5.4, "put_wall": 2900.0, "call_wall": 3000.0}]})
    trades = [
        {"type": "paper_entry", "symbol": "RELIANCE", "direction": "BUY",
         "price": 2900.0, "score": 72, "strategy": "intraday", "segment": "FNO",
         "trigger": {"bar_time": "2026-07-10T10:05:00"}, "logged_at": NOW},
        {"type": "paper_entry", "symbol": "CRUDEOIL", "direction": "SELL",
         "price": 6900.0, "score": 68, "strategy": "swing", "segment": "MCX",
         "trigger": {"bar_time": "2026-07-10T11:15:00"}, "logged_at": NOW}]
    return trades


QUESTIONS = [
    "What are my open positions and how are they doing right now?",
    "Give me the agent score for a RELIANCE buy, and the top agent families.",
    "What's the macro / news picture and the risk score right now?",
    "Are there any swing signals or OI-strategy hits at the moment?",
    "How is my OI watchlist performing since I added the stocks?",
    "Explain how the OI buildup strategy decides on a hit.",
    "Should I buy RELIANCE right now?",
]


def main():
    client = LLMClient()
    print(f"endpoint : {client.base_url}   model: {client.model}")
    if not client.available():
        print("!! LLM not configured — set LLM_BASE_URL / LLM_API_KEY in .env")
        return
    try:
        import openai  # noqa: F401
    except ImportError:
        print("!! `pip install openai` first")
        return

    import tempfile
    state_dir = tempfile.mkdtemp(prefix="asst_live_")
    trades = _seed(state_dir)

    hub = MockHub()
    import engine.watchlist as _wl_mod              # isolate the store to temp
    _wl_mod.STORE = os.path.join(state_dir, "watchlists.json")
    wl = WatchlistStore(hub.price, clock=lambda: NOW)
    wl.items = []                                   # fresh (ignore any real file)
    wl.add(symbol="RELIANCE", strategy="oi", direction="BUY", segment="FNO",
           price_at_add=2920.0)
    wl.add(symbol="TATASTEEL", strategy="swing", direction="BUY", segment="FNO",
           price_at_add=140.0)

    scanner = NS(
        exits=None, hub=hub, watchlist=wl,
        paper=NS(tail=lambda n: trades[-n:]),
        evaluate=lambda symbol, direction, segment=None: {
            "symbol": symbol, "direction": direction,
            "segment": segment or hub.segment_of(symbol),
            "price": PRICES.get(symbol), "score": 72 if direction == "BUY" else 45,
            "threshold": 60, "accepted": direction == "BUY", "vetoed_by": None,
            "tree": {"children": [{"key": "smc", "score": 70}, {"key": "snr", "score": 68},
                                  {"key": "volatility", "score": 75}, {"key": "volume", "score": 66},
                                  {"key": "macro", "score": 71}]}})

    try:
        rag = DocIndex()
    except Exception:
        rag = None
    ctx = ToolContext(hub=hub, scanner=scanner, rag=rag, state_dir=state_dir)
    asst = Assistant(client, ctx, audit=AuditLog(os.path.join(state_dir, "audit.jsonl")))

    print(f"docs indexed: {len(rag.chunks) if rag else 0} chunks\n" + "=" * 72)
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[{i}] USER: {q}")
        t0 = time.perf_counter()
        try:
            r = asst.chat("live", q)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue
        dt = time.perf_counter() - t0
        print(f"    tools : {r['tools_used'] or '—'}   "
              f"mode={r['mode']}  validator={r['validator']}  ({dt:.1f}s)")
        print(f"    ASSISTANT: {r['reply'].strip()[:800]}")
    print("\n" + "=" * 72 + "\nlive test complete.")


if __name__ == "__main__":
    main()

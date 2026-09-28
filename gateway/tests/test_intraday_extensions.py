"""Tests for the intraday-engine Gateway extensions:

  1. depth-forwarding WS mode  — TickerManager forwards SNAPQUOTE (dk/df) frames,
     which carry open interest, to clients that opted into feed="depth", without
     disturbing the touchline path.
  2. /api/candles/batch        — fetch many instruments' candles in one call,
     normalised oldest-first, with per-item error isolation.

Both run fully offline (fake broker); no live Shoonya session required.
"""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── 1. depth-forwarding WS mode ─────────────────────────────────────────────
def test_depth_forwarding_routes_snapquote_and_forwards_oi():
    import ticker_manager as TM

    class FakeBroker:
        def __init__(self):
            self.calls = []

        def ws_subscribe(self, sym, feed=None):
            self.calls.append(("sub", sym, feed))

        def ws_unsubscribe(self, sym, feed=None):
            self.calls.append(("unsub", sym, feed))

    tm = TM.TickerManager()
    tm._snapquote = "d"          # pre-seed FeedType.SNAPQUOTE so no SDK import
    tm._broker = FakeBroker()
    tm._ws_open = True

    # depth subscribe → SNAPQUOTE; touchline default unchanged
    tm.subscribe(["NFO|111"], feed="depth")
    tm.subscribe(["NSE|26000"])
    assert ("sub", "NFO|111", "d") in tm._broker.calls
    assert ("sub", "NSE|26000", None) in tm._broker.calls
    assert "NFO|111" in tm._depth_subscribed
    assert "NSE|26000" in tm._subscribed

    captured = []

    class FakeWS:
        async def send_text(self, m):
            captured.append(json.loads(m))

    loop = asyncio.new_event_loop()
    tm._loop = loop
    tm._clients = {FakeWS()}

    async def drive():
        # df = depth feed (carries oi/poi); tf = touchline
        tm._on_tick({"t": "df", "tk": "111", "e": "NFO", "lp": "120.5",
                     "oi": "1500", "poi": "1400", "ft": "123"})
        tm._on_tick({"t": "tf", "tk": "26000", "e": "NSE", "lp": "24800"})
        await asyncio.sleep(0.05)   # let scheduled broadcasts flush

    loop.run_until_complete(drive())
    loop.close()

    depth = [m for m in captured if m.get("type") == "depth"]
    touch = [m for m in captured if "type" not in m]
    assert depth and depth[0]["oi"] == "1500" and depth[0]["poi"] == "1400"
    assert touch and touch[0]["lp"] == "24800"

    # unsubscribe removes from the correct feed
    tm.unsubscribe(["NFO|111"])
    assert ("unsub", "NFO|111", "d") in tm._broker.calls
    assert "NFO|111" not in tm._depth_subscribed


def test_touchline_only_client_never_sees_depth_when_no_depth_subs():
    """A pure touchline consumer's path is byte-for-byte unchanged."""
    import ticker_manager as TM

    tm = TM.TickerManager()
    tm._ws_open = True
    captured = []

    class FakeWS:
        async def send_text(self, m):
            captured.append(json.loads(m))

    loop = asyncio.new_event_loop()
    tm._loop = loop
    tm._clients = {FakeWS()}

    async def drive():
        tm._on_tick({"t": "tf", "tk": "26000", "e": "NSE", "lp": "24800",
                     "ltq": "50", "v": "1000"})
        await asyncio.sleep(0.02)

    loop.run_until_complete(drive())
    loop.close()
    assert len(captured) == 1 and captured[0]["lp"] == "24800"
    assert captured[0]["ltq"] == "50"  # ghost-tick guard field preserved


# ── 2. /api/candles/batch ───────────────────────────────────────────────────
def _mini_app_with_fake_broker(monkeypatch):
    import app.routers.market as market
    from brokers.base import BrokerError

    class FakeBroker:
        async def getCandles(self, session, credentials, *, exchange, symbol_token,
                             interval=15, starttime=0, endtime=0,
                             tradingsymbol="", daily=False):
            if symbol_token == "BAD":
                raise BrokerError("boom", broker="shoonya")
            return [  # NorenApi returns newest-first
                {"time": "2", "into": "101", "inth": "103", "intl": "100",
                 "intc": "102", "intv": "50"},
                {"time": "1", "into": "100", "inth": "101", "intl": "99",
                 "intc": "100", "intv": "40"},
            ]

    monkeypatch.setattr(market, "_require_legacy_auth",
                        lambda: SimpleNamespace(auth_token="t", user_id="U"))
    monkeypatch.setattr(market, "get_broker", lambda name: FakeBroker())
    app = FastAPI()
    app.include_router(market.router)
    return app


def test_candles_batch_normalizes_oldest_first_and_isolates_errors(monkeypatch):
    client = TestClient(_mini_app_with_fake_broker(monkeypatch))
    r = client.post("/api/candles/batch", json={"items": [
        {"exchange": "NSE", "token": "26000", "interval": 5},
        {"exchange": "NFO", "token": "BAD", "interval": 5},
    ], "concurrency": 4})
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    good = next(x for x in res if x["token"] == "26000")
    bad = next(x for x in res if x["token"] == "BAD")
    assert good["candles"][0]["time"] == "1"       # reversed to oldest-first
    assert good["candles"][0]["close"] == 100.0
    assert good["candles"][-1]["close"] == 102.0
    assert "error" in bad and bad["candles"] == []


def test_single_candles_endpoint_intact(monkeypatch):
    client = TestClient(_mini_app_with_fake_broker(monkeypatch))
    r = client.get("/api/candles", params={"exchange": "NSE", "token": "26000",
                                            "interval": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == 5 and len(body["candles"]) == 2

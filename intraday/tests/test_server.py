"""Cockpit backend smoke test (offline, sim market): the FastAPI app serves the
live state over REST + WebSocket and the engine trades in the background."""
import os
import tempfile
import time

from fastapi.testclient import TestClient


def _client():
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "cfg.json")
    with open(cfg, "w") as f:
        f.write('{"demo_step_seconds": 0.1, "score_threshold": 52, "score_margin": 3,'
                ' "paper_exec_delay_s": 0}')
    os.environ["INTRADAY_CONFIG"] = cfg
    os.environ["INTRADAY_DB"] = os.path.join(tmp, "intraday.db")
    from intraday.server.app import app
    return TestClient(app)


def test_rest_and_ws_stream_live_state():
    with _client() as c:
        time.sleep(1.5)                       # ~15 engine steps
        st = c.get("/api/state").json()
        assert st["mode"] == "sim" and st["equity"] is not None
        assert "regime" in st

        chain = c.get("/api/chain", params={"symbol": "NIFTY"}).json()
        assert chain["spot"] and len(chain["rows"]) > 0
        assert "pcr_oi" in chain["features"]

        ag = c.get("/api/agents").json()
        assert len(ag["rows"]) == 25          # all agents scored/logged

        rep = c.get("/api/reporting").json()
        assert "win_rate" in rep and "avg_latency_ms" in rep

        # live WebSocket delivers advancing snapshots
        with c.websocket_connect("/ws") as ws:
            a = ws.receive_json()
            b = ws.receive_json()
            assert a["type"] == "snapshot" and b["seq"] >= a["seq"]
            assert "state" in b and "chains" in b and len(b["agent_rows"]) > 0


def test_snapshot_carries_position_and_close_audit():
    with _client() as c:
        time.sleep(1.0)
        with c.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert "closed_today" in snap and isinstance(snap["closed_today"], list)
            for p in snap.get("positions", []):
                for k in ("lot_size", "lots", "qty", "stop", "stop_prem",
                          "risk_per_share", "r_mult", "entry_ts"):
                    assert k in p, f"position missing {k}: {sorted(p)}"


def test_orderbook_and_positions_offline_without_gateway():
    with _client() as c:
        ob = c.get("/api/orderbook").json()
        assert ob == {"connected": False, "orders": []}
        bp = c.get("/api/broker-positions").json()
        assert bp == {"connected": False, "positions": []}


def test_orderbook_and_positions_with_fake_gateway():
    import intraday.server.app as appmod

    class _FakeGW:
        def order_book(self):
            return [
                {"norenordno": "111", "tsym": "NIFTY29SEP26C24800", "exch": "NFO",
                 "prd": "I", "trantype": "B", "qty": "50", "fillshares": "50",
                 "status": "COMPLETE", "prc": "100.5", "avgprc": "100.4",
                 "pytime": "10:01:11", "remarks": "entry:test"},
                {"norenordno": "112", "tsym": "NIFTY29SEP26C24800", "exch": "NFO",
                 "prd": "I", "trantype": "S", "qty": "50", "fillshares": "0",
                 "status": "OPEN", "prc": "150.0", "avgprc": "0",
                 "pytime": "10:05:00", "remarks": "exit:test"},
            ]

        def positions(self):
            return [
                {"tsym": "NIFTY29SEP26C24800", "exch": "NFO", "prd": "I",
                 "netqty": "50", "buyavgprc": "100.4", "sellavgprc": "0",
                 "lp": "120.0", "urmtom": "980.0", "rpnl": "0",
                 "total_pnl": "980.0", "lotsize": "50"},
                {"tsym": "FLATLEG", "exch": "NFO", "prd": "I", "netqty": "0",
                 "buyavgprc": "0", "sellavgprc": "0", "lp": "0", "lotsize": "1"},
            ]

        def funds(self):
            return {"cash": 95000.0, "margin_used": 12000.0,
                    "payin": 100000.0, "collateral": 0.0}

    with _client() as c:
        appmod.runner.client = _FakeGW()
        ob = c.get("/api/orderbook").json()
        assert ob["connected"] is True and len(ob["orders"]) == 2
        first, second = ob["orders"]          # latest first
        assert (first["id"], first["side"], first["status"]) == ("112", "SELL", "OPEN")
        assert (second["id"], second["filled"], second["avg"]) == ("111", 50, 100.4)
        bp = c.get("/api/broker-positions").json()
        assert bp["connected"] is True and len(bp["positions"]) == 1  # flat row dropped
        p = bp["positions"][0]
        assert (p["tsym"], p["side"], p["qty"], p["avg"]) == \
            ("NIFTY29SEP26C24800", "BUY", 50, 100.4)
        assert p["mtm"] == 980.0 and p["lot_size"] == 50
        f = c.get("/api/funds").json()
        assert f["funds"]["cash"] == 95000.0
        appmod.runner.client = None


def test_pause_toggle_and_config_update():
    with _client() as c:
        assert c.post("/api/pause", json={"paused": True}).json()["paused"] is True
        assert c.post("/api/pause", json={"paused": False}).json()["paused"] is False
        r = c.post("/api/config", json={"score_threshold": 61}).json()
        assert r["ok"] and r["config"]["score_threshold"] == 61
        # LIVE mode is guarded off the sim engine
        assert "error" in c.post("/api/mode", json={"mode": "live"}).json()


def test_kill_and_resume_with_typed_confirm():
    with _client() as c:
        assert c.post("/api/kill").json()["halted"] is True
        assert c.post("/api/resume", json={}).status_code == 400
        r = c.post("/api/resume", json={"confirm": "RESUME"}).json()
        assert r["halted"] is False and r["paused"] is False

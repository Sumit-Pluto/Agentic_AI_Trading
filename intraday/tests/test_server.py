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


def test_pause_toggle_and_config_update():
    with _client() as c:
        assert c.post("/api/pause", json={"paused": True}).json()["paused"] is True
        assert c.post("/api/pause", json={"paused": False}).json()["paused"] is False
        r = c.post("/api/config", json={"score_threshold": 61}).json()
        assert r["ok"] and r["config"]["score_threshold"] == 61
        # LIVE mode is guarded off the sim engine
        assert "error" in c.post("/api/mode", json={"mode": "live"}).json()

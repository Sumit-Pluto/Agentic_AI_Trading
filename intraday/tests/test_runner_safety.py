"""Runner/server safety rails (offline).

  • LIVE broker is refused on the simulated data feed, even with a Gateway
    client and a typed confirmation — real orders need real data.
  • Mutating REST endpoints require a bearer token once INTRADAY_API_TOKEN is
    set, and stay open otherwise (zero-setup local cockpit).
"""
import os
import tempfile

from fastapi.testclient import TestClient


def _runner(tmp_path, monkeypatch, **cfg_kw):
    monkeypatch.setenv("INTRADAY_DB", str(tmp_path / "runner.db"))
    monkeypatch.setenv("INTRADAY_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.delenv("GATEWAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_SECRET", raising=False)
    from intraday.server.runner import EngineRunner
    cfg = {"engine_mode": "sim", "mode": "paper", "universe": ["NIFTY"],
           "demo_step_seconds": 60.0}
    cfg.update(cfg_kw)
    return EngineRunner(cfg)


def test_live_refused_on_simulated_data(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    assert r.client is None                        # no gateway creds here
    r.client = object()                            # ...even with one present
    res = r.set_mode("live", "LIVE")
    assert "error" in res and "simulated" in res["error"]
    assert r.cfg["mode"] == "paper" and r.loop.broker.name == "paper"


def test_live_allowed_on_live_engine_with_client(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    r.client = object()
    r.engine_mode = "live"                         # real-data deployment
    res = r.set_mode("live", "LIVE")
    assert res["mode"] == "live" and res["broker"] == "gateway"


def _server_client(monkeypatch, token=None):
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "cfg.json")
    open(cfg, "w").write('{"demo_step_seconds": 60.0}')
    monkeypatch.setenv("INTRADAY_CONFIG", cfg)
    monkeypatch.setenv("INTRADAY_DB", os.path.join(tmp, "intraday.db"))
    monkeypatch.delenv("GATEWAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_SECRET", raising=False)
    if token is None:
        monkeypatch.delenv("INTRADAY_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INTRADAY_API_TOKEN", token)
    from intraday.server.app import app
    return TestClient(app)


def test_mutations_open_without_token(monkeypatch):
    with _server_client(monkeypatch) as c:
        assert c.post("/api/pause", json={"paused": True}).status_code == 200
        assert c.post("/api/pause", json={"paused": False}).status_code == 200


def test_mutations_need_token_when_configured(monkeypatch):
    with _server_client(monkeypatch, token="s3cret") as c:
        assert c.post("/api/pause", json={"paused": True}).status_code == 401
        assert c.post("/api/mode", json={"mode": "paper"}).status_code == 401
        assert c.post("/api/kill").status_code == 401
        h = {"Authorization": "Bearer s3cret"}
        assert c.post("/api/pause", json={"paused": True}, headers=h).status_code == 200
        assert c.get("/api/state").status_code == 200      # reads stay open

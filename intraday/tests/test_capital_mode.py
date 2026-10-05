"""Stage C: capital allocation (budget/caps/global-SL) in the Governor, and the
server's PAPER<->LIVE toggle (typed confirm), kill switch, and budget endpoint."""
import os
import tempfile
import time

from fastapi.testclient import TestClient

from intraday.risk import Governor


class _P:
    def __init__(self, px, q):
        self.entry_px, self.qty, self.is_long = px, q, True


def test_governor_budget_caps_and_global_sl():
    g = Governor({"total_budget": 100000, "soft_cap_pct": 80, "hard_cap_pct": 90,
                  "global_sl_pct": 5, "max_daily_loss_rupees": 9e9})
    assert g.budget() == 100000
    pos = [_P(200, 50)]                       # 10,000 deployed
    assert g.deployed_premium(pos) == 10000
    assert g.utilisation_pct(pos) == 10.0 and g.cap_state(pos) == "OK"
    assert g.can_open_new(pos, extra_premium=10000)[0] is True
    # near the hard cap -> a new entry is refused
    big = [_P(85000, 1)]
    ok, why = g.can_open_new(big, extra_premium=10000)   # 95% projected
    assert ok is False and "hard cap" in why
    assert g.cap_state([_P(82000, 1)]) == "SOFT_CAP"
    # global MTM stop: 5% of 100k = 5,000
    assert g.daily_loss_breached(-4000.0, -1500.0) is True     # -5,500
    assert g.daily_loss_breached(-1000.0) is False


def _server_client():
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "cfg.json")
    open(cfg, "w").write('{"demo_step_seconds": 0.1, "total_budget": 150000,'
                        ' "paper_exec_delay_s": 0}')
    os.environ["INTRADAY_CONFIG"] = cfg
    os.environ["INTRADAY_DB"] = os.path.join(tmp, "intraday.db")
    # ensure no gateway creds leak in from the environment
    for k in ("GATEWAY_CLIENT_ID", "GATEWAY_CLIENT_SECRET"):
        os.environ.pop(k, None)
    from intraday.server.app import app
    return TestClient(app)


def test_paper_live_toggle_kill_and_budget():
    with _server_client() as c:
        time.sleep(0.6)
        # defaults to PAPER on the paper broker (no gateway creds in this env)
        m = c.get("/api/mode").json()
        assert m["mode"] == "paper" and m["broker"] == "paper"

        # LIVE needs a typed confirmation
        r = c.post("/api/mode", json={"mode": "live"})
        assert r.status_code == 400 and "confirm" in r.json()["error"]
        # confirmed, but no Gateway client available -> refused (stays safe)
        r = c.post("/api/mode", json={"mode": "live", "confirm": "LIVE"})
        assert r.status_code == 400 and "Gateway" in r.json()["error"]
        assert c.get("/api/mode").json()["mode"] == "paper"

        # budget tile data
        b = c.get("/api/budget").json()
        assert b["budget"]["total"] == 150000 and b["budget"]["cap_state"] == "OK"

        # kill switch halts + requests a flatten
        k = c.post("/api/kill").json()
        assert k["halted"] is True and k["paused"] is True

"""Agents + combiner + scanner, offline with synthetic bars + chain.

Confirms: every registered agent runs without raising and returns a valid
result; a strong uptrend + bullish (put-heavy) chain fires a BUY that selects a
CE leg; regime veto (VIX spike) suppresses all signals.
"""
import datetime as dt

import numpy as np
import pandas as pd

from intraday.agents import REGISTRY, AgentInput, run_all
from intraday.agents.base import chain_features
from intraday.contracts import Brain
from intraday.intelligence import Scanner, select_instrument
from intraday.options import ist_now
from intraday.options.chain_builder import build_chain
from intraday.tests.test_options_math import _synthetic_payload


def _bars(closes, vols=None, start=24000.0):
    """Ascending 5m bars with a cumulative VWAP column."""
    n = len(closes)
    vols = vols or [10000] * n
    rows = []
    prev = start
    cum_pv = cum_v = 0.0
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = prev
        hi = max(o, c) + 2
        lo = min(o, c) - 2
        tp = (hi + lo + c) / 3.0
        cum_pv += tp * v
        cum_v += v
        rows.append({"time": i, "open": o, "high": hi, "low": lo, "close": c,
                     "volume": v, "prev_close": prev, "vwap": cum_pv / cum_v})
        prev = c
    return pd.DataFrame(rows)


def _bullish_chain(spot=24800.0):
    """A chain with put OI >> call OI (PCR>1, bullish) at a known IV."""
    p = _synthetic_payload(spot=spot, dte=7, iv=0.13, step=100, n=6, lot=50)
    for row in p["chain"]:
        row["PE"]["oi_num"] = 200000        # put-heavy
        row["PE"]["prev_oi"] = 150000       # puts being written (dOI +)
        row["CE"]["oi_num"] = 80000
        row["CE"]["prev_oi"] = 82000
    return build_chain(p)


class FakeContext:
    index_symbol = "NIFTY"

    def __init__(self, bars_map, chain, vix=12.0):
        self._bars = bars_map
        self._chain = chain
        self._vix = vix

    def symbols(self): return ["NIFTY"]
    def bars(self, s): return self._bars.get(s)
    def chain(self, s): return self._chain
    def spot(self, s): return self._chain.spot
    def positioning(self, s): return {"d_pcr": 0.10}
    def vix(self): return self._vix
    def prev_day(self, s): return {"pdh": 24850, "pdl": 24600, "pdc": 24700, "pdo": 24650}

    def now(self):
        # fixed mid-session clock: session context (opening range, elapsed
        # minutes) must not depend on the wall-clock hour the suite runs at.
        from intraday.options.models import IST
        d = dt.datetime(2026, 9, 30, 10, 30, tzinfo=IST)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        return d


def test_all_agents_run_without_raising():
    up = _bars([24000 + i * 25 for i in range(40)])
    inp = AgentInput(symbol="NIFTY", bars=up, index_bars=up, chain=_bullish_chain(),
                     spot=24800.0, now=ist_now(), cfg={"bar_timeframe": "5m"},
                     vix=12.0, prev_day={"pdh": 24850, "pdl": 24600, "pdc": 24700},
                     session={"minutes_since_open": 200, "minutes_to_close": 175,
                              "or_hi": 24100, "or_lo": 23980, "or_minutes": 15,
                              "first_hour_hi": 24300, "first_hour_lo": 23980,
                              "day_open": 24000, "breadth_pct": 65.0})
    results = run_all(inp)
    assert len(results) == len(REGISTRY) == 25
    for r in results:
        assert r.agent and r.family in ("R", "S", "F", "V", "M", "C")
        if r.scored:
            assert 0 <= r.score_buy <= 100 and 0 <= r.score_sell <= 100


def test_chain_features_bullish_pcr_and_walls():
    f = chain_features(_bullish_chain())
    assert f["pcr_oi"] > 1.5                        # put-heavy
    assert f["put_oi"] > f["call_oi"]
    assert f["call_doi"] < 0 and f["put_doi"] > 0   # puts written, calls covered
    assert f["max_pain"] > 0 and f["atm_iv"] > 0


def test_uptrend_fires_buy_and_picks_a_call():
    up = _bars([24000 + i * 25 for i in range(45)])     # steady uptrend
    ctx = FakeContext({"NIFTY": up}, _bullish_chain(), vix=12.0)
    cfg = {"bar_timeframe": "5m", "score_threshold": 55, "score_margin": 5,
           "or_minutes": 15, "session_open": "09:15", "session_close": "15:30"}
    sigs, regime, rows = Scanner(ctx, Brain.equal(), cfg).scan()
    assert regime["on"] is True
    assert len(sigs) == 1
    s = sigs[0]
    assert s.direction == "BUY" and s.instrument["right"] == "CE"
    assert s.instrument["ask"] > 0 and s.instrument["token"]


def test_session_ctx_slices_todays_bars_from_multiday_cache():
    from intraday.intelligence.scanner import _session_ctx
    bars = _bars([24000 + i * 5 for i in range(240)])   # ~3 sessions cached
    cfg = {"bar_timeframe": "5m", "or_minutes": 15,
           "session_open": "09:15", "session_close": "15:30"}
    s = _session_ctx(bars, cfg, None, FakeContext({}, None).now())  # 10:30
    assert s["minutes_since_open"] == 75.0
    assert s["minutes_to_close"] == 300.0
    # today's slice = last 16 bars (75 // 5 + 1); levels come from it alone
    assert s["day_open"] == float(bars["open"].iloc[-16])
    assert s["or_hi"] == float(bars["high"].iloc[-16:-13].max())
    # clockless keeps the legacy full-bars read
    leg = _session_ctx(bars, cfg)
    assert leg["minutes_since_open"] == 240 * 5
    assert leg["day_open"] == float(bars["open"].iloc[0])


def test_sim_market_chains_have_ivs_and_greeks():
    """Sim chains are anchored to sim time: IVs invert, greeks are alive, and
    sessions can roll without the chains decaying into 'expired' (t=0 kills
    IVs → delta 0 → the V/F agents and sizing silently fall back)."""
    from intraday.data.sim import SimContext
    from intraday.options import greeks_for
    ctx = SimContext({"universe": ["NIFTY"], "bar_timeframe": "5m"})
    ch = ctx.chain("NIFTY")
    assert ch is not None and ch.asof == ctx.now() and not ch.expired
    atm = ch.get(ch.atm, True)
    assert atm is not None and (atm.iv or 0) > 0
    g = greeks_for(atm, ch)
    assert 0.3 < g["delta"] < 0.7 and g["gamma"] > 0
    for _ in range(80):                       # roll across sessions
        ctx.step()
    ch2 = ctx.chain("NIFTY")
    assert ch2.asof == ctx.now() and not ch2.expired
    assert (ch2.get(ch2.atm, True).iv or 0) > 0


def test_vix_spike_vetoes_the_session():
    up = _bars([24000 + i * 25 for i in range(45)])
    ctx = FakeContext({"NIFTY": up}, _bullish_chain(), vix=30.0)   # VIX spike
    sigs, regime, rows = Scanner(ctx, Brain.equal(),
                                 {"bar_timeframe": "5m"}).scan()
    assert regime["on"] is False and sigs == []
    assert any("VIX" in v for v in regime["vetoes"])


def test_regime_threshold_is_configurable():
    from intraday.agents.base import AgentResult
    from intraday.intelligence import regime_gate
    rs = [AgentResult(agent="R1", family="R", score_buy=35.0, score_sell=35.0)]
    assert regime_gate(rs)["on"] is False                      # default 40
    assert regime_gate(rs, min_avg=30.0)["on"] is True
    assert regime_gate(rs, min_avg=36.0)["on"] is False
    # threshold also flows through the scanner cfg
    up = _bars([24000 + i * 25 for i in range(45)])
    ctx = FakeContext({"NIFTY": up}, _bullish_chain(), vix=12.0)
    base = {"bar_timeframe": "5m", "score_threshold": 55, "score_margin": 5,
            "or_minutes": 15, "session_open": "09:15", "session_close": "15:30"}
    assert Scanner(ctx, Brain.equal(), dict(base, regime_min_avg=95.0)
                   ).scan()[1]["on"] is False
    assert Scanner(ctx, Brain.equal(), dict(base, regime_min_avg=10.0)
                   ).scan()[1]["on"] is True

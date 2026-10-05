"""Futures-vs-options selector: routing rules + loop integration, offline.

Pure-router tests use light fakes (no broker); the loop tests reuse the
ReplayContext pattern from test_session_loop.py with `futures_available`
opted in, proving the FUT leg flows through the same
scan → size → gate → place → exit path as options.
"""
import datetime as dt
from types import SimpleNamespace

from intraday.contracts import Brain
from intraday.journal.store import Store
from intraday.runtime import SessionLoop
from intraday.selector import build_features, choose, select
from intraday.selector.router import build_futures_instrument
from intraday.tests.test_options_math import _synthetic_payload
from intraday.tests.test_session_loop import _bars, _chain, _weekday


# ---------- fakes ----------

def _quote(spread_pct=0.8, oi=5000, volume=20000, iv=0.14, ask=55.0, bid=54.0,
           delta=0.5):
    return SimpleNamespace(spread_pct=spread_pct, oi=oi, volume=volume, iv=iv,
                           ask=ask, bid=bid, ltp=54.5, delta=delta)


def _chainlike(spot=24800.0, dte=7, lot=50):
    return SimpleNamespace(spot=spot, lot_size=lot,
                           days_to_expiry=dte,
                           expiry=(dt.date(2026, 10, 8)))


def _sig(symbol="NIFTY", direction="BUY", fam=None, regime=None):
    s = SimpleNamespace(symbol=symbol, direction=direction,
                        family_scores=fam or {"M": 55.0},
                        regime=regime or {"on": True, "scalar": 1.0})
    return s


_QUOTE_KEYS = {"spread_pct", "oi", "volume", "iv", "ask", "bid", "delta"}


def _feats(dte=7, **kw):
    # quote-level overrides (spread/oi/...) reshape the fake quote; everything
    # else overrides the build_features defaults (session/family/atr/...).
    qkw = {k: kw.pop(k) for k in list(kw) if k in _QUOTE_KEYS}
    params = dict(symbol="NIFTY", direction="BUY",
                  family_scores={"M": 55.0},
                  regime={"on": True, "scalar": 1.0},
                  session={"minutes_to_close": 200},
                  atr_pts=60.0, futures_available=True)
    params.update(kw)
    return build_features(chain=_chainlike(dte=dte), quote=_quote(**qkw),
                          greeks={"delta": 0.5, "theta": -3.0}, **params)


# ---------- router rules ----------

def test_liquid_cheap_option_stays_opt():
    # cheap premium (breakeven 20pts) + no trend + small expected move → OPT
    f = build_features(symbol="NIFTY", direction="BUY", chain=_chainlike(),
                       quote=_quote(ask=10.0, bid=9.8, oi=5000, iv=0.12),
                       greeks={"delta": 0.5, "theta": -1.0},
                       family_scores={"M": 52.0},
                       regime={"on": True, "scalar": 1.0},
                       session={"minutes_to_close": 200},
                       iv_percentile=0.2, atr_pts=60.0, futures_available=True)
    d = choose(f, {})
    assert d.kind == "OPT", d.reason


def test_wide_spread_routes_fut():
    f = _feats(spread_pct=8.0)
    d = choose(f, {})
    assert d.kind == "FUT" and "spread" in d.reason


def test_thin_oi_routes_fut():
    f = _feats(oi=10)
    d = choose(f, {})
    assert d.kind == "FUT" and "OI" in d.reason


def test_high_iv_routes_fut():
    f = _feats(iv_percentile=0.9)
    d = choose(f, {})
    assert d.kind == "FUT" and "IV" in d.reason


def test_late_session_routes_fut():
    f = _feats(dte=0, session={"minutes_to_close": 30})
    d = choose(f, {})
    assert d.kind == "FUT" and "close" in d.reason


def test_strong_trend_routes_fut():
    f = _feats(family_scores={"M": 90.0}, atr_pts=40.0)
    d = choose(f, {})
    assert d.kind == "FUT" and "trend" in d.reason


def test_event_blackout_skips():
    f = _feats(event_blackout=True)
    d = choose(f, {})
    assert d.kind == "SKIP"


def test_no_futures_config_stays_opt():
    f = _feats(futures_available=False)
    d = choose(f, {})
    assert d.kind == "OPT"


def test_selector_disabled_stays_opt():
    f = _feats(spread_pct=9.0)
    d = choose(f, {"selector_enabled": False})
    assert d.kind == "OPT"


def test_no_opt_leg_routes_fut():
    f = build_features(symbol="NIFTY", direction="BUY", chain=_chainlike(),
                       quote=None, session={"minutes_to_close": 200},
                       futures_available=True)
    d = choose(f, {})
    assert d.kind == "FUT"


def test_build_futures_instrument_shape():
    inst = build_futures_instrument(symbol="NIFTY", direction="BUY", spot=24810.0,
                                    lot_size=50)
    assert inst["kind"] == "FUT" and inst["right"] == "FUT"
    assert inst["entry_prem"] == 24810.0 and inst["delta"] == 1.0


def test_select_returns_fut_leg_with_reason():
    sig = _sig()
    inst, d = select(signal=sig, chain=_chainlike(), quote=_quote(spread_pct=9.0),
                     greeks={"delta": 0.5}, opt_leg={"tsym": "X", "strike": 24800,
                                                     "right": "CE", "ask": 55.0},
                     session={"minutes_to_close": 200}, atr_pts=60.0,
                     cfg={"futures_available": True})
    assert d.kind == "FUT" and inst is not None and inst["kind"] == "FUT"
    assert "selector_reason" in inst


# ---------- loop integration ----------

class FutReplayContext:
    """ReplayContext twin with a futures quote wired (ctx.future)."""
    index_symbol = "NIFTY"

    def __init__(self):
        self._now = _weekday(10, 0)
        self._spot = 24800.0
        self._square = False
        self._bars_df = _bars(self._spot, "up")
        self._chain_obj = _chain(self._spot)

    def symbols(self): return ["NIFTY"]
    def bars(self, s): return self._bars_df
    def chain(self, s): return self._chain_obj
    def spot(self, s): return self._spot
    def positioning(self, s): return {"d_pcr": 0.10}
    def vix(self): return 12.0
    def prev_day(self, s): return {"pdh": 24700, "pdl": 24400, "pdc": 24500, "pdo": 24450}
    def now(self): return self._now
    def is_square_off(self, now=None): return self._square
    def future(self, s): return {"px": self._spot + 5.0, "lot_size": 50,
                                 "exch": "NFO", "token": "FUT1", "tsym": "NIFTY-FUT"}


FUTCFG = {"bar_timeframe": "5m", "score_threshold": 55, "score_margin": 5,
          "or_minutes": 15, "session_open": "09:15", "session_close": "15:30",
          "no_new_entries_after": "15:00", "square_off_time": "15:15",
          "equity_rupees": 300000.0, "risk_per_trade_pct": 1.0,
          "max_daily_loss_rupees": 6000.0, "max_positions": 4, "max_lots_per_symbol": 10,
          "max_prem_loss_pct": 40, "target_r_1": 1.0, "target_r_2": 2.0,
          "futures_available": True, "selector_max_spread_pct": 0.01,
          "paper_exec_delay_s": 0}


def test_loop_opens_fut_position_when_spread_wide(tmp_path):
    store = Store(tmp_path / "fut.db")
    ctx = FutReplayContext()
    loop = SessionLoop(ctx, store, FUTCFG, brain=Brain.equal())
    st = loop.step()
    assert st["entries"] == 1 and st["positions"] == 1
    pos = loop.positions[0]
    assert pos.right == "FUT" and pos.delta_at_entry == 1.0 and pos.qty > 0
    # unrealized tracks the futures mark
    ctx._spot = 24900.0
    assert loop._unrealized(ctx.now()) > 0
    store.close()


def test_loop_legacy_ctx_still_opens_ce(tmp_path):
    """Without futures configured the loop behaves exactly as before (OPT)."""
    from intraday.tests.test_session_loop import ReplayContext, CFG
    store = Store(tmp_path / "leg.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    st = loop.step()
    assert st["entries"] == 1
    assert loop.positions[0].right == "CE"
    store.close()


def _fut_sig(ctx, direction):
    from intraday.contracts import Signal
    return Signal(ts=ctx.now(), symbol="NIFTY", direction=direction,
                  score_buy=75.0, score_sell=10.0,
                  family_scores={"M": 80.0}, agent_rows=[], vetoes=[],
                  n_scored=5, regime={"on": True, "scalar": 1.0},
                  instrument=build_futures_instrument(
                      symbol="NIFTY", direction=direction, spot=ctx.spot("NIFTY"),
                      lot_size=50, exch="NFO", token="FUT1", tsym="NIFTY-FUT"))


def test_loop_fut_entry_takes_signal_side(tmp_path):
    """FUT legs are linear: a SELL view SELLS futures (options keep BUYing the
    put leg). The old code bought futures for both directions."""
    store = Store(tmp_path / "side.db")
    loop = SessionLoop(FutReplayContext(), store, FUTCFG, brain=Brain.equal())
    assert loop._enter(_fut_sig(loop.ctx, "SELL"), {"scalar": 1.0}, loop.ctx.now())
    pos = loop.positions[0]
    assert (pos.right, pos.side) == ("FUT", "SELL")
    assert pos.stop > pos.entry_spot          # bearish stop sits above entry
    o = store.one("SELECT side FROM orders ORDER BY id DESC LIMIT 1")
    assert o["side"] == "SELL"
    store.close()


def test_loop_long_fut_survives_first_manage(tmp_path):
    """A long FUT above its stop is still open after the next step (the old
    exit read treated every FUT leg as bearish and I1-exited it at once)."""
    store = Store(tmp_path / "hold.db")
    ctx = FutReplayContext()
    loop = SessionLoop(ctx, store, FUTCFG, brain=Brain.equal())
    assert loop._enter(_fut_sig(ctx, "BUY"), {"scalar": 1.0}, ctx.now())
    st = loop.step()                          # manage (hold) + scan (dup-gated)
    assert st["positions"] == 1 and st["entries"] == 0
    assert loop.positions[0].side == "BUY"
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 0  # no I1 churn
    store.close()

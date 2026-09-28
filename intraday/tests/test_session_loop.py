"""Full intraday session loop, offline (PaperBroker + a scripted ReplayContext):

  • an uptrend + bullish chain fires a BUY that opens a CE position and journals
    a signal, a latency-stamped order, and a position row
  • a large favourable move triggers an I3 target EXIT with a positive trade P&L
  • the mandatory square-off (I0) flattens any open position
  • the daily-loss kill halts trading and flattens

Same code path the live/paper engine runs; only the context + broker differ.
"""
import datetime as dt

import pandas as pd

from intraday.contracts import Brain
from intraday.journal.store import Store
from intraday.options.chain_builder import build_chain
from intraday.options.models import IST
from intraday.runtime import SessionLoop
from intraday.tests.test_options_math import _synthetic_payload


def _weekday(h, m):
    d = dt.datetime(2026, 9, 30, h, m, tzinfo=IST)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _bars(spot, trend="up", n=45):
    rows = []
    if trend == "up":
        closes = [spot - (n - i) * 20 for i in range(n)]     # rising into spot
    else:
        closes = [spot + ((-1) ** i) * 5 for i in range(n)]  # flat/chop around spot
    prev = closes[0]
    cum_pv = cum_v = 0.0
    for i, c in enumerate(closes):
        o = prev; hi = max(o, c) + 3; lo = min(o, c) - 3; v = 12000
        tp = (hi + lo + c) / 3.0; cum_pv += tp * v; cum_v += v
        rows.append({"time": i, "open": o, "high": hi, "low": lo, "close": c,
                     "volume": v, "prev_close": prev, "vwap": cum_pv / cum_v})
        prev = c
    return pd.DataFrame(rows)


def _chain(spot):
    p = _synthetic_payload(spot=spot, dte=7, iv=0.14, step=100, n=6, lot=50)
    for row in p["chain"]:            # bullish: put-heavy, puts being written
        row["PE"]["oi_num"] = 200000; row["PE"]["prev_oi"] = 150000
        row["CE"]["oi_num"] = 80000;  row["CE"]["prev_oi"] = 82000
    return build_chain(p)


class ReplayContext:
    index_symbol = "NIFTY"

    def __init__(self):
        self._now = _weekday(10, 0)
        self._spot = 24800.0
        self._square = False
        self._trend = "up"
        self._rebuild()

    def _rebuild(self):
        self._bars_df = _bars(self._spot, self._trend)
        self._chain_obj = _chain(self._spot)

    def set(self, *, now=None, spot=None, square=None, trend=None):
        if now is not None: self._now = now
        if spot is not None: self._spot = spot
        if square is not None: self._square = square
        if trend is not None: self._trend = trend
        self._rebuild()

    def symbols(self): return ["NIFTY"]
    def bars(self, s): return self._bars_df
    def chain(self, s): return self._chain_obj
    def spot(self, s): return self._spot
    def positioning(self, s): return {"d_pcr": 0.10}
    def vix(self): return 12.0
    def prev_day(self, s): return {"pdh": 24700, "pdl": 24400, "pdc": 24500, "pdo": 24450}
    def now(self): return self._now
    def is_square_off(self, now=None): return self._square


CFG = {"bar_timeframe": "5m", "score_threshold": 55, "score_margin": 5,
       "or_minutes": 15, "session_open": "09:15", "session_close": "15:30",
       "no_new_entries_after": "15:00", "square_off_time": "15:15",
       "equity_rupees": 300000.0, "risk_per_trade_pct": 1.0,
       "max_daily_loss_rupees": 6000.0, "max_positions": 4, "max_lots_per_symbol": 10,
       "max_prem_loss_pct": 40, "target_r_1": 1.0, "target_r_2": 2.0}


def test_entry_then_target_exit(tmp_path):
    store = Store(tmp_path / "loop.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())

    st = loop.step()                              # uptrend -> BUY a CE
    assert st["entries"] == 1 and st["positions"] == 1
    pos = loop.positions[0]
    assert pos.right == "CE" and pos.qty > 0
    # order journalled with a latency stamp; signal + position rows exist
    o = store.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
    assert o["status"] == "FILLED" and o["latency_ms"] is not None
    assert store.one("SELECT COUNT(*) n FROM signals")["n"] >= 1
    assert store.one("SELECT COUNT(*) n FROM positions WHERE status='OPEN'")["n"] == 1

    # big favourable move (spot +400), no new signals (flat bars, 15:05 cutoff)
    ctx.set(now=_weekday(15, 5), spot=25200.0, trend="flat")
    st2 = loop.step()
    assert st2["positions"] == 0                   # target/exit closed it
    tr = store.one("SELECT * FROM trades ORDER BY id DESC LIMIT 1")
    assert tr is not None and tr["pnl"] > 0 and "I" in (tr["exit_reason"] or "")
    store.close()


def test_square_off_flattens(tmp_path):
    store = Store(tmp_path / "sq.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    # trip the mandatory square-off
    ctx.set(now=_weekday(15, 16), square=True, trend="flat")
    st = loop.step()
    assert st["positions"] == 0
    tr = store.one("SELECT * FROM trades ORDER BY id DESC LIMIT 1")
    assert "I0" in (tr["exit_reason"] or "") and "square" in (tr["exit_reason"] or "").lower()
    store.close()


def test_daily_loss_halts(tmp_path):
    store = Store(tmp_path / "dl.db")
    cfg = dict(CFG, max_daily_loss_rupees=500.0)   # tiny limit
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, cfg, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    # sharp adverse move: stop hit -> realized loss > limit -> halt
    ctx.set(now=_weekday(11, 0), spot=24300.0, trend="flat")
    st = loop.step()
    assert st["halted"] is True and st["positions"] == 0
    # a subsequent step opens nothing (halted)
    ctx.set(now=_weekday(11, 5), spot=24800.0, trend="up")
    assert loop.step()["entries"] == 0
    store.close()

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
       "max_prem_loss_pct": 40, "target_r_1": 1.0, "target_r_2": 2.0,
       "paper_exec_delay_s": 0}                # keep the suite fast (delay covered at broker level)


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


def test_activity_feed_records_scan(tmp_path):
    store = Store(tmp_path / "act.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    assert list(loop.activity) == []
    loop.step()
    stages = [e["stage"] for e in loop.activity]
    assert "scan" in stages
    ev = next(e for e in loop.activity if e["stage"] == "scan")
    assert set(ev) == {"ts", "stage", "msg"} and "symbols" in ev["msg"]
    # per-symbol fetch state is exposed for the UI
    assert {"symbol", "bars", "chain"} <= set(loop.scanner.progress[0])
    # identical consecutive scans are logged once (plus a 60 s heartbeat)
    loop.step()
    assert sum(1 for e in loop.activity if e["stage"] == "scan") == 1
    store.close()


def test_restore_recovers_open_positions_after_restart(tmp_path):
    store = Store(tmp_path / "re.db")
    loop = SessionLoop(ReplayContext(), store, CFG, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    saved = loop.positions[0]
    # a fresh loop (restart) starts blind, then restores from the journal
    loop2 = SessionLoop(ReplayContext(), store, CFG, brain=Brain.equal())
    assert loop2.positions == []
    out = loop2.restore_open_positions()
    assert len(out) == 1
    p = loop2.positions[0]
    assert (p.symbol, p.qty, p.side) == (saved.symbol, saved.qty, saved.side)
    assert p.stop == saved.stop and p.position_id == saved.position_id
    assert p.entry_ts == saved.entry_ts and p.strategy == saved.strategy
    assert loop2.restore_open_positions() == []      # idempotent
    assert len(loop2.positions) == 1
    store.close()


def test_reconcile_closes_ghosts_and_adopts_unknowns(tmp_path):
    store = Store(tmp_path / "rec.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    n_orders = store.one("SELECT COUNT(*) n FROM orders")["n"]

    # broker is flat: the journal row is closed WITHOUT placing an exit order
    # (exiting a ghost would open a reversed position at the broker)
    s = loop.reconcile_with_broker([], now=ctx.now())
    assert s["closed_ghosts"] == 1 and loop.positions == []
    assert store.one("SELECT COUNT(*) n FROM orders")["n"] == n_orders
    tr = store.one("SELECT * FROM trades ORDER BY id DESC LIMIT 1")
    assert tr is not None and "reconcile" in (tr["exit_reason"] or "")

    # broker holds what the journal never saw: adopt it as tracked
    s = loop.reconcile_with_broker(
        [{"tsym": "NIFTY02OCT25P24800", "exch": "NFO", "prd": "I",
          "netqty": "-50", "buyavgprc": "0", "sellavgprc": "120.0",
          "lp": "118.0", "lotsize": "50", "token": "T1"}], now=ctx.now())
    assert s["adopted"] == 1 and len(loop.positions) == 1
    p = loop.positions[0]
    assert (p.side, p.qty, p.strategy) == ("SELL", 50, "adopted")
    assert (p.right, p.strike, p.underlying) == ("PE", 24800.0, "NIFTY")
    assert p.stop == 0.0                              # no invented option stop
    assert p.risk_per_share > 0
    # non-MIS rows belong to another product: ignored, never adopted
    s = loop.reconcile_with_broker(
        [{"tsym": "NIFTY02OCT25C24800", "exch": "NFO", "prd": "M",
          "netqty": "50", "buyavgprc": "100", "sellavgprc": "0",
          "lp": "100", "lotsize": "50", "token": "T2"}], now=ctx.now())
    assert s["ignored_non_mis"] == 1 and s["adopted"] == 0
    store.close()


def test_reconcile_adopts_broker_qty_as_truth(tmp_path):
    store = Store(tmp_path / "rq.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    tsym = loop.positions[0].symbol
    live_qty = loop.positions[0].qty * 2    # broker holds more than journal
    s = loop.reconcile_with_broker(
        [{"tsym": tsym, "exch": "NFO", "prd": "I", "netqty": str(live_qty),
          "buyavgprc": "90.0", "sellavgprc": "0", "lp": "95.0",
          "lotsize": "50", "token": "T9"}], now=ctx.now())
    assert s["matched"] == 1 and s["qty_adjusted"] == 1
    assert loop.positions[0].qty == live_qty  # exits must match live size
    store.close()


def test_failed_exit_keeps_position_managed(tmp_path):
    from intraday.brokers import PaperBroker

    class _FailBroker:
        name = "failing"

        def place(self, intent):
            return {"broker_order_id": "", "status": "ERROR",
                    "reason": "broker down"}

        def cancel(self, oid):
            return False

        def positions(self):
            return []

    store = Store(tmp_path / "fx.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    # broker dies and square-off hits: the close is NOT journalled, the
    # position stays managed and retries instead of going orphan
    loop.set_broker(_FailBroker())
    ctx.set(now=_weekday(15, 16), square=True, trend="flat")
    st = loop.step()
    assert st["positions"] == 1
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 0
    assert any("holding for retry" in m for m in loop.positions[0].log)
    # broker recovers: the retry closes it for real
    loop.set_broker(PaperBroker(0.10))
    assert loop.step()["positions"] == 0
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 1
    store.close()


def test_halt_resets_on_new_trading_day(tmp_path):
    store = Store(tmp_path / "ro.db")
    cfg = dict(CFG, max_daily_loss_rupees=500.0)
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, cfg, brain=Brain.equal())
    assert loop.step()["positions"] == 1
    ctx.set(now=_weekday(11, 0), spot=24300.0, trend="flat")
    assert loop.step()["halted"] is True
    # next session: the halt is gone (a server must not stay dead tomorrow)
    nxt = ctx.now() + dt.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    ctx.set(now=nxt.replace(hour=10, minute=0), spot=24800.0, trend="up")
    st = loop.step()
    assert st["halted"] is False
    assert any(e["stage"] == "session" for e in loop.activity)
    store.close()


def test_signal_view_carries_full_card(tmp_path):
    store = Store(tmp_path / "sv.db")
    loop = SessionLoop(ReplayContext(), store, CFG, brain=Brain.equal())
    assert loop.step()["entries"] == 1
    views = loop.last_scan["signals"]
    assert len(views) == 1
    v = views[0]
    for k in ("ts", "symbol", "direction", "score_buy", "score_sell",
              "composite", "margin", "scan_ms", "family_scores", "vetoes",
              "n_scored", "regime", "brain_version", "instrument", "agents"):
        assert k in v, k
    assert v["composite"] == max(v["score_buy"], v["score_sell"])
    assert abs(v["margin"] - abs(v["score_buy"] - v["score_sell"])) < 0.11
    assert v["scan_ms"] is not None and v["scan_ms"] >= 0
    assert v["regime"]["on"] is True and v["n_scored"] > 0
    assert v["n_scored"] <= len(v["agents"]) > 0
    assert all(a.get("agent") and a.get("family") for a in v["agents"])
    assert v["instrument"]["right"] == "CE"
    assert loop.last_scan["scan_ms"] == v["scan_ms"]
    store.close()


def test_scan_runs_at_cadence_not_every_tick(tmp_path):
    store = Store(tmp_path / "cg.db")
    ctx = ReplayContext()                       # frozen clock
    loop = SessionLoop(ctx, store, dict(CFG, scan_every_seconds=30),
                       brain=Brain.equal())
    calls = []
    orig = loop.scanner.scan
    loop.scanner.scan = lambda: (calls.append(1), orig())[1]
    loop.step()
    loop.step()                                 # same `now`: scan skipped
    assert len(calls) == 1
    assert loop.last_scan and loop.last_scan["signals"] is not None
    ctx.set(now=_weekday(10, 1))                # +60 s: cadence due again
    loop.step()
    assert len(calls) == 2
    store.close()


def test_regime_off_still_fetches_stock_data(tmp_path, monkeypatch):
    import intraday.intelligence.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "regime_gate",
                        lambda r, min_avg=40.0: {"on": False, "scalar": 0.0,
                                                "vetoes": [], "avg": 30.0,
                                                "detail": "regime avg 30"})
    store = Store(tmp_path / "actoff.db")
    loop = SessionLoop(ReplayContext(), store, CFG, brain=Brain.equal())
    loop.step()
    assert loop.last_scan["regime"]["on"] is False
    assert loop.last_scan["signals"] == []
    # bars + chains are still fetched for every candidate while gated
    assert len(loop.scanner.progress) >= 2
    assert all({"symbol", "bars", "chain"} <= set(p)
               for p in loop.scanner.progress)
    ev = next(e for e in loop.activity if e["stage"] == "scan")
    assert "regime OFF" in ev["msg"] and "symbols" in ev["msg"]
    store.close()


class _HalfFillBroker:
    """Fills entries via paper; exits fill exactly half (live partial fill)."""

    def __init__(self, exit_status="WORKING"):
        from intraday.brokers import PaperBroker
        self.name = "half-fill"
        self._paper = PaperBroker(0.10)
        self._exit_status = exit_status

    def place(self, intent):
        if str(intent.reason or "").startswith("exit:"):
            half = max(0, int(intent.qty) // 2)
            return {"broker_order_id": "LIVE-1", "status": self._exit_status,
                    "fill_px": intent.limit_px or 100.0, "filled_qty": half}
        return self._paper.place(intent)

    def cancel(self, oid):
        return False

    def positions(self):
        return []


def _open_one(store, ctx, loop):
    assert loop.step()["positions"] == 1
    return loop.positions[0].qty


def test_exit_working_partial_books_filled_and_holds_rest(tmp_path):
    store = Store(tmp_path / "wp.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    full = _open_one(store, ctx, loop)
    loop.set_broker(_HalfFillBroker("WORKING"))
    ctx.set(now=_weekday(15, 16), square=True, trend="flat")
    assert loop.step()["positions"] == 1          # NOT closed
    assert loop.positions[0].qty == full - full // 2
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 1  # partial booked
    assert any("partial fill" in e["msg"] for e in loop.activity)
    store.close()


def test_exit_filled_partial_qty_books_rest_not_full(tmp_path):
    store = Store(tmp_path / "fp.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    full = _open_one(store, ctx, loop)
    loop.set_broker(_HalfFillBroker("FILLED"))
    ctx.set(now=_weekday(15, 16), square=True, trend="flat")
    assert loop.step()["positions"] == 1
    row = store.one("SELECT qty FROM trades")
    assert row["qty"] == full // 2                # only filled shares booked
    store.close()


def test_failed_entry_placement_is_logged(tmp_path):
    class _RejectBroker(_HalfFillBroker):
        def place(self, intent):
            return {"broker_order_id": "", "status": "REJECTED",
                    "reason": "RMS: margin short"}

    store = Store(tmp_path / "re.db")
    loop = SessionLoop(ReplayContext(), store, CFG, brain=Brain.equal())
    loop.set_broker(_RejectBroker())
    assert loop.step()["positions"] == 0
    assert any("not placed" in e["msg"] and "REJECTED" in e["msg"]
               for e in loop.activity)
    store.close()


def test_flatten_retries_and_accounts_partials(tmp_path):
    from intraday.brokers import PaperBroker

    class _Flaky:
        name = "flaky"

        def __init__(self):
            self._paper = PaperBroker(0.10)

        def place(self, intent):
            if str(intent.reason or "").startswith("entry:"):
                return self._paper.place(intent)
            half = max(0, int(intent.qty) // 2)
            return {"broker_order_id": "LIVE-9", "status": "WORKING",
                    "fill_px": intent.limit_px or 100.0, "filled_qty": half}

        def cancel(self, oid):
            return False

        def positions(self):
            return []

    store = Store(tmp_path / "fl.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, CFG, brain=Brain.equal())
    full = _open_one(store, ctx, loop)
    ctx.set(now=_weekday(15, 16), square=True, trend="flat")  # no new entries
    loop.set_broker(_Flaky())
    loop.request_flatten("test kill")
    assert loop.step()["positions"] == 1
    # one tick books TWO partials (flatten 100→50, then the I0 exit 50→25):
    # every confirmed share is journalled, never the unfilled rest
    assert loop.positions[0].qty == 25
    assert loop._flatten_request is True                # re-armed for retry
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 2
    loop.set_broker(PaperBroker(0.10))
    assert loop.step()["positions"] == 0                # retry completed it
    assert store.one("SELECT COUNT(*) n FROM trades")["n"] == 3
    assert loop._flatten_request is False
    store.close()

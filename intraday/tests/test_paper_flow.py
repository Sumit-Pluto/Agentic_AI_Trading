"""End-to-end Phase-1 paper flow (offline, no broker):

  gateway payload -> Chain(+greeks) -> Signal -> OrderIntent -> PaperBroker fill
  -> journal (signal, order, position) -> close -> trade row with correct P&L.

Proves the spine + single-code-path broker + Trade DB all fit together.
"""
import datetime as dt

from intraday.brokers import PaperBroker
from intraday.contracts import OrderIntent, Position, Signal
from intraday.journal.store import Store
from intraday.options import greeks_for, ist_now
from intraday.options.chain_builder import build_chain
from intraday.tests.test_options_math import _synthetic_payload


def test_paper_entry_and_exit_end_to_end(tmp_path):
    store = Store(tmp_path / "intraday_test.db")
    ch = build_chain(_synthetic_payload(spot=24800.0, iv=0.14, lot=50))

    # 1. pick the ATM call, price it, greeks
    leg = ch.get(ch.atm, True)
    g = greeks_for(leg, ch)
    now = ist_now()

    # 2. signal
    sig = Signal(ts=now, symbol="NIFTY", direction="BUY",
                 score_buy=72.0, score_sell=10.0,
                 family_scores={"S": 70, "F": 65, "V": 55, "M": 80, "C": 50},
                 agent_rows=[{"agent": "ORB", "family": "S", "score_buy": 70,
                              "score_sell": 0, "na": False, "veto": False}],
                 vetoes=[], n_scored=1,
                 regime={"on": True, "scalar": 1.0, "detail": "trend", "vetoes": []},
                 instrument={"token": leg.token, "tsym": leg.tsym, "exch": "NFO",
                             "strike": leg.strike, "right": "CE",
                             "expiry": ch.expiry.isoformat(), "lot_size": ch.lot_size,
                             "entry_prem": leg.ask})
    sid = store.save_signal(sig)
    assert sid > 0

    # 3. order intent -> PaperBroker (marketable buy at the ask)
    qty = ch.lot_size                       # 1 lot
    intent = OrderIntent(symbol=leg.tsym, side="BUY", qty=qty,
                         order_type="MARKETABLE_LIMIT", limit_px=leg.ask,
                         reason="entry:signal", signal_id=sid, exch="NFO",
                         token=leg.token, strike=leg.strike, right="CE",
                         expiry=ch.expiry, lot_size=ch.lot_size, strategy="ORB")
    broker = PaperBroker(slippage_pct=0.10)
    ack = broker.place(intent)
    assert ack["status"] == "FILLED"
    assert ack["fill_px"] >= leg.ask        # slippage hurts a buy

    oid = store.save_order(intent, status="FILLED", broker=broker.name,
                           date=now.date(), broker_order_id=ack["broker_order_id"])
    store.fill_order(oid, ack["fill_px"])
    store.mark_signal_acted(sid)

    # 4. open position (underlying stop 30 pts below entry spot)
    entry_spot = ch.spot
    stop_spot = entry_spot - 30.0
    pos = Position(symbol=leg.tsym, qty=qty, side="BUY", entry_px=ack["fill_px"],
                   entry_ts=now, stop=stop_spot,
                   risk_per_share=max(leg.ask - leg.bid, 1.0),
                   strike=leg.strike, right="CE", expiry=ch.expiry,
                   lot_size=ch.lot_size, exch="NFO", token=leg.token,
                   underlying="NIFTY", entry_spot=entry_spot,
                   delta_at_entry=g["delta"], strategy="ORB")
    pid = store.open_position(pos)
    pos.position_id = pid
    assert len(store.open_positions()) == 1

    # 5. exit: premium rises 20% -> book the win
    pos.exit_px = round(ack["fill_px"] * 1.20, 2)
    pos.exit_ts = now + dt.timedelta(minutes=25)
    pos.exit_reason = "I3 target +1R"
    pos.age_bars = 5
    tid = store.close_position(pos)
    assert tid > 0
    assert len(store.open_positions()) == 0

    # 6. trade row: long P&L = (exit-entry)*qty, > 0
    tr = store.one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["side"] == "BUY" and tr["qty"] == qty
    expected_pnl = (pos.exit_px - ack["fill_px"]) * qty
    assert abs(tr["pnl"] - expected_pnl) < 1e-6 and tr["pnl"] > 0
    assert tr["exit_reason"] == "I3 target +1R" and tr["strategy"] == "ORB"

    # realized-pnl accessor (feeds the daily-loss kill switch in Phase 2)
    assert abs(store.realized_pnl_today(now.date()) - expected_pnl) < 1e-6
    store.close()


def test_paper_short_pnl_sign(tmp_path):
    """A SELL (short premium) that we buy back cheaper is a profit."""
    store = Store(tmp_path / "s.db")
    now = ist_now()
    pos = Position(symbol="NIFTY24800PE", qty=50, side="SELL", entry_px=100.0,
                   entry_ts=now, stop=0.0, risk_per_share=20.0, strike=24800,
                   right="PE", lot_size=50, underlying="NIFTY", entry_spot=24800.0)
    pos.position_id = store.open_position(pos)
    pos.exit_px = 60.0                       # bought back cheaper
    pos.exit_ts = now + dt.timedelta(minutes=10)
    pos.exit_reason = "I3 target"
    tid = store.close_position(pos)
    tr = store.one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["pnl"] == (100.0 - 60.0) * 50   # short profit = entry - exit
    store.close()


def test_paper_fills_at_live_touch_like_real_execution():
    """With a quote provider the paper broker re-prices at execution time:
    buys pay the live ask, sells take the live bid (spread always paid),
    then slippage hurts. A stale decision price is ignored."""
    touch = {"NIFTY24800CE": (99.0, 101.0)}
    b = PaperBroker(slippage_pct=0.10,
                    quote_provider=lambda i: touch.get(i.symbol))
    buy = OrderIntent(symbol="NIFTY24800CE", side="BUY", qty=50,
                      limit_px=60.0)                    # stale decision price
    r = b.place(buy)
    assert r["status"] == "FILLED"
    assert r["fill_px"] == round(101.0 * 1.001, 2)      # live ask + slippage
    sell = OrderIntent(symbol="NIFTY24800CE", side="SELL", qty=50,
                       limit_px=150.0)
    r = b.place(sell)
    assert r["fill_px"] == round(99.0 * 0.999, 2)       # live bid - slippage


def test_paper_falls_back_to_decision_price():
    """No touch (no provider / error / garbage / one-sided book) → fill at
    the decision price instead of failing. Never invent a price."""
    def _buy(broker):
        return broker.place(OrderIntent(symbol="X", side="BUY", qty=10,
                                        limit_px=100.0))

    assert _buy(PaperBroker(0.10))["fill_px"] == round(100.0 * 1.001, 2)

    def _boom(intent):
        raise RuntimeError("quoter down")
    assert _buy(PaperBroker(0.10, quote_provider=_boom))["fill_px"] == \
        round(100.0 * 1.001, 2)
    assert _buy(PaperBroker(0.10, quote_provider=lambda i: ("xx", None)))["fill_px"] == \
        round(100.0 * 1.001, 2)
    assert _buy(PaperBroker(0.10, quote_provider=lambda i: (99.0, 0.0)))["fill_px"] == \
        round(100.0 * 1.001, 2)                          # no ask → limit
    # nothing at all to price from → honest reject, not a zero fill
    r = PaperBroker(0.10).place(OrderIntent(symbol="X", side="BUY", qty=10))
    assert r["status"] == "REJECTED"


def test_paper_exec_delay_ages_the_fill():
    import time as _t
    b = PaperBroker(0.10, exec_delay_s=0.05)
    t0 = _t.perf_counter()
    r = b.place(OrderIntent(symbol="X", side="BUY", qty=10, limit_px=100.0))
    assert r["status"] == "FILLED" and _t.perf_counter() - t0 >= 0.05


def test_loop_wires_live_touch_into_paper(tmp_path):
    """End of the wire: a loop-driven paper broker ignores a stale decision
    price and fills at the chain touch; opting out restores limit fills."""
    from intraday.contracts import Brain
    from intraday.journal.store import Store
    from intraday.runtime import SessionLoop
    from intraday.tests.test_selector import FutReplayContext
    from intraday.tests.test_session_loop import CFG, ReplayContext

    store = Store(tmp_path / "w.db")
    ctx = ReplayContext()
    loop = SessionLoop(ctx, store, dict(CFG, paper_exec_delay_s=0),
                       brain=Brain.equal())
    ch = ctx.chain("NIFTY")
    leg = ch.get(ch.atm, True)
    stale = OrderIntent(symbol=leg.tsym, side="BUY", qty=50, limit_px=1.0,
                        underlying="NIFTY", strike=leg.strike, right="CE")
    r = loop.broker.place(stale)
    assert r["fill_px"] > 50.0                    # live ask, not the 1.0 stale
    assert abs(r["fill_px"] - round(leg.ask * 1.001, 2)) < 0.02

    off = SessionLoop(ctx, store, dict(CFG, paper_exec_delay_s=0,
                                       paper_use_live_touch=False),
                      brain=Brain.equal())
    r2 = off.broker.place(stale)
    assert r2["fill_px"] == round(1.0 * 1.001, 2)  # opted out → limit fill

    # futures legs quote at the futures mark
    floop = SessionLoop(FutReplayContext(), store, dict(CFG, paper_exec_delay_s=0),
                        brain=Brain.equal())
    fut = OrderIntent(symbol="NIFTY-FUT", side="SELL", qty=50, limit_px=1.0,
                      underlying="NIFTY", right="FUT")
    rf = floop.broker.place(fut)
    assert abs(rf["fill_px"] - round(24805.0 * 0.999, 2)) < 0.02
    store.close()


def test_execution_quality_stats(tmp_path):
    """Decision-vs-fill deviation: buys paying up and sells giving down both
    read as positive cost bps; unfilled orders never enter the stats."""
    store = Store(tmp_path / "q.db")
    now = ist_now()
    b = OrderIntent(symbol="A", side="BUY", qty=10, limit_px=100.0)
    s = OrderIntent(symbol="A", side="SELL", qty=10, limit_px=100.0)
    o1 = store.save_order(b, status="FILLED", broker="paper", date=now.date())
    store.fill_order(o1, 101.0)                       # +100 bps cost
    o2 = store.save_order(s, status="FILLED", broker="paper", date=now.date())
    store.fill_order(o2, 99.0)                        # +100 bps cost
    store.save_order(b, status="BLOCKED:halt", broker="paper", date=now.date())
    q = store.execution_quality()
    assert q["fills"] == 2
    assert q["avg_cost_bps"] == 100.0
    assert q["avg_cost_bps_buy"] == 100.0 and q["avg_cost_bps_sell"] == 100.0
    assert q["max_cost_bps"] == 100.0
    assert q["avg_fill_delay_ms"] is not None and q["avg_fill_delay_ms"] >= 0

    empty = Store(tmp_path / "e.db")
    q0 = empty.execution_quality()
    assert q0["fills"] == 0 and q0["avg_cost_bps"] is None
    store.close(); empty.close()


def test_roundtrip_costs_booked_into_pnl(tmp_path):
    """Configured per-share costs (brokerage/STT/...) reduce booked P&L, so the
    daily-loss kill and the reports see net — not fantasy gross — numbers."""
    store = Store(tmp_path / "c.db")
    now = ist_now()
    pos = Position(symbol="NIFTY24800CE", qty=50, side="BUY", entry_px=100.0,
                   entry_ts=now, stop=0.0, risk_per_share=20.0, strike=24800,
                   right="CE", lot_size=50, underlying="NIFTY", entry_spot=24800.0)
    pos.position_id = store.open_position(pos)
    pos.exit_px = 110.0
    pos.exit_ts = now + dt.timedelta(minutes=10)
    pos.exit_reason = "I3 target"
    tid = store.close_position(pos, cost_per_share=2.0)
    tr = store.one("SELECT * FROM trades WHERE id=?", (tid,))
    assert tr["pnl"] == (110.0 - 100.0) * 50 - 2.0 * 50
    # partials share the same accounting
    pos2 = Position(symbol="NIFTY24800CE", qty=50, side="BUY", entry_px=100.0,
                    entry_ts=now, stop=0.0, risk_per_share=20.0, strike=24800,
                    right="CE", lot_size=50, underlying="NIFTY", entry_spot=24800.0)
    pos2.position_id = store.open_position(pos2)
    tid2 = store.book_partial(pos2, 110.0, 25, "I3 book", now, cost_per_share=2.0)
    tr2 = store.one("SELECT * FROM trades WHERE id=?", (tid2,))
    assert tr2["pnl"] == (110.0 - 100.0) * 25 - 2.0 * 25
    assert pos2.qty == 25
    store.close()

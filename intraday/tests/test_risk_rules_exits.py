"""Unit tests for the execution agents: Governor (sizing/limits), RuleEngine
(pre-trade gate), and the I0–I8 exit machine."""
import datetime as dt

from intraday.contracts import OrderIntent, Position
from intraday.exits import ExitMarket, manage
from intraday.options.models import IST
from intraday.options import ist_now
from intraday.options.chain_builder import build_chain
from intraday.risk import Governor
from intraday.rules import RuleEngine
from intraday.tests.test_options_math import _synthetic_payload


def _weekday_10am():
    d = dt.datetime(2026, 9, 30, 10, 0, tzinfo=IST)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _atm_call(spot=24800.0):
    ch = build_chain(_synthetic_payload(spot=spot, iv=0.14, lot=50))
    return ch, ch.get(ch.atm, True)


# ── Governor ────────────────────────────────────────────────────────────────
def test_governor_sizes_positive_lots():
    ch, leg = _atm_call()
    g = Governor({"risk_per_trade_pct": 1.0, "max_daily_loss_rupees": 6000,
                  "max_lots_per_symbol": 10, "max_premium_pct_of_equity": 25,
                  "max_prem_loss_pct": 40})
    inst = {"strike": leg.strike, "right": "CE", "lot_size": 50,
            "entry_prem": leg.ask, "iv": leg.iv}
    r = g.size(instrument=inst, chain=ch, sl_pts=40.0, equity=200000.0)
    assert r.ok and r.lots >= 1 and r.qty == r.lots * 50
    assert 0 < r.risk_per_share <= leg.ask       # long option risk <= premium paid
    assert r.caps["risk_budget_lots"] >= r.lots


def test_governor_daily_loss_kill_and_drawdown():
    g = Governor({"max_daily_loss_rupees": 5000})
    assert g.daily_loss_breached(-5000.0) is True
    assert g.daily_loss_breached(-3000.0, unrealized_pnl=-2500.0) is True
    assert g.daily_loss_breached(-1000.0) is False
    assert g.drawdown_scalar([100, 103, 101]) == 1.0        # <5% dd
    assert g.drawdown_scalar([100, 110, 104]) == 0.75       # ~5.5% dd
    assert g.drawdown_scalar([100, 120, 100]) == 0.5        # ~17% dd


def test_governor_premium_cap_binds_when_tiny_equity():
    ch, leg = _atm_call()
    g = Governor({"risk_per_trade_pct": 100.0, "max_premium_pct_of_equity": 5.0,
                  "max_lots_per_symbol": 999})
    inst = {"strike": leg.strike, "right": "CE", "lot_size": 50,
            "entry_prem": leg.ask, "iv": leg.iv}
    r = g.size(instrument=inst, chain=ch, sl_pts=40.0, equity=100000.0)
    assert r.caps["premium_cap_lots"] <= r.caps["risk_budget_lots"]


# ── RuleEngine ────────────────────────────────────────────────────────────────
def _intent(underlying="NIFTY", right="CE", lots=1, lot=50):
    return OrderIntent(symbol=f"{underlying}24800{right}", side="BUY", qty=lots * lot,
                       underlying=underlying, right=right, lot_size=lot, strike=24800)


def test_rules_allow_entry_in_session():
    re = RuleEngine({"square_off_time": "15:15", "no_new_entries_after": "15:00",
                     "max_positions": 4, "max_lots_per_symbol": 10})
    ok, why = re.check(_intent(), _weekday_10am(), [])
    assert ok, why


def test_rules_block_paused_halted_and_late():
    re = RuleEngine({"square_off_time": "15:15", "no_new_entries_after": "15:00"})
    now = _weekday_10am()
    assert re.check(_intent(), now, [], paused=True)[0] is False
    assert re.check(_intent(), now, [], halted=True)[0] is False
    late = now.replace(hour=15, minute=20)
    assert re.check(_intent(), late, [])[0] is False
    exits_always = re.check(_intent(), late, [], is_exit=True)
    assert exits_always[0] is True          # exits bypass the time gate


def test_rules_block_duplicate_and_max_positions():
    re = RuleEngine({"max_positions": 2, "max_lots_per_symbol": 10})
    now = _weekday_10am()
    held = Position(symbol="NIFTY24800CE", qty=50, side="BUY", entry_px=100,
                    entry_ts=now, stop=0, risk_per_share=20, right="CE",
                    underlying="NIFTY", lot_size=50)
    ok, why = re.check(_intent(underlying="NIFTY", right="CE"), now, [held])
    assert ok is False and "duplicate" in why.lower()
    # different direction on same underlying is allowed (until max positions)
    ok2, _ = re.check(_intent(underlying="NIFTY", right="PE"), now, [held])
    assert ok2 is True


# ── Exit machine ──────────────────────────────────────────────────────────────
def _pos(entry=100.0, stop=24770.0, rps=20.0, spot=24800.0):
    return Position(symbol="NIFTY24800CE", qty=50, side="BUY", entry_px=entry,
                    entry_ts=ist_now(), stop=stop, risk_per_share=rps, right="CE",
                    strike=24800, lot_size=50, underlying="NIFTY", entry_spot=spot,
                    max_prem=entry, max_fav_spot=spot)


def _mkt(spot, mid, square=False):
    return ExitMarket(now=ist_now(), spot=spot, leg_bid=mid * 0.99, leg_ask=mid * 1.01,
                      leg_mid=mid, bars=None, vwap=None, is_square_off=square)


def test_exit_square_off_overrides():
    d = manage(_pos(), _mkt(24900, 130, square=True), {})
    assert d.action == "EXIT" and "I0" in d.reason


def test_exit_underlying_stop():
    d = manage(_pos(stop=24770), _mkt(24760, 90), {})   # spot below stop
    assert d.action == "EXIT" and "I1" in d.reason


def test_exit_target_and_partial():
    cfg = {"target_r_1": 1.0, "target_r_2": 2.0, "partial_pct_1": 50}
    # +1R (mid=entry+1*rps=120) -> PARTIAL + breakeven
    p = _pos()
    d1 = manage(p, _mkt(24850, 120), cfg)
    assert d1.action == "PARTIAL" and p.breakeven_done and p.stop == p.entry_spot
    # +2R (mid=140) -> full EXIT
    d2 = manage(_pos(), _mkt(24900, 140), cfg)
    assert d2.action == "EXIT" and "target" in d2.reason.lower()


def test_exit_hold_updates_max_prem():
    p = _pos()
    d = manage(p, _mkt(24810, 105), {"target_r_1": 5, "target_r_2": 9})
    assert d.action == "HOLD" and p.max_prem >= 105 and p.age_bars == 1


# ── FUT legs: direction-aware exits, budget, dedup ────────────────────────────
def _fut_pos(side="BUY", entry=24800.0, stop=24760.0, rps=40.0):
    return Position(symbol="NIFTY-FUT", qty=50, side=side, entry_px=entry,
                    entry_ts=ist_now(), stop=stop, risk_per_share=rps,
                    right="FUT", strike=0, lot_size=50, underlying="NIFTY",
                    entry_spot=entry, max_prem=entry, max_fav_spot=entry)


def _fut_mkt(mark):
    return ExitMarket(now=ist_now(), spot=mark, leg_bid=mark, leg_ask=mark,
                      leg_mid=mark, bars=None, vwap=None, is_square_off=False)


def test_exit_fut_follows_trade_direction():
    hold_cfg = {"target_r_1": 5, "target_r_2": 9}
    # long FUT above its stop HOLDS (a CE/PE-only read stops it instantly)
    d = manage(_fut_pos("BUY"), _fut_mkt(24810), hold_cfg)
    assert d.action == "HOLD", d.reason
    d = manage(_fut_pos("BUY"), _fut_mkt(24750), {})
    assert d.action == "EXIT" and "I1" in d.reason
    # short FUT mirrors
    d = manage(_fut_pos("SELL", stop=24840), _fut_mkt(24790), hold_cfg)
    assert d.action == "HOLD", d.reason
    d = manage(_fut_pos("SELL", stop=24840), _fut_mkt(24850), {})
    assert d.action == "EXIT" and "I1" in d.reason


def test_governor_counts_fut_by_stop_risk():
    g = Governor({"total_budget": 100000, "soft_cap_pct": 80, "hard_cap_pct": 90})
    fut = _fut_pos("BUY")                       # 25k mark, 40 pts of stop risk
    assert g.deployed_premium([fut]) == 40 * 50  # not 24800 * 50
    assert g.utilisation_pct([fut]) == 2.0
    assert g.can_open_new([fut], extra_premium=10000)[0] is True
    short = _fut_pos("SELL", stop=24840)
    assert g.deployed_premium([short]) == 40 * 50  # shorts count too


def test_rules_fut_dedup_is_side_aware():
    re = RuleEngine({"square_off_time": "15:15", "no_new_entries_after": "15:00",
                     "max_positions": 4, "max_lots_per_symbol": 10})
    now = _weekday_10am()
    held = _fut_pos("BUY")

    def _fut_intent(side):
        return OrderIntent(symbol="NIFTY-FUT", side=side, qty=50,
                           underlying="NIFTY", right="FUT", lot_size=50)
    ok, why = re.check(_fut_intent("BUY"), now, [held])
    assert ok is False and "duplicate" in why.lower()
    ok, _ = re.check(_fut_intent("SELL"), now, [held])
    assert ok is True                              # opposite side is not a dup
    # options behaviour unchanged: same right still blocks
    opt_held = Position(symbol="NIFTY24800CE", qty=50, side="BUY", entry_px=100,
                        entry_ts=now, stop=0, risk_per_share=20, right="CE",
                        underlying="NIFTY", lot_size=50)
    ok, _ = re.check(_intent(underlying="NIFTY", right="CE"), now, [opt_held])
    assert ok is False

"""Market-hours gate + settings tests.

Covers the bug this feature fixes (pre-open ticks must not fire auto-exits),
the DST-aware MCX default, the DB override/holiday round-trip, and the
fail-open behaviour for unknown venues.

Run:
    ./venv/bin/pytest tests/test_market_hours.py -v
"""
import asyncio
import os
import tempfile
from datetime import date, datetime, time

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_mh.db")

import pytest

import session_windows as sw
from app.core import state
from db.engine import Base, engine
import db.models  # noqa: F401 — registers all tables with Base before create_all


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Start every test from clean in-memory config.
    sw.set_session_overrides({})
    sw.set_holiday_overrides(set())
    sw.set_enforced(True)
    yield


# --------------------------------------------------------------------------
# session_windows pure logic
# --------------------------------------------------------------------------

def test_mcx_close_tracks_us_dst():
    # Summer (US DST on) → 23:55; winter → 23:30.
    assert sw._default_session("MCX", date(2026, 7, 1))[1] == time(23, 55)
    assert sw._default_session("MCX", date(2026, 1, 10))[1] == time(23, 30)
    # Open time never shifts.
    assert sw._default_session("MCX", date(2026, 7, 1))[0] == time(9, 0)


def test_nfo_window_is_static():
    assert sw._default_session("NFO", date(2026, 7, 1)) == (time(9, 15), time(15, 30))
    assert sw._default_session("NFO", date(2026, 1, 10)) == (time(9, 15), time(15, 30))


def test_gate_blocks_pre_open_allows_in_session():
    wed = date(2026, 7, 1)  # a non-holiday weekday
    assert sw.is_market_open_for_orders("NFO", datetime(2026, 7, 1, 9, 7, tzinfo=sw.IST)) is False
    assert sw.is_market_open_for_orders("NFO", datetime(2026, 7, 1, 9, 20, tzinfo=sw.IST)) is True
    assert sw.is_market_open_for_orders("NFO", datetime(2026, 7, 1, 15, 45, tzinfo=sw.IST)) is False
    assert sw.is_trading_day("NFO", wed) is True


def test_gate_fails_open_for_unknown_venue():
    # An exchange with no window must never be silently blocked.
    pre_open = datetime(2026, 7, 1, 9, 7, tzinfo=sw.IST)
    assert sw.is_market_open_for_orders("NCDEX", pre_open) is True


def test_enforce_toggle_off_always_allows_orders_but_not_sweeper():
    pre_open = datetime(2026, 7, 1, 9, 7, tzinfo=sw.IST)
    sw.set_enforced(False)
    assert sw.is_market_open_for_orders("NFO", pre_open) is True
    # The sweeper's window check ignores the enforce toggle.
    assert sw.is_session_open("NFO", pre_open) is False


def test_holiday_closes_the_day():
    holiday = date(2026, 1, 26)  # Republic Day (baked in)
    assert sw.is_trading_day("NFO", holiday) is False
    noon = datetime(2026, 1, 26, 12, 0, tzinfo=sw.IST)
    assert sw.is_market_open_for_orders("NFO", noon) is False


# --------------------------------------------------------------------------
# DB-backed service round-trip
# --------------------------------------------------------------------------

def test_override_persists_and_loads():
    from app.services.market_hours import (
        get_market_hours_settings,
        load_market_hours_config,
        reset_exchange_hours,
        upsert_exchange_hours,
    )
    upsert_exchange_hours("MCX", "10:00", "20:00")
    assert sw.effective_session("MCX", date(2026, 7, 1)) == (time(10, 0), time(20, 0))

    row = next(e for e in get_market_hours_settings().exchanges if e.exch == "MCX")
    assert row.is_custom is True
    assert (row.open, row.close) == ("10:00", "20:00")
    # DST default still surfaced for the reset affordance.
    assert row.default_close == "23:55"

    # Fresh process would rebuild the same overrides from the DB.
    sw.set_session_overrides({})
    load_market_hours_config()
    assert sw.effective_session("MCX", date(2026, 7, 1)) == (time(10, 0), time(20, 0))

    reset_exchange_hours("MCX")
    assert sw.effective_session("MCX", date(2026, 7, 1)) == (time(9, 0), time(23, 55))


def test_added_holiday_blocks_gate():
    from app.services.market_hours import add_holiday, remove_holiday
    d = datetime(2026, 7, 2, 12, 0, tzinfo=sw.IST)  # a normal trading Thursday
    assert sw.is_market_open_for_orders("NFO", d) is True
    add_holiday("2026-07-02", "Custom closure")
    assert sw.is_market_open_for_orders("NFO", d) is False
    remove_holiday("2026-07-02")
    assert sw.is_market_open_for_orders("NFO", d) is True


def test_enforced_setting_persists():
    from app.services.market_hours import (
        get_market_hours_settings,
        load_market_hours_config,
        set_enforced_setting,
    )
    set_enforced_setting(False)
    assert get_market_hours_settings().enforced is False
    sw.set_enforced(True)  # simulate drift
    load_market_hours_config()
    assert sw.is_enforced() is False


# --------------------------------------------------------------------------
# The bug: _on_target_tick must not place an exit while the market is closed
# --------------------------------------------------------------------------

def _arm_symbol_target():
    """Set up an in-the-money long position with a hit target in state."""
    key = "NFO|99999"
    state._symbol_targets.clear()
    state._exchange_targets.clear()
    state._auto_exited_tokens.clear()
    state._target_positions.clear()
    state._current_bids.clear()
    state._current_asks.clear()
    state._current_ltps.clear()
    state._symbol_targets[key] = {"enabled": True, "target_value": 50.0, "carried_pnl": 0.0}
    state._target_positions[key] = [{
        "netqty": 1, "exch": "NFO", "tsym": "X", "prd": "M", "lotsize": 1, "prcftr": 1.0,
        "upldprc": 0.0, "daybuyamt": 100.0, "daysellamt": 0.0,
        "cfbuyqty": 0, "cfsellqty": 0, "netavgprc": 100.0,
    }]
    state._current_bids[key] = 200.0  # exit at 200 → total_pnl = 100 >= 50
    return {"e": "NFO", "tk": "99999", "lp": "200", "bp1": "200", "sp1": "200"}


def test_tick_does_not_place_exit_when_market_closed(monkeypatch):
    import app.services.targets as targets
    tick = _arm_symbol_target()
    calls = []

    async def _fake_place(positions_map, remarks):
        calls.append(remarks)
        return {k: True for k in positions_map}

    monkeypatch.setattr(targets, "_place_exit_orders", _fake_place)
    monkeypatch.setattr(targets, "is_market_open_for_orders", lambda exch, now=None: False)

    asyncio.run(targets._on_target_tick(tick))
    assert calls == [], "exit must not be placed while the market is closed"
    # Latch untouched so it can fire once the market opens.
    assert "NFO|99999" not in state._auto_exited_tokens


def test_tick_places_exit_when_market_open(monkeypatch):
    import app.services.targets as targets
    tick = _arm_symbol_target()
    calls = []

    async def _fake_place(positions_map, remarks):
        calls.append(remarks)
        return {k: True for k in positions_map}

    monkeypatch.setattr(targets, "_place_exit_orders", _fake_place)
    monkeypatch.setattr(targets, "is_market_open_for_orders", lambda exch, now=None: True)
    monkeypatch.setattr(targets, "_clear_symbol_target_carried", lambda *a, **k: None)

    asyncio.run(targets._on_target_tick(tick))
    assert calls == ["auto_exit_target"], "exit should fire once the market is open"

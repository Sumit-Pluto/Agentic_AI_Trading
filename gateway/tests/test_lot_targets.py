"""Unit tests for per-order (per-lot) auto-exit targets.

Each OPEN lot carries its OWN target on its own live P&L, sized to just that
lot — so two batches of the same symbol fire independently, and the per-order
path never double-fires against a symbol/exchange exit already in flight.

Run:
    ./venv/bin/pytest tests/test_lot_targets.py -v
"""
import asyncio
import os
import tempfile

os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_lot_targets.db")
)

import pytest

import app.services.targets as t
from app.core import state
from db.engine import Base, SessionLocal, engine
from db.models import LotExit, OrderLot

OWNER = "TESTUID"
EXCH = "MCX"
TSYM = "NATURALGAS25JULFUT"
TOKEN = "54321"
KEY = f"{EXCH}|{TOKEN}"


class _StubAuth:
    auth_token = "tok"
    user_id = OWNER

    def is_authenticated(self):
        return True


class _StubScripmaster:
    # prcftr=1 for natural gas — (qty × price) is already rupees.
    master = {f"{EXCH}|{TSYM}": {"prcftr": "1", "lotsize": "1"}}

    def get_token(self, exch, tsym):
        return TOKEN


class _StubTicker:
    def subscribe(self, keys):
        pass


class _RecordingBroker:
    """Captures every placeOrder payload; always accepts."""

    def __init__(self):
        self.orders = []

    async def placeOrder(self, token, credentials, order):
        self.orders.append(order)
        return {"stat": "Ok", "norenordno": f"AUTO{len(self.orders)}"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    broker = _RecordingBroker()
    monkeypatch.setattr(t, "_require_legacy_auth", lambda: _StubAuth())
    monkeypatch.setattr(t, "get_scripmaster", lambda: _StubScripmaster())
    monkeypatch.setattr(t, "get_broker", lambda _name: broker)
    monkeypatch.setattr(t, "ticker_manager", _StubTicker())
    monkeypatch.setattr(t, "is_market_open_for_orders", lambda _exch: True)

    async def _fake_reconcile(_owner):
        return None
    # _auto_exit_lot lazily imports this from the reconciliation module.
    monkeypatch.setattr(
        "app.services.reconciliation._short_poll_reconcile", _fake_reconcile, raising=False
    )

    # Clean slate for every shared in-memory cache the tick reads.
    for d in (state._lot_targets, state._symbol_targets, state._exchange_targets,
              state._target_positions, state._current_bids, state._current_asks,
              state._current_ltps):
        d.clear()
    state._lot_target_exited.clear()
    state._auto_exited_tokens.clear()
    state._exchange_target_exited.clear()

    yield broker


def _add_lot(target_value, *, side="B", open_qty=100, entry=500.0, status="OPEN") -> int:
    db = SessionLocal()
    try:
        lot = OrderLot(
            owner_uid=OWNER, exch=EXCH, tsym=TSYM, token=TOKEN, lotsize=1,
            product_type="M", side=side, entry_qty=open_qty, open_qty=open_qty,
            avg_entry_price=entry, status=status, broker_entry_orderid="ORDX",
            client_ref=f"lot{target_value}{side}{open_qty}",
            target_enabled=target_value > 0, target_value=target_value,
        )
        db.add(lot)
        db.commit()
        return lot.id
    finally:
        db.close()


def _tick(bid):
    return {"e": EXCH, "tk": TOKEN, "lp": str(bid), "bp1": str(bid), "sp1": str(bid)}


def _pending_exits():
    db = SessionLocal()
    try:
        return db.query(LotExit).filter(LotExit.status == "PENDING").all()
    finally:
        db.close()


def test_load_lot_targets_builds_cache():
    lot_id = _add_lot(1000.0)
    t._load_lot_targets()
    assert KEY in state._lot_targets
    entries = state._lot_targets[KEY]
    assert len(entries) == 1
    e = entries[0]
    assert e["lot_id"] == lot_id
    assert e["open_qty"] == 100 and e["avg_entry_price"] == 500.0
    assert e["target_value"] == 1000.0 and e["prcftr"] == 1.0


def test_pending_lot_not_cached():
    # A PENDING lot has no open position — it must not arm a live target.
    _add_lot(1000.0, status="PENDING")
    t._load_lot_targets()
    assert KEY not in state._lot_targets


def test_per_order_target_fires_sized_to_lot(_fresh):
    broker = _fresh
    lot_id = _add_lot(1000.0)  # long 100 @ 500
    t._load_lot_targets()
    # bid 511 → live = (511-500)*100 = 1100 ≥ 1000 → fire.
    asyncio.run(t._on_target_tick(_tick(511)))
    assert len(broker.orders) == 1
    o = broker.orders[0]
    assert o["buy_or_sell"] == "S"          # long exits by selling
    assert o["quantity"] == 100             # sized to THIS lot only
    assert o["tradingsymbol"] == TSYM
    assert lot_id in state._lot_target_exited
    assert len(_pending_exits()) == 1       # went through the LotExit machinery


def test_per_order_target_holds_below_threshold(_fresh):
    broker = _fresh
    _add_lot(1000.0)  # needs +1000; at bid 505 live is only +500
    t._load_lot_targets()
    asyncio.run(t._on_target_tick(_tick(505)))
    assert broker.orders == []
    assert _pending_exits() == []


def test_two_lots_same_symbol_fire_independently(_fresh):
    broker = _fresh
    big = _add_lot(5000.0, open_qty=100)   # needs +5000 → +50/pt
    small = _add_lot(100.0, open_qty=10)   # needs +100  → +10/pt
    t._load_lot_targets()
    # bid 511: big live = 1100 (< 5000, holds); small live = 110 (≥ 100, fires).
    asyncio.run(t._on_target_tick(_tick(511)))
    assert len(broker.orders) == 1
    assert broker.orders[0]["quantity"] == 10   # only the small lot exited
    assert small in state._lot_target_exited
    assert big not in state._lot_target_exited


def test_per_order_skips_when_symbol_latched(_fresh):
    broker = _fresh
    _add_lot(1000.0)
    t._load_lot_targets()
    # Symbol/exchange target already exiting the whole net position for this key.
    state._auto_exited_tokens.add(KEY)
    asyncio.run(t._on_target_tick(_tick(511)))
    assert broker.orders == []   # no second exit → no oversell


def test_short_lot_fires_on_ask(_fresh):
    broker = _fresh
    _add_lot(1000.0, side="S", entry=500.0)  # short 100 @ 500
    t._load_lot_targets()
    # For a short, profit grows as price falls; ask 489 → (489-500)*100*(-1)=1100.
    asyncio.run(t._on_target_tick(_tick(489)))
    assert len(broker.orders) == 1
    assert broker.orders[0]["buy_or_sell"] == "B"   # short exits by buying back

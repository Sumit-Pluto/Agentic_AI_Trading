"""Unit tests for the Re-entry P&L carry-forward feature in place_order.

Run:
    ./venv/bin/pytest tests/test_orders_reentry.py -v
"""
import asyncio
import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_gateway.db")

import pytest

import app.routers.orders as orders_mod
from app.core import deps as deps_mod
from app.core.security import Principal
from app.schemas.orders import PlaceOrderRequest
from db.engine import Base, SessionLocal, engine
from db.models import OrderLot

OWNER = "TESTUID"
# A human caller (kind="user"); source_service stays NULL. place_order takes
# `principal` via Depends, so direct calls must pass one explicitly.
_PRINCIPAL = Principal(kind="user")


class _StubAuth:
    user_id = OWNER
    auth_token = "tok"

    def is_authenticated(self):
        return True


class _StubScripmaster:
    master: dict = {}

    def get_token(self, exch, tsym):
        return "12345"


class _StubBroker:
    async def placeOrder(self, token, credentials, payload):
        return {"stat": "Ok", "norenordno": "ORD123"}


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(orders_mod, "_require_legacy_auth", lambda: _StubAuth())
    monkeypatch.setattr(orders_mod, "get_broker", lambda name: _StubBroker())
    monkeypatch.setattr(orders_mod, "get_scripmaster", lambda: _StubScripmaster())
    monkeypatch.setattr(orders_mod.asyncio, "create_task", lambda coro: coro.close())
    yield


def _add_closed_lot(**kw) -> int:
    db = SessionLocal()
    defaults = dict(
        owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ", token="12345",
        lotsize=1, product_type="M", side="B", entry_qty=100, open_qty=0,
        avg_entry_price=500.0, status="CLOSED", client_ref="lotsrc123",
        realized_pnl=1000.0, carried_pnl=200.0,
    )
    defaults.update(kw)
    lot = OrderLot(**defaults)
    db.add(lot)
    db.commit()
    lot_id = lot.id
    db.close()
    return lot_id


def _base_req(**kw) -> PlaceOrderRequest:
    defaults = dict(
        buy_or_sell="B", product_type="M", exchange="NSE",
        tradingsymbol="RELIANCE-EQ", quantity=100, price_type="LMT", price=520.0,
    )
    defaults.update(kw)
    return PlaceOrderRequest(**defaults)


def test_reentry_from_closed_lot_recycles_the_same_row_carrying_pnl_forward():
    src_id = _add_closed_lot()
    req = _base_req(reentry_source_lot_id=src_id)

    result = asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    db = SessionLocal()
    # No new row: the CLOSED row is reused in place.
    assert result["lot_id"] == src_id
    assert db.query(OrderLot).count() == 1

    lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    # Prior realized+carried rides forward; the reopened leg's own realized resets.
    assert lot.carried_pnl == pytest.approx(1000.0 + 200.0)
    assert lot.realized_pnl == 0.0
    assert lot.is_reentry is True
    assert lot.reentry_source_lot_id == src_id
    # Reopened as a fresh entry for the new quantity.
    assert lot.status == "PENDING"
    assert lot.open_qty == 100
    assert lot.closed_at is None


def test_plain_order_without_reentry_source_has_no_carry():
    req = _base_req()

    result = asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    new_lot = SessionLocal().query(OrderLot).filter(OrderLot.id == result["lot_id"]).first()
    assert new_lot.carried_pnl == 0.0
    assert new_lot.is_reentry is False
    assert new_lot.reentry_source_lot_id is None


def test_reentry_from_open_lot_makes_a_new_row_and_does_not_carry_pnl():
    # OPEN lot, never exited: nothing booked yet. The live position must stay
    # on its own row, so re-entry adds a *new* lot rather than reusing this one.
    src_id = _add_closed_lot(status="OPEN", open_qty=100, realized_pnl=0.0, carried_pnl=0.0)
    req = _base_req(reentry_source_lot_id=src_id)

    result = asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    db = SessionLocal()
    assert result["lot_id"] != src_id
    assert db.query(OrderLot).count() == 2

    new_lot = db.query(OrderLot).filter(OrderLot.id == result["lot_id"]).first()
    assert new_lot.carried_pnl == 0.0
    assert new_lot.is_reentry is True
    assert new_lot.reentry_source_lot_id == src_id
    # The source OPEN lot is left untouched.
    src_lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    assert src_lot.status == "OPEN"


def test_reentry_from_partial_lot_tags_but_does_not_carry_its_booked_pnl():
    # PARTIAL lot: some qty already exited and booked, but the lot itself isn't
    # CLOSED yet — per spec, only a fully CLOSED source carries its P&L forward.
    src_id = _add_closed_lot(status="PARTIAL", open_qty=40, realized_pnl=600.0, carried_pnl=0.0)
    req = _base_req(reentry_source_lot_id=src_id)

    result = asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    new_lot = SessionLocal().query(OrderLot).filter(OrderLot.id == result["lot_id"]).first()
    assert new_lot.carried_pnl == 0.0
    assert new_lot.is_reentry is True


def test_reentry_source_from_another_user_is_ignored():
    src_id = _add_closed_lot(owner_uid="OTHERUID", client_ref="lotother123")
    req = _base_req(reentry_source_lot_id=src_id)

    result = asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    new_lot = SessionLocal().query(OrderLot).filter(OrderLot.id == result["lot_id"]).first()
    assert new_lot.carried_pnl == 0.0
    assert new_lot.is_reentry is False


class _FailingBroker:
    async def placeOrder(self, token, credentials, payload):
        return {"stat": "Not_Ok", "emsg": "rejected"}


def test_reentry_clears_the_temp_exit_tag():
    # A TE-tagged closed lot is kept on the list across days; re-entering it
    # makes the row live again, so the tag is consumed.
    src_id = _add_closed_lot(is_temp_exit=True)
    req = _base_req(reentry_source_lot_id=src_id)

    asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    lot = SessionLocal().query(OrderLot).filter(OrderLot.id == src_id).first()
    assert lot.is_temp_exit is False
    assert lot.status == "PENDING"


def test_rejected_reentry_restores_the_temp_exit_tag(monkeypatch):
    # If the broker rejects the re-entry, the recycled row is restored to its
    # finished CLOSED state — including its TE tag — so it isn't lost.
    monkeypatch.setattr(orders_mod, "get_broker", lambda name: _FailingBroker())
    src_id = _add_closed_lot(is_temp_exit=True)
    req = _base_req(reentry_source_lot_id=src_id)

    with pytest.raises(Exception):
        asyncio.run(orders_mod.place_order(req, principal=_PRINCIPAL))

    lot = SessionLocal().query(OrderLot).filter(OrderLot.id == src_id).first()
    assert lot.is_temp_exit is True
    assert lot.status == "CLOSED"

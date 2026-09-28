"""Tests for the Temporary-Exit (TE) visibility rule in GET /api/lots.

A TE-tagged CLOSED lot must stay in the day view across days (exempt from the
closed-today filter); a plain CLOSED lot from a prior day must not.

Run:
    ./venv/bin/pytest tests/test_lots_temp_exit.py -v
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_gateway_te.db")

import pytest

import app.routers.lots as lots_mod
from db.engine import Base, SessionLocal, engine
from db.models import OrderLot

OWNER = "TESTUID"


class _StubAuth:
    user_id = OWNER
    auth_token = "tok"

    def is_authenticated(self):
        return True


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(lots_mod, "_require_legacy_auth", lambda: _StubAuth())
    # Don't touch the tick manager or the heavy serializer in a unit test —
    # we only care which rows the query returns.
    monkeypatch.setattr(lots_mod.ticker_manager, "subscribe", lambda keys: None)
    monkeypatch.setattr(lots_mod, "_serialize_lot", lambda lot: lot)
    yield


def _add(**kw) -> int:
    db = SessionLocal()
    defaults = dict(
        owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ", token="12345",
        lotsize=1, product_type="M", side="B", entry_qty=100, open_qty=0,
        avg_entry_price=500.0, status="CLOSED",
        client_ref="lot" + os.urandom(6).hex(),
    )
    defaults.update(kw)
    lot = OrderLot(**defaults)
    db.add(lot)
    db.commit()
    lot_id = lot.id
    db.close()
    return lot_id


def _get_lot_ids() -> set[int]:
    db = SessionLocal()
    try:
        rows = asyncio.run(lots_mod.get_lots(db=db))
        return {l.id for l in rows}
    finally:
        db.close()


def test_te_closed_lot_from_a_prior_day_stays_visible():
    yesterday = datetime.utcnow() - timedelta(days=1)
    te_id = _add(is_temp_exit=True, closed_at=yesterday)
    plain_id = _add(is_temp_exit=False, closed_at=yesterday)

    ids = _get_lot_ids()

    assert te_id in ids          # TE tag exempts it from the closed-today filter
    assert plain_id not in ids   # a plain prior-day close drops off, as before


def test_te_tag_does_not_affect_today_and_open_lots():
    open_id = _add(status="OPEN", open_qty=100, closed_at=None)
    closed_today_id = _add(closed_at=datetime.utcnow())

    ids = _get_lot_ids()

    assert open_id in ids
    assert closed_today_id in ids

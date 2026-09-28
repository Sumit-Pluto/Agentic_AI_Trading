"""Unit tests for lot reconciliation: partial-fill-then-cancel handling and
position-sync/order-book dedup.

Run:
    ./venv/bin/pytest tests/test_reconciliation.py -v
"""
import os
import tempfile
from datetime import datetime, timedelta

# Must be set before db.engine is imported anywhere in this process.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_gateway.db")

import pytest

import app.services.reconciliation as rec
from db.engine import Base, SessionLocal, engine
from db.models import LotExit, OrderLot, PersistentOrder

OWNER = "TESTUID"


class _StubScripmaster:
    master: dict = {}

    def get_token(self, exch, tsym):
        return "12345"


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(rec, "get_scripmaster", lambda: _StubScripmaster())
    yield


def _add_lot(**kw) -> int:
    db = SessionLocal()
    defaults = dict(
        owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ", token="12345",
        lotsize=1, product_type="M", side="B", entry_qty=100, open_qty=100,
        avg_entry_price=500.0, status="PENDING", broker_entry_orderid="ORD1",
        client_ref="lotabc123",
    )
    defaults.update(kw)
    lot = OrderLot(**defaults)
    db.add(lot)
    db.commit()
    lot_id = lot.id
    db.close()
    return lot_id


def _add_exit(lot_id: int, **kw) -> int:
    db = SessionLocal()
    defaults = dict(
        lot_id=lot_id, exit_qty=100, client_ref="lotexitxyz",
        status="PENDING", broker_exit_orderid="ORD2",
    )
    defaults.update(kw)
    ex = LotExit(**defaults)
    db.add(ex)
    db.commit()
    ex_id = ex.id
    db.close()
    return ex_id


def _order(**kw) -> dict:
    o = {
        "norenordno": "ORD1", "status": "CANCELED", "trantype": "B",
        "fillshares": "40", "avgprc": "505.0", "qty": "100", "prc": "500",
        "remarks": "lotabc123", "exch": "NSE", "tsym": "RELIANCE-EQ",
        "prd": "M", "norentm": "10:00:00 12-07-2026",
    }
    o.update(kw)
    return o


def _get(model, row_id):
    db = SessionLocal()
    row = db.query(model).filter_by(id=row_id).first()
    db.close()
    return row


def _lots_for_symbol(tsym="RELIANCE-EQ", statuses=("OPEN", "PARTIAL")):
    db = SessionLocal()
    rows = (
        db.query(OrderLot)
        .filter(OrderLot.owner_uid == OWNER, OrderLot.tsym == tsym,
                OrderLot.status.in_(list(statuses)))
        .all()
    )
    db.close()
    return rows


# ── Entry orders: partial fill then cancel ──────────────────────────────────

def test_entry_partial_fill_then_cancel_keeps_filled_part():
    lot_id = _add_lot()
    rec._reconcile_lots_from_orderbook_impl([_order()], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.entry_qty == 40
    assert lot.open_qty == 40
    assert lot.avg_entry_price == 505.0


def test_entry_cancel_without_fill_marks_cancelled():
    lot_id = _add_lot()
    rec._reconcile_lots_from_orderbook_impl([_order(fillshares="0", avgprc="0")], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "CANCELLED"


def test_entry_cancelled_lot_revived_when_fills_show_up_later():
    # A WS cancel event without fill fields lands first; the REST order book
    # with cumulative fillshares arrives on the next poll.
    lot_id = _add_lot()
    rec._reconcile_lots_from_orderbook_impl([_order(fillshares="0", avgprc="0")], OWNER)
    assert _get(OrderLot, lot_id).status == "CANCELLED"
    rec._reconcile_lots_from_orderbook_impl([_order()], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.open_qty == 40


def test_entry_partial_cancel_is_idempotent_across_polls():
    lot_id = _add_lot()
    for _ in range(3):
        rec._reconcile_lots_from_orderbook_impl([_order()], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.open_qty == 40
    assert len(_lots_for_symbol()) == 1


# ── Exit orders: partial fill then cancel ───────────────────────────────────

def test_exit_partial_fill_then_cancel_books_filled_part():
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id)
    order = _order(norenordno="ORD2", remarks="lotexitxyz", trantype="S",
                   fillshares="40", avgprc="510.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    ex = _get(LotExit, ex_id)
    lot = _get(OrderLot, lot_id)
    assert ex.status == "FILLED"
    assert ex.filled_qty == 40
    assert ex.avg_exit_price == 510.0
    assert lot.status == "PARTIAL"
    assert lot.open_qty == 60
    assert lot.realized_pnl == pytest.approx((510.0 - 500.0) * 40)


def test_exit_cancel_without_fill_marks_cancelled():
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id)
    order = _order(norenordno="ORD2", remarks="lotexitxyz", trantype="S",
                   fillshares="0", avgprc="0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    ex = _get(LotExit, ex_id)
    lot = _get(OrderLot, lot_id)
    assert ex.status == "CANCELLED"
    assert lot.status == "OPEN"
    assert lot.open_qty == 100


def test_exit_complete_still_closes_lot():
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id)
    order = _order(norenordno="ORD2", remarks="lotexitxyz", trantype="S",
                   status="COMPLETE", fillshares="100", avgprc="510.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    ex = _get(LotExit, ex_id)
    lot = _get(OrderLot, lot_id)
    assert ex.status == "FILLED"
    assert lot.status == "CLOSED"
    assert lot.open_qty == 0
    assert lot.realized_pnl == pytest.approx((510.0 - 500.0) * 100)


# ── External PENDING lots: partial fill then cancel ─────────────────────────

def test_external_pending_partial_fill_then_cancel_keeps_filled_part():
    lot_id = _add_lot(is_external=True, broker_entry_orderid="EXT1",
                      client_ref="ext_abc123")
    order = _order(norenordno="EXT1", remarks="")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.entry_qty == 40
    assert lot.open_qty == 40
    assert lot.avg_entry_price == 505.0


def test_external_pending_cancel_without_fill_marks_cancelled():
    lot_id = _add_lot(is_external=True, broker_entry_orderid="EXT1",
                      client_ref="ext_abc123")
    order = _order(norenordno="EXT1", remarks="", fillshares="0", avgprc="0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    assert _get(OrderLot, lot_id).status == "CANCELLED"


# ── Persistent orders: re-place only the unfilled remainder ─────────────────

def _add_persistent(quantity=100) -> int:
    db = SessionLocal()
    po = PersistentOrder(owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ",
                         side="B", quantity=quantity, status="ACTIVE")
    db.add(po)
    db.commit()
    po_id = po.id
    db.close()
    return po_id


def test_persistent_order_partial_fill_reduces_remainder():
    po_id = _add_persistent(quantity=100)
    lot_id = _add_lot(persistent_order_id=po_id)
    rec._reconcile_lots_from_orderbook_impl([_order()], OWNER)
    po = _get(PersistentOrder, po_id)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN" and lot.open_qty == 40
    assert po.status == "ACTIVE"
    assert po.quantity == 60


def test_persistent_order_fully_filled_via_partial_cancel_retires():
    po_id = _add_persistent(quantity=40)
    lot_id = _add_lot(persistent_order_id=po_id)
    rec._reconcile_lots_from_orderbook_impl([_order()], OWNER)
    po = _get(PersistentOrder, po_id)
    assert po.status == "FILLED"
    assert po.quantity == 0
    assert po.filled_lot_id == lot_id


def test_persistent_order_plain_cancel_stays_active():
    po_id = _add_persistent(quantity=100)
    _add_lot(persistent_order_id=po_id)
    rec._reconcile_lots_from_orderbook_impl([_order(fillshares="0", avgprc="0")], OWNER)
    po = _get(PersistentOrder, po_id)
    assert po.status == "ACTIVE"
    assert po.quantity == 100


# ── Position sync: order-book pass first, then netqty delta ─────────────────

def _position(netqty="50", netavgprc="100.0", prd="M"):
    return {"exch": "NSE", "tsym": "RELIANCE-EQ", "netqty": netqty,
            "netavgprc": netavgprc, "prd": prd}


def test_position_delta_after_book_reconcile_does_not_duplicate():
    # An untagged broker-UI fill appears in both the order book and positions.
    book = [_order(norenordno="UNTAG1", remarks="", status="COMPLETE",
                   fillshares="50", avgprc="100.0")]
    rec._reconcile_lots_from_orderbook_impl(book, OWNER)
    lots = _lots_for_symbol()
    assert len(lots) == 1 and lots[0].open_qty == 50

    rec._import_position_deltas_impl([_position()], OWNER)
    lots = _lots_for_symbol()
    assert len(lots) == 1, "position sync must not re-import a fill the book explained"
    assert lots[0].open_qty == 50


def test_position_delta_imports_untracked_position():
    # Gateway-was-down case: nothing in the book, position exists at broker.
    rec._import_position_deltas_impl([_position()], OWNER)
    lots = _lots_for_symbol()
    assert len(lots) == 1
    assert lots[0].side == "B"
    assert lots[0].open_qty == 50
    assert lots[0].is_external is True


# ── Product-type separation (MIS vs NRML vs CNC) ────────────────────────────

def test_position_delta_is_per_product():
    # Broker: long 50 NRML (tracked) and short 50 MIS (untracked) in the same
    # symbol. The MIS short must import into its own product, and the NRML row
    # must not see a phantom delta against the whole-symbol net.
    _add_lot(status="OPEN", product_type="M", entry_qty=50, open_qty=50)
    rec._import_position_deltas_impl(
        [_position(netqty="50", prd="M"), _position(netqty="-50", prd="I")], OWNER
    )
    lots = _lots_for_symbol()
    assert len(lots) == 2
    m = [l for l in lots if l.product_type == "M"]
    i = [l for l in lots if l.product_type == "I"]
    assert len(m) == 1 and m[0].side == "B" and m[0].open_qty == 50
    assert len(i) == 1 and i[0].side == "S" and i[0].open_qty == 50


def test_position_delta_per_product_is_idempotent():
    positions = [_position(netqty="50", prd="M"), _position(netqty="-50", prd="I")]
    rec._import_position_deltas_impl(positions, OWNER)
    rec._import_position_deltas_impl(positions, OWNER)
    assert len(_lots_for_symbol()) == 2


# ── Position sync: carry-forward entry price derived from total P&L ─────────

def test_position_delta_cf_derives_entry_from_pnl(monkeypatch):
    # Carry-forward long: broker's netavgprc is the prior close, not the real
    # entry. True basis 222.95; at ltp 219.71 the position P&L is
    # 3550 * (219.71 - 222.95) = -11502. Sync must back the entry out of that
    # P&L (entry = ltp - pnl/netqty) instead of trusting netavgprc.
    monkeypatch.setattr(rec.state, "_current_ltps", {})
    pos = {
        "exch": "NSE", "tsym": "ADANIPOWER-EQ", "netqty": "3550",
        "netavgprc": "220.50", "prd": "C",
        "cfbuyqty": "3550", "cfsellqty": "0", "upldprc": "222.95",
        "lp": "219.71",
    }
    rec._import_position_deltas_impl([pos], OWNER)
    lots = _lots_for_symbol("ADANIPOWER-EQ")
    assert len(lots) == 1
    assert lots[0].avg_entry_price == pytest.approx(222.95, abs=0.01)


def test_position_delta_cf_short_derives_entry(monkeypatch):
    monkeypatch.setattr(rec.state, "_current_ltps", {})
    # Carried short 100 at true entry 250; ltp 240 → pnl = +1000.
    pos = {
        "exch": "NSE", "tsym": "RELIANCE-EQ", "netqty": "-100",
        "netavgprc": "245.0", "prd": "M",
        "cfbuyqty": "0", "cfsellqty": "100", "upldprc": "250.0",
        "lp": "240.0",
    }
    rec._import_position_deltas_impl([pos], OWNER)
    lots = _lots_for_symbol()
    assert len(lots) == 1 and lots[0].side == "S"
    assert lots[0].avg_entry_price == pytest.approx(250.0, abs=0.01)


def test_position_delta_day_position_keeps_netavgprc():
    # No carry-forward component: netavgprc is trustworthy and must be used.
    rec._import_position_deltas_impl([_position(netavgprc="101.5")], OWNER)
    lots = _lots_for_symbol()
    assert len(lots) == 1
    assert lots[0].avg_entry_price == 101.5


def test_offset_does_not_cross_product_types():
    # Long NRML and short MIS are both live positions at the broker — the DB
    # must not net them against each other.
    id_m = _add_lot(status="OPEN", product_type="M", side="B", client_ref="lot_m")
    id_i = _add_lot(status="OPEN", product_type="I", side="S", client_ref="lot_i",
                    broker_entry_orderid="ORD9")
    db = SessionLocal()
    dirty = rec._offset_opposing_lots(db, OWNER, [])
    db.commit()
    db.close()
    assert dirty is False
    assert _get(OrderLot, id_m).status == "OPEN"
    assert _get(OrderLot, id_i).status == "OPEN"


def test_offset_within_same_product_still_nets():
    id_old = _add_lot(status="OPEN", product_type="M", side="B", client_ref="lot_old")
    id_new = _add_lot(status="OPEN", product_type="M", side="S", client_ref="lot_new",
                      broker_entry_orderid="ORD9", avg_entry_price=510.0)
    db = SessionLocal()
    dirty = rec._offset_opposing_lots(db, OWNER, [])
    db.commit()
    db.close()
    assert dirty is True
    old = _get(OrderLot, id_old)
    new = _get(OrderLot, id_new)
    assert old.status == "CLOSED" and new.status == "CLOSED"
    assert old.realized_pnl == pytest.approx((510.0 - 500.0) * 100)


def test_untagged_fill_does_not_close_other_product_lot():
    # An untagged MIS sell must open a short in its own product, not close
    # the NRML long.
    id_m = _add_lot(status="OPEN", product_type="M")
    order = _order(norenordno="UNTAG9", remarks="", status="COMPLETE",
                   trantype="S", fillshares="100", avgprc="510.0", prd="I")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    m = _get(OrderLot, id_m)
    assert m.status == "OPEN" and m.open_qty == 100
    lots = _lots_for_symbol()
    assert len(lots) == 2
    i = [l for l in lots if l.product_type == "I"][0]
    assert i.side == "S" and i.open_qty == 100


def test_untagged_fill_closes_same_product_lot():
    id_m = _add_lot(status="OPEN", product_type="M")
    order = _order(norenordno="UNTAG9", remarks="", status="COMPLETE",
                   trantype="S", fillshares="100", avgprc="510.0", prd="M")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    m = _get(OrderLot, id_m)
    assert m.status == "CLOSED"
    assert m.realized_pnl == pytest.approx((510.0 - 500.0) * 100)


def test_pending_order_other_product_not_treated_as_exit():
    # A pending MIS sell in the broker UI is a new short, not an exit of the
    # NRML long — it must import as a PENDING external lot.
    _add_lot(status="OPEN", product_type="M")
    order = _order(norenordno="PEND1", remarks="", status="OPEN", trantype="S",
                   qty="100", prc="510", fillshares="0", avgprc="0", prd="I")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    db = SessionLocal()
    pend = db.query(OrderLot).filter_by(owner_uid=OWNER, status="PENDING").all()
    db.close()
    assert len(pend) == 1
    assert pend[0].product_type == "I"


def test_pending_order_same_product_exit_still_skipped():
    _add_lot(status="OPEN", product_type="M")
    order = _order(norenordno="PEND1", remarks="", status="OPEN", trantype="S",
                   qty="100", prc="510", fillshares="0", avgprc="0", prd="M")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    db = SessionLocal()
    pend = db.query(OrderLot).filter_by(owner_uid=OWNER, status="PENDING").all()
    db.close()
    assert len(pend) == 0


def test_norm_prd_defaults_and_normalizes():
    assert rec._norm_prd(None) == "M"
    assert rec._norm_prd("") == "M"
    assert rec._norm_prd(" i ") == "I"
    assert rec._norm_prd("M") == "M"


# ── Orphaned fills: broker filled, DB said CANCELLED ────────────────────────

def test_orphaned_complete_fill_revives_cancelled_entry():
    # place_order lost the response and wrote CANCELLED, but the broker
    # filled the order — the book's COMPLETE must win.
    lot_id = _add_lot(status="CANCELLED")
    order = _order(status="COMPLETE", fillshares="100", avgprc="505.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.open_qty == 100
    assert lot.avg_entry_price == 505.0


def test_complete_entry_adopts_broker_fill_qty():
    # Order reduced from 100 to 60 in the broker UI before filling.
    lot_id = _add_lot(status="PENDING")
    order = _order(status="COMPLETE", fillshares="60", avgprc="505.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    lot = _get(OrderLot, lot_id)
    assert lot.status == "OPEN"
    assert lot.entry_qty == 60
    assert lot.open_qty == 60


def test_orphaned_complete_exit_fill_booked():
    # Exit marked CANCELLED locally but the broker filled it.
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id, status="CANCELLED")
    order = _order(norenordno="ORD2", remarks="lotexitxyz", trantype="S",
                   status="COMPLETE", fillshares="100", avgprc="510.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    ex = _get(LotExit, ex_id)
    lot = _get(OrderLot, lot_id)
    assert ex.status == "FILLED"
    assert lot.status == "CLOSED"
    assert lot.realized_pnl == pytest.approx((510.0 - 500.0) * 100)


# ── Malformed orders must not block the pass ────────────────────────────────

def test_malformed_orders_do_not_block_pass():
    lot_id = _add_lot(status="PENDING")
    good = _order(status="COMPLETE", fillshares="100", avgprc="505.0")
    weird = {"norenordno": "WEIRD", "status": None, "fillshares": {"x": 1},
             "avgprc": "junk", "remarks": None}
    rec._reconcile_lots_from_orderbook_impl(["junk", weird, good], OWNER)
    assert _get(OrderLot, lot_id).status == "OPEN"


# ── TRIGGER_PENDING (broker-UI stop-loss) orders ────────────────────────────

def test_trigger_pending_sl_imported():
    order = _order(norenordno="SL1", remarks="", status="TRIGGER_PENDING",
                   trantype="S", qty="100", prc="0", trgprc="495.5",
                   fillshares="0", avgprc="0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    db = SessionLocal()
    pend = db.query(OrderLot).filter_by(owner_uid=OWNER, status="PENDING").all()
    db.close()
    assert len(pend) == 1
    assert pend[0].side == "S"
    assert pend[0].avg_entry_price == 495.5
    assert pend[0].is_external is True


def test_trigger_pending_sl_imported_uses_broker_order_time():
    # opened_at should reflect the broker's own order timestamp (IST,
    # converted to UTC) rather than the moment the sync happened to run.
    order = _order(norenordno="SL2", remarks="", status="TRIGGER_PENDING",
                   trantype="S", qty="100", prc="0", trgprc="495.5",
                   fillshares="0", avgprc="0", norentm="10:00:00 12-07-2026")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    db = SessionLocal()
    pend = db.query(OrderLot).filter_by(owner_uid=OWNER, status="PENDING").first()
    db.close()
    assert pend.opened_at == datetime(2026, 7, 12, 4, 30, 0)


def test_trigger_pending_sl_protecting_position_skipped():
    # SL sell fully covered by a same-product long = a pending exit, not a
    # new position.
    _add_lot(status="OPEN", product_type="M")
    order = _order(norenordno="SL1", remarks="", status="TRIGGER_PENDING",
                   trantype="S", qty="100", prc="0", trgprc="495.5",
                   fillshares="0", avgprc="0", prd="M")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    db = SessionLocal()
    pend = db.query(OrderLot).filter_by(owner_uid=OWNER, status="PENDING").all()
    db.close()
    assert len(pend) == 0


# ── Stale PENDING lots (no broker order id) ─────────────────────────────────

def test_stale_pending_lot_without_broker_id_swept():
    stale_id = _add_lot(status="PENDING", broker_entry_orderid="",
                        opened_at=datetime.utcnow() - timedelta(minutes=15))
    fresh_id = _add_lot(status="PENDING", broker_entry_orderid="",
                        client_ref="lot_fresh")
    tracked_id = _add_lot(status="PENDING", client_ref="lot_tracked",
                          opened_at=datetime.utcnow() - timedelta(minutes=15))
    noop = _order(norenordno="ZZZ", remarks="", status="REJECTED",
                  fillshares="0", avgprc="0")
    rec._reconcile_lots_from_orderbook_impl([noop], OWNER)
    assert _get(OrderLot, stale_id).status == "CANCELLED"
    assert _get(OrderLot, fresh_id).status == "PENDING"
    assert _get(OrderLot, tracked_id).status == "PENDING"


# ── Over-exit P&L clamp ─────────────────────────────────────────────────────

def test_over_exit_pnl_clamped_to_open_qty():
    # Lot has 60 open but the broker filled an exit of 100 — PnL must be
    # booked on 60, not 100.
    lot_id = _add_lot(status="OPEN", entry_qty=100, open_qty=60)
    ex_id = _add_exit(lot_id, exit_qty=60)
    order = _order(norenordno="ORD2", remarks="lotexitxyz", trantype="S",
                   status="COMPLETE", fillshares="100", avgprc="510.0")
    rec._reconcile_lots_from_orderbook_impl([order], OWNER)
    lot = _get(OrderLot, lot_id)
    ex = _get(LotExit, ex_id)
    assert lot.status == "CLOSED"
    assert lot.open_qty == 0
    assert lot.realized_pnl == pytest.approx((510.0 - 500.0) * 60)
    assert ex.filled_qty == 100  # broker truth preserved on the exit row


# ── Order timestamp sort key ────────────────────────────────────────────────

def test_order_time_key_tolerates_garbage():
    good = {"norentm": "10:00:01 12-07-2026"}
    later = {"norentm": "10:00:02 12-07-2026"}
    bad = {"norentm": "not-a-time"}
    assert rec._order_time_key(bad) < rec._order_time_key(good) < rec._order_time_key(later)


# ── Auto-rollover: far leg fires when the near exit fills ────────────────────

import asyncio  # noqa: E402

from db.models import RolloverIntent  # noqa: E402


class _StubAuth:
    user_id = OWNER
    auth_token = "tok"

    def is_authenticated(self):
        return True


def _add_rollover_intent(lot_id, exit_id, **kw) -> int:
    db = SessionLocal()
    defaults = dict(
        owner_uid=OWNER, lot_id=lot_id, exit_id=exit_id, exch="NSE",
        near_tsym="RELIANCE-EQ", far_tsym="RELIANCE-FAR", far_token="99999",
        side="B", product_type="M", price_type="MKT", entry_price=0.0,
        carry_target=False, near_target_enabled=False, near_target_value=0.0,
        full_roll=True, status="PENDING",
    )
    defaults.update(kw)
    it = RolloverIntent(**defaults)
    db.add(it)
    db.commit()
    iid = it.id
    db.close()
    return iid


@pytest.fixture
def _fake_far_order(monkeypatch):
    """Capture far-leg place_order calls and stub the auth/broker/WS bits the
    auto-fire path reaches out to."""
    import app.routers.orders as orders_mod
    import app.routers.targets as targets_mod

    calls = []

    async def fake_place_order(req):
        calls.append(req)
        return {"status": "success", "order_id": "FAR1", "lot_id": 999}

    async def fake_update_target(*a, **k):
        return None

    async def fake_broadcast(*a, **k):
        return None

    monkeypatch.setattr(orders_mod, "place_order", fake_place_order)
    monkeypatch.setattr(targets_mod, "update_symbol_target", fake_update_target)
    monkeypatch.setattr(rec._auth_module, "_auth", _StubAuth())
    monkeypatch.setattr(rec.ticker_manager, "_broadcast_order", fake_broadcast)
    monkeypatch.setattr(rec.ticker_manager, "broadcast_alert", fake_broadcast)
    return calls


def test_rollover_fires_when_exit_filled(_fake_far_order):
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id, status="FILLED", filled_qty=100, client_ref="lotexitA")
    iid = _add_rollover_intent(lot_id, ex_id)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    assert len(_fake_far_order) == 1
    req = _fake_far_order[0]
    assert req.tradingsymbol == "RELIANCE-FAR"
    assert req.quantity == 100          # rolls the actually-filled qty
    assert req.buy_or_sell == "B"
    it = _get(RolloverIntent, iid)
    assert it.status == "DONE"
    assert it.far_order_id == "FAR1"
    assert it.far_lot_id == 999


def test_rollover_not_fired_while_exit_pending(_fake_far_order):
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id, status="PENDING", filled_qty=0, client_ref="lotexitB")
    iid = _add_rollover_intent(lot_id, ex_id)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    assert len(_fake_far_order) == 0
    assert _get(RolloverIntent, iid).status == "PENDING"


def test_rollover_marked_failed_when_exit_rejected(_fake_far_order):
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id, status="REJECTED", filled_qty=0, client_ref="lotexitC")
    iid = _add_rollover_intent(lot_id, ex_id)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    assert len(_fake_far_order) == 0
    assert _get(RolloverIntent, iid).status == "FAILED"


def test_rollover_fires_exactly_once_across_passes(_fake_far_order):
    # Two reconcile passes (e.g. short-poll + WS event) must not double-place.
    lot_id = _add_lot(status="OPEN")
    ex_id = _add_exit(lot_id, status="FILLED", filled_qty=100, client_ref="lotexitD")
    _add_rollover_intent(lot_id, ex_id)

    asyncio.run(rec._fire_ready_rollovers(OWNER))
    asyncio.run(rec._fire_ready_rollovers(OWNER))

    assert len(_fake_far_order) == 1


def test_rollover_carries_pnl_onto_far_lot(monkeypatch):
    # Long entered at 500, near exit fills at 510 → +10/unit × 100 = +1000
    # (stub prcftr=1) should land on the far lot's carried_pnl.
    import app.routers.orders as orders_mod
    import app.routers.targets as targets_mod

    lot_id = _add_lot(status="OPEN", side="B", avg_entry_price=500.0)
    ex_id = _add_exit(lot_id, status="FILLED", filled_qty=100, avg_exit_price=510.0, client_ref="lotexitE")
    _add_rollover_intent(lot_id, ex_id, far_tsym="RELIANCE-FAR", carry_pnl=True)

    created = {}

    async def fake_place_order(req):
        db = SessionLocal()
        far = OrderLot(
            owner_uid=OWNER, exch="NSE", tsym=req.tradingsymbol, token="",
            lotsize=1, product_type="M", side=req.buy_or_sell,
            entry_qty=req.quantity, open_qty=req.quantity, avg_entry_price=0.0,
            status="PENDING", client_ref="farlotE",
        )
        db.add(far)
        db.commit()
        created["id"] = far.id
        db.close()
        return {"status": "success", "order_id": "FAR2", "lot_id": created["id"]}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(orders_mod, "place_order", fake_place_order)
    monkeypatch.setattr(targets_mod, "update_symbol_target", noop)
    monkeypatch.setattr(rec._auth_module, "_auth", _StubAuth())
    monkeypatch.setattr(rec.ticker_manager, "_broadcast_order", noop)
    monkeypatch.setattr(rec.ticker_manager, "broadcast_alert", noop)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    far = _get(OrderLot, created["id"])
    assert far.carried_pnl == pytest.approx((510.0 - 500.0) * 100)
    assert far.is_rollover is True   # far leg tagged with the ROLL badge


def test_rollover_carry_pnl_off_leaves_far_lot_flat(monkeypatch):
    import app.routers.orders as orders_mod
    import app.routers.targets as targets_mod

    lot_id = _add_lot(status="OPEN", side="B", avg_entry_price=500.0)
    ex_id = _add_exit(lot_id, status="FILLED", filled_qty=100, avg_exit_price=510.0, client_ref="lotexitF")
    _add_rollover_intent(lot_id, ex_id, far_tsym="RELIANCE-FAR", carry_pnl=False)

    created = {}

    async def fake_place_order(req):
        db = SessionLocal()
        far = OrderLot(
            owner_uid=OWNER, exch="NSE", tsym=req.tradingsymbol, token="",
            lotsize=1, product_type="M", side=req.buy_or_sell,
            entry_qty=req.quantity, open_qty=req.quantity, avg_entry_price=0.0,
            status="PENDING", client_ref="farlotF",
        )
        db.add(far)
        db.commit()
        created["id"] = far.id
        db.close()
        return {"status": "success", "order_id": "FAR3", "lot_id": created["id"]}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(orders_mod, "place_order", fake_place_order)
    monkeypatch.setattr(targets_mod, "update_symbol_target", noop)
    monkeypatch.setattr(rec._auth_module, "_auth", _StubAuth())
    monkeypatch.setattr(rec.ticker_manager, "_broadcast_order", noop)
    monkeypatch.setattr(rec.ticker_manager, "broadcast_alert", noop)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    far = _get(OrderLot, created["id"])
    assert far.carried_pnl == 0.0
    assert far.is_rollover is True   # tagged ROLL even when P&L carry is off


def test_rollover_far_leg_failure_restores_near_carry(monkeypatch):
    # Partial roll of a lot that already carries +10000 from a prior roll: roll
    # 600 of 1250, far entry rejected. Phase 1 moves 600/1250 of the carry off
    # the near lot; when the far leg fails, that share must be put back so the
    # +10000 isn't shown on neither lot.
    from fastapi import HTTPException
    import app.routers.orders as orders_mod
    import app.routers.targets as targets_mod

    lot_id = _add_lot(status="PARTIAL", side="B", avg_entry_price=500.0,
                      open_qty=650, entry_qty=1250, carried_pnl=10000.0)
    ex_id = _add_exit(lot_id, status="FILLED", filled_qty=600, avg_exit_price=510.0,
                      client_ref="lotexitG")
    _add_rollover_intent(lot_id, ex_id, far_tsym="RELIANCE-FAR",
                         carry_pnl=True, full_roll=False)

    async def failing_place_order(req):
        raise HTTPException(status_code=400, detail="entry rejected")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(orders_mod, "place_order", failing_place_order)
    monkeypatch.setattr(targets_mod, "update_symbol_target", noop)
    monkeypatch.setattr(rec._auth_module, "_auth", _StubAuth())
    monkeypatch.setattr(rec.ticker_manager, "_broadcast_order", noop)
    monkeypatch.setattr(rec.ticker_manager, "broadcast_alert", noop)

    asyncio.run(rec._fire_ready_rollovers(OWNER))

    # Far leg failed → full prior carry restored on the near lot.
    assert _get(OrderLot, lot_id).carried_pnl == pytest.approx(10000.0)


# ── Symbol target counts carried (rollover) P&L ─────────────────────────────

import app.services.targets as tgt  # noqa: E402
from app.core import state as app_state  # noqa: E402


def _reset_target_state():
    for d in (app_state._symbol_targets, app_state._target_positions, app_state._current_ltps,
              app_state._current_bids, app_state._current_asks, app_state._exchange_targets):
        d.clear()
    app_state._auto_exited_tokens.clear()
    app_state._exchange_target_exited.clear()


def _long_pos(exit_price, tsym="LEADTEST", exch="MCX"):
    # 100 long bought @500 today; total_pnl at exit_price = 100*exit_price − 50000
    # (so exit_price 550 → +5000).
    return {
        "netqty": 100, "netavgprc": 500.0, "buyavgprc": 500.0, "sellavgprc": 0.0,
        "totbuyavgprc": 500.0, "totsellavgprc": 0.0, "rpnl": 0.0, "urmtom": 0.0,
        "lp": exit_price, "upldprc": 0.0,
        "daybuyqty": 100, "daysellqty": 0, "daybuyavgprc": 500.0, "daysellavgprc": 0.0,
        "daybuyamt": 50000.0, "daysellamt": 0.0, "cfbuyqty": 0, "cfsellqty": 0,
        "tsym": tsym, "exch": exch, "prd": "M", "lotsize": 1, "prcftr": 1.0,
    }


def test_symbol_target_counts_carried_pnl(monkeypatch):
    # Contract P&L is only +5000 (< 10000 target), but +6000 carried from a
    # prior roll pushes the running total to 11000 → the target must fire.
    _reset_target_state()
    key = "MCX|TOK1"
    app_state._target_positions[key] = [_long_pos(550.0)]
    app_state._symbol_targets[key] = {"enabled": True, "target_value": 10000.0, "carried_pnl": 6000.0}
    calls = []

    async def fake_exit(pos_map, remarks):
        calls.append(remarks)
        return {k: True for k in pos_map}

    monkeypatch.setattr(tgt, "_place_exit_orders", fake_exit)
    # Bypass the session-window gate so the target logic is what's under test,
    # not the wall-clock at test time.
    monkeypatch.setattr(tgt, "is_market_open_for_orders", lambda exch, now=None: True)
    asyncio.run(tgt._on_target_tick({"e": "MCX", "tk": "TOK1", "lp": "550", "bp1": "550"}))
    assert calls == ["auto_exit_target"]
    _reset_target_state()


def test_symbol_target_carried_below_threshold_does_not_fire(monkeypatch):
    _reset_target_state()
    key = "MCX|TOK2"
    app_state._target_positions[key] = [_long_pos(550.0)]
    app_state._symbol_targets[key] = {"enabled": True, "target_value": 10000.0, "carried_pnl": 3000.0}
    calls = []

    async def fake_exit(pos_map, remarks):
        calls.append(remarks)
        return {k: True for k in pos_map}

    monkeypatch.setattr(tgt, "_place_exit_orders", fake_exit)
    monkeypatch.setattr(tgt, "is_market_open_for_orders", lambda exch, now=None: True)
    asyncio.run(tgt._on_target_tick({"e": "MCX", "tk": "TOK2", "lp": "550", "bp1": "550"}))
    assert calls == []  # 5000 + 3000 = 8000 < 10000
    _reset_target_state()

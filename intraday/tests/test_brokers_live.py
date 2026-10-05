"""GatewayBroker fill confirmation (offline, scripted Gateway client).

A placement ack is NOT a fill: place() must confirm against the order book and
return FILLED only for a broker-confirmed fill. Anything still working after
the window is cancelled; leftovers return TIMEOUT/WORKING, never a phantom
fill the loop would then (mis)manage.
"""
from intraday.brokers import GatewayBroker
from intraday.contracts import OrderIntent


def _intent(**kw):
    d = dict(symbol="NIFTY29SEP26C24800", side="BUY", qty=50,
             order_type="MARKETABLE_LIMIT", limit_px=100.0, exch="NFO",
             underlying="NIFTY", strike=24800, right="CE", lot_size=50)
    d.update(kw)
    return OrderIntent(**d)


class ScriptedClient:
    """place_order -> fixed ack; order_book -> one scripted row per call."""
    def __init__(self, ack, books):
        self._ack, self._books = ack, list(books)
        self.cancelled: list[str] = []
        self.placed: list[dict] = []

    def place_order(self, **kw):
        self.placed.append(kw)
        return self._ack

    def order_book(self):
        if len(self._books) > 1:
            return self._books.pop(0)
        return self._books[0]

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"status": "cancelled"}


def _row(status, fill="50", avg="101.5", oid="NOR123"):
    return [{"norenordno": oid, "status": status, "fillshares": fill,
             "avgprc": avg, "qty": "50", "rejreason": ""}]


def _broker(client):
    return GatewayBroker(client, confirm_timeout_s=0.3, poll_s=0.2)


def test_confirmed_fill_returns_filled():
    b = _broker(ScriptedClient({"status": "success", "order_id": "NOR123"},
                               [_row("COMPLETE")]))
    r = b.place(_intent())
    assert r["status"] == "FILLED" and r["fill_px"] == 101.5
    assert r["filled_qty"] == 50 and r["broker_order_id"] == "NOR123"


def test_open_then_complete_polls_to_filled():
    c = ScriptedClient({"status": "success", "order_id": "NOR123"},
                       [_row("OPEN", fill="0", avg="0"), _row("COMPLETE")])
    r = _broker(c).place(_intent())
    assert r["status"] == "FILLED" and r["filled_qty"] == 50
    assert c.cancelled == []                       # filled: nothing to cancel


def test_rejection_carries_broker_reason():
    rows = [{"norenordno": "NOR123", "status": "REJECTED", "fillshares": "0",
             "avgprc": "0", "qty": "50", "rejreason": "RMS: margin short"}]
    r = _broker(ScriptedClient({"status": "success", "order_id": "NOR123"},
                               [rows])).place(_intent())
    assert r["status"] == "REJECTED" and "margin short" in r["reason"]


def test_working_order_is_cancelled_then_timeout():
    c = ScriptedClient({"status": "success", "order_id": "NOR123"},
                       [_row("OPEN", fill="0", avg="0")])
    r = _broker(c).place(_intent())
    assert r["status"] == "TIMEOUT"
    assert c.cancelled == ["NOR123"]               # no untracked live order


def test_partial_before_cancel_tracks_filled_shares():
    rows = [{"norenordno": "NOR123", "status": "CANCELLED", "fillshares": "20",
             "avgprc": "100.5", "qty": "50", "rejreason": ""}]
    r = _broker(ScriptedClient({"status": "success", "order_id": "NOR123"},
                               [rows])).place(_intent())
    assert r["status"] == "FILLED" and r["filled_qty"] == 20
    assert r["fill_px"] == 100.5


def test_ack_without_order_id_rejects():
    r = _broker(ScriptedClient({"status": "success"}, [[]])).place(_intent())
    assert r["status"] == "REJECTED" and r["broker_order_id"] == ""


def test_limit_intent_passes_price_through():
    c = ScriptedClient({"status": "success", "order_id": "NOR123"},
                       [_row("COMPLETE")])
    _broker(c).place(_intent(order_type="LIMIT", limit_px=99.5))
    assert c.placed[0]["price_type"] == "LMT" and c.placed[0]["price"] == 99.5
    assert c.placed[0]["buy_or_sell"] == "B"

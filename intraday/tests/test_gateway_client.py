"""Gateway client shape handling (offline): the broker-positions envelope."""
from intraday.gateway_client.client import flatten_positions


def _row(tsym="NIFTY29SEP26C24800"):
    return {"tsym": tsym, "exch": "NFO", "prd": "I", "netqty": "50",
            "buyavgprc": "100.4", "sellavgprc": "0", "lp": "120.0",
            "lotsize": "50", "token": "1"}


def test_flatten_positions_accepts_grouped_envelope():
    payload = {"symbol_groups": [
        {"symbol": "NIFTY", "exchange": "NFO", "symbol_pnl": 980.0,
         "positions": [_row(), _row("NIFTY29SEP26P24800")]},
        {"symbol": "EMPTY", "exchange": "NFO", "symbol_pnl": 0.0,
         "positions": []},
    ], "total_pnl": 980.0}
    rows = flatten_positions(payload)
    assert len(rows) == 2 and rows[0]["netqty"] == "50"


def test_flatten_positions_accepts_flat_shapes():
    assert flatten_positions([_row()])[0]["tsym"].startswith("NIFTY")
    assert flatten_positions({"positions": [_row()]})[0]["lp"] == "120.0"


def test_flatten_positions_rejects_garbage():
    assert flatten_positions(None) == []
    assert flatten_positions({"symbol_groups": "nope"}) == []
    assert flatten_positions({"unexpected": 1}) == []
    assert flatten_positions([_row(), "junk", None])[0]["exch"] == "NFO"

"""GatewayChainsContext reads OptionSmith's chains.db (coexistence-safe) and
pulls bars/vix/prev-day via the gateway REST client (mocked). No OI sweep."""
import json
import sqlite3
import time

from intraday.data.chains_db import GatewayChainsContext
from intraday.options import greeks_for
from intraday.options.mathx import bs_price


def _make_chains_db(path, symbol="RELIANCE", spot=2900.0, iv=0.22, lot=250):
    """Write one chain_snapshot row in OptionSmith's exact format."""
    import datetime as dt
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE chain_snapshot(
        symbol TEXT PRIMARY KEY, expiry TEXT, spot REAL, lot_size INTEGER,
        built_at REAL, quote_span_s REAL, source TEXT, carry_rate REAL,
        quarantined INTEGER, gate_ok INTEGER, gate_errors INTEGER, gate_warns INTEGER,
        verdict TEXT, legs TEXT, stored_at REAL, exchange TEXT);""")
    expiry = (dt.date.today() + dt.timedelta(days=5))
    atm = round(spot / 20) * 20
    legs = []
    for i in range(-5, 6):
        k = atm + i * 20
        for r, is_call in (("C", True), ("P", False)):
            mid = max(bs_price(is_call, spot, k, 5 / 365, iv, 0.07), 0.05)
            legs.append({"k": k, "r": r, "ltp": round(mid, 2),
                         "b": round(mid * 0.99, 2), "a": round(mid * 1.01, 2),
                         "oi": 100000 + i * 1000, "poi": 95000, "ft": time.time(),
                         "iv": iv, "v": 5000, "bq": 250, "sq": 250,
                         "pc": round(mid, 2)})
    con.execute("INSERT INTO chain_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, expiry.isoformat(), spot, lot, time.time(), 2.0,
                 "gateway:NFO", 0.07, 0, 1, 0, 0, "ok", json.dumps(legs),
                 time.time(), "NFO"))
    con.commit(); con.close()


class FakeClient:
    def search(self, q, exchange="NSE"):
        return [{"token": "2885", "exch": "NSE", "tsym": q}]

    def candles(self, exch, token, *, interval=5, lookback_minutes=0,
                daily=False, days=30, tradingsymbol=""):
        if daily:
            return {"candles": [{"time": str(i), "open": 2880, "high": 2920,
                                 "low": 2870, "close": 2900, "volume": 1e6}
                                for i in range(5)]}
        base = 2880.0
        return {"candles": [{"time": str(i), "open": base + i, "high": base + i + 5,
                             "low": base + i - 5, "close": base + i + 2,
                             "volume": 10000} for i in range(60)]}

    def quote(self, exch, token):
        return {"lp": "13.4"}


def test_reads_chain_from_optionsmith_db(tmp_path):
    db = tmp_path / "chains.db"
    _make_chains_db(str(db))
    cfg = {"universe": ["RELIANCE"], "chains_db_path": str(db),
           "bar_timeframe": "5m", "chains_max_age_s": 300}
    ctx = GatewayChainsContext(FakeClient(), cfg)

    ch = ctx.chain("RELIANCE")
    assert ch is not None and ch.symbol == "RELIANCE" and ch.lot_size == 250
    assert len(ch.quotes) == 22 and ch.carry_rate == 0.07
    atm_call = ch.get(ch.atm, True)
    assert atm_call.iv == 0.22                        # IV preserved from store
    # tradingsymbol synthesised for order routing (scripmaster-exact format)
    assert atm_call.tsym.startswith("RELIANCE") and "C" in atm_call.tsym
    g = greeks_for(atm_call, ch)
    assert g["delta"] > 0 and g["gamma"] > 0          # greeks computed on read

    assert ctx.spot("RELIANCE") == 2900.0
    b = ctx.bars("RELIANCE")
    assert b is not None and "vwap" in b and len(b) == 60
    assert ctx.vix() == 13.4
    pd_ = ctx.prev_day("RELIANCE")
    assert pd_["pdh"] == 2920 and pd_["pdc"] == 2900
    # d_pcr tracked across calls
    ctx.positioning("RELIANCE")
    assert "d_pcr" in ctx.positioning("RELIANCE")


def test_stale_or_missing_row_returns_none(tmp_path):
    db = tmp_path / "chains.db"
    _make_chains_db(str(db))
    # force staleness
    con = sqlite3.connect(str(db))
    con.execute("UPDATE chain_snapshot SET stored_at=?", (time.time() - 9999,))
    con.commit(); con.close()
    ctx = GatewayChainsContext(FakeClient(),
                               {"chains_db_path": str(db), "chains_max_age_s": 120})
    assert ctx.chain("RELIANCE") is None              # too old -> refuse
    assert ctx.chain("INFY") is None                  # absent -> None

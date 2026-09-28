"""Option-chain builder tests.

The chain feeds a pricing model (the OptionSmith advisor), so the failures that
matter here are the silent ones — a leg carrying another instrument's price, a
window centred on nothing, an OI delta invented out of a missing snapshot. Each
of those produces a well-formed response that is simply wrong.

Run:
    ./venv/bin/pytest tests/test_option_chain.py -v
"""
import asyncio
import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_oc.db")

import pytest

import brokers.shoonya.option_chain as oc
from brokers.shoonya import oi_history
from brokers.shoonya.adapter import ShoonyaBroker, _QUOTE_VERIFY_ATTEMPTS
from brokers.base import BrokerError
from db.engine import Base, engine
import db.models  # noqa: F401 — registers all tables before create_all


# ---------------------------------------------------------------- fixtures
SPOT = 1320.0
UNDERLYING = ("NSE", "2885")


class FakeScripmaster:
    """Minimal stand-in: NSE RELIANCE-EQ plus a 5-strike NFO option grid."""

    def __init__(self):
        self.master = {"NSE|RELIANCE-EQ": {
            "exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ",
            "sym": "RELIANCE", "instrumenttype": "EQ", "lotsize": "1"}}
        rows = []
        for i, k in enumerate([1300, 1310, 1320, 1330, 1340, 1350, 1360]):
            for right in ("CE", "PE"):
                rows.append({
                    "exch": "NFO", "token": f"{right[0]}{i}",
                    "tsym": f"RELIANCE25AUG26{right[0]}{k}", "sym": "RELIANCE",
                    "instrumenttype": "OPTSTK", "expd": "25-AUG-2026",
                    "opttype": right, "strikeprice": str(k),
                    "lotsize": "500", "ticksize": "0.05"})
        # a second expiry, so expiry selection has something to choose between
        rows.append({"exch": "NFO", "token": "Z1", "tsym": "RELIANCE29SEP26C1320",
                     "sym": "RELIANCE", "instrumenttype": "OPTSTK",
                     "expd": "29-SEP-2026", "opttype": "CE",
                     "strikeprice": "1320", "lotsize": "500", "ticksize": "0.05"})
        rows.append({"exch": "NFO", "token": "Z2", "tsym": "RELIANCE29SEP26P1320",
                     "sym": "RELIANCE", "instrumenttype": "OPTSTK",
                     "expd": "29-SEP-2026", "opttype": "PE",
                     "strikeprice": "1320", "lotsize": "500", "ticksize": "0.05"})
        self.sym_index = {"reliance": rows}
        for r in rows:
            self.master[f"NFO|{r['tsym']}"] = r

    def is_loaded(self):
        return True


class FakeBroker:
    """Records every token asked for; answers with that token's own quote."""

    def __init__(self):
        self.asked = []

    async def getQuote(self, session, creds, symbol, exchange):
        self.asked.append((exchange, symbol))
        if (exchange, symbol) == UNDERLYING:
            return {"token": "2885", "tsym": "RELIANCE-EQ", "lp": str(SPOT)}
        return {"token": symbol, "tsym": f"T{symbol}", "lp": "12.5",
                "bp1": "12.4", "sp1": "12.6", "bq1": "500", "sq1": "500",
                "oi": "7000", "v": "1000", "c": "11.0", "ls": "500", "ti": "0.05"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(oc, "get_scripmaster", FakeScripmaster)
    oc._cache.clear()
    oc._cache_locks.clear()
    yield


def build(broker=None, **kw):
    broker = broker or FakeBroker()
    kw.setdefault("symbol", "RELIANCE")
    kw.setdefault("exchange", "NSE")
    kw.setdefault("count", 2)
    kw.setdefault("use_cache", False)
    return broker, asyncio.run(oc.build_option_chain(broker, None, {}, **kw))


# ---------------------------------------------------------------- the chain
def test_nse_symbol_maps_to_nfo_options():
    _, d = build()
    assert d["exchange"] == "NFO"


def test_spot_is_resolved_from_the_underlying():
    """Without it the ATM window has no centre and every strike gets fetched."""
    broker, d = build()
    assert d["spot"] == SPOT
    assert UNDERLYING in broker.asked
    assert d["underlying_exchange"], d["underlying_token"] == UNDERLYING


def test_window_is_centred_on_the_resolved_atm():
    _, d = build(count=1)
    assert [r["strike"] for r in d["chain"]] == [1310.0, 1320.0, 1330.0]


def test_caller_supplied_atm_overrides_the_quote():
    _, d = build(count=1, atm=1350.0)
    assert [r["strike"] for r in d["chain"]] == [1340.0, 1350.0, 1360.0]


def test_expiry_is_reported_in_both_formats():
    _, d = build()
    assert d["expiry"] == "25-AUG-2026"
    assert d["expiry_iso"] == "2026-08-25"
    assert d["expiries_iso"][:2] == ["2026-08-25", "2026-09-29"]


def test_expiries_are_ordered_chronologically_not_alphabetically():
    """'25-AUG-2026' sorts before '29-SEP-2026' by date but after it by string."""
    _, d = build()
    assert d["expiries"] == ["25-AUG-2026", "29-SEP-2026"]


def test_a_later_expiry_can_be_selected():
    _, d = build(expiry="29-SEP-2026", count=1)
    assert d["expiry"] == "29-SEP-2026"
    assert [r["strike"] for r in d["chain"]] == [1320.0]


def test_legs_carry_the_executable_book_not_just_ltp():
    _, d = build(count=1)
    leg = d["chain"][0]["CE"]
    assert (leg["bid"], leg["ask"]) == (12.4, 12.6)
    assert leg["ltp"] == 12.5 and leg["oi_num"] == 7000
    assert leg["lot_size"] == 500 and leg["quoted"] is True


def test_lot_size_reaches_the_top_level():
    _, d = build()
    assert d["lot_size"] == 500


def test_unknown_symbol_reports_an_error_not_an_empty_chain():
    _, d = build(symbol="NOSUCH")
    assert d["chain"] == [] and "No options found" in d["error"]


# ---------------------------------------------------------------- quality
def test_a_leg_that_cannot_be_quoted_is_marked_unquoted():
    """Better an explicit gap than a zero that reads as a free option."""
    class Failing(FakeBroker):
        async def getQuote(self, session, creds, symbol, exchange):
            if symbol == "C2":                      # the 1320 call
                raise BrokerError("no quote", broker="shoonya")
            return await super().getQuote(session, creds, symbol=symbol,
                                          exchange=exchange)

    _, d = build(broker=Failing(), count=0)
    leg = d["chain"][0]["CE"]
    assert leg["quoted"] is False
    assert leg["ltp"] == 0.0 and leg["bid"] == 0.0
    assert d["quality"]["quoted"] < d["quality"]["legs"]


def test_quality_counts_what_actually_arrived():
    _, d = build(count=1)
    q = d["quality"]
    assert q["legs"] == 6 and q["quoted"] == 6


# ---------------------------------------------------------------- OI history
def test_prev_oi_is_zero_until_a_previous_day_exists():
    _, d = build(count=1)
    assert all(r[s]["prev_oi"] == 0 for r in d["chain"] for s in ("CE", "PE"))


def test_prev_oi_comes_from_the_most_recent_earlier_day():
    from db.engine import SessionLocal
    from db.models import OptionOISnapshot
    db = SessionLocal()
    # two earlier days: the NEARER one must win
    db.add(OptionOISnapshot(exch="NFO", token="C2", trade_date="2020-01-01", oi=111))
    db.add(OptionOISnapshot(exch="NFO", token="C2", trade_date="2020-06-01", oi=222))
    db.commit()
    db.close()

    _, d = build(count=0)
    assert d["chain"][0]["CE"]["prev_oi"] == 222
    assert d["quality"]["prev_oi_known"] == 1


def test_todays_snapshot_is_not_served_as_its_own_previous():
    """Otherwise every OI delta collapses to zero and 'no change' is asserted
    on a day where nothing is actually known."""
    oi_history.record("NFO", {"C2": 5000})
    _, d = build(count=0)
    assert d["chain"][0]["CE"]["prev_oi"] == 0


def test_a_fetch_records_todays_oi():
    build(count=0)
    from db.engine import SessionLocal
    from db.models import OptionOISnapshot
    db = SessionLocal()
    rows = db.query(OptionOISnapshot).filter_by(trade_date=oi_history.today_ist()).all()
    db.close()
    assert {r.token for r in rows} == {"C2", "P2"}
    assert all(r.oi == 7000 for r in rows)


# ---------------------------------------------------------------- caching
def test_repeat_requests_inside_the_ttl_reuse_the_build():
    broker = FakeBroker()
    asyncio.run(oc.build_option_chain(broker, None, {}, symbol="RELIANCE",
                                      exchange="NSE", count=1))
    n = len(broker.asked)
    d = asyncio.run(oc.build_option_chain(broker, None, {}, symbol="RELIANCE",
                                          exchange="NSE", count=1))
    assert d["cached"] is True and len(broker.asked) == n


def test_use_cache_false_forces_a_live_rebuild():
    broker = FakeBroker()
    asyncio.run(oc.build_option_chain(broker, None, {}, symbol="RELIANCE",
                                      exchange="NSE", count=1))
    n = len(broker.asked)
    d = asyncio.run(oc.build_option_chain(broker, None, {}, symbol="RELIANCE",
                                          exchange="NSE", count=1, use_cache=False))
    assert not d.get("cached") and len(broker.asked) > n


# ------------------------------------------------- misrouted-quote guard
class _MisroutingApi:
    """Shoonya's observed defect: answer with the UNDERLYING's quote instead of
    the option's, for the first `bad` calls."""

    def __init__(self, bad):
        self.bad = bad
        self.calls = 0

    def get_quotes(self, exchange, token):
        self.calls += 1
        if self.calls <= self.bad:
            return {"stat": "Ok", "token": "2885", "tsym": "RELIANCE-EQ",
                    "lp": "1320.00"}
        return {"stat": "Ok", "token": token, "tsym": "OPT", "lp": "12.5"}


def _broker_with(api):
    b = ShoonyaBroker.__new__(ShoonyaBroker)      # skip __init__'s env checks
    b._api = api
    b._inject_oauth = lambda *a, **k: None
    return b


def test_a_misrouted_quote_is_retried_until_it_matches():
    api = _MisroutingApi(bad=2)
    q = _broker_with(api)._get_quote_sync(None, {}, "141832", "NFO")
    assert q["token"] == "141832"
    assert api.calls == 3


def test_a_persistently_misrouted_quote_raises_rather_than_lying():
    """The wrong instrument's price is well-formed and undetectable downstream,
    so returning it would corrupt every structure built on that leg."""
    api = _MisroutingApi(bad=99)
    with pytest.raises(BrokerError, match="kept returning token"):
        _broker_with(api)._get_quote_sync(None, {}, "141832", "NFO")
    assert api.calls == _QUOTE_VERIFY_ATTEMPTS


def test_a_correct_quote_costs_no_extra_calls():
    api = _MisroutingApi(bad=0)
    _broker_with(api)._get_quote_sync(None, {}, "141832", "NFO")
    assert api.calls == 1


def test_a_response_without_a_token_is_still_accepted():
    """Some endpoints legitimately omit it; only a token naming a DIFFERENT
    instrument counts as misrouted."""
    class NoToken:
        def get_quotes(self, exchange, token):
            return {"stat": "Ok", "lp": "12.5"}
    assert _broker_with(NoToken())._get_quote_sync(None, {}, "1", "NFO")["lp"] == "12.5"

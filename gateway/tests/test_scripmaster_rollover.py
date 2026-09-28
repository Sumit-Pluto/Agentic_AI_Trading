"""Unit tests for the rollover next-expiry resolver (ScriptMaster.next_expiries).

Uses a tiny fixture CSV with the real column layout so we don't depend on the
9 MB production scripmaster. Covers the LEAD futures roll (the motivating case),
the LEAD/LEADMINI separation, index futures (FUTIDX), option exclusion, the
last-expiry boundary, and unknown contracts.
"""
import pytest

from brokers.shoonya.scripmaster import ScriptMaster

# exch,token,tsym,sym,instrumenttype,expd,opttype,strikeprice,lotsize,ticksize,prcftr
FIXTURE_CSV = """exch,token,tsym,sym,instrumenttype,expd,opttype,strikeprice,lotsize,ticksize,prcftr
MCX,562049,LEAD31JUL26,LEAD,FUTCOM,31-JUL-2026,,0,5,0.05,1000
MCX,568833,LEAD31AUG26,LEAD,FUTCOM,31-AUG-2026,,0,5,0.05,1000
MCX,571300,LEAD30SEP26,LEAD,FUTCOM,30-SEP-2026,,0,5,0.05,1000
MCX,562050,LEADMINI31JUL26,LEADMINI,FUTCOM,31-JUL-2026,,0,1,0.05,1000
MCX,568832,LEADMINI31AUG26,LEADMINI,FUTCOM,31-AUG-2026,,0,1,0.05,1000
MCX,90001,LEAD31AUG26CE,LEAD,OPTFUT,31-AUG-2026,CE,190,5,0.05,1000
NFO,53001,NIFTY28JUL26F,NIFTY,FUTIDX,28-JUL-2026,,0,50,0.05,1
NFO,53002,NIFTY25AUG26F,NIFTY,FUTIDX,25-AUG-2026,,0,50,0.05,1
"""


@pytest.fixture
def sm(tmp_path):
    csv_path = tmp_path / "scripmaster.csv"
    csv_path.write_text(FIXTURE_CSV)
    return ScriptMaster(csv_path=csv_path)


def _tsyms(rows):
    return [r["tsym"] for r in rows]


def test_lead_rolls_to_later_expiries_in_order(sm):
    rows = sm.next_expiries("MCX", "LEAD31JUL26")
    # AUG then SEP, nearest-first; the option row is excluded (wrong type),
    # and LEADMINI never leaks in (different underlying).
    assert _tsyms(rows) == ["LEAD31AUG26", "LEAD30SEP26"]
    assert all(r["sym"] == "LEAD" for r in rows)
    assert all(r["instrumenttype"] == "FUTCOM" for r in rows)


def test_leadmini_is_isolated_from_lead(sm):
    assert _tsyms(sm.next_expiries("MCX", "LEADMINI31JUL26")) == ["LEADMINI31AUG26"]
    # And LEAD does not surface any LEADMINI contract.
    assert not any(r["sym"] == "LEADMINI" for r in sm.next_expiries("MCX", "LEAD31JUL26"))


def test_last_listed_expiry_has_no_roll_target(sm):
    assert sm.next_expiries("MCX", "LEAD30SEP26") == []


def test_unknown_contract_returns_empty(sm):
    assert sm.next_expiries("MCX", "BOGUS99XYZ26") == []


def test_index_futures_futidx_path(sm):
    assert _tsyms(sm.next_expiries("NFO", "NIFTY28JUL26F")) == ["NIFTY25AUG26F"]


def test_options_are_never_roll_targets(sm):
    # LEAD31AUG26CE shares the LEAD underlying and a later expiry, but is an
    # option — it must not appear as a futures roll target.
    assert not any(r["opttype"] for r in sm.next_expiries("MCX", "LEAD31JUL26"))

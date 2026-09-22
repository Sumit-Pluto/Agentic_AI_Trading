"""Merged corporate calendar.

Sources: indianapi (ipo / corporate_actions / recent_announcements),
macro_cal.get_av_calendars() (Alpha Vantage earnings + IPO CSV), and — when
the package is importable — nsepython board-meeting events (skipped silently
otherwise).

Return shape: {'ipo': [], 'results': [], 'dividends': [], 'actions': []}
where each item is {date, symbol, detail, source}. Dividends / results are
routed out of corporate-action & announcement text by keyword (dividend /
result / board meeting / bonus / split / ...).
"""

from __future__ import annotations

import os

try:
    from . import indianapi as _iapi
    from . import macro_cal as _mcal
    from .indianapi import _first
except ImportError:  # direct script execution
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import indianapi as _iapi
    import macro_cal as _mcal
    from indianapi import _first

HEALTH: dict = {}

_DIVIDEND_KW = ("dividend",)
_RESULT_KW = ("result", "board meeting", "earnings", "quarterly",
              "financial statement")
_ACTION_KW = ("bonus", "split", "buyback", "buy back", "rights issue",
              "merger", "amalgamation", "demerger", "delisting",
              "agm", "egm")


def classify(text: str | None) -> str:
    """Route purpose/subject text -> 'dividends' | 'results' | 'actions'."""
    t = (text or "").lower()
    if any(k in t for k in _DIVIDEND_KW):
        return "dividends"
    if any(k in t for k in _RESULT_KW):
        return "results"
    return "actions"


def _item(date, symbol, detail, source) -> dict:
    return {"date": str(date)[:10] if date else None,
            "symbol": str(symbol) if symbol else None,
            "detail": str(detail) if detail else "",
            "source": source}


def _from_indianapi_items(items, source) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        raw = it.get("raw") if isinstance(it.get("raw"), dict) else it
        text = " ".join(str(x) for x in (
            it.get("summary"), it.get("title"),
            _first(raw, "purpose", "subject", "desc")) if x)
        out.append((classify(text),
                    _item(it.get("date"),
                          it.get("symbol") or it.get("title"),
                          it.get("summary") or it.get("title") or "",
                          source)))
    return out


def _nsepython_events() -> list[dict]:
    """Optional nsepython board-meeting/event calendar; silent on any issue."""
    try:
        import nsepython  # noqa: F401
    except Exception:
        return []
    rows = []
    try:
        fn = getattr(nsepython, "nse_events", None)
        if not callable(fn):
            return []
        df = fn()
        if hasattr(df, "to_dict"):
            rows = df.to_dict("records")
        elif isinstance(df, list):
            rows = [r for r in df if isinstance(r, dict)]
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            out.append({"date": _first(r, "date", "bm_date", "meetingdate"),
                        "symbol": _first(r, "symbol", "bm_symbol"),
                        "purpose": _first(r, "purpose", "bm_purpose",
                                          "bm_desc", "desc") or ""})
        except Exception:
            continue
    return out


_NSE_SYMS: set | None = None


def _nse_symbol_set() -> set:
    """NSE symbol universe for filtering foreign rows out of Indian panels.
    Empty set when the scrip master is unavailable — callers then DROP
    unverifiable rows (AV earnings are US-listed; dropping is correct)."""
    global _NSE_SYMS
    if _NSE_SYMS is None:
        syms: set = set()
        try:
            from shoonya_client import load_scripmaster
            for tsym, row in load_scripmaster("NSE").items():
                if tsym.endswith("-EQ"):
                    syms.add(tsym[:-3].upper())
        except Exception:
            pass
        _NSE_SYMS = syms
    return _NSE_SYMS


def _dedupe_sort(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = (it.get("date"), it.get("symbol"),
             (it.get("detail") or "")[:48].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    out.sort(key=lambda x: (x.get("date") or "9999-99-99",
                            x.get("symbol") or ""))
    return out


def build(ipo=None, corporate_actions=None, announcements=None, av=None,
          nse_events=None) -> dict:
    """Pure merge of pre-fetched inputs (fixture-friendly, no I/O)."""
    cal = {"ipo": [], "results": [], "dividends": [], "actions": []}

    for it in ipo or []:
        if isinstance(it, dict):
            cal["ipo"].append(_item(it.get("date"),
                                    it.get("symbol") or it.get("title"),
                                    it.get("summary") or it.get("title")
                                    or "IPO", "indianapi"))

    for bucket, item in _from_indianapi_items(corporate_actions,
                                              "indianapi"):
        cal[bucket].append(item)
    for bucket, item in _from_indianapi_items(announcements, "indianapi"):
        cal[bucket].append(item)

    av = av or {}
    # Alpha Vantage's earnings calendar is US-listed companies (thousands of
    # rows) — wrong panel for the INDIAN corporate calendar. Keep AV earnings
    # only for symbols that exist on NSE (rarely any); Indian results come
    # from NSE announcements/indianapi instead.
    nse_syms = _nse_symbol_set()
    for r in av.get("results") or []:
        if isinstance(r, dict):
            sym = str(r.get("symbol") or "").upper()
            if sym not in nse_syms:      # empty universe → drop all (US rows)
                continue
            cal["results"].append(_item(r.get("date"), r.get("symbol"),
                                        r.get("detail") or "earnings",
                                        r.get("source") or "alphavantage"))
    for r in av.get("ipo") or []:
        if isinstance(r, dict):
            cal["ipo"].append(_item(r.get("date"), r.get("symbol"),
                                    r.get("detail") or "IPO",
                                    r.get("source") or "alphavantage"))

    for r in nse_events or []:
        if not isinstance(r, dict):
            continue
        purpose = r.get("purpose") or ""
        cal[classify(purpose)].append(
            _item(r.get("date"), r.get("symbol"),
                  purpose or "board meeting", "nsepython"))

    return {k: _dedupe_sort(v) for k, v in cal.items()}


def get_corporate_calendar(indian: "_iapi.IndianAPI | None" = None,
                           av: dict | None = None,
                           nse_events: list | None = None,
                           symbols: list[str] | None = None) -> dict:
    """Live entry point. Every source is individually failure-isolated.

    symbols: optional watchlist for per-stock Indian API enrichment
    (corporate_actions / recent_announcements are per-STOCK endpoints —
    each symbol costs 2 budget requests, so callers pass a short list or
    nothing; the market-wide calendar comes from NSE events + AV + IPO)."""
    ipo, actions, ann = [], [], []
    try:
        indian = indian if indian is not None else _iapi.client()
        ipo = indian.ipo()
        for sym in (symbols or [])[:10]:      # hard cap: 20 budget requests
            actions.extend(indian.corporate_actions(stock_name=sym) or [])
            ann.extend(indian.recent_announcements(stock_name=sym) or [])
        _mcal._mark(HEALTH, "corp_cal.indianapi", True)
    except Exception as e:  # client itself already guards; belt & braces
        _mcal._mark(HEALTH, "corp_cal.indianapi", False,
                    f"{type(e).__name__}: {e}")
    if av is None:
        try:
            av = _mcal.get_av_calendars()
        except Exception as e:
            _mcal._mark(HEALTH, "corp_cal.alphavantage", False,
                        f"{type(e).__name__}: {e}")
            av = {"results": [], "ipo": []}
    if nse_events is None:
        nse_events = _nsepython_events()
    return build(ipo=ipo, corporate_actions=actions, announcements=ann,
                 av=av, nse_events=nse_events)


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="corp_cal_test_")

    def fake_fetch(ep, params):
        if ep == "ipo":
            return {"upcoming": [{"name": "Acme Ltd", "symbol": "ACME",
                                  "bidding_start_date": "2026-07-10"}]}
        if ep == "corporate_actions":
            return [
                {"symbol": "INFY", "purpose": "Interim Dividend Rs 20",
                 "ex_date": "2026-07-08"},
                {"symbol": "TCS", "purpose":
                 "Board Meeting to consider Q1 Results",
                 "ex_date": "2026-07-09"},
                {"symbol": "WIPRO", "purpose": "Bonus issue 1:1",
                 "ex_date": "2026-07-11"},
                {"symbol": "IRCTC", "purpose": "Stock Split 1:5",
                 "ex_date": "2026-07-12"},
                {"symbol": "XYZ", "purpose": "AGM",
                 "ex_date": "2026-07-13"},
            ]
        if ep == "recent_announcements":
            return {"announcements": [
                {"symbol": "HDFCBANK",
                 "subject": "Outcome of board meeting - financial results",
                 "an_dt": "2026-07-03"},
                {"symbol": "SBIN", "subject": "Record date for dividend",
                 "an_dt": "2026-07-02"},
            ]}
        return []

    indian = _iapi.IndianAPI(
        api_key="test", fetcher=fake_fetch, health={},
        budget=_iapi.Budget(os.path.join(tmp, "b.json"), cap=480),
        cache_path=os.path.join(tmp, "c.json"))

    av_fixture = {
        "results": [{"date": "2026-07-15", "symbol": "INFY",
                     "detail": "Infosys Ltd; estimate=0.23",
                     "source": "alphavantage"}],
        "ipo": [{"date": "2026-07-20", "symbol": "ACMEUS",
                 "detail": "Acme US", "source": "alphavantage"}],
    }
    nse_fixture = [
        {"date": "2026-07-09", "symbol": "TCS",
         "purpose": "Financial Results"},          # dup-ish of indianapi row
        {"date": "2026-07-14", "symbol": "RELIANCE",
         "purpose": "Buyback of equity shares"},
    ]

    # pin the NSE universe so the AV filter is deterministic (no network)
    _NSE_SYMS = {"INFY", "TCS", "HDFCBANK", "SBIN", "RELIANCE"}

    cal = get_corporate_calendar(indian=indian, av=av_fixture,
                                 nse_events=nse_fixture,
                                 symbols=["INFY"])   # per-stock enrichment

    assert sorted(cal.keys()) == ["actions", "dividends", "ipo", "results"]
    for bucket in cal.values():
        for it in bucket:
            assert set(it) == {"date", "symbol", "detail", "source"}

    ipo_syms = {i["symbol"] for i in cal["ipo"]}
    assert ipo_syms == {"ACME", "ACMEUS"}

    div_syms = {i["symbol"] for i in cal["dividends"]}
    assert div_syms == {"INFY", "SBIN"}          # keyword 'dividend' routed

    res_syms = {i["symbol"] for i in cal["results"]}
    assert {"TCS", "HDFCBANK", "INFY", "RELIANCE"} >= res_syms  # sanity
    assert "TCS" in res_syms and "HDFCBANK" in res_syms \
        and "INFY" in res_syms                   # board meeting/result/AV

    act_syms = {i["symbol"] for i in cal["actions"]}
    assert {"WIPRO", "IRCTC", "XYZ", "RELIANCE"} == act_syms  # bonus/split/agm

    # 'board meeting to consider dividend' -> dividend wins over results
    assert classify("Board meeting to consider dividend") == "dividends"
    assert classify("Q4 Results") == "results"
    assert classify("Rights issue") == "actions"
    assert classify(None) == "actions"

    # sorted by date within buckets
    for bucket in cal.values():
        dates = [i["date"] or "9999-99-99" for i in bucket]
        assert dates == sorted(dates)

    # everything empty -> clean empty shape, no crash
    empty = build()
    assert empty == {"ipo": [], "results": [], "dividends": [],
                     "actions": []}

    # malformed inputs tolerated
    messy = build(ipo=[None, "x", {}], corporate_actions=[{"raw": None}],
                  av={"results": [None]}, nse_events=["junk"])
    assert isinstance(messy, dict) and sorted(messy.keys()) == \
        ["actions", "dividends", "ipo", "results"]

    print("corp_cal self-test OK")

"""Economic calendar (IN + US, next 14 days) with a weekly cache.

get_calendar():
  1. state/macro_calendar.json {fetched_at, events:[EconEvent]} younger than
     7 days -> returned as-is.
  2. Else RapidAPI economic-calendar9 (env RAPIDAPI_KEY, own budget file
     state/rapidapi_budget.json cap 8/month) is tried; whatever shape comes
     back is normalized into EconEvent dicts.
  3. Repo events.json (manual file: {"events":[{date,name,scope,severity}]},
     scope MARKET -> country IN, severity -> impact) is always merged in as
     fallback/supplement.

get_av_calendars(): Alpha Vantage EARNINGS_CALENDAR / IPO_CALENDAR CSV
endpoints (env ALPHAVANTAGE_KEY), parsed defensively, cached 24h at
state/av_calendar.json -> {'results': [...], 'ipo': [...]} for corp_cal.

No key / dead source => cached/stale/manual data, health note, no exception.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

try:
    from .schema import STATE_DIR
    from .indianapi import Budget, _load_json, _save_json, _mark, _first, \
        _collect_dicts, IST
except ImportError:  # direct script execution
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from schema import STATE_DIR
    from indianapi import Budget, _load_json, _save_json, _mark, _first, \
        _collect_dicts, IST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL_PATH = os.path.join(STATE_DIR, "macro_calendar.json")
AV_CACHE_PATH = os.path.join(STATE_DIR, "av_calendar.json")
RAPID_BUDGET_PATH = os.path.join(STATE_DIR, "rapidapi_budget.json")
RAPID_CAP = 8
RAPID_HOST = "economic-calendar9.p.rapidapi.com"
RAPID_URL = (f"https://{RAPID_HOST}/web-crawling/api/economic-calendar/"
             "economic-events")
AV_URL = "https://www.alphavantage.co/query"
CAL_MAX_AGE = 7 * 86400
AV_MAX_AGE = 86400
WINDOW_DAYS = 14

HEALTH: dict = {}

_COUNTRY_MAP = {"united states": "US", "usa": "US", "us": "US", "usd": "US",
                "india": "IN", "in": "IN", "inr": "IN",
                "united states of america": "US"}


def map_impact(v) -> int:
    """volatility/importance/severity of any flavour -> 1|2|3 (unknown -> 2)."""
    s = str(v if v is not None else "").strip().lower()
    if s in ("3", "high", "***", "red", "hi"):
        return 3
    if s in ("1", "0", "low", "*", "none", "gray", "grey", "holiday"):
        return 1
    if s in ("2", "medium", "moderate", "mid", "**", "orange", "yellow"):
        return 2
    try:
        return min(3, max(1, int(float(s))))
    except (TypeError, ValueError):
        return 2


def _norm_country(v) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    return _COUNTRY_MAP.get(s.lower(), s.upper() if len(s) <= 3 else s)


def _split_dt(v) -> tuple[str | None, str | None]:
    """'2026-07-10T12:30:00Z' / '2026-07-10 12:30' / '2026-07-10' -> (d, t)."""
    if v in (None, ""):
        return None, None
    s = str(v).strip()
    for sep in ("T", " "):
        if sep in s:
            d, _, t = s.partition(sep)
            return d[:10] or None, t[:5] or None
    return s[:10], None


def _econ_event(d, t, country, event, impact, actual, forecast, previous,
                source) -> dict:
    return {"date": d, "time": t, "country": country, "event": event,
            "impact": impact, "actual": actual, "forecast": forecast,
            "previous": previous, "source": source}


def _normalize_rapid(raw) -> list[dict]:
    out = []
    for r in _collect_dicts(raw):
        dt = _first(r, "date", "dateUtc", "date_utc", "eventDate", "start",
                    "time_utc", "datetime", "dateTime")
        d, t = _split_dt(dt)
        t = _first(r, "time") or t
        if isinstance(t, str):
            t = t[:5] or None
        if not d:
            continue
        event = _first(r, "event", "title", "name", "indicator",
                       "event_name", "eventName")
        if not event:
            continue
        out.append(_econ_event(
            d, t,
            _norm_country(_first(r, "country", "countryCode", "country_code",
                                 "currency", "ccy")) or "??",
            str(event),
            map_impact(_first(r, "volatility", "importance", "impact",
                              "priority", "severity")),
            _first(r, "actual"),
            _first(r, "forecast", "consensus", "estimate"),
            _first(r, "previous", "prior"),
            "rapidapi"))
    return out


def _load_manual_events(events_path: str | None, today: date) -> list[dict]:
    """Repo events.json (manual) -> EconEvents inside [today, today+14d]."""
    paths = [events_path] if events_path else [
        os.path.join(ROOT, "events.json"),
        os.path.join(ROOT, "data", "events.json")]
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        data = _load_json(p, {})
        rows = data.get("events") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            continue
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            d, _ = _split_dt(r.get("date"))
            name = r.get("name") or r.get("event")
            if not d or not name:
                continue
            try:
                ed = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (today <= ed <= today + timedelta(days=WINDOW_DAYS)):
                continue
            scope = str(r.get("scope", "market")).strip().lower()
            if scope != "market":
                continue  # per-stock rows belong to the corporate calendar
            out.append(_econ_event(d, None, "IN", str(name),
                                   map_impact(r.get("severity")),
                                   None, None, None, "events.json"))
        _mark(HEALTH, "events.json", True)
        return out
    _mark(HEALTH, "events.json", False, "events.json not found")
    return []


def _fetch_rapid_live(frm: str, to: str):
    import requests
    r = requests.get(RAPID_URL,
                     params={"countries": "US,IN", "from": frm, "to": to},
                     headers={"x-rapidapi-host": RAPID_HOST,
                              "x-rapidapi-key": os.getenv("RAPIDAPI_KEY", "")},
                     timeout=12)
    r.raise_for_status()
    return r.json()


def get_calendar(fetch_fn=None, cache_path: str = CAL_PATH,
                 budget: Budget | None = None, events_path: str | None = None,
                 now: datetime | None = None) -> list[dict]:
    """list[EconEvent], IN+US, next ~14 days. Weekly-cached; never raises.

    fetch_fn: injectable callable(from_str, to_str) -> raw payload (fixtures).
    """
    now = now or datetime.now(IST)
    today = now.date()
    cache = _load_json(cache_path, {})
    cached_events = cache.get("events") if isinstance(
        cache.get("events"), list) else []
    try:
        age = now.timestamp() - float(cache.get("fetched_at", 0))
    except (TypeError, ValueError):
        age = CAL_MAX_AGE + 1
    if cached_events and age < CAL_MAX_AGE:
        return cached_events

    frm = today.isoformat()
    to = (today + timedelta(days=WINDOW_DAYS)).isoformat()
    rapid_events: list[dict] = []
    rapid_ok = False
    budget = budget if budget is not None else Budget(RAPID_BUDGET_PATH,
                                                      cap=RAPID_CAP)
    key = os.getenv("RAPIDAPI_KEY")
    if fetch_fn is None and not key:
        _mark(HEALTH, "rapidapi.calendar", False,
              "RAPIDAPI_KEY not set — source disabled")
    elif not budget.allow():
        _mark(HEALTH, "rapidapi.calendar", False,
              f"monthly budget exhausted ({budget.used}/{budget.cap})")
    else:
        try:
            budget.spend()
            raw = fetch_fn(frm, to) if fetch_fn is not None \
                else _fetch_rapid_live(frm, to)
            rapid_events = _normalize_rapid(raw)
            rapid_ok = True
            _mark(HEALTH, "rapidapi.calendar", True)
        except Exception as e:
            _mark(HEALTH, "rapidapi.calendar", False,
                  f"{type(e).__name__}: {e}")

    manual = _load_manual_events(events_path, today)

    # merge: rapid + manual (+ stale cache if the fetch failed), dedupe
    pool = rapid_events + manual + ([] if rapid_ok else cached_events)
    seen, events = set(), []
    for e in pool:
        k = (e.get("date"), e.get("country"),
             str(e.get("event", "")).strip().lower())
        if k in seen:
            continue
        seen.add(k)
        events.append(e)
    events.sort(key=lambda e: (e.get("date") or "9999",
                               e.get("time") or "99:99"))
    if rapid_ok:  # only advance the weekly clock on a real fetch success
        _save_json(cache_path, {"fetched_at": now.timestamp(),
                                "events": events})
    return events


# ------------------------------------------------------------ alpha vantage
def _parse_av_csv(text, date_field_candidates, source="alphavantage") -> list:
    """Defensive CSV -> [{date, symbol, name, detail, source}]."""
    if not text or not isinstance(text, str):
        return []
    if text.lstrip().startswith("{"):  # AV returns JSON error/rate-limit notes
        return []
    out = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not isinstance(row, dict):
                continue
            row = {(k or "").strip(): (v or "").strip()
                   for k, v in row.items() if k}
            d = next((row[c] for c in date_field_candidates if row.get(c)),
                     None)
            if not d:
                continue
            sym = row.get("symbol") or row.get("name") or "?"
            detail_bits = []
            if row.get("name") and row.get("name") != sym:
                detail_bits.append(row["name"])
            for k in ("estimate", "currency", "exchange", "priceRangeLow",
                      "priceRangeHigh", "fiscalDateEnding"):
                if row.get(k):
                    detail_bits.append(f"{k}={row[k]}")
            out.append({"date": d[:10], "symbol": sym,
                        "detail": "; ".join(detail_bits) or "scheduled",
                        "source": source})
    except (csv.Error, ValueError):
        pass
    return out


def _fetch_av_csv_live(function: str, key: str) -> str:
    import requests
    params = {"function": function, "apikey": key}
    if function == "EARNINGS_CALENDAR":
        params["horizon"] = "3month"
    r = requests.get(AV_URL, params=params, timeout=12)
    r.raise_for_status()
    return r.text


def get_av_calendars(fixture_earnings: str | None = None,
                     fixture_ipo: str | None = None,
                     cache_path: str = AV_CACHE_PATH,
                     now: datetime | None = None) -> dict:
    """{'results': [...], 'ipo': [...]} — empty lists without a key."""
    now = now or datetime.now(IST)
    cache = _load_json(cache_path, {})
    try:
        age = now.timestamp() - float(cache.get("fetched_at", 0))
    except (TypeError, ValueError):
        age = AV_MAX_AGE + 1
    have_fixture = fixture_earnings is not None or fixture_ipo is not None
    if not have_fixture and age < AV_MAX_AGE and \
            isinstance(cache.get("results"), list):
        return {"results": cache.get("results") or [],
                "ipo": cache.get("ipo") or []}

    key = os.getenv("ALPHAVANTAGE_KEY")
    if not have_fixture and not key:
        _mark(HEALTH, "alphavantage.calendar", False,
              "ALPHAVANTAGE_KEY not set — source disabled")
        return {"results": cache.get("results") or [],
                "ipo": cache.get("ipo") or []}

    results, ipo = [], []
    try:
        etext = fixture_earnings if fixture_earnings is not None \
            else _fetch_av_csv_live("EARNINGS_CALENDAR", key)
        results = _parse_av_csv(etext, ("reportDate", "date"))
        itext = fixture_ipo if fixture_ipo is not None \
            else _fetch_av_csv_live("IPO_CALENDAR", key)
        ipo = _parse_av_csv(itext, ("ipoDate", "date"))
        _mark(HEALTH, "alphavantage.calendar", True)
        _save_json(cache_path, {"fetched_at": now.timestamp(),
                                "results": results, "ipo": ipo})
    except Exception as e:
        _mark(HEALTH, "alphavantage.calendar", False,
              f"{type(e).__name__}: {e}")
        results = results or cache.get("results") or []
        ipo = ipo or cache.get("ipo") or []
    return {"results": results, "ipo": ipo}


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="macro_cal_test_")
    cal_path = os.path.join(tmp, "macro_calendar.json")
    bud_path = os.path.join(tmp, "rapid_budget.json")
    ev_path = os.path.join(tmp, "events.json")
    now = datetime(2026, 7, 4, 10, 0, tzinfo=IST)

    with open(ev_path, "w") as f:
        json.dump({"events": [
            {"date": "2026-07-08", "name": "RBI MPC decision",
             "scope": "market", "severity": "high"},
            {"date": "2026-07-10", "name": "TCS results",
             "scope": "TCS", "severity": "high"},        # stock-scope: skipped
            {"date": "2026-09-01", "name": "far away",
             "scope": "market", "severity": "low"},      # outside window
            {"date": "bad-date", "name": "junk", "scope": "market"},
        ]}, f)

    calls = {"n": 0}

    def fake_rapid(frm, to):
        calls["n"] += 1
        assert frm == "2026-07-04" and to == "2026-07-18"
        return {"result": [
            {"dateUtc": "2026-07-09T18:00:00Z", "country": "United States",
             "event": "FOMC Minutes", "volatility": "HIGH",
             "consensus": None, "previous": "5.50%"},
            {"date": "2026-07-08", "time": "11:30", "currency": "INR",
             "name": "CPI YoY", "importance": 2, "actual": "5.1%"},
            {"date": "2026-07-08", "country": "IN",
             "event": "RBI MPC decision", "impact": "high"},  # dup vs manual
            {"event": "no-date row"},                          # dropped
        ]}

    ev = get_calendar(fetch_fn=fake_rapid, cache_path=cal_path,
                      budget=Budget(bud_path, cap=RAPID_CAP),
                      events_path=ev_path, now=now)
    assert calls["n"] == 1
    dates = [(e["date"], e["event"]) for e in ev]
    assert ("2026-07-09", "FOMC Minutes") in dates
    assert ("2026-07-08", "CPI YoY") in dates
    # manual + rapid duplicate collapsed to one
    assert sum(1 for e in ev if e["event"].lower() == "rbi mpc decision") == 1
    assert all(e["impact"] in (1, 2, 3) for e in ev)
    fomc = next(e for e in ev if e["event"] == "FOMC Minutes")
    assert fomc["impact"] == 3 and fomc["country"] == "US" \
        and fomc["previous"] == "5.50%"
    cpi = next(e for e in ev if e["event"] == "CPI YoY")
    assert cpi["country"] == "IN" and cpi["time"] == "11:30" \
        and cpi["actual"] == "5.1%"
    assert not any(e["event"] in ("TCS results", "far away", "junk")
                   for e in ev)

    # weekly cache: second call does not re-fetch
    ev2 = get_calendar(fetch_fn=fake_rapid, cache_path=cal_path,
                       budget=Budget(bud_path, cap=RAPID_CAP),
                       events_path=ev_path, now=now + timedelta(days=3))
    assert calls["n"] == 1 and ev2 == ev

    # cache expiry (8 days) + rapid failure -> manual + stale merge, no raise
    def rapid_boom(frm, to):
        raise RuntimeError("quota")
    ev3 = get_calendar(fetch_fn=rapid_boom, cache_path=cal_path,
                       budget=Budget(os.path.join(tmp, "b2.json"),
                                     cap=RAPID_CAP),
                       events_path=ev_path, now=now + timedelta(days=8))
    assert any(e["event"] == "FOMC Minutes" for e in ev3)  # stale kept
    assert HEALTH["rapidapi.calendar"]["ok"] is False

    # budget exhausted -> no fetch, manual fallback only (fresh cache path)
    calls["n"] = 0
    b0 = Budget(os.path.join(tmp, "b3.json"), cap=0)
    ev4 = get_calendar(fetch_fn=fake_rapid,
                       cache_path=os.path.join(tmp, "cal2.json"),
                       budget=b0, events_path=ev_path, now=now)
    assert calls["n"] == 0 and \
        [e["event"] for e in ev4] == ["RBI MPC decision"]

    # impact mapping table
    assert [map_impact(x) for x in
            ("HIGH", "Low", 3, "2", None, "weird", "***")] == \
        [3, 1, 3, 2, 2, 2, 3]

    # alpha vantage CSV fixtures
    av_path = os.path.join(tmp, "av.json")
    earn_csv = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
                "INFY,Infosys Ltd,2026-07-15,2026-06-30,0.23,USD\n"
                ",missing symbol,2026-07-16,,,\n"
                "BAD,NoDate,,,,\n")
    ipo_csv = ("symbol,name,ipoDate,priceRangeLow,priceRangeHigh,currency,"
               "exchange\nACME,Acme Ltd,2026-07-20,90,100,USD,NASDAQ\n")
    av = get_av_calendars(fixture_earnings=earn_csv, fixture_ipo=ipo_csv,
                          cache_path=av_path, now=now)
    assert len(av["results"]) == 2 and av["results"][0]["symbol"] == "INFY"
    assert "Infosys Ltd" in av["results"][0]["detail"]
    assert av["ipo"][0] == {"date": "2026-07-20", "symbol": "ACME",
                            "detail": "Acme Ltd; currency=USD; "
                                      "exchange=NASDAQ; priceRangeLow=90; "
                                      "priceRangeHigh=100",
                            "source": "alphavantage"}
    # 24h cache serves without fixtures/key
    os.environ.pop("ALPHAVANTAGE_KEY", None)
    av2 = get_av_calendars(cache_path=av_path, now=now + timedelta(hours=5))
    assert av2["results"] == av["results"] and av2["ipo"] == av["ipo"]
    # expired cache + no key -> stale served, health notes missing key
    av3 = get_av_calendars(cache_path=av_path, now=now + timedelta(days=2))
    assert av3["results"] == av["results"]
    assert HEALTH["alphavantage.calendar"]["ok"] is False
    # AV JSON rate-limit note instead of CSV -> parsed to empty, no crash
    assert _parse_av_csv('{"Note": "rate limited"}', ("reportDate",)) == []
    # no cache, no key -> clean empties
    av4 = get_av_calendars(cache_path=os.path.join(tmp, "av_none.json"),
                           now=now)
    assert av4 == {"results": [], "ipo": []}

    print("macro_cal self-test OK")

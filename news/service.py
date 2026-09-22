"""NewsHub — the single background orchestrator for the market-news module.

One daemon thread refreshes on a 60-second heartbeat:
  * market tape (markets.fetch_tape)          — every cycle
  * RSS feeds (rss.fetch_all)                 — every 2nd cycle (~120 s)
  * NSE FII/DII flows (markets.fii_dii)       — every 30th cycle (~30 min)
  * indianapi news (indianapi.client().news)  — every 30th cycle, and only
                                                09:00–16:00 IST Mon–Fri
  * corporate + macro calendars               — once at start, then daily

After every cycle the hub tags items (sectors.tag), scores them
(weighted.score_items), assembles the FULL schema state and persists it via
schema.write_state() so other processes (the macro quant agent) read a file,
not a live object.

Resilience rules:
  * sibling modules are imported defensively — a missing/broken module only
    yields a health note, never an exception;
  * every sub-fetch is individually guarded; a dead source NEVER crashes a
    refresh — the previous value is kept and health records the error;
  * refresh_once(fixtures=...) accepts injected data for every source so the
    self-test below runs with ZERO network calls.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# The API-key sources read os.getenv at call time; make sure .env is loaded
# even when the hub runs outside run_app.py (tests, scripts, cron).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

log = logging.getLogger("news.service")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if __package__ in (None, "") and _ROOT not in sys.path:   # direct script run
    sys.path.insert(0, _ROOT)

from news import schema                                    # the contract

IST = timezone(timedelta(hours=5, minutes=30))

CYCLE_SECONDS = 60.0
RSS_EVERY = 2            # every 2nd cycle  (~120 s)
FII_EVERY = 30           # every 30th cycle (~30 min)
IAPI_EVERY = 30          # every 30th cycle, market hours only
INDIAN_CAP, GLOBAL_CAP = 120, 60

_MODULE_NAMES = ("markets", "rss", "sectors", "weighted",
                 "indianapi", "corp_cal", "macro_cal")

_EMPTY_CORP = {"ipo": [], "results": [], "dividends": [], "actions": []}


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _age_min(published, now_utc: datetime) -> float:
    if not published:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now_utc - dt).total_seconds() / 60.0), 1)


class NewsHub:
    """Construct with start=False for tests; .start() spawns the one
    daemon refresh thread (idempotent)."""

    def __init__(self, start: bool = False):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle = 0
        self._health: dict = {}

        # defensive sibling imports — written concurrently by other coders
        self._mods: dict = {}
        for name in _MODULE_NAMES:
            try:
                self._mods[name] = importlib.import_module("news." + name)
            except Exception as e:                    # noqa: BLE001
                self._mods[name] = None
                self._health["import." + name] = {
                    "ok": False, "last_ok": None,
                    "error": f"{type(e).__name__}: {e}"}

        # sticky last-known values (a dead source keeps its previous data)
        self._tape: dict = {}
        self._fii: dict | None = None
        self._raw_indian: list = []
        self._raw_global: list = []
        self._iapi_raw: list = []
        self._corporate: dict = dict(_EMPTY_CORP)
        self._macro_events: list = []
        self._last_cal_day: str | None = None

        self.state: dict = schema.empty_state()
        if start:
            self.start()

    # ── plumbing ─────────────────────────────────────────────────────────
    def _fn(self, mod_name: str, *candidates):
        """Probe a sibling module for the first callable among names."""
        mod = self._mods.get(mod_name)
        if mod is None:
            return None
        for name in candidates:
            fn = getattr(mod, name, None)
            if callable(fn):
                return fn
        return None

    def _mark(self, name: str, ok: bool, error: str | None = None):
        prev = self._health.get(name) or {}
        self._health[name] = {
            "ok": ok,
            "last_ok": _now_iso() if ok else prev.get("last_ok"),
            "error": None if ok else (error or "unknown error")[:200]}

    @staticmethod
    def _market_hours_ist(now: datetime | None = None) -> bool:
        now = now or datetime.now(IST)
        return now.weekday() < 5 and 9 <= now.hour < 16

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> "NewsHub":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="newshub-refresh", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.refresh_once()
            except Exception as e:                    # noqa: BLE001
                log.warning("news refresh cycle failed: %s", e)
            delay = max(5.0, CYCLE_SECONDS - (time.time() - t0))
            if self._stop.wait(delay):
                break

    # ── one refresh cycle ────────────────────────────────────────────────
    def refresh_once(self, fixtures: dict | None = None):
        """Run one cycle. fixtures (tests only) may inject any of:
        'tape', 'rss', 'fii_dii', 'indianapi_news', 'corporate', 'macro' —
        an injected source is used verbatim and no live call is made."""
        fx = fixtures or {}
        cyc = self._cycle
        now = datetime.now(IST)

        # 1) market tape — every cycle
        if "tape" in fx:
            self._tape = fx["tape"] or {}
            self._mark("hub.tape", True)
        else:
            fetch = self._fn("markets", "fetch_tape")
            if fetch is None:
                self._mark("hub.tape", False, "markets.fetch_tape unavailable")
            else:
                try:
                    self._tape = fetch() or {}
                    self._mark("hub.tape", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.tape", False, f"{type(e).__name__}: {e}")

        # 2) RSS — every 2nd cycle
        if "rss" in fx:
            self._adopt_rss(fx["rss"])
        elif cyc % RSS_EVERY == 0:
            fetch = self._fn("rss", "fetch_all")
            if fetch is None:
                self._mark("hub.rss", False, "rss.fetch_all unavailable")
            else:
                try:
                    self._adopt_rss(fetch())
                    self._mark("hub.rss", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.rss", False, f"{type(e).__name__}: {e}")

        # 3) FII/DII — every 30th cycle
        if "fii_dii" in fx:
            self._fii = fx["fii_dii"]
        elif cyc % FII_EVERY == 0:
            fetch = self._fn("markets", "fii_dii")
            if fetch is None:
                self._mark("hub.fii_dii", False,
                           "markets.fii_dii unavailable")
            else:
                try:
                    out = fetch()
                    if out is not None:               # keep last-known on miss
                        self._fii = out
                    self._mark("hub.fii_dii", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.fii_dii", False,
                               f"{type(e).__name__}: {e}")

        # 4) indianapi news — every 30th cycle, market hours IST only
        if "indianapi_news" in fx:
            self._iapi_raw = fx["indianapi_news"] or []
        elif cyc % IAPI_EVERY == 0 and self._market_hours_ist(now):
            client_fn = self._fn("indianapi", "client")
            if client_fn is None:
                self._mark("hub.indianapi_news", False,
                           "indianapi.client unavailable")
            else:
                try:
                    items = client_fn().news()
                    if items:
                        self._iapi_raw = items
                    self._mark("hub.indianapi_news", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.indianapi_news", False,
                               f"{type(e).__name__}: {e}")

        # 5) calendars — once at start, then daily
        day = now.date().isoformat()
        need_cal = self._last_cal_day != day
        if "corporate" in fx:
            self._corporate = fx["corporate"] or dict(_EMPTY_CORP)
        elif need_cal:
            fetch = self._fn("corp_cal", "get_corporate_calendar")
            if fetch is None:
                self._mark("hub.corp_cal", False,
                           "corp_cal.get_corporate_calendar unavailable")
            else:
                try:
                    cal = fetch()
                    if isinstance(cal, dict):
                        self._corporate = {k: cal.get(k) or []
                                           for k in _EMPTY_CORP}
                    self._mark("hub.corp_cal", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.corp_cal", False,
                               f"{type(e).__name__}: {e}")
        if "macro" in fx:
            self._macro_events = fx["macro"] or []
        elif need_cal:
            fetch = self._fn("macro_cal", "get_calendar")
            if fetch is None:
                self._mark("hub.macro_cal", False,
                           "macro_cal.get_calendar unavailable")
            else:
                try:
                    ev = fetch()
                    if isinstance(ev, list):
                        self._macro_events = ev
                    self._mark("hub.macro_cal", True)
                except Exception as e:                # noqa: BLE001
                    self._mark("hub.macro_cal", False,
                               f"{type(e).__name__}: {e}")
        self._last_cal_day = day

        # 6) tag + score + assemble + persist
        self._assemble(now)
        self._cycle += 1
        return self.state

    def _adopt_rss(self, result):
        if not isinstance(result, dict):
            raise TypeError("rss.fetch_all returned non-dict")
        self._raw_indian = result.get("indian") or []
        self._raw_global = result.get("global") or []
        for k, v in (result.get("health") or {}).items():
            if isinstance(v, dict):
                self._health[k] = dict(v)

    # ── assembly ─────────────────────────────────────────────────────────
    def _iapi_as_raw(self) -> list:
        out = []
        for it in self._iapi_raw:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            link = it.get("url") or it.get("link") or \
                f"indianapi://news/{schema.news_id(str(it.get('title')))}"
            out.append({"title": it.get("title"), "link": link,
                        "source": "indianapi",
                        "published": it.get("date") or it.get("published"),
                        "source_weight": 0.7, "region": "IN"})
        return out

    def _enrich(self, raw_list: list, now_utc: datetime, seen: set) -> list:
        """raw feed items -> NewsItem dicts (+ transient source_weight)."""
        tag_fn = self._fn("sectors", "tag")
        out = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            title = raw.get("title") or ""
            link = raw.get("link") or raw.get("url") or ""
            iid = raw.get("id") or schema.news_id(link or title)
            if iid in seen:
                continue
            seen.add(iid)
            secs, syms = raw.get("sectors"), raw.get("symbols")
            if secs is None and syms is None:
                secs, syms = [], []
                if tag_fn is not None:
                    try:
                        tagged = tag_fn(title) or {}
                        secs = tagged.get("sectors") or []
                        syms = tagged.get("symbols") or []
                    except Exception:                 # noqa: BLE001
                        pass
            out.append({"id": iid, "title": title, "link": link,
                        "source": raw.get("source") or "unknown",
                        "published": raw.get("published"),
                        "age_min": _age_min(raw.get("published"), now_utc),
                        "sectors": secs or [], "symbols": syms or [],
                        "weight": 0.0, "reasons": [],
                        "source_weight": float(
                            raw.get("source_weight", 0.5) or 0.5)})
        return out

    @staticmethod
    def _clean(item: dict) -> dict:
        it = dict(item)
        it.pop("source_weight", None)
        return it

    def _assemble(self, now: datetime):
        now_utc = now.astimezone(timezone.utc)
        seen: set = set()
        indian = self._enrich(list(self._raw_indian) + self._iapi_as_raw(),
                              now_utc, seen)[:INDIAN_CAP]
        global_ = self._enrich(self._raw_global, now_utc, seen)[:GLOBAL_CAP]

        # weighted top list + risk score
        weighted_items: list = []
        risk = float(self.state.get("risk_score") or 0.0)
        score_fn = self._fn("weighted", "score_items")
        if score_fn is None:
            self._mark("hub.weighted", False,
                       "weighted.score_items unavailable")
        else:
            try:
                weighted_items, risk = score_fn(indian + global_,
                                                self._tape, self._fii)
                self._mark("hub.weighted", True)
            except Exception as e:                    # noqa: BLE001
                self._mark("hub.weighted", False, f"{type(e).__name__}: {e}")
                weighted_items = self.state.get("weighted") or []
        weighted_items = [self._clean(w) for w in weighted_items
                          if isinstance(w, dict)]
        try:
            risk = max(-1.0, min(1.0, float(risk)))
        except (TypeError, ValueError):
            risk = 0.0

        non_in = [e for e in self._macro_events
                  if isinstance(e, dict)
                  and str(e.get("country") or "").upper() != "IN"]
        glob_cal = [e for e in non_in if (e.get("impact") or 0) >= 2]
        if not glob_cal:            # calendar sources graded everything low —
            glob_cal = sorted(     # show the next 15 events instead of nothing
                non_in, key=lambda e: (e.get("date") or "9999",
                                       e.get("time") or ""))[:15]

        budgets: dict = {}
        try:
            client_fn = self._fn("indianapi", "client")
            if client_fn is not None:
                budgets["indianapi"] = client_fn().budget_snapshot()
        except Exception:                             # noqa: BLE001
            pass
        try:
            mcal = self._mods.get("macro_cal")
            if mcal is not None and hasattr(mcal, "Budget"):
                budgets["rapidapi"] = mcal.Budget(
                    mcal.RAPID_BUDGET_PATH, cap=mcal.RAPID_CAP).snapshot()
        except Exception:                             # noqa: BLE001
            pass

        # merge health: hub-level + per-module HEALTH dicts
        health = {k: dict(v) for k, v in self._health.items()
                  if isinstance(v, dict)}
        for name in ("markets", "indianapi", "macro_cal", "corp_cal"):
            h = getattr(self._mods.get(name), "HEALTH", None)
            if isinstance(h, dict):
                for k, v in h.items():
                    if isinstance(v, dict):
                        health[k] = dict(v)

        # Indian panel gets ONLY Indian events (RBI, CPI-IN, expiry days...);
        # everything non-IN lives in the global calendar on the right panel.
        in_cal = [e for e in self._macro_events
                  if isinstance(e, dict)
                  and str(e.get("country") or "").upper() in ("IN", "INDIA")]
        state = {"generated_at": now.isoformat(timespec="seconds"),
                 "indian_news": [self._clean(i) for i in indian],
                 "global_news": [self._clean(i) for i in global_],
                 "weighted": weighted_items,
                 "market_tape": self._tape,
                 "fii_dii": self._fii,
                 "risk_score": risk,
                 "calendars": {"macro": in_cal,
                               "global": glob_cal,
                               "corporate": self._corporate},
                 "budgets": budgets,
                 "health": health}
        try:
            schema.write_state(state)
        except Exception as e:                        # noqa: BLE001
            log.warning("news state persist failed: %s", e)
        with self._lock:
            self.state = state

    # ── read API ─────────────────────────────────────────────────────────
    def get_state(self) -> dict:
        with self._lock:
            st = self.state
        if st and st.get("generated_at"):
            return st
        return schema.read_state() or st or schema.empty_state()


# ── offline self-test (fixtures only — ZERO network calls) ──────────────
if __name__ == "__main__":
    import tempfile

    # keep the repo state dir pristine: redirect persistence to a temp dir
    _tmp = tempfile.mkdtemp(prefix="newshub_test_")
    schema.STATE_DIR = _tmp
    schema.NEWS_STATE_PATH = os.path.join(_tmp, "news_state.json")

    now_iso = datetime.now(timezone.utc).isoformat()
    fixtures = {
        "tape": {"usdinr": {"price": 83.4, "chg_pct": 0.45, "asof": now_iso},
                 "crude_brent": {"price": 90.2, "chg_pct": 2.6,
                                 "asof": now_iso},
                 "spx": {"price": 5900.0, "chg_pct": -1.4, "asof": now_iso},
                 "us10y": {"price": 4.5, "chg_pct": 3.4, "asof": now_iso},
                 "nikkei": None, "hangseng": None,
                 "gold": {"price": 2400.0, "chg_pct": 0.2, "asof": now_iso}},
        "rss": {"indian": [
                    {"id": schema.news_id("https://x/in1"),
                     "title": "HDFC Bank Q1 results beat estimates",
                     "link": "https://x/in1", "source": "etmarkets",
                     "published": now_iso, "source_weight": 0.9,
                     "region": "IN"},
                    {"id": schema.news_id("https://x/in2"),
                     "title": "RBI holds repo rate, stance unchanged",
                     "link": "https://x/in2", "source": "moneycontrol_top",
                     "published": now_iso, "source_weight": 0.8,
                     "region": "IN"}],
                "global": [
                    {"id": schema.news_id("https://x/g1"),
                     "title": "Fed signals rate cut in September",
                     "link": "https://x/g1", "source": "cnbc_world",
                     "published": now_iso, "source_weight": 0.8,
                     "region": "GLOBAL"}],
                "health": {"rss.etmarkets": {"ok": True, "last_ok": now_iso,
                                             "error": None},
                           "rss.nse_announcements": {
                               "ok": False, "last_ok": None,
                               "error": "HTTP 403"}}},
        "fii_dii": {"date": "2026-07-03", "fii_net_cr": -1850.0,
                    "dii_net_cr": 2100.0},
        "indianapi_news": [{"title": "SEBI clears new F&O framework",
                            "date": "2026-07-03", "symbol": None,
                            "summary": "regulatory", "url": "https://x/ia1"}],
        "corporate": {"ipo": [{"date": "2026-07-10", "symbol": "ACME",
                               "detail": "IPO opens", "source": "indianapi"}],
                      "results": [], "dividends": [], "actions": []},
        "macro": [{"date": "2026-07-08", "time": "14:00", "country": "IN",
                   "event": "RBI MPC decision", "impact": 3, "actual": None,
                   "forecast": None, "previous": None,
                   "source": "events.json"},
                  {"date": "2026-07-09", "time": "18:00", "country": "US",
                   "event": "FOMC Minutes", "impact": 3, "actual": None,
                   "forecast": None, "previous": None, "source": "rapidapi"},
                  {"date": "2026-07-07", "time": None, "country": "US",
                   "event": "Consumer Credit", "impact": 1, "actual": None,
                   "forecast": None, "previous": None, "source": "rapidapi"}],
    }

    hub = NewsHub(start=False)                 # NO thread, NO network
    st = hub.refresh_once(fixtures=fixtures)

    # full schema shape
    assert set(st) == set(schema.empty_state()), set(st)
    assert st["generated_at"]

    # indian list: 2 rss + 1 indianapi, NewsItem shape, no leakage
    assert len(st["indian_news"]) == 3, st["indian_news"]
    for it in st["indian_news"] + st["global_news"]:
        assert set(it) == {"id", "title", "link", "source", "published",
                           "age_min", "sectors", "symbols", "weight",
                           "reasons"}, set(it)
    tagged = {i["link"]: i for i in st["indian_news"]}
    assert "BANKING" in tagged["https://x/in1"]["sectors"]
    assert "HDFCBANK" in tagged["https://x/in1"]["symbols"]
    assert len(st["global_news"]) == 1

    # weighted: present, sorted desc, includes synthesised tape alerts
    assert st["weighted"], "weighted list empty"
    ws = [w["weight"] for w in st["weighted"]]
    assert ws == sorted(ws, reverse=True)
    assert all("source_weight" not in w for w in st["weighted"])
    assert any(w["source"] == "market-tape" for w in st["weighted"])

    # risk: risk-off tape + FII selling -> negative
    assert -1.0 <= st["risk_score"] < 0.0, st["risk_score"]

    # calendars split: Indian panel = IN events only; global = non-IN imp>=2
    mcal = st["calendars"]["macro"]
    assert len(mcal) == 1 and mcal[0]["event"] == "RBI MPC decision", mcal
    gcal = st["calendars"]["global"]
    assert len(gcal) == 1 and gcal[0]["event"] == "FOMC Minutes", gcal
    assert set(st["calendars"]["corporate"]) == set(_EMPTY_CORP)

    # health: rss + hub entries, dead feed reported not fatal
    assert st["health"]["rss.nse_announcements"]["ok"] is False
    assert st["health"]["hub.tape"]["ok"] is True

    # persisted + get_state round trip
    on_disk = schema.read_state()
    assert on_disk and on_disk["generated_at"] == st["generated_at"]
    assert hub.get_state() is st or hub.get_state() == st

    # second cycle (odd -> rss cadence skips live; fixture reused) keeps data
    st2 = hub.refresh_once(fixtures=fixtures)
    assert len(st2["indian_news"]) == 3 and st2["generated_at"]

    # fallback: a fresh hub with no cycles reads the persisted file
    hub2 = NewsHub(start=False)
    assert hub2.get_state().get("generated_at") == st2["generated_at"]

    # market-hours gate
    assert NewsHub._market_hours_ist(datetime(2026, 7, 3, 10, 0, tzinfo=IST))
    assert not NewsHub._market_hours_ist(
        datetime(2026, 7, 4, 10, 0, tzinfo=IST))      # Saturday
    assert not NewsHub._market_hours_ist(
        datetime(2026, 7, 3, 16, 30, tzinfo=IST))     # after close

    # corrupt state file tolerated
    with open(schema.NEWS_STATE_PATH, "w") as f:
        f.write("{not json")
    hub3 = NewsHub(start=False)
    assert hub3.get_state() == schema.empty_state()

    print("service.py self-test OK:",
          len(st["indian_news"]), "IN /", len(st["global_news"]), "GLOBAL /",
          len(st["weighted"]), "weighted; risk", st["risk_score"])

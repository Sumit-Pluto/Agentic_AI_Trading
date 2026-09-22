"""indianapi.in client — budget-capped, cached, corrupt-tolerant.

Base: https://stock.indianapi.in  auth header {'X-Api-Key': INDIANAPI_KEY}.
Budget: state/indianapi_budget.json {month:'YYYY-MM', used:int}, cap 480/month,
auto-reset on month rollover. Every live call is gated by budget.allow().
Cache: state/indianapi_cache.json {endpoint: {'ts': epoch, 'data': [...]}}
with per-endpoint TTLs. Responses are normalized defensively: lists of dicts
with common fields picked out and the original record kept under 'raw'.

No key / dead API / exhausted budget => stale cache (if any) or [], with a
health note — never an exception to the caller.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

try:
    from .schema import STATE_DIR
except ImportError:  # direct script execution
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from schema import STATE_DIR

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    IST = timezone.utc

BASE = "https://stock.indianapi.in"
BUDGET_PATH = os.path.join(STATE_DIR, "indianapi_budget.json")
CACHE_PATH = os.path.join(STATE_DIR, "indianapi_cache.json")
MONTHLY_CAP = 480

TTL = {"news": 1800, "ipo": 86400, "corporate_actions": 86400,
       "recent_announcements": 3600, "commodities": 3600, "trending": 3600}

HEALTH: dict = {}  # 'indianapi.<endpoint>' -> {'ok','last_ok','error'}


# ---------------------------------------------------------------- json utils
def _load_json(path: str, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass  # a failed cache write must never crash a refresh


def _mark(health: dict, name: str, ok: bool, error: str | None = None) -> None:
    prev = health.get(name) or {}
    health[name] = {
        "ok": ok,
        "last_ok": datetime.now(timezone.utc).isoformat() if ok
        else prev.get("last_ok"),
        "error": None if ok else (error or "unknown error"),
    }


# ------------------------------------------------------------------- budget
class Budget:
    """Persistent monthly call budget with automatic month rollover."""

    def __init__(self, path: str = BUDGET_PATH, cap: int = MONTHLY_CAP,
                 now_fn=None):
        self.path = path
        self.cap = int(cap)
        self._now_fn = now_fn or (lambda: datetime.now(IST))
        data = _load_json(path, {})
        self.month = data.get("month") if isinstance(
            data.get("month"), str) else self._cur_month()
        try:
            self.used = max(0, int(data.get("used", 0)))
        except (TypeError, ValueError):
            self.used = 0
        self._roll()

    def _cur_month(self) -> str:
        return self._now_fn().strftime("%Y-%m")

    def _roll(self) -> None:
        m = self._cur_month()
        if m != self.month:
            self.month, self.used = m, 0
            self._save()

    def _save(self) -> None:
        _save_json(self.path, {"month": self.month, "used": self.used})

    def allow(self) -> bool:
        self._roll()
        return self.used < self.cap

    def spend(self, n: int = 1) -> None:
        self._roll()
        self.used += n
        self._save()

    def snapshot(self) -> dict:
        self._roll()
        return {"month": self.month, "used": self.used, "cap": self.cap}


# -------------------------------------------------------- response normalize
_LIST_KEYS = ("data", "results", "news", "items", "announcements", "ipo",
              "ipoData", "upcoming", "listed", "active", "closed",
              "commodities", "trending", "top_gainers", "top_losers",
              "corporate_actions", "events", "records")


def _collect_dicts(node, depth: int = 0) -> list:
    """Pull every plausible record (dict) out of an unknown-ish payload."""
    out = []
    if isinstance(node, list):
        for r in node:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, list) and depth < 2:
                out.extend(_collect_dicts(r, depth + 1))
    elif isinstance(node, dict):
        for k in _LIST_KEYS:
            if isinstance(node.get(k), (list, dict)):
                out.extend(_collect_dicts(node[k], depth + 1))
        if not out and depth < 2:
            for v in node.values():
                if isinstance(v, (list, dict)):
                    got = _collect_dicts(v, depth + 1)
                    out.extend(got)
        if not out and depth == 0 and node:
            out = [node]  # single-record payload
    return out


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def normalize_items(raw) -> list[dict]:
    """Unknown payload -> list of {'title','date','symbol','summary','url','raw'}."""
    items = []
    for r in _collect_dicts(raw):
        items.append({
            "title": _first(r, "title", "headline", "name", "company_name",
                            "companyName", "company", "event", "commodity",
                            "symbol"),
            "date": _first(r, "date", "pub_date", "pubDate", "published_at",
                           "publishedAt", "ex_date", "exDate", "an_dt",
                           "announcement_date", "listing_date", "listingDate",
                           "bidding_start_date", "biddingStartDate",
                           "boardMeetingDate", "timestamp"),
            "symbol": _first(r, "symbol", "ticker", "nse_symbol", "nseSymbol",
                             "scrip", "isin"),
            "summary": _first(r, "summary", "description", "desc", "purpose",
                              "subject", "details", "text", "content"),
            "url": _first(r, "url", "link", "source_url", "sourceUrl",
                          "attachment"),
            "raw": r,
        })
    return items


# ------------------------------------------------------------------- client
class IndianAPI:
    """Cached, budgeted indianapi.in client.

    fetcher: optional injectable callable(endpoint, params) -> parsed payload
             (used by tests/fixtures; when set, no network is touched).
    """

    def __init__(self, api_key: str | None = None, budget: Budget | None = None,
                 cache_path: str = CACHE_PATH, fetcher=None,
                 health: dict | None = None):
        self.api_key = api_key if api_key is not None \
            else os.getenv("INDIANAPI_KEY")
        self.budget = budget if budget is not None else Budget()
        self.cache_path = cache_path
        self.fetcher = fetcher
        self.health = health if health is not None else HEALTH

    # -- plumbing ------------------------------------------------------
    def _fetch_live(self, ep: str, params: dict | None):
        import requests
        r = requests.get(f"{BASE}/{ep}", headers={"X-Api-Key": self.api_key},
                         params=params or {}, timeout=12)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _stale(entry) -> list:
        if isinstance(entry, dict) and isinstance(entry.get("data"), list):
            return entry["data"]
        return []

    def _get(self, ep: str, ttl: int, params: dict | None = None) -> list:
        hname = f"indianapi.{ep}"
        cache = _load_json(self.cache_path, {})
        key = ep if not params else \
            ep + "?" + json.dumps(params, sort_keys=True)
        entry = cache.get(key)
        if isinstance(entry, dict):
            try:
                if time.time() - float(entry.get("ts", 0)) < ttl:
                    return self._stale(entry)
            except (TypeError, ValueError):
                pass
        if self.fetcher is None and not self.api_key:
            _mark(self.health, hname, False,
                  "INDIANAPI_KEY not set — source disabled")
            return self._stale(entry)
        if not self.budget.allow():
            _mark(self.health, hname, False,
                  "monthly budget exhausted "
                  f"({self.budget.used}/{self.budget.cap})")
            return self._stale(entry)
        try:
            self.budget.spend()
            raw = self.fetcher(ep, params) if self.fetcher is not None \
                else self._fetch_live(ep, params)
            data = normalize_items(raw)
            cache[key] = {"ts": time.time(), "data": data}
            _save_json(self.cache_path, cache)
            _mark(self.health, hname, True)
            return data
        except Exception as e:  # a dead source never crashes a refresh
            _mark(self.health, hname, False, f"{type(e).__name__}: {e}")
            return self._stale(entry)

    # -- endpoints -----------------------------------------------------
    def news(self) -> list:
        return self._get("news", TTL["news"])

    def ipo(self) -> list:
        return self._get("ipo", TTL["ipo"])

    def corporate_actions(self, stock_name: str | None = None) -> list:
        # per-STOCK endpoint (422 without stock_name). Market-wide sweeps
        # would eat the monthly budget — only call with an explicit symbol
        # (on-demand enrichment); otherwise skip without an HTTP call.
        if not stock_name:
            _mark(self.health, "indianapi.corporate_actions", True)
            return []
        return self._get("corporate_actions", TTL["corporate_actions"],
                         params={"stock_name": stock_name})

    def recent_announcements(self, stock_name: str | None = None) -> list:
        if not stock_name:
            _mark(self.health, "indianapi.recent_announcements", True)
            return []
        return self._get("recent_announcements",
                         TTL["recent_announcements"],
                         params={"stock_name": stock_name})

    def commodities(self) -> list:
        return self._get("commodities", TTL["commodities"])

    def trending(self) -> list:
        return self._get("trending", TTL["trending"])

    def budget_snapshot(self) -> dict:
        return self.budget.snapshot()


_DEFAULT: IndianAPI | None = None


def client() -> IndianAPI:
    """Shared default client (lazy)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = IndianAPI()
    return _DEFAULT


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp(prefix="indianapi_test_")
    bpath = os.path.join(tmp, "budget.json")
    cpath = os.path.join(tmp, "cache.json")

    calls = {"n": 0}

    def fake_fetch(ep, params):
        calls["n"] += 1
        if ep == "news":
            return {"data": [
                {"title": "RBI holds repo rate", "pub_date": "2026-07-03",
                 "url": "https://x/1", "summary": "MPC outcome"},
                {"headline": "TCS Q1 results date", "date": "2026-07-04"},
            ]}
        if ep == "ipo":
            return {"upcoming": [{"name": "Acme Ltd",
                                  "bidding_start_date": "2026-07-10",
                                  "symbol": "ACME"}],
                    "listed": [{"name": "Old IPO",
                                "listing_date": "2026-06-20"}]}
        if ep == "trending":
            return {"trending_stocks": {
                "top_gainers": [{"company_name": "HDFC Bank",
                                 "ticker": "HDFCBANK"}],
                "top_losers": [{"company_name": "Wipro", "ticker": "WIPRO"}]}}
        if ep == "corporate_actions":
            return [{"symbol": "INFY", "purpose": "Dividend - Rs 20",
                     "ex_date": "2026-07-08"}]
        return []

    api = IndianAPI(api_key="test-key", budget=Budget(bpath, cap=480),
                    cache_path=cpath, fetcher=fake_fetch, health={})

    # normalization
    news = api.news()
    assert len(news) == 2 and news[0]["title"] == "RBI holds repo rate"
    assert news[0]["url"] == "https://x/1" and "raw" in news[0]
    ipos = api.ipo()
    assert len(ipos) == 2 and ipos[0]["symbol"] == "ACME"
    assert ipos[0]["date"] == "2026-07-10"
    tr = api.trending()
    assert len(tr) == 2 and tr[0]["symbol"] == "HDFCBANK"
    # per-stock endpoint: no symbol -> [] without any call or budget spend
    spent_before = api.budget.used
    assert api.corporate_actions() == []
    assert api.recent_announcements() == []
    assert api.budget.used == spent_before
    assert api.health["indianapi.corporate_actions"]["ok"] is True
    # with a symbol -> fixture flows through with the stock_name param
    ca = api.corporate_actions(stock_name="INFY")
    assert ca[0]["summary"] == "Dividend - Rs 20"

    # TTL cache: repeated calls hit cache, budget unchanged
    n_before = calls["n"]
    used_before = api.budget.used
    assert api.news() == news and calls["n"] == n_before
    assert api.budget.used == used_before
    assert api.health["indianapi.news"]["ok"] is True

    # budget exhaustion -> stale cache served, health notes it
    aged = _load_json(cpath, {})
    for ent in aged.values():
        ent["ts"] = time.time() - 999999  # force every entry past its TTL
    _save_json(cpath, aged)
    api2 = IndianAPI(api_key="k", budget=Budget(
        os.path.join(tmp, "b2.json"), cap=0), cache_path=cpath,
        fetcher=fake_fetch, health={})
    assert api2.news() == news  # stale-but-served
    assert api2.health["indianapi.news"]["ok"] is False
    assert "budget" in api2.health["indianapi.news"]["error"]

    # no key, empty cache -> [] + health note, no crash
    api3 = IndianAPI(api_key="", cache_path=os.path.join(tmp, "empty.json"),
                     budget=Budget(os.path.join(tmp, "b3.json")), health={})
    assert api3.commodities() == []
    assert "INDIANAPI_KEY" in api3.health["indianapi.commodities"]["error"]

    # fetcher raising -> [] (no cache) and health error, never an exception
    def boom(ep, params):
        raise RuntimeError("api down")
    api4 = IndianAPI(api_key="k", budget=Budget(os.path.join(tmp, "b4.json")),
                     cache_path=os.path.join(tmp, "c4.json"), fetcher=boom,
                     health={})
    assert api4.trending() == []
    assert "api down" in api4.health["indianapi.trending"]["error"]

    # month rollover resets budget
    b = Budget(os.path.join(tmp, "b5.json"), cap=480,
               now_fn=lambda: datetime(2026, 6, 30, tzinfo=IST))
    b.spend(479)
    assert b.allow() and b.used == 479
    b._now_fn = lambda: datetime(2026, 7, 1, tzinfo=IST)
    assert b.allow() and b.snapshot() == {"month": "2026-07", "used": 0,
                                          "cap": 480}

    # corrupt budget + cache files tolerated
    with open(os.path.join(tmp, "corrupt.json"), "w") as f:
        f.write("{not json")
    bc = Budget(os.path.join(tmp, "corrupt.json"))
    assert bc.allow()
    api5 = IndianAPI(api_key="k", budget=Budget(os.path.join(tmp, "b6.json")),
                     cache_path=os.path.join(tmp, "corrupt.json"),
                     fetcher=fake_fetch, health={})
    assert len(api5.news()) == 2

    print("indianapi self-test OK")

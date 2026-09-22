"""News module — shared contract. Every producer writes into this state
shape; the API, the UI and the macro agent all read it. The full state is
persisted to state/news_state.json on every refresh so the macro agent
(separate process concerns) reads a file, not a live object.

STATE SHAPE (all keys always present; empty lists/None when unavailable):
{
  "generated_at": "2026-07-06T10:15:00+05:30",
  "indian_news":  [ NewsItem... ]     # newest first, capped 120
  "global_news":  [ NewsItem... ]     # capped 60
  "weighted":     [ NewsItem... ]     # top ~15 by weight, with reasons
  "market_tape": {                    # Twelve Data, refreshed ~60s
      "usdinr":   Quote, "crude_brent": Quote, "crude_wti": Quote,
      "us10y":    Quote, "dxy": Quote, "spx": Quote, "nasdaq": Quote,
      "nikkei":   Quote, "hangseng": Quote, "gold": Quote
  },
  "fii_dii":     {"date": "...", "fii_net_cr": -1234.5,
                  "dii_net_cr": 987.6} | None,
  "risk_score":  float in [-1, 1],    # -1 = strong risk-off, +1 = risk-on
  "calendars": {
      "macro":     [ EconEvent... ],  # IN + US, next 14 days
      "global":    [ EconEvent... ],  # non-IN subset with impact >= 2
      "corporate": {"ipo": [...], "results": [...],
                    "dividends": [...], "actions": [...]}
  },
  "budgets": {"indianapi": {"month": "2026-07", "used": 17, "cap": 480},
              "rapidapi":  {"month": "2026-07", "used": 1,  "cap": 8}},
  "health":  {"rss.etmarkets": {"ok": true, "last_ok": iso, "error": null},
              ...one entry per source...}
}

NewsItem = {
  "id": sha1(link)[:16], "title": str, "link": str, "source": str,
  "published": iso-str | null, "age_min": float,
  "sectors": ["BANKING", ...],        # keyword-classified, may be []
  "symbols": ["HDFCBANK", ...],       # F&O names matched in title, may be []
  "weight": float 0..100,             # impact score (weighted.py)
  "reasons": ["FED/rate keyword", "source weight", ...]   # weighted only
}

Quote = {"price": float, "chg_pct": float, "asof": iso-str} | None
EconEvent = {"date": "YYYY-MM-DD", "time": "HH:MM" | null,
             "country": "IN"/"US"/..., "event": str, "impact": 1|2|3,
             "actual": str|null, "forecast": str|null, "previous": str|null,
             "source": "rapidapi"/"alphavantage"/"events.json"/"indianapi"}
"""

from __future__ import annotations

import hashlib
import json
import os

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "state")
NEWS_STATE_PATH = os.path.join(STATE_DIR, "news_state.json")

SECTORS = ["BANKING", "IT", "PHARMA", "AUTO", "METAL", "ENERGY", "FMCG",
           "REALTY", "INFRA", "FINANCE", "TELECOM", "CEMENT", "POWER",
           "DEFENCE", "CHEMICALS"]


def news_id(link: str) -> str:
    return hashlib.sha1((link or "").encode()).hexdigest()[:16]


def empty_state() -> dict:
    return {"generated_at": None, "indian_news": [], "global_news": [],
            "weighted": [], "market_tape": {}, "fii_dii": None,
            "risk_score": 0.0,
            "calendars": {"macro": [], "global": [],
                          "corporate": {"ipo": [], "results": [],
                                        "dividends": [], "actions": []}},
            "budgets": {}, "health": {}}


def write_state(state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = NEWS_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, NEWS_STATE_PATH)


def read_state() -> dict | None:
    try:
        with open(NEWS_STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

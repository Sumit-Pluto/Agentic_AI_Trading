"""RSS aggregation for the market-news module.

Pulls Indian + global market headlines from a fixed set of feeds via
feedparser, normalises entries into raw items, dedupes across feeds by
news_id (keeping the copy from the highest-weight source) and reports
per-feed health.  A dead/geo-blocked feed NEVER crashes a refresh — it
just shows up as {'ok': False, ...} in the health map.

Raw item shape (input to news/weighted.py):
    {"id", "title", "link", "source", "published" (iso|None),
     "source_weight" (0..1), "region" ("IN"|"GLOBAL")}

Offline testing: fetch_all(fixtures={name: xml_text}) never touches the
network; parse_feed_text(name, xml_text) parses a single fixture feed.

The last successful fetch time per feed is persisted (corrupt-tolerantly)
to state/rss_health.json so 'last_ok' survives restarts.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone

import feedparser

try:
    from news.schema import STATE_DIR, news_id
except ImportError:                                    # run as news/rss.py
    from schema import STATE_DIR, news_id

USER_AGENT = "Mozilla/5.0"
RSS_HEALTH_PATH = os.path.join(STATE_DIR, "rss_health.json")

FEEDS = {
    "etmarkets": {
        "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "region": "IN", "weight": 0.9},
    "moneycontrol_top": {
        "url": "https://www.moneycontrol.com/rss/MCtopnews.xml",
        "region": "IN", "weight": 0.85},
    "moneycontrol_buzzing": {
        "url": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
        "region": "IN", "weight": 0.7},
    "livemint_markets": {
        "url": "https://www.livemint.com/rss/markets",
        "region": "IN", "weight": 0.8},
    "business_standard": {
        "url": "https://www.business-standard.com/rss/markets-106.rss",
        "region": "IN", "weight": 0.8},
    "nse_announcements": {   # frequently geo-blocked outside IN — degrades
        "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
        "region": "IN", "weight": 0.95},
    "cnbc_world": {
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml"
               "?partnerId=wrss01&id=100727362",
        "region": "GLOBAL", "weight": 0.8},
    "yahoo_finance": {
        "url": "https://finance.yahoo.com/news/rssindex",
        "region": "GLOBAL", "weight": 0.6},
}


# ── helpers ──────────────────────────────────────────────────────────────
def _struct_to_iso(st) -> str | None:
    """feedparser time.struct_time (already UTC) -> ISO-8601 string."""
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _published_dt(item) -> datetime:
    """Sort key: parsed publish time, epoch for undated items."""
    iso = item.get("published")
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _entry_to_item(name: str, meta: dict, entry) -> dict | None:
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None
    published = _struct_to_iso(entry.get("published_parsed")
                               or entry.get("updated_parsed"))
    return {"id": news_id(link), "title": title, "link": link,
            "source": name, "published": published,
            "source_weight": float(meta.get("weight", 0.5)),
            "region": meta.get("region", "GLOBAL")}


def parse_feed_text(name: str, xml_text) -> list[dict]:
    """Parse raw feed XML (str/bytes) into raw items — used by fixtures
    and by fetch_all(fixtures=...); performs NO network I/O."""
    meta = FEEDS.get(name, {"region": "GLOBAL", "weight": 0.5})
    parsed = feedparser.parse(xml_text)
    items = []
    for entry in parsed.get("entries", []):
        item = _entry_to_item(name, meta, entry)
        if item:
            items.append(item)
    return items


# ── persisted last_ok (corrupt tolerant) ─────────────────────────────────
def _load_last_ok() -> dict:
    try:
        with open(RSS_HEALTH_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_last_ok(last_ok: dict):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = RSS_HEALTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(last_ok, f)
        os.replace(tmp, RSS_HEALTH_PATH)
    except OSError:
        pass                                   # cache write failure is non-fatal


# ── main entry point ─────────────────────────────────────────────────────
def fetch_all(timeout: int = 10, fixtures: dict | None = None,
              persist: bool | None = None) -> dict:
    """Fetch every configured feed, dedupe across feeds, report health.

    fixtures: {feed_name: xml_text} — when given, NO network calls are
              made; feeds absent from the dict are marked unhealthy.
    persist:  write last_ok cache to state/rss_health.json
              (default: only on live fetches).

    Returns {'indian': [raw items newest-first], 'global': [...],
             'health': {'rss.<name>': {'ok', 'last_ok', 'error'}}}.
    """
    if persist is None:
        persist = fixtures is None
    last_ok = _load_last_ok()
    now_iso = datetime.now(timezone.utc).isoformat()
    health: dict = {}
    by_id: dict = {}

    for name, meta in FEEDS.items():
        key = f"rss.{name}"
        try:
            if fixtures is not None:
                if name not in fixtures:
                    raise RuntimeError("no fixture provided")
                items = parse_feed_text(name, fixtures[name])
            else:
                prev_to = socket.getdefaulttimeout()
                socket.setdefaulttimeout(timeout)
                try:
                    parsed = feedparser.parse(meta["url"], agent=USER_AGENT)
                finally:
                    socket.setdefaulttimeout(prev_to)
                if parsed.get("bozo") and not parsed.get("entries"):
                    raise RuntimeError(
                        str(parsed.get("bozo_exception") or "malformed feed"))
                status = parsed.get("status")
                if status and status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                items = [it for it in
                         (_entry_to_item(name, meta, e)
                          for e in parsed.get("entries", []))
                         if it]
            if not items:
                raise RuntimeError("0 entries")

            for it in items:                       # cross-feed dedupe
                prev = by_id.get(it["id"])
                if prev is None or it["source_weight"] > prev["source_weight"]:
                    by_id[it["id"]] = it
            last_ok[name] = now_iso
            health[key] = {"ok": True, "last_ok": now_iso, "error": None}
        except Exception as exc:                   # a dead feed never crashes
            health[key] = {"ok": False, "last_ok": last_ok.get(name),
                           "error": str(exc)[:200]}

    if persist:
        _save_last_ok(last_ok)

    indian = sorted((it for it in by_id.values() if it["region"] == "IN"),
                    key=_published_dt, reverse=True)
    global_ = sorted((it for it in by_id.values() if it["region"] != "IN"),
                     key=_published_dt, reverse=True)
    return {"indian": indian, "global": global_, "health": health}


# ── offline self-test (fixtures only, no network) ────────────────────────
if __name__ == "__main__":
    def _rss(*entries):
        body = "".join(
            f"<item><title>{t}</title><link>{l}</link>"
            f"<pubDate>{d}</pubDate></item>" for t, l, d in entries)
        return ("<?xml version='1.0'?><rss version='2.0'><channel>"
                f"<title>fx</title>{body}</channel></rss>")

    dup = "https://example.com/shared-story"
    fx = {
        "etmarkets": _rss(
            ("Sensex jumps 500 pts", "https://example.com/a",
             "Fri, 03 Jul 2026 09:30:00 GMT"),
            ("RBI holds repo rate", dup, "Fri, 03 Jul 2026 08:00:00 GMT")),
        "moneycontrol_top": _rss(
            ("RBI holds repo rate", dup, "Fri, 03 Jul 2026 08:05:00 GMT"),
            ("FII selling continues", "https://example.com/b",
             "Fri, 03 Jul 2026 10:00:00 GMT")),
        "cnbc_world": _rss(
            ("Fed signals rate cut", "https://example.com/c",
             "Fri, 03 Jul 2026 07:00:00 GMT")),
    }

    # parse_feed_text: shape + timestamps
    items = parse_feed_text("etmarkets", fx["etmarkets"])
    assert len(items) == 2
    for it in items:
        assert set(it) == {"id", "title", "link", "source", "published",
                           "source_weight", "region"}
    assert items[0]["published"].startswith("2026-07-03T09:30:00")
    assert items[0]["region"] == "IN" and items[0]["source_weight"] == 0.9

    out = fetch_all(fixtures=fx, persist=False)
    # dedupe across feeds keeps highest source_weight copy
    shared = [i for i in out["indian"] if i["id"] == news_id(dup)]
    assert len(shared) == 1 and shared[0]["source"] == "etmarkets"
    assert len(out["indian"]) == 3                 # a + b + deduped shared
    assert len(out["global"]) == 1
    # newest first
    pubs = [_published_dt(i) for i in out["indian"]]
    assert pubs == sorted(pubs, reverse=True)
    # health: every configured feed reported; missing fixtures degrade only
    assert set(out["health"]) == {f"rss.{n}" for n in FEEDS}
    assert out["health"]["rss.etmarkets"]["ok"] is True
    assert out["health"]["rss.nse_announcements"]["ok"] is False
    assert out["health"]["rss.nse_announcements"]["error"]
    # malformed xml degrades, does not crash
    bad = dict(fx, etmarkets="<not-xml")
    out2 = fetch_all(fixtures=bad, persist=False)
    assert out2["health"]["rss.etmarkets"]["ok"] is False
    print("rss.py self-test OK:",
          len(out["indian"]), "IN /", len(out["global"]), "GLOBAL items")

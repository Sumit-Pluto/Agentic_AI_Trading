"""Impact weighting of news items + tape-derived risk score.

score_items(items, tape, fii_dii) -> (weighted_top15, risk_score)

WEIGHT (0..100 per item) — a sigmoid-ish squash of impact points, decayed
by age:

    p      = T3 + T2 + T1 + source_weight * 15
      T3   = 25 pts per TIER3 keyword hit (macro shockers), capped at 50
      T2   = 12 pts per TIER2 keyword hit (market movers), capped at 36
      T1   =  5 pts per TIER1 hit (sector word / stock name), capped at 15
    base   = 100 * p / (p + 45)          # saturating, sigmoid-ish
    weight = base * exp(-age_min / 240)  # recency decay (~4 h half-life-ish)
             (undated items use a flat 0.85 multiplier)

Each item's `reasons` list records exactly which keywords fired
("T3:fed", "T2:crude", "T1:sector ENERGY", ...), the source term and the
decay applied.

TAPE ALERTS — big tape moves are synthesised as NewsItem-like entries
(source 'market-tape', weight 40..80 scaled by how far past the trigger
the move is):  |crude| > 2%, |usdinr| > 0.3%, |spx| > 1%, |us10y| > 3%,
|nikkei|/|hangseng| > 1.5%.

RISK SCORE in [-1, +1]  (+1 = strong risk-on, -1 = strong risk-off):

    risk_score = clamp( -0.35 * Σ_i clip(m_i, -1, +1)
                        -0.30 * clip(-fii_net_cr / 3000, -1, +1), -1, +1 )

    with risk-OFF-positive normalised tape moves m_i:
        m_usdinr =  chg% / 0.30      (rupee weakening   → risk-off)
        m_crude  =  chg% / 2.00      (brent, wti fallback; oil spike → off)
        m_spx    = -chg% / 1.00      (S&P falling       → risk-off)
        m_us10y  =  chg% / 3.00      (yields spiking    → risk-off)
    Missing quotes contribute 0; FII net selling of 3000 cr saturates the
    FII term.  (No VIX feed here, so the tape moves proxy for vol.)
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

try:
    from news.schema import news_id
    from news import sectors as _sectors
except ImportError:                                  # run as news/weighted.py
    from schema import news_id
    import sectors as _sectors

# ── impact keyword tiers ─────────────────────────────────────────────────
TIER3 = ["fed", "fomc", "rate hike", "rate cut", "rbi policy", "repo",
         "cpi", "inflation", "war", "air strike", "airstrike",
         "missile strike", "drone strike", "military strike", "sanctions",
         "tariff", "budget", "gdp", "recession", "default", "crash"]
TIER2 = ["fii", "dii", "crude", "opec", "earnings", "results", "downgrade",
         "upgrade", "merger", "acquisition", "ipo", "sebi", "rupee",
         "bond yield", "bond yields"]

T3_PTS, T3_CAP = 25.0, 50.0
T2_PTS, T2_CAP = 12.0, 36.0
T1_PTS, T1_CAP = 5.0, 15.0
SRC_MULT = 15.0
DECAY_TAU_MIN = 240.0
UNDATED_DECAY = 0.85
TOP_N = 15


def _kw_rx(words):
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % alt, re.I)


_T3_RX = [(w, _kw_rx([w])) for w in TIER3]
_T2_RX = [(w, _kw_rx([w])) for w in TIER2]


def _age_min(item, now) -> float | None:
    a = item.get("age_min")
    if isinstance(a, (int, float)) and a >= 0:
        return float(a)
    iso = item.get("published")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 60.0)
    except ValueError:
        return None


def _score_one(item, now) -> dict:
    title = item.get("title") or ""
    reasons = []

    t3 = [w for w, rx in _T3_RX if rx.search(title)]
    pts3 = min(T3_CAP, T3_PTS * len(t3))
    reasons += [f"T3:{w}" for w in t3]

    t2 = [w for w, rx in _T2_RX if rx.search(title)]
    pts2 = min(T2_CAP, T2_PTS * len(t2))
    reasons += [f"T2:{w}" for w in t2]

    if item.get("sectors") is not None or item.get("symbols") is not None:
        secs = item.get("sectors") or []
        syms = item.get("symbols") or []
    else:
        tagged = _sectors.tag(title)
        secs, syms = tagged["sectors"], tagged["symbols"]
    t1_hits = len(secs) + len(syms)
    pts1 = min(T1_CAP, T1_PTS * t1_hits)
    reasons += [f"T1:sector {s}" for s in secs]
    reasons += [f"T1:symbol {s}" for s in syms]

    src_w = float(item.get("source_weight", 0.5))
    src_pts = src_w * SRC_MULT
    reasons.append(f"src {src_w:.2f}x15={src_pts:.1f}")

    p = pts3 + pts2 + pts1 + src_pts
    base = 100.0 * p / (p + 45.0)

    age = _age_min(item, now)
    if age is None:
        decay, age_out = UNDATED_DECAY, 0.0
        reasons.append(f"decay {UNDATED_DECAY:.2f} (undated)")
    else:
        decay, age_out = math.exp(-age / DECAY_TAU_MIN), age
        reasons.append(f"decay {decay:.2f} (age {age:.0f}m)")

    return {"id": item.get("id") or news_id(item.get("link") or title),
            "title": title, "link": item.get("link") or "",
            "source": item.get("source") or "unknown",
            "published": item.get("published"),
            "age_min": round(age_out, 1),
            "sectors": secs, "symbols": syms,
            "weight": round(max(0.0, min(100.0, base * decay)), 1),
            "reasons": reasons}


# ── tape alerts ──────────────────────────────────────────────────────────
# key -> (threshold %, label, risk-off when move is positive?)
_TAPE_TRIGGERS = {
    "crude_brent": (2.0, "Brent crude", True),
    "crude_wti":   (2.0, "WTI crude", True),
    "usdinr":      (0.3, "USDINR", True),
    "spx":         (1.0, "S&P 500", False),
    "us10y":       (3.0, "US 10Y yield", True),
    "nikkei":      (1.5, "Nikkei", False),
    "hangseng":    (1.5, "Hang Seng", False),
    # overnight/pre-open leads for the India session
    "gift_nifty":  (0.75, "GIFT Nifty", False),
    "spx_fut":     (1.0, "S&P futures", False),
    "nasdaq_fut":  (1.2, "Nasdaq futures", False),
    "dow_fut":     (1.0, "Dow futures", False),
}


def tape_alerts(tape: dict, now=None) -> list[dict]:
    """Synthesise NewsItem-like alerts for outsized tape moves.
    Weight 40 at the trigger, +40 per additional 1x of the threshold,
    capped at 80."""
    now = now or datetime.now(timezone.utc)
    alerts = []
    for key, (thr, label, off_when_up) in _TAPE_TRIGGERS.items():
        q = (tape or {}).get(key)
        if not isinstance(q, dict):
            continue
        chg = q.get("chg_pct")
        if not isinstance(chg, (int, float)) or abs(chg) <= thr:
            continue
        w = min(80.0, 40.0 + 40.0 * (abs(chg) / thr - 1.0))
        risk_off = (chg > 0) == off_when_up
        link = f"tape://{key}/{q.get('asof') or now.isoformat()}"
        alerts.append({
            "id": news_id(link),
            "title": f"TAPE: {label} {chg:+.2f}% "
                     f"({'risk-off' if risk_off else 'risk-on'} move)",
            "link": link, "source": "market-tape",
            "published": q.get("asof") or now.isoformat(),
            "age_min": 0.0, "sectors": [], "symbols": [],
            "weight": round(w, 1),
            "reasons": [f"tape |{key}| {abs(chg):.2f}% > {thr:.2f}%"]})
    return alerts


# ── risk score ───────────────────────────────────────────────────────────
def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def risk_score(tape: dict, fii_dii: dict | None) -> float:
    """Composite risk appetite in [-1, +1]; formula in module docstring."""
    tape = tape or {}

    def chg(key):
        q = tape.get(key)
        c = q.get("chg_pct") if isinstance(q, dict) else None
        return float(c) if isinstance(c, (int, float)) else None

    comps = []
    c = chg("usdinr")
    if c is not None:
        comps.append(c / 0.30)
    c = chg("crude_brent")
    if c is None:
        c = chg("crude_wti")
    if c is not None:
        comps.append(c / 2.00)
    c = chg("spx")
    if c is not None:
        comps.append(-c / 1.00)
    c = chg("us10y")
    if c is not None:
        comps.append(c / 3.00)
    tape_off = sum(_clip(m) for m in comps)

    fii_off = 0.0
    if isinstance(fii_dii, dict):
        net = fii_dii.get("fii_net_cr")
        if isinstance(net, (int, float)):
            fii_off = _clip(-float(net) / 3000.0)

    return round(_clip(-0.35 * tape_off - 0.30 * fii_off), 3)


# ── main entry point ─────────────────────────────────────────────────────
def score_items(items: list, tape: dict, fii_dii: dict | None
                ) -> tuple[list, float]:
    """Score raw news items (from news/rss.py), merge in synthesised tape
    alerts, and return (top-15 by weight desc, risk_score)."""
    now = datetime.now(timezone.utc)
    scored = [_score_one(it, now) for it in (items or [])]
    scored += tape_alerts(tape or {}, now)
    scored.sort(key=lambda x: x["weight"], reverse=True)
    return scored[:TOP_N], risk_score(tape, fii_dii)


# ── offline self-test (fixtures only, no network) ────────────────────────
if __name__ == "__main__":
    _sectors.set_symbol_universe(list(_sectors.FALLBACK_FNO))
    now_iso = datetime.now(timezone.utc).isoformat()

    fixture_items = [
        {"title": "Fed signals rate hike as inflation stays hot",   # T3 x3
         "link": "https://x/t3", "source": "cnbc_world",
         "published": now_iso, "source_weight": 0.8, "region": "GLOBAL"},
        {"title": "Crude jumps after OPEC surprise; FII flows watched",  # T2
         "link": "https://x/t2", "source": "etmarkets",
         "published": now_iso, "source_weight": 0.8, "region": "IN"},
        {"title": "Tata Steel commissions new plant",              # T1 only
         "link": "https://x/t1", "source": "etmarkets",
         "published": now_iso, "source_weight": 0.8, "region": "IN"},
        {"title": "Firm opens new office campus in Pune",           # none
         "link": "https://x/t0", "source": "etmarkets",
         "published": now_iso, "source_weight": 0.8, "region": "IN"},
    ]

    risk_off_tape = {
        "usdinr":      {"price": 84.9, "chg_pct": 0.5,  "asof": now_iso},
        "crude_brent": {"price": 91.0, "chg_pct": 3.0,  "asof": now_iso},
        "spx":         {"price": 5900, "chg_pct": -1.8, "asof": now_iso},
        "us10y":       {"price": 4.6,  "chg_pct": 4.0,  "asof": now_iso},
        "nikkei":      {"price": 39000, "chg_pct": -2.0, "asof": now_iso},
        "hangseng":    None,                       # dead quote tolerated
        "gold":        {"price": 2400, "chg_pct": 0.2, "asof": now_iso},
    }

    top, rs = score_items(fixture_items, risk_off_tape,
                          {"date": "2026-07-03", "fii_net_cr": -2500.0,
                           "dii_net_cr": 1800.0})

    by_link = {t["link"]: t for t in top if t["link"].startswith("https")}
    w3 = by_link["https://x/t3"]["weight"]
    w2 = by_link["https://x/t2"]["weight"]
    w1 = by_link["https://x/t1"]["weight"]
    w0 = by_link["https://x/t0"]["weight"]
    assert w3 > w2 > w1 > w0, (w3, w2, w1, w0)      # tier ordering holds
    assert all(0 <= t["weight"] <= 100 for t in top)
    assert top == sorted(top, key=lambda x: x["weight"], reverse=True)

    r3 = by_link["https://x/t3"]["reasons"]
    assert any(r.startswith("T3:fed") for r in r3), r3
    assert any(r.startswith("T3:rate hike") for r in r3), r3
    r1 = by_link["https://x/t1"]["reasons"]
    assert any("METAL" in r for r in r1), r1
    assert any("TATASTEEL" in r for r in r1), r1

    # tape alerts synthesised: brent, usdinr, spx, us10y, nikkei (5 firing)
    tape_items = [t for t in top if t["source"] == "market-tape"]
    assert len(tape_items) == 5, [t["title"] for t in tape_items]
    assert all(40.0 <= t["weight"] <= 80.0 for t in tape_items)
    assert all("risk-off" in t["title"] for t in tape_items)

    # risk-off tape + FII selling -> strongly negative risk score
    assert -1.0 <= rs <= -0.5, rs

    # mirrored risk-on tape + FII buying -> positive risk score
    risk_on_tape = {
        "usdinr":      {"price": 84.1, "chg_pct": -0.4, "asof": now_iso},
        "crude_brent": {"price": 85.0, "chg_pct": -3.0, "asof": now_iso},
        "spx":         {"price": 6100, "chg_pct": 1.5,  "asof": now_iso},
        "us10y":       {"price": 4.1,  "chg_pct": -3.5, "asof": now_iso},
    }
    _, rs_on = score_items([], risk_on_tape,
                           {"date": "2026-07-03", "fii_net_cr": 2000.0,
                            "dii_net_cr": -100.0})
    assert rs_on >= 0.5, rs_on

    # neutral inputs -> ~0; missing tape/fii tolerated
    _, rs_neutral = score_items([], {}, None)
    assert rs_neutral == 0.0, rs_neutral

    # recency decay: same story 8h old must weigh less
    old = dict(fixture_items[0], link="https://x/t3old",
               published=None, age_min=480.0)
    top2, _ = score_items([fixture_items[0], old], {}, None)
    ws = {t["link"]: t["weight"] for t in top2}
    assert ws["https://x/t3"] > ws["https://x/t3old"], ws

    print(f"weighted.py self-test OK: risk_off={rs} risk_on={rs_on}; "
          f"top weights {w3}>{w2}>{w1}>{w0}")

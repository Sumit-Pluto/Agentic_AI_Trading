"""engine/watchlist.py — saved watchlists with per-strategy P&L-since-add.

You save a scanned stock (e.g. an OI-strategy hit) to a watchlist to TEST that
strategy over time. P&L is measured from the moment you added it
(``price_at_add``) and attributed to that strategy ONLY — so you can judge
whether the strategy actually performs, independent of the global book.

Each item: {id, symbol, strategy, direction, segment, added_at, price_at_add,
note}. P&L is computed live via an injected ``price_fn(symbol, segment)`` (i.e.
``DataHub.price``). Persisted atomically to state/watchlists.json.

Offline smoke test: ``python -m engine.watchlist``.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

STORE = os.path.join("state", "watchlists.json")


class WatchlistStore:
    def __init__(self, price_fn: Callable[[str, Optional[str]], Optional[float]],
                 clock: Callable[[], str] | None = None):
        self.price_fn = price_fn
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._load()

    # ── mutations ────────────────────────────────────────────────────────
    def add(self, *, symbol: str, strategy: str, direction: str = "BUY",
            segment: str | None = None, note: str = "",
            price_at_add: float | None = None) -> dict:
        symbol = symbol.upper()
        px = price_at_add
        if px is None:
            try:
                px = self.price_fn(symbol, segment)
            except Exception:
                px = None
        item = {"id": uuid.uuid4().hex[:8], "symbol": symbol,
                "strategy": strategy, "direction": direction.upper(),
                "segment": segment, "added_at": self._clock(),
                "price_at_add": round(float(px), 4) if px else None, "note": note}
        with self._lock:
            self.items.append(item)
            self._save()
        return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != item_id]
            changed = len(self.items) != before
            if changed:
                self._save()
        return changed

    # ── P&L since add ────────────────────────────────────────────────────
    def _pnl_of(self, item: dict) -> dict | None:
        ref = item.get("price_at_add")
        if not ref:
            return None
        try:
            cur = self.price_fn(item["symbol"], item.get("segment"))
        except Exception:
            cur = None
        if not cur:
            return None
        sign = 1.0 if item.get("direction", "BUY") == "BUY" else -1.0
        chg = (cur - ref) * sign
        return {"current": round(cur, 2), "change": round(chg, 2),
                "change_pct": round(chg / ref * 100.0, 2)}

    def list(self, strategy: str | None = None) -> list[dict]:
        with self._lock:
            items = [dict(i) for i in self.items]
        out = []
        for i in items:
            if strategy and i["strategy"] != strategy:
                continue
            i["pnl"] = self._pnl_of(i)
            out.append(i)
        return out

    def strategy_pnl(self, strategy: str) -> dict:
        """Aggregate P&L-since-add for ONE strategy's watchlist (not global)."""
        items = self.list(strategy)
        total_pct, scored, wins = 0.0, 0, 0
        for i in items:
            p = i.get("pnl")
            if p:
                total_pct += p["change_pct"]
                scored += 1
                if p["change_pct"] > 0:
                    wins += 1
        return {"strategy": strategy, "count": len(items), "scored": scored,
                "avg_change_pct": round(total_pct / scored, 2) if scored else None,
                "win_rate": round(wins / scored * 100.0, 1) if scored else None,
                "items": items}

    def strategies(self) -> list[str]:
        with self._lock:
            return sorted({i["strategy"] for i in self.items})

    def overview(self) -> dict:
        """Per-strategy summaries for the Watchlist tab."""
        return {"strategies": [self.strategy_pnl(s) for s in self.strategies()]}

    # ── persistence (atomic) ─────────────────────────────────────────────
    def _save(self) -> None:
        try:
            os.makedirs("state", exist_ok=True)
            tmp = STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"items": self.items}, f)
            os.replace(tmp, STORE)
        except Exception:
            pass

    def _load(self) -> None:
        try:
            with open(STORE) as f:
                self.items = json.load(f).get("items", [])
        except (OSError, ValueError):
            self.items = []


if __name__ == "__main__":
    # Offline smoke test — no network, in-memory price map.
    prices = {"RELIANCE": 2900.0, "GOLD": 71000.0}

    def price_fn(sym, seg=None):
        return prices.get(sym)

    import tempfile
    STORE = os.path.join(tempfile.mkdtemp(), "watchlists.json")
    wl = WatchlistStore(price_fn, clock=lambda: "2026-07-10T10:00:00")

    a = wl.add(symbol="RELIANCE", strategy="oi", direction="BUY")
    wl.add(symbol="GOLD", strategy="oi", direction="BUY", segment="MCX")
    wl.add(symbol="RELIANCE", strategy="swing", direction="SELL")
    assert a["price_at_add"] == 2900.0

    # price moves: RELIANCE up 1%, GOLD down ~1.4%
    prices["RELIANCE"] = 2929.0
    prices["GOLD"] = 70000.0

    oi = wl.strategy_pnl("oi")
    assert oi["count"] == 2 and oi["scored"] == 2
    # RELIANCE BUY +1.0%, GOLD BUY -1.408% -> avg ~ -0.20%
    got = {i["symbol"]: i["pnl"]["change_pct"] for i in oi["items"]}
    assert abs(got["RELIANCE"] - 1.0) < 1e-6, got
    assert abs(got["GOLD"] - (-1.408)) < 0.01, got

    # swing strategy: RELIANCE SELL at 2900 -> now 2929 -> SELL loses -1.0%
    sw = wl.strategy_pnl("swing")
    assert abs(sw["items"][0]["pnl"]["change_pct"] - (-1.0)) < 1e-6, sw

    # per-strategy isolation: oi list excludes the swing item
    assert all(i["strategy"] == "oi" for i in wl.list("oi"))
    assert wl.strategies() == ["oi", "swing"]

    # remove
    assert wl.remove(a["id"]) is True
    assert wl.strategy_pnl("oi")["count"] == 1

    print("watchlist smoke test: OK "
          f"(per-strategy P&L-since-add isolated; oi avg={oi['avg_change_pct']}%)")

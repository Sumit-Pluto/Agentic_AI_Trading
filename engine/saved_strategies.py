"""engine/saved_strategies.py — user-saved option-strategy structures (from the
Strategy Advisor) with LIVE mark-to-market P&L.

A structure is multi-leg, so its P&L is not a single stock price move: we store
each leg (side, CE/PE, strike, entry premium) and the entry net premium, then
re-price the legs off the live option chain to get the current net. With the
convention net = Σ sign·premium (BUY=+1, SELL=-1), the entry cost equals
``net_premium_per_share`` from the advisor (DEBIT>0 / CREDIT<0), and:

    P&L per share = current_net - entry_net     (you are long the structure)

e.g. a credit spread sold for -10 net, now worth -5 to close -> +5 profit.

Persisted atomically to state/saved_strategies.json. Live pricing uses an
injected ``chain_fn(symbol) -> ChainSnapshot|None`` (i.e. DataHub.chain_snapshot).

Offline smoke test: ``python -m engine.saved_strategies``.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

STORE = os.path.join("state", "saved_strategies.json")


def _f(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


class SavedStrategies:
    def __init__(self, chain_fn: Callable[[str], object],
                 clock: Callable[[], str] | None = None):
        self.chain_fn = chain_fn
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._load()

    # ── mutations ────────────────────────────────────────────────────────
    def add(self, *, symbol: str, label: str, legs: list,
            entry_net_per_share, lot=None, metrics: dict | None = None) -> dict:
        norm: list[dict] = []
        for l in (legs or []):
            try:
                otype = (l.get("type")
                         or ("CE" if l.get("is_call") else "PE")).upper()
                norm.append({"side": str(l.get("side", "")).upper(),
                             "opt_type": "CE" if otype.startswith("C") else "PE",
                             "strike": float(l.get("strike")),
                             "entry_premium": _f(l.get("premium")),
                             "tsym": l.get("tsym") or ""})
            except (TypeError, ValueError, AttributeError):
                continue
        item = {"id": uuid.uuid4().hex[:8], "symbol": str(symbol).upper(),
                "label": label or "structure", "legs": norm,
                "entry_net_per_share": _f(entry_net_per_share),
                "lot": _f(lot), "metrics": metrics or {},
                "saved_at": self._clock()}
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

    # ── live pricing ─────────────────────────────────────────────────────
    def _current_net(self, item: dict):
        """Re-price legs off the live chain -> (net_per_share, spot) or (None,None)."""
        try:
            chain = self.chain_fn(item["symbol"])
        except Exception:
            chain = None
        if not chain or not getattr(chain, "strikes", None):
            return None, None
        by = {}
        for r in chain.strikes:
            try:
                by[round(float(r.strike), 2)] = r
            except (TypeError, ValueError):
                continue
        net = 0.0
        for l in item.get("legs", []):
            row = by.get(round(float(l["strike"]), 2))
            leg = getattr(row, "ce" if l["opt_type"] == "CE" else "pe", None) if row else None
            ltp = getattr(leg, "ltp", None) if leg else None
            if ltp is None:
                return None, None                 # can't price whole structure
            net += (1.0 if l["side"] == "BUY" else -1.0) * float(ltp)
        return net, getattr(chain, "spot", None)

    def list_with_pnl(self) -> list[dict]:
        with self._lock:
            items = [dict(i) for i in self.items]
        out = []
        for it in items:
            cur_net, spot = self._current_net(it)
            entry = it.get("entry_net_per_share")
            pnl = (round(cur_net - entry, 2)
                   if cur_net is not None and entry is not None else None)
            lot = it.get("lot")
            it["current_net_per_share"] = None if cur_net is None else round(cur_net, 2)
            it["spot"] = None if spot is None else round(float(spot), 2)
            it["pnl_per_share"] = pnl
            it["pnl_per_lot"] = None if (pnl is None or not lot) else round(pnl * lot, 2)
            out.append(it)
        return out

    def overview(self) -> dict:
        rows = self.list_with_pnl()
        priced = [r for r in rows if r.get("pnl_per_share") is not None]
        total_lot = sum(r["pnl_per_lot"] for r in rows
                        if r.get("pnl_per_lot") is not None)
        return {"count": len(rows), "priced": len(priced),
                "total_pnl_per_lot": round(total_lot, 2) if priced else None,
                "items": rows}

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
    # Offline smoke test — stub chain, no network.
    import tempfile
    from types import SimpleNamespace as NS
    STORE = os.path.join(tempfile.mkdtemp(), "saved_strategies.json")

    # credit spread: SELL PE 1580 @82.3, BUY PE 1570 @72.3  -> entry_net = -10
    prices = {(1580.0, "PE"): 82.3, (1570.0, "PE"): 72.3}

    def chain_fn(sym):
        rows = []
        for k in (1570.0, 1580.0):
            rows.append(NS(strike=k,
                           pe=NS(ltp=prices.get((k, "PE")), oi=1000),
                           ce=NS(ltp=5.0, oi=1000)))
        return NS(strikes=rows, spot=1574.0)

    ss = SavedStrategies(chain_fn, clock=lambda: "2026-07-11T19:00:00")
    it = ss.add(symbol="TESTSTK", label="short put spread",
                legs=[{"side": "SELL", "type": "PE", "strike": 1580.0, "premium": 82.3},
                      {"side": "BUY", "type": "PE", "strike": 1570.0, "premium": 72.3}],
                entry_net_per_share=-10.0, lot=250)
    ov = ss.overview()
    r = ov["items"][0]
    assert r["pnl_per_share"] == 0.0, r          # unchanged -> flat
    # premiums decay (good for a credit seller): PE 1580 -> 40, PE 1570 -> 35
    prices[(1580.0, "PE")] = 40.0
    prices[(1570.0, "PE")] = 35.0
    r = ss.list_with_pnl()[0]
    # cur_net = -40 + 35 = -5 ; pnl = -5 - (-10) = +5/share ; *250 = +1250/lot
    assert r["pnl_per_share"] == 5.0, r
    assert r["pnl_per_lot"] == 1250.0, r
    assert ss.remove(it["id"]) is True
    print("saved_strategies smoke test: OK "
          "(multi-leg re-priced from chain; credit-spread decay -> +5/sh, "
          "+1250/lot)")

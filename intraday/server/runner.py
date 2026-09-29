"""EngineRunner — drives the SessionLoop on a background thread and broadcasts a
live snapshot to WebSocket subscribers each tick.

Demo mode (default): SimContext generates a market so the whole agentic system
trades on screen with no broker. Live mode: LiveContext + the Gateway (VPS).
Everything else — agents, risk, rules, exits, journal — is identical (single
code path).
"""
from __future__ import annotations

import asyncio
import math
import threading
import time

from ..agents.base import chain_features
from ..config import load as load_cfg
from ..contracts import Brain
from ..journal.store import Store
from ..options import greeks_for
from ..runtime import SessionLoop


def _num(x):
    """JSON-safe number (nan/inf -> None)."""
    try:
        f = float(x)
        return round(f, 4) if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class EngineRunner:
    def __init__(self, cfg: dict | None = None, mode: str | None = None):
        self.cfg = cfg or load_cfg()
        self.mode = mode or self.cfg.get("engine_mode", "sim")   # "sim" | "live"
        self.store = Store()
        self.ctx = self._make_ctx()
        self.loop = SessionLoop(self.ctx, self.store, self.cfg, brain=Brain.equal())
        self.step_seconds = float(self.cfg.get("demo_step_seconds", 2.0))
        self._subs: set[asyncio.Queue] = set()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._seq = 0

    def _make_ctx(self):
        if self.mode == "live":
            from ..data.context import LiveContext
            from ..gateway_client import GatewayClient
            return LiveContext(GatewayClient(), self.cfg)
        from ..data.sim import SimContext
        return SimContext(self.cfg)

    # ---------- lifecycle ----------
    def start(self, async_loop: asyncio.AbstractEventLoop):
        self._async_loop = async_loop
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="engine-loop", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # prime one snapshot immediately so a client connecting sees data at once
        self._tick()
        while not self._stop.wait(self.step_seconds):
            self._tick()

    def _tick(self):
        try:
            st = self.loop.step()
        except Exception as e:
            st = {"error": f"{type(e).__name__}: {e}"}
        if self.mode == "sim":
            try:
                self.ctx.step()
            except Exception:
                pass
        snap = self._snapshot(st)
        with self._lock:
            self._snap = snap
        self._broadcast(snap)

    # ---------- snapshot ----------
    def _snapshot(self, st: dict) -> dict:
        self._seq += 1
        now = self.ctx.now()
        chains = {s: self._chain_view(self.ctx.chain(s)) for s in self.ctx.symbols()}
        return {
            "seq": self._seq,
            "state": {
                "mode": self.mode, "paused": bool(self.cfg.get("paused", False)),
                "halted": self.loop.halted, "now": now.isoformat(),
                "square_off": self.ctx.is_square_off(now),
                "equity": _num(st.get("equity")), "realized": _num(st.get("realized")),
                "unrealized": _num(st.get("unrealized")),
                "day_pnl": _num((st.get("realized") or 0) + (st.get("unrealized") or 0)),
                "n_positions": st.get("positions", 0), "entries": st.get("entries", 0),
                "vix": _num(getattr(self.ctx, "vix", lambda: None)()),
                "regime": (self.loop.last_scan or {}).get("regime", {}),
            },
            "positions": [self._pos_view(p) for p in self.loop.positions],
            "signals": (self.loop.last_scan or {}).get("signals", []),
            "agent_rows": (self.loop.last_scan or {}).get("rows", []),
            "chains": chains,
            "equity_point": {"t": now.isoformat(), "equity": _num(st.get("equity"))},
        }

    def _pos_view(self, p) -> dict:
        is_call = str(p.right).upper().startswith("C")
        q = None
        ch = self.ctx.chain(p.underlying)
        if ch:
            q = ch.get(p.strike, is_call)
        mark = (q.bid if p.is_long else q.ask) if q else p.entry_px
        pnl = (1 if p.is_long else -1) * ((mark or p.entry_px) - p.entry_px) * p.qty
        return {"symbol": p.symbol, "underlying": p.underlying, "side": p.side,
                "right": p.right, "strike": p.strike, "qty": p.qty,
                "lots": p.qty // (p.lot_size or 1), "entry_px": _num(p.entry_px),
                "mark": _num(mark), "pnl": _num(pnl), "stop": _num(p.stop),
                "age_bars": p.age_bars, "strategy": p.strategy,
                "entry_ts": p.entry_ts.isoformat()}

    def _chain_view(self, ch) -> dict | None:
        if ch is None:
            return None
        rows = []
        for k in ch.strikes:
            rows.append({"strike": k, "ce": self._leg_view(ch.get(k, True), ch),
                         "pe": self._leg_view(ch.get(k, False), ch)})
        f = {k: _num(v) if isinstance(v, (int, float)) else v
             for k, v in (chain_features(ch) or {}).items()}
        return {"symbol": ch.symbol, "spot": _num(ch.spot), "atm": _num(ch.atm),
                "expiry": ch.expiry.isoformat(), "lot_size": ch.lot_size,
                "features": f, "rows": rows}

    def _leg_view(self, q, ch) -> dict | None:
        if q is None:
            return None
        g = greeks_for(q, ch)
        return {"ltp": _num(q.ltp), "bid": _num(q.bid), "ask": _num(q.ask),
                "oi": q.oi, "d_oi": q.d_oi, "volume": q.volume,
                "iv": _num((q.iv or 0) * 100) if q.iv else None,
                "delta": _num(g.get("delta"))}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    # ---------- pub/sub ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def _broadcast(self, snap: dict):
        if not self._async_loop:
            return
        for q in list(self._subs):
            try:
                self._async_loop.call_soon_threadsafe(self._safe_put, q, snap)
            except RuntimeError:
                pass

    @staticmethod
    def _safe_put(q: asyncio.Queue, snap: dict):
        if q.full():
            try:
                q.get_nowait()          # drop the oldest for a slow client
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(snap)
        except asyncio.QueueFull:
            pass

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
        # engine_mode = DATA source (sim | live); cfg["mode"] = BROKER (paper | live)
        self.engine_mode = mode or self.cfg.get("engine_mode", "sim")
        self.store = Store()
        self.client = self._build_client()
        self.ctx = self._make_ctx()
        self.loop = SessionLoop(self.ctx, self.store, self.cfg, brain=Brain.equal(),
                                broker=self._make_broker(str(self.cfg.get("mode", "paper"))))
        self._wire_funds()
        self.step_seconds = float(self.cfg.get("demo_step_seconds", 2.0))
        self._subs: set[asyncio.Queue] = set()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._seq = 0
        self._funds_at, self._funds_val = 0.0, None

    @property
    def mode(self) -> str:                    # data-source label (for the snapshot)
        return self.engine_mode

    def _build_client(self):
        """A GatewayClient if creds are configured, else None (local dev / sim)."""
        try:
            from ..gateway_client import GatewayClient
            return GatewayClient()
        except Exception as e:
            print(f"[runner] gateway client unavailable ({e}); sim data / paper broker")
            return None

    def _make_ctx(self):
        if self.engine_mode == "live":
            # LIVE = REAL data only. Never silently fall back to the simulator —
            # if the gateway client is missing, fail loudly so no dummy data is
            # ever served on a live/production deployment.
            if self.client is None:
                raise RuntimeError(
                    "engine_mode=live but no Gateway client (set GATEWAY_CLIENT_ID / "
                    "GATEWAY_CLIENT_SECRET). Refusing to fall back to the simulated "
                    "market — live runs on REAL gateway data only.")
            from ..data.chains_db import GatewayChainsContext
            return GatewayChainsContext(self.client, self.cfg)
        from ..data.sim import SimContext      # sim is for local/offline testing only
        return SimContext(self.cfg)

    def _make_broker(self, mode: str):
        def _f(key: str, default: float) -> float:
            try:
                return float(self.cfg.get(key, default))
            except (TypeError, ValueError):
                return default
        if str(mode).lower() == "live" and self.client is not None:
            from ..brokers import GatewayBroker
            return GatewayBroker(  # MIS: distinct from snowball/swing NRML
                self.client, product_type="I",
                confirm_timeout_s=_f("live_confirm_timeout_s", 12.0),
                poll_s=_f("live_fill_poll_s", 1.0))
        from ..brokers import PaperBroker
        return PaperBroker(_f("slippage_pct", 0.10))

    def _wire_funds(self):
        live = str(self.cfg.get("mode", "paper")).lower() == "live" and self.client is not None
        self.loop.funds_provider = (lambda: self._funds()) if live else None

    def _funds(self):
        if time.time() - self._funds_at < 5 and self._funds_val is not None:
            return self._funds_val
        try:
            self._funds_val = self.client.funds() if self.client else None
        except Exception:
            self._funds_val = None
        self._funds_at = time.time()
        return self._funds_val

    def set_mode(self, mode: str, confirm: str = "") -> dict:
        """Toggle PAPER<->LIVE. LIVE requires typed confirm + a Gateway client
        + REAL gateway data: live orders on the simulated feed are refused."""
        mode = str(mode).lower()
        if mode == "live":
            if confirm != "LIVE":
                return {"error": "type LIVE to confirm real-money trading"}
            if self.client is None:
                self.client = self._build_client()
            if self.client is None:
                return {"error": "no Gateway client — set GATEWAY_CLIENT_ID/SECRET and "
                                 "ensure the Gateway broker session is connected"}
            if self.engine_mode != "live":
                return {"error": "refusing LIVE broker on simulated data — "
                                 "restart with engine_mode=live (real Gateway "
                                 "data) before trading real money"}
        self.cfg["mode"] = mode
        try:
            from .. import config as _c
            _c.save(self.cfg)
        except Exception:
            pass
        self.loop.set_broker(self._make_broker(mode))
        self._wire_funds()
        return {"mode": mode, "broker": self.loop.broker.name}

    # ---------- lifecycle ----------
    def start(self, async_loop: asyncio.AbstractEventLoop):
        self._async_loop = async_loop
        if self._thread and self._thread.is_alive():
            return
        # restart recovery BEFORE the first tick (single-threaded here, no race):
        # journal OPEN rows become tracked positions again, then LIVE mode
        # reconciles them against the broker book (ghosts closed, unknowns
        # adopted, qty mismatches resolved to broker truth).
        try:
            self.loop.restore_open_positions()
            live = (str(self.cfg.get("mode", "paper")).lower() == "live"
                    and getattr(self.loop.broker, "name", "") == "gateway")
            if live:
                try:
                    bpos = self.loop.broker.positions()
                except Exception as e:
                    bpos = None
                    self.loop._log("reconcile", f"broker book unreadable ({e}) — "
                                                "journal state kept, verify manually")
                if bpos is not None:
                    self.loop.reconcile_with_broker(bpos)
        except Exception as e:
            print(f"[runner] startup recovery failed ({e}); starting without it")
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
        gov = self.loop.governor
        pos = self.loop.positions
        trading_mode = str(self.cfg.get("mode", "paper"))
        funds = self._funds() if (self.client and trading_mode.lower() == "live") else None
        budget = {
            "total": _num(gov.budget()), "deployed": _num(gov.deployed_premium(pos)),
            "utilisation_pct": _num(gov.utilisation_pct(pos)),
            "cap_state": gov.cap_state(pos),
            "soft_cap_pct": self.cfg.get("soft_cap_pct"),
            "hard_cap_pct": self.cfg.get("hard_cap_pct"),
        }
        return {
            "seq": self._seq,
            "state": {
                "mode": self.mode, "trading_mode": trading_mode,
                "broker": self.loop.broker.name,   # the object orders really go to
                "data_source": type(self.ctx).__name__,   # GatewayChainsContext=real | SimContext=sim
                "live_data": self.engine_mode == "live",
                "paused": bool(self.cfg.get("paused", False)),
                "halted": self.loop.halted, "now": now.isoformat(),
                "square_off": self.ctx.is_square_off(now),
                "equity": _num(st.get("equity")), "realized": _num(st.get("realized")),
                "unrealized": _num(st.get("unrealized")),
                "day_pnl": _num((st.get("realized") or 0) + (st.get("unrealized") or 0)),
                "n_positions": st.get("positions", 0), "entries": st.get("entries", 0),
                "cap_state": budget["cap_state"],
                "vix": _num(getattr(self.ctx, "vix", lambda: None)()),
                "regime": (self.loop.last_scan or {}).get("regime", {}),
            },
            "budget": budget,
            "funds": funds,
            "positions": [self._pos_view(p) for p in pos],
            "signals": (self.loop.last_scan or {}).get("signals", []),
            "agent_rows": (self.loop.last_scan or {}).get("rows", []),
            "activity": list(self.loop.activity),
            "scan": list(getattr(self.loop.scanner, "progress", [])),
            "chains": chains,
            "equity_point": {"t": now.isoformat(), "equity": _num(st.get("equity"))},
        }

    def _pos_view(self, p) -> dict:
        if str(p.right).upper() == "FUT":
            mark = self.loop._fut_mark(p.underlying, p.entry_px) or p.entry_px
        else:
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

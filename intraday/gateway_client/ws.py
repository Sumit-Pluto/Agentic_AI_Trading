"""Live WebSocket subscriber to the Gateway (/api/ws/ticker).

Mints a fresh service token per (re)connect, subscribes its symbol set on the
touchline feed (price) and/or the depth feed (OI — the mode added to the
Gateway in Phase 0), and dispatches frames to callbacks:

    on_tick(symbol_token, msg)   # touchline: has 'lp'
    on_depth(symbol_token, msg)  # {'type':'depth'}: has 'oi'/'poi'
    on_order(msg)                # {'type':'order_update'}

Background thread with bounded-backoff reconnect + re-subscribe, mirroring the
Gateway's own ticker resilience. Live-only (needs websocket-client + a running
Gateway); exercised on the VPS.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from .client import GatewayClient
from .config import GatewaySettings


class GatewayWS:
    def __init__(self, settings: GatewaySettings | None = None,
                 on_tick: Callable | None = None,
                 on_depth: Callable | None = None,
                 on_order: Callable | None = None):
        self.settings = settings or GatewaySettings.load()
        self.on_tick = on_tick
        self.on_depth = on_depth
        self.on_order = on_order
        self._touch: set[str] = set()      # EXCHANGE|TOKEN on touchline
        self._depth: set[str] = set()      # EXCHANGE|TOKEN on depth
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = GatewayClient(self.settings)   # token minting

    def subscribe(self, symbols: list[str], feed: str = "touchline") -> None:
        tgt = self._depth if feed == "depth" else self._touch
        new = [s for s in symbols if s not in tgt]
        tgt.update(new)
        if self._ws is not None and new:
            try:
                self._ws.send(json.dumps({"action": "subscribe", "symbols": new, "feed": feed}))
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gateway-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    def _token(self) -> str:
        if self._client._token is None:
            self._client._login()
        return self._client._token or ""

    def _run(self) -> None:
        try:
            from websocket import create_connection
        except Exception as e:   # pragma: no cover
            print(f"[GatewayWS] websocket-client not installed: {e}")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                token = self._token()
                url = f"{self.settings.ws_url}?token={token}"
                self._ws = create_connection(url, timeout=15,
                                             enable_multithread=True)
                # (re)subscribe everything on connect
                if self._touch:
                    self._ws.send(json.dumps({"action": "subscribe",
                                              "symbols": list(self._touch), "feed": "touchline"}))
                if self._depth:
                    self._ws.send(json.dumps({"action": "subscribe",
                                              "symbols": list(self._depth), "feed": "depth"}))
                backoff = 1.0
                self._ws.settimeout(30)
                while not self._stop.is_set():
                    raw = self._ws.recv()
                    if not raw:
                        continue
                    self._dispatch(raw)
            except Exception as e:
                if self._stop.is_set():
                    break
                print(f"[GatewayWS] disconnected: {e}; reconnecting in {backoff:.0f}s")
                self._client._token = None      # force a fresh token next connect
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        typ = msg.get("type")
        if typ == "order_update":
            if self.on_order:
                self.on_order(msg)
            return
        tk = msg.get("tk", "")
        sym = f"{msg.get('e', '')}|{tk}" if tk else ""
        if typ == "depth":
            if self.on_depth:
                self.on_depth(sym, msg)
        elif "lp" in msg:
            if self.on_tick:
                self.on_tick(sym, msg)

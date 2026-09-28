"""TickerManager: bridges Shoonya NorenApi WebSocket to frontend FastAPI WebSocket clients."""

import asyncio
import json
import time
from typing import Callable, Optional

from fastapi import WebSocket

from session_windows import is_session_open

# --- self-healing feed ------------------------------------------------------
# A human establishes the broker session (POST /api/connect); the gateway is
# responsible for keeping the WebSocket alive after that. When the socket drops
# or goes silent we reopen it with the SAME session — no human, no restart. Only
# when the session itself has expired (reopen keeps failing) do we hand it back
# to the human with a "please reconnect" alert.
_STALE_SECONDS = 30.0          # no tick this long during an open session → force reconnect
_WATCHDOG_INTERVAL = 10.0      # how often the stale-feed watchdog checks
_REOPEN_WAIT = 15.0            # wait this long for _on_open to fire after a reopen
_RECONNECT_BACKOFF = (2.0, 5.0, 10.0)   # per-attempt wait; exhausting these ⇒ session expired


class TickerManager:
    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._subscribed: set[str] = set()   # "NSE|22" / "MCX|432556" format
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._broker = None
        self.is_running: bool = False    # True after start_websocket() is called
        self._ws_open: bool = False      # True only after _on_open fires (WS actually ready)
        self.on_tick_callback: Optional[Callable] = None             # async fn called on every tick
        self.on_order_update_callback: Optional[Callable] = None     # async fn called on every order update
        self.on_depth_callback: Optional[Callable] = None            # async fn called on every depth (snapquote) frame

        # Depth-feed (SNAPQUOTE) subscriptions, tracked separately from the
        # touchline set. The depth feed is the only one carrying open interest,
        # so an intraday OI/flow consumer subscribes its option legs here. See
        # subscribe(..., feed="depth"). Kept distinct because a symbol can be on
        # both feeds and each must be unsubscribed with the feed it was sent on.
        self._depth_subscribed: set[str] = set()   # "NFO|12345" on the SNAPQUOTE feed
        self._snapquote = None                     # cached FeedType.SNAPQUOTE (lazy)

        # session held from the human's connect, reused to reopen the socket
        self._auth_token: Optional[str] = None
        self._uid: Optional[str] = None

        # self-healing state
        self._last_tick_at: float = 0.0        # monotonic of the last tick seen
        self._intentional_stop: bool = False   # True after stop() — suppress auto-reconnect
        self._reconnecting: bool = False       # guard against overlapping reconnects
        self._session_expired: bool = False    # latched when reopen keeps failing
        self._watchdog_started: bool = False

    def start(self, broker, auth_token: str, uid: str, loop: asyncio.AbstractEventLoop) -> None:
        """
        Start the Shoonya WebSocket connection.
        `loop` must be the running event loop, passed from the async route handler
        via asyncio.get_running_loop().
        """
        if self.is_running:
            return
        self._loop = loop
        self._broker = broker
        self._auth_token = auth_token
        self._uid = uid
        # A fresh human connect clears any prior "expired" latch and stop flag.
        self._intentional_stop = False
        self._session_expired = False
        broker._api.set_credentials(auth_token, uid, uid)
        broker.start_websocket(
            subscribe_callback=self._on_tick,
            socket_open_callback=self._on_open,
            socket_close_callback=self._on_close,
            order_update_callback=self._on_order_update,
        )
        self.is_running = True
        self._ensure_watchdog()
        print(f"[TickerManager] start_websocket called, waiting for _on_open...")

    def stop(self) -> None:
        """Human-initiated disconnect (POST /api/disconnect).

        Marks the stop as intentional so the socket-close callback does NOT
        auto-reconnect, then tears the Shoonya WebSocket down.
        """
        self._intentional_stop = True
        self.is_running = False
        self._ws_open = False
        if self._broker is not None:
            try:
                self._broker._stop_websocket()
            except Exception as e:
                print(f"[TickerManager] stop: teardown failed: {e}")

    def _ensure_watchdog(self) -> None:
        """Launch the stale-feed watchdog once, on the event loop."""
        if self._watchdog_started or self._loop is None:
            return
        self._watchdog_started = True
        self._loop.create_task(self._watchdog_loop())

    def _snapquote_feed(self):
        """FeedType.SNAPQUOTE (the depth feed), imported lazily and cached.

        Kept out of module scope so importing ticker_manager never requires the
        broker SDK; depth subscription genuinely needs it, so failing here is
        the correct behaviour when it is absent."""
        if self._snapquote is None:
            from NorenRestApiPy.NorenApi import FeedType
            self._snapquote = FeedType.SNAPQUOTE
        return self._snapquote

    # ------------------------------------------------------------------
    # NorenApi callbacks (called from background thread)
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        self._ws_open = True
        # A successful (re)open means the session is alive again — clear the
        # expired latch and give the watchdog a fresh grace period.
        self._session_expired = False
        self._last_tick_at = time.monotonic()
        print(f"[TickerManager] Shoonya WS connected. Subscribing {len(self._subscribed)} queued symbols: {self._subscribed}")
        for sym in list(self._subscribed):
            print(f"[TickerManager] _on_open subscribing: {sym}")
            self._broker.ws_subscribe(sym)
        if self._depth_subscribed:
            try:
                sq = self._snapquote_feed()
                for sym in list(self._depth_subscribed):
                    print(f"[TickerManager] _on_open subscribing (depth): {sym}")
                    self._broker.ws_subscribe(sym, sq)
            except Exception as e:
                print(f"[TickerManager] depth re-subscribe failed: {e}")
        try:
            self._broker.ws_subscribe_orders()
            print("[TickerManager] subscribed to broker order updates")
        except Exception as e:
            print(f"[TickerManager] order-update subscribe failed: {e}")

    def _on_close(self) -> None:
        # Fires from NorenApi's background thread. Don't drop is_running here —
        # the reconnect coroutine owns the session lifecycle now. Just kick a
        # reconnect, unless the human deliberately disconnected.
        print("[TickerManager] Shoonya WS disconnected.")
        self._ws_open = False
        if self._intentional_stop:
            self.is_running = False
            return
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._reconnect("socket closed"), self._loop)

    def _on_tick(self, tick: dict) -> None:
        # Diagnostic tap, ahead of the tf/tk filter below. Off unless something
        # sets it, so normal operation is byte-for-byte unchanged; it exists
        # because depth ('dk'/'df') messages are otherwise dropped here and we
        # cannot see what that feed actually carries.
        probe = getattr(self, "raw_probe", None)
        if probe is not None:
            try:
                probe(tick)
            except Exception:
                pass
        if not self._loop:
            return
        t = tick.get("t")
        # "dk" = depth ack, "df" = depth feed (SNAPQUOTE). These carry open
        # interest (oi/poi) and the bid/ask ladder — the intraday OI/flow feed.
        # Forwarded only to clients that opted into depth (feed="depth"); a
        # pure-touchline UI simply never sees a {"type":"depth"} frame.
        if t in ("dk", "df"):
            self._last_tick_at = time.monotonic()
            if self._clients:
                asyncio.run_coroutine_threadsafe(self._broadcast_depth(tick), self._loop)
            if self.on_depth_callback:
                asyncio.run_coroutine_threadsafe(self.on_depth_callback(tick), self._loop)
            return
        # "tk" = touchline ack (initial price sent on subscribe)
        # "tf" = touchline feed (subsequent price updates)
        if t not in ("tf", "tk"):
            return
        self._last_tick_at = time.monotonic()   # heartbeat for the stale-feed watchdog
        if self._clients:
            asyncio.run_coroutine_threadsafe(self._broadcast(tick), self._loop)
        if self.on_tick_callback:
            asyncio.run_coroutine_threadsafe(self.on_tick_callback(tick), self._loop)

    def _on_order_update(self, order: dict) -> None:
        """Fires on every broker order state change (placed/filled/cancelled/rejected)."""
        if not self._loop:
            return
        if self.on_order_update_callback:
            asyncio.run_coroutine_threadsafe(self.on_order_update_callback(order), self._loop)
        if self._clients:
            asyncio.run_coroutine_threadsafe(self._broadcast_order(order), self._loop)

    async def _broadcast_order(self, order: dict) -> None:
        payload = json.dumps({
            "type":       "order_update",
            "norenordno": order.get("norenordno", ""),
            "status":     order.get("status", ""),
            "remarks":    order.get("remarks", ""),
            "tsym":       order.get("tsym", ""),
            "exch":       order.get("exch", ""),
        })
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] order-update send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    async def broadcast_alert(self, level: str, message: str) -> None:
        """Push a user-facing alert (e.g. an auto-rollover result) to all
        connected frontend clients. level is "success" | "error" | "info"."""
        await self._send_all({"type": "alert", "level": level, "message": message})

    async def broadcast_feed_status(self, state: str, message: str = "") -> None:
        """Push live-feed health to the UI on its own channel (separate from the
        rollover alert channel). state is "session_expired" (human must Connect)
        or "live" (feed healed — clear any warning)."""
        await self._send_all({"type": "feed_status", "state": state, "message": message})

    async def _send_all(self, payload_dict: dict) -> None:
        payload = json.dumps(payload_dict)
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    # ------------------------------------------------------------------
    # Async helpers (run on the event loop)
    # ------------------------------------------------------------------

    async def _broadcast(self, tick: dict) -> None:
        payload = json.dumps({
            "e":   tick.get("e", ""),
            "tk":  tick.get("tk", ""),
            "lp":  tick.get("lp", ""),
            "bp1": tick.get("bp1", ""),
            "sp1": tick.get("sp1", ""),
            "o":   tick.get("o", ""),
            "c":   tick.get("c", ""),
            # last-trade qty + volume: the engines' ghost-tick guard reads ltq
            # (a real trade has qty > 0); dropping them silently disables it.
            "ltq": tick.get("ltq", ""),
            "v":   tick.get("v", ""),
        })
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] client send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    async def _broadcast_depth(self, tick: dict) -> None:
        # SNAPQUOTE frames are deltas — Shoonya sends only changed fields — so
        # forward whatever changed among the fields the OI/flow agents read.
        # (tk, e) identify the leg; the consumer merges deltas into a snapshot.
        payload = {"type": "depth"}
        for k in ("e", "tk", "lp", "oi", "poi", "ft", "v", "ltq",
                  "bp1", "sp1", "bq1", "sq1", "bp2", "sp2", "bp3", "sp3",
                  "bp4", "sp4", "bp5", "sp5", "c", "o", "h", "l", "ap"):
            if k in tick:
                payload[k] = tick[k]
        msg = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception as e:
                print(f"[TickerManager] depth send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    async def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        print(f"[TickerManager] frontend client connected. total={len(self._clients)}")

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        print(f"[TickerManager] frontend client disconnected. total={len(self._clients)}")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, symbols: list[str], feed: str = "touchline") -> None:
        """Subscribe symbols on the touchline feed (default) or the depth feed.

        feed="depth" (aka "snapquote"/"d") asks for the SNAPQUOTE stream, which
        carries open interest and the bid/ask ladder — what the intraday OI/flow
        agents need. Backward compatible: existing callers pass symbols only and
        get touchline exactly as before."""
        depth = str(feed).lower() in ("depth", "snapquote", "d")
        for sym in symbols:
            if depth:
                if sym not in self._depth_subscribed:
                    self._depth_subscribed.add(sym)
                    print(f"[TickerManager] subscribe depth({sym}) — ws_open={self._ws_open}")
                    if self._ws_open:
                        self._broker.ws_subscribe(sym, self._snapquote_feed())
                    # else queued in _depth_subscribed; _on_open subscribes it
            else:
                print(f"[TickerManager] subscribe({sym}) — ws_open={self._ws_open}")
                if sym not in self._subscribed:
                    self._subscribed.add(sym)
                    if self._ws_open:
                        # WS is ready — subscribe directly
                        self._broker.ws_subscribe(sym)
                    # else: symbol queued in _subscribed; _on_open subscribes it

    def unsubscribe(self, symbols: list[str]) -> None:
        for sym in symbols:
            was_touch = sym in self._subscribed
            was_depth = sym in self._depth_subscribed
            self._subscribed.discard(sym)
            self._depth_subscribed.discard(sym)
            if self._ws_open:
                # Must unsubscribe on the SAME feed(s) the symbol was sent on:
                # unsubscribing touchline leaves a live depth stream, and vice versa.
                if was_touch:
                    self._broker.ws_unsubscribe(sym)
                if was_depth:
                    self._broker.ws_unsubscribe(sym, self._snapquote_feed())

    # ------------------------------------------------------------------
    # Self-healing: reconnect the socket without a human, unless the
    # broker SESSION itself has expired (then alert and wait for connect).
    # ------------------------------------------------------------------

    async def _reconnect(self, reason: str) -> None:
        """Reopen the Shoonya WebSocket with the session we already hold.

        Retries a few times with backoff. If every attempt fails the session
        has almost certainly expired — we can't fix that without the human's
        credentials, so we alert and stand down until POST /api/connect.
        """
        if self._reconnecting or self._intentional_stop:
            return
        self._reconnecting = True
        try:
            for attempt, backoff in enumerate(_RECONNECT_BACKOFF, start=1):
                if self._intentional_stop:
                    return
                print(f"[TickerManager] reconnect attempt {attempt}/{len(_RECONNECT_BACKOFF)} ({reason})")
                if await self._reopen_once():
                    print("[TickerManager] reconnect succeeded — feed live again")
                    return
                await asyncio.sleep(backoff)
            await self._declare_session_expired(reason)
        finally:
            self._reconnecting = False

    async def _reopen_once(self) -> bool:
        """Tear down and reopen the socket. True if _on_open fired in time."""
        if self._broker is None or self._loop is None:
            return False
        self._ws_open = False

        def _do_reopen() -> None:
            # Credentials persist on _api from start(); re-set to be explicit.
            self._broker._api.set_credentials(self._auth_token, self._uid, self._uid)
            self._broker.start_websocket(
                subscribe_callback=self._on_tick,
                socket_open_callback=self._on_open,
                socket_close_callback=self._on_close,
                order_update_callback=self._on_order_update,
            )

        try:
            # start_websocket tears down the old thread (blocking join) then
            # spawns a new one — keep it off the event loop.
            await self._loop.run_in_executor(None, _do_reopen)
        except Exception as e:
            print(f"[TickerManager] reopen call failed: {e}")
            return False

        # _on_open (background thread) flips _ws_open once the socket is live.
        deadline = time.monotonic() + _REOPEN_WAIT
        while time.monotonic() < deadline:
            if self._ws_open:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _declare_session_expired(self, reason: str) -> None:
        """Reconnect exhausted: hand the session back to the human."""
        self._session_expired = True
        self._ws_open = False
        # Drop is_running so POST /api/connect (guarded on `not is_running`)
        # re-arms the feed with a freshly-issued token.
        self.is_running = False
        print(f"[TickerManager] session appears expired ({reason}); awaiting human reconnect")
        await self.broadcast_feed_status(
            "session_expired",
            "Live price feed disconnected — the broker session has expired. "
            "Please click Connect to log back in.",
        )

    def _any_subscribed_session_open(self) -> bool:
        """True if any subscribed exchange is inside its trading window — i.e.
        ticks are expected right now. Keeps the watchdog quiet overnight."""
        both = self._subscribed | self._depth_subscribed
        exchanges = {s.split("|", 1)[0] for s in both if "|" in s}
        return any(is_session_open(e) for e in exchanges)

    async def _watchdog_loop(self) -> None:
        """Force a reconnect if the socket looks open but has gone silent during
        an open session — catches half-dead connections that never fire close."""
        print("[TickerManager] stale-feed watchdog started")
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            try:
                if not (self.is_running and self._ws_open):
                    continue
                if self._reconnecting or self._intentional_stop or self._session_expired:
                    continue
                if not self._subscribed or self._last_tick_at == 0.0:
                    continue
                if not self._any_subscribed_session_open():
                    continue
                silent_for = time.monotonic() - self._last_tick_at
                if silent_for >= _STALE_SECONDS:
                    print(f"[TickerManager] no ticks for {silent_for:.0f}s during open session — forcing reconnect")
                    await self._reconnect(f"stale feed {silent_for:.0f}s")
            except Exception as e:
                print(f"[TickerManager] watchdog error: {e}")


# Module-level singleton imported by server.py
ticker_manager = TickerManager()

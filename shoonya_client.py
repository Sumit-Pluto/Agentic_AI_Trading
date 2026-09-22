"""shoonya_client.py — lean, low-latency trading connector for Shoonya's 2026 OAuth API.

IMPORTANT: Shoonya decommissioned the old QuickAuth API (NorenWClientTP — it
now returns 502 for everyone). This client targets the replacement:
    REST       https://api.shoonya.com/NorenWClientAPI/
    WebSocket  wss://api.shoonya.com/NorenWSAPI/
    Reference  github.com/deepak-dhyani8742/Shoonya_oAuthAPI-py
               (confirmed official by Finvasia API support; pip: NorenRestApiOAuth)

Login/token handling lives in shoonya_login/ (see that package's docstring).
build_session() below wires the two together: it loads credentials via
shoonya_login and returns a ShoonyaSession — the class here that adds order
placement, books, and quotes on top of shoonya_login.ShoonyaAuth's login/
token transport. For the daily login flow itself, see
shoonya_login.flow.daily_login / `python -m shoonya_login`.

Transport per the official client:
  * REST: body "jData=<json>", header "Authorization: Bearer <access_token>"
  * WS  : connect {"t":"a",...,"accesstoken":...}; server ack {"t":"ak"};
          heartbeat ping '{"t":"h"}' every 3 s (server drops silent clients)

Latency rules for callers:
  * on_tick / on_order run on the socket thread. NEVER block in them —
    push to a queue.Queue and process on your own thread.
  * Shoonya allows ONE websocket per session: ticks, depth and order
    updates all share this feed.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
import zipfile
from datetime import date

import requests
import websocket  # pip package "websocket-client" (NOT "websocket")

from shoonya_login import MissingCredentials, ShoonyaAuth, ShoonyaError, load_credentials
from shoonya_login.config import ws_proxy_kwargs

log = logging.getLogger("shoonya")

WS_URL = "wss://api.shoonya.com/NorenWSAPI/"

SCRIP_MASTERS = {
    "NSE": "https://api.shoonya.com/NSE_symbols.txt.zip",
    "NFO": "https://api.shoonya.com/NFO_symbols.txt.zip",
    "BSE": "https://api.shoonya.com/BSE_symbols.txt.zip",
    "BFO": "https://api.shoonya.com/BFO_symbols.txt.zip",
    "CDS": "https://api.shoonya.com/CDS_symbols.txt.zip",
    "MCX": "https://api.shoonya.com/MCX_symbols.txt.zip",
}


# ═════════════════════════════════════════════════════════════════════════
# REST: orders + books + quotes, built on shoonya_login.ShoonyaAuth's
# login/token transport (thread-safe, one keep-alive session)
# ═════════════════════════════════════════════════════════════════════════
class ShoonyaSession(ShoonyaAuth):
    """Trading REST calls (orders, books, quotes) on top of ShoonyaAuth's
    login/token lifecycle. See shoonya_login/ for auth; this class only adds
    the endpoints below. Do not define __init__ here — ShoonyaAuth.__init__
    sets up the pooled+proxied requests.Session this all depends on."""

    # ── orders ─────────────────────────────────────────────────────────
    def place_order(self, *, buy_or_sell, exchange, tradingsymbol, quantity,
                    price_type="MKT", price=0.0, trigger_price=None,
                    product="I", retention="DAY", disclose_qty=0, remarks="api"):
        """buy_or_sell: 'B'/'S' · price_type: MKT/LMT/SL-LMT/SL-MKT ·
        product: I (intraday MIS) / C (CNC) / M (NRML).
        Placement acceptance is NOT a fill — track fills via the
        order-update feed and reconcile with order_book()."""
        payload = {
            "uid": self.userid, "actid": self.actid,
            # official client URL-encodes tsym: symbols like "M&M-EQ" contain
            # characters that would corrupt the form body otherwise
            "exch": exchange, "tsym": urllib.parse.quote_plus(tradingsymbol),
            "qty": str(int(quantity)), "dscqty": str(int(disclose_qty)),
            "prd": product, "trantype": buy_or_sell, "prctyp": price_type,
            "prc": str(price), "ret": retention,
            "remarks": remarks, "ordersource": "API",
        }
        if trigger_price is not None:
            payload["trgprc"] = str(trigger_price)
        return self._post("PlaceOrder", payload, timeout=(3.05, 5))

    def modify_order(self, *, order_no, exchange, tradingsymbol, quantity,
                     price_type, price=0.0, trigger_price=None, retention="DAY"):
        payload = {
            "uid": self.userid, "norenordno": str(order_no),
            "exch": exchange, "tsym": urllib.parse.quote_plus(tradingsymbol),
            "qty": str(int(quantity)), "prctyp": price_type,
            "prc": str(price), "ret": retention,
        }
        if trigger_price is not None:
            payload["trgprc"] = str(trigger_price)
        return self._post("ModifyOrder", payload, timeout=(3.05, 5))

    def cancel_order(self, order_no):
        return self._post("CancelOrder",
                          {"uid": self.userid, "norenordno": str(order_no)},
                          timeout=(3.05, 5))

    # ── books / account (read-only → safe to retry) ────────────────────
    def order_book(self):
        return self._post("OrderBook", {"uid": self.userid}, retries=2)

    def trade_book(self):
        return self._post("TradeBook", {"uid": self.userid,
                                        "actid": self.actid}, retries=2)

    def positions(self):
        return self._post("PositionBook", {"uid": self.userid,
                                           "actid": self.actid}, retries=2)

    def limits(self):
        return self._post("Limits", {"uid": self.userid,
                                     "actid": self.actid}, retries=2)

    def get_quote(self, exchange, token):
        return self._post("GetQuotes", {"uid": self.userid, "exch": exchange,
                                        "token": str(token)}, retries=2)

    def get_time_series(self, exchange, token, start_epoch, end_epoch,
                        interval="1"):
        """Historical intraday candles (TPSeries). Requires login.
        interval minutes: 1,3,5,10,15,30,60,120,240. Newest first:
        time, into/inth/intl/intc = OHLC, intv = volume, intoi = OI."""
        return self._post("TPSeries", {
            "ordersource": "API", "uid": self.userid,
            "exch": exchange, "token": str(token),
            "st": str(int(start_epoch)), "et": str(int(end_epoch)),
            "intrv": str(interval),
        }, retries=2)

    def get_daily_history(self, exchange, tradingsymbol, start_epoch, end_epoch):
        """Daily EOD candles (EODChartData). Requires login."""
        rows = self._post("EODChartData", {
            "uid": self.userid,
            "sym": f"{exchange}:{tradingsymbol}",
            "from": str(int(start_epoch)), "to": str(int(end_epoch)),
        }, timeout=(3.05, 30), retries=2)
        # rows may arrive as JSON *strings* — decode to dicts
        return [json.loads(x) if isinstance(x, str) else x for x in rows]


# ═════════════════════════════════════════════════════════════════════════
# WebSocket feed: ticks + depth + order updates, with self-healing
# ═════════════════════════════════════════════════════════════════════════
class ShoonyaFeed:
    """The single Shoonya websocket. Reconnects forever until stop().

    on_tick(msg, snapshot): msg is the raw delta from the wire; snapshot is
        the merged full state for that instrument (lp=LTP, o/h/l/c,
        v=volume, oi, bp1/sp1..., ft=feed time epoch).
    on_order(msg): raw order update (norenordno, status, fillshares,
        avgprc, rejreason...).
    """

    def __init__(self, session, *, on_tick=None, on_order=None,
                 on_connect=None, stale_after=15.0):
        self.session = session
        self.on_tick = on_tick
        self.on_order = on_order
        self.on_connect = on_connect
        self.stale_after = stale_after
        self.quotes = {}            # "NSE|26000" -> merged live snapshot
        self._subs = {}             # "NSE|26000" -> "t" (touchline) | "d" (depth)
        self._want_orders = False
        self._ws = None
        self._send_lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._last_msg = 0.0
        self._backoff = 1

    # ── public API ─────────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, name="shoonya-ws", daemon=True).start()
        threading.Thread(target=self._watchdog, name="shoonya-watchdog",
                         daemon=True).start()

    def stop(self):
        self._stop.set()
        self._connected.clear()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def wait_connected(self, timeout=15):
        return self._connected.wait(timeout)

    def subscribe(self, keys, *, depth=False):
        """keys: 'NSE|26000' or a list of such exchange|token strings."""
        keys = [keys] if isinstance(keys, str) else list(keys)
        kind = "d" if depth else "t"
        for k in keys:
            self._subs[k] = kind
        if self._connected.is_set():
            self._send({"t": kind, "k": "#".join(keys)})

    def unsubscribe(self, keys):
        keys = [keys] if isinstance(keys, str) else list(keys)
        touchline, depth = [], []
        for k in keys:
            kind = self._subs.pop(k, None)
            (depth if kind == "d" else touchline).append(k)
        if self._connected.is_set():
            if touchline:
                self._send({"t": "u", "k": "#".join(touchline)})
            if depth:
                self._send({"t": "ud", "k": "#".join(depth)})

    def subscribe_orders(self):
        """Enable real-time order updates — the source of truth for fills."""
        self._want_orders = True
        if self._connected.is_set():
            self._send({"t": "o", "actid": self.session.actid})

    # ── internals ──────────────────────────────────────────────────────
    def _send(self, obj):
        try:
            with self._send_lock:
                self._ws.send(json.dumps(obj))
        except Exception as e:
            log.warning("ws send failed (%s) — state is replayed on reconnect", e)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_pong=self._on_pong,
                    on_error=lambda ws, err: log.warning("ws error: %s", err),
                    on_close=lambda ws, code, msg: log.info("ws closed (%s %s)",
                                                            code, msg),
                )
                # TCP_NODELAY: small frames leave immediately (no Nagle).
                # ping_interval/ping_payload: the official client heartbeats
                # {"t":"h"} every 3s — the server drops silent connections.
                kwargs = dict(
                    sockopt=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),),
                    ping_interval=3,
                    ping_payload='{"t":"h"}')
                kwargs.update(ws_proxy_kwargs())   # same SOCKS tunnel as the REST side
                self._ws.run_forever(**kwargs)
            except Exception as e:
                log.warning("ws loop crashed: %s", e)
            self._connected.clear()
            if self._stop.is_set():
                break
            log.info("reconnecting in %ss", self._backoff)
            if self._stop.wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2, 30)

    def _on_open(self, ws):
        # 2026 OAuth API: connect type is "a" with the Bearer access token
        ws.send(json.dumps({
            "t": "a",
            "uid": self.session.userid,
            "actid": self.session.actid,
            "source": "API",
            "accesstoken": self.session.access_token,
        }))

    def _on_message(self, ws, raw):
        self._last_msg = time.monotonic()
        try:
            data = json.loads(raw)
        except ValueError:
            return
        for msg in (data if isinstance(data, list) else [data]):
            self._dispatch(msg)

    def _dispatch(self, msg):
        t = msg.get("t")
        if t in ("tk", "tf", "dk", "df"):
            key = f"{msg.get('e')}|{msg.get('tk')}"
            snap = self.quotes.setdefault(key, {})
            snap.update(msg)        # tf/df carry only changed fields — merge
            if self.on_tick:
                self.on_tick(msg, snap)
        elif t == "om":
            if self.on_order:
                self.on_order(msg)
        elif t in ("ak", "ck"):     # new API acks "ak" (old was "ck")
            if msg.get("s") == "OK":
                log.info("ws authenticated")
                self._backoff = 1
                self._connected.set()
                self._replay_state()
                if self.on_connect:
                    self.on_connect()
            else:
                log.error("ws auth rejected (%s) — access token expired or "
                          "invalid. Re-run the daily login (`python -m "
                          "shoonya_login`) and restart; feed keeps retrying.",
                          msg)
                self._backoff = 30      # don't hammer with a dead token
                try:
                    self._ws.close()
                except Exception:
                    pass
        elif t == "ok":
            log.info("order updates enabled")

    def _replay_state(self):
        """After (re)connect: restore every subscription + order stream."""
        touchline = [k for k, v in self._subs.items() if v == "t"]
        depth = [k for k, v in self._subs.items() if v == "d"]
        if touchline:
            self._send({"t": "t", "k": "#".join(touchline)})
        if depth:
            self._send({"t": "d", "k": "#".join(depth)})
        if self._want_orders:
            self._send({"t": "o", "actid": self.session.actid})

    def _on_pong(self, ws, message):
        # heartbeat pongs count as life signs, so the watchdog measures true
        # connection liveness — no false reconnects outside market hours
        self._last_msg = time.monotonic()

    def _watchdog(self):
        """Force a reconnect when the link is truly dead: with a heartbeat
        ping every 3s, a healthy connection always shows pongs or data
        within stale_after seconds — silence means the TCP session died."""
        while not self._stop.wait(5):
            if (self._connected.is_set() and self._last_msg
                    and time.monotonic() - self._last_msg > self.stale_after):
                log.warning("no data/pong for %.0fs — forcing reconnect",
                            time.monotonic() - self._last_msg)
                try:
                    self._ws.close()
                except Exception:
                    pass


# ═════════════════════════════════════════════════════════════════════════
# Scrip master: tradingsymbol → token mapping (refresh daily)
# ═════════════════════════════════════════════════════════════════════════
def load_scripmaster(exchange, cache_dir="data"):
    """Download (once per day) and parse the exchange scrip master.

    Returns {tradingsymbol: row} where row includes Token, LotSize, TickSize,
    and for NFO also Expiry / OptionType / StrikePrice. Subscribe with
    f"{exchange}|{row['Token']}". Tokens can change after corporate actions,
    hence the daily refresh.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{exchange}_symbols_{date.today():%Y%m%d}.txt")
    if not os.path.exists(path):
        url = SCRIP_MASTERS[exchange]
        log.info("downloading scrip master %s", url)
        raw = requests.get(url, timeout=60).content
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            with zf.open(zf.namelist()[0]) as f, open(path, "wb") as out:
                out.write(f.read())
    table = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            tsym = (row.get("TradingSymbol") or "").strip()
            if tsym:
                table[tsym] = row
    return table


# ═════════════════════════════════════════════════════════════════════════
# Session construction — the one place that turns .env credentials into a
# ready-to-use trading ShoonyaSession. shoonya_login owns *credential
# loading*; this owns *client construction*, since a ShoonyaSession (with
# order/quote methods) is trading-client code, not auth code.
# ═════════════════════════════════════════════════════════════════════════
def build_session() -> ShoonyaSession:
    try:
        creds = load_credentials()
    except MissingCredentials as e:
        sys.exit(str(e))
    return ShoonyaSession(credentials=creds)

"""REST client from the intraday engine to the broker Gateway.

Auth + retry machinery ported from OptionSmith's proven client (exchange client
creds for a scoped service JWT, attach to every call, re-login once on the
token-expiry 401, distinguish that from a broker-offline 401). Adds the read
methods the engine needs plus ORDER placement — OptionSmith's client is
read-only by design, the engine's is not, so this one needs the `orders` scope
in addition to `market`/`positions`/`funds`.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from .config import GatewaySettings

logger = logging.getLogger(__name__)

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_TOKEN_401_HINTS = ("token expired", "invalid token", "authorization header",
                    "no longer exists")


class GatewayError(RuntimeError):
    """Any failure talking to the Gateway, with the Gateway's own reason kept."""


class BrokerOffline(GatewayError):
    """The Gateway is up but has no live broker session — a human must connect."""


def option_tradingsymbol(symbol: str, expiry, is_call: bool, strike: float) -> str:
    """The broker's own name for one option contract, e.g. NIFTY29SEP26C24800.

    Every order-side endpoint is keyed by this string (the Gateway looks it up
    as exch|tsym in the scripmaster). Reproduces the scripmaster's own tsym
    character-for-character; the strike carries only the decimals it needs."""
    try:
        d = expiry if isinstance(expiry, dt.date) else dt.date.fromisoformat(str(expiry)[:10])
        kf = float(strike)
    except (TypeError, ValueError) as e:
        raise GatewayError(f"cannot name contract {symbol} {strike} {expiry}: {e}") from e
    if not (math.isfinite(kf) and kf > 0):
        raise GatewayError(f"cannot name contract {symbol} {strike!r}: strike must be positive finite")
    k = f"{kf:.4f}".rstrip("0").rstrip(".")
    if not str(symbol).strip():
        raise GatewayError("cannot name a contract with no underlying symbol")
    return (f"{str(symbol).strip().upper()}{d.day:02d}{_MONTHS[d.month - 1]}"
            f"{d.year % 100:02d}{'C' if is_call else 'P'}{k}")


class GatewayClient:
    """Synchronous client. FastAPI + the engine loop both call it from threads."""

    def __init__(self, settings: GatewaySettings | None = None):
        if settings is None:
            try:
                settings = GatewaySettings.load()
            except ValueError as e:
                raise GatewayError(f"gateway settings are unreadable: {e}") from e
        self.settings = settings
        if not self.settings.configured:
            raise GatewayError(
                "gateway credentials missing — set GATEWAY_CLIENT_ID and "
                "GATEWAY_CLIENT_SECRET (issue with the Gateway's "
                "`python -m scripts.register_service --name intraday-engine "
                "--scopes market,orders,positions,funds,status`)")
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise GatewayError("the gateway client needs `requests`") from e
        self._requests = requests
        self._session = requests.Session()
        self._token: str | None = None

    # ---------------- auth ----------------
    def _login(self) -> None:
        try:
            r = self._session.post(
                f"{self.settings.base_url}/api/auth/service-token",
                json={"client_id": self.settings.client_id,
                      "client_secret": self.settings.client_secret},
                timeout=self.settings.timeout, verify=self.settings.verify_tls)
        except self._requests.RequestException as e:
            raise GatewayError(f"cannot reach gateway at {self.settings.base_url}: {e}") from e
        if r.status_code == 401:
            raise GatewayError("gateway rejected the service credentials "
                               "(check GATEWAY_CLIENT_ID / GATEWAY_CLIENT_SECRET)")
        if r.status_code >= 400:
            raise GatewayError(f"gateway login failed [{r.status_code}]: {self._detail(r)}")
        try:
            body = r.json()
        except ValueError as e:
            raise GatewayError(f"gateway login answered non-JSON — is "
                               f"{self.settings.base_url} the Gateway? {(r.text or '')[:200]!r}") from e
        token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
        if not token:
            raise GatewayError(f"gateway login answered without a token: {str(body)[:200]}")
        self._token = token

    def _detail(self, r) -> str:
        try:
            return str((r.json() or {}).get("detail", ""))
        except Exception:
            return (r.text or "")[:300]

    def _request(self, method: str, path: str, **kw) -> Any:
        if self._token is None:
            self._login()
        url = f"{self.settings.base_url}{path}"
        kw.setdefault("timeout", self.settings.timeout)
        kw.setdefault("verify", self.settings.verify_tls)

        def send():
            return self._session.request(
                method, url, headers={"Authorization": f"Bearer {self._token}"}, **kw)

        try:
            r = send()
            if r.status_code == 401:
                detail = self._detail(r).lower()
                if any(h in detail for h in _TOKEN_401_HINTS):
                    self._login()
                    r = send()
                else:
                    raise BrokerOffline(f"gateway has no live broker session: {self._detail(r)}")
        except self._requests.RequestException as e:
            raise GatewayError(f"cannot reach gateway at {self.settings.base_url}: {e}") from e
        if r.status_code >= 400:
            raise GatewayError(f"gateway {method} {path} failed [{r.status_code}]: {self._detail(r)}")
        try:
            return r.json()
        except ValueError as e:
            raise GatewayError(f"gateway {method} {path} answered non-JSON "
                               f"[{r.status_code}]: {(r.text or '')[:200]!r}") from e

    # ---------------- market data (read: `market` scope) ----------------
    def status(self) -> dict:
        return self._request("GET", "/api/status")

    def quote(self, exchange: str, token: str) -> dict:
        return self._request("GET", "/api/quote", params={"exchange": exchange, "token": token})

    def option_chain(self, symbol: str, exchange: str = "NSE", *, expiry: str = "",
                     atm: float = 0.0, count: int = 15, use_cache: bool = True) -> dict:
        return self._request("POST", "/api/option-chain", json={
            "symbol": symbol, "exchange": exchange, "expiry": expiry,
            "atm": atm, "count": count, "use_cache": use_cache})

    def candles(self, exchange: str, token: str, *, interval: int = 5,
                lookback_minutes: int = 0, daily: bool = False,
                tradingsymbol: str = "", days: int = 30) -> dict:
        return self._request("GET", "/api/candles", params={
            "exchange": exchange, "token": token, "interval": interval,
            "lookback_minutes": lookback_minutes, "daily": daily,
            "tradingsymbol": tradingsymbol, "days": days})

    def candles_batch(self, items: list[dict], concurrency: int = 8) -> list[dict]:
        """Warm many instruments' candles in one call (the /api/candles/batch
        endpoint added for the intraday engine). Each item: {exchange, token,
        interval?, lookback_minutes?, daily?, tradingsymbol?, days?}."""
        r = self._request("POST", "/api/candles/batch",
                          json={"items": items, "concurrency": concurrency})
        return r.get("results", []) if isinstance(r, dict) else r

    def spots(self, symbols: list[str], exchange: str = "NSE") -> dict:
        return self._request("GET", "/api/_probe/spots",
                             params={"symbols": ",".join(symbols), "exch": exchange},
                             timeout=60).get("spots", {})

    def oi_sweep(self, symbols: list[str], *, strikes: int = 10, expiry_index: int = 0,
                 settle: float = 0.6, spots: dict | None = None, legs: bool = False,
                 timeout: float | None = None) -> dict:
        return self._request("GET", "/api/_probe/oisweep", params={
            "symbols": ",".join(symbols), "strikes": strikes,
            "expiry_index": expiry_index, "settle": settle, "legs": 1 if legs else 0,
            "spots": ",".join(f"{k}:{v}" for k, v in (spots or {}).items() if k in symbols)},
            timeout=timeout or max(self.settings.timeout, 180))

    # ---------------- account (read: `funds`/`positions` scope) ----------------
    def funds(self) -> dict:
        return self._request("GET", "/api/funds")

    def positions(self) -> list[dict]:
        r = self._request("GET", "/api/positions")
        return r.get("positions", r) if isinstance(r, dict) else r

    def order_margin(self, exchange: str, tradingsymbol: str, quantity: int,
                     buy_or_sell: str = "B", product_type: str = "M") -> dict:
        return self._request("POST", "/api/order_margin", json={
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "quantity": quantity, "buy_or_sell": buy_or_sell, "product_type": product_type})

    # ---------------- orders (`orders` scope) ----------------
    def place_order(self, *, exchange: str, tradingsymbol: str, quantity: int,
                    buy_or_sell: str, price_type: str = "MKT", price: float = 0.0,
                    product_type: str = "I", trigger_price: float = 0.0,
                    retention: str = "DAY", remarks: str = "") -> dict:
        """Place an order. product_type "I"=MIS (intraday, default), "M"=NRML,
        "C"=CNC. buy_or_sell "B"/"S". price_type "MKT"/"LMT". The Gateway
        converts a plain MKT to a marketable limit (Shoonya RMS blocks API MKT)."""
        return self._request("POST", "/api/orders", json={
            "buy_or_sell": buy_or_sell, "product_type": product_type,
            "exchange": exchange, "tradingsymbol": tradingsymbol, "quantity": quantity,
            "price_type": price_type, "price": price, "trigger_price": trigger_price,
            "retention": retention, "remarks": remarks})

    def cancel_order(self, broker_order_id: str) -> dict:
        return self._request("DELETE", f"/api/orders/{broker_order_id}")

    def modify_order(self, broker_order_id: str, changes: dict) -> dict:
        return self._request("PUT", f"/api/orders/{broker_order_id}", json=changes)

    def order_book(self) -> list[dict]:
        r = self._request("GET", "/api/orders")
        return r.get("orders", r) if isinstance(r, dict) else r

    # ---------------- lifecycle ----------------
    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

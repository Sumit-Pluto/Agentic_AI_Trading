"""Shoonya (Finvasia) broker adapter."""

import tls_compat  # noqa: F401  # force TLS 1.2 (Shoonya rejects TLS 1.3)
import asyncio
import hashlib
import json
import os
import time
import urllib.parse
import requests as _requests
from datetime import datetime
from typing import Any

from NorenRestApiPy.NorenApi import NorenApi
from brokers.base import BrokerBase, SessionToken, UserProfile, BrokerError
from brokers.shoonya import _token_cache
from brokers.shoonya import netproxy

import logging
_log = logging.getLogger(__name__)
from brokers.shoonya.auth_code import get_auth_code_with_credentials
from brokers.shoonya.logout import logout as shoonya_logout
from brokers.shoonya.user_details import get_user_details
from brokers.shoonya.scripmaster import get_scripmaster


BROKER_NAME = "shoonya"

# Quote-integrity retry (see _get_quote_sync). Three attempts clears it in
# practice; the backoff is short because a chain fetch multiplies any delay by
# the number of legs still queued behind it.
_QUOTE_VERIFY_ATTEMPTS = 3
_QUOTE_VERIFY_BACKOFF = 0.15   # seconds, multiplied by the attempt number


class ShoonyaBroker(BrokerBase):
    """
    Shoonya broker adapter (stateless).

    All I/O runs in thread pool via run_in_executor.
    Uses NorenRestApiOAuth library for API calls.
    Token caching uses _token_cache (token_store.json).
    """

    def __init__(self):
        """Initialize Shoonya broker (stateless, but keeps API instance)."""
        api_host = os.getenv("SHOONYA_API_HOST")
        ws_url = os.getenv("SHOONYA_WS_URL")
        if not api_host or not ws_url:
            raise RuntimeError(
                "SHOONYA_API_HOST and SHOONYA_WS_URL must be set in your .env file."
            )
        self._api = NorenApi(host=f"{api_host.rstrip('/')}/", websocket=ws_url)

    async def login(self, credentials: dict) -> SessionToken:
        """Full Shoonya login flow via OAuth."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._login_sync, credentials)

    async def logout(self, token: SessionToken, credentials: dict) -> bool:
        """Invalidate session on Shoonya server."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._logout_sync, token, credentials)

    async def refreshSession(self, token: SessionToken, credentials: dict) -> SessionToken:
        """Refresh the session token (re-authenticate)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._refresh_session_sync, token, credentials)

    async def validate_token(self, token: SessionToken, credentials: dict) -> bool:
        """Check if token is valid."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._validate_token_sync, token, credentials)

    async def get_user_profile(self, token: SessionToken, credentials: dict) -> UserProfile:
        """Get user profile from broker."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_user_profile_sync, token, credentials)

    async def placeOrder(self, token: SessionToken, credentials: dict, order: dict) -> dict:
        """Place an order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._place_order_sync, token, credentials, order)

    async def modifyOrder(self, token: SessionToken, credentials: dict, order_id: str, params: dict) -> dict:
        """Modify an order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._modify_order_sync, token, credentials, order_id, params)

    async def cancelOrder(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Cancel an order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._cancel_order_sync, token, credentials, order_id)

    async def getOrderStatus(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Get order status."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_order_status_sync, token, credentials, order_id)

    async def getOrderBook(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get order book."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_order_book_sync, token, credentials)

    async def getPositions(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get positions."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_positions_sync, token, credentials)

    async def getHoldings(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get holdings."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_holdings_sync, token, credentials)

    async def getMargins(self, token: SessionToken, credentials: dict) -> dict:
        """Get margin details."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_margins_sync, token, credentials)

    async def getOrderMargin(self, token: SessionToken, credentials: dict, positions: list) -> dict:
        """SPAN+exposure margin for prospective positions via Shoonya span_calculator."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_order_margin_sync, token, credentials, positions)

    async def getQuote(self, token: SessionToken, credentials: dict, symbol: str, exchange: str) -> dict:
        """Get market quote."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_quote_sync, token, credentials, symbol, exchange)

    async def getInstruments(self, token: SessionToken, credentials: dict, exchange: str) -> list[dict]:
        """Get available instruments."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_instruments_sync, token, credentials, exchange)

    async def getCandles(
        self,
        token: SessionToken,
        credentials: dict,
        exchange: str,
        symbol_token: str,
        interval: int | None = None,
        starttime: float | None = None,
        endtime: float | None = None,
        tradingsymbol: str | None = None,
        daily: bool = False,
    ) -> list[dict]:
        """Fetch historical candles.

        If daily=True, uses get_daily_price_series (requires tradingsymbol).
        Else uses get_time_price_series with interval in minutes (1,3,5,10,15,30,60,120,240).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._get_candles_sync,
            token,
            credentials,
            exchange,
            symbol_token,
            interval,
            starttime,
            endtime,
            tradingsymbol,
            daily,
        )

    async def subscribeMarketData(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """
        Subscribe to market data (WebSocket).

        Args:
            token: SessionToken
            credentials: Credentials dict
            symbols: List of symbols in format:
                - "NSE|22" (token format) - direct subscription
                - "NSE|RELIANCE-EQ" (symbol format) - auto-lookup via searchscrip
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._subscribe_market_data_sync, token, credentials, symbols)

    async def unsubscribeMarketData(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Unsubscribe from market data (WebSocket)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._unsubscribe_market_data_sync, token, credentials, symbols)

    async def subscribeOrderUpdates(self, token: SessionToken, credentials: dict) -> None:
        """Subscribe to order updates (WebSocket)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._subscribe_order_updates_sync, token, credentials)

    # -----------------------------------------------------------------------
    # Private sync implementations (called via run_in_executor)
    # -----------------------------------------------------------------------

    def _login_sync(self, credentials: dict) -> SessionToken:
        """Perform full Shoonya login (OAuth flow)."""
        user_id = credentials.get("user_id")
        api_host = credentials.get("api_host") or os.getenv("SHOONYA_API_HOST")

        # Check cached token first
        cached = _token_cache.get_cached_session(user_id)
        if cached:
            return SessionToken(
                token=cached["susertoken"],
                broker_uid=user_id,
                issued_at=cached["susertoken_issued_at"],
                broker_name=BROKER_NAME,
            )

        # Try up to twice: if GenAcsTok rejects a stale/consumed OAuth code
        # (AUTHCODE_EXPIRED etc.), discard it and retry once with a brand-new
        # code from a fresh headless login.
        last_emsg = "Unknown error"
        last_result: dict | None = None
        for attempt in (1, 2):
            # Get OAuth code (from cache or Selenium)
            oauth_code = _token_cache.get_cached_oauth_code(user_id)
            if not oauth_code:
                try:
                    oauth_code = get_auth_code_with_credentials(credentials)
                    _token_cache.cache_oauth_code(user_id, oauth_code)
                except Exception as e:
                    raise BrokerError(f"Failed to get OAuth code: {e}", broker=BROKER_NAME) from e

            # Exchange oauth_code for session token via /GenAcsTok
            try:
                vendor_code = credentials["vendor_code"]
                api_secret = credentials["api_secret"]
                checksum = self._build_checksum(vendor_code, api_secret, oauth_code)

                payload = {
                    "vc": vendor_code,
                    "appver": "1.0",
                    "uid": user_id,
                    "code": oauth_code,
                    "checksum": checksum,
                }
                json_str = json.dumps(payload, separators=(",", ":"))
                body = f"jData={json_str}"

                import requests
                # GenAcsTok is IP-bound: Shoonya only accepts it from the
                # registered static IP. Direct first; the proxy carries the
                # exchange only after an INVALID_IP rejection (the OAuth code
                # is not consumed by the IP check, so it can be reused).
                resp, _egress_path = netproxy.post(
                    requests, f"{api_host}/GenAcsTok", "auth",
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30,
                )
                resp.raise_for_status()
                result = resp.json()
                if _egress_path == "proxy":
                    _log.warning("shoonya: account %s authenticated via proxy — this "
                                 "host's IP was refused by the broker", user_id)
            except Exception as e:
                raise BrokerError(
                    f"OAuth token exchange error: {e}",
                    broker=BROKER_NAME,
                ) from e

            if result.get("stat") == "Ok":
                susertoken = result.get("susertoken")
                issued_at = datetime.now().isoformat()
                _token_cache.cache_session(user_id, susertoken, issued_at)

                return SessionToken(
                    token=susertoken,
                    broker_uid=user_id,
                    issued_at=issued_at,
                    broker_name=BROKER_NAME,
                )

            # failed — throw away the (likely stale/consumed) OAuth code
            last_result = result
            last_emsg = result.get("emsg", "Unknown error")
            _token_cache.invalidate_oauth_code(user_id)
            # retry ONCE with a fresh code if the code was expired/invalid
            if attempt == 1 and any(k in last_emsg.upper() for k in ("EXPIRED", "AUTHCODE")):
                continue
            break

        raise BrokerError(
            f"OAuth token exchange failed: {last_emsg}",
            broker=BROKER_NAME,
            raw=last_result,
        )

    def _logout_sync(self, token: SessionToken, credentials: dict) -> bool:
        """Perform logout."""
        try:
            result = shoonya_logout(
                user_id=token.broker_uid,
                auth_token=token.token,
                endpoint=None,
            )
            if result.get("success"):
                _token_cache.invalidate_session(token.broker_uid)
                return True
            return False
        except Exception as e:
            raise BrokerError(f"Logout failed: {e}", broker=BROKER_NAME) from e

    def _refresh_session_sync(self, token: SessionToken, credentials: dict) -> SessionToken:
        """Refresh session (re-authenticate)."""
        _token_cache.invalidate_session(credentials["user_id"])
        return self._login_sync(credentials)

    def _validate_token_sync(self, token: SessionToken, credentials: dict) -> bool:
        """Validate token by calling UserDetails API."""
        try:
            self._inject_oauth(token, credentials)
            result = get_user_details(
                user_id=credentials["user_id"],
                auth_token=token.token,
                api_host=credentials.get("api_host"),
            )
            return result.get("stat") == "Ok"
        except Exception:
            return False

    def _get_user_profile_sync(self, token: SessionToken, credentials: dict) -> UserProfile:
        """Fetch user profile from Shoonya API."""
        try:
            self._inject_oauth(token, credentials)
            result = get_user_details(
                user_id=credentials["user_id"],
                auth_token=token.token,
                api_host=credentials.get("api_host"),
            )

            if result.get("stat") != "Ok":
                raise BrokerError(
                    f"Failed to fetch user details: {result.get('emsg')}",
                    broker=BROKER_NAME,
                    raw=result,
                )

            return UserProfile(
                broker_uid=result.get("uid", ""),
                account_id=result.get("actid", ""),
                email=result.get("email", ""),
                mobile=result.get("m_num", ""),
                broker_name=BROKER_NAME,
                full_name=result.get("brkname", ""),
                branch_id=result.get("brnchid", ""),
                enabled_exchanges=result.get("exarr", []),
                enabled_products=result.get("prarr", []),
                raw=result,
            )
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"User profile fetch error: {e}", broker=BROKER_NAME) from e

    def _place_order_sync(self, token: SessionToken, credentials: dict, order: dict) -> dict:
        """Place order — calls Shoonya directly to preserve emsg on rejection."""
        try:
            self._inject_oauth(token, credentials)
            # Access name-mangled private attrs to build the request ourselves.
            # NorenApi.place_order() discards emsg on rejection (returns None),
            # so we bypass it to get the real error reason.
            api = self._api
            headers = api._NorenApi__OAuthHeaders
            uid = api._NorenApi__username
            actid = api._NorenApi__accountid
            host = NorenApi._NorenApi__service_config["host"]
            url = f"{host}/PlaceOrder"

            payload_dict = {
                "ordersource": "API",
                "uid": uid,
                "actid": actid,
                "trantype": order.get("buy_or_sell", "B"),
                "prd": order.get("product_type", "C"),
                "exch": order.get("exchange", "NSE"),
                "tsym": urllib.parse.quote_plus(order.get("tradingsymbol", "")),
                "qty": str(order.get("quantity", 1)),
                "dscqty": str(order.get("discloseqty", 0)),
                "prctyp": order.get("price_type", "LMT"),
                "prc": str(order.get("price", 0)),
                "trgprc": str(order.get("trigger_price", 0)),
                "ret": order.get("retention", "DAY"),
                "remarks": order.get("remarks", ""),
            }
            payload = "jData=" + json.dumps(payload_dict)
            # SEBI's retail-algo circular binds order placement to the
            # registered static IP, so orders share auth's proxy fallback;
            # quotes and the websocket stay direct.
            res, _egress_path = netproxy.post(_requests, url, "order",
                                              data=payload, headers=headers,
                                              timeout=30)
            result = res.json()
            if _egress_path == "proxy":
                _log.warning("shoonya: order for %s went via proxy — this host's "
                             "IP was refused by the broker",
                             order.get("tradingsymbol", "?"))

            if result.get("stat") != "Ok":
                emsg = result.get("emsg", "Order rejected (no reason given)")
                raise BrokerError(f"Order rejected: {emsg}", broker=BROKER_NAME, raw=result)
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Place order error: {e}", broker=BROKER_NAME) from e

    def _modify_order_sync(self, token: SessionToken, credentials: dict, order_id: str, params: dict) -> dict:
        """Modify order — raw call instead of NorenApi.modify_order().

        Modify/cancel are IP-bound like placement (SEBI ties order flow to the
        registered static IP); the library posts directly and bypasses the
        netproxy fallback, which is how cancels died with INVALID_IP.
        """
        try:
            self._inject_oauth(token, credentials)
            api = self._api
            headers = api._NorenApi__OAuthHeaders
            host = NorenApi._NorenApi__service_config["host"]
            values = {
                "ordersource": "API",
                "uid": api._NorenApi__username,
                "actid": api._NorenApi__accountid,
                "norenordno": str(order_id),
                "exch": params.get("exchange", "NSE"),
                "tsym": urllib.parse.quote_plus(params.get("tradingsymbol") or ""),
                "qty": str(params.get("newquantity")),
                "prctyp": params.get("newprice_type", "LMT"),
                "prc": str(params.get("newprice", 0)),
            }
            if params.get("newtrigger_price") is not None:
                values["trgprc"] = str(params.get("newtrigger_price"))
            payload = "jData=" + json.dumps(values)
            res, _egress_path = netproxy.post(_requests, f"{host}/ModifyOrder", "order",
                                              data=payload, headers=headers, timeout=30)
            result = res.json()
            if _egress_path == "proxy":
                _log.warning("shoonya: modify of %s went via proxy — this host's IP "
                             "was refused by the broker", order_id)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(f"Modify order failed: {result.get('emsg')}", broker=BROKER_NAME, raw=result)
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Modify order error: {e}", broker=BROKER_NAME) from e

    def _cancel_order_sync(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Cancel order — raw call instead of NorenApi.cancel_order().

        Same reason as modify: the library bypasses the egress proxy, so a
        cancel from a non-whitelisted IP is rejected by the broker while the
        pending order stays alive.
        """
        try:
            self._inject_oauth(token, credentials)
            api = self._api
            headers = api._NorenApi__OAuthHeaders
            host = NorenApi._NorenApi__service_config["host"]
            payload = "jData=" + json.dumps({
                "ordersource": "API",
                "uid": api._NorenApi__username,
                "norenordno": str(order_id),
            })
            res, _egress_path = netproxy.post(_requests, f"{host}/CancelOrder", "order",
                                              data=payload, headers=headers, timeout=30)
            result = res.json()
            if _egress_path == "proxy":
                _log.warning("shoonya: cancel of %s went via proxy — this host's IP "
                             "was refused by the broker", order_id)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(f"Cancel order failed: {result.get('emsg')}", broker=BROKER_NAME, raw=result)
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Cancel order error: {e}", broker=BROKER_NAME) from e

    def _get_order_status_sync(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Get single order history."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.single_order_history(orderno=order_id)
            if not isinstance(result, list):
                raise BrokerError(f"Unexpected order status response", broker=BROKER_NAME, raw=result)
            return result[0] if result else {}
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get order status error: {e}", broker=BROKER_NAME) from e

    def _get_order_book_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get order book."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.get_order_book()
            if isinstance(result, list):
                return result
            # Shoonya returns None or {"stat":"Not_Ok","emsg":"No Data"} when empty
            if result is None or (isinstance(result, dict) and result.get("emsg") == "No Data"):
                return []
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(f"Order book error: {result.get('emsg', 'Unknown')}", broker=BROKER_NAME, raw=result)
            return []
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get order book error: {e}", broker=BROKER_NAME) from e

    @staticmethod
    def _raise_if_rejected(result, what: str) -> None:
        """Turn a rejected request into an error instead of an empty answer.

        Shoonya reports an expired session with HTTP 200 and `stat: Not_Ok`, so
        a caller that only checks the TYPE of the response reads "your session is
        dead" as "you have no positions" or "you have no money" — and callers
        did, presenting ₹0 and an empty book as fact. A flat account and an
        unreadable one must not look alike.

        "No Data" is the broker's genuine way of saying nothing is there, so it
        stays empty; anything else is a failure and is raised.
        """
        if not isinstance(result, dict):
            return
        stat = result.get("stat")
        if not stat or stat == "Ok":
            return
        emsg = str(result.get("emsg") or "").strip()
        if emsg.lower().replace("_", " ").strip() in ("no data", "nodata", ""):
            return
        raise BrokerError(f"{what}: {emsg or 'request rejected by broker'}",
                          broker=BROKER_NAME, raw=result)

    def _get_positions_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get positions."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.get_positions()
            self._raise_if_rejected(result, "Positions unavailable")
            return result if isinstance(result, list) else []
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get positions error: {e}", broker=BROKER_NAME) from e

    def _get_holdings_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get holdings."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.get_holdings()
            self._raise_if_rejected(result, "Holdings unavailable")
            return result if isinstance(result, list) else []
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get holdings error: {e}", broker=BROKER_NAME) from e

    def _get_margins_sync(self, token: SessionToken, credentials: dict) -> dict:
        """Get margins/limits."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.get_limits()
            self._raise_if_rejected(result, "Funds unavailable")
            return result if isinstance(result, dict) else {}
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get margins error: {e}", broker=BROKER_NAME) from e

    def _get_order_margin_sync(self, token: SessionToken, credentials: dict, positions: list) -> dict:
        """SPAN+exposure for prospective positions via NorenApi.span_calculator."""
        try:
            self._inject_oauth(token, credentials)
            actid = self._api._NorenApi__accountid
            result = self._api.span_calculator(actid, positions)
            self._raise_if_rejected(result, "Margin calculation unavailable")
            return result if isinstance(result, dict) else {}
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Order margin (span) error: {e}", broker=BROKER_NAME) from e

    def _get_quote_sync(self, token: SessionToken, credentials: dict, symbol: str, exchange: str) -> dict:
        """Get quote by symbol token, verifying the response is for that token.

        Shoonya intermittently answers a valid option-token request with a
        DIFFERENT instrument's quote — in practice the underlying's (asking for
        RELIANCE25AUG26C740 returns RELIANCE-EQ at 1320). Measured on a 206-leg
        chain it hit ~17% of legs, and it happens at concurrency 1 too, so it is
        the broker misrouting rather than a race in this process.

        Nothing downstream can detect it: the price is well-formed, just for the
        wrong contract — a deep-ITM call priced at spot makes a spread look free.
        The response carries `token`, so verify it and retry; a quote that never
        matches is raised as an error rather than returned as a plausible lie.
        """
        try:
            self._inject_oauth(token, credentials)
            # symbol is expected to be token (e.g., '22' for RELIANCE)
            want = str(symbol)
            last_seen = ""
            for attempt in range(_QUOTE_VERIFY_ATTEMPTS):
                result = self._api.get_quotes(exchange=exchange, token=symbol)
                self._raise_if_rejected(result, "Quote unavailable")
                if not isinstance(result, dict):
                    return {}
                got = str(result.get("token", ""))
                if got == want or not got:
                    # A response with no token at all predates this check and is
                    # taken at face value; only a token that names a DIFFERENT
                    # instrument is treated as misrouted.
                    return result
                last_seen = got
                _log.warning("shoonya quote misrouted: asked %s|%s, got token %s (%s) "
                             "— attempt %d/%d", exchange, want, got,
                             result.get("tsym", ""), attempt + 1, _QUOTE_VERIFY_ATTEMPTS)
                time.sleep(_QUOTE_VERIFY_BACKOFF * (attempt + 1))
            raise BrokerError(
                f"Quote for {exchange}|{want} kept returning token {last_seen} "
                f"after {_QUOTE_VERIFY_ATTEMPTS} attempts", broker=BROKER_NAME)
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get quote error: {e}", broker=BROKER_NAME) from e

    def _get_instruments_sync(self, token: SessionToken, credentials: dict, exchange: str) -> list[dict]:
        """Get instruments (search scrip) for exchange."""
        try:
            self._inject_oauth(token, credentials)
            return []
        except Exception as e:
            raise BrokerError(f"Get instruments error: {e}", broker=BROKER_NAME) from e

    def _get_candles_sync(
        self,
        token: SessionToken,
        credentials: dict,
        exchange: str,
        symbol_token: str,
        interval: int | None,
        starttime: float | None,
        endtime: float | None,
        tradingsymbol: str | None,
        daily: bool,
    ) -> list[dict]:
        try:
            self._inject_oauth(token, credentials)
            if daily:
                if not tradingsymbol:
                    raise BrokerError("tradingsymbol required for daily candles", broker=BROKER_NAME)
                result = self._api.get_daily_price_series(
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    startdate=starttime,
                    enddate=endtime,
                )
            else:
                result = self._api.get_time_price_series(
                    exchange=exchange,
                    token=symbol_token,
                    starttime=starttime,
                    endtime=endtime,
                    interval=interval,
                )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return [result]
            return []
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get candles error: {e}", broker=BROKER_NAME) from e

    def _search_symbols_sync(self, token: SessionToken, credentials: dict, exchange: str, query: str) -> list[dict]:
        """Search symbols via Shoonya's searchscrip API."""
        try:
            self._inject_oauth(token, credentials)
            result = self._api.searchscrip(exchange=exchange, searchtext=query)
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            raise BrokerError(f"Symbol search error: {e}", broker=BROKER_NAME) from e

    def start_websocket(self, subscribe_callback, socket_open_callback, socket_close_callback, order_update_callback=None) -> None:
        """Start Shoonya NorenApi WebSocket in a background daemon thread."""
        self._stop_websocket()
        self._api.start_websocket(
            subscribe_callback=subscribe_callback,
            order_update_callback=order_update_callback,
            socket_open_callback=socket_open_callback,
            socket_close_callback=socket_close_callback,
        )

    def _stop_websocket(self) -> None:
        """Stop any previous NorenApi WebSocket thread before starting a new one.

        NorenApi.close_websocket() early-returns when the socket is already
        disconnected, but its reconnect thread keeps running — so a later
        start_websocket() would leave two live connections delivering every
        tick and order update twice. Set the stop event and join the thread
        directly (name-mangled access, same as _inject_oauth's).
        """
        stop_event = getattr(self._api, "_NorenApi__stop_event", None)
        if stop_event is None:
            return  # websocket never started
        stop_event.set()
        ws = getattr(self._api, "_NorenApi__websocket", None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = getattr(self._api, "_NorenApi__ws_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def ws_subscribe_orders(self) -> None:
        """Subscribe to broker-side order updates over the live WebSocket."""
        self._api.subscribe_orders()

    def ws_subscribe(self, symbol: str, feed_type=None) -> None:
        # feed_type=None keeps the historical TOUCHLINE default untouched.
        # FeedType.SNAPQUOTE ('d') asks for the depth feed, which carries more
        # per-leg state than touchline does — including open interest, which
        # the REST path currently costs one round-trip per leg to obtain.
        if feed_type is None:
            self._api.subscribe(symbol)
        else:
            self._api.subscribe(symbol, feed_type)

    def ws_unsubscribe(self, symbol: str, feed_type=None) -> None:
        # Must mirror the feed_type used to subscribe: unsubscribing TOUCHLINE
        # does not remove a SNAPQUOTE subscription, and the stale one keeps
        # streaming.
        if feed_type is not None:
            self._api.unsubscribe(symbol, feed_type)
            return
        self._api.unsubscribe(symbol)

    async def searchSymbols(self, token: SessionToken, credentials: dict, exchange: str, query: str) -> list[dict]:
        """Search for trading symbols using live Shoonya API."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._search_symbols_sync, token, credentials, exchange, query)

    def _subscribe_market_data_sync(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Subscribe to market data via WebSocket."""
        try:
            self._inject_oauth(token, credentials)
            # Convert symbol names to token format if needed
            token_symbols = self._resolve_symbols_to_tokens(credentials, symbols)
            # Subscribe to each symbol
            for sym in token_symbols:
                self._api.subscribe(sym)
        except Exception as e:
            raise BrokerError(f"Market data subscription failed: {e}", broker=BROKER_NAME) from e

    def _unsubscribe_market_data_sync(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Unsubscribe from market data via WebSocket."""
        try:
            self._inject_oauth(token, credentials)
            # Convert symbol names to token format if needed
            token_symbols = self._resolve_symbols_to_tokens(credentials, symbols)
            # Unsubscribe from each symbol
            for sym in token_symbols:
                self._api.unsubscribe(sym)
        except Exception as e:
            raise BrokerError(f"Market data unsubscription failed: {e}", broker=BROKER_NAME) from e

    def _subscribe_order_updates_sync(self, token: SessionToken, credentials: dict) -> None:
        """Subscribe to order updates via WebSocket."""
        try:
            self._inject_oauth(token, credentials)
            self._api.subscribe_orders()
        except Exception as e:
            raise BrokerError(f"Order updates subscription failed: {e}", broker=BROKER_NAME) from e

    def _resolve_symbols_to_tokens(self, credentials: dict, symbols: list[str]) -> list[str]:
        """
        Convert symbol names to token format.

        Input formats:
          - "NSE|22" → already token format, return as-is
          - "NSE|RELIANCE-EQ" → symbol format, lookup token via:
            1. scripmaster.csv (if loaded) - fast, no API calls
            2. searchscrip API (fallback) - slower, uses API

        Returns:
            List of symbols in "EXCHANGE|TOKEN" format
        """
        resolved = []
        scripmaster = get_scripmaster()

        for sym in symbols:
            if not sym or '|' not in sym:
                continue

            parts = sym.split('|')
            if len(parts) != 2:
                continue

            exchange, identifier = parts

            # If identifier is numeric, it's already a token
            if identifier.isdigit():
                resolved.append(sym)
            else:
                # Symbol name, need to search for token
                token_num = None

                # Try scripmaster first (fast, local)
                if scripmaster.is_loaded():
                    token_num = scripmaster.get_token(exchange, identifier)

                # Fallback to API search (slower, but always works)
                if not token_num:
                    try:
                        result = self._api.searchscrip(exchange=exchange, searchtext=identifier)
                        if result and result.get("stat") == "Ok" and result.get("values"):
                            token_num = result["values"][0].get("token")
                    except Exception as e:
                        print(f"Warning: Failed to resolve {sym}: {e}")

                if token_num:
                    resolved.append(f"{exchange}|{token_num}")

        return resolved

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _inject_oauth(self, token: SessionToken, credentials: dict) -> None:
        """Inject OAuth headers into API client."""
        account_id = credentials.get("account_id", "")
        self._api.injectOAuthHeader(token.token, token.broker_uid, account_id)

    @staticmethod
    def _build_checksum(vendor_code: str, api_secret: str, oauth_code: str) -> str:
        """Build sha256 checksum for /GenAcsTok request."""
        return hashlib.sha256(f"{vendor_code}{api_secret}{oauth_code}".encode()).hexdigest()

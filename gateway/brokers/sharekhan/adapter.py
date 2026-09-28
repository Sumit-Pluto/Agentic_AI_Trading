"""Sharekhan broker adapter."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from SharekhanApi import SharekhanConnect
from brokers.base import BrokerBase, SessionToken, UserProfile, BrokerError


BROKER_NAME = "sharekhan"
TOKEN_STORE_PATH = Path(__file__).parent.parent.parent / "token_store_sharekhan.json"


class SharekhanTokenCache:
    """Token cache for Sharekhan sessions with 24-hour auto-refresh."""

    def __init__(self, cache_path=TOKEN_STORE_PATH):
        self.cache_path = cache_path

    def _load_cache(self) -> dict:
        """Load token cache from file."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self, data: dict) -> None:
        """Save token cache to file."""
        try:
            with open(self.cache_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to save token cache: {e}")

    def get_cached_session(self, user_id: str) -> dict | None:
        """Get cached session token for user if still valid (< 24 hours old).

        Returns None if:
        - No cached token
        - Token older than 24 hours (auto-refresh needed)
        """
        cache = self._load_cache()
        cached = cache.get(user_id)

        if not cached:
            return None

        # Check if token is still valid (less than 24 hours old)
        issued_at = datetime.fromisoformat(cached["issued_at"])
        age_hours = (datetime.now() - issued_at).total_seconds() / 3600

        if age_hours > 24:
            print(f"⏰ Token for {user_id} is {age_hours:.1f} hours old - needs refresh")
            return None  # Token expired, needs refresh

        return cached

    def cache_session(self, user_id: str, access_token: str, issued_at: str) -> None:
        """Cache session token for user."""
        cache = self._load_cache()
        cache[user_id] = {
            "access_token": access_token,
            "issued_at": issued_at,
        }
        self._save_cache(cache)
        print(f"✅ Cached token for {user_id} (expires in 24h)")

    def invalidate_session(self, user_id: str) -> None:
        """Invalidate cached session for user."""
        cache = self._load_cache()
        cache.pop(user_id, None)
        self._save_cache(cache)


class SharekhanBroker(BrokerBase):
    """Sharekhan broker adapter (stateless).

    All I/O runs in thread pool via run_in_executor.
    Uses shareconnect library for API calls.
    Token caching uses SharekhanTokenCache.
    """

    def __init__(self):
        """Initialize Sharekhan broker."""
        self._token_cache = SharekhanTokenCache()

    async def login(self, credentials: dict) -> SessionToken:
        """Full Sharekhan login flow (3-step OAuth)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._login_sync, credentials)

    async def logout(self, token: SessionToken, credentials: dict) -> bool:
        """Invalidate session."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._logout_sync, token, credentials)

    async def refreshSession(self, token: SessionToken, credentials: dict) -> SessionToken:
        """Refresh the session token (re-authenticate)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._refresh_session_sync, token, credentials)

    async def placeOrder(self, token: SessionToken, credentials: dict, order: dict) -> dict:
        """Place an order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._place_order_sync, token, credentials, order)

    async def modifyOrder(self, token: SessionToken, credentials: dict, order_id: str, params: dict) -> dict:
        """Modify an existing order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._modify_order_sync, token, credentials, order_id, params)

    async def cancelOrder(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Cancel an order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._cancel_order_sync, token, credentials, order_id)

    async def getOrderStatus(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Get the status of a specific order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_order_status_sync, token, credentials, order_id)

    async def getOrderBook(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get all orders for the account."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_order_book_sync, token, credentials)

    async def getPositions(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get open positions."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_positions_sync, token, credentials)

    async def getHoldings(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get securities held in the account."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_holdings_sync, token, credentials)

    async def getMargins(self, token: SessionToken, credentials: dict) -> dict:
        """Get account margin details."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_margins_sync, token, credentials)

    async def getQuote(self, token: SessionToken, credentials: dict, symbol: str, exchange: str) -> dict:
        """Get market quote for a symbol."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_quote_sync, token, credentials, symbol, exchange)

    async def getInstruments(self, token: SessionToken, credentials: dict, exchange: str) -> list[dict]:
        """Get list of available instruments on an exchange."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_instruments_sync, token, credentials, exchange)

    async def subscribeMarketData(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Subscribe to market data (WebSocket)."""
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
        """Perform full Sharekhan login (3-step OAuth flow).

        Step 1: Get request_token (auto via Selenium if not provided)
        Step 2: Generate session from request_token
        Step 3: Exchange for access_token
        Step 4: Cache for 24 hours

        Credentials dict can contain:
        - user_id: Required
        - api_key: Required
        - secret_key: Required
        - request_token: Optional (if not provided, extracted via Selenium)
        - password: Required if request_token not provided
        - totp_secret: Optional for 2FA
        """
        user_id = credentials.get("user_id")
        api_key = credentials.get("api_key")
        secret_key = credentials.get("secret_key")
        request_token = credentials.get("request_token")

        if not all([user_id, api_key, secret_key]):
            raise BrokerError(
                "Missing required credentials: user_id, api_key, secret_key",
                broker=BROKER_NAME,
            )

        # Check cached token first (valid for 24 hours)
        cached = self._token_cache.get_cached_session(user_id)
        if cached:
            print(f"✅ Using cached token for {user_id}")
            return SessionToken(
                token=cached["access_token"],
                broker_uid=user_id,
                issued_at=cached["issued_at"],
                broker_name=BROKER_NAME,
            )

        try:
            # If request_token not provided, extract via Selenium
            if not request_token:
                print(f"🔐 Requesting token not provided - extracting via Selenium...")
                if not credentials.get("password"):
                    raise BrokerError(
                        "Password required for automatic request token extraction",
                        broker=BROKER_NAME,
                    )

                from brokers.sharekhan.auth_code import get_request_token_with_credentials
                request_token = get_request_token_with_credentials(credentials)
                print(f"✅ Extracted request_token via Selenium")

            # Initialize client with API key
            login_client = SharekhanConnect(api_key)

            # Generate session from request_token
            session = login_client.generate_session(request_token, secret_key)

            # Exchange for access_token (with state parameter)
            access_token = login_client.get_access_token(api_key, session, state="12345")

            if not access_token:
                raise BrokerError("Failed to obtain access token", broker=BROKER_NAME)

            issued_at = datetime.now().isoformat()
            self._token_cache.cache_session(user_id, access_token, issued_at)

            return SessionToken(
                token=access_token,
                broker_uid=user_id,
                issued_at=issued_at,
                broker_name=BROKER_NAME,
            )

        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Login failed: {e}", broker=BROKER_NAME) from e

    def _logout_sync(self, token: SessionToken, credentials: dict) -> bool:
        """Perform logout."""
        try:
            user_id = token.broker_uid
            self._token_cache.invalidate_session(user_id)
            return True
        except Exception as e:
            raise BrokerError(f"Logout failed: {e}", broker=BROKER_NAME) from e

    def _refresh_session_sync(self, token: SessionToken, credentials: dict) -> SessionToken:
        """Refresh session (re-authenticate)."""
        user_id = credentials.get("user_id")
        self._token_cache.invalidate_session(user_id)
        return self._login_sync(credentials)

    def _place_order_sync(self, token: SessionToken, credentials: dict, order: dict) -> dict:
        """Place order."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            order_params = {
                "customerId": customer_id,
                "scripCode": order.get("scrip_code"),
                "tradingSymbol": order.get("trading_symbol"),
                "exchange": order.get("exchange", "NC"),
                "transactionType": order.get("transaction_type", "B"),
                "quantity": order.get("quantity", 1),
                "price": str(order.get("price", 0)),
                "orderType": order.get("order_type", "REGULAR"),
                "validity": order.get("validity", "DAY"),
                "productType": order.get("product_type", "INVESTMENT"),
            }

            if order.get("trigger_price"):
                order_params["triggerPrice"] = str(order["trigger_price"])

            result = client.placeOrder(order_params)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(
                    f"Place order failed: {result.get('emsg')}",
                    broker=BROKER_NAME,
                    raw=result,
                )
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Place order error: {e}", broker=BROKER_NAME) from e

    def _modify_order_sync(self, token: SessionToken, credentials: dict, order_id: str, params: dict) -> dict:
        """Modify order."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            order_params = {
                "customerId": customer_id,
                "orderId": order_id,
                "scripCode": params.get("scrip_code"),
                "tradingSymbol": params.get("trading_symbol"),
                "exchange": params.get("exchange", "NC"),
                "transactionType": params.get("transaction_type"),
                "quantity": params.get("quantity"),
                "price": str(params.get("price", 0)),
                "orderType": params.get("order_type", "REGULAR"),
                "validity": params.get("validity", "DAY"),
                "productType": params.get("product_type", "INVESTMENT"),
                "requestType": "MODIFY",
            }

            result = client.modifyOrder(order_params)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(
                    f"Modify order failed: {result.get('emsg')}",
                    broker=BROKER_NAME,
                    raw=result,
                )
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Modify order error: {e}", broker=BROKER_NAME) from e

    def _cancel_order_sync(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Cancel order."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            order_params = {
                "customerId": customer_id,
                "orderId": order_id,
                "requestType": "CANCEL",
            }

            result = client.cancelOrder(order_params)
            if isinstance(result, dict) and result.get("stat") != "Ok":
                raise BrokerError(
                    f"Cancel order failed: {result.get('emsg')}",
                    broker=BROKER_NAME,
                    raw=result,
                )
            return result
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Cancel order error: {e}", broker=BROKER_NAME) from e

    def _get_order_status_sync(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Get single order status."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            result = client.exchange("NC", customer_id, order_id)
            return result if isinstance(result, dict) else {}
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Get order status error: {e}", broker=BROKER_NAME) from e

    def _get_order_book_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get order book (all orders for the day)."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            result = client.reports(customer_id)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise BrokerError(f"Get order book error: {e}", broker=BROKER_NAME) from e

    def _get_positions_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get positions (open trades)."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            result = client.trades(customer_id)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise BrokerError(f"Get positions error: {e}", broker=BROKER_NAME) from e

    def _get_holdings_sync(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get holdings (demat securities)."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")

            result = client.holdings(customer_id)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise BrokerError(f"Get holdings error: {e}", broker=BROKER_NAME) from e

    def _get_margins_sync(self, token: SessionToken, credentials: dict) -> dict:
        """Get account margin/fund details."""
        try:
            client = self._get_client(token, credentials)
            customer_id = credentials.get("customer_id")
            exchange = "NC"

            result = client.funds(exchange, customer_id)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            raise BrokerError(f"Get margins error: {e}", broker=BROKER_NAME) from e

    def _get_quote_sync(self, token: SessionToken, credentials: dict, symbol: str, exchange: str) -> dict:
        """Get quote by scrip code.

        Note: SharekhanConnect doesn't provide a real-time quote API.
        Use historicaldata() for OHLCV data or WebSocket streaming for live quotes.
        """
        try:
            # Return empty dict - live quotes not available via REST API
            return {}
        except Exception as e:
            raise BrokerError(f"Get quote error: {e}", broker=BROKER_NAME) from e

    def _get_instruments_sync(self, token: SessionToken, credentials: dict, exchange: str) -> list[dict]:
        """Get instruments (scrip master) for exchange."""
        try:
            client = self._get_client(token, credentials)
            result = client.master(exchange)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise BrokerError(f"Get instruments error: {e}", broker=BROKER_NAME) from e

    def _subscribe_market_data_sync(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Subscribe to market data via WebSocket.

        Symbol format: "NC22" (exchange prefix + scrip code)
        """
        try:
            # WebSocket streaming not yet fully integrated
            # Would need SharekhanWebSocket and callback management
            pass
        except Exception as e:
            raise BrokerError(f"Market data subscription failed: {e}", broker=BROKER_NAME) from e

    def _unsubscribe_market_data_sync(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Unsubscribe from market data via WebSocket."""
        try:
            # WebSocket streaming not yet fully integrated
            pass
        except Exception as e:
            raise BrokerError(f"Market data unsubscription failed: {e}", broker=BROKER_NAME) from e

    def _subscribe_order_updates_sync(self, token: SessionToken, credentials: dict) -> None:
        """Subscribe to order updates via WebSocket."""
        try:
            # WebSocket streaming not yet fully integrated
            pass
        except Exception as e:
            raise BrokerError(f"Order updates subscription failed: {e}", broker=BROKER_NAME) from e

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _get_client(self, token: SessionToken, credentials: dict) -> SharekhanConnect:
        """Get or create SharekhanConnect client with access token."""
        api_key = credentials.get("api_key")
        return SharekhanConnect(api_key, token.token)

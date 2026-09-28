from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionToken:
    token: str
    broker_uid: str
    issued_at: str
    broker_name: str


@dataclass
class UserProfile:
    broker_uid: str
    account_id: str
    email: str
    mobile: str
    broker_name: str
    full_name: str
    branch_id: str
    enabled_exchanges: list[str]
    enabled_products: list[str]
    raw: dict = field(default_factory=dict)


class BrokerError(Exception):
    def __init__(self, message: str, broker: str = "", code: str = "", raw: dict | None = None):
        super().__init__(message)
        self.broker = broker
        self.code = code
        self.raw = raw or {}


class BrokerBase(ABC):
    """Abstract base class for all broker adapters."""

    # Session methods
    @abstractmethod
    async def login(self, credentials: dict) -> SessionToken:
        """Full login flow. Returns SessionToken on success. Raises BrokerError on failure."""
        pass

    @abstractmethod
    async def logout(self, token: SessionToken, credentials: dict) -> bool:
        """Invalidate the session on broker's server. Returns True if successful."""
        pass

    @abstractmethod
    async def refreshSession(self, token: SessionToken, credentials: dict) -> SessionToken:
        """Refresh/renew the session token. Returns a new SessionToken."""
        pass

    # Order methods
    @abstractmethod
    async def placeOrder(self, token: SessionToken, credentials: dict, order: dict) -> dict:
        """Place a new order. Returns order response dict with order_id, status, etc."""
        pass

    @abstractmethod
    async def modifyOrder(self, token: SessionToken, credentials: dict, order_id: str, params: dict) -> dict:
        """Modify an existing order. Returns modification response dict."""
        pass

    @abstractmethod
    async def cancelOrder(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Cancel an order. Returns cancellation response dict."""
        pass

    @abstractmethod
    async def getOrderStatus(self, token: SessionToken, credentials: dict, order_id: str) -> dict:
        """Get the status of a specific order. Returns order status dict."""
        pass

    @abstractmethod
    async def getOrderBook(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get all orders for the account. Returns list of order dicts."""
        pass

    # Portfolio methods
    @abstractmethod
    async def getPositions(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get open positions. Returns list of position dicts (holdings with P&L)."""
        pass

    @abstractmethod
    async def getHoldings(self, token: SessionToken, credentials: dict) -> list[dict]:
        """Get securities held in the account. Returns list of holding dicts."""
        pass

    @abstractmethod
    async def getMargins(self, token: SessionToken, credentials: dict) -> dict:
        """Get account margin details. Returns margin info dict (available, utilised, etc.)."""
        pass

    async def getOrderMargin(self, token: SessionToken, credentials: dict, positions: list) -> dict:
        """SPAN+exposure margin required for a set of prospective positions.
        Optional per broker — default is 'not supported'. `positions` is a list
        of broker-native position dicts."""
        raise NotImplementedError("order margin not supported for this broker")

    # Market data methods
    @abstractmethod
    async def getQuote(self, token: SessionToken, credentials: dict, symbol: str, exchange: str) -> dict:
        """Get market quote for a symbol. Returns quote dict (LTP, bid, ask, volume, etc.)."""
        pass

    @abstractmethod
    async def getInstruments(self, token: SessionToken, credentials: dict, exchange: str) -> list[dict]:
        """Get list of available instruments on an exchange. Returns list of instrument dicts."""
        pass

    @abstractmethod
    async def subscribeMarketData(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Subscribe to market data for symbols. Stub for now — streaming pattern TBD."""
        pass

    @abstractmethod
    async def unsubscribeMarketData(self, token: SessionToken, credentials: dict, symbols: list[str]) -> None:
        """Unsubscribe from market data. Stub for now."""
        pass

    @abstractmethod
    async def subscribeOrderUpdates(self, token: SessionToken, credentials: dict) -> None:
        """Subscribe to order updates. Stub for now — streaming pattern TBD."""
        pass

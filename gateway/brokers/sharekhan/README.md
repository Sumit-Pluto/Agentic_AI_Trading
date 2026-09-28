# Sharekhan Broker Adapter

Complete implementation of the Sharekhan broker adapter for the Gateway System.

## Overview

The Sharekhan adapter implements all required BrokerBase methods for trading via Sharekhan's API:
- **Session Management**: Login, logout, refresh via 3-step OAuth
- **Order Operations**: Place, modify, cancel, status, order book
- **Portfolio**: Positions, holdings, margins
- **Market Data**: Quotes, instruments, WebSocket streaming (stub)

## Authentication Flow

Sharekhan uses a 3-step OAuth process:

1. **Generate Login URL**: User visits Sharekhan's login page
2. **Get Request Token**: After login, Sharekhan redirects with `request_token`
3. **Generate Session**: Client-side AES-GCM encryption of request_token
4. **Exchange for Access Token**: POST to `/skapi/services/access/token`

All subsequent API calls use the `access_token` in headers.

## Setup

### 1. Environment Variables

Add these to your `.env` file:

```env
SHAREKHAN_API_KEY=<your-api-key>
SHAREKHAN_SECRET_KEY=<your-32-byte-secret-key>
SHAREKHAN_API_HOST=https://api.sharekhan.com
SHAREKHAN_STREAM_HOST=wss://stream.sharekhan.com/skstream/api/stream
```

### 2. Credentials Dictionary

When calling `login()`, provide a credentials dict:

```python
credentials = {
    "user_id": "YOUR_USER_ID",
    "api_key": os.getenv("SHAREKHAN_API_KEY"),
    "secret_key": os.getenv("SHAREKHAN_SECRET_KEY"),
    "request_token": "token_from_sharekhan_login",  # After user logs in
    "customer_id": "YOUR_CUSTOMER_ID",  # Required for API calls
}
```

### 3. Installation

Install the required package:

```bash
pip install shareconnect==1.0.0.11
```

## Usage Example

```python
import asyncio
from brokers.registry import get_broker

async def test_sharekhan():
    broker = get_broker("sharekhan")
    
    credentials = {
        "user_id": "USER123",
        "api_key": "your-api-key",
        "secret_key": "your-secret-key",
        "request_token": "req_token_from_oauth",
        "customer_id": "CUST123",
    }
    
    # Login
    token = await broker.login(credentials)
    
    # Place order
    order = {
        "scrip_code": "11",
        "trading_symbol": "RELIANCE",
        "exchange": "NC",  # NC=NSE Cash, NF=NSE F&O, RN=NSE Currency
        "transaction_type": "B",  # B=Buy, S=Sell
        "quantity": 1,
        "price": "2500.00",
        "order_type": "REGULAR",
        "validity": "DAY",
        "product_type": "INVESTMENT",
    }
    result = await broker.placeOrder(token, credentials, order)
    
    # Get positions
    positions = await broker.getPositions(token, credentials)
    
    # Logout
    await broker.logout(token, credentials)
```

## Exchange Codes

Sharekhan uses exchange prefixes in symbol formats:

| Code | Exchange | Notes |
|------|----------|-------|
| NC   | NSE Cash | Equity shares |
| NF   | NSE F&O  | Futures & Options |
| RN   | NSE Currency | Currency pairs |
| MC   | MCX Currency | Commodity currency |
| MX   | MCX Commodity | Metals, energy, agri |

## API Methods

### Session Methods

- **`login(credentials)`**: Authenticate user, return SessionToken
- **`logout(token, credentials)`**: Invalidate session
- **`refreshSession(token, credentials)`**: Re-authenticate (new access token)

### Order Methods

- **`placeOrder(token, credentials, order)`**: Create new order
- **`modifyOrder(token, credentials, order_id, params)`**: Update existing order
- **`cancelOrder(token, credentials, order_id)`**: Cancel order
- **`getOrderStatus(token, credentials, order_id)`**: Get single order details
- **`getOrderBook(token, credentials)`**: Get all orders for today

### Portfolio Methods

- **`getPositions(token, credentials)`**: Open/closed trades
- **`getHoldings(token, credentials)`**: Demat holdings
- **`getMargins(token, credentials)`**: Account funds and margins

### Market Data Methods

- **`getQuote(token, credentials, symbol, exchange)`**: LTP, bid, ask, volume
- **`getInstruments(token, credentials, exchange)`**: Scrip master for exchange
- **`subscribeMarketData(token, credentials, symbols)`**: WebSocket subscription (stub)
- **`unsubscribeMarketData(token, credentials, symbols)`**: Unsubscribe (stub)
- **`subscribeOrderUpdates(token, credentials)`**: Order updates via WebSocket (stub)

## Token Caching

Session tokens are cached in `token_store_sharekhan.json` by user_id:

```json
{
  "USER123": {
    "access_token": "...",
    "issued_at": "2024-06-08T10:30:45.123456"
  }
}
```

Cache is automatically used on next login call for same user.

## WebSocket (Not Yet Implemented)

WebSocket subscriptions are stubs. To implement:

1. Use `SharekhanWebSocket` from the `shareconnect` library
2. Connect to `wss://stream.sharekhan.com/skstream/api/stream?access_token=...`
3. Subscribe to symbols in format: `"NC22,NF37833"` (exchange + scrip code)
4. Parse binary tick frames for market data
5. Re-subscribe on reconnection

## Error Handling

All API errors raise `BrokerError` with:
- `message`: Human-readable error description
- `broker`: Broker name ("sharekhan")
- `code`: Error code (if available)
- `raw`: Raw broker response dict

Example:

```python
try:
    result = await broker.placeOrder(token, credentials, order)
except BrokerError as e:
    print(f"Order failed: {e.message}")
    print(f"Raw response: {e.raw}")
```

## Known Limitations

1. **WebSocket Streaming**: Market data and order update subscriptions are stubs
2. **customerId Required**: Unlike some brokers, Sharekhan requires explicit customer_id on all calls
3. **Request Token**: Must be obtained manually via Sharekhan's OAuth flow (browser login)
4. **Single API Host**: Unlike Shoonya, there's one base URL for all endpoints

## API Reference

For complete Sharekhan API details, visit:
https://www.sharekhan.com/trading-api/documentation/overview

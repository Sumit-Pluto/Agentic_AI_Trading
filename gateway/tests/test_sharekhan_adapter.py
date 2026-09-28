"""Test cases for Sharekhan broker adapter.

These are LIVE-integration tests for the Sharekhan broker, which is out of scope
for this Shoonya-only intraday deployment (Sharekhan is registered lazily and
optional — see brokers/registry.py). They hit the network and need real
credentials, so they are skipped by default. Set RUN_SHAREKHAN_TESTS=1 to run.
"""

import asyncio
import os
from datetime import datetime

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SHAREKHAN_TESTS") != "1",
    reason="Sharekhan out of scope (Shoonya-only); live-integration tests. "
           "Set RUN_SHAREKHAN_TESTS=1 to run.",
)

from brokers.sharekhan.adapter import SharekhanBroker
from brokers.base import BrokerError


# Test configuration
CREDENTIALS = {
    "user_id": "TEST_USER_ID",
    "api_key": os.getenv("SHAREKHAN_API_KEY", "test_api_key"),
    "secret_key": os.getenv("SHAREKHAN_SECRET_KEY", "test_secret_key_32_bytes_long!!!"),
    "request_token": os.getenv("SHAREKHAN_REQUEST_TOKEN", "test_request_token"),
    "customer_id": os.getenv("SHAREKHAN_CUSTOMER_ID", "TEST_CUST_ID"),
}


async def test_login():
    """Test login functionality."""
    print("\n" + "="*80)
    print("TEST 1: Login")
    print("="*80)

    broker = SharekhanBroker()

    try:
        token = await broker.login(CREDENTIALS)
        print(f"✅ Login successful")
        token_str = str(token.token)
        print(f"   Token: {(token_str[:50] + '...') if len(token_str) > 50 else token_str}")
        print(f"   User ID: {token.broker_uid}")
        print(f"   Issued At: {token.issued_at}")
        print(f"   Broker: {token.broker_name}")
        return token
    except BrokerError as e:
        print(f"❌ Login failed: {e}")
        print(f"   Error code: {e.code}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_positions(token):
    """Test getting positions."""
    print("\n" + "="*80)
    print("TEST 2: Get Positions")
    print("="*80)

    broker = SharekhanBroker()

    try:
        positions = await broker.getPositions(token, CREDENTIALS)
        print(f"✅ Get positions successful")
        print(f"   Total positions: {len(positions)}")
        if positions:
            print(f"   Sample position: {positions[0]}")
        return positions
    except BrokerError as e:
        print(f"❌ Get positions failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_holdings(token):
    """Test getting holdings."""
    print("\n" + "="*80)
    print("TEST 3: Get Holdings")
    print("="*80)

    broker = SharekhanBroker()

    try:
        holdings = await broker.getHoldings(token, CREDENTIALS)
        print(f"✅ Get holdings successful")
        print(f"   Total holdings: {len(holdings)}")
        if holdings:
            print(f"   Sample holding: {holdings[0]}")
        return holdings
    except BrokerError as e:
        print(f"❌ Get holdings failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_margins(token):
    """Test getting margins/funds."""
    print("\n" + "="*80)
    print("TEST 4: Get Margins")
    print("="*80)

    broker = SharekhanBroker()

    try:
        margins = await broker.getMargins(token, CREDENTIALS)
        print(f"✅ Get margins successful")
        print(f"   Margins data: {margins}")
        return margins
    except BrokerError as e:
        print(f"❌ Get margins failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_order_book(token):
    """Test getting order book."""
    print("\n" + "="*80)
    print("TEST 5: Get Order Book")
    print("="*80)

    broker = SharekhanBroker()

    try:
        orders = await broker.getOrderBook(token, CREDENTIALS)
        print(f"✅ Get order book successful")
        print(f"   Total orders: {len(orders)}")
        if orders:
            print(f"   Sample order: {orders[0]}")
        return orders
    except BrokerError as e:
        print(f"❌ Get order book failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_quote(token):
    """Test getting market quote."""
    print("\n" + "="*80)
    print("TEST 6: Get Quote")
    print("="*80)

    broker = SharekhanBroker()

    # Test with a common scrip code (RELIANCE = 11)
    scrip_code = "11"
    exchange = "NC"

    try:
        quote = await broker.getQuote(token, CREDENTIALS, scrip_code, exchange)
        print(f"✅ Get quote successful")
        print(f"   Quote for {scrip_code}: {quote}")
        return quote
    except BrokerError as e:
        print(f"❌ Get quote failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_instruments(token):
    """Test getting instruments/scrip master."""
    print("\n" + "="*80)
    print("TEST 7: Get Instruments")
    print("="*80)

    broker = SharekhanBroker()
    exchange = "NC"  # NSE Cash

    try:
        instruments = await broker.getInstruments(token, CREDENTIALS, exchange)
        print(f"✅ Get instruments successful")
        print(f"   Total instruments on {exchange}: {len(instruments)}")
        if instruments:
            print(f"   Sample instrument: {instruments[0]}")
        return instruments
    except BrokerError as e:
        print(f"❌ Get instruments failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_place_order(token):
    """Test placing an order (DEMO - doesn't actually place)."""
    print("\n" + "="*80)
    print("TEST 8: Place Order (Demo Order)")
    print("="*80)

    broker = SharekhanBroker()

    # Demo order - set to very high price so it doesn't execute
    order = {
        "scrip_code": "11",  # RELIANCE
        "trading_symbol": "RELIANCE",
        "exchange": "NC",
        "transaction_type": "B",
        "quantity": 1,
        "price": "99999.00",  # Unrealistic price
        "order_type": "REGULAR",
        "validity": "DAY",
        "product_type": "INVESTMENT",
    }

    try:
        result = await broker.placeOrder(token, CREDENTIALS, order)
        print(f"✅ Place order successful")
        print(f"   Order response: {result}")
        return result
    except BrokerError as e:
        print(f"❌ Place order failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_get_order_status(token):
    """Test getting order status (requires valid order_id)."""
    print("\n" + "="*80)
    print("TEST 9: Get Order Status")
    print("="*80)

    broker = SharekhanBroker()

    # You would need a real order_id from a previous order placement
    order_id = os.getenv("TEST_ORDER_ID", "TEST_ORDER_123")

    try:
        status = await broker.getOrderStatus(token, CREDENTIALS, order_id)
        print(f"✅ Get order status successful")
        print(f"   Order status: {status}")
        return status
    except BrokerError as e:
        print(f"❌ Get order status failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_refresh_session(token):
    """Test refreshing the session."""
    print("\n" + "="*80)
    print("TEST 10: Refresh Session")
    print("="*80)

    broker = SharekhanBroker()

    try:
        new_token = await broker.refreshSession(token, CREDENTIALS)
        print(f"✅ Refresh session successful")
        token_str = str(new_token.token)
        print(f"   New token: {(token_str[:50] + '...') if len(token_str) > 50 else token_str}")
        print(f"   Issued at: {new_token.issued_at}")
        return new_token
    except BrokerError as e:
        print(f"❌ Refresh session failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_logout(token):
    """Test logout."""
    print("\n" + "="*80)
    print("TEST 11: Logout")
    print("="*80)

    broker = SharekhanBroker()

    try:
        result = await broker.logout(token, CREDENTIALS)
        print(f"✅ Logout successful: {result}")
        return result
    except BrokerError as e:
        print(f"❌ Logout failed: {e}")
        print(f"   Raw response: {e.raw}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None


async def test_error_handling():
    """Test error handling with invalid credentials."""
    print("\n" + "="*80)
    print("TEST 12: Error Handling")
    print("="*80)

    broker = SharekhanBroker()

    invalid_credentials = {
        "user_id": "INVALID_USER",
        "api_key": "invalid_key",
        "secret_key": "invalid_secret",
        "request_token": "invalid_token",
        "customer_id": "INVALID_CUST",
    }

    try:
        token = await broker.login(invalid_credentials)
        print(f"⚠️  Error handling test - should have failed")
    except BrokerError as e:
        print(f"✅ Error handled correctly")
        print(f"   Error message: {e}")
        print(f"   Broker: {e.broker}")
        print(f"   Code: {e.code}")
    except Exception as e:
        print(f"✅ Error handled (unexpected type): {type(e).__name__}")
        print(f"   Message: {e}")


async def run_all_tests():
    """Run all tests in sequence."""
    print("\n")
    print("╔" + "="*78 + "╗")
    print("║" + " "*78 + "║")
    print("║" + "SHAREKHAN ADAPTER TEST SUITE".center(78) + "║")
    print("║" + " "*78 + "║")
    print("╚" + "="*78 + "╝")

    print("\n📋 CREDENTIALS CONFIGURATION:")
    print(f"   User ID: {CREDENTIALS['user_id']}")
    print(f"   API Key: {CREDENTIALS['api_key'][:20]}..." if CREDENTIALS['api_key'] != "test_api_key" else "   API Key: NOT SET")
    print(f"   Secret Key: {CREDENTIALS['secret_key'][:20]}..." if CREDENTIALS['secret_key'] != "test_secret_key_32_bytes_long!!!" else "   Secret Key: NOT SET")
    print(f"   Request Token: {CREDENTIALS['request_token'][:20]}..." if CREDENTIALS['request_token'] != "test_request_token" else "   Request Token: NOT SET")
    print(f"   Customer ID: {CREDENTIALS['customer_id']}")

    print("\n⚠️  NOTE: Tests will FAIL without real credentials!")
    print("   Set environment variables:")
    print("   - SHAREKHAN_API_KEY")
    print("   - SHAREKHAN_SECRET_KEY")
    print("   - SHAREKHAN_REQUEST_TOKEN")
    print("   - SHAREKHAN_CUSTOMER_ID")

    # Run tests
    token = await test_login()

    if token:
        await test_get_positions(token)
        await test_get_holdings(token)
        await test_get_margins(token)
        await test_get_order_book(token)
        await test_get_quote(token)
        await test_get_instruments(token)
        await test_place_order(token)
        await test_get_order_status(token)
        await test_refresh_session(token)
        await test_logout(token)
    else:
        print("\n❌ Login failed - skipping remaining tests")

    await test_error_handling()

    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print("✅ All test methods executed")
    print("⚠️  Some tests may have failed due to missing credentials")
    print("   See individual test results above for details")
    print("="*80 + "\n")


async def test_single_method():
    """Test a single method (customize as needed)."""
    print("\n" + "="*80)
    print("SINGLE METHOD TEST")
    print("="*80)

    broker = SharekhanBroker()

    # Try to login first
    try:
        token = await broker.login(CREDENTIALS)

        # Then test a specific method
        print("\nTesting getPositions()...")
        positions = await broker.getPositions(token, CREDENTIALS)
        print(f"Result: {positions}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    # Run all tests
    asyncio.run(run_all_tests())

    # Or run single method test (uncomment to use):
    # asyncio.run(test_single_method())

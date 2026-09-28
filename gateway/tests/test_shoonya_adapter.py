#!/usr/bin/env python3
"""
Test script for ShoonyaBroker adapter.

Usage:
    # Step 1: Generate FERNET_KEY and add to .env
    python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())"

    # Step 2: Copy the output and add to .env file

    # Step 3: Run this test
    python3 test_shoonya_adapter.py
"""

import asyncio
import os
from dotenv import load_dotenv
from brokers.shoonya import ShoonyaBroker
from crypto import encrypt_json, decrypt_json

load_dotenv()


async def test_adapter():
    """Test the ShoonyaBroker adapter with credentials from .env"""

    print("=" * 80)
    print("SHOONYA BROKER ADAPTER TEST")
    print("=" * 80)

    # Prepare credentials from .env
    credentials = {
        "user_id": os.getenv("SHONYA_USER_ID"),
        "password": os.getenv("SHONYA_PASSWORD"),
        "totp_secret": os.getenv("SHONYA_TWO_FA"),
        "vendor_code": os.getenv("SHONYA_VENDOR_CODE"),
        "api_secret": os.getenv("SHONYA_API_SECRET"),
        "api_host": os.getenv("SHOONYA_API_HOST"),
        "account_id": "",  # Not needed for login
    }

    print("\n1. Testing credential encryption/decryption...")
    try:
        encrypted = encrypt_json(credentials)
        decrypted = decrypt_json(encrypted)
        print("   ✓ Encryption/decryption works")
        print(f"   ✓ Encrypted size: {len(encrypted)} chars")
    except Exception as e:
        print(f"   ✗ Encryption failed: {e}")
        return

    print("\n2. Testing ShoonyaBroker.login()...")
    broker = ShoonyaBroker()
    try:
        token = await broker.login(credentials)
        print(f"   ✓ Login successful!")
        print(f"     - Token: {token.token[:30]}...")
        print(f"     - Broker UID: {token.broker_uid}")
        print(f"     - Issued at: {token.issued_at}")
        print(f"     - Broker name: {token.broker_name}")
    except Exception as e:
        print(f"   ✗ Login failed: {e}")
        return

    print("\n3. Testing ShoonyaBroker.validate_token()...")
    try:
        is_valid = await broker.validate_token(token, credentials)
        if is_valid:
            print("   ✓ Token validation successful!")
        else:
            print("   ⚠ Token is invalid")
    except Exception as e:
        print(f"   ✗ Validation failed: {e}")

    print("\n4. Testing ShoonyaBroker.get_user_profile()...")
    try:
        profile = await broker.get_user_profile(token, credentials)
        print("   ✓ User profile fetched!")
        print(f"     - UID: {profile.broker_uid}")
        print(f"     - Account ID: {profile.account_id}")
        print(f"     - Email: {profile.email}")
        print(f"     - Mobile: {profile.mobile}")
        print(f"     - Broker: {profile.broker_name}")
        print(f"     - Exchanges: {profile.enabled_exchanges}")
    except Exception as e:
        print(f"   ✗ Profile fetch failed: {e}")

    print("\n5. Testing ShoonyaBroker.logout()...")
    try:
        result = await broker.logout(token, credentials)
        if result:
            print("   ✓ Logout successful!")
        else:
            print("   ⚠ Logout returned False (check if token was invalidated)")
    except Exception as e:
        print(f"   ✗ Logout failed: {e}")

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    # Check if FERNET_KEY is set
    if not os.getenv("FERNET_KEY") or os.getenv("FERNET_KEY") == "your_fernet_key_here":
        print("❌ ERROR: FERNET_KEY not set in .env")
        print("\nTo generate FERNET_KEY, run:")
        print("  python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        print("\nThen add to .env file as:")
        print("  FERNET_KEY=<generated_key>")
        exit(1)

    asyncio.run(test_adapter())

"""Sharekhan OAuth request token extraction via Selenium."""

import re
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def get_request_token_with_credentials(credentials: dict) -> str:
    """
    Obtain Sharekhan request token via Selenium browser automation.

    Flow:
    1. Open OAuth login URL in headless Chrome
    2. Fill in credentials (user_id, password, TOTP if needed)
    3. Wait for redirect to callback URL
    4. Extract request_token from redirect URL

    Args:
        credentials: Dict with keys:
            - user_id: Sharekhan user ID
            - password: Sharekhan password
            - totp_secret: (optional) TOTP secret for 2FA
            - api_key: Sharekhan API key

    Returns:
        request_token: Extracted from OAuth redirect URL

    Raises:
        Exception: If login fails or timeout occurs
    """
    api_key = credentials.get("api_key")
    user_id = credentials.get("user_id")
    password = credentials.get("password")
    totp_secret = credentials.get("totp_secret")

    if not all([api_key, user_id, password]):
        raise ValueError("Missing required credentials: api_key, user_id, password")

    # Build OAuth login URL
    login_url = f"https://api.sharekhan.com/skapi/auth/login.html?api_key={api_key}&state=12345"

    # Setup Chrome options for headless browser
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = None
    try:
        # Initialize Chrome driver
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        print(f"Opening Sharekhan OAuth login URL...")
        driver.get(login_url)

        # Wait for login form to load
        wait = WebDriverWait(driver, 30)

        # Find and fill user ID field
        print(f"Filling user ID: {user_id}")
        user_id_field = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[name*='user'], input[placeholder*='User']"))
        )
        user_id_field.clear()
        user_id_field.send_keys(user_id)
        time.sleep(1)

        # Find and fill password field
        print(f"Filling password...")
        password_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        password_field.clear()
        password_field.send_keys(password)
        time.sleep(1)

        # If TOTP is provided, fill TOTP field
        if totp_secret:
            import pyotp
            totp_code = pyotp.TOTP(totp_secret).now()
            print(f"Filling TOTP code...")
            try:
                totp_field = driver.find_element(
                    By.CSS_SELECTOR, "input[type='text'][placeholder*='OTP'], input[name*='otp'], input[placeholder*='2FA']"
                )
                totp_field.clear()
                totp_field.send_keys(totp_code)
                time.sleep(1)
            except Exception as e:
                print(f"Warning: Could not find TOTP field: {e}")

        # Find and click login button
        print(f"Clicking login button...")
        login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button:contains('Login')")
        login_button.click()

        # Wait for redirect to callback URL (with request_token)
        print(f"Waiting for OAuth callback...")
        wait.until(lambda d: "request_token=" in d.current_url)

        # Extract request_token from URL
        callback_url = driver.current_url
        print(f"Got callback URL: {callback_url[:80]}...")

        # Parse request_token from URL
        match = re.search(r"request_token=([^&]+)", callback_url)
        if not match:
            raise Exception(f"Could not extract request_token from URL: {callback_url}")

        request_token = match.group(1)
        print(f"✅ Successfully extracted request_token")

        return request_token

    except Exception as e:
        print(f"❌ Failed to get request token: {e}")
        raise

    finally:
        if driver:
            driver.quit()


def get_request_token_from_env() -> str:
    """
    Get cached request token from environment variable.

    Returns:
        request_token: From SHAREKHAN_REQUEST_TOKEN env var
    """
    import os
    token = os.getenv("SHAREKHAN_REQUEST_TOKEN")
    if not token:
        raise ValueError("SHAREKHAN_REQUEST_TOKEN not set in environment")
    return token

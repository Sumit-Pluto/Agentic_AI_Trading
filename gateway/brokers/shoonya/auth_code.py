import os
import re
import time
import pyotp
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

load_dotenv()

# Shoonya's login page shows a dismissible red banner reading
# "Invalid Input : <reason>" (e.g. "Wrong Password", "Invalid OTP") when the
# login itself is rejected — as opposed to a slow/flaky page load, which just
# never redirects. Retrying with the same credentials after this banner
# appears would only fail the same way again (and risks tripping the
# broker's own lockout on repeated bad attempts), so it's raised as a
# distinct, non-retried error.
_ERROR_BANNER_XPATH = "//*[contains(text(), 'Invalid Input')]"


class LoginRejectedError(RuntimeError):
    """Shoonya rejected the login credentials/OTP itself — not a transient failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Shoonya rejected login: {reason}")


def get_auth_code_with_credentials(credentials: dict) -> str:
    """
    Log in to Shoonya via headless Chrome using provided credentials dict.

    Args:
        credentials: Dict with keys: vendor_code, user_id, password, totp_secret

    Returns:
        OAuth auth code string

    Raises:
        ValueError: If required credentials are missing
        RuntimeError: If auth code cannot be obtained
    """
    api_key     = credentials.get("vendor_code")
    user_id     = credentials.get("user_id")
    password    = credentials.get("password")
    totp_secret = credentials.get("totp_secret")

    missing = [k for k, v in {
        "vendor_code": api_key,
        "user_id":  user_id,
        "password": password,
        "totp_secret":   totp_secret,
    }.items() if not v]
    if missing:
        raise ValueError(
            f"Missing required credentials for browser login: {', '.join(missing)}"
        )

    return _get_auth_code_impl(api_key, user_id, password, totp_secret)


def get_auth_code() -> str:
    """
    Log in to Shoonya via headless Chrome and return the OAuth auth code from the redirect URL.

    Reads credentials from env vars:
      SHONYA_VENDOR_CODE - used as api_key in the OAuth login URL
      SHONYA_USER_ID     - Shoonya user ID (e.g. FN131640)
      SHONYA_PASSWORD    - Shoonya account password
      SHONYA_TWO_FA      - TOTP secret for generating the OTP during login

    Raises:
        ValueError:   If any required env var is missing.
        RuntimeError: If the auth code cannot be obtained from the redirect URL.
    """
    api_key     = os.getenv("SHONYA_VENDOR_CODE")
    user_id     = os.getenv("SHONYA_USER_ID")
    password    = os.getenv("SHONYA_PASSWORD")
    totp_secret = os.getenv("SHONYA_TWO_FA")

    missing = [k for k, v in {
        "SHONYA_VENDOR_CODE": api_key,
        "SHONYA_USER_ID":  user_id,
        "SHONYA_PASSWORD": password,
        "SHONYA_TWO_FA":   totp_secret,
    }.items() if not v]
    if missing:
        raise ValueError(
            f"Missing required credentials for browser login: {', '.join(missing)}. "
            "Please set them in your .env file."
        )

    return get_auth_code_with_credentials({
        "vendor_code": api_key,
        "user_id": user_id,
        "password": password,
        "totp_secret": totp_secret,
    })


def _attempt_login(login_url: str, user_id: str, password: str, totp_secret: str) -> str:
    """Single headless-Chrome login attempt. Returns the OAuth code from the redirect URL."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ssl-version-max=tls1.2")  # Shoonya rejects TLS 1.3

    driver = webdriver.Chrome(options=options)
    wait   = WebDriverWait(driver, 30)

    try:
        driver.get(login_url)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
        time.sleep(1)

        all_inputs     = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
        visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]

        for element, value in zip(
            visible_inputs[:3],
            [user_id, password, pyotp.TOTP(totp_secret).now()],
        ):
            element.click()
            time.sleep(0.1)
            element.clear()
            element.send_keys(value)
            time.sleep(0.1)

        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))).click()

        start = time.time()
        while time.time() - start < 30:
            match = re.search(r"code=([a-zA-Z0-9\-]+)", driver.current_url)
            if match:
                return match.group(1)

            banners = [b for b in driver.find_elements(By.XPATH, _ERROR_BANNER_XPATH)
                       if b.is_displayed() and b.text.strip()]
            if banners:
                raise LoginRejectedError(banners[0].text.strip())

            time.sleep(0.5)

        raise RuntimeError(
            f"Timed out waiting for auth code redirect. Last URL: {driver.current_url}"
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _get_auth_code_impl(api_key: str, user_id: str, password: str, totp_secret: str,
                        attempts: int = 3) -> str:
    """OAuth auth code retrieval via Selenium, retrying a flaky headless login."""
    login_url = (
        f"https://trade.shoonya.com/OAuthlogin/investor-entry-level/login"
        f"?api_key={api_key}&route_to={user_id}"
    )

    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return _attempt_login(login_url, user_id, password, totp_secret)
        except LoginRejectedError as e:
            # Shoonya rejected the credentials/OTP itself — same input will
            # fail the same way every time, so don't burn the remaining
            # attempts (and don't risk tripping the broker's lockout).
            print(f"[auth] login rejected, not retrying: {e}")
            raise
        except (InvalidSessionIdException, WebDriverException, RuntimeError) as e:
            last_err = e
            print(f"[auth] attempt {attempt}/{attempts} failed: {str(e).splitlines()[0][:120]}")
            if attempt < attempts:
                time.sleep(3)

    raise RuntimeError(f"Browser error during auth code retrieval after "
                       f"{attempts} attempts: {last_err}")


if __name__ == "__main__":
    try:
        code = get_auth_code()
        print(f"Auth Code: {code}")
    except Exception as e:
        print(f"[ERROR] {e}")

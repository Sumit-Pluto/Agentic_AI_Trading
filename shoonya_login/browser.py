"""shoonya_login/browser.py — headless-Chrome capture of the Shoonya OAuth auth code.

Based on the official GetAuthcode helper from the Shoonya_oAuthAPI-py repo
(confirmed official by Finvasia API support), with two additions over the
naive version:

  * fail fast on a rejected password/TOTP (LoginRejectedError) instead of
    polling the full timeout and reporting a generic "timed out" message.
    Retrying a wrong password blindly risks tripping the broker's own
    account lockout — this stops the moment the broker's own error banner
    shows up, with its exact text.
  * a TOTP window-boundary guard: if the current 30s code is about to
    expire, wait for the next window before typing it, so it can't go
    stale between generation and form submission.

Requirements on the box:
    pip install selenium          # 4.6+ auto-manages chromedriver
    Google Chrome or Chromium installed
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import pyotp

from shoonya_login.errors import LoginRejectedError

log = logging.getLogger("shoonya.browser")

# Broker's own rejection banner (wrong password, wrong OTP, etc.) — watched
# alongside the redirect so a bad credential fails in seconds, not after the
# full timeout, and is never blindly retried.
_ERROR_BANNER_XPATH = ("//*[contains(text(), 'Invalid') or "
                       "contains(text(), 'incorrect') or "
                       "contains(text(), 'Incorrect')]")

# TLS 1.2 pin for the headless browser — OFF by default. Some Shoonya
# integrations (e.g. the Gateway reference project) pin this unconditionally
# because the site has reportedly rejected TLS 1.3 in the past; our current
# login works without it, so it's an opt-in safety valve rather than a
# default. Flip on with SHOONYA_CHROME_TLS12=1 if logins start failing with
# no clear reason.
_PIN_TLS12 = os.getenv("SHOONYA_CHROME_TLS12", "0") == "1"


def _fresh_totp(totp_secret: str, *, min_remaining: float = 3.0) -> str:
    """A TOTP code with at least `min_remaining` seconds left in its window,
    so it can't go stale between generation and form submission."""
    totp = pyotp.TOTP(totp_secret)
    remaining = totp.interval - (time.time() % totp.interval)
    if remaining < min_remaining:
        time.sleep(remaining + 0.1)
    return totp.now()


def find_code_in_url(url):
    """Extract ?code=... from a redirect URL.

    Shoonya's real redirect has been observed with a MALFORMED query string —
    a second '?' instead of '&' before code=, e.g.
    '...oauth?client_id=FA29913_U?code=b7cb93a6-...'. urllib.parse treats
    everything after the first '?' as one opaque query value in that case, so
    parse_qs never sees a 'code' key at all. Match it directly with a regex
    instead of relying on standards-compliant query parsing.
    """
    m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", url or "")
    return m.group(1) if m else None


def scan_for_auth_code(driver):
    """Look for the auth code in network traffic, current URL, or page body."""
    # 1. network requests (the official helper's method)
    try:
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                if message.get("method") == "Network.requestWillBeSent":
                    url = message.get("params", {}).get("request", {}).get("url", "")
                    if "code=" in url and "shoonya" in url.lower():
                        code = find_code_in_url(url)
                        if code:
                            return code
            except Exception:
                continue
    except Exception:
        pass
    # 2. address bar
    code = find_code_in_url(getattr(driver, "current_url", ""))
    if code:
        return code
    # 3. displayed on the page (the portal shows the code after login)
    try:
        m = re.search(r"code=([A-Za-z0-9_\-]{8,})", driver.page_source or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _scan_for_error(driver):
    """Return the broker's own rejection text if a visible error banner is
    showing (wrong password, wrong OTP, account locked, etc.)."""
    try:
        from selenium.webdriver.common.by import By
        banners = [b for b in driver.find_elements(By.XPATH, _ERROR_BANNER_XPATH)
                   if b.is_displayed() and b.text.strip()]
        if banners:
            return banners[0].text.strip()
    except Exception:
        pass
    return None


def fetch_auth_code(login_url, userid, password, totp_secret, timeout=90):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    if _PIN_TLS12:
        options.add_argument("--ssl-version-max=tls1.2")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)
    try:
        log.info("opening login page (headless)…")
        driver.get(login_url)
        wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[type='password']")))
        time.sleep(1)

        inputs = [el for el in driver.find_elements(
            By.CSS_SELECTOR,
            "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
            if el.is_displayed()]
        if len(inputs) < 3:
            raise RuntimeError(f"login page layout unexpected "
                               f"({len(inputs)} visible inputs)")

        def fill(el, value):
            el.click(); time.sleep(0.1)
            el.clear(); el.send_keys(value); time.sleep(0.1)

        otp = _fresh_totp(totp_secret)
        fill(inputs[0], userid)
        fill(inputs[1], password)
        fill(inputs[2], otp)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space()='LOGIN']"))).click()
        log.info("credentials submitted — waiting for auth code…")

        start = time.time()
        while time.time() - start < timeout:
            code = scan_for_auth_code(driver)
            if code:
                return code
            error = _scan_for_error(driver)
            if error:
                raise LoginRejectedError(error)
            # TOTP rolled over mid-login? try once with the fresh value.
            # Guarded: by the time 45s have passed, the page may already be
            # navigating (redirect in progress) and the cached `inputs`
            # references go stale — in that case just let the next loop
            # iteration re-scan for the code/error instead of crashing.
            fresh = pyotp.TOTP(totp_secret).now()
            if fresh != otp and time.time() - start > 45:
                try:
                    log.info("retrying with fresh TOTP…")
                    fill(inputs[2], fresh)
                    driver.find_element(
                        By.XPATH, "//button[normalize-space()='LOGIN']").click()
                    otp = fresh
                except Exception as e:
                    log.info("fresh-TOTP resubmit skipped (%s) — page likely "
                             "navigating; continuing to poll", e)
            time.sleep(0.5)
        raise RuntimeError("timed out waiting for the auth code — check "
                           "credentials, and that THIS machine's IP is the "
                           "one registered in the Shoonya portal")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

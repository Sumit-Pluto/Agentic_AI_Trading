"""shoonya_login/flow.py — the on-demand "make sure I'm logged in" flow, used
whenever a human runs the app (python main.py / python run_app.py).

For the unattended 8:45am cron path, see shoonya_login/__main__.py instead —
that path NEVER falls back to asking a human to paste a code.
"""
from __future__ import annotations

import logging
import os
import sys
import time

from shoonya_login.browser import fetch_auth_code
from shoonya_login.errors import LoginRejectedError, ShoonyaError

log = logging.getLogger("shoonya.flow")


def check_registered_ip():
    """Compare this machine's public IP with the one registered at Shoonya.

    Shoonya's API host is IPv4-only, so the IPv4 address is the one that
    must match the portal's Primary IP. Mobile-hotspot IPs can rotate —
    catching a mismatch here beats a confusing 'invalid IP' at login."""
    # Split-tunnel mode (WireGuard routes ONLY broker IPs through the VM):
    # a public-IP check would show the hotspot IP and false-alarm, so verify
    # the ROUTE instead — broker-bound traffic must use the utun interface.
    if (os.getenv("SHOONYA_TUNNEL_MODE") or "").lower() == "split":
        import socket
        import subprocess
        try:
            ip = socket.gethostbyname("api.shoonya.com")
            out = subprocess.run(["route", "-n", "get", ip],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
            iface = next((line.split(":", 1)[1].strip()
                          for line in out.splitlines()
                          if "interface" in line), "?")
            if iface.startswith("utun"):
                logging.info("split tunnel OK — broker traffic via %s "
                             "(api.shoonya.com=%s)", iface, ip)
            else:
                logging.warning(
                    "SPLIT TUNNEL NOT ACTIVE: broker traffic would leave via "
                    "%s — Shoonya will see your LOCAL IP, not the VM's! "
                    "Check WireGuard is Active and AllowedIPs includes %s/32.",
                    iface, ip)
        except Exception as e:
            logging.warning("split-tunnel route check failed: %s", e)
        return

    registered = (os.getenv("SHOONYA_REGISTERED_IP") or "").strip()
    if not registered:
        return
    try:
        import requests

        from shoonya_login.config import proxy_dict
        # go through the same proxy/tunnel as broker traffic, so this shows
        # the IP Shoonya will actually see
        current = requests.get("https://api.ipify.org", timeout=8,
                               proxies=proxy_dict()).text.strip()
    except Exception:
        return                      # no internet check ≠ fatal
    if current == registered:
        logging.info("public IP %s matches the registered Primary IP", current)
    else:
        logging.warning(
            "PUBLIC IP MISMATCH: this machine is now %s but Shoonya has %s "
            "registered — login will likely fail with 'invalid IP'. Either "
            "update the Primary IP in the portal (allowed once/week) or get "
            "back on the connection that had the old IP.",
            current, registered)


def _auto_login(session) -> bool:
    """Fully automated headless login (same mechanism as `python -m
    shoonya_login`) — the reason a human never has to paste a code by hand.
    Uses SHOONYA_PASSWORD + SHOONYA_TOTP_SECRET to drive a headless Chrome
    login and capture the auth code, then caches the day token. Returns True
    on success; on ANY problem returns False so the caller falls back to the
    manual paste step — logging *why* it failed (missing dependency, a
    rejected credential, or an environment/network problem) rather than
    swallowing it silently."""
    password = os.getenv("SHOONYA_PASSWORD")
    totp_secret = os.getenv("SHOONYA_TOTP_SECRET")
    if not (password and totp_secret):
        return False                       # no creds → manual step
    try:
        import selenium  # noqa: F401 — presence check; see the message below
    except ImportError:
        log.warning(
            "headless login unavailable: selenium is not installed in this "
            "Python environment (%s) — activate the project's venv "
            "(`source venv/bin/activate`) or install selenium. Falling back "
            "to manual paste.", sys.executable)
        return False
    try:
        logging.info("logging in to Shoonya headlessly (TOTP, no paste)…")
        code = fetch_auth_code(session.login_url(), session.userid,
                               password, totp_secret)
        session.login(auth_code=code)
        logging.info("headless login OK — day token cached in %s",
                     getattr(session, "token_cache", ".session_token"))
        return True
    except LoginRejectedError as e:
        log.error("Shoonya rejected the login credentials: %s — check "
                 "SHOONYA_PASSWORD and SHOONYA_TOTP_SECRET in .env. Falling "
                 "back to manual paste.", e.reason)
        return False
    except Exception as e:
        log.warning("headless login failed (%s) — falling back to manual "
                    "paste. If this box has Chrome + selenium and the right "
                    "IP, check your .env credentials.", e)
        return False


def daily_login(session):
    """Restore today's cached token; else log in AUTOMATICALLY (headless Chrome +
    TOTP, when SHOONYA_PASSWORD + SHOONYA_TOTP_SECRET are set) — no paste needed.
    Falls back to the manual browser step only when automated login isn't possible."""
    if session.restore_session():
        return
    if _auto_login(session):
        return
    print("\n─── Daily Shoonya login (new OAuth API) ───")
    print("1. Open this URL in a browser ON THE MACHINE WHOSE IP IS")
    print("   registered in the Shoonya portal (profile → API Key):\n")
    print("   " + session.login_url() + "\n")
    print("2. Log in with your user ID, password and TOTP.")
    print("3. After login you get an auth code (shown on page / as code= in")
    print("   the address bar). Copy it.")
    code = input("\nPaste the auth code (or the full URL) here: ").strip()
    while True:
        try:
            session.login(auth_code=code)
            return
        except ShoonyaError as e:
            if "giving up" in str(e):
                logging.warning("Shoonya server hiccup — retrying in 30s "
                                "(Ctrl-C to stop)")
                time.sleep(30)
                continue
            sys.exit(f"login failed: {e}\n"
                     "Hints: 'invalid ip' → you must log in from the machine "
                     "whose IP is registered in the portal; checksum/auth "
                     "errors → re-check SHOONYA_CLIENT_ID and "
                     "SHOONYA_SECRET_CODE in .env, and use a FRESH auth code "
                     "(they are single-use).")

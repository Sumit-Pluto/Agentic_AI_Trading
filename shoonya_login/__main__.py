#!/usr/bin/env python3
"""shoonya_login/__main__.py — unattended daily Shoonya OAuth login (headless Chrome).

Run this ON THE MACHINE WHOSE STATIC IP IS REGISTERED in the Shoonya portal
(profile → API Key → Primary IP). Shoonya rejects logins from any other IP
("invalid IP") — that's why this belongs on the VPS, in cron, before market
open (e.g. 08:45 IST):

    45 8 * * 1-5   cd /path/to/Trading_project_2.0 && venv/bin/python -m shoonya_login

(The `cd` matters: the cached token is written relative to the current
directory, and `-m` needs the repo root on sys.path.)

After it succeeds, `python run_app.py` (or the trading engine) starts
instantly with no prompts, reusing the cached day token.

Unlike shoonya_login.flow.daily_login (used when a human runs the app), this
NEVER falls back to asking someone to paste a code — nobody's there to
answer at 08:45. On a rejected password/TOTP it exits non-zero with the
broker's exact reason, so a monitoring setup (or the next person checking
the log) sees "wrong TOTP secret", not a silent 90-second timeout.

Requirements on the box:
    pip install selenium          # 4.6+ auto-manages chromedriver
    Google Chrome or Chromium installed
"""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from shoonya_login.browser import fetch_auth_code
from shoonya_login.credentials import MissingCredentials, load_credentials
from shoonya_login.errors import LoginRejectedError
from shoonya_login.session import ShoonyaAuth

log = logging.getLogger("shoonya-login-cron")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv()

    try:
        creds = load_credentials()
    except MissingCredentials as e:
        sys.exit(str(e))

    session = ShoonyaAuth(credentials=creds)
    if session.restore_session():
        log.info("today's session already cached — nothing to do")
        return

    if not (creds.password and creds.totp_secret):
        sys.exit("cron login needs SHOONYA_PASSWORD and SHOONYA_TOTP_SECRET "
                 "in .env")

    try:
        code = fetch_auth_code(session.login_url(), session.userid,
                               creds.password, creds.totp_secret)
    except LoginRejectedError as e:
        sys.exit(f"Shoonya rejected the login: {e.reason} — check "
                 "SHOONYA_PASSWORD / SHOONYA_TOTP_SECRET in .env")

    log.info("auth code captured — exchanging for access token…")
    session.login(auth_code=code)
    log.info("done: day token cached in %s", session.token_cache)


if __name__ == "__main__":
    main()

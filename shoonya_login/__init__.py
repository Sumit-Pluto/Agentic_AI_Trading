"""shoonya_login/ — everything needed to authenticate to Shoonya's 2026 OAuth API.

Covers both:
  * the on-demand flow used when a human runs the app (`daily_login`,
    `check_registered_ip`), and
  * the unattended flow used by the 8:45am cron job (`python -m shoonya_login`).

The trading REST client (order placement, quotes, the live-price websocket)
lives in shoonya_client.py, whose ShoonyaSession builds on ShoonyaAuth here.
"""
from __future__ import annotations

from shoonya_login.credentials import MissingCredentials, ShoonyaCredentials, load_credentials
from shoonya_login.errors import LoginRejectedError, ShoonyaError
from shoonya_login.flow import check_registered_ip, daily_login
from shoonya_login.session import ShoonyaAuth

__all__ = [
    "ShoonyaAuth", "ShoonyaError", "LoginRejectedError",
    "ShoonyaCredentials", "MissingCredentials", "load_credentials",
    "check_registered_ip", "daily_login",
]

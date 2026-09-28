"""Shoonya UserDetails API — fetches full user profile."""

import json
import os
import requests
from typing import Dict, Any

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "TradingGateway/1.0.0",
}


def get_user_details(user_id: str, auth_token: str, api_host: str | None = None) -> Dict[str, Any]:
    """
    POST to /NorenWClientAPI/UserDetails and return the raw response dict.

    Args:
        user_id: Broker user ID
        auth_token: Session token (susertoken)
        api_host: API base URL (e.g. https://api.shoonya.com/NorenWClientAPI).
                  If None, falls back to SHOONYA_API_HOST env var.

    Returns:
        Raw Shoonya response dict
    """
    if api_host is None:
        api_host = os.getenv("SHOONYA_API_HOST")

    base = api_host.rstrip("/")
    payload = {"uid": user_id}
    compact = json.dumps(payload, separators=(",", ":"))
    body = f"jData={compact}&jKey={auth_token}"

    resp = requests.post(
        f"{base}/UserDetails",
        data=body,
        headers=_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

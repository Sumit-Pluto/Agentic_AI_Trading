"""Shoonya API Logout Module."""

import json
import requests
from typing import Dict, Any

LOGOUT_ENDPOINT = "https://api.shoonya.com/NorenWClientTP/Logout"

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "TradingGateway/1.0.0",
}


def logout(user_id: str, auth_token: str, endpoint: str = LOGOUT_ENDPOINT) -> Dict[str, Any]:
    """
    Logout and invalidate the current session on the Shoonya server.

    Args:
        user_id: The authenticated user's UID.
        auth_token: The active session token (susertoken).
        endpoint: Logout URL (defaults to LOGOUT_ENDPOINT).

    Returns:
        Dict with keys: success (bool), message (str), response (dict).
    """
    payload = {"uid": user_id}
    body = f"jData={json.dumps(payload, separators=(',', ':'))}&jKey={auth_token}"

    try:
        response = requests.post(endpoint, data=body, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get("stat") == "Ok":
            return {
                "success": True,
                "message": "Logged out successfully",
                "response": data,
            }
        return {
            "success": False,
            "message": data.get("emsg", "Logout failed"),
            "response": data,
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "message": f"Logout request failed: {str(e)}",
            "response": {},
        }

"""
Shoonya API Authentication Module
Handles authentication to the Shoonya (Finvasia) Noren REST API via GenAcsTok.
"""

import tls_compat  # noqa: F401  # force TLS 1.2 (Shoonya rejects TLS 1.3)
import os
import json
import hashlib
import requests
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from brokers.shoonya.auth_code import get_auth_code
from brokers.shoonya import netproxy

load_dotenv()

SHOONYA_API_HOST = os.getenv("SHOONYA_API_HOST")

_TOKEN_STORE_PATH = Path(__file__).parent.parent.parent / "token_store.json"


def _load_token_store() -> dict:
    if _TOKEN_STORE_PATH.exists():
        try:
            return json.loads(_TOKEN_STORE_PATH.read_text()) or {}
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_token_store(data: dict) -> None:
    _TOKEN_STORE_PATH.write_text(json.dumps(data, indent=2))


def _issued_today(timestamp_str: Optional[str]) -> bool:
    if not timestamp_str:
        return False
    try:
        return datetime.fromisoformat(timestamp_str).date() == date.today()
    except (ValueError, TypeError):
        return False


class ShonyaAuthenticator:
    """Handles authentication with the Shoonya (Finvasia) Noren REST API."""

    def __init__(self):
        """Initialize authenticator with credentials from environment variables."""
        self.vendor_code = os.getenv("SHONYA_VENDOR_CODE")
        self.secret_code = os.getenv("SHONYA_API_SECRET")

        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.session: Optional[requests.Session] = None

        self._validate_credentials()

    def _validate_credentials(self) -> None:
        """Validate that all required credentials are present."""
        required = {
            "SHONYA_VENDOR_CODE": self.vendor_code,
            "SHONYA_API_SECRET": self.secret_code,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required Shoonya API credentials: {', '.join(missing)}. "
                "Please set them in your .env file."
            )

    @classmethod
    def from_session(cls, auth_token: str, user_id: str) -> "ShonyaAuthenticator":
        """Build a token-only authenticator from an already-obtained session.

        Used when the session was acquired via the broker adapter's
        `login(credentials)` (per-user stored credentials) rather than this
        class's own env-based `authenticate()`. Bypasses __init__ credential
        validation — we already hold a live token.
        """
        self = cls.__new__(cls)
        self.vendor_code = os.getenv("SHONYA_VENDOR_CODE")
        self.secret_code = os.getenv("SHONYA_API_SECRET")
        self.auth_token = auth_token
        self.user_id = user_id
        self.session = requests.Session()
        self.session.headers.update({
            "jKey": auth_token,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        return self

    @staticmethod
    def _sha256(value: str) -> str:
        """Return the SHA-256 hex digest of a string."""
        return hashlib.sha256(value.encode()).hexdigest()

    def authenticate(self, api_url: str = SHOONYA_API_HOST, force: bool = False) -> Dict[str, Any]:
        """
        Authenticate with the Shoonya Noren REST API via GenAcsTok.

        Obtains an OAuth auth code via browser login, then computes:
          checksum = sha256(vendor_code + secret_code + oauth_code)

        Args:
            api_url: Base URL of the Shoonya API (defaults to SHOONYA_API_HOST).
            force: Skip the same-day cached token/code and perform a genuine
                re-login. Used when a session was invalidated mid-day (e.g. you
                logged into the Shoonya app, which kills the API session) — the
                cached token is still "issued today" so it would otherwise be
                reused and every broker call would return empty.

        Returns:
            Dict with keys: success (bool), message (str), token (str|None),
            response (dict).
        """
        try:
            store = _load_token_store()

            if not force and store.get("susertoken") and _issued_today(store.get("susertoken_issued_at")):
                self.auth_token = store["susertoken"]
                self.user_id = store.get("user_id")
                self.session = requests.Session()
                self.session.headers.update({
                    "jKey": self.auth_token,
                    "Content-Type": "application/x-www-form-urlencoded",
                })
                return {
                    "success": True,
                    "message": "Using cached Shoonya session token",
                    "token": self.auth_token,
                    "response": {},
                }

            # `force` also ignores any cached OAuth code (a stored code from earlier
            # today is often already single-use-consumed / expired → AUTHCODE_EXPIRED).
            oauth_code: Optional[str] = None
            if not force and store.get("oauth_code") and _issued_today(store.get("oauth_code_issued_at")):
                oauth_code = store["oauth_code"]

            base_url = api_url.rstrip("/")
            auth_endpoint = f"{base_url}/GenAcsTok"
            last_emsg = "Unknown error from API"

            # Try up to twice: if GenAcsTok rejects a stale/expired code, discard it
            # and retry with a brand-new one from a fresh headless login.
            for attempt in (1, 2):
                if not oauth_code:
                    oauth_code = get_auth_code()
                    store["oauth_code"] = oauth_code
                    store["oauth_code_issued_at"] = datetime.now().isoformat()
                    _save_token_store(store)

                checksum = self._sha256(f"{self.vendor_code}{self.secret_code}{oauth_code}")
                payload = {"code": oauth_code, "checksum": checksum}
                self.session = requests.Session()
                post_data = f"jData={json.dumps(payload)}"
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                # Direct first; only if Shoonya refuses THIS host's IP does the
                # call go out again through the proxy. The OAuth code survives
                # the retry — the IP check rejects before consuming it.
                response, self.auth_path = netproxy.post(
                    self.session, auth_endpoint, "auth",
                    data=post_data, headers=headers, timeout=30, verify=True)
                response.raise_for_status()
                auth_response = response.json()

                if auth_response.get("stat") == "Ok":
                    self.auth_token = auth_response.get("susertoken")
                    self.user_id = auth_response.get("uid")
                    if self.auth_token:
                        self.session.headers.update({
                            "jKey": self.auth_token,
                            "Content-Type": "application/x-www-form-urlencoded",
                        })
                        store["susertoken"] = self.auth_token
                        store["susertoken_issued_at"] = datetime.now().isoformat()
                        store["user_id"] = self.user_id
                        _save_token_store(store)
                    return {
                        "success": True,
                        "message": "Successfully authenticated with Shoonya API",
                        "token": self.auth_token,
                        "response": auth_response,
                    }

                # failed — throw away the bad OAuth code
                last_emsg = auth_response.get("emsg", "Unknown error from API")
                store.pop("oauth_code", None)
                store.pop("oauth_code_issued_at", None)
                _save_token_store(store)
                # retry ONCE with a fresh code if the code was expired/invalid
                if attempt == 1 and any(k in last_emsg.upper() for k in ("EXPIRED", "AUTHCODE")):
                    oauth_code = None
                    continue
                break

            return {
                "success": False,
                "message": f"Authentication failed: {last_emsg}",
                "token": None,
                "response": {},
            }

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status >= 500:
                return {
                    "success": False,
                    "message": (
                        f"Shoonya GenAcsTok returned HTTP {status} — the server appears to be down. "
                        "Try again in a few minutes. "
                        f"Endpoint: {api_url.rstrip('/')}/GenAcsTok"
                    ),
                    "token": None,
                    "error": str(e),
                }
            return {
                "success": False,
                "message": f"Authentication failed with HTTP {status}: {str(e)}",
                "token": None,
                "error": str(e),
            }

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            return {
                "success": False,
                "message": (
                    f"Could not reach Shoonya API at {api_url}: {e}. "
                    "Check network connectivity and try again."
                ),
                "token": None,
                "error": str(e),
            }

        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "message": f"Request error during authentication: {str(e)}",
                "token": None,
                "error": str(e),
            }

    def get_session(self) -> requests.Session:
        """Return the authenticated session for making subsequent API requests."""
        if not self.auth_token or not self.session:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self.session

    def is_authenticated(self) -> bool:
        """Return True if a valid session token is held."""
        return self.auth_token is not None


def get_authenticated_session(api_url: str = SHOONYA_API_HOST) -> requests.Session:
    """
    Convenience function: authenticate and return a ready-to-use session.

    Args:
        api_url: Base URL of the Shoonya API.

    Returns:
        Authenticated requests.Session with jKey header set.

    Raises:
        ValueError: If any required credential is missing from the environment.
        RuntimeError: If authentication fails.
    """
    authenticator = ShonyaAuthenticator()
    result = authenticator.authenticate(api_url)

    if not result["success"]:
        raise RuntimeError(result["message"])

    return authenticator.get_session()

"""shoonya_login/credentials.py — load Shoonya credentials from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass

from shoonya_login.errors import ShoonyaError


class MissingCredentials(ShoonyaError):
    """Raised when a required SHOONYA_* env var is absent."""


@dataclass(frozen=True)
class ShoonyaCredentials:
    userid: str
    client_id: str
    secret_code: str
    password: str | None = None
    totp_secret: str | None = None


def load_credentials() -> ShoonyaCredentials:
    """Read SHOONYA_* from the environment (call load_dotenv() first).

    client_id/secret_code fall back to the legacy names SHOONYA_VENDOR_CODE /
    SHOONYA_API_KEY — this project's real .env uses those legacy names, so
    the fallback is load-bearing, not vestigial.
    """
    userid = os.getenv("SHOONYA_USERID")
    client_id = os.getenv("SHOONYA_CLIENT_ID") or os.getenv("SHOONYA_VENDOR_CODE")
    secret_code = os.getenv("SHOONYA_SECRET_CODE") or os.getenv("SHOONYA_API_KEY")
    missing = [name for name, val in [("SHOONYA_USERID", userid),
                                      ("SHOONYA_CLIENT_ID", client_id),
                                      ("SHOONYA_SECRET_CODE", secret_code)] if not val]
    if missing:
        raise MissingCredentials(
            f"Missing in .env: {', '.join(missing)} — see .env.example. "
            "(client id / secret code are on the Shoonya portal: "
            "profile → API Key)")
    return ShoonyaCredentials(
        userid=userid,
        client_id=client_id,
        secret_code=secret_code,
        password=os.getenv("SHOONYA_PASSWORD"),
        totp_secret=os.getenv("SHOONYA_TOTP_SECRET"),
    )

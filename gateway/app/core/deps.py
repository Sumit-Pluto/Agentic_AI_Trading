"""FastAPI dependency helpers."""
from fastapi import HTTPException

from app.core import auth
from brokers.shoonya.authenticator import ShonyaAuthenticator


def _require_legacy_auth() -> ShonyaAuthenticator:
    if auth._auth is None or not auth._auth.is_authenticated():
        raise HTTPException(status_code=401, detail="Not authenticated with Shoonya broker")
    return auth._auth

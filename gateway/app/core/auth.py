"""Legacy single-user Shoonya authenticator singleton.

Rule: always access as `auth._auth` — never `from app.core.auth import _auth`.
Reassigned at runtime by the legacy connect/disconnect endpoints.
"""
from typing import Optional

from brokers.shoonya.authenticator import ShonyaAuthenticator

_auth: Optional[ShonyaAuthenticator] = None

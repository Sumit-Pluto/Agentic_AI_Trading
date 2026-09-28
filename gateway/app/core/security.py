"""App-level user auth: password hashing (bcrypt) + JWT (PyJWT) + FastAPI deps.

Distinct from the broker-level session auth in `app/core/auth.py` — this layer
authenticates a human *user* of the app, not a broker connection.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from db.engine import get_db
from db.models import Service, User

_JWT_SECRET = os.environ.get("JWT_SECRET")
if not _JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET environment variable is not set. "
        "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))' "
        "and add it to your .env file."
    )

_JWT_ALGO = "HS256"
_TOKEN_TTL = timedelta(days=7)          # human user tokens
_SERVICE_TOKEN_TTL = timedelta(hours=1)  # machine/service tokens (auto-refreshed by the engine)

# The full set of scopes the Gateway understands. A service is granted a subset
# (stored on its `Service.scopes` row); a human user implicitly holds all of them.
ALL_SCOPES = ("orders", "positions", "funds", "market", "profile", "status")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return a bcrypt hash (utf-8 string) for storage."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(user_id: int) -> str:
    """Sign a *user* JWT whose subject is the user id, expiring after _TOKEN_TTL."""
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "typ": "user", "iat": now, "exp": now + _TOKEN_TTL}
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)


def create_service_token(client_id: str, scopes: list[str]) -> str:
    """Sign a *service* JWT for a machine identity, carrying its granted scopes."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": client_id,
        "typ": "service",
        "scopes": list(scopes),
        "iat": now,
        "exp": now + _SERVICE_TOKEN_TTL,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)


def decode_token(token: str) -> int:
    """Decode a *user* JWT and return the user id. Raises jwt exceptions on failure."""
    payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])
    return int(payload["sub"])


def decode_token_payload(token: str) -> dict:
    """Decode any JWT (user or service) and return its full claims. Raises on failure."""
    return jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return authorization.split(" ", 1)[1].strip()


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated User from the Bearer token, or raise 401."""
    token = _extract_bearer(authorization)
    try:
        user_id = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(User).filter_by(id=user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def require_form_filled(user: User = Depends(get_current_user)) -> User:
    """Like get_current_user, but 403s until the user has saved broker credentials."""
    if not user.form_filled:
        raise HTTPException(status_code=403, detail="Broker credentials not configured")
    return user


def user_from_token(token: str, db: Session) -> Optional[User]:
    """Non-raising resolver for contexts without HTTP headers (e.g. WebSocket
    query param). Returns the User or None."""
    try:
        user_id = decode_token(token)
    except jwt.InvalidTokenError:
        return None
    return db.query(User).filter_by(id=user_id).first()


# ---------------------------------------------------------------------------
# Principals (human user OR machine service) + scope-based authorization
# ---------------------------------------------------------------------------

@dataclass
class Principal:
    """The authenticated caller of a request: either a human `user` or a machine
    `service`. `require_scope` authorizes based on `kind` + `scopes`."""
    kind: str                              # "user" | "service"
    user: Optional[User] = None            # set when kind == "user"
    client_id: Optional[str] = None        # set when kind == "service"
    scopes: list = field(default_factory=list)  # granted scopes when kind == "service"


def _principal_from_payload(payload: dict, db: Session) -> Optional[Principal]:
    typ = payload.get("typ", "user")
    if typ == "service":
        return Principal(
            kind="service",
            client_id=str(payload.get("sub")),
            scopes=list(payload.get("scopes") or []),
        )
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if user is None:
        return None
    return Principal(kind="user", user=user)


def get_principal(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    """Resolve the caller (user or service) from the Bearer token, or raise 401."""
    token = _extract_bearer(authorization)
    try:
        payload = decode_token_payload(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    principal = _principal_from_payload(payload, db)
    if principal is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return principal


def require_scope(scope: str, user_needs_form: bool = True):
    """Dependency factory: allow a human user (full access via the UI) OR a service
    whose token carries `scope`. Raises 403 otherwise.

    `user_needs_form` gates the human branch on `form_filled` (True for trading/data
    routes; False for status/profile, which only need a logged-in user as before)."""
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.kind == "user":
            if user_needs_form and not principal.user.form_filled:
                raise HTTPException(status_code=403, detail="Broker credentials not configured")
            return principal
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail=f"Service lacks required scope '{scope}'")
        return principal
    return dependency


def principal_from_token(token: str, db: Session) -> Optional[Principal]:
    """Non-raising principal resolver for the WebSocket query-param path."""
    try:
        payload = decode_token_payload(token)
    except jwt.InvalidTokenError:
        return None
    return _principal_from_payload(payload, db)

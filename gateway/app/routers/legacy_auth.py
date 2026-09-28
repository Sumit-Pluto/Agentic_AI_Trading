import asyncio
import time as _time

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import auth as _auth_module
from app.core.deps import _require_legacy_auth
from app.core.security import Principal, get_current_user, require_form_filled, require_scope
from app.services.targets import _load_target_positions
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.authenticator import ShonyaAuthenticator
from brokers.shoonya.logout import logout as shoonya_logout
from brokers.shoonya.user_details import get_user_details
from crypto import decrypt_json
from db.engine import get_db
from db.models import Account, User
from ticker_manager import ticker_manager

router = APIRouter()


async def _reauth_shoonya(user: User, db: Session) -> bool:
    """Re-login the connected Shoonya session using the user's stored credentials.

    Called when a broker call rejects the in-memory session token (expired
    overnight or invalidated by an app login). Invalidates the stale cached
    token, performs a fresh OAuth login, and swaps the `_auth` singleton for the
    new session. Returns True if a fresh token was obtained.
    """
    account = db.query(Account).filter_by(user_id=user.id).first()
    if account is None or account.broker.name != "shoonya":
        return False
    try:
        credentials = decrypt_json(account.credentials_enc)
        broker = get_broker(account.broker.name)
    except Exception:
        return False

    stale = _auth_module._auth
    stub = SessionToken(
        token=stale.auth_token if stale else "",
        broker_uid=(stale.user_id if stale else credentials.get("user_id")),
        issued_at="",
        broker_name="shoonya",
    )
    try:
        fresh = await broker.refreshSession(stub, credentials)
    except Exception:
        return False

    _auth_module._auth = ShonyaAuthenticator.from_session(fresh.token, fresh.broker_uid)
    return True


def _is_session_error(emsg: str) -> bool:
    """True if a Shoonya `stat != Ok` message looks like an expired/invalid session."""
    m = (emsg or "").lower()
    return any(k in m for k in ("session", "expired", "invalid token", "unauthor"))


@router.post("/api/connect")
async def connect_legacy(
    user: User = Depends(require_form_filled),
    db: Session = Depends(get_db),
):
    """Connect to the broker using the logged-in user's OWN stored credentials."""
    account = db.query(Account).filter_by(user_id=user.id).first()
    if account is None:
        raise HTTPException(status_code=400, detail="No broker configured for this user")

    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Credential decryption failed: {e}")

    broker_name = account.broker.name
    try:
        broker = get_broker(broker_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker_name}")

    try:
        token = await broker.login(credentials)
    except BrokerError as e:
        raise HTTPException(status_code=401, detail=f"Broker login failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Broker login error: {e}")

    if broker_name == "shoonya":
        # Populate the singleton the whole live engine expects, using the token
        # we just obtained from the user's stored credentials.
        _auth_module._auth = ShonyaAuthenticator.from_session(token.token, token.broker_uid)
        if not ticker_manager.is_running:
            ticker_manager.start(broker, token.token, token.broker_uid, asyncio.get_running_loop())
        asyncio.create_task(_load_target_positions())
        return {"connected": True, "method": "rest", "broker": broker_name}

    # Sharekhan authenticates, but the live ticker/auto-exit engine is Shoonya-only
    # today (Phase 2 wires Sharekhan into the live dashboard).
    return {
        "connected": True,
        "method": "rest",
        "broker": broker_name,
        "message": "Connected to Sharekhan. Live ticker/auto-exit dashboard is Shoonya-only for now.",
    }


@router.post("/api/disconnect")
def disconnect_legacy(user: User = Depends(get_current_user)):
    # Stop the live feed first, flagged intentional so the ticker's auto-reconnect
    # doesn't resurrect the socket we're about to log out from.
    ticker_manager.stop()
    if _auth_module._auth is not None and _auth_module._auth.is_authenticated():
        try:
            shoonya_logout(_auth_module._auth.user_id, _auth_module._auth.auth_token)
        except Exception:
            pass
    _auth_module._auth = None
    return {"connected": False}


# Holding a token is not the same as the broker still honouring it: Shoonya
# invalidates a session overnight, or the moment the user logs into its app, and
# then answers every call with an empty 200. This flag is the engine's entire
# trading gate, so "connected" has to mean the broker actually answered us.
# Probed at most this often, because the engine polls status every 15 seconds.
_LIVENESS_TTL = 45.0
_liveness = {"at": 0.0, "ok": False}


async def _session_answers(user_id: str, auth_token: str) -> bool:
    """Cheap, cached proof that the broker still accepts our session."""
    now = _time.monotonic()
    if now - _liveness["at"] < _LIVENESS_TTL:
        return _liveness["ok"]
    ok = False
    try:
        data = await asyncio.to_thread(get_user_details, user_id, auth_token)
        ok = isinstance(data, dict) and data.get("stat") == "Ok"
    except Exception:
        ok = False
    _liveness.update(at=now, ok=ok)
    return ok


@router.get("/api/status")
async def status_legacy(_=Depends(require_scope("status", user_needs_form=False))):
    auth = _auth_module._auth
    connected = auth is not None and auth.is_authenticated()
    if connected:
        # A held token that the broker no longer accepts is worse than no token:
        # it keeps the engine trading against reads that come back empty.
        connected = await _session_answers(auth.user_id, auth.auth_token)
    resp = {"connected": connected, "method": "rest" if connected else "none"}
    try:
        from brokers.shoonya import netproxy
        resp["egress"] = netproxy.status()
    except Exception:
        pass
    return resp


@router.get("/api/user")
async def user_details_legacy(
    principal: Principal = Depends(require_scope("profile", user_needs_form=False)),
    db: Session = Depends(get_db),
):
    # Try once with the current session; on an expired/invalid token, transparently
    # re-authenticate using the user's stored credentials and retry once. Only a
    # human caller can be re-authed (we need their stored broker credentials).
    can_reauth = principal.kind == "user"
    data: dict = {}
    for attempt in (1, 2):
        auth = _require_legacy_auth()
        try:
            data = await asyncio.to_thread(get_user_details, auth.user_id, auth.auth_token)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (401, 403) and attempt == 1 and can_reauth and await _reauth_shoonya(principal.user, db):
                continue
            raise HTTPException(status_code=502, detail=f"Shoonya API error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Shoonya API error: {e}")

        if data.get("stat") != "Ok":
            emsg = data.get("emsg", "Failed to fetch user details")
            if attempt == 1 and can_reauth and _is_session_error(emsg) and await _reauth_shoonya(principal.user, db):
                continue
            raise HTTPException(status_code=400, detail=emsg)
        break

    return {
        "uid": data.get("uid", ""),
        "actid": data.get("actid", ""),
        "email": data.get("email", ""),
        "m_num": data.get("m_num", ""),
        "brkname": data.get("brkname", ""),
        "brnchid": data.get("brnchid", ""),
        "uprev": data.get("uprev", ""),
        "exarr": data.get("exarr", []),
        "orarr": data.get("orarr", []),
        "prarr": data.get("prarr", []),
        "request_time": data.get("request_time", ""),
    }

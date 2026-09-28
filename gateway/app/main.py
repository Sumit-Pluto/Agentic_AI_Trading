"""FastAPI server for multi-broker, multi-account trading gateway."""
import tls_compat  # noqa: F401  # force TLS 1.2 (Shoonya rejects TLS 1.3) — must import first
import logging
import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.background import startup as background_startup
from app.core import state
from app.core.security import require_form_filled, require_scope
from app.services.market_hours import load_market_hours_config
from app.routers import (
    accounts,
    credentials,
    funds,
    health,
    legacy_auth,
    lots,
    market,
    market_hours,
    order_margin,
    orders,
    persistent_orders,
    positions,
    service_auth,
    targets,
    user_auth,
    watchlist,
    ws,
)
from db.engine import get_db, init_db
from db.models import ExchangeTarget

logger = logging.getLogger(__name__)

app = FastAPI(title="Gateway API")

_ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    init_db()
    db = next(get_db())
    try:
        state._exchange_targets.clear()
        for cfg in db.query(ExchangeTarget).all():
            state._exchange_targets[cfg.exch] = {
                "enabled": cfg.enabled,
                "target_value": cfg.target_value,
            }
    finally:
        db.close()
    # Load per-exchange market-hours overrides into session_windows before the
    # tick callbacks (auto-exit gate) start firing.
    load_market_hours_config()
    background_startup.register_ticker_callbacks()
    background_startup.launch_tasks()
    await _restore_sessions_if_enabled()


async def _restore_sessions_if_enabled() -> None:
    """Re-establish the broker session from today's cached token on boot.

    OPT-IN: does nothing unless GATEWAY_AUTO_RECONNECT=1, so default behaviour
    is byte-for-byte unchanged.

    Why this is free rather than a re-login: the Shoonya session token is
    already cached per user per day (brokers/shoonya/_token_cache), and the
    legacy auth singleton is built from exactly that token via
    ShonyaAuthenticator.from_session(). A restart during the trading day
    therefore only loses an in-memory object, not the session itself — but
    every downstream service (market data, option chains, this gateway's own
    UI) is dead until a human notices and clicks connect.
    """
    import os
    if os.getenv("GATEWAY_AUTO_RECONNECT", "0").lower() not in ("1", "true", "yes"):
        return
    try:
        from app.core import auth as _auth_module
        from brokers.shoonya import _token_cache
        from brokers.shoonya.authenticator import ShonyaAuthenticator
    except Exception as e:                                   # pragma: no cover
        print(f"[startup] auto-reconnect unavailable: {e}", flush=True)
        return

    if getattr(_auth_module, "_auth", None) is not None:
        return                                    # already live; leave it alone
    try:
        store = _token_cache.load()
    except Exception as e:
        print(f"[startup] auto-reconnect: token store unreadable: {e}", flush=True)
        return
    if not store:
        print("[startup] auto-reconnect: no cached sessions to restore", flush=True)
        return

    for uid in store:
        entry = _token_cache.get_cached_session(uid)     # None unless issued TODAY
        if not entry:
            print(f"[startup] auto-reconnect: cached token for {uid} is not "
                  f"from today — manual connect required", flush=True)
            continue
        try:
            _auth_module._auth = ShonyaAuthenticator.from_session(
                entry["susertoken"], uid)
            ok = _auth_module._auth.is_authenticated()
            if not ok:
                _auth_module._auth = None
                continue

            # The auth singleton alone is not a working gateway: the live feed
            # is a separate object, and without it TickerManager._broker stays
            # None and every market-data subscription fails. /api/connect does
            # both; so must this.
            try:
                import asyncio as _a
                from brokers.registry import get_broker
                from ticker_manager import ticker_manager as _tm
                if not _tm.is_running:
                    _tm.start(get_broker("shoonya"), entry["susertoken"], uid,
                              _a.get_running_loop())
                    print(f"[startup] auto-reconnect: live ticker started",
                          flush=True)
            except Exception as e:
                print(f"[startup] auto-reconnect: session restored but ticker "
                      f"failed to start: {type(e).__name__}: {e}", flush=True)

            # Deliberately NOT calling _load_target_positions(): restoring
            # MARKET DATA after a restart is safe, re-arming trading logic
            # without a human present is not.
            print(f"[startup] auto-reconnect: restored session for {uid} "
                  f"(authenticated=True)", flush=True)
            return
        except Exception as e:
            _auth_module._auth = None
            print(f"[startup] auto-reconnect failed for {uid}: "
                  f"{type(e).__name__}: {e}", flush=True)


# Open routers: user auth (signup/signin), service token grant, and health need no token.
app.include_router(user_auth.router)
app.include_router(service_auth.router)
app.include_router(health.router)

# legacy_auth guards its own endpoints per-route (connect requires form_filled;
# disconnect/status/user require a logged-in user).
app.include_router(legacy_auth.router)

# credentials form requires a logged-in user (guarded inside the router).
app.include_router(credentials.router)

# Trading/data routers require either a form-filled human user (full UI access) or
# a service whose token carries the router's scope. `require_scope` layers
# get_principal + the user form-gate / service scope-check.
app.include_router(accounts.router, dependencies=[Depends(require_scope("funds"))])
app.include_router(funds.router, dependencies=[Depends(require_scope("funds"))])
app.include_router(positions.router, dependencies=[Depends(require_scope("positions"))])
app.include_router(orders.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(order_margin.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(lots.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(persistent_orders.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(market.router, dependencies=[Depends(require_scope("market"))])
app.include_router(market_hours.router, dependencies=[Depends(require_scope("market"))])
# targets/watchlist are UI-only config — humans only (no service uses them).
_user_only = [Depends(require_form_filled)]
app.include_router(targets.router, dependencies=_user_only)
app.include_router(watchlist.router, dependencies=_user_only)

# WebSocket ticker authenticates via ?token= query param (see ws.py).
app.include_router(ws.router)
# Diagnostic probes: same "market" scope as every other market-data route.
app.include_router(ws.probe_router, dependencies=[Depends(require_scope("market"))])

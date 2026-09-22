"""shoonya_login/config.py — endpoints and shared proxy/pool settings.

Env-derived values are read lazily (as functions, never module-level
constants) because every entry point calls load_dotenv() AFTER this module
is first imported — freezing a value at import time would silently miss
whatever is in .env. ShoonyaFeed also re-reads the proxy on every reconnect,
so a tunnel brought up later is picked up rather than baked in once.
"""
from __future__ import annotations

import os
import urllib.parse

REST_HOST = "https://api.shoonya.com/NorenWClientAPI"
OAUTH_LOGIN_URL = "https://api.shoonya.com/OAuthlogin/investor-entry-level/login"

# Relative to CWD, matching the original behavior exactly — NOT pinned to the
# repo root (deliberate choice; see the plan/README for the cron deployment
# note this implies: the cron job must `cd` into the repo before running).
DEFAULT_TOKEN_CACHE = ".session_token"


def proxy_url() -> str | None:
    """SHOONYA_PROXY, e.g. socks5h://127.0.0.1:1080 — routes broker traffic
    through a static-IP tunnel so the IP whitelist matches even when this
    machine's own IP is dynamic. None if unset."""
    v = os.getenv("SHOONYA_PROXY", "").strip()
    return v or None


def proxy_dict() -> dict | None:
    """requests-style {"http": ..., "https": ...} proxy mapping, or None."""
    p = proxy_url()
    return {"http": p, "https": p} if p else None


def ws_proxy_kwargs() -> dict:
    """kwargs for websocket-client's run_forever(); {} if no proxy is set."""
    p = proxy_url()
    if not p:
        return {}
    u = urllib.parse.urlparse(p)
    kwargs = dict(proxy_type=u.scheme or "socks5h",
                  http_proxy_host=u.hostname,
                  http_proxy_port=u.port or 1080)
    if u.username:
        kwargs["http_proxy_auth"] = (u.username, u.password)
    return kwargs


def pool_size() -> int:
    return int(os.getenv("SHOONYA_POOL_SIZE", "48"))

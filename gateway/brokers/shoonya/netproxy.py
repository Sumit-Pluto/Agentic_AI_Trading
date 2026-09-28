"""Fallback egress for Shoonya calls when this host's IP is not the whitelisted one.

Shoonya binds API access to a registered static IP. When the line's public IP
changes — a failover, a lease renewal, a VPN drop — Shoonya answers
`Invalid Input : INVALID_IP` and nothing works. Worse, it stays invisible: the
gateway reuses a session token for the whole calendar day, so it keeps reporting
"connected" until something forces a genuine re-login, which may not be until
09:15 the next morning.

The policy here is deliberately least-effort:

    1. a cached session token, if one is still valid   (no network login at all)
    2. this host's own IP, directly                    (normal, and free)
    3. the proxy                                       (only when 1 and 2 fail)

The proxy is a fallback, never the default. It also carries only the calls that
are actually IP-bound:

  * AUTH   — proven: GenAcsTok is refused from a non-whitelisted IP, while the
             browser login at trade.shoonya.com is not checked at all, so
             Selenium never needs the proxy.
  * ORDERS — SEBI's retail algo circular requires order placement to originate
             from the registered static IP. That is the whole point of the
             mandate: an audit trail tying each order to a known address.
  * DATA   — quotes and the websocket are ~19,000 requests a day and are not
             IP-restricted. Sending those through the proxy would multiply its
             load by a factor of several hundred for no benefit, so by default
             they stay direct.

Auth plus orders is on the order of fifty requests a day. That is the entire
cost of staying compliant while the heavy traffic stays off the proxy.

SHOONYA_PROXY may list several proxies, comma-separated. They are tried in order
until one is accepted, so losing a single proxy box — down, unreachable, or its
own IP dropped off Shoonya's whitelist — no longer breaks order flow. Each proxy
must itself sit on an IP registered with Shoonya; you gain redundancy only for as
many whitelisted IPs as you actually have.
"""
from __future__ import annotations

import logging
import os
import threading
import time

_log = logging.getLogger(__name__)

# ---- configuration --------------------------------------------------------
# SHOONYA_PROXY        http://user:pass@host:port   (empty = feature off)
#     Several proxies may be listed comma-separated; they are tried in order
#     until one is accepted:  http://u:p@host1:port,http://u:p@host2:port
# SHOONYA_PROXY_MODE   off | auto | on
#     off  — never. auto — direct first, proxy only after an IP rejection.
#     on   — always use the proxy for whatever is in scope.
# SHOONYA_PROXY_SCOPE  auth | auth+orders | all
_STICKY_SECONDS = 900.0     # remember an IP rejection this long, then re-probe


def _urls() -> list[str]:
    """The configured proxies, in try order. Empty list = feature off."""
    raw = (os.getenv("SHOONYA_PROXY") or "").strip()
    return [u.strip() for u in raw.split(",") if u.strip()]


def _mode() -> str:
    return (os.getenv("SHOONYA_PROXY_MODE") or "auto").strip().lower()


def _scope() -> str:
    return (os.getenv("SHOONYA_PROXY_SCOPE") or "auth+orders").strip().lower()


def _mask(url: str) -> str:
    """Hide user:pass@ so credentials never reach logs or the status endpoint."""
    if not url or "@" not in url:
        return url
    scheme, sep, rest = url.partition("://")
    host = rest.split("@", 1)[1]
    return f"{scheme}{sep}***@{host}" if sep else f"***@{host}"


def configured() -> bool:
    return bool(_urls()) and _mode() != "off"


def in_scope(kind: str) -> bool:
    """kind: 'auth' | 'order' | 'data'."""
    scope = _scope()
    if scope == "all":
        return True
    if scope == "auth":
        return kind == "auth"
    return kind in ("auth", "order")        # auth+orders, the default


def proxies_for(url: str) -> dict:
    """A requests-style proxies mapping for one proxy URL."""
    return {"http": url, "https": url}


# ---- IP-rejection detection ----------------------------------------------
# Shoonya's wording. Kept broad because the same condition is reported
# differently across Noren deployments, and a missed match here means the
# fallback never fires — the failure mode we are trying to remove.
_IP_MARKERS = ("INVALID_IP", "INVALID IP", "IP NOT", "NOT REGISTERED",
               "IP_NOT_ALLOWED", "UNAUTHORIZED IP")


def is_ip_rejection(message: str | None) -> bool:
    if not message:
        return False
    upper = str(message).upper()
    return any(m in upper for m in _IP_MARKERS)


# ---- sticky state ---------------------------------------------------------
# After an IP rejection, stop paying for a doomed direct attempt on every
# subsequent call — go straight to the proxy for a while. Re-probing after
# _STICKY_SECONDS means the system returns to direct on its own once the line
# is fixed, instead of quietly living on the proxy forever.
_lock = threading.Lock()
_ip_failed_at: float = 0.0
_last_path: str = "direct"
_last_good_proxy: str | None = None     # the proxy that last carried a call


def note_ip_rejection() -> None:
    global _ip_failed_at
    with _lock:
        _ip_failed_at = time.time()
    _log.warning("shoonya: IP rejected by broker — routing IP-bound calls via proxy "
                 "for the next %.0fs", _STICKY_SECONDS)


def note_direct_success() -> None:
    """Direct worked — drop the sticky flag so we stay off the proxy."""
    global _ip_failed_at, _last_path
    with _lock:
        _ip_failed_at = 0.0
        _last_path = "direct"


def note_proxy_success(url: str) -> None:
    """A proxy carried the call — remember it so the next fallback starts here."""
    global _last_path, _last_good_proxy
    with _lock:
        _last_path = "proxy"
        _last_good_proxy = url


def _ordered_proxies() -> list[str]:
    """Configured proxies, with the last-known-good one moved to the front.

    A dead proxy at position 0 would otherwise cost a timeout on every fallback;
    starting from whatever last worked keeps that cost off the hot path.
    """
    urls = _urls()
    with _lock:
        last = _last_good_proxy
    if last and last in urls:
        return [last] + [u for u in urls if u != last]
    return urls


def prefer_proxy(kind: str) -> bool:
    """Should this call skip the direct attempt and go straight out the proxy?"""
    if not configured() or not in_scope(kind):
        return False
    if _mode() == "on":
        return True
    with _lock:
        return _ip_failed_at > 0 and (time.time() - _ip_failed_at) < _STICKY_SECONDS


def status() -> dict:
    """For the gateway's status endpoint — which path is live, and why.

    Proxy URLs are masked: credentials never leave through this endpoint.
    """
    with _lock:
        failed_at, last, last_good = _ip_failed_at, _last_path, _last_good_proxy
    urls = _urls()
    return {
        "proxy_configured": bool(urls),
        "proxy_count": len(urls),
        "proxies": [_mask(u) for u in urls],
        "proxy_mode": _mode(),
        "proxy_scope": _scope(),
        "last_path": last,
        "last_good_proxy": _mask(last_good) if last_good else None,
        "ip_rejected_recently": bool(failed_at and (time.time() - failed_at) < _STICKY_SECONDS),
        "ip_rejected_seconds_ago": round(time.time() - failed_at, 1) if failed_at else None,
    }


def post(session_or_requests, url: str, kind: str, **kwargs):
    """POST that falls back to the proxy on an IP rejection.

    Returns (response, path) where path is "direct" or "proxy". The caller still
    inspects the body — Noren answers HTTP 200 with stat=Not_Ok, so a rejection
    is not visible at the transport layer and cannot be detected here.
    """
    use_proxy_first = prefer_proxy(kind)

    # last_resp: the best response we can hand back if every proxy fails. In auto
    # mode this is the direct (IP-rejected) response, so the caller still sees the
    # broker's emsg. In "on" mode there is no direct attempt, so it stays None and
    # a total proxy failure re-raises — matching how a single dead proxy behaved.
    last_resp = None
    last_exc = None

    if not use_proxy_first:
        resp = session_or_requests.post(url, **kwargs)
        if not (configured() and in_scope(kind)):
            return resp, "direct"
        try:
            body = resp.json()
        except Exception:
            return resp, "direct"
        if not is_ip_rejection(body.get("emsg")):
            note_direct_success()
            return resp, "direct"
        note_ip_rejection()
        last_resp = resp
        _log.warning("shoonya: %s rejected on IP from this host — retrying via proxy", kind)

    # Fall back through each configured proxy until one is accepted. Two failures
    # both advance to the next proxy: the box is unreachable (exception), or the
    # broker rejects that proxy's own IP (emsg). A non-JSON body on a live proxy
    # is treated as success — the transport worked and there's no rejection to see.
    for proxy_url in _ordered_proxies():
        try:
            resp = session_or_requests.post(url, proxies=proxies_for(proxy_url), **kwargs)
        except Exception as e:
            last_exc = e
            _log.warning("shoonya: proxy %s unreachable (%s) — trying next", _mask(proxy_url), e)
            continue
        last_resp = resp
        try:
            body = resp.json()
        except Exception:
            note_proxy_success(proxy_url)
            return resp, "proxy"
        if not is_ip_rejection(body.get("emsg")):
            note_proxy_success(proxy_url)
            return resp, "proxy"
        _log.warning("shoonya: proxy %s rejected on IP by broker — trying next", _mask(proxy_url))

    if last_resp is None and last_exc is not None:
        raise last_exc
    return last_resp, "proxy"

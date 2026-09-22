"""shoonya_login/session.py — ShoonyaAuth: token lifecycle + authenticated HTTP transport.

Auth flow (once per trading day):
  1. Open ShoonyaAuth.login_url() in a browser and sign in with your user ID +
     password + TOTP (or let shoonya_login.browser capture this automatically).
  2. After login, the browser's address bar contains ?code=<auth_code>.
  3. ShoonyaAuth.login(auth_code=...) exchanges it at /GenAcsTok with
     checksum = sha256(client_id + secret_code + auth_code) and receives a
     Bearer access token, cached on disk for the rest of the day. Restarts
     reuse the cache via restore_session() — no browser needed again.

shoonya_client.ShoonyaSession subclasses ShoonyaAuth to add the trading REST
methods (orders, books, quotes) on top of the _post()/_headers() transport
defined here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import date

import requests

from shoonya_login.config import DEFAULT_TOKEN_CACHE, OAUTH_LOGIN_URL, REST_HOST, pool_size, proxy_dict, proxy_url
from shoonya_login.credentials import ShoonyaCredentials
from shoonya_login.errors import ShoonyaError

log = logging.getLogger("shoonya.auth")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ShoonyaAuth:
    """Credentials, token lifecycle, and the authenticated HTTP transport.

    credentials:
      userid       your client code, e.g. FA329598
      client_id    the API app's client id (shown on the API page,
                   usually <userid>_U)
      secret_code  the API app's secret key
      password / totp_secret are only needed for automated browser login —
      the manual flow just needs the pasted auth code.

    Subclasses (e.g. shoonya_client.ShoonyaSession) add trading REST methods
    on top of self._post()/self._headers(). Do NOT define __init__ in a
    subclass without calling super().__init__() — the pooled+proxied
    requests.Session and the token cache path are set up here.
    """

    def __init__(self, userid=None, client_id=None, secret_code=None,
                 password=None, totp_secret=None,
                 credentials: ShoonyaCredentials | None = None,
                 token_cache=DEFAULT_TOKEN_CACHE):
        if credentials is not None:
            userid = credentials.userid
            client_id = credentials.client_id
            secret_code = credentials.secret_code
            password = credentials.password
            totp_secret = credentials.totp_secret
        self.userid = userid
        self.client_id = client_id
        self.secret_code = secret_code
        self.password = password
        self.totp_secret = (totp_secret or "").replace(" ", "").upper()
        self.token_cache = token_cache
        self.access_token = None
        self.susertoken = None
        self.actid = userid
        # keep-alive session: connection + TLS reused across all calls.
        # urllib3's default pool keeps only 10 connections per host, but our
        # concurrency to api.shoonya.com is NESTED and multiplies: the OI
        # scanner runs OI_WORKERS (8) symbols in parallel, and each symbol's
        # chain_snapshot fans out again over CHAIN_FETCH_WORKERS (6) to fetch
        # its option legs -> up to 8x6 = 48 in-flight requests at peak (plus
        # the swing scanner). With pool_maxsize=10 the surplus connections
        # can't be pooled, so urllib3 discards them ("Connection pool is full")
        # and re-does the TLS handshake next round. Sizing the pool to cover
        # the peak doesn't raise the concurrent-connection count (that's set by
        # the thread pools) — it just lets those connections be REUSED.
        self.http = requests.Session()
        _pool = pool_size()
        _adapter = requests.adapters.HTTPAdapter(
            pool_connections=_pool, pool_maxsize=_pool)
        self.http.mount("https://", _adapter)
        self.http.mount("http://", _adapter)
        # optional SOCKS tunnel (SHOONYA_PROXY=socks5h://127.0.0.1:1080):
        # routes broker traffic out of a static-IP machine so the whitelist
        # matches even when this machine's own IP is dynamic
        proxies = proxy_dict()
        if proxies:
            self.http.proxies = proxies
            log.info("REST traffic routed via proxy %s", proxy_url())

    # ── auth ───────────────────────────────────────────────────────────
    def login_url(self):
        """Browser URL for the daily login (yields the auth code)."""
        return (f"{OAUTH_LOGIN_URL}?api_key={self.client_id}"
                f"&route_to={self.userid}")

    def restore_session(self):
        """Reuse today's cached access token. Returns True on success."""
        try:
            with open(self.token_cache) as f:
                cached = json.load(f)
            if (cached.get("date") == str(date.today())
                    and cached.get("uid") == self.userid
                    and cached.get("access_token")):
                self.access_token = cached["access_token"]
                self.susertoken = cached.get("susertoken")
                self.actid = cached.get("actid", self.userid)
                log.info("restored cached session for %s", self.userid)
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

    def login(self, auth_code=None):
        """Exchange the browser auth code for the day's Bearer token."""
        if auth_code is None:
            if self.restore_session():
                return self.access_token
            raise ShoonyaError(
                "no cached session for today — open this URL in a browser, "
                "log in, then pass the ?code= value to login(auth_code=...): "
                + self.login_url())

        code = self._extract_code(auth_code)
        payload = {
            "code": code,
            "checksum": _sha256(self.client_id + self.secret_code + code),
            "uid": self.userid,
        }
        body = "jData=" + json.dumps(payload, separators=(",", ":"))
        last_err = None
        for attempt in range(4):                    # ride out transient 5xx
            if attempt:
                wait = 2 ** attempt
                log.warning("GenAcsTok transient failure (%s) — retry in %ss",
                            last_err, wait)
                time.sleep(wait)
            try:
                r = self.http.post(f"{REST_HOST}/GenAcsTok", data=body,
                                   timeout=(3.05, 15))
                if r.status_code in (502, 503, 504):
                    last_err = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                resp = r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                continue
            if "access_token" not in resp:
                raise ShoonyaError(f"GenAcsTok: {resp.get('emsg', resp)}")
            self.access_token = resp["access_token"]
            self.susertoken = resp.get("susertoken")
            self.actid = resp.get("actid", self.userid)
            self._store_token()
            log.info("logged in as %s (actid %s)",
                     resp.get("USERID", self.userid), self.actid)
            return self.access_token
        raise ShoonyaError(f"GenAcsTok: giving up — {last_err}")

    def logout_cache(self):
        """Drop the cached token (forces a fresh browser login)."""
        self.access_token = None
        try:
            os.remove(self.token_cache)
        except OSError:
            pass

    @staticmethod
    def _extract_code(text):
        """Accept a bare auth code or a full redirect URL with ?code=...

        Shoonya's real redirect URL has been observed with a MALFORMED query
        string — a second '?' instead of '&' before code= (e.g.
        '...oauth?client_id=FA29913_U?code=b7cb93a6-...'). urllib.parse's
        query parser treats everything after the first '?' as one opaque
        value in that case and never finds a 'code' key, so match it
        directly with a regex instead of relying on standards-compliant
        query parsing.
        """
        text = text.strip().strip('"').strip("'")
        m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", text)
        if m:
            return m.group(1)
        return text

    def _store_token(self):
        try:
            with open(self.token_cache, "w") as f:
                json.dump({"date": str(date.today()), "uid": self.userid,
                           "access_token": self.access_token,
                           "susertoken": self.susertoken,
                           "actid": self.actid}, f)
            os.chmod(self.token_cache, 0o600)
        except OSError as e:
            log.warning("could not cache session token: %s", e)

    # ── transport ──────────────────────────────────────────────────────
    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=utf-8"}

    def _post(self, endpoint, payload, *, timeout=(3.05, 10), retries=0):
        """POST jData with the OAuth Bearer header.

        `retries` covers only transient transport failures (connection
        errors, timeouts, HTTP 502/503/504). It MUST stay 0 for
        non-idempotent calls like PlaceOrder: a "failed" request may still
        have reached the OMS, and a blind retry would double-fire.
        """
        if not self.access_token:
            raise ShoonyaError("not logged in — call login()/restore_session()")
        body = "jData=" + json.dumps(payload, separators=(",", ":"))
        last_err = None
        for attempt in range(retries + 1):
            if attempt:
                wait = min(2 ** attempt, 8)
                log.warning("%s transient failure (%s) — retry %d/%d in %ss",
                            endpoint, last_err, attempt, retries, wait)
                time.sleep(wait)
            try:
                r = self.http.post(f"{REST_HOST}/{endpoint}", data=body,
                                   headers=self._headers(), timeout=timeout)
                if r.status_code in (502, 503, 504):
                    last_err = f"HTTP {r.status_code} (Shoonya server-side)"
                    continue
                r.raise_for_status()
                resp = r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                continue
            if isinstance(resp, dict) and resp.get("stat") not in ("Ok", "OK"):
                emsg = resp.get("emsg", "")
                if "no data" in emsg.lower():
                    return []      # empty book/positions is not an error
                raise ShoonyaError(f"{endpoint}: {emsg or resp}")
            return resp
        raise ShoonyaError(
            f"{endpoint}: giving up after {retries + 1} attempts — {last_err}")

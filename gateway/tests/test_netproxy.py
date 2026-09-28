"""Multi-proxy egress fallback tests for brokers/shoonya/netproxy.

Covers the reason this exists: a single proxy box that is down, unreachable, or
whose own IP has dropped off Shoonya's whitelist must not break order flow — the
next configured proxy should carry the call. Also guards the single-URL
backward-compatible path and that status() never leaks proxy credentials.

Run:
    ./venv/bin/pytest tests/test_netproxy.py -v
"""
import pytest

from brokers.shoonya import netproxy


# ---- test doubles ---------------------------------------------------------
class _Resp:
    """Minimal stand-in for a requests.Response — only .json() is used here."""
    def __init__(self, body):
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    """Records each .post and returns the next scripted outcome.

    A scripted outcome is either a _Resp (returned) or an Exception (raised),
    keyed by whether the call was direct or via which proxy URL.
    """
    def __init__(self, direct=None, by_proxy=None):
        self._direct = direct
        self._by_proxy = by_proxy or {}
        self.proxy_calls = []       # proxy URLs used, in order

    def post(self, url, proxies=None, **kwargs):
        if proxies is None:
            outcome = self._direct
        else:
            proxy_url = proxies["https"]
            self.proxy_calls.append(proxy_url)
            outcome = self._by_proxy.get(proxy_url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


OK = {"stat": "Ok", "susertoken": "tok"}
IP_REJECT = {"stat": "Not_Ok", "emsg": "Invalid Input : INVALID_IP"}

P1 = "http://u:p1@host1:8080"
P2 = "http://u:p2@host2:8080"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Every test starts with a clean sticky state and auto mode.
    netproxy.note_direct_success()
    netproxy._last_good_proxy = None
    monkeypatch.setenv("SHOONYA_PROXY_MODE", "auto")
    monkeypatch.setenv("SHOONYA_PROXY_SCOPE", "auth+orders")
    yield


# ---- multi-proxy fallback -------------------------------------------------
def test_first_proxy_down_second_carries(monkeypatch):
    """Proxy #1 unreachable → #2 accepted. Direct was IP-rejected first."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    sess = _Session(direct=_Resp(IP_REJECT),
                    by_proxy={P1: ConnectionError("refused"), P2: _Resp(OK)})

    resp, path = netproxy.post(sess, "http://api/auth", "auth")

    assert path == "proxy"
    assert resp.json() == OK
    assert sess.proxy_calls == [P1, P2]          # tried #1, fell through to #2
    assert netproxy.status()["last_good_proxy"] == "http://***@host2:8080"


def test_first_proxy_ip_rejected_second_carries(monkeypatch):
    """Proxy #1's own IP is rejected by the broker → #2 accepted."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    sess = _Session(direct=_Resp(IP_REJECT),
                    by_proxy={P1: _Resp(IP_REJECT), P2: _Resp(OK)})

    resp, path = netproxy.post(sess, "http://api/order", "order")

    assert path == "proxy"
    assert resp.json() == OK
    assert sess.proxy_calls == [P1, P2]


def test_all_proxies_ip_rejected_returns_last(monkeypatch):
    """Every proxy rejected → caller still gets a real response with the emsg."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    sess = _Session(direct=_Resp(IP_REJECT),
                    by_proxy={P1: _Resp(IP_REJECT), P2: _Resp(IP_REJECT)})

    resp, path = netproxy.post(sess, "http://api/order", "order")

    assert path == "proxy"
    assert resp.json() == IP_REJECT              # caller raises BrokerError on this


def test_all_proxies_unreachable_reraises(monkeypatch):
    """'on' mode, no direct attempt, every proxy down → the exception surfaces."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    monkeypatch.setenv("SHOONYA_PROXY_MODE", "on")
    sess = _Session(by_proxy={P1: ConnectionError("a"), P2: ConnectionError("b")})

    with pytest.raises(ConnectionError):
        netproxy.post(sess, "http://api/order", "order")


def test_sticky_last_good_tried_first(monkeypatch):
    """After #2 wins, the next fallback starts from #2, not the top of the list."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    netproxy.note_proxy_success(P2)

    sess = _Session(direct=_Resp(IP_REJECT), by_proxy={P1: _Resp(OK), P2: _Resp(OK)})
    netproxy.post(sess, "http://api/order", "order")

    assert sess.proxy_calls[0] == P2             # last-good first


# ---- backward compatibility & scope ---------------------------------------
def test_single_proxy_unchanged(monkeypatch):
    """A lone SHOONYA_PROXY behaves exactly as the pre-multi-proxy code did."""
    monkeypatch.setenv("SHOONYA_PROXY", P1)
    sess = _Session(direct=_Resp(IP_REJECT), by_proxy={P1: _Resp(OK)})

    resp, path = netproxy.post(sess, "http://api/auth", "auth")

    assert path == "proxy"
    assert sess.proxy_calls == [P1]


def test_not_configured_stays_direct(monkeypatch):
    """No proxy set → direct, no fallback, path 'direct'."""
    monkeypatch.delenv("SHOONYA_PROXY", raising=False)
    sess = _Session(direct=_Resp(IP_REJECT))

    resp, path = netproxy.post(sess, "http://api/order", "order")

    assert path == "direct"
    assert sess.proxy_calls == []


def test_direct_success_never_touches_proxy(monkeypatch):
    """Direct accepted → proxy is never consulted even when configured."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    sess = _Session(direct=_Resp(OK), by_proxy={P1: _Resp(OK), P2: _Resp(OK)})

    resp, path = netproxy.post(sess, "http://api/auth", "auth")

    assert path == "direct"
    assert sess.proxy_calls == []


def test_data_kind_stays_direct(monkeypatch):
    """'data' is out of the default auth+orders scope → always direct."""
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    sess = _Session(direct=_Resp(IP_REJECT), by_proxy={P1: _Resp(OK)})

    resp, path = netproxy.post(sess, "http://api/quote", "data")

    assert path == "direct"
    assert sess.proxy_calls == []


# ---- credential masking ---------------------------------------------------
def test_status_masks_credentials(monkeypatch):
    monkeypatch.setenv("SHOONYA_PROXY", f"{P1},{P2}")
    st = netproxy.status()

    assert st["proxy_count"] == 2
    assert st["proxies"] == ["http://***@host1:8080", "http://***@host2:8080"]
    for shown in st["proxies"]:
        # neither the password nor the username survives
        assert "p1" not in shown and "p2" not in shown and "u:" not in shown

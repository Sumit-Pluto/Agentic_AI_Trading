"""Force TLS 1.2 for all outbound HTTPS/WSS in this process."""
import ssl

_MAX = ssl.TLSVersion.TLSv1_2


def _cap(ctx):
    try:
        ctx.maximum_version = _MAX
    except Exception:
        pass
    return ctx


_orig_default = ssl.create_default_context
ssl.create_default_context = lambda *a, **k: _cap(_orig_default(*a, **k))

try:
    import urllib3.util.ssl_ as _u
    _orig_u = _u.create_urllib3_context

    def _patched_u(*a, **k):
        return _cap(_orig_u(*a, **k))

    _u.create_urllib3_context = _patched_u
    for _name in ("urllib3.connection", "requests.adapters"):
        try:
            _m = __import__(_name, fromlist=["create_urllib3_context"])
            if hasattr(_m, "create_urllib3_context"):
                _m.create_urllib3_context = _patched_u
        except Exception:
            pass
except Exception:
    pass

try:
    import websocket as _wsmod

    _orig_run_forever = _wsmod.WebSocketApp.run_forever

    def _run_forever_tls12(self, *a, **k):
        sslopt = dict(k.get("sslopt") or {})
        if "context" not in sslopt and "ssl_version" not in sslopt:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                ctx.minimum_version = _MAX
                ctx.maximum_version = _MAX
            except Exception:
                pass
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sslopt["context"] = ctx
            k["sslopt"] = sslopt
        return _orig_run_forever(self, *a, **k)

    _wsmod.WebSocketApp.run_forever = _run_forever_tls12
except Exception:
    pass

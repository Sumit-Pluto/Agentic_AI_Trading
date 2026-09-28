# Gateway — broker service (Shoonya / Finvasia)

The always-on service that **owns the Shoonya session and secrets**. Every
strategy/engine talks to *this*, never to Shoonya directly. Vendored from
`gateway_system/Gateway/gateway_backend` and extended for the intraday engine.

- **REST**: orders · positions · funds · quotes · candles · option-chain · search
- **WebSocket** `/api/ws/ticker`: one upstream Shoonya socket fanned out to N clients
- **Auth**: JWT — human-user tokens *and* scoped **service tokens** (one per strategy)
- Owns: OAuth login (headless Selenium + TOTP → `GenAcsTok`), per-day token cache,
  TLS-1.2 pin, IP-proxy fallback, scripmaster, OI/spot depth sweeps

It satisfies the PDF's **§3 Broker Integration** and is the base for **§8 Strategy
Gateway & Conflict Management** (the ExecutionCoordinator lands here in Phase 2).

## Run it

```bash
cd gateway
cp .env.example .env            # fill FERNET_KEY, JWT_SECRET (generators in the file)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Login is IP-whitelisted.** It only works from the machine whose static IP is
registered in the Shoonya portal (the Mumbai VPS). Off-VPS you can boot the
service and run offline tests, but a fresh login fails with "invalid IP".
A human connects the broker session via `POST /api/connect` (or set
`GATEWAY_AUTO_RECONNECT=1` to restore today's cached token on boot).

Register a strategy as a service client (prints CLIENT_ID / CLIENT_SECRET once):

```bash
python -m scripts.register_service --name intraday-engine
```

## What we changed vs. the upstream gateway

| Change | Where | Why |
|---|---|---|
| **Depth-forwarding WS mode** | `ticker_manager.py`, `app/routers/ws.py` | `dk`/`df` (SNAPQUOTE) frames carry **open interest**; upstream dropped them. Clients now `{"action":"subscribe","feed":"depth","symbols":[...]}` and receive `{"type":"depth", ...}` frames (oi/poi + bid/ask ladder). Touchline path unchanged. |
| **`POST /api/candles/batch`** | `app/routers/market.py` | Warm N underlyings + option legs in one bounded-concurrency call instead of N round-trips; per-item error isolation. |
| **Sharekhan optional** | `brokers/registry.py` | This deployment is Shoonya-only; Sharekhan (needs `shareconnect`) is registered lazily and never fails the gateway if absent. Its tests are gated behind `RUN_SHAREKHAN_TESTS=1`. |

## Tests

```bash
pytest -q                       # 113 pass, Sharekhan skipped (out of scope)
```
Known-failing: 2 `test_reconciliation.py::test_offset_*` — a **pre-existing** stale
test (`_offset_opposing_lots()` gained a `siblings_to_cancel` arg the test wasn't
updated for). Owned when the ExecutionCoordinator/netting is built in Phase 2.

## Acceptance smoke test (run on the VPS)

With the gateway running and the broker session connected:

```bash
GATEWAY_BASE_URL=http://127.0.0.1:8000 \
GATEWAY_CLIENT_ID=... GATEWAY_CLIENT_SECRET=... \
python scripts/smoke_intraday.py --symbol NIFTY
```

Checks health, service-token, option-chain, candles + `candles/batch`, `oisweep`,
and the touchline **and depth** WS feeds. Non-zero exit if a required check fails.

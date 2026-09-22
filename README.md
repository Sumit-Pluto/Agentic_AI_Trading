# Trading_project_2.0

Algo-trading system for Indian markets on the Shoonya (Finvasia) API.
Priorities: minimum latency, high reliability.

> **⚠️ April 2026 API migration:** Shoonya replaced its old QuickAuth API
> (`NorenWClientTP` — now returns 502 for everyone) with an **OAuth API**.
> This project targets the new one:
> REST `https://api.shoonya.com/NorenWClientAPI/` ·
> WS `wss://api.shoonya.com/NorenWSAPI/` ·
> reference repo [Shoonya_oAuthAPI-py](https://github.com/deepak-dhyani8742/Shoonya_oAuthAPI-py)
> (confirmed official by Finvasia API support).
> **Login is IP-whitelisted**: it only works from the machine whose static IP
> is registered in the portal — from anywhere else you get "invalid IP".

## What's here

| File | Purpose |
|---|---|
| `run_app.py` | **Quant scanner + trading app** — OBS+Supertrend signals scored by the agent tree, web UI, paper log, and a PAPER/LIVE toggle (UI button, typed confirmation) that routes the same entries/exits as real Shoonya MIS orders. See `docs/QUANT_SYSTEM.md` |
| `engine/executor.py` | **LiveExecutor** — real-order router: risk-sized MKT MIS orders, websocket fill tracking (order_book polling fallback), rejection handling, daily-loss kill switch, failed-exit retry net, startup broker reconciliation. Env knobs: `LIVE_RISK_RUPEES`, `LIVE_MAX_QTY`, `LIVE_MAX_CAPITAL`, `LIVE_MAX_POSITIONS`, `LIVE_MAX_DAILY_LOSS` |
| `core/trade_mode.py` | PAPER (default) vs LIVE mode as a file sentinel (`state/live_mode.flag`) — survives restarts so a crash mid-day keeps managing real positions |
| `check_broker.py` | **Broker readiness CLI** — read-only checks (auth/IP, order book, margin, positions, scrip master, websocket); `--order-test` places + cancels one real unfillable 1-share limit order to prove the buy/sell pipeline. Same checks run in the UI's go-LIVE dialog (`/api/broker/check`, `/api/broker/ordertest`) |
| `strategist/` | **Strategy Advisor** — OI-footprint scan → view → *generates* option structures ranked by reward:risk. UI tab + `/api/strategist`. See `docs/STRATEGIST.md` |
| `news/` | **News module** — RSS aggregation (8 feeds), weighted-news scoring, market tape (yfinance), macro/corporate calendars (RapidAPI weekly + AV + Indian API, all budget-guarded), FII/DII, risk meter. Feeds the live `macro_news` agent. UI "News" tab + `/api/news` (no broker login needed) |
| `quant/`, `signals/`, `engine/`, `server/` | The quant system (agent tree, indicator ports, scanner, UI) |
| `shoonya_client.py` | Trading connector built on `shoonya_login`: REST orders over one keep-alive session, self-healing WebSocket feed (ticks/depth/order updates, 3-s heartbeat), scrip-master loader, `build_session()` |
| `shoonya_login/` | **All Shoonya login/token logic**: credentials, OAuth auth-code capture (headless Chrome + TOTP, fails fast on a rejected password/OTP), `GenAcsTok` token exchange, day-caching. Covers both the on-demand flow (`daily_login`, used by a human running the app) and the unattended flow (`python -m shoonya_login`, the VPS cron job) |
| `main.py` | Tick-stream connectivity demo (prints prices for two symbols, then stops) — not used by run_app.py, just a sanity check |
| `fetch_history.py` | Historical OHLCV via the Shoonya API (needs a cached login) |
| `indicators/` | TradingView Pine Script indicators (Supertrend Pullback v2, OBS) |
| `docs/` | Research syntheses + agent specs + system architecture |
| `.env.example` | Template for credentials — copy to `.env`, never commit |

## One-time setup (Shoonya portal)

Portal → profile → **API Key** page:
1. Copy **client id** (usually `<userid>_U`) and **secret code** → into `.env`.
2. **Redirect URL**: anything, e.g. `https://google.com` (it's not used by us).
3. **Primary IP**: the static IP of the machine that will log in — i.e. the
   Mumbai VPS. Changeable only **once per calendar week**, so register the
   permanent VPS IP, not a hotspot IP.

## Quickstart

```bash
source venv/bin/activate         # on this machine; conda activate sm_agent elsewhere
cp .env.example .env             # fill in your credentials
python main.py                   # prints the login URL, asks for the auth code
```

Daily flow: the browser login yields an **auth code** → exchanged for a
Bearer **access token**, cached in `.session_token` for the day. Restarts
skip the browser. On the VPS, `python -m shoonya_login` does the whole thing
headlessly via cron; `run_app.py`/`main.py` then start with zero prompts.

## Design notes (latency & reliability)

- **One WebSocket** (Shoonya's limit) carries ticks, depth and order updates.
  `tf`/`df` messages contain only changed fields — the client merges deltas
  into full per-instrument snapshots (`feed.quotes["NSE|26000"]`).
- **Heartbeat**: `{"t":"h"}` ping every 3 s exactly like the official client
  (the server drops silent connections); pongs feed the 15-s dead-link
  watchdog, so reconnects trigger only on genuine failures.
- **Callbacks run on the socket thread** — never block in them; enqueue and
  process on your own thread (see `main.py`).
- **Reconnect** is automatic: exponential backoff (1→30 s), full re-subscribe
  + order-stream re-arm on every reconnect (the official client doesn't
  re-subscribe — ours does).
- **Orders** go over a persistent HTTPS session — no per-order TLS handshake.
  Transient 502/503/504 are retried **only** on idempotent calls (login,
  books, quotes) — never on PlaceOrder (double-fire risk). Placement ack ≠
  fill: the order-update feed is the source of truth; reconcile with
  `order_book()` periodically.
- **Scrip master**: subscribe by `EXCHANGE|token`, mapped from tradingsymbol
  via `load_scripmaster("NFO")`; refresh daily (tokens change).
- **Run it on a Mumbai VPS** (`ap-south-1`) — mandatory anyway for the IP
  whitelist, and it puts you ~1–2 ms from Shoonya's own AWS load balancer.

## Build order (roadmap)

1. ✅ OAuth login + feed + orders scaffold (migrated to the 2026 API)
2. Mumbai VPS: register its IP, deploy, cron `python -m shoonya_login` + engine start
3. ✅ Order manager: LiveExecutor (fills via ws order stream, rejection
   handling, retry net), startup reconciliation, daily-loss kill switch,
   PAPER/LIVE toggle in the UI
4. ✅ Strategy layer (scanner + agent tree + ExitEngine drive both paper
   and live through one code path; universe ticks stream over the ws)
5. Paper-size live testing (small qty) → scale — **do this before trusting
   it with real size**

## Security

- `.env` and `.session_token` are gitignored — keep it that way.
- Never share the secret code / TOTP secret with anyone or paste them into
  chats, emails, or screenshots. Rotate the key if it ever leaks.

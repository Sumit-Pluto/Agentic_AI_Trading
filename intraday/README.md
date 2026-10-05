# intraday — agentic intraday options engine

A port of the swing_hyena agentic spine to an **intraday** cadence, feeding off
the broker Gateway (`../gateway`) for Shoonya market data + orders and computing
option greeks/IV locally (Black-Scholes). Intraday-only; swing lives elsewhere.

**Design invariant:** single code path — *backtest == paper == live*. Only the
`Broker` implementation and the `IntradayContext` data source change; scanner,
agents, risk, rules and exits run byte-for-byte the same in all three modes.

## Layout (■ built, ▹ later phase)

```
intraday/
  options/            ■ Black-Scholes greeks/IV (mathx) + Chain models + chain_builder
  contracts.py        ■ Signal · OrderIntent · Fill · Position · Broker · Brain
  config.py           ■ typed defaults, JSON-persisted, UI-editable
  journal/store.py    ■ SQLite Trade DB (PDF §4): signals/orders/positions/trades/equity/...
  gateway_client/     ■ REST client to the Gateway (read + orders) + option_tradingsymbol
  brokers.py          ■ PaperBroker (offline fills) + GatewayBroker (routes to Gateway)
  data/context.py     ■ IntradayContext interface + LiveContext; ▹ BacktestContext (Phase 4)
  scripts/demo_paper  ■ live acceptance: real chain -> greeks -> paper order -> journal
  tests/              ■ greeks vs analytic refs, chain pipeline, end-to-end paper flow

  agents/             ■ 25 deterministic agents (R/S/F/V/M/C); base=AgentInput+@agent+chain_features
  intelligence/       ■ per-bar scanner + combiner (ported) + instrument selection
  risk/governor.py    ■ Money&Risk: intraday-ATR lot sizing, daily-loss kill, heat/margin caps
  rules/engine.py     ■ Rule Engine: dup/max-positions/exposure/time-window/square-off gate
  orders/manager.py   ■ Order Mgmt: single choke point, <5s latency stamp, journal (never retries a place)
  exits/machine.py    ■ I0–I8 exit machine (square-off override, ATR trail, partials)
  data/bars.py        ■ tick→OHLCV bar aggregator (+ vwap); LiveContext wired to it
  gateway_client/ws   ■ live WS subscriber (touchline + depth), reconnecting
  runtime/loop.py     ■ intraday session loop — step(): exits→kill→scan→size→gate→place→mark
  data/sim.py         ■ SimContext — simulated NIFTY/BANKNIFTY market (demo, no broker)
  server/             ■ FastAPI: WS live-push + REST (state/positions/orders/trades/signals/
                          agents/chain/equity/reporting/config/pause/mode); serves the cockpit
  training/ llm/      ▹ Phase 4 — champion/challenger brain · optional LLM explain hook
```

The **cockpit** UI lives in `../frontend` (React + Vite + TS, WebSocket-driven):
Cockpit (tiles + live equity + signals + positions), Option Chain heatmap,
Agents (per-family score bars + regime), Journal (orders + trades), Reporting
(win-rate/P&L/drawdown/latency + by-strategy/symbol/exit), Settings (live risk knobs).

§8 conflict management lives in the Gateway: `gateway/app/services/execution_coordinator.py`
(per-contract order-lock, cross-strategy intent dedup, per-account serialized submit),
wired into `POST /api/orders` for NEW ENTRIES, enabled with `EXECUTION_COORDINATOR=1`.

PDF module map: §2.1 Money&Risk→`risk`, §2.2 Order Mgmt→`orders`+`brokers`,
§2.3 Rule Engine→`rules` (+ Gateway ExecutionCoordinator, §8), §2.4 AI Market
Intelligence→`agents`+`intelligence`, §2.5 Learning→`journal`+`training`,
§4 Order DB→`journal.store`, §6 LLM→`llm` (off), §7 latency→`orders.manager` stamps.

## Test (offline, no broker)

```bash
python -m pytest intraday/tests -q      # 84 pass: greeks/chain pipeline, paper flow,
                                        # all 25 agents, scanner, governor/rules/exits,
                                        # full session loop (entry→exit, square-off,
                                        # daily-loss halt), FUT-vs-OPT routing, live
                                        # broker fill-confirm, restart recovery +
                                        # broker reconciliation, server safety rails
```

## Run the cockpit demo (no broker needed)

```bash
./run_cockpit.sh          # builds the UI if needed, serves on http://127.0.0.1:8080
```

Opens the live cockpit driven by a **simulated** NIFTY/BANKNIFTY market — the
whole agentic system (25 agents → scanner → risk → rules → orders → I0–I8 exits)
trades on screen, journaling everything, with live P&L/positions/option-chain/
agent-breakdown. Needs Python 3.11+ with `fastapi uvicorn pandas numpy` and a
built `frontend/dist` (the script builds it). To drive the **real Shoonya** feed,
run with `engine_mode=live` once the Gateway is up (see `../gateway/README.md`) —
identical engine, only the data source changes.

## Live acceptance (on the VPS, Gateway up + connected)

```bash
python -m scripts.register_service --name intraday-engine \
    --scopes market,orders,positions,funds,status      # in ../gateway, once
GATEWAY_BASE_URL=http://127.0.0.1:8000 \
GATEWAY_CLIENT_ID=... GATEWAY_CLIENT_SECRET=... \
python -m intraday.scripts.demo_paper --symbol NIFTY
```

Builds a real NIFTY chain, computes greeks, and runs one PAPER order into the
journal (no real order placed). Config: `intraday/state/intraday_config.json`
(env `INTRADAY_CONFIG`); Trade DB: `intraday/state/intraday.db` (env `INTRADAY_DB`).

## Safety rails (live-readiness)

- **LIVE needs real data**: the PAPER→LIVE toggle (typed `LIVE` + Gateway
  client) is additionally refused unless `engine_mode=live` — real orders can
  never fire off the simulated feed. The header badges (REAL MONEY/PAPER,
  LIVE/SIM DATA, broker) always show what is actually trading.
- **Fills are confirmed, not assumed**: a live placement returns only after a
  bounded order-book poll (`live_confirm_timeout_s`, `live_fill_poll_s`).
  Unfilled-after-window orders are cancelled; leftovers report TIMEOUT
  (nothing filled) or WORKING (still live — resolve in the book). Partials
  track filled shares only. Exits/partials/flattens journal a close ONLY on a
  confirmed fill (at the real fill price) and retry next tick otherwise.
- **Restart recovery**: journal OPEN rows are restored to tracked positions
  before the first tick; LIVE startups then reconcile against the broker book
  (externally-closed rows journal-closed with NO exit order, unknown broker
  nets adopted as tracked with loud logs, qty mismatches resolved to broker
  truth, non-MIS rows ignored). The daily halt resets on session rollover.
- **FUT legs are first-class**: futures take the signal side (SELL views sell),
  exits/trailing/VWAP follow the trade direction (not CE-vs-PE), budget counts
  stop-risk notional, dedup is side-aware, and the UI marks FUT P&L live.
- **Honest numbers**: `roundtrip_cost_per_lot` (₹, default 0 — set ~40–60 for
  NSE options) books brokerage/STT into trade P&L so the kill switch and the
  reports see net; paper exits fill at bid/ask ± slippage like entries do.
- **Paper bleeds like live**: paper fills re-price at the live touch at
  execution time (buys pay the ask, sells take the bid — `paper_use_live_touch`,
  config-file only), occur `paper_exec_delay_s` (default 1 s) after submit like
  a real round trip, and every order journals decision vs fill. Reporting shows
  realized slip in bps per side plus avg submit→fill delay; the Journal shows
  Decide/Fill/Slip per order. Same accounting covers live fills automatically.
- **API guard**: set `INTRADAY_API_TOKEN` on the VPS and all mutating endpoints
  (mode/kill/pause/config) require `Authorization: Bearer <token>`; the cockpit
  prompts once and remembers it. Unset locally = zero-setup open server.
- **Live load**: exits, kill and marks run every tick; the full agent scan runs
  at `scan_every_seconds` (sim/backtest clocks scan every tick). Session
  levels (day open, opening range, first hour) are sliced to today's bars by
  the clock, so multi-session bar caches can't poison the setup agents.

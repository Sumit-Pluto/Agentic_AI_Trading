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
  server/             ▹ Phase 3 — FastAPI (REST from journal + WS push to the UI)
  training/ llm/      ▹ Phase 4 — champion/challenger brain · optional LLM explain hook
```

§8 conflict management lives in the Gateway: `gateway/app/services/execution_coordinator.py`
(per-contract order-lock, cross-strategy intent dedup, per-account serialized submit),
wired into `POST /api/orders` for NEW ENTRIES, enabled with `EXECUTION_COORDINATOR=1`.

PDF module map: §2.1 Money&Risk→`risk`, §2.2 Order Mgmt→`orders`+`brokers`,
§2.3 Rule Engine→`rules` (+ Gateway ExecutionCoordinator, §8), §2.4 AI Market
Intelligence→`agents`+`intelligence`, §2.5 Learning→`journal`+`training`,
§4 Order DB→`journal.store`, §6 LLM→`llm` (off), §7 latency→`orders.manager` stamps.

## Test (offline, no broker)

```bash
python -m pytest intraday/tests -q      # 27 pass: greeks/chain pipeline, paper flow,
                                        # all 25 agents, scanner, governor/rules/exits,
                                        # and a full session loop (entry→exit, square-off,
                                        # daily-loss halt)
```

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

# Quant Scoring System — Phase 1 (Signals + UI, Paper Mode)

## Signal flow (exactly the designed pipeline)

```
5m candles (Shoonya TPSeries, cash series)
   │
   ▼
OBS indicator  ──►  buy/sell triangle?          (signals/obs.py — port of indicators/OBS.txt)
   │ yes
   ▼
Supertrend(10, 3.0) trend agreement?            (signals/engine.py)
   │ yes → Candidate(symbol, direction, price)
   ▼
QUANT AGENT TREE                                (quant/…)
   Q ─ composite 0-100, weighted
   ├─ smc         ICT/SMC: structure BOS/CHoCH/MSS · zones OB/FVG/breaker ·
   │              liquidity sweeps · killzones · Power-of-3/AMD
   ├─ snr         S&R: OI walls · gamma levels (GEX/flip) · max pain ·
   │              pivots · dom (future stub)
   ├─ volatility  India VIX regime · IV percentile · 25Δ skew ·
   │              vega env · event calendar (events.json)
   ├─ volume      PCR · orderflow pressure · VWAP · relative volume ·
   │              futures OI buildup · volume profile (future stub)
   └─ macro       future stub (Phase 2+)
   │
   ▼
composite ≥ signal_threshold (default 60)?
   │ yes
   ▼
paper_trades.jsonl entry + scanner UI feed      (engine/scanner.py — PAPER MODE)
```

## Key behaviours

- **Missing data → skip**: any agent that can't get its data from Shoonya
  (illiquid chain, no VIX, no futures) returns unavailable; the branch
  renormalizes the remaining weights. Nothing is ever guessed.
- **Weights**: per agent, per direction (buy/sell), with per-symbol
  overrides — `quant_config.json`. Defaults came from the research pass;
  the Phase-2 backtest/training module will overwrite them.
- **Toggles**: every node can be switched off in the UI (or via
  `POST /api/agents/toggle`). Disabled nodes are excluded from scoring.
- **Persistent self-building stores** under `state/`: daily India VIX
  closes and per-symbol ATM IV history (IV percentile becomes meaningful
  after ~20 sessions of running).
- **events.json** (repo root, optional): `[{"date":"2026-07-30",
  "type":"earnings","scope":"RELIANCE","severity":"high"}, ...]` — the
  event agent penalizes signals near events; monthly expiry Tuesdays are
  added automatically.

## Running

```bash
conda activate sm_agent
python run_app.py           # daily Shoonya login → scanner + UI
# open http://127.0.0.1:8000
```

`.env` knobs: `UNIVERSE_LIMIT` (0 = all F&O stocks), `APP_PORT`,
`SCAN_POLL_SECONDS`.

Offline test (no login needed): `python tests/test_offline.py`

## UI

- **Left**: live signal feed (score chip green = accepted ≥ threshold),
  manual evaluate box (any symbol, BUY/SELL).
- **Main**: 5-minute candlestick chart with the signal marker.
- **Bottom**: the agent tree — click to drill down Q → family → sub-agents,
  green/red vs each node's threshold, gray = no data, dimmed = toggled off.
  Toggle switches persist to `quant_config.json` and re-evaluate live.

## Module map

| Path | Role |
|---|---|
| `quant/base.py` | Agent contract + weighted tree evaluation |
| `quant/config.py` | Weights/toggles/thresholds persistence |
| `quant/registry.py` | Tree assembly (add future families here) |
| `quant/context.py` | Per-signal MarketContext (lazy, cached) |
| `quant/datahub.py` | All Shoonya data access (chain, VIX, futures, candles) |
| `quant/mathutils.py` | Black-Scholes price/greeks/IV inversion |
| `quant/agents/{smc,snr,volatility,volume}.py` | The 20 leaf agents |
| `signals/{obs,engine,indicators}.py` | OBS + Supertrend ports, trigger logic |
| `engine/scanner.py` | Universe sweep, paper book |
| `server/app.py`, `server/static/index.html` | FastAPI + web UI |
| `docs/specs/*.json` | Frozen research specs each agent implements |

## Position lifecycle & automation (ported from the old SM Agent project, 2026-07-05)

- **Exit engine** (`engine/exits.py`): every accepted paper signal becomes a
  managed position — stop at protective OI wall/Supertrend/ATR (tightest),
  target1 at the opposing wall (50% partial + breakeven ratchet), OI-level
  stop ladder, Supertrend trail, wall-unwind tightening (>30% OI drop),
  15:12 EOD square-off. Stops only ever move in your favor. Persisted in
  `state/paper_positions.json`.
- **Orchestrator** (`engine/orchestrator.py`): IST session phases — 08:50
  pre-open warmup, 09:15–15:30 scanning window (auto-paused outside),
  15:35 EOD summary into the paper book.
- **Kill-switch**: file sentinel `state/killswitch.flag` (survives restarts),
  UI pause button; pausing blocks new entries but exits keep managing.
- **Activity journal** (`core/activity.py`): every sweep/signal/partial/
  stop-tighten as a one-liner; header ticker + dropdown in the UI.
- **Swing structure** (`signals/market_structure.py`, ported ZigZag):
  HH/HL/LH/LL pivot markers via the chart's Swings toggle.
- **TradingView-style chart**: supertrend overlay (color flips with regime),
  OI wall lines (PUT/CALL WALL), live position lines (ENTRY/SL/T1/T2 — the
  SL line moves as the engine ratchets). `/api/overlays`.

## Phase 2 (agreed roadmap)

1. Backtesting + weight-training module (writes into `quant_config.json`)
2. Execution module behind the paper flag (order manager)
3. DOM + volume profile agents when data exists; Telegram alerts (explicitly
   deferred by user)

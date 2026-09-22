# Swing Strategy + MCX + OI Scanner + Manual Execution — Build Plan

> **Status:** APPROVED — implementation in progress.
> **Owner:** Vivek (vivektr@insigniaconsultancy.com)
> **Last updated:** 2026-07-10
> **Audience:** engineering team. Build contract for four new capabilities.
> Edit in place; keep the Change Log current.

---

## 1. Scope & decisions

Four new capabilities, on top of the existing intraday buy/sell system:

1. **Swing signal** — Rbknox + Order Block, alerting across Cash / MCX / F&O.
2. **MCX** — full multi-segment support across the platform.
3. **Faster OI scanner + watchlist** — with per-strategy P&L-since-add.
4. **Manual order execution** — search → score → manual buy/sell, all segments.

### Decisions locked in (2026-07-10)

| # | Decision | Choice |
|---|---|---|
| 1 | OI-strategy hit | **Buildup matrix (price×OI) confirmed by wall / max-pain** structure |
| 2 | Swing trigger | **Rbknox (Knoxville) reversal at the OB tap IS the signal** (OB supplies breakout+zone+retrace context) |
| 3 | Swing timeframe | **Daily AND intraday (15m/1h)** |
| 4 | Manual orders | **Real Shoonya orders, with explicit confirm**, kill-switch respected, all segments |

### Key source finding

**"Rbknox" = "RB Knox" = Rob Booker's Knoxville Divergence.** Source project
`/Users/mac/Downloads/div_4_doc/Trading_Project/Agents/SM Agent`:

- `intraday/app/services/knoxville.py` — `compute_knoxville(df, p) -> KnoxResult`.
  Bullish reversal = new price low + momentum higher-low (divergence) + RSI was
  ≤30 in lookback, confirmed by Stoch-RSI roll-up within 5 bars. Pure numpy.
- `intraday/app/services/ob_reversal.py` + `market_structure.py` (ATR-ZigZag) —
  breakout (close beyond a major swing ≥5 bars back, body ≥45% range, volume
  ≥2.0× 20-bar avg) → OB zone (base candle full range) → retrace tap → confirm.
- **The Knoxville-at-OB fusion does NOT exist there** — the OB uses its own
  pop-out trigger. Per decision #2 we replace that trigger with a Knoxville
  reversal check at the OB tap. Both engines take a plain OHLCV DataFrame.

---

## 2. Architecture principles

- **Reuse, don't rebuild.** Lift `knoxville.py` / `ob_reversal.py` /
  `market_structure.py` (numpy-only), swap their yfinance data source for
  Shoonya candles.
- **Segment-aware, not NSE-hardcoded.** Introduce a segment concept
  (CASH / FNO / MCX); DataHub resolves exchange+token per symbol internally so
  agents and the scanner stay single-arg (symbol).
- **One scoring tree, many signal generators.** The intraday `check_signal` and
  the new swing generator both feed the same `evaluate() → paper.record →
  open_from_signal` pipeline, tagged by `strategy`.
- **Attribution via a `strategy` tag** on every position/paper row — the
  foundation for per-strategy P&L (OI watchlist, swing vs intraday).

---

## 3. Phase A — Multi-segment foundation (prerequisite)

Everything else depends on this.

| Item | File | Change |
|---|---|---|
| Load MCX master + index | `quant/datahub.py` | `self.mcx = load_scripmaster("MCX")`, `_index_mcx()` (front-month `FUTCOM`/`FUTBLN`/…), `mcx_universe()` |
| Segment resolution | `quant/datahub.py` | `segment_of(symbol)` / `resolve(symbol, segment) -> (exchange, token, tsym)`; a `SEGMENTS = {CASH, FNO, MCX}` map |
| Exchange-parametrized data | `quant/datahub.py` | thread `segment`/`exchange` through `candles_5m`/`daily_candles`/`cash_quote`/`futures_quote`/`chain_snapshot` (default NSE/NFO preserves current behaviour) |
| Cash universe | `quant/datahub.py` | `cash_universe()` (configurable; default = F&O underlyings’ equities + optional list) |
| Feed subscription | `run_app.py` | subscribe `MCX\|token` (and cash tokens) for the active universes |
| Executor exchange | `engine/executor.py` | un-hardcode `_place(exchange=...)` (resolve per symbol/segment) |
| Strategy tag | `engine/exits.py` (`PaperPosition`), `engine/scanner.py` | add `strategy: str = "intraday"`, thread through `evaluate()`/`open_from_signal`/`paper_entry` |

**Acceptance:** `mcx_universe()` returns commodity front-month symbols; candles +
quotes fetch for an MCX symbol; a paper position carries a `strategy` tag;
existing NSE/NFO behaviour unchanged (regression: intraday self-tests pass).

Scale note: "all Cash" = ~2000 NSE equities. Cash/MCX universes are
**config-capped** (env `CASH_UNIVERSE`, `MCX_UNIVERSE`) so scans stay bounded;
daily swing scans run concurrently. Log any cap applied.

---

## 4. Phase B — Swing signal (Knoxville + OB)

- New package `signals/swing/`: `knoxville.py`, `ob_reversal.py`,
  `structure.py` (ATR-ZigZag) lifted + de-yfinanced (fed Shoonya candles).
- **Fusion** `signals/swing/engine.py` `swing_signal(df) -> SwingCandidate|None`:
  OB engine → breakout + OB zone + retrace `tap_idx`; then
  `compute_knoxville()` on bars around the tap; emit BUY only when a **bullish
  Knoxville divergence fires while price is in/leaving the OB zone** (mirror for
  SELL). Carry OB levels (entry/stop/target) + Knox detail.
- **Timeframes:** daily + intraday (15m/1h) per decision #3 — a small TF loop.
- **Scanner hook:** a swing sweep (own cadence/cooldown) beside the intraday
  sweep in `engine/scanner.py`, feeding `evaluate(…, strategy="swing")` →
  `paper.record` → `open_from_signal`. Alerts across all 3 segments.
- **Optional C++** later (the zone/structure loops) using the proven `qcore`
  pattern, only if profiling shows it matters.

---

## 5. Phase C — Faster OI scanner + watchlist

- **OI-strategy hit (decision #1):** price×OI buildup (long/short buildup,
  short-covering, long-unwinding from `state/oi_history` + futures OI) **AND**
  confirming wall / max-pain structure (reuse `oi_walls`/`gamma_levels`/
  `max_pain`). A hit = fresh buildup aligned with wall support/resistance.
- **Speed:** concurrent per-symbol scan (thread pool, already have the pattern);
  a `qcore` C++ kernel for the buildup math over the universe if it dominates.
- **Watchlist store** `state/watchlists.json` (atomic write pattern): each item
  `{symbol, segment, strategy, direction, added_at, price_at_add, note}`.
- **Per-strategy P&L-since-add:** for each watchlist item, live delta =
  `(cash_quote(sym) − price_at_add) × sign`; aggregate **only within that
  strategy's list** (not global). New routes `GET/POST /api/watchlist`,
  `GET /api/watchlist/pnl?strategy=`.
- **UI:** OI Scanner tab (ranked hits + "save to watchlist") + Watchlist tab
  (items grouped by strategy with since-add P&L beneath each).

---

## 6. Phase D — Manual execution + per-symbol score

- **Score:** `/api/evaluate` already returns a single-symbol score+tree; make it
  segment-aware (Cash/MCX/F&O).
- **Manual order (decision #4):** `POST /api/manual/order`
  `{symbol, segment, direction, qty, confirm}` → resolve exchange+tsym →
  `core.broker_check.place_marketable(...)` (marketable-limit fallback) →
  `_await_fill`; kill-switch + daily-loss guards respected; records a
  `live_manual` row tagged `strategy="manual"`. Requires explicit `confirm`.
- **UI:** Manual tab — search symbol → show score/tree → qty + BUY/SELL with a
  confirm step. (Reuses the existing `#eval-box` template.)

---

## 7. Verification constraints

Offline-testable: segment resolution, the Knoxville+OB fusion vs the source
engines on fixtures, OI-buildup classification, watchlist P&L math, universe
construction. Needs the VPS + market hours: live MCX ticks, real manual orders,
end-to-end swing alerts on live data.

---

## Implementation status (2026-07-10) — ALL PHASES DONE (verified offline)

| Phase | Delivered | Verified |
|---|---|---|
| Buy-gold removal | GOLDPETAL test button + `/api/broker/manualorder` route removed | grep clean |
| **A** MCX + segments | `quant/datahub.py` (MCX masters, `_index_mcx`, `mcx_universe`/`cash_universe`, `segment_of`, `candle_ref`, segment-aware `candles`/`daily_candles`/`futures_candles`/`cash_quote`/`futures_quote`/`price`); `run_app.py` MCX feed sub; `engine/executor.py` exchange-parametrized `_place`; `engine/exits.py`+`engine/scanner.py` `strategy`/`segment` tags | 30 MCX commodities resolved vs real masters; tag round-trip |
| **B** Swing (Rbknox+OB) | `signals/swing/` (knoxville/ob_reversal/market_structure **byte-identical** + `engine.swing_signal` fusion); `engine/swing_scanner.py` (3 segments, daily+15m+60m, ranked, persisted); `/api/swing`; run_app wired | fusion-logic test; scanner smoke test |
| **C** OI scanner + watchlist | `engine/oi_scanner.py` (buildup×wall, concurrent, persisted); `engine/watchlist.py` (per-strategy P&L-since-add); `/api/oi-scan`, `/api/watchlist` (GET/POST/remove); run_app wired; **UI tabs** (OI Scanner, Watchlist) | OI smoke test (buildup+confirm); watchlist P&L-isolation test |
| **D** Manual + score | segment-aware `/api/evaluate`; `POST /api/manual/order` (real, confirm, kill-switch, Cash/F&O/MCX); **UI Manual tab** (search→score→BUY/SELL) | route registration; app builds |

UI: 4 new tabs (Swing / OI Scanner / Watchlist / Manual) in `server/static/index.html`
(JS syntax-checked). **Live MCX ticks, real orders and end-to-end alerts on live
data still need the VPS during market hours.**

## Change Log

| Date | Who | Change |
|---|---|---|
| 2026-07-10 | Claude | Initial plan: Rbknox=Knoxville finding, 4 decisions locked, Phases A–D |
| 2026-07-10 | Claude | Implemented + verified offline all phases: buy-gold removal; A (MCX/segments); B (swing Rbknox+OB fusion + scanner); C (OI buildup+wall scanner + watchlist per-strategy P&L); D (manual order + segment-aware score); 4 UI tabs |
| | | |

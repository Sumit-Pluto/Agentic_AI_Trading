# C++ Engine + Shoonya Push-Feed Architecture

> **Status:** APPROVED PLAN — implementation in progress (Phase 1).
> **Owner:** Vivek (vivektr@insigniaconsultancy.com)
> **Last updated:** 2026-07-09
> **Audience:** engineering team. This is the implementation contract for the
> data-layer rewrite and the C++ compute engine. Edit in place; keep the Change
> Log current.

---

## 1. Goal & scope

Make the scanner fast enough for tick-by-tick evaluation and fastest-possible
execution, by:

1. Replacing serial REST polling with a **push-based Shoonya websocket feed**
   (reused from the Snowball system).
2. Removing **yfinance** entirely.
3. Adding a **C++ compute kernel** for the heavy per-symbol math — *after* the
   data layer is fixed and measured.

### Decisions locked in (2026-07-09)

| # | Decision | Choice |
|---|---|---|
| 1 | C++ sequencing | **Data layer first, then C++.** Fix I/O, measure, then build the native kernel where math still dominates. |
| 2 | Global macro data (yfinance replacement) | **Keep one non-yfinance global source.** Shoonya for Indian instruments; a separate API for global tickers. |
| 3 | Snowball gateway reuse | **Lift the WS code into this project.** Vendor the gateway/auth/tick-cache directly — no separate service. |

---

## 2. The bottleneck finding (why this ordering)

Measured from the current code, the scan is **I/O-bound, not CPU-bound**:

- Scanner is a single-threaded serial loop over **228 symbols**, every ~60s
  (`engine/scanner.py` `sweep_once`).
- Every sweep re-fetches 5-min candles for all 228 symbols over sequential REST
  → a **~14s floor** before any scoring, even when nothing triggers.
- Each triggered symbol fires **~34 sequential option-chain quote calls**
  (CE+PE per strike), all funneled through a single global `0.06s` throttle
  that serializes every Shoonya request.
- The websocket currently pushes **only cash LTP**; candles, option chain, VIX
  and futures all go over REST.

**Implication for C++:** a native rewrite of code that spends 99% of its time
waiting on the network yields ~nothing. The order-of-magnitude win is
architectural — push feed + concurrent I/O. C++ then earns its keep on the real
CPU cost (SMC structure scans `O(bars×zones)`; Black-Scholes IV inversion
~2,700 evals/symbol; GEX/gamma) **once we scan the full universe per tick**.

---

## 3. Target architecture

**Principle: Python owns I/O; C++ owns compute; pybind11 is the bridge.**
Shoonya auth (Selenium + OAuth + TOTP) and the NorenApi websocket are already
solved in Python by Snowball — reimplementing them in C++ would be pure I/O work
for zero gain. C++ receives ticks/candles/chains from Python and returns
signals.

```
  Shoonya cloud
   ▲   │
 orders ticks            PYTHON — I/O & orchestration
   │   ▼                 ────────────────────────────────────────────────
   │            feed/ (vendored from Snowball): NorenApi OAuth WebSocket
   │              → ticker bridge → tick cache (PriceState, O(1))
   │                     │  push ticks/candles/chain  (zero-copy numpy)
   │                     ▼
   │            ┌────────────────────────────────┐
   │            │  C++ COMPUTE KERNEL (pybind11)  │   ← Phase 2
   │            │  native market state            │
   │            │  SMC scan · Black-Scholes/IV · GEX
   │            │  scans all symbols, GIL released → true parallel
   │            └────────────────────────────────┘
   │                     │ candidate signals
   │                     ▼
   └──────────  Python execution / exits / FastAPI UI   (Phase 3: stays Python)
     REST orders
```

**Why pybind11 in-process (not a separate C++ process):** share numpy arrays by
pointer (zero serialization), release the GIL during the C++ scan for true
multi-threaded scanning across symbols, and keep Python for orchestration, the
FastAPI UI, logging, and execution. Same pattern numpy/pandas use internally.

**Why execution stays in Python:** order placement is a REST call to Shoonya
(network RTT in ms) — C++ vs Python is negligible there. The latency that
matters is decision→order, which the C++ scan kernel already owns. Execution
reuses Snowball's order path + reconciliation.

---

## 4. Phase 1 — Data layer (in progress)

Goal: kill the serial-REST floor; candles/chain/VIX/futures arrive by push or
concurrent fetch; yfinance gone. No agent code changes (everything funnels
through `quant/context.py` `ctx.*` and `DataHub` methods).

### 4.1 Vendor the Snowball **consumption model** onto the existing transport

**Key discovery (2026-07-09):** this project's `shoonya_client.py` already has a
**proxy-aware** WS transport that Snowball's gateway lacks — it tunnels both REST
and the websocket through a SOCKS proxy (`SHOONYA_PROXY`, lines 106-108 /
438-445) for Shoonya's **IP-whitelist**, and already does the 2026 OAuth Bearer
WS handshake (`ShoonyaFeed`, `subscribe`, `subscribe_orders`). Snowball has no
proxy layer. So we **keep this project's transport** and vendor only Snowball's
*consumption model* on top — otherwise we'd regress the live-account login.

| New file | Adapted from | Contents |
|---|---|---|
| `feed/cache.py` | Snowball `core/engine.py` (`PriceState`, `_on_tick`) | slotted O(1) tick cache keyed `EXCH\|TOKEN`, monotonic-age freshness, partial-frame merge. **Self-contained, unit-testable offline.** |
| `feed/pump.py` | Snowball `ticker_manager.py` + `_price_keeper_loop` | binds `ShoonyaFeed` dispatch → `TickCache`; batched REST fallback for stale tokens; subscribe/unsubscribe management |

Transport (kept as-is): `shoonya_client.py` `ShoonyaSession` (OAuth + proxy) and
`ShoonyaFeed` (proxy-aware WS). No new NorenApi dependency, no proxy regression.
The Snowball auth (Selenium+TOTP daily cache) is **not** needed — this project
already caches the day's Bearer token on disk.

### 4.2 Wire the feed into `DataHub`

Replace what sits behind these methods (agents untouched):

| `DataHub` method | Today | After |
|---|---|---|
| `cash_quote` | WS snapshot w/ 30s gate | push cache (`PriceState`, monotonic age) |
| `candles_5m` | serial REST / yfinance | Shoonya time series, **concurrent** (4.3) |
| `chain_snapshot` | ~34 serial REST/symbol | subscribe strikes to WS (push) + concurrent REST fallback |
| `india_vix` / `futures_quote` | REST/symbol | push cache (subscribe the tokens) |

### 4.3 Concurrency (the actual speedup)

- Candles: replace the 228-symbol serial loop with **chunked `asyncio.gather`**
  (Snowball's batched-in-4 pattern) — or better, drive scanning off pushed bars.
- Option chain: **subscribe the ATM±span strikes to the websocket** so quotes
  push instead of 34 pull-calls; keep a concurrent REST fallback for cold cache.
- Net effect: the ~14s candle floor and the per-symbol 34-call chain fetch both
  collapse toward event-driven updates.

### 4.4–4.6 Remove yfinance (4 call sites)

| Site | File | Replacement |
|---|---|---|
| candle fallback (default in paper mode) | `quant/datahub.py` `_candles_yahoo` | Shoonya `get_time_series` |
| universe prefetch | `quant/datahub.py` `prefetch_candles` | Shoonya batched time series |
| macro tape (14 global tickers) | `news/markets.py` `fetch_tape` | **Decision 2:** Indian → Shoonya/MCX; global (US10Y, DXY, SPX, Nasdaq, Nikkei, HangSeng) → one global API (proposed: **Twelve Data** — generous free tier, batch quotes, global indices/fx/commodities; Alpha Vantage as fallback if a key already exists) |
| offline CSV backfill | `fetch_history.py` | Shoonya daily series |
| dependency | `requirements.txt` | remove `yfinance` |

**Note on Decision 2:** Shoonya/NSE cannot provide US10Y, DXY, S&P, Nasdaq,
Nikkei, Hang Seng (Brent/WTI/Gold only via MCX). Those globals move to the one
external source; everything Indian comes from Shoonya. Final source TBD-confirm.

### Phase 1 acceptance

- Scanner runs with **zero yfinance imports** (grep clean).
- A full sweep with nothing triggering no longer blocks ~14s on candle REST.
- `cash_quote`, `india_vix`, `futures` read from the push cache; freshness via
  monotonic `age()`.
- Live cash LTP + order fills flow through the vendored feed.

---

## 5. Phase 2 — C++ compute kernel (after Phase 1 is measured)

Build only if, post-Phase-1, per-symbol math is the measured limit for
full-universe per-tick scanning.

- **Boundary:** a pybind11 module `qcore` exposing `scan(symbols, market_state)
  → [signals]`. Python pushes ticks/candles/chain arrays (numpy, zero-copy);
  C++ maintains native per-symbol state and returns candidates.
- **Port first (highest CPU):** SMC structure/zone scan (`quant/agents/smc.py`
  `_active_zones`/`_simulate`, `O(bars×zones)`), Black-Scholes + bisection IV
  (`quant/mathutils.py`, `quant/context.py` `compute_ivs`), GEX/gamma-flip
  (`quant/agents/snr.py` `gamma_levels`).
- **Parallelism:** release the GIL, scan symbols across `std::thread`/OpenMP —
  the thing Python can't do.
- **Keep in Python:** the weighted-tree orchestration (`quant/base.py`) and the
  cheap leaves; only the hot math crosses into C++.
- **Build:** `pybind11` + CMake, built as a wheel; graceful import fallback to
  the existing pure-Python path so the system still runs without the compiled
  module.

---

## 6. Phase 3 — Execution (stays Python)

Reuse Snowball's order path (`brokers/shoonya/adapter.py` `place_order` that
preserves `emsg` rejection reasons) + reconciliation loop. C++ owns the
decision; Python owns the network order + fill confirmation (websocket
`order_update`, polling fallback). No C++ here — it's I/O.

---

## 7. Verification constraints (read before testing)

End-to-end verification needs **live Shoonya credentials + market hours** — the
feed can't stream without an authenticated OAuth session, and IP whitelisting
means login must run from the fixed-IP VPS. What we *can* verify offline:
yfinance-removal (grep + unit calls against Shoonya series in a recorded
session), the concurrency refactor (structural), and the C++ kernel (unit tests
vs the Python reference outputs, bit-for-bit on sample chains). Live tick flow,
subscribe/reconnect, and fill latency are VPS + market-hours tests.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Feed swap touches the live trading data layer | Phase-1 behind a flag; keep REST path as fallback until push feed is proven live |
| No proxy/IP-whitelist in Snowball gateway | Add the SOCKS/whitelist tunnel this project already uses (`shoonya_client.py`) to the vendored feed |
| Global source rate limits (Twelve Data free) | 14 tickers @ ~60s cadence; batch quotes; cache; degrade to last-good on 429 |
| C++ math diverges from Python reference | Unit tests assert kernel == Python outputs on fixtures before switching the scanner over |
| Chain-via-websocket increases subscription count | Subscribe only ATM±span per active symbol; unsubscribe on symbol churn |

---

## Implementation status (2026-07-09)

**Phase 1 — DONE, verified offline.** yfinance fully removed (zero code refs);
push-cache feed layer added; option-chain + candle fetch now concurrent.

| Item | Files | Verified |
|---|---|---|
| 1.1 Feed cache + pump | `feed/cache.py`, `feed/pump.py`, `feed/__init__.py` | `python -m feed.cache`, `python -m feed.pump` (green) |
| 1.2 DataHub push-feed wiring | `quant/datahub.py` (`attach_feed(cache)`, cache-first `_feed_quote`/`_quote_raw`), `run_app.py` (FeedPump wired) | compiles; agents untouched (contract preserved) |
| 1.3 Concurrent chain + candle | `quant/datahub.py` (`_quotes_batch`, concurrent `prefetch_candles`) | compiles; `CHAIN_FETCH_WORKERS` knob |
| 1.4 Candles Shoonya-only | `quant/datahub.py` (yahoo paths removed) | grep clean |
| 1.5 Tape → Twelve Data | `news/markets.py`, `.env.example` (`TWELVEDATA_KEY`) | `python news/markets.py` self-test (green) |
| 1.6 CLI + deps | `fetch_history.py` (Shoonya), `requirements.txt` (yfinance out) | compiles |

Live smoke test (tick flow, subscribe/reconnect, fill latency) still pending on
the VPS — see §7.

**Phase 2a — DONE, verified.** C++ kernel `qcore` (Black-Scholes price/gamma/vega
+ bisection IV) built via `qcore/build.sh` (pybind11, no CMake). `python -m
qcore.test_qcore`: **bit-for-bit identical to quant.mathutils (max Δ = 0 across
640 grid cases + 640 IV solves), 21.7× faster on 20k IV inversions (393ms →
18ms, GIL released).** Wired into all 5 BS/IV consumers (`quant/context.py`
`compute_ivs`, `quant/agents/{snr,volatility,volume}.py`) via `from qcore import
…`, which transparently **falls back to `quant.mathutils` when the `.so` isn't
built** — so the app always runs. Confirmed live through the real `compute_ivs`
path (`backend: cpp`).

**Phase 2b — DONE (SMC zone sim), verified.** Ported the dominant SMC hot loop —
`SmcZones._simulate`, the per-zone lifecycle walk that runs O(zones×n) — to a
**batched** C++ kernel `qcore.simulate_zones` (all zones in one GIL-released
call, constants passed from Python so it can't drift from `smc.py`). Wired into
`smc._active_zones` via a new `_simulate_all` that uses the C++ batch when built
and falls back to the per-zone Python `_simulate` otherwise (`import qcore` is
defensive). `python -m qcore.test_smc`: **600 zones, 0 mismatch vs the Python
reference (state/mitig/weakened/inv_at), SmcZones score identical C++-vs-Python,
190× faster on the batched sim (78ms → 0.4ms for 4k zones).** All four SMC-family
agent self-tests pass unchanged.

*Remaining optional SMC ports (same proven pattern, not yet done):* `_pivots`,
the structure state machine, `_find_fvgs`, and the GEX/gamma aggregation in
`snr.py` (its `bs_gamma` is already on the C++ kernel; only the strike-loop
aggregation is still Python). These are lower-order costs than the zone sim; add
them if live profiling shows they matter.

**Phase 3 — CONFIRMED, no change.** Execution stays in Python
(`engine/executor.py` order path + reconciliation). Order placement is
network-bound (REST to Shoonya), so C++ adds nothing; the C++ scan owns the
latency-critical decision. Nothing to port.

## Change Log

| Date | Who | Change |
|---|---|---|
| 2026-07-09 | Claude | Initial approved plan: bottleneck finding (I/O-bound), Python-I/O + C++-compute architecture, Phase 1 data-layer detail, Phase 2 C++ kernel design, decisions 1–3 locked |
| 2026-07-09 | Claude | Phase 1 implemented + verified offline (feed cache/pump, DataHub push wiring, concurrent chain/candle, yfinance removed → Shoonya + Twelve Data). Phase 2a C++ kernel `qcore` built, bit-exact vs Python, 21.7× faster, wired with fallback. Phase 3 confirmed no-change. |
| 2026-07-09 | Claude | Phase 2b: ported `SmcZones._simulate` → batched C++ `qcore.simulate_zones` (GIL-released), wired into `_active_zones` with defensive Python fallback. Verified: 600 zones 0-mismatch vs reference, score parity, 190× faster; all SMC agent self-tests pass. Optional remaining SMC ports (`_pivots`, state machine, FVG, GEX aggregation) noted for later. |
| | | |

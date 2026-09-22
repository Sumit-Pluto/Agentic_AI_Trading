# Intraday Agent Pipeline — v-next Blueprint (Plug-in Strategies · Pluggable Filters · Meta-Model)

> **Status:** DESIGN OF RECORD — supersedes the architecture sketch in
> `AGENT_ARCHITECTURE_RESEARCH.md` by folding in the full research-corpus findings
> (see `DATA_CATALOG_FOR_TRAINING.md` v2 for the feature evidence grades, and
> `../OptionSmith/Research/_FLAGGED_weak_sources/` for what was discarded).
> **Owner:** Vivek · **Date:** 2026-09-15
> **Companion diagram:** `architecture_A_rule_based_subagents.drawio` (v2).

This document answers, concretely: what the pipeline looks like end to end, what NEW
modules/data go in, how the **strategies are pluggable trigger sources**, how the
**filters are pluggable (user-toggleable) gates**, and how the **agents + meta-model get
trained** (which layers, which feature sets, which labels, which validation).

---

## 0. The one-paragraph shape

Many **strategy plugs** propose trade candidates → every candidate is **enriched** with the
same shared feature vector → a **triple-barrier labeler** grades history → one **meta-model**
(LightGBM + calibration) scores P(win) → a **pluggable filter stack** (each filter
independently toggleable) applies gates/vetoes → a **portfolio selector** ranks survivors and
sizes them → **execution + exits** → outcomes flow back to the **outcome DB** that retrains
everything. Nothing downstream of a strategy cares which strategy fired — so strategies,
filters, and features are all hot-swappable registries.

```
STRATEGY PLUGS (triggers)                     PLUGGABLE FILTER STACK (toggleable)
  algo_docx setups 1-9                          [x] regime_gate      [x] event_blackout
  obs_supertrend                                [x] liquidity_screen [x] ban_gate (FutEq)
  agent_primary (tree-as-trigger)               [x] wall_proximity   [ ] forensic_avoid
  research intraday (ORB/VWAP/gap-fade)          [x] quote_staleness  [ ] sentiment_veto
  end_to_end model (later)                      [x] cost_floor       [ ] overnight_hold
        │  TriggerEvent{strategy_id,setup_id,sym,dir,entry,stop,target,horizon,ts}          │
        ▼                                                                                    │
  FEATURE ENRICHMENT  ── shared vector: agent-family scores + context (§2) ──┐              │
        ▼                                                                     ▼              │
  TRIPLE-BARRIER LABELER (train time)  ─────►  META-MODEL  P(win)  ──►  FILTER STACK ────────┘
   (§4: stop/target/EOD + cost floor)          (§5: LightGBM+calib)     (§6: gates/vetoes)
                                                                              ▼
                                                        PORTFOLIO SELECTOR (rank by P(win),
                                                        size ∝ (P−p*), vol-target/¼-Kelly)
                                                                              ▼
                                                        EXECUTION & EXITS  ──►  OUTCOME DB ──► retrain
```

---

## 1. LAYER A — Strategy Plugs (pluggable trigger sources)   [task 5]

A "strategy" is only a **candidate generator**. Each plug implements one contract so the
rest of the pipeline is strategy-agnostic; adding/removing a strategy is a registry edit +
a nightly retrain (never a new model — see `AGENT_TRAINING_AND_SCALING.md`).

**The `TriggerEvent` contract (every plug emits this):**
```
TriggerEvent = {
  strategy_id, setup_id,          # who fired (both become model features)
  symbol, direction,             # BUY / SELL
  bar_time, entry_price,
  stop_rule, target_rule, horizon,  # the plug's OWN barrier geometry (feeds the labeler)
  extras{}                       # strategy-specific scratch (e.g. which candle, gap size)
}
```

**Registry of plugs (v-next):**

| Plug | Kind | Triggers on | Status |
|---|---|---|---|
| `algo_docx` | rule setups 1–7, 9 (from Algo.docx) | ORB/PDH break, lowest-vol retrace, DEMA pullback, gap fade, 3-candle reversal | build (§1.1) |
| `obs_supertrend` | indicator | OBS + Supertrend pullback (already live) | exists |
| `agent_primary` | tree-as-trigger | composite score crosses bar, both directions (already live) | exists |
| `research_intraday` | research strategies | ORB+VIX-gate, VWAP-band, GIFT gap-fade, expiry playbook | build (§1.2) |
| `end_to_end` | learned model | scans every bar; the "no-blind-spot" plug | phase 3 |

### 1.1 `algo_docx` — which setups are automatable (from the Algo.docx review)
Ship **setups 1, 2, 3, 4, 6 + the setup-9 modifier** first (crisp, fully candle-computable);
add **5 and 7** with flagged geometry assumptions; **park setup 8** (positional, manual S/R
levels → different horizon class) and the "pink candle" filter (depends on private TradingView
conditions not in the doc). The doc's own risk rules (SL≤1% or skip, book 50% at 1:2 or 0.8%,
SL→cost, square-off 15:15) become the **label geometry**, not code branches.

### 1.2 `research_intraday` — trigger ideas worth testing (measure locally, don't trust doc numbers)
ORB(9:15–9:30)+VIX-band gate; GIFT gap-fade conditioned on reversal-by-9:30; VWAP-band
(ADX<20 → fade ±2σ, ADX>25 → breakout); Judas/gap-and-trap; expiry-day max-pain-distance
fade + pin trade. All sourced from a FLAGGED doc → treat as candidate triggers, prove each
in the per-setup scorecard before it earns allocation.

### 1.3 New "no-blind-spot" note
The meta-model can only judge what a plug proposes — it improves **precision**, not **recall**.
Widen the opportunity set by adding plugs (rules now, the end-to-end scanner later), not by
retraining a bigger single model.

---

## 2. LAYER B — Feature Enrichment (the NEW modules)   [task 2]

Every candidate gets the same vector. Full evidence grades + sources live in
`DATA_CATALOG_FOR_TRAINING.md`; this is the module map. Families new since the first
architecture doc are marked **NEW**.

| Module | Key features | Data (free unless noted) |
|---|---|---|
| Agent-family scores | smc, snr, volatility, volume, macro (0–100, per direction) | existing tree |
| **Sector / cross-stock** NEW | sector index trend/RS/β (Vasicek-shrunk), de-macroed residual, breadth (%>200-DMA, A/D), BankNifty constituent nowcast, implied-correlation ρ + Δρ spike | `ind_close_all` archive |
| **Institutional / flow** NEW | participant-OI *changes* (FII short-put/call build, Pro sign-flip, DII put-hedge), FII/DII flow level+deceleration+cluster, futures basis + SSF-volume tell, CPIV, OTM-call-OI concentration | bhavcopy, participant-OI file |
| **Insider / smart-money** NEW | PIT opportunistic buys (routine-filtered), bulk/block entity forward-CAR tracker, delivery-% accumulation composite, promoter-pledge flags, forensic avoid-list | NSE/BSE PIT + bulk.csv + SAST |
| Options-derived | VRP **split overnight/intraday**, signed+unsigned GEX+zero-gamma zone, wall-distance-in-EM + wall migration, skew z-family (XZZ, RR25 z, spot-conditional), term slope, event-crush priors, ΔOI×ΔIV 2-bit flow | option chain |
| **Regime** (expanded) | VIX pctile + VRP + term-slope + vol-of-vol (dial); **+ Hurst + ADX + HMM posterior/duration + RV-own-pctile + first-15-min day-type** | chain + indices |
| **Calendar / era** NEW | days_to_event, dte_at_event, announce slot, DTE clock, delivery-margin ramp, seasonality survivors (Jul/Jan VIX, E+1), **era-tag columns (12 break dates)** | event calendar |
| **Gap / pre-open** NEW | gap in ATR units, gap-quality (size×relvol×breadth), gap taxonomy, per-stock gap personality, band-clipped flag, ADR overnight, **pre-open auction imbalance/IEP** (record-first) | pre-open feed |
| **Microstructure** (retail-honest) | OFI (pressure gauge, not forecast), queue-imbalance gate, micro-price, Kyle λ, absorption, sweep-and-thin | 1Hz depth (record-first) |
| Anchors | fractal S/R with bounce-count, anchored VWAP, volume profile POC/VAH/VAL, round-number grid | candles |

**Design rule (from the corpus, unanimous):** every feature is a **gate / multiplier /
tie-breaker**, never a standalone EV; friction-net expectancy is the master gate.

### 2.1 India data-layer sources not in the original list   [task 2]
All free, verified URL patterns (plain GET + User-Agent, ~3 req/s):
- `ind_close_all_{DDMMYYYY}.csv` — all index OHLC + India VIX, history ~2012 (the big unlock).
- F&O bhavcopy UDiFF (`BhavCopy_NSE_FO_..._F_0000.csv.zip`) — per-contract OI, ΔOI, volume, **trade count**, settle.
- Equity bhavcopy `sec_bhavdata_full` — spot OHLC + **DELIV_QTY / DELIV_PER**.
- `fao_participant_oi_*` **and** `fao_participant_vol_*` (Client/Pro/FII/DII long-short).
- `combineoi_*` (MWPL) + `fo_secban_*` — **use the FutEq (delta-adjusted) column** for bans.
- NSE JSON APIs: `market-data-pre-open`, corporate-announcements (carries a `difference`
  filing→dissemination timestamp), `live-analysis-oi-spurts`, `fiidiiTradeReact`, `allIndices`
  (VIX + breadth), holiday-master, option-chain.
- NSE/BSE **bulk & block deal** CSVs (named counterparties), PIT `corporates-pit`, SAST-31 pledge.
- NSCCL **SPAN** files (~6×/day) + `fo_mktlots.csv` — real margin & runtime lot sizes.
- Record-first (cannot be bought later): pre-open auction stream, own chain snapshots
  (3-min OI clock), own L2 depth to parquet.
- Paid, only if EOD features prove out: Upstox expired-contract 1-min+OI (6-mo), GDFL/TrueData/Stolo.

---

## 3. LAYER C — Triple-Barrier Labeler (train time)

Grade each historical trigger by its plug's OWN geometry: upper = target, lower = stop,
vertical = horizon/EOD; label +1/−1/0 by first barrier hit. **Add a cost floor** (label +1
only if realized P&L clears STT+brokerage+slippage). Our `engine/exits.simulate_position`
already IS this walk. **New refinements:** (a) label in forecast-σ units, not fixed %;
(b) sample-uniqueness weights for overlapping windows; (c) tag whether the trade **crosses
overnight** (VRP is overnight-positive / intraday-negative — a first-class label split).

---

## 4. LAYER D — Meta-Model  →  see `AGENT_TRAINING_AND_SCALING.md` §T-next for the full training design.

Summary: one **LightGBM** primary over [agent scores · context · strategy_id] → target
after-cost win → **probability calibration** (Platt→isotonic→Venn-Abers, Mondrian buckets) →
threshold p* chosen on **purged CPCV** by after-cost EV. Per-stock and per-strategy behavior
emerge from ID features + tree splits (shrinkage), never separate models.

---

## 5. LAYER E — Pluggable Filter Stack   [task 4]

The single most-requested new capability: **filters are independent, user-toggleable gates.**
Turning a filter OFF removes it from the result path entirely (no effect on candidates).

### 5.1 The interface
```python
class Filter:
    id: str                     # "regime_gate", "ban_gate", ...
    default_enabled: bool
    kind: str                   # "veto" (drop candidate) | "multiplier" (scale P(win)/size) | "annotate"
    def applies(self, cand, ctx) -> bool          # is this filter relevant to this candidate?
    def evaluate(self, cand, ctx) -> FilterResult # {passed, factor, reason}
```
- **Config-driven toggles** live next to the agent config, e.g. `quant_config.json`:
  ```json
  "filters": { "regime_gate": {"enabled": true},
               "forensic_avoid": {"enabled": false},
               "overnight_hold": {"enabled": true, "params": {"block": true}} }
  ```
- A disabled filter is **skipped** — never evaluated, never logged as a veto. (Same pattern as
  the agent tree's per-node `enabled` toggle, so the UI drill-down and config plumbing reuse.)
- **Veto** filters drop a candidate; **multiplier** filters scale its P(win) or size;
  **annotate** filters only add a column (for study). The stack runs after the meta-model so a
  filter can act on P(win) itself.
- **Every filter decision is logged to the outcome DB** even when it fires — so we can later
  measure each filter's realized value (did turning it on actually help?) and let the data,
  not opinion, decide defaults.

### 5.2 The filter registry (v-next)
| Filter id | kind | what it does | default |
|---|---|---|---|
| `cost_floor` | veto | drop if expected edge < friction stack (STT+brokerage+slippage) | ON (non-negotiable) |
| `regime_gate` | multiplier | down-weight families unsuited to current regime (danger state = backwardation+HV>IV+rising vol-of-vol) | ON |
| `ban_gate` | veto | block fresh entries when FutEq MWPL >90%; flag 80–90% | ON |
| `event_blackout` | veto | no fresh short-gamma into in-expiry events / RBI / Budget / results (uses event-aware calendar) | ON |
| `liquidity_screen` | veto | require ≥50k daily option contracts + ≥100k front-OI + full strike ladder | ON |
| `wall_proximity` | veto | never BUY into call wall / SELL into put wall within 0.6 ATR (already in scanner) | ON |
| `quote_staleness` | veto | drop if per-leg quote age/spread beyond staleness law (e* ≈ 1.7–2.1× spread) | ON |
| `dte_gate` | veto | block fresh shorts ≤5 DTE; flag 10–15 DTE theta knee | ON |
| `vrp_gate` | multiplier | scale short-premium by VRP z; block when HV>IV 3+ sessions | ON |
| `forensic_avoid` | veto | exclude GSM/ESM/T2T + forensic-flag names (avoid-list engine) | OFF (opt-in) |
| `sentiment_veto` | multiplier | down-weight against extreme news/FinBERT (build; unproven) | OFF |
| `overnight_hold` | veto/mult | block or down-weight positions that would cross overnight (VRP intraday-negative) | OFF |
| `anti_spoof` | annotate | flag ghost-liquidity share >40% (intraday only) | OFF |

Filters graded S (speculative) in the catalog ship **OFF by default** — they exist, users can
switch them on, and the outcome DB measures whether they earn their place.

---

## 6. LAYER F — Portfolio Selector & Execution
Rank all surviving candidates across all plugs by calibrated P(win); take top-K per the daily
budget; size ∝ (P−p*), vol-targeted, ¼-Kelly-capped, CVaR-limited on t(5) scenarios; apply
session/portfolio/drawdown risk limits (non-negotiable, code the model cannot override).
Execution = broker router (existing), fills logged with four timestamps for slippage learning.

---

## 7. What changed vs the earlier architecture (delta for review)
1. Strategies are now a **registry of plugs** with a shared `TriggerEvent` contract (was: two hard-wired scanners).
2. Added a **pluggable, user-toggleable filter stack** as a first-class layer (was: hard-coded vetoes only).
3. Feature enrichment gained **6 new module families** (sector/cross-stock, institutional, insider, gap/pre-open, expanded regime, microstructure) + the free India data-layer sources.
4. Labeler gains **cost floor, σ-unit barriers, uniqueness weights, overnight/intraday split**.
5. Corrections folded in: **VPIN dropped, FutEq OI for bans, VRP split, max-pain demoted, era tags mandatory** (see catalog §0).

## Change log
| Date | Change |
|---|---|
| 2026-09-15 | v-next blueprint: plug-in strategies, pluggable filters, new feature modules, India data-layer, corrections. |

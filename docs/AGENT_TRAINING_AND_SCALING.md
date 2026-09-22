# Training & Scaling the Agent Tree

> **Status:** T0 + T1 SHIPPED (2026-09-10, forward-collection + win-probability);
> T2/T3 still design. See §10 for the shipped pipeline.
> **Owner:** Vivek (vivektr@insigniaconsultancy.com)
> **Last updated:** 2026-09-10

---

## 1. What "training" means for THIS system

Our agents (`quant/agents/*`, ~22 leaves under 5 families) are **deterministic
scoring functions**, not neural networks: each leaf reads market data and
returns a 0–100 score; the tree combines them as a **weighted average** with
per-agent `weight_buy/weight_sell`, `threshold`, and `veto_below` — all already
externalized in `quant_config.json` (`quant/config.py`), including per-symbol
overrides.

That architecture choice (rules as features + a thin configurable combiner) is
exactly the professional pattern: **the leaves are feature generators; the
TRAINABLE part is the thin layer on top.** So "training" here means, in order
of value:

| Surface | What gets learned | How it deploys |
|---|---|---|
| **1. Tree weights** | `weight_buy/weight_sell` per leaf (which agents actually predict wins) | written into `quant_config.json` — **zero code change** |
| **2. Thresholds & vetoes** | `signal_threshold`, per-agent `veto_below` floors | same config file |
| **3. Meta-model gate** | P(win) classifier over the leaf scores — accepts/sizes signals | one new "leaf"/gate consuming the tree vector |
| **4. Leaf parameters** | e.g. SMC buffers, killzone table, OI thresholds | per-leaf constants; touch LAST (see §6) |
| **5. LLM layer** | nothing to train — prompts/tools (and LoRA adapters if ever needed) | see §7 |

The industry name for surface #3 is **meta-labeling** (López de Prado,
*Advances in Financial Machine Learning*): the primary system (our tree +
triggers) decides *direction*; a secondary model learns *which of those signals
to trust and how big to bet*, trained on the primary's own historical outcomes.
It filters false positives and raises F1 without touching the primary logic —
i.e. **the agents themselves never need retraining**.

## 2. The training data we ALREADY collect (our biggest asset)

Every accepted signal is logged with its **full per-leaf score breakdown**:

- `paper_trades.jsonl` — each `paper_entry` carries `symbol, direction, price,
  score, threshold, strategy, segment` and the entire `tree` dict (every leaf's
  score/available/detail) at decision time.
- `state/paper_positions.json` / ExitEngine — each position carries
  `entry_price, stop, target1/2, exits[{price, pnl_per_share, reason}],
  realized_pnl_per_share`, i.e. the **outcome**.
- New since the strategy work: `strategy` + `segment` tags on every position,
  and the watchlist gives **per-strategy P&L-since-add** — a per-strategy
  scorecard for free.

What's missing is only the **joiner + label**:

- **Labels**: our stop/target/EOD exits are exactly the **triple-barrier
  method** (hit target = +1, hit stop = −1, time-out = 0/return-based). The
  exits list already records which barrier fired.
- **Dataset row** = `[leaf_score_1 … leaf_score_N, regime features (VIX bucket,
  risk_score, killzone), strategy, segment] → label`.

### Volume problem and its fix: REPLAY

Live paper trading accumulates maybe tens of signals/week — too few to fit 22
weights honestly. But the leaves are **deterministic functions of stored data**
(candles, chain snapshots via `state/iv_store`, `state/oi_history`,
`state/bhavcopy`, daily history). `Scanner.simulate_day()` already replays
days. Extending replay across months × 209 symbols generates **thousands of
labeled rows offline** — no waiting for live data. This is the single highest
-leverage build item.

## 3. The training pipeline (build order)

### Phase T0 — Outcome joiner (the dataset builder)
`training/dataset.py`: join `paper_entry` rows to their position outcomes
(match `symbol` + `trigger.bar_time`, as `engine/pnl.trade_detail` already
does), flatten the tree into leaf-score columns, attach the triple-barrier
label. Output: one parquet/CSV. Also a `training/replay.py` that drives
`simulate_day` over historical data to mass-produce rows.

### Phase T1 — Fit the tree weights (shallow, honest)
The tree is a linear combiner, so weight-fitting is logistic regression:
`P(win) = σ(Σ wᵢ·scoreᵢ)` fit on the dataset → normalize coefficients →
write `weight_buy/weight_sell` per leaf into `quant_config.json`.
Alternative: Bayesian/Optuna search directly over config weights with the
walk-forward P&L as the objective (slower, but optimizes what we actually
care about). Either way the deployment artifact is **just the config file**.

**Validation — non-negotiable rules** (this is where ML funds die, per López
de Prado's "10 reasons" paper):
- **Walk-forward only**: train on a rolling window, test on the *next* period,
  roll forward. Never shuffle time.
- **Purge + embargo**: drop training samples whose outcome window overlaps the
  test window (our trades resolve intraday, so a 1-day embargo suffices).
- **Baseline gate**: new weights ship only if they beat the current config
  out-of-sample by a margin. Otherwise keep the hand-tuned defaults.
- Keep it **shallow**: 22 coefficients + regularization (L2, shrink toward
  current config values). No deep nets on thousands of rows.

### Phase T2 — Meta-model gate (the real win-rate lever)
Train a small gradient-boosted classifier (or even regularized logistic) on
the same rows: input = leaf scores + regime features; output = P(win).
Deploy as a **gate + sizer**: signals pass only if P(win) ≥ p*, position size
scales with probability (fractional Kelly-style). Implement as either a
`meta` leaf with `veto_below`, or a gate inside `scanner.evaluate` after the
tree — the leaves stay untouched. Refit monthly via the same walk-forward
harness; the model is small enough to train in seconds.

### Phase T3 — Online strategy allocation (bandit layer)
We now run multiple strategies (intraday / swing / oi / manual) with
per-strategy P&L tracked. Allocating attention/capital across them is a
**multi-armed bandit / online ensemble** problem: keep per-strategy weights
updated from realized outcomes (e.g. exponentially-weighted returns or
Thompson sampling), decaying stale performance. New strategies start with a
small exploration budget instead of a from-scratch backtest. (See
performance-bounded online ensembles, PB-OEL, arXiv 2503.15581.)

## 4. Scaling: new agents WITHOUT retraining from scratch

This is where the architecture pays off. The recipe for any new leaf agent:

1. **Plug in** — drop `quant/agents/<new>.py` with `build()`; the registry
   auto-discovers it. Weights/thresholds live in config, so it ships
   **disabled or weight-0**.
2. **Shadow mode** — run it with `weight=0`: it scores every signal and is
   logged in the tree breakdown but cannot affect decisions. It accumulates
   live evaluation history for free.
3. **Backfill** — because leaves are deterministic, run the replay harness to
   compute the new agent's scores over the SAME historical rows the model was
   trained on → the new feature column appears across the whole dataset
   instantly, no waiting.
4. **Warm-start refit** — the trainable layer is shallow (linear weights /
   small GBM). Adding one column does not invalidate the others: initialize
   the new weight at 0/neutral, keep existing weights as the regularization
   prior, refit the top layer only. **Minutes of compute, not a from-scratch
   campaign.** (For the GBM meta-model, simply refit on the widened matrix —
   it's seconds of training either way.)
5. **Gate + promote** — if walk-forward says the new agent adds value, ship
   its learned weight in config; if not, it stays weight-0/disabled. Nothing
   else in the system changed.

The same pattern covers **new strategies** (a new signal generator like swing
was): tag it (`strategy="x"`), paper-trade it in parallel, let the watchlist
per-strategy P&L be its live scorecard, and let the bandit layer (T3) grow its
allocation from evidence. And **new symbols/segments** inherit global weights
via the existing per-symbol override hierarchy — only override where evidence
justifies it (hierarchical shrinkage: global → segment → symbol).

## 5. Cadence & drift

- **Refit**: monthly (or after every N≥300 new labeled rows), always via the
  walk-forward harness with the ship/hold gate.
- **Monitor drift**: track each leaf's rolling hit-rate contribution; a leaf
  whose learned weight collapses across two refits is flagged for review
  (regime change or broken data source).
- **Never** silently co-tune leaf params and weights in the same cycle —
  change one layer at a time or attribution becomes impossible.

## 6. What NOT to do

- ❌ End-to-end RL or deep networks over raw candles — our sample sizes are
  4–5 orders of magnitude too small; that's the classic failure mode.
- ❌ Random/shuffled cross-validation on time series (leakage → fake edge).
- ❌ Retuning the 30+ SMC/OB constants by grid search on the same data used
  for weights (curse of dimensionality + overfit). Leaf params only change
  with a hypothesis, and are validated on data the weights never saw.
- ❌ Deleting the hand-tuned defaults — they are the prior and the fallback.

## 7. The LLM agents (assistant + MacroLLM)

No gradient training needed or wise here:
- Improvement path = **better tools + prompts + few-shot examples** in
  `assistant/guardrails.py` / `macro_job.py` prompts; measure with the audit
  log (validator pass-rate, tool-use correctness).
- The MacroLLM leaf participates in the SAME weight-training as any other
  leaf — if its bias×confidence scores predict outcomes, T1 raises its
  weight; if not, it decays. That is how the LLM's macro read earns (or
  loses) influence, with zero fine-tuning.
- If task-specific fine-tuning ever becomes justified: **LoRA adapters** on
  Qwen3.5-9B — small, swappable per task, never from-scratch; the base model
  and the serving stack stay untouched.

## 8. Build order & effort

| Step | Effort | Value |
|---|---|---|
| T0 dataset builder + replay harness | ~1–2 sessions | unlocks everything |
| T1 weight fitting + walk-forward validator | ~1 session | tree stops being hand-tuned |
| Shadow-mode + backfill convention for new agents | trivial (config) | new agents cost ~0 training |
| T2 meta-model gate + probability sizing | ~1 session | biggest win-rate lever |
| T3 bandit strategy allocator | ~1 session | scales to many strategies |

## 9. Agent-PRIMARY signals (shipped 2026-07-12): the tree as the trigger

`engine/agent_scanner.py` inverts the confirmation architecture: on its own
cadence it scores every universe symbol BOTH ways through `scanner.evaluate`
and fires from scores alone — no OBS/Supertrend trigger. Deterministic v1
rules (no training needed): score ≥ 70 (`AGENTS_MIN_SCORE`, above the 60
confirm bar) AND ≥ 10-point edge over the opposite direction
(`AGENTS_MARGIN`) AND a rising-edge crossing (a symbol parked above the bar
alerts once, not every sweep) AND veto-clean, with per-side cooldown and a
per-sweep cap. Signals are paper-traded as `strategy="agents"` so the
per-strategy scorecard accumulates the labeled outcomes — **this is the T0
evidence loop running live**. LIVE orders stay blocked until
`AGENTS_PRIMARY_LIVE=1`.

Accuracy ladder from here (maps onto §3):
1. **Now (v1)**: hand-rules above — evidence collection, zero risk.
2. **T1**: fit per-leaf weights ON PRIMARY OUTCOMES (replay + the live
   `strategy=agents` trades). The confirmation-tuned weights are the prior,
   not the answer — a leaf that confirms well may trigger poorly.
3. **T2**: replace the threshold pair with the meta-model gate:
   P(win | leaf vector, regime) ≥ p* becomes the trigger. This is the "most
   accurate" configuration: same leaves, learned decision boundary.
4. **T3**: the bandit allocates between indicator-primary and agent-primary
   by realized P&L — the two strategies compete on evidence, not opinion.

## 10. Implementation — SHIPPED (`quant/training/`, 2026-09-10)

T0 (dataset) and T1 (weight fit + walk-forward + baseline gate) are built. The
chosen configuration is **forward collection + win-probability objective**, at
the **family level first** (5 weights × 2 directions = 10) rather than all ~22
leaves — honest for the data volume we actually have; the sample schema stores
every leaf score too, so a leaf-level fit is a drop-in later.

Both paths are built — **backtest replay is the primary one** (it uses history we
already have), forward collection adds unbiased live data over time. The
per-stock candle archive that makes replay possible lives in **Hyena_X**
(`swing_hyena_in/test_pipeline/data/raw/dhan/intraday1m_<SYM>.json`): 26 F&O
stocks of 1-minute OHLCV, 2026-07-01..08-10, resampled to 5m. Daily bars are
derived from the same series; VIX from `state/vix_daily.json`. What *can't* be
replayed is the intraday option chain — `dhanopt` is empty and Shoonya serves
the chain only live — so the chain-dependent agents (`snr` zones, `volatility`
IV, `volume` OI/orderflow) SKIP during replay and keep their defaults; the
candle-based agents (`smc`, price parts of `volume`/`snr`) train fully. That is
the honest split, and it's why forward collection still matters (it's the only
way those chain agents ever get trained).

### Modules
| File | Role |
|---|---|
| `collector.py` | fail-silent tap; the scanners call `record(result, source)` for EVERY evaluated (symbol, direction) → `state/agent_training/samples.jsonl`. Flattens the score tree; families=None when skipped (never 0). Disable with `AGENTS_TRAINING_COLLECT=0`. |
| `labeler.py` | attaches the forward outcome by walking the rest of the entry day with `engine.exits.simulate_position` (the real stop/target/EOD rules → triple-barrier label); win = realized pnl > cost. Candle *providers* (`HubCandlesProvider`, `CsvCandlesProvider`) make it work live or from `fetch_history.py` CSVs. Fixed-horizon fallback (`method="horizon"`). Idempotent. |
| `bootstrap.py` | one-shot warm start: joins existing `paper_trades.jsonl` `paper_entry`(features)↔`paper_exit`(pnl, summed over partials) into labeled samples (biased — accepted-only). |
| `replay_hub.py` | offline DataHub over the Hyena_X dhan archive: 1m→5m candles, daily (self-derived), VIX — all point-in-time (`set_asof`); chain/futures return None so those agents skip. |
| `replay.py` | the backtest: grid-samples the entry window in both directions, scores each via the real `scanner.evaluate`, labels via `simulate_position`. Writes the same sample schema. |
| `dataset.py` | labeled samples → per-direction, **time-ordered** `(X, y)` with imputation (skipped family → neutral 50) and per-family availability. |
| `trainer.py` | pure-numpy L2 logistic regression (IRLS, no sklearn) → `P(win)=σ(Σβ·score)`; coefs → non-negative, mean-1.0 family weights (a family that predicts losses decays to the floor, never negative). Guardrails: `MIN_SAMPLES=200`/direction, `MIN_PER_CLASS=30`. |
| `validate.py` | the non-negotiables from §3: expanding-window **walk-forward** with a 1-day **embargo**, and the **baseline gate** — pooled OOS AUC of the trained model vs. the *current config weights*. `apply` writes a direction ONLY if it beats baseline OOS by a margin. |
| `__main__.py` | CLI: `status · bootstrap · label · train · report · apply`. `apply` backs up `quant_config.json` first and writes via `QuantConfig.set_weight`. |
| `test_training.py` | offline self-test (no broker): flatten, collector round-trip, labeling, signal recovery, walk-forward SHIP, guardrail refusal. |

### Two ways to fill the dataset — BACK-DATA first, then forward
Both write the identical sample schema, so they mix into one training set.

**A. Backtest / replay (primary — use the historic data we already have):**
`replay.py` + `replay_hub.py` replay the real `scanner.evaluate` over the
Hyena_X 1-minute archive (26 F&O stocks, 2026-07-01..08-10, resampled to 5m),
labeling each scored (symbol, direction) with `engine.exits.simulate_position`.
Point-in-time: features from the candle prefix, daily/VIX as-of gated, chain
absent → those agents skip. ~19k labeled samples in ~13 min.
```
DHAN=<...>/Hyena_X/swing_hyena_in/test_pipeline/data/raw/dhan
python -m quant.training replay --dhan-dir "$DHAN" \
       --vix <...>/state/vix_daily.json --stride 5 --fresh
python -m quant.training report          # inspect (writes nothing)
python -m quant.training apply --yes     # if it beats current config OOS
```

**B. Forward collection (adds unbiased live data over time):**
```
# run the app; the agent scanner logs every scanned candidate automatically.
python -m quant.training label --csv-dir data/history   # after fetch_history 5m
python -m quant.training report
python -m quant.training apply --yes
```
Warm start from existing paper trades: `python -m quant.training bootstrap paper_trades.jsonl`.

The apply gate is the same for both: >=200 samples/dir, both classes, and the
fit must beat the current config out-of-sample (walk-forward).

**State today:** 22 real samples imported from both projects' paper logs
(6W/16L) — correctly BELOW every guardrail, so the pipeline reports and holds
the hand-tuned defaults. It starts producing shippable weights once forward
collection accumulates ~200+ labeled trades per direction.

### Not yet built (unchanged from the design above)
Leaf-level weights (schema already captures them), regime features in the
vector (VIX bucket / killzone), T2 meta-model gate + probability sizing, T3
bandit allocator, and hierarchical per-symbol shrinkage.

---

## 11. T-next — the full multi-layer training pipeline (from the research corpus)

This is the concrete "how everything gets trained" design that the v-next architecture
(`INTRADAY_PIPELINE_Vnext.md`) plugs into. Feature evidence grades are in
`DATA_CATALOG_FOR_TRAINING.md`. Everything here is a step up from the shipped T0/T1, not a
rewrite of it — same collector/labeler/validate spine, richer feature vector and a real
meta-model on top.

### 11.1 What has "layers", and what each layer's training actually is

| Layer | Model / form | How it is trained | Retrain cadence |
|---|---|---|---|
| **Rule sub-agents** (smc/snr/vol/volume/macro) | deterministic scoring functions | NOT trained — they are *feature generators*. Their code changes only on a hypothesis, validated on data the weights never saw. | never (code changes only) |
| **Agent-family / leaf weights** | linear combiner (weighted avg) | logistic P(win)=σ(Σβ·score) → non-negative mean-1.0 weights, per direction, per-stock via shrinkage-to-global-prior. **Shipped (T1).** | nightly refit, ship-gated OOS |
| **Meta-model** (the win-rate lever) | **LightGBM** over [agent scores · full context · strategy_id · setup_id] | gradient boosting on after-cost triple-barrier labels; trees create per-stock/per-strategy/per-regime splits automatically. | nightly/weekly on rolling window |
| **Probability calibration** | Platt → isotonic → Venn-Abers, Mondrian buckets | fit on a held-out fold after the meta-model; buckets = family×DTE×IV-tercile×event-flag×ban-proximity | with each meta-model refit |
| **Filter stack** | toggleable gates/vetoes | not "trained" — but each filter's realized value IS measured from the outcome DB (turn-on-vs-off lift); data sets defaults over time | monitored |
| **Portfolio selector / sizing** | ¼-Kelly + CVaR-LP on t(5) scenarios | (p,b) estimated per strategy from realized outcomes; a **contextual bandit** (not deep RL) can tune the p* threshold + allocation later | monthly |

**No deep RL for entries** (sample sizes 4–5 orders too small; LTR/meta-labeling dominate).
RL — if ever — is only for *execution* policy after the outcome DB matures. LLM leaves are
features/vetoes, never in the hot loop.

### 11.2 The meta-model feature vector (concrete, meta-labeling-relevant)   [task 3]
Log at entry, per candidate (this IS the training row):
```
[ agent-family scores (5) · leaf scores (~22) ·
  strategy_id, setup_id ·                                     # who fired
  regime state (VIX pctile, VRP, term slope, vol-of-vol, Hurst, ADX, HMM posterior) ·
  sector/RS/β, de-macroed residual, breadth, implied-corr ρ ·
  institutional (participant-OI Δ, FII/DII flow + deceleration, futures basis, CPIV) ·
  options (VRP overnight/intraday, signed+unsigned GEX, wall-distance-EM, skew z, term slope) ·
  TIMING_STATE (dte, dte_at_event, days_to_event, days_to_budget, weekday, entry_time_bucket,
                is_tom_window, scheduled_gap_nights, crosses_overnight) ·
  microstructure (spread ticks & σ, depth, Kyle λ, quote_age_ok) ·
  ban proximity (FutEq MWPL), liquidity tier, era_tag ·
  per-leg executable quotes + quote age + displayed size, frictions assumed ]
target = after-cost realized P&L > 0 (triple-barrier)
```
At resolution log: realized P&L, MAE/MFE, exit reason, realized vol over hold, overnight gap,
fill latency, which filters fired. Encode the underlying via features (sector/tier/ban),
**never per-name models** — pool across ~180 names × 12 expiries (~2,000+ trades/yr) for
statistical strength (Sirignano–Cont universality: pooled beats per-name by ~10pts).

### 11.3 Label & cost math (sets the win-rate target)
Triple-barrier in forecast-σ units (h_u∈{1.0,1.5}, h_d=1.0, τ∈{15,30}min), entry at next
bar micro-price + half-spread, upper barrier must clear 2× the all-in rupee hurdle, |r|<c
timeouts → 0 (kept for the meta-model, dropped from the primary), **uniqueness weights
mandatory**. Break-even accuracy p* = ½ + c/(2h): a 5-min futures trade needs ~71% at 1:1,
a 30-min ~58.5% — which is *why* selectivity/abstention, not raw accuracy, is the lever.
**Win rate is a design parameter of (h_u,h_d,τ)** — always report geometry + coverage +
after-cost EV beside it.

### 11.4 Stat-arb / non-randomness signals — concrete construction   [task 3]
Not standalone strategies (retail cost kills fast signals) — they enter the meta-model as
**context features / bias tilts**:
- **Overnight vs intraday drift split** (strongest measured Indian non-randomness): NIFTY
  overnight +0.106%/day vs intraday −0.063%/day (2011–26). Use as a feature ("crosses
  overnight") + split μ_ON/μ_ID in the path/POP engine — *not* a trade on its own.
- **Pairs (options-tilt, not a pairs book)**: rank pairs by Do–Faff **zero-crossings ≥20**
  (cointegration gating is near-vacuous on NSE); OU half-life with Yu bias correction
  (N≥252); Bertram entry a*≈(3c/2)^{1/3} ≈ 0.69σ at NSE costs; **no z-stops** (time-stop +
  regime veto only). Emit pair-z as a bias-score feature for the name being traded.
- **PEAD/SUE**: SUE=(EPS_q−EPS_{q−4})/P; India top-minus-bottom +4.8%/64d. Feature =
  days-since-earnings + SUE sign (a *direction* on top of the results-wave calendar).
- **Call-OI-surge momentum** (~1-week; refereed cousin of "long buildup") and **within-
  industry residual** (test-only; daily version not harvestable at India cost).
- **Encode as non-builds**: index autocorrelation (VR≈1), naive MA/breakout rules
  (0/64 beat B&H net) — so candle/S-R features must be *context*, never standalone triggers.

### 11.5 Calibration + validation stack (non-negotiable)
- **Calibration:** reliability + Brier decomposition per family; Platt (n≈300) → isotonic
  (≥1000) → **Venn-Abers intervals** ("POP 68–74%"); **size on the lower VA bound**.
- **Validation:** purged K-fold + embargo; **CPCV** (N=12,k=3 → 55 paths, report the path
  *distribution* not a point); **PBO < 0.5**; **Deflated Sharpe > 0.95** with a full trial
  ledger and clustered effective-N; ±20% parameter-plateau test on every threshold.
- **Drift/retrain:** ADWIN/Page-Hinkley on streaming Brier; alarms halve size and refit the
  calibrator first; **hard resets at the era-tag break dates** (see catalog §5).
- **Data volume gates:** ≥5,000 non-overlapping labels per barrier geometry before trading;
  n≈616 trades to prove 55% at significance.

### 11.6 Honest ceilings (bake into expectations)
No clean published intraday direction model > ~56% unselected at 1–30-min bars. **Target:
53–56% unselected, 60–65% at 5–10% coverage** — abstention is where win% comes from
(meta-labeling lifts precision ~0.48→0.54). A zero-skill model cherry-picked over 100
configs on 500 trades "shows" 55.7% — the null any headline must beat. OOS Sharpe ≈ 30–50%
of backtest; optimize **expectancy/Sharpe**, treat win% as a diagnostic.

### 11.7 Build order (extends the shipped T0/T1)
1. **Tier-1 features now** (free, years-deep): `ind_close_all` + bhavcopy archives → sector/
   breadth/RS/β, FutEq-OI features, delivery-% family, regime axes, era tags → add as context
   columns in `replay.py`; re-run → measure AUC lift vs the current ≈0.50 baseline.
2. **Swap the linear weight-fit for the LightGBM meta-model** behind the same walk-forward /
   baseline gate; add calibration.
3. **Strategy plugs** (`algo_docx` first) + the **pluggable filter stack**, both logged to
   the outcome DB so their value is measured, not assumed.
4. **Record-first collectors** (pre-open, chain snapshots, depth) running from day one.
5. Contextual-bandit p*/allocation, then (only if justified) the end-to-end encoder as a plug.

## Sources

- [Meta-labeling — Wikipedia](https://en.wikipedia.org/wiki/Meta-Labeling) ·
  [Hudson & Thames: Does Meta-Labeling Add to Signal Efficacy?](https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/)
- [Triple-barrier labeling (López de Prado)](https://www.newsletter.quantreo.com/p/the-triple-barrier-labeling-of-marco)
- [López de Prado — The 10 Reasons Most ML Funds Fail (GARP)](https://www.garp.org/hubfs/Whitepapers/a1Z1W0000054x6lUAA.pdf)
- [Walk-forward validation framework for microstructure signals (arXiv 2512.12924)](https://arxiv.org/pdf/2512.12924)
- [Performance-bounded online ensemble learning via multi-armed bandits (arXiv 2503.15581)](https://arxiv.org/abs/2503.15581)
- [Adaptive alpha weighting for LLM-generated alphas (arXiv 2509.01393)](https://arxiv.org/pdf/2509.01393)

## Change Log

| Date | Who | Change |
|---|---|---|
| 2026-07-11 | Claude | Initial research + design: meta-labeling architecture, replay-based dataset, warm-start scaling recipe, bandit strategy allocation, LLM-layer policy |
| 2026-09-10 | Claude | Shipped T0+T1 as `quant/training/` (forward collection + win-probability, family-level): collector/labeler/bootstrap/dataset/trainer/validate/CLI + offline tests. Walk-forward + baseline gate enforced in `apply`. Revised the replay plan → forward collection (intraday chain history is not reconstructable). See §10. |
| 2026-09-15 | Claude | Added §11 T-next: full multi-layer training pipeline from the research corpus — meta-model feature vector, label/cost math, stat-arb-as-features, calibration+CPCV+DSR validation, honest ceilings, build order. Pairs with `INTRADAY_PIPELINE_Vnext.md` and `DATA_CATALOG_FOR_TRAINING.md` v2. |

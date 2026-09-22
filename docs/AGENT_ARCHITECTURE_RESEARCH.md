# Architectures for Rule-Subagent & End-to-End Signal Models — Validation and Recommendation

> **Status:** REVIEW + RECOMMENDATION — validates two external LLM research reports on
> the topic "per-stock trained weights for rule-based subagents vs. end-to-end learned
> signals," refines them into two buildable architectures, and grounds the observations
> in (a) established literature and (b) this repo's own experiments.
> **Companion diagrams:** `architecture_A_rule_based_subagents.drawio` ·
> `architecture_B_end_to_end.drawio`
> **Owner:** Vivek · **Date:** 2026-09-11

---

## 1. Executive verdict

Both reports converge on the same three pillars, and the convergence is correct:

1. **Keep the predefined rules — as feature extractors, not as the decider.** Rules
   (volatility, OI walls, GEX, structure) are cheap, interpretable, high-recall
   detectors. The literature-standard move is to stop hand-setting their weights and
   let a **meta-model** learn a *conditional* weighting.
2. **Replace "candle that goes up directly" labeling with the Triple-Barrier Method.**
   Your proposed label has lookahead and path-dependency flaws (a rally that first
   dips 3% would have stopped you out). Triple-barrier labels the *tradeable outcome*.
3. **Per-stock weights should come from context + stock identity inside ONE global
   model, not from training a separate model per stock.** Per-stock-only fits overfit
   (this repo has already measured exactly that — §6).

**Report 2 (López de Prado school) is the right backbone — ~90% of it is textbook-solid.
Report 1 (HNS-MMoE) is a research menu, not a build plan** — several components are
real and valuable (shared encoder + expert heads, gating on recent per-rule performance,
uncertainty-aware expert losses), but as a single five-stage system it is over-engineered,
and several of its citations/numbers are unverifiable (§3).

**Recommendation: build Architecture A (Contextual Meta-Labeled Ensemble) first; build
Architecture B (end-to-end TFT-style) second as an additional expert, not a replacement.**

---

## 2. Validation of the two reports

### 2.1 Report 2 ("senior quant desk" — triple-barrier / meta-labeling / LightGBM)

| Claim | Verdict | Notes |
|---|---|---|
| Triple-Barrier labeling | ✅ **Solid** | López de Prado, *Advances in Financial ML* (2018), ch.3. Industry standard. Critically: **this repo's `engine/exits.simulate_position` (stop / target / EOD walk) already IS a triple-barrier labeler** — vol-scaled stop = lower barrier, target = upper, EOD = vertical. |
| Meta-labeling (secondary model filters primary signals for precision) | ✅ **Solid** | AFML ch.3.6; independently replicated (e.g., Hudson & Thames studies). This is exactly your "meta model sees all subagent values" idea — your instinct was right, the literature already named it. |
| Contextual GBM (XGBoost/LightGBM) over rule scores + context | ✅ **Solid** | For tabular, heterogeneous, small-to-mid-size data, gradient-boosted trees remain the strongest baseline (Grinsztajn et al. 2022, "Why do tree-based models still outperform deep learning on tabular data"). Trees splitting on stock×regime×rule = dynamic weights without a weight table. |
| Purged K-Fold + embargo / CPCV | ✅ **Solid** | AFML ch.7. Non-negotiable with forward-looking labels. |
| Sample-uniqueness weights for overlapping labels | ✅ **Solid** | AFML ch.4. Report 1 misses this entirely; it matters (overlapping triple-barrier windows massively inflate effective sample count). |
| Focal loss for rare-setup imbalance | ✅ **Solid** | Lin et al. 2017; standard practice. For GBMs, class weights / `scale_pos_weight` achieve the same. |
| Fractional differentiation | ⚠️ **Real but overrated** | AFML ch.5, mathematically sound. In practice, log-returns + rolling normalization (RevIN-style) capture most of the benefit; treat frac-diff as an ablation, not a prerequisite. |
| HMM regime clustering as feature | ✅ **Reasonable** | Long literature (Hamilton 1989 onward). Start simpler: realized-vol bucket + trend state + time-of-day; add HMM only if it beats those. |

### 2.2 Report 1 (HNS-MMoE — the five-layer hybrid)

| Component | Verdict | Notes |
|---|---|---|
| Mixture-of-Experts + gating network | ✅ **Solid concept** | Jacobs et al. 1991; modern sparse MoE Shazeer et al. 2017. Correct frame for "which rule to trust when." |
| AlphaMix (diversified trading experts, individual uncertainty-aware loss) | ✅ **Real paper** | Sun et al., KDD 2023. The transferable idea: train experts with *individual* losses so they decorrelate — applicable when/if rule heads become learnable. |
| MAML per-stock few-shot adaptation | ✅ **Real method, questionable first choice** | Finn et al. 2017. For this problem, plain hierarchical shrinkage (global prior → per-stock residual) achieves the goal with 10% of the machinery. MAML is a phase-3 experiment, not a foundation. |
| TFT / channel-attention encoders | ✅ **Solid** | Lim et al. 2021 (TFT); RevIN Kim et al. 2022. The Variable Selection Network genuinely is "learned per-timestep subagent weights." |
| DeepLOB-family order-flow nets | ✅ **Real** | Zhang et al. 2019. Only relevant if/when you have depth/LOB data. |
| Neuro-symbolic verification layer | ⚠️ **Sound idea, thin citations** | Keeping hard vetoes symbolic (never buy into a call wall) is good engineering — this repo already does it (`veto_below`, wall-proximity veto). The specific "Logic-Q / program synthesis" citations could not be treated as load-bearing. |
| FactorMoE, ChanFormer, Lamformer, Hi-DARTS, HARL-TRADE, OF-MATNet, EMA-StockPredictor, HybridSwingNet | ❓ **Unverified / niche** | Not established literature; several may be preprint-only or hallucinated. **The recommended design does not depend on any of them.** The one idea worth stealing regardless: condition the gating on *recent per-rule performance*. |
| "HARL-TRADE 42.15% return, 4.19 Sharpe" · "DDQN 73.33% win rate" | ❌ **Do not trust** | The DDQN claim is over **15 trades** — statistically meaningless (95% CI on 73% @ n=15 spans ~48–90%). Headline numbers from single-market, single-period academic backtests essentially never survive costs + replication. Treat all such numbers as marketing. |
| DPO for signal quality | ⚠️ **Speculative** | DPO (Rafailov et al. 2023) is real for LLM alignment; its transfer to trading signals is early-stage. Phase-3 experiment at most. |
| Five-stage training pipeline (SSL → MAML → DPO → NeSy → RL) | ❌ **Over-engineered as a plan** | Each stage adds a failure mode and a tuning surface. No evidence the full stack beats GBM meta-labeling on realistic data volumes. Use it as an upgrade menu (§4.3). |

### 2.3 Validation of *your* two original ideas

- **Idea 1 (rules + meta-model over subagent values):** ✅ correct — it is meta-labeling
  + contextual gating. The refinements you were missing: triple-barrier labels (not
  "big green candle"), context features so weights are regime-conditional, purged CV,
  and shrinkage for per-stock.
- **Idea 2 (one model sees all candles and learns what precedes rallies):** ✅ a real
  architecture family (Architecture B) — but the data-hungriest, least interpretable,
  most overfit-prone option. On intraday equity data, end-to-end nets that beat
  boosted trees + good features are the exception, not the rule (Gu, Kelly & Xiu 2020
  find ML gains come mostly from *features and ensembling*, not depth). Build it
  second, as an expert feeding the meta-model.

---

## 3. Architecture A — for predefined rule subagents (BUILD FIRST)

**Contextual Meta-Labeled Ensemble** — full diagram in
`architecture_A_rule_based_subagents.drawio`.

```
Data (point-in-time) ─► Rule subagents (5 families, score 0–100 + availability)
                     ─► Context vector (vol-regime, time-of-day, expiry distance,
                         rolling per-rule P&L on THIS stock, stock-ID, breadth)
Rule scores + Context ─► LightGBM meta-model ─► P(win)   [trained on triple-barrier labels]
P(win) ≥ p* ─► size ∝ P(win) (vol-targeted) ─► symbolic hard vetoes ─► execution
Execution outcomes ─► labeler ─► nightly refit (purged CV, baseline gate)
```

Key decisions and why:

1. **GBM, not a neural gate.** Tabular features, ~10³–10⁵ samples per regime cell —
   exactly where boosted trees dominate. SHAP gives you *readable* per-stock,
   per-regime effective weights (the client-facing "why did it trade" answer).
2. **Per-stock weights emerge from features, not per-stock models.** Stock-ID +
   context lets one global model express "RELIANCE in high-vol → volatility rule
   dominates." Where a stock has enough history, add a per-stock residual model
   **only if it beats the global model OOS** (this repo's baseline-gate philosophy,
   already implemented in `quant/training/validate.py`).
3. **Labels = the live exit engine.** Use `simulate_position` outcomes as the
   triple-barrier — training target and production exits can never drift apart.
   Add a **cost floor**: label +1 only if P&L clears brokerage+slippage.
4. **Win-rate arithmetic is set by the barriers.** With upper = 2.0σ, lower = 1.5σ,
   breakeven win rate ≈ 1.5/(2.0+1.5) ≈ **43%**. Anything the meta-model adds above
   that is edge. Chasing 70%+ win rates means shrinking the target — vanity, not P&L.
5. **Hard vetoes stay symbolic.** Never learned away: wall-proximity, structure-break
   floor, event blackout. (Already in `engine/scanner.py`.)

**Training recipe (order matters):**
1. Generate samples: replay history through the real tree (`quant/training/replay.py`
   already does this) → per-bar rule scores + triple-barrier outcome.
2. Weight samples by label uniqueness (overlap correction).
3. Train LightGBM with class weights; tune with **purged K-fold + 1-day embargo**.
4. Calibrate probabilities (isotonic/temperature) on a held-out fold; choose p* for
   the precision/frequency point you want.
5. Ship through the **baseline gate**: only if it beats the current config
   out-of-sample (walk-forward), same as the existing `quant/training` apply-gate.

---

## 4. Architecture B — for open-data end-to-end training (BUILD SECOND)

**TFT-style multi-channel encoder with triple-barrier heads** — full diagram in
`architecture_B_end_to_end.drawio`.

```
Channels (returns/frac-diff, volume, OI, IV/GEX, VIX, time) + statics (stock emb, sector)
  ─► Variable Selection Network (learned per-timestep channel weights + RevIN)
  ─► LSTM + multi-head attention temporal core (GRN residuals)
  ─► Heads: P(+1/0/−1) triple-barrier (focal loss) · quantiles · calibrated confidence
  ─► abstain below confidence → policy → same exits as A
```

**Training curriculum:**
- **Stage 1 — self-supervised pretrain** on the full universe (the 713M-row NSE minute
  dataset makes this actually feasible): masked-channel/masked-bar reconstruction.
- **Stage 2 — supervised fine-tune** on triple-barrier labels, focal loss, purged CV.
- **Stage 3 — per-stock adaptation**: freeze backbone, fine-tune last layers + stock
  embedding (few-shot). Try simple fine-tuning before MAML.
- **Stage 4 — calibrate + baseline-gate** exactly as Architecture A.

**Honest positioning:** B's *only* advantage over A is discovering patterns your rules
don't encode. Its costs: needs the pretrain to work at all, near-zero interpretability
(mitigate by logging VSN weights per trade), catastrophic-forgetting risk on regime
shifts, and much higher engineering surface. The literature does not support "end-to-end
beats rules+GBM" as a default on intraday equities — it supports it *occasionally, with
lots of data and careful validation*.

### 4.3 The hybrid (endgame, not the start)
Architecture B's encoder becomes **one more expert** feeding Architecture A's meta-model
(its P(+1) is just another column next to the rule scores). Meta-labeling then arbitrates
between human rules and the learned model per regime. This is the defensible version of
Report 1's HNS-MMoE — same spirit, one trainable stage at a time, each gated OOS.
Phase-3 experiments menu (only after A beats baseline OOS): per-rule-performance-conditioned
gating, AlphaMix-style individual losses on learnable heads, MAML, DPO/RL threshold-policy polish.

---

## 5. Observations & expected results (grounded)

**From established literature (trustworthy):**
- Meta-labeling reliably converts high-recall/low-precision primaries into
  higher-precision systems — F1 gains through *fewer, better* trades (AFML;
  replications). It is the single highest-evidence win-rate lever for setup A.
- Boosted trees over engineered features are the strongest published baseline for
  tabular financial prediction (Grinsztajn 2022; Gu-Kelly-Xiu 2020 for the
  features-over-architecture point).
- TFT is a genuine SOTA *forecasting* architecture with built-in interpretability
  (Lim 2021) — but forecasting skill ≠ trading profit after costs.
- Selective classification / abstention ("only trade when confident") raises realized
  win rate at the cost of trade count — the correct trade, since expectancy is the
  real objective.

**From THIS repo's own experiments (2026-09, 3 F&O stocks, 2022→2024 train, 2025 OOS):**
- Candle-only rule families produced walk-forward **AUC ≈ 0.50** (coin-flip) — the
  meta-model's ceiling is set by feature informativeness, not by architecture. **No
  gating/meta/MoE sophistication fixes uninformative features.**
- The one improvement observed (RELIANCE +6.5% OOS in agent-primary mode) came from
  *trading less* (113→52 trades) — selectivity, not better ranking; n=52 is not evidence.
- ⇒ **Priority order: features first (option-chain OI/IV/GEX, regime, time-of-day),
  meta-labeling second, deep architectures third.** This is also exactly what the
  data supports funding: the chain-dependent rules are the untrained ones.

**Realistic expectations (after costs, intraday Indian F&O):**
- Win rate with symmetric-ish barriers: **48–56%** for a working system; the money is
  in payoff asymmetry + selectivity, not 70% win rates.
- Any paper/LLM claiming 70%+ sustained intraday win rates: assume label leakage,
  micro-samples, or cost-free fills until proven otherwise.
- Success metric to report to stakeholders: **expectancy per trade and profit factor
  at a fixed trade frequency**, with win rate as a secondary diagnostic.

---

## 6. Mapping to this codebase (what already exists)

| Blueprint block | Already in repo | Gap to close |
|---|---|---|
| Rule subagents w/ scores + availability | `quant/agents/*`, tree in `quant/registry.py` | — |
| Triple-barrier labeler | `engine/exits.simulate_position` (stop/target/EOD) | add cost floor param (exists: `cost`) |
| Sample generation (replay) | `quant/training/replay.py` + `replay_hub.py` + HF minute data | extend features: context vector columns |
| Purged walk-forward + baseline gate | `quant/training/validate.py` | upgrade folds → purged K-fold w/ uniqueness weights |
| Per-stock shrinkage | `quant/training/trainer.py` (prior-shrunk logistic) | swap linear model → LightGBM meta-model |
| Meta-labeling layer | — (the linear trainer is a placeholder for it) | **the main build item** |
| Context features (regime/time/expiry/rolling rule P&L) | partial (VIX daily) | **second build item** |
| Architecture B encoder | — | phase 3; pretrain on the HF 713M-row dataset |

**Bottom line:** the repo is already ~60% of Architecture A. The two build items that
change outcomes are (1) the context-feature vector and chain-derived features, and
(2) replacing the linear weight-fit with the LightGBM meta-labeler behind the same
walk-forward gate.

---

## Change log
| Date | Change |
|---|---|
| 2026-09-11 | Initial validation of the two external research reports; Architectures A/B specified; diagrams added. |

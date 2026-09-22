# Strategist Module — Rulebook (distilled from "Executive Summary.pdf")

Source: the 7-page research PDF in the repo root. This distillation is the
implementation contract for `strategist/`. Where the PDF assumes data we
don't have (block trades, time-and-sales, 13F filings), the rule is marked
SKIP with the reason — never fake it.

## Metrics to compute per symbol (from live chain + bhavcopy history)

| Metric | Definition | Source |
|---|---|---|
| OI walls | max Call OI strike (resistance), max Put OI strike (support) | live chain |
| ΔOI per strike | today live `oi - poi` per leg; plus day-over-day series from bhavcopy history (≥10 sessions) | live + bhavcopy |
| ΔOI% | ΔOI / prior OI; flag ≥ +20% builds and ≤ −30% unwinds | both |
| Volume spike | leg volume ≥ 3–5× its 20-session average (bhavcopy) | bhavcopy baseline |
| PCR (OI & volume) | total P/C across the chain window; extreme bands: >1.2 bearish-crowd / <0.6 bullish-crowd (contrarian alert) | live chain |
| Concentration | share of total OI in top-2 strikes per side; high = pinning | live chain |
| Max pain | standard minimization | live chain |
| ATM IV, 25Δ skew | BS inversion (mathutils); skew = IV(25Δ put) − IV(25Δ call) | live chain |
| GEX profile + flip | per-strike gamma × OI × lot × spot²-convention; flip level; dealer long/short gamma zone | live chain |
| Rollout filter | near expiry: −ΔOI front month with matching +ΔOI next month at same strike → neutral roll, EXCLUDE from signals | bhavcopy (both expiries) |
| Z-scores | ΔOI and volume z vs own 20-session history; require >2σ (PDF: 3σ for "unusual") | bhavcopy |
| Block trades / sweeps | SKIP — no time-and-sales from Shoonya | — |
| Filings/dark pool | SKIP — no source | — |

## Footprint templates (PDF signal table → code)

Each footprint: kind, strikes involved, direction, confidence (LOW/MED/MED_HIGH/HIGH), rationale string.

1. **single_leg_build**: one strike, one side: ΔOI% ≥ +20% AND volume ≥ 3× avg → new long call (bullish if strike ≥ spot) / new long put (bearish if strike ≤ spot). Confidence MEDIUM.
2. **straddle_build**: CE and PE at same (near-ATM) strike both ΔOI% ≥ +20%, sizes within 40% of each other → long straddle footprint (vol expansion view). Confidence MED_HIGH.
3. **call_vertical_build**: two adjacent OTM call strikes with same-day ΔOI builds (sizes within 50%) → call vertical footprint (directional-capped view). Confidence HIGH.
4. **put_vertical_build**: mirror on puts. Confidence HIGH.
5. **condor_build**: builds on 2 call strikes + 2 put strikes (wings), center strikes flat (|ΔOI%| < 10%), sizes roughly matched → short iron condor footprint (range view). Confidence HIGH if size match within 50%, else MEDIUM.
6. **unwind**: any wall strike losing >30% OI in a session → position unwinding, potential trend change / wall removal. Confidence HIGH (as a *warning* modifier: weakens that wall's S/R and any range thesis).
7. **pcr_extreme**: PCR_OI > 1.2 or < 0.6 → sentiment extreme, mild contrarian tilt. Confidence MEDIUM (modifier, not standalone).
8. **rollover**: matched −front/+next ΔOI near expiry → no view; EXCLUDE those strikes from 1–5.

Liquidity guard everywhere: a leg counts only if its OI ≥ 500 contracts-equivalent AND it has a live premium; thin chains → footprint confidence downgraded one level; unusable chain → advisor returns "no reliable read" honestly.

## Regime classification (drives which strategies are proposed)

- direction: bullish / bearish / neutral — from net footprint direction, spot vs max-pain pull, put-wall/call-wall headroom asymmetry, futures OI regime if available.
- volatility: iv_rich / iv_cheap / normal — ATM IV vs its self-recorded percentile (state/iv_store), skew steepness as modifier.
- range conviction: strong walls + condor/pinning footprints + inside gamma-positive zone.

## Strategy GENERATOR (primary engine — creates structures, not just picks them)

The recommender does NOT merely choose from a template menu. It runs an
optimizer over the space of leg combinations on the live chain:

1. **View distribution** (view.py): metrics+footprints → a quantitative view
   of the terminal price: lognormal with drift tilted by directional bias
   (bias strength b ∈ [-1,1] → μ shift of b × 0.6σ√T), vol multiplier from
   the vol view (iv_cheap → expect realized > implied: ×1.15; iv_rich →
   ×0.85), optional range compression when wall/pinning conviction is high
   (mixture: w_range × truncated-between-walls + (1-w_range) × lognormal).
2. **Candidate space** (generator.py): all combinations of 1–4 legs from
   liquid contracts (leg OI ≥ floor, live premium), sides ±1, qty 1 (qty 2
   allowed on at most one leg for broken-wing/ratio shapes IF the net
   structure stays bounded). Constraints: bounded max loss (tail-safe via
   net-calls/net-puts sign checks), max_loss ≤ per-lot rupee cap,
   strike span ≤ 6 steps, net premium sanity.
   Vectorized: precompute per-contract expiry payoff vectors on an S-grid
   (numpy), combos = signed row sums; enumerate 1/2/3-leg exhaustively,
   4-leg within structured families (2C+2P, 3+1 same-type broken wings).
3. **Objective**: maximize  E[payoff | view] / max_loss  (view-conditional
   return on risk), subject to floors: POP ≥ 25%, RR ≥ 0.15, and
   cost-awareness (net premium round-trip friction subtracted).
4. **Output**: top 5 structurally-distinct candidates (dedupe by payoff-shape
   correlation > 0.95 on the grid), each re-evaluated EXACTLY via
   payoff.evaluate_strategy, then labeled by a shape classifier: recognized
   classics get their name ("iron condor", "broken-wing butterfly",
   "bull put spread"); everything else = "custom structure" with a payoff
   description. Each carries: legs, credit/debit, max P/L per lot,
   breakevens, POP, reward:risk, E[P&L|view] per lot, rationale linking back
   to the OI evidence that shaped the view.

## Strategy menu (benchmarks only — the generator must beat them)

| Strategy | When proposed | Construction from live chain |
|---|---|---|
| Bull put credit spread | bullish + iv_rich | short put at/below put wall, long put 1–2 strikes lower |
| Bull call debit spread | bullish + iv_cheap | long call ≈ ATM/25-40Δ, short call at call wall |
| Bear call credit spread | bearish + iv_rich | short call at/above call wall, long call above |
| Bear put debit spread | bearish + iv_cheap | long put ≈ ATM, short put at put wall |
| Iron condor | neutral + iv_rich + strong walls, no unwind warnings | short strikes just beyond both walls, wings 1–2 strikes further |
| Long straddle/strangle | straddle_build footprint + iv_cheap (low percentile) | ATM straddle or 1-strike strangle |
| Butterfly (pin) | DTE ≤ 5 + heavy single-strike concentration/max-pain magnet | centered on the magnet strike |

For every candidate: exact legs (tsym, strike, side, premium), net credit/debit,
max profit, max loss, breakevens, **reward:risk = max_profit/max_loss**,
POP (lognormal under ATM IV), margin note, and per-lot rupee amounts (lot from chain).
Rank = regime_fit (0-1) × confidence_weight (LOW .4 / MED .6 / MH .8 / HIGH 1.0) × min(POP·RR quality, capped)
— exact scoring in recommend.py; return top 3 with rationale strings + the "views, not orders" disclaimer and sizing rule (max loss ≤ 2% capital).

## Data honesty rules

- Live ΔOI uses `oi - poi` (poi = previous day close OI from Shoonya quotes).
- Bhavcopy history: NSE UDiFF FO bhavcopy (free), fetched on demand per symbol,
  cached under `state/oi_history/`; if <10 sessions available, z-score rules
  degrade to plain ΔOI% thresholds with confidence downgraded one level.
- Intraday OI is ~3-min lagged & provisional until 16:15 — advisor labels
  intraday reads as PROVISIONAL after 15:00 and before 09:45.
- Everything the PDF sources from block trades/filings is skipped, and the
  report's confidence column already accounts for their absence.

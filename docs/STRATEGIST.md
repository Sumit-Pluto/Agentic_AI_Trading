# Strategy Advisor — OI-Based Option Strategy Generator

Built from the methodology in `Executive Summary.pdf` (repo root), distilled
into `docs/specs/strategist_rules.md`. User picks any F&O stock → the system
scans the option chain + OI history → forms a quantitative view → **generates**
option structures (it does not just pick from a menu) → ranks them by
view-conditional expected profit ÷ max loss, with exact payoff math.

## Pipeline

```
symbol
  │  DataHub.chain_snapshot(span=10)  +  NSE bhavcopy history (free, cached)
  ▼
metrics.py     walls · ΔOI per strike (live oi−poi + 20-session baselines/z-scores)
               PCR (OI+vol) · concentration · max pain · ATM IV · 25Δ skew
               GEX profile + gamma flip · unwind flags · rollover exclusion
  ▼
footprints.py  smart-money templates from the PDF's signal table:
               single-leg build (MED) · straddle build (MED-HIGH) ·
               call/put vertical build (HIGH) · condor build (HIGH) ·
               unwind >30% (HIGH, warning) · PCR extreme (MED, modifier)
               — block-trade signals skipped honestly (no time-and-sales)
  ▼
view.py        bias ∈ [-1,1] · vol multiplier · range conviction
               → terminal-price distribution (tilted lognormal, wall-truncated
                 mixture when range conviction is high)
  ▼
generator.py   enumerate 1–4 leg combos from liquid contracts (numpy-vectorized),
               defined-risk only, liquidity floors, ₹ max-loss cap,
               score = E[payoff | view] − friction, per unit of max loss;
               floors POP ≥ 25%, RR ≥ 0.15; dedupe by payoff-shape;
               finalists re-priced exactly (payoff.py); classic shapes named,
               novel ones labeled "custom structure"
  ▼
recommend.py   top-5 generated + classic-menu benchmarks (must-beat yardstick)
  ▼
service.py     full JSON report: metrics, footprints, view rationale,
               strategies with legs/₹ max P-L per lot/breakevens/POP/RR
```

## Using it

- UI: header tab **Strategy Advisor** → enter symbol → Analyze.
- API: `GET /api/strategist?symbol=RELIANCE` (90 s cache per symbol).
- Offline test: `python tests/test_strategist.py` (synthetic chain with
  planted walls/straddle-build — no login needed).

## Zero-Loss Strategy Stock Search (sub-tab)

Scans every F&O chain for structures whose **worst-case expiry payoff ≥ 0
at EXECUTABLE prices** (BUY legs at ask, SELL legs at bid) **minus friction**
(₹40/leg). This is the *correct* definition of "zero loss" — NOT the YouTube
version where credits == debits ("zero cost"): the video-style broken-wing
(sell 24700c/buy 2×25000c/sell 25600c/buy 26200c at 350/200/60/10) is zero
COST but has a **−₹300/share valley** if expiry lands between the strikes.
Our engine rejects it; only true positive-floor mispricings pass.

- Families: verticals, flies, short flies, broken-wing 4-strike sets
  (incl. the −1/+2/−1/+1 shape), mixed call+put boxes/collars — all
  tail-safe by construction.
- Verified both ways: a fairly-priced chain yields **zero** hits (no false
  free lunches); a chain with a planted mispriced wing is caught with the
  exact ask price on the bought leg.
- **Hardened rules (after a user-verified ABB stale-LTP box false positive):**
  every leg REQUIRES a live two-sided quote (no LTP guessing — market closed
  means the scan honestly finds nothing); +1% slippage haircut per leg on top
  of bid/ask; deep-ITM legs (intrinsic >80% of mid) excluded; box/collar
  families disabled on stock options (physical settlement ≠ arb); no claims
  inside delivery week (DTE < 7). The amber badge now flags WIDE spreads.
- Expectation: hits are RARE and decay in seconds — treat as a shortlist
  for immediate manual verification, not standing free money. "Nothing
  found" is the normal state of an efficient market.
- API: `POST /api/zeroloss {limit?}` · `GET /api/zeroloss` (status+results).

## Honesty notes

- Requires a usable chain: ≥6 liquid strikes (OI ≥ 500 + live premium);
  otherwise the report says "no usable option chain" instead of guessing.
- ΔOI baselines need bhavcopy history (downloads free from NSE; may be
  geo-blocked outside India — degrades to live oi−poi only, with confidence
  downgraded one level as per the rulebook).
- POP is the risk-neutral (lognormal) estimate — a ranking yardstick, not a
  promise. E[P&L | view] is conditional on OUR view being right.
- Output is advisory ("views, not orders" — the PDF's own execution rule).
  Sizing rule attached to every report: max loss ≤ 2% of capital.
- Intraday OI is ~3-min lagged and provisional before 09:45 / after 15:00;
  reports carry a `provisional` flag.

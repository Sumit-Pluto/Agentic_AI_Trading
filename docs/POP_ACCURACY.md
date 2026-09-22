# Strategy Analyzer — POP Accuracy: what was fixed, what remains

> **Status:** IMPLEMENTED (2026-07-12) — the "99.99% POP illusion" fixes.
> Plus the honest backlog of gaps that need data we don't have yet.
> **Owner:** Vivek (vivektr@insigniaconsultancy.com)

## 1. The three fixes that shipped (with the math)

### A. Fat tails — Student-t instead of pure lognormal (`strategist/payoff.py`)

The classic POP assumed log-returns are normal. Real equity returns have
power-law tails: crashes and gap moves happen orders of magnitude more often
than a normal predicts, which is exactly how a wide short condor printed
99.99%.

Now the terminal log-return is **Student-t (default df=5, env
`STRAT_TAIL_DF`; ≤2 disables → lognormal)**:

```
X = ln(S_T/S_0) = μ + s·T_ν      μ = (r − σ²/2)·t      s = σ√t·√((ν−2)/ν)
```

- **Variance-matched** to the IV (`Var X = σ²t`) and **median-matched** to the
  lognormal's drift, so classic and fat-tailed POP are directly comparable —
  only tail mass moves. (A log-t has no finite E[S_T], so matching mean is
  impossible; median+variance is the standard overlay.)
- CDF is exact via the regularized incomplete beta (no scipy).
- Effect is regime-specific and *correct*, not uniformly pessimistic: the
  variance-matched t is peakier in the center, fatter in the FAR tails. Wide
  short structures (breakevens 3σ+ out — the illusion zone) lose POP
  (99.9% → 99.4% before friction); far-OTM crash hedges gain POP; moderate
  structures barely move.

### B. Friction inside POP, not just the score (`payoff.py`, `generator.py`)

A "win" is now `payoff > friction/lot` per share (₹40/leg-lot round trip,
`FRICTION_PER_LEG`), both in the vectorized search POP and the exact POP.
`breakevens_net` reports where the trade covers its costs;
raw `breakevens` stay textbook-gross, as do max profit/loss.

### C. Executable prices, not mid (`generator.py`)

`build_universe` now carries `bid/ask/buy_px/sell_px`: **BUY legs cost the
ask, SELL legs earn the bid** (env `STRAT_SLIP_FRAC` adds extra haircut;
zeroloss keeps its own ±1%). The enumeration uses side-aware payoff matrices,
so every combo is priced as it would actually fill — the ₹0.10-bid/₹5-ask
"₹2.55 butterfly leg" can no longer exist. Benchmarks (`recommend.py`) and
saved-strategy legs go through the same `make_legs`, so they inherit it.
- Spread gates: quotes with spread > 60% of mid are skipped, > 25% flagged
  "(wide spread — verify fill)" — **only when the spread is also > ₹0.50
  absolute**: a 1-tick spread on a ₹0.10 wing is normal and already fully
  charged by ask/bid pricing (the illusion needs an absolutely wide gap).

### D. Smile, first-order (`payoff.py` + call sites)

POP's σ is now the **mean of the legs' own implied vols** when available
(condor wings price a fatter tail than ATM), falling back to ATM IV. The
number used is reported as `iv_used_pct`.

### The optimism gap stays visible

Every structure now carries both `pop_pct` (net, fat-tailed, smile) and
`pop_classic_pct` (the old frictionless lognormal-ATM). The UI shows
"POP (net)" and "POP (classic)". `pop_view_pct` remains the view-tilted
search POP — a deliberate feature for ranking under YOUR bias, never the
headline number (floors are enforced on the exact neutral POP).

## 2. Gaps that need things we DON'T have yet (later updates)

Ordered by expected accuracy gain:

1. **Market-implied density (Breeden–Litzenberger)** — read the risk-neutral
   distribution off the whole smile (∂²C/∂K²) instead of any parametric t.
   *Needs:* dense, clean quotes across strikes (ours are ~10–13 noisy retail
   quotes) + smoothing/arbitrage-free fitting (SVI). The single biggest jump.
2. **Per-symbol tail calibration** — fit `df` per symbol/index from years of
   daily returns instead of a global 5 (indexes ~5–7, single stocks 3–5,
   event-prone stocks fatter). *Needs:* the T0 replay/history dataset
   (see AGENT_TRAINING_AND_SCALING.md) + a nightly fit job.
3. **Outcome-dependent Indian friction** — the flat ₹40/leg-lot ignores:
   STT 0.125% on ITM expiry settlement value (dominates for ITM finishes!),
   assignment on short legs, exchange txn charges, GST, stamp duty, SEBI
   fees. *Needs:* the actual Shoonya contract-note schedule + settlement
   rules per outcome path, then friction becomes f(S_T) inside the integral.
4. **Margin-aware ROI** — score divides by max loss; real capital for short
   legs is SPAN+exposure margin, so credit-structure returns are overstated.
   *Needs:* a SPAN margin calculator or Shoonya's basket-margin API.
5. **Path risk / early death** — POP is expiry-only. A trade that wins at
   expiry can die earlier on margin call, stop-out, or early assignment
   (deep-ITM shorts before dividends). *Needs:* an intraday path model
   (MC on the t-dynamics) + the margin engine from #4.
6. **Event-aware vol** — earnings/RBI/Fed inside the DTE window fatten
   specific dates, not all dates. We already collect calendars in the news
   module — wiring a per-event vol bump into σ(t) is a natural next step.
7. **Term structure / calendar spreads** — we price single-expiry structures
   only; calendars need at least two expiries' IVs and a forward-vol model.
8. **Fill-probability model beyond the spread** — queue position, latency,
   size. *Needs:* our own depth/fill history (the websocket already streams
   depth; start recording it).
9. **Dividend/borrow adjustments** — discrete dividends shift stock forwards
   (puts rich, calls cheap around ex-dates). *Needs:* a dividend calendar
   (Alpha Vantage key exists — partial source already available).
10. **Volatility risk premium** — implied > realized on average, so a
    risk-neutral POP structurally understates seller edge. Realized vol we
    can already compute from candles; calibrating the premium per symbol
    belongs with #2's dataset.

## 3. Knobs

| Env | Default | Meaning |
|---|---|---|
| `STRAT_TAIL_DF` | 5 | Student-t df for POP tails; ≤2 = lognormal |
| `STRAT_SLIP_FRAC` | 0 | extra haircut beyond bid/ask per leg |

## 4. Verification

`python -m strategist.payoff` — t-CDF vs known quantiles (t₀.₉₅,₅=2.015,
Cauchy F(1)=0.75), wide-strangle POP drops under t, crash-put POP rises,
friction monotonically lowers POP, net-breakevens sit inside raw for credit
structures, smile sigma = mean leg IV. Plus generator/recommend/zeroloss
self-tests and tests/test_strategist.py — all green. zeroloss's "fair chain
yields ZERO risk-free structures" invariant still holds.

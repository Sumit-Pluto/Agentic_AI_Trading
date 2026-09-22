# OI-Based Options Strategy — Research Synthesis (July 2026)

Four-agent research sweep: OI signal evidence, defined-risk structures, data
availability, and the adversarial case. 107 sources. Key conclusions below;
this doc is the design basis for the strategy layer.

## 1. The verdict on "risk → almost zero"

**Does not exist. Not as an engineering gap — as a mathematical impossibility.**

- Formal: a strategy with vanishing risk and positive reward is the exact
  object ruled out by the no-arbitrage theorems (Delbaen–Schachermayer NFLVR).
  Claiming one exists = claiming NSE index options are systematically
  mispriced against the most algo-arbitraged flow in India (SEBI: 96–97% of
  prop/FPI profits in the segment are algorithmic).
- Empirical (SEBI's own studies): **89%** of individual F&O traders lost money
  in FY22 (avg ₹1.1 lakh); **93%** lost over FY22–24 (aggregate >₹1.8 lakh
  crore); **91%** lost in FY25 (₹1.06 lakh crore net). Four consecutive years.
- The strategies that *look* safest (high-win-rate premium selling) have the
  worst tails: 4 Jun 2024 (Nifty −5.9%, VIX 9-yr-high regime — both sides of
  the book destroyed within 24h of exit polls), 5 Aug 2024 (VIX +60% intraday
  on BoJ hike — unforecastable from any OI chart), Mar 2020 (VIX >70, circuit
  halts), XIV Feb 2018 (−96% in 50 minutes after years of smooth gains).
- The math that makes it inescapable: for any credit structure of width `w`
  collecting credit `c`, **breakeven win rate = 1 − c/w**. A "90% POP" iron
  condor collects ~10 points per 100 wide → risks 9× its reward. Win rate is
  not edge; it's the shape of the payoff. The correct goal is **defined risk +
  positive expectancy + survivable drawdowns**, never "no risk".

## 2. What OI data is actually worth

| Signal | Evidence quality |
|---|---|
| OI has information content (daily+ horizon) | **Real.** Srivastava (SSRN 606121): stock-option OI predicts Indian spot better than volume. Jena et al. 2019: OI-PCR Granger-causes Nifty at ~12-day cycles — but time-varying, appears/disappears by subperiod |
| 4 regimes (long/short buildup, covering, unwinding) | Universal practitioner doctrine; **zero published NSE backtests**. Also inferential — every contract has a long AND a short; "long buildup" is inferred from price direction, not observed |
| PCR threshold rules (0.7 / 1.5) | Weak. Taiwan study: public PCR insignificant; only non-public order-flow PCR predicts. Retail sees only the weak version |
| OI walls as S/R | No hold-vs-break statistics exist anywhere. Mechanism (dealer hedging) is real but double-edged: pins price in ranges, *accelerates* moves once broken. Partially circular (OI clusters where price already is) |
| Max pain | Academic verdict: pinning is real but tiny (~1% excess pin rate), mostly in illiquid single stocks; useless as a standalone rule |
| Intraday ΔOI as "smart money footprint" | NSE disseminates OI only ~every 3 min, provisional until 16:15 IST reconciliation. Usable for multi-hour swing context, not scalping |
| **The killer caveat** | SEBI's Jane Street order (Jul 2025): expiry-day OI/price patterns were *deliberately manufactured* at ₹36,502 crore net profit scale. The "big player footprints" can be bait |
| Crowding | OI dashboards are free (Sensibull, NiftyTrader, NSE itself). McLean–Pontiff: published signals lose 26–58% of returns. A free public signal carries no equilibrium edge by itself (Grossman–Stiglitz) |

**Design conclusion: OI is a legitimate *context/filter* layer — never a
standalone entry signal. Its highest-value uses: regime confirmation for a
price-based signal, and strike *placement* for defined-risk structures.**

## 3. NSE structural facts (2026) that constrain any design

- Weekly expiries: **only NIFTY (NSE, Tuesday) and SENSEX (BSE, Thursday)**.
  Bank Nifty weeklies are gone — any backtest of 2019–24 BankNifty weeklies
  has no live equivalent.
- Lot sizes (Jan 2026): NIFTY 65, BANKNIFTY 30, FINNIFTY 60. Revised
  periodically to stay in SEBI's ₹10–15 lakh band.
- STT on options 0.10% → **0.15%** of premium (Apr 2026). A 1-lot NIFTY condor
  collecting 25 pts (₹1,625) pays ~₹250–350 round-trip friction = 15–22% of
  max profit — **thin-credit/high-POP structures are largely dead after costs**.
- Margin: hedged baskets get ~60–70% SPAN relief (condor ~⅓ of strangle
  margin); 2% extra ELM on short options on expiry day; calendar margin
  benefit vanishes the evening before near-leg expiry.
- **Stock options are physically settled** — spread legs pinning around
  strikes create delivery obligations. Rule: index options for defined-risk
  structures; exit any stock-option position before expiry week.

## 4. Data plan

| Need | Source | Cost |
|---|---|---|
| Daily per-contract OI (backtests) | NSE UDiFF F&O bhavcopy — verified live: `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip` (cols: OpnIntrst, ChngInOpnIntrst). UDiFF from 2024-01-02; older archives may need an Indian IP | Free |
| Intraday 1-min options OHLCV+OI history | ICICI Breeze API (~10 yrs Nifty/BankNifty, per forum reports) — best free option; else AlgoTest ₹499/mo (7.5 yrs, slippage-modeled) or Stockmock ₹349/mo | Free–₹500/mo |
| Live option chain + OI | Shoonya: GetOptionChain (strike ladder) + websocket `oi`/`poi`/`toi` fields (confirmed in official docs) | Free |
| Proprietary intraday OI history | **Record our own**: subscribe the relevant chain on our websocket and log OI snapshots daily — an asset nobody sells at retail | Free |

## 5. Backtest integrity rules (non-negotiable)

1. Point-in-time strike selection — choose strikes from data available at the
   entry bar; lag OI signals ≥1 dissemination interval (3 min).
2. Model slippage per leg + full 2026 cost stack; skip strikes below an
   OI/volume floor.
3. Expiry-day rules differ (theta collapse, unwind-dominated OI) — model
   separately or stay out.
4. Point-in-time F&O universe from bhavcopies (stock list churns under SEBI's
   2024 criteria — survivorship bias).
5. Pre-Nov-2024 backtests are structurally non-comparable (weekly expiry
   massacre, 3× lot sizes) — weight 2025+ results.
6. Anti-overfitting: count every variant tried (Harvey–Liu–Zhu: demand
   t-stat > 3; Bailey–López de Prado deflated Sharpe); hold out a validation
   period untouched until the design is frozen.

## 6. The two candidate blueprints (to be backtested, not believed)

Both reuse the regime engine already built (Supertrend + ADX in
`indicators/Supertrend_Pullback_v2.pine`, to be ported to Python):

**A. Trend regime (ADX ≥ 20): OI-confirmed directional vertical**
- Entry: Supertrend Pullback v2 signal on the index (price-based, primary)
  + confirmation: futures OI regime agrees (long buildup for longs) and no
  major OI wall within ~0.5 ATR of entry in the trade direction.
- Vehicle: debit vertical (buy 1Δ~40, sell Δ~25) when IV low; credit vertical
  on the opposite side when IV high. Max loss = defined at entry.
- Exit: Supertrend flip, or 50–60% of max profit, or fixed bars.

**B. Chop regime (ADX < 20 + IV percentile high): OI-wall iron condor**
- NIFTY weekly (Tuesday) only. Short strikes at/just beyond the max-Put-OI
  and max-Call-OI walls; wings sized so max loss ≤ 2% of account.
- Enter T-4 to T-2 days, exit at 50% of max credit or T-0 morning — never
  hold through expiry afternoon.
- Hard event filter: no positions over budget/election/Fed/RBI/major expiry
  event days. Wall-break rule: short covering at the threatened wall → close
  that side immediately.
- Honest expectation after costs: modest income, ~65–75% wins, occasional
  full losers; positive expectancy is *not guaranteed* — that's what the
  backtest decides.

**Sizing/kill rules (both):** risk per trade ≤ 1–2% of capital as max loss;
daily loss cap; kill switch flag; paper trade ≥ 1 month before real money.

## 7. Reading list (primary sources)

- SEBI F&O loss studies: Jan 2023, Sep 2024, Jul 2025 (sebi.gov.in)
- SEBI interim order, Jane Street (Jul 2025)
- Jena, Tiwari & Mitra 2019 (MDPI Economies 7(1):24) — PCR causality on Nifty
- Ni, Pearson & Poteshman 2005 (JFE) — strike pinning via dealer hedging
- McLean & Pontiff 2016 (JoF) — signal decay post-publication
- Harvey, Liu & Zhu 2016 (RFS); Bailey & López de Prado — backtest overfitting
- Zerodha Varsity — structures, margins, physical settlement chapters

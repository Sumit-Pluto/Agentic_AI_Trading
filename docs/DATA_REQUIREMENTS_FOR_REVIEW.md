# Data Requirements — Intraday F&O Agent (for review)

**Purpose:** the precise, attribute-level data we need to (1) **train** our models and
(2) **run** the agent live — with an honest source and cost for each. Prepared 2026-09-16.

**How to read the "How" column:**
- **⬇ Download** = a raw feed/file we fetch as-is.
- **⚙ Compute** = we calculate it ourselves (it is NOT available as a download anywhere).
- **⏺ Record** = no history exists; we must start capturing it live from day one.

> ### ⚠ READ FIRST — the truth about options data (this is where sloppiness happens)
> **1. Implied Volatility (IV), Greeks (delta/gamma/theta/vega), skew, IV-surface, GEX/VEX,
> max-pain and PCR are NEVER a download.** They are all **computed** by us (Black-Scholes
> inversion) from: option price + underlying spot + risk-free rate (MIBOR) + time-to-expiry
> + dividends. Any vendor "IV/greeks" is just their computation with their assumptions.
> **2. There is NO free, open-source historical INTRADAY option chain for India** with
> per-strike OI/IV/greeks/bid-ask. It does not exist. Options history that is free is
> **end-of-day only** (from NSE bhavcopy), and even that has **only price/OI/volume per
> strike — no IV, no greeks, no bid/ask** (we compute IV/greeks; bid/ask is simply absent).
> **3. The HuggingFace minute dataset is UNDERLYING (spot) candles only** — its `oi` column
> is ~0 for cash stocks. It gives us #1 below; it gives us **zero option-chain data**.
> **4. Consequences:** deep intraday option-chain history is our **one real paid gap**
> (GDFL/TrueData/Stolo) OR a **record-from-now** item; per-strike **bid/ask & depth** have
> essentially no history and must be recorded; EOD-computed IV/greeks are usable but noisy on
> illiquid strikes (many bhavcopy option marks are *theoretical settlement*, not real trades).

---

## TABLE 1 — TRAINING DATA (historical)

| # | Data & exact attributes | How | Module that uses it (what it does) | Source (+ history depth) | Cost | Priority |
|---|---|---|---|---|---|---|
| **A. PRICE / UNDERLYING** | | | | | | |
| 1 | **Underlying spot candles** 1-min→5-min: open, high, low, close, volume | ⬇ | SMC/structure, volume, momentum agents + meta-model — the price action the system reads | HuggingFace NSE minute set (2022–26, **spot only, no options**) / broker `fetch_history` / paid vendor for deeper | Free (deeper = Paid) | Must |
| 2 | **Daily candles**: O/H/L/C, volume (split/bonus-adjusted) | ⬇ | Daily context (prior-day levels, trend), regime, swing agents | NSE bhavcopy / HF (~2000→) | Free | Must |
| 3 | **Index & sector-index daily + India VIX daily**: O/H/L/C, VIX level | ⬇ | Sector module (trend, relative strength, beta), regime dial | NSE `ind_close_all` file — all indices + VIX in one file/day (~2012→) | Free | Must |
| **B. OPTIONS — RAW (per strike, per expiry)** | | | | | | |
| 4 | **Option EOD per strike**: strike, type (CE/PE), expiry, option O/H/L/C, **settle, OI, ΔOI, volume, underlying price** | ⬇ | OI agents, max-pain, PCR, positioning (raw options data) | NSE F&O bhavcopy (UDiFF) (~20yr). **Contains NO IV, NO greeks, NO bid/ask.** | Free | Must |
| 5 | **Option per-strike bid/ask & depth**: best bid, best ask, spread, bid/ask qty | ⏺ | Liquidity screen, quote-staleness filter, execution/fill realism | **Not in bhavcopy; not in most paid history.** Record from live chain going forward | Free (if we record) | Nice — record now |
| 6 | **Option INTRADAY per strike** (minute-level): OI, price, volume (and ideally IV/greeks) | ⚠ | All options agents at the resolution they actually run live | **No free source.** Paid: Stolo (1-min chains ~4yr), GDFL, TrueData; Upstox expired-API (1-min OHLCV+OI, 6-mo, no greeks/bid-ask) — else ⏺ record-first | Paid / record-first | Nice |
| **C. OPTIONS — COMPUTED (we calculate; never a download)** | | | | | | |
| 7 | **Implied Volatility (IV)** per strike | ⚙ | Volatility agent, IV-rank/percentile, VRP, skew | Computed (Black-Scholes) from #4 (EOD, noisy on illiquid strikes) or live chain + MIBOR rate + dividends | Free to compute | Must |
| 8 | **Greeks: delta, gamma, theta, vega** per strike | ⚙ | GEX/VEX/DEX, hedge ratios, gamma-wall agents | Computed from IV (#7) + Black-Scholes | Free to compute | Must |
| 9 | **Skew / IV-surface / term structure** (25Δ risk-reversal, butterfly, slope) | ⚙ | Skew agents, volatility regime | Computed from #7 across strikes & expiries | Free to compute | Must |
| 10 | **GEX / VEX / DEX, max-pain, PCR, IV-rank, VRP** | ⚙ | Dealer-positioning agents, regime dial, VRP module | Computed from OI (#4) × greeks (#8) + IV history (#7) + realized vol (#1) | Free to compute | Must |
| **D. POSITIONING / FLOW** | | | | | | |
| 11 | **OI dynamics**: build/unwind quadrants, OI velocity, wall migration | ⚙ | OI agents | Computed from #4 / #6 | Free to compute | Must |
| 12 | **MWPL / ban list**: utilization, **FutEq (delta-adjusted) OI** (post-Oct-2025), ban days | ⬇ | Ban-gate filter + ban-squeeze signal | NSE MWPL / `fo_secban` files (years) | Free | Must |
| 13 | **Delivery data**: delivery quantity, delivery % | ⬇ | Institutional-accumulation module (quiet-accumulation detection) | NSE equity bhavcopy `sec_bhavdata_full` (years) | Free | Nice |
| 14 | **Participant-wise OI & volume**: FII/DII/Pro/Client long-short | ⬇ | Institutional-flow module | NSE participant files (daily). **Market-wide AGGREGATE only — NOT per-stock** | Free | Nice |
| 15 | **FII/DII cash flows (EOD)** + **bulk/block deals** (named counterparties) | ⬇ | Smart-money / flow module | NSE / BSE (daily EOD) | Free | Nice |
| 16 | **Insider trades (PIT)** + **promoter pledge (SAST-31)** | ⬇ | Insider module (promoter buying = bullish; pledge = risk veto) | NSE/BSE disclosure feeds (~2020→) | Free | Nice |
| **E. CONTEXT / CALENDAR / MACRO / LABELS** | | | | | | |
| 17 | **Corporate actions + earnings/board-meeting calendar** | ⬇ | Event/calendar module + price adjustment (splits/bonus/dividend) | NSE corporate-action & board-meeting feeds (years) | Free | Must |
| 18 | **Macro / cross-market**: GIFT Nifty, US futures (ES/NQ), USD/INR, DXY, Brent, US 10Y | ⬇ | Macro-overlay module (global risk, sector drivers) | Daily: Yahoo/FRED (free). Intraday: vendor (paid) | Free / Paid | Nice |
| 19 | **News / sentiment**: timestamped headlines + scores | ⚙/⬇ | News/macro agent (catalysts, sentiment) | Paid machine-readable feed or build NLP in-house; **timestamped history is hard/expensive** | Paid / build | Nice |
| 20 | **Our realized trade outcomes**: entry, exit, P&L, exit reason (the labels) | ⏺ | The label source — meta-model learns from what actually happened | Generated internally (paper trading + backtest replay); grows daily | Free (internal) | Must |

---

## TABLE 2 — LIVE DATA (to run the agent in real time)

| # | Live data & attributes | How | Module that uses it | Source | Cost | Update freq | Priority |
|---|---|---|---|---|---|---|---|
| 1 | **Live candles** 1-min/5-min (O/H/L/C/vol), full universe | ⬇ | All candle agents + the signal scanner | Broker WebSocket (Shoonya) | Free (broker) | tick→1-min | Must |
| 2 | **Live option chain — raw**: per strike LTP, OI, volume, **best bid/ask** | ⬇ | OI/volatility agents; exit engine (stops/targets on option legs) | Broker live chain (Shoonya) | Free (broker) | 1–3 sec | Must |
| 3 | **Live IV, greeks, skew, GEX** per strike | ⚙ | Volatility/skew/gamma agents, regime | **Computed in-house** from #2 (Shoonya does NOT stream greeks; Upstox is the one broker that does) | Free to compute | 1–3 sec | Must |
| 4 | **Live futures quote + basis** | ⬇ | Volume/basis module (futures lead cash; informed pre-event flow) | Broker feed | Free (broker) | tick | Must |
| 5 | **Live India VIX** | ⬇ | Regime dial / sizing / filters | Broker feed | Free (broker) | seconds | Must |
| 6 | **Live intraday OI** per contract | ⬇ | OI agents + OI-velocity/build-up | Broker / NSE OI feed | Free | ~1–3 min | Must |
| 7 | **Live index + sector-index ticks** | ⬇ | Sector module (relative strength), market breadth | Broker / NSE | Free (broker) | seconds | Must |
| 8 | **Order-book depth** (5-level bid/ask + qty) | ⬇ | Microstructure filters (liquidity, quote-staleness, spoof/absorption) | Broker WebSocket (5-level; deeper = paid/co-lo) | Free (5-level) | tick | Nice |
| 9 | **Pre-open auction feed** (9:00–9:08 indicative price & imbalance) | ⬇ | Gap module (predict the open ~7 min early) | NSE pre-open API | Free | during 9:00–9:08 | Nice |
| 10 | **Live macro / cross-market** (GIFT Nifty, USD/INR, US futures) | ⬇ | Macro-overlay (opening gap direction, global risk) | NSE/GIFT (free) + vendor for US futures (paid) | Free / Paid | sec–min | Nice |
| 11 | **Live news feed** | ⬇/⚙ | News/macro veto (pause on catalysts) | RSS (free) / paid machine-readable feed | Free / Paid | event-driven | Nice |
| 12 | **MWPL / ban list (daily) + margin (SPAN)** | ⬇ | Ban-gate filter + position sizing (real margin) | NSE files / broker margin API | Free | daily / intraday | Must |
| 13 | **Order & fill stream** (our own orders) | ⬇ | Execution engine + logs every trade to the outcome DB (feeds retraining) | Broker order feed | Free (broker) | real-time | Must |

---

## Summary for approval

- **What we can start with today, all free:** underlying candles (spot), daily + index/VIX
  history, **EOD** option data from bhavcopy (with IV/greeks **computed** by us), MWPL/ban,
  corporate/earnings calendar, and our own trade outcomes — plus the matching **live** broker
  feeds (candles, raw chain, futures, VIX, OI, orders, with IV/greeks computed in-house).
  **This is enough for a first trained system on candle- and OI-based signals.**
- **The one genuine paid gap:** deep **historical intraday option chain** with per-strike
  OI/IV/bid-ask (GDFL / TrueData / Stolo). Needed only to train the *intraday* volatility/skew
  agents on history — decide after the free EOD-based features prove out.
- **Start recording NOW (free, but no history exists and cannot be bought later):**
  live intraday option-chain snapshots, per-strike **bid/ask & depth**, and the **pre-open
  auction** feed. Every day we don't record these is data we can never recover.
- **Key correction vs the earlier draft:** IV, greeks, skew, GEX, max-pain, PCR are **computed
  by us, not downloaded**; and there is **no free historical intraday option chain** — free
  options history is EOD-only and lacks IV/greeks/bid-ask.
- **Realistic note:** more/better data raises the ceiling but does not guarantee win rate;
  after costs, a working intraday system targets ~52–58% win rate with disciplined selectivity.

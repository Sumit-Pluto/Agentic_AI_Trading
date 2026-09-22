# Data Catalog for Model Training — v2 (full research-corpus sweep)

> **Status:** RESEARCH SYNTHESIS v2 — now covers the ENTIRE OptionSmith research corpus:
> T1–T5 + GAP/GAP2 markdowns, the Z.ai SOTA-Data-Stack PDF, `research_doc_3_sep2026/`
> (5 docx incl. insider/smart-money), `more_research_docs/` (5 docx + 8 Z.ai PDFs ~250pp),
> and `research_10_sep/` raw notes (12 notes — **unverified**, their fact-check stage never ran).
> Everything graded. **Owner:** Vivek · **Date:** 2026-09-15
> Legend: **HV** = measured/refereed · P = practitioner-grade · S = speculative/unvalidated ·
> **DNB** = do-not-build · ⚠ = correction to v1. Z.ai-volume hit-rates are synthetic — use their
> formulas as feature templates, never their win percentages.
>
> **How this doc is consumed:** it is the *feature/data evidence base*. The pipeline that
> uses it is `INTRADAY_PIPELINE_Vnext.md` (Layer B = these features; Layer A = strategy plugs;
> Layer E = the pluggable filter registry), and the *training* of the models over these
> features is `AGENT_TRAINING_AND_SCALING.md` §11. Sources graded **S here ship OFF by default**
> as filters/features until the outcome DB proves them.
> **Discarded sources** (Z.ai volumes with synthetic numbers / fabricated citations) are
> quarantined in `../../OptionSmith/Research/_FLAGGED_weak_sources/` with per-file weakness
> notes — do not re-import their percentages.

---

## 0. CORRECTIONS TO v1 (from the deeper sweep)

| ⚠ Item | Correction |
|---|---|
| VPIN | **Demote/DNB** — Andersen–Bondarenko: no incremental power over trade-imbalance + RV; peaked *after* the flash crash |
| MWPL utilization | Ban runs on **FUTURE-EQUIVALENT (delta-adjusted) OI** since Oct-2025, not raw OI — use the FutEq column; raw>100% names traded freely |
| Unsigned-GEX-only | Keep **both signed and unsigned** GEX + zero-gamma flip *zone* (not line); India dealer-sign convention unverified — let the model choose |
| Per-name VRP | **Split overnight vs intraday** — Nifty short-option returns are positive OVERNIGHT, negative INTRADAY (Muravyev–Ni; India-replicated JFM 2024). Intraday-only selling has no unconditional edge |
| Max pain | ≈ coin flip in India (8/17 expiries, 47%); "works" only when VIX<14 AND spot already within 0.5% — OI follows price, doesn't attract it |
| PCR thresholds (1.3/0.7) | No published support; India frequency-domain: volume-PCR predicts at ~2.5-day cycles, OI-PCR at ~12-day (Jena et al. 2019); index put OI is FPI hedging |
| Pairs cointegration gate | Near-vacuous on NSE (10.6% pass vs 5% null); rank by **Do–Faff zero-crossings ≥20** instead; no z-stops (they destroyed the edge); cost-aware entry ≈0.69σ not 2σ |
| OI cadence | Exchange OI refreshes ~1–3 min (site cache adds 70–110s) — OI features live on a 3-min clock, price/IV on tick clock; **never mix clocks in one z-score** |
| GIFT gap | Compare vs prior NIFTY **futures** close, adjust overnight USDINR; direction-of-open ~85–90% but no day-level info; foreign index returns add ~nothing beyond GIFT |
| Straddle EM | Day range ≈ 0.85× straddle contains realized ~68–70% of days (weekly ATM) |

## 1. SECTOR & CROSS-STOCK (v1 core retained, additions ⬇)

Retained: sector index trend/RS/β (via `ind_close_all` archive to ~2012), de-macroed residuals, implied-correlation gauge ρ≈(σ_idx/Σwᵢσᵢ)² + Δρ spike, results-wave position, peer-IV spillover, cohort (sector×vol-band) shrinkage, driver_tag + macro calendars.
**New:**
- **Breadth**: % Nifty500 >200-DMA (<20–30% = capitulation zone), A/D ratio (<0.2), % sector above VWAP. free/deriv. [S but standard]
- **BankNifty constituent nowcast**: HDFCBK+ICICI ≈53% of index — weighted constituent-return vs index residual = lead/lag feature. free. [P]
- **PSU-vs-private bank divergence** (claimed ρ≈−0.045 — verify) as rotation feature; BankNifty/Nifty ratio Bollinger-break. [S]
- **Idiosyncratic-decline veto**: stock −15% while sector flat + no filing = avoid (asymmetric use of relative strength). [S]
- **Overnight global composite**: S&P + Hang Seng + DXY + US10Y + Brent→USDINR chain (multi-flag gate, not single signals). free. [S]
- **Index-vs-basket divergence filter** (Jane-Street signature): index moves >1.5× 30-min ATR while top-6 constituents' flow z<1 → "unsupported move," distrust breaks. [regulator-documented]

## 2. INSTITUTIONAL / FLOW / SMART-MONEY (major expansion)

**Participant-wise OI decoded** (free daily; use CHANGES not levels — FII L/S ratio is hedge-contaminated; verified example: "max bearish" index-fut ratio while net LONG 624k stock-fut):
FII short-put build >10%/day (floor confidence), FII short-call build below 20-DMA, Pro net-position sign-flip, DII long-put spike during stress (contrarian bottom) — plus companion **participant-VOLUME file**. [P/S]
**FII/DII flow dynamics**: level + **deceleration** (2nd derivative), 5-day sell cluster >₹10–15k cr = capitulation-bounce prior, joint FII&DII-both-selling state = the SIP-put failure mode. free. [P]
**Insider layer (PIT Reg 7(2))**: opportunistic-vs-routine classification (same-month ≥3yr = routine, drop it), promoter/KMP *market purchases* only, ≥₹10L & ≥0.01% mcap, cluster (≥2 PANs/10d) amplifier, window-reopen ×1.5 weight; India CAAR +2.27%/20d. free NSE/BSE APIs. [HV method, refereed anchors]
**Bulk/block deal entity tracker**: daily client-name CSV (no cookies!) → per-entity forward-CAR table, follow only positive-history names (follower gets residual drift, front-runner premium is gone); entity resolution (Jaro–Winkler + alias table). free. [HV data / P edge]
**Delivery-% family**: robust z on log DELIV_QTY (60d, MAD), "quiet accumulation" = deliv z>2.5 + DELIV_PER>60% + |ret|<2%; deliv%×price×futures-OI matrix for next-day bias. Standalone deliv% has NO Indian academic validation — composite only. free bhavcopy. [P]
**Pre-event accumulation scanner** ("stocks in play"): CAR(−10,−1)>+5% + volume z>2 + delivery z>1.5 + no announcement; options overlay = OTM-call vol-share z>3 on ≥2/5 days; **SSF-volume tell** — informed pre-earnings flow goes to single-stock FUTURES (refereed NSE). free. [HV]
**Promoter-pledge features**: ≥25% pledged + fresh creations + price near margin-trigger = avoid; INVOKE = crash flag; pledge >50% = veto. free SAST filings. [HV panel evidence]
**Forensic avoid-list** (veto set): auditor mid-tenure resignation, rating ≥3-notch drop / default-keyword rationale, CFO+CS both resign ≤180d, Beneish/Piotroski composite ≥4 pts = uninvestable (scores invalid for banks — use RBI divergence). free. [case-validated]
**Pump-and-dump contra-detector**: microcap +100%/3m + volume z>3 + promotion spike + insider sells = never-buy. [regulator-documented]
**DNB**: politician trade-copier (no transaction disclosure in India), BENPOS, per-stock inference from aggregate participant file, signed DDOI/HIRO.

## 3. OPTIONS-DERIVED (v1 retained; key upgrades)

- **VRP**: split overnight/intraday (⚠ above); measured Nifty anatomy as calibration targets: positive 74.9% days, mean +1.21 vol pts, 25.1% inversion, 1.98× left-tail. [HV]
- **GEX/positioning**: signed + unsigned + zero-gamma zone; GEX 20-session percentile as scalp-regime filter (<30th momentum / >70th fade — refereed at 15–60-min horizons, US); charm-cascade expiry timing table (delta decay accelerating 3%→25%/hr through the day). [mixed]
- **Wall refinements**: wall MIGRATION > static level; broken walls = fuel; delta/premium-weight everything post-Oct-2025 (FutEq era); put-floor integrity = wall + OI-direction interaction.
- **ΔOI×ΔIV 2-bit flow read** (best India substitute for signed volume): ΔOI↑&ΔIV↑ = net buying; ΔOI↑&ΔIV↓ = net writing — confirmation on the 3-min OI clock. [logical construct]
- **Event-crush priors** per event type: Budget 50–70%, elections 70–90%, earnings 40–60%, RBI 30–50%, FOMC 25–40% (S — calibrate from own IV history; NSE monthly stock options crush only ~8–13%).
- **Skew upgrades**: RR25 z>|2| (2y window) as the extreme definition; spot-conditional skew z (residual of RR25 on standardized spot); term-inversion event + normalization as reversal feature.
- **0DTE/expiry**: India VIX is blind to weeklies — build own remaining-variance estimator from the weekly chain; per-weekday variance-time clock φ(t) (U-shaped; price theta in variance time); pin-candidate score (OI conc × 1/(vol×distance-σ)) — ordinal only; EPPI-style pin probability (RND × OI × GEX weight) as template.
- **Intraday chain deltas**: ΔIV_ATM and Δ(25Δ skew) over 5–30 min from own snapshots (never NSE's IV column).

## 4. REGIME (expanded axes)

v1 dial retained (VIX pctile · VRP + HV>IV-3-sessions · term slope · vol-of-vol, hysteresis 80/60). **Add axes:** Hurst (200-bar; <0.45 MR / >0.55 trend) + ADX bands (only fade oversold when H<0.45 & ADX<25; ADX>35 = never fade) + HMM posterior/duration as *features* (2-state priors: P(calm→calm)=0.92, crisis E[dur]≈6d; per-bar intraday HMM = DNB, flips 47–147× vs ~40 true switches) + RV-own-percentile (>95th 5y = capitulation zone) + **first-15-min day-type classifier** (~70% published: gap/ATR, first-15-range vs 10d, 5-min candle, ΔVIX, event dummies — trend days only ~20–25% of sessions). Jump-model lessons (persistence penalties) still rank above HMM. Lagged VIX R²=21.3% for |return| remains the dominant single state variable.

## 5. CALENDAR / EVENT / ERA TAGS (consolidated full list)

Survivors + new: July VIX-down (12/13, p=.003) · January VIX-up (12/12, p=.0005) · Budget ramp/crush (day-after sets real trend; short-straddle exhibit: 79% win but max loss 6× median win) · turn-of-month (tie-breaker) · E+1 rebound (t=2.89) · expiry-day −2.76% VIX (regime-stale) · BANKNIFTY has NO expiry-day effect (refereed) · policy-day pattern: RBI 10:00 decision then presser reversal ~2h; Fed day-*after* sets trend · PEAD/SUE drift (+4.8%/64d India) with days-since-earnings + SUE sign · weekday expiry map dummies (Tue NIFTY-0DTE / Thu SENSEX-0DTE / last-Tue "everything expires").
**Era-tag master list (mandatory sample columns):** 2019-10 physical settlement · 2020-12 peak margin · 2023-07 HDFC merger (pairs) · 2024-10-01 STT#1 · 2024-11-20 lots/weeklies/ELM · 2025-02-01 upfront premium + calendar-margin cliff · 2025-04-01 intraday limits · 2025-09-01 Tue/Thu expiry swap · 2025-10-01 FutEq OI switch · 2025-12-07/08 block-deal revamp + F&O pre-open (futures) · 2026-04-01 STT#2 (0.15% option-sell / 0.05% futures-sell — several docs carry stale figures; verify on a contract note) · 2026-08-03 session to 15:40 + closing-VWAP window (verify circular). ⚠ Our 2022–24-train/2025-test split straddles at least four of these.

## 6. GAP / PRE-OPEN LAYER (new — from gap-prediction doc)

- **⭐ Pre-open auction recorder — the SCARCE dataset**: poll `api/market-data-pre-open?key=ALL` every 15–20s, 9:00–9:08 (IEP, buy/sell qty, matched qty). **NSE does not archive this; it cannot be bought later. Start recording before modeling.** Final IEP ~9:07:55 ≈ actual open → 7-min head start; IEP trajectory variance + last-60s shift = manipulation/thinness features; auction imbalance (US prior: predicts post-open *reversal* of uninformed flow); futures-vs-cash IEP basis (futures pre-open since Dec-2025); GIFT-vs-IEP disagreement >0.2% = unstable-open flag.
- **Gap features**: gap in ATR14 units; gap-quality = size × relative volume × sector breadth; gap-type taxonomy (breakaway/continuation/exhaustion/common by gap-day volume); measured Nifty base rates: ≥1% gap-ups → 29% same-day fill, 55% close below open, fade-toward-VWAP beats fade-to-fill; 78% of fills happen before 11:30; per-stock gap personality (frequency + fill rate persistent); band-clipped state (non-F&O 2–20% bands censor gaps); ADR overnight moves (INFY/HDB/IBN…) as stock-specific gap signals; evening filing-wave features (LODR 5–11pm wave = biggest gap cause).
- **Honesty benchmark**: only published NIFTY gap-fill ML: 59% acc vs 66% naive "always fills" — the naive base rate is the bar.

## 7. MICROSTRUCTURE AT RETAIL LATENCY (what's real vs not)

- **OFI = pressure gauge, NOT forecast** (contemporaneous R²≈65%; 1-min forecast R² negative OOS). Queue imbalance predicts only one tick (not monetizable) — use as gate |I|≥0.3. Micro-price > mid as fair value. Kyle λ as impact/thinness feature.
- **BVC trade classification** (no aggressor flags on Indian retail feeds): CVD swings/divergences only, never levels; ≥10–20% classification error.
- **Actionable composites** [P, thresholds = priors]: sweep-and-thin trigger (quote advances ≥2 levels + ≥80% displayed traded + depth <0.6× median + anti-spoof persistence), absorption-as-OFI-residual (defended level), pull-vs-trade decomposition (pulled wall before break), synthetic-iceberg reload detector (≥3 reloads ≤2s; NSE F&O has NO native hidden orders), metaorder print-follow (square-root impact law), basis-jump front-run (1–30s lead; take only big jumps at 300–400ms retail latency), trapped-trader failed-breakout (Osler stop-cascade in reverse).
- **Anti-spoof gate**: ghost-liquidity share >40% of a side → disable imbalance entries 10 min.
- **Structural limits**: retail = ~1Hz 5-level snapshots (Dhan 20-level free; Fyers ~50-level TBT), order RT 300–400ms vs colo 4–50µs — sub-second edges structurally unavailable; **record own depth to parquet now** (nobody sells historical NIFTY options depth). Cost floor: ATM option scalp ≈1.1–1.6 option pts all-in; futures scalping dead post-Apr-2026 (~14.8 idx pts RT); sub-5-pt scalps structurally unprofitable.

## 8. OI-PREDICTABILITY SCREENS (which names OI signals work on)

Liquidity screen: ≥50k daily option contracts AND ≥100k front-month OI + full strike ladder. Ranked universe: NIFTY > BankNifty > Sensex > RIL/HDFCB/ICICI/SBIN > results-window names > high-beta (crowded, need delivery confirm) > ban-list regulars (trade the ban rule, not the chart). Avoid the ~200-name illiquid tail + F&O additions <2yr. **Ban-squeeze mechanic**: ≥90% FutEq + short buildup (price↓ OI↑ + discount) = squeeze candidate; ban entry = reduce-only forced covering; episodic + shrinking post-FutEq. Rollover% context-only. Call-OI-surge momentum ≈1-week horizon (refereed US; India-adjacent). Straddle-premium day-high break after 13:00 = writer capitulation.

## 9. HONEST CEILINGS (calibration priors for the meta-model)

- No clean published intraday direction model >~56% unselected; target **53–56% unselected, 60–65% at 5–10% coverage** (abstention is the win% lever; meta-labeling lifts precision ~0.48→0.54).
- Zero-skill classifier cherry-picked over 100 configs on 500 trades shows 55.7% — the null to beat. n≈616 trades to prove 55%; ≥5,000 non-overlapping labels per barrier geometry.
- OOS Sharpe ≈ 30–50% of backtest; live decay 15–30%; India institutional Sharpe 1.0–1.5.
- Gates: purged CPCV (report path *distribution*), PBO<0.5, Deflated Sharpe with trial ledger, ±20% parameter-plateau test, evaluate NET of the full friction stack.

## 10. HALLUCINATION FLAGS (never cite)

"HexaDelta Research" (nonexistent, recurs in 3 volumes) · FinBERT R²=0.76 next-day returns (impossible) · SSRN 6416558 (implausible ID) · "Stanford CS224R confirms RL slippage" (course, not study) · Z.ai layer-lift percentages (52–58%→75–82% etc.) · "82% max-pain accuracy" · 89–90% VIX-fade hit rates · "11:15 beats 9:20" study (unverifiable) · buyback pre-announcement CARs. All research_10_sep notes = unverified (their fact-check stage died). Future-dated regulatory specifics (Dec-2025 lots, Aug-2026 close) — verify each circular before encoding.

## 11. PRIORITY ORDER v2

**Tier 1 (free, backtestable years-deep, start now):** `ind_close_all` + F&O/equity bhavcopy + MWPL/ban + participant-OI archives (verified URL patterns, plain GET) → sector/breadth/RS/β features, FutEq-aware OI features, delivery-% family, era tags, regime dial, event calendar + DTE clock.
**Tier 2 (derivable, medium):** implied correlation, VRP panel (overnight/intraday split), CPIV/skew z-family, de-macroed residuals, FII/DII dynamics, insider/PIT + bulk-deal entity tracker, gap features from bhavcopy.
**Tier 3 (record-first — cannot be bought later):** ⭐ pre-open auction recorder · own chain snapshots (3-min OI clock) · own 5–20-level depth to parquet · outcome DB with full TIMING_STATE vector.
**Skip/DNB:** VPIN, per-bar HMM, signed-only GEX, politician copier, sub-second microstructure, native-iceberg detection on F&O, month dummies, z-stops on pairs.

## Change log
| Date | Change |
|---|---|
| 2026-09-15 | v1 — T1–T5/GAP sweep + Z.ai PDF + repo experiments |
| 2026-09-15 | v2 — full corpus: 3_sep2026 docx set, more_research_docs (5 docx + 8 PDFs), research_10_sep raw notes; corrections table; insider/gap/microstructure/OI-screen layers; era-tag master list; honest ceilings; hallucination flags |

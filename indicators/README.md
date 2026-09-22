# Supertrend Pullback Signals v2 [Filtered]

Hardened fork of the "Supertrend Pullback Signals" indicator (from Telegram / @Theindicatorroom).
Original source: `~/Downloads/Telegram Desktop/Indicator Supertrend Pullback Signals for TradingView/`.

**File:** `Supertrend_Pullback_v2.pine` — Pine Script v6, paste into TradingView → Pine Editor → Add to chart.

---

## Flaws found in v1 and how v2 fixes them

| # | Flaw in v1 | Consequence | Fix in v2 |
|---|---|---|---|
| 1 | **No chop/regime filter.** Signals fire in sideways markets. | Supertrend's #1 documented failure mode — whipsaw legs generate "pullback" entries that are just range noise. | ADX filter (default: skip when ADX < 20) + gray background shading over chop zones. |
| 2 | **Zone touch and confirmation candle required on the SAME bar** (`low <= dvz_upper AND close > open AND close > close[1]`). | The classic 2-bar entry (red candle dips into zone → next green candle confirms) was silently **missed**. Only rare single-bar V-reversals signaled. | Zone-touch flag with a validity window (default 3 bars). Touch bar and trigger bar can differ. |
| 3 | **Pullback depth measured leg-extreme → close of the trigger bar.** | By trigger time price has already bounced, so true depth is understated; a spike 3×ATR deep that recovered by the close still passed the "max 2.2" reversal screen. | Depth = leg extreme → **deepest wick** of the retracement, tracked bar by bar. |
| 4 | **Signals evaluated on every tick.** | Labels appeared intrabar and vanished if conditions failed by the close — visual repainting; chart history looked better than live behavior. | All signals gated by `barstate.isconfirmed` — nothing prints until the bar closes. Non-repainting. |
| 5 | **One signal per trend leg, ever.** | A 200-bar trend leg with 5 clean pullbacks gave 1 signal, then went silent until the next flip (where risk is highest). | Re-arm logic: after a signal, a **new leg extreme** re-arms the system. Max signals per leg (default 2) + cooldown (default 5 bars). |
| 6 | **Any green/red candle counted as "momentum confirmation".** | A candle closing in the bottom 10% of its range after a violent rejection still triggered a BULL signal. | Strong-close option (default on): close must be in the top 40% of the candle's range (bull) / bottom 40% (bear). |
| 7 | **No entry-distance guard.** | A giant candle from the zone to new highs signaled at the close — far from the ST stop → terrible risk:reward. | Trigger candle must close within 1.5 × ATR of the Supertrend line (configurable). |
| 8 | **No higher-timeframe context.** | "Pullback" on the 5m chart is often a reversal on the 1h — the top documented pullback-trading mistake. | Optional HTF Supertrend alignment filter using the non-repainting `[1]` + `lookahead_on` idiom (last **closed** HTF bar only). Off by default. |
| 9 | **No volume confirmation option.** | Weak-participation bounces treated the same as institutional ones. | Optional: trigger candle volume ≥ 1.0 × its 20-bar SMA. Off by default (indices have no volume). |
| 10 | **`alert()` called unconditionally with a possibly-`na` message** (flip alerts). | Sloppy runtime behavior; wasted alert evaluation every bar. | Flip alerts wrapped in `if` blocks. |
| 11 | **Flip-bar counter off by one** (`bars_since_flip` reset to 0 then incremented on the same bar). | "Min 2 bars after flip" actually enforced a different number than documented. | Counter is 0 on the flip bar, clean `>=` comparison. |
| 12 | **No ATR floor.** | Early bars / illiquid symbols → tiny or `na` ATR → depth ratios explode. | ATR floored at `syminfo.mintick`. |
| 13 | Telegram ad table + channel branding baked into alerts. | Noise. | Removed. MPL-2.0 license notice retained (the fork requirement). |

## Default settings (tuned for intraday NSE / liquid instruments)

| Group | Setting | Default | Notes |
|---|---|---|---|
| Supertrend | ATR length / factor | 10 / 3.0 | Same as v1. Raise factor to 4 on very volatile F&O to cut flips. |
| PQF | Min / Max depth | 0.3 / 2.2 ×ATR | Depth is now wick-based — if migrating v1 settings, these mean slightly deeper values. |
| PQF | Max entry distance | 1.5 ×ATR | Lower it (1.0) for tighter risk:reward. |
| DVZ | Half-width / touch validity | 0.5 ×ATR / 3 bars | |
| Filters | ADX ≥ 20 (on) | on | The single highest-impact filter. 18–25 is the sensible range. |
| Filters | HTF alignment | off | Turn on for 5m/15m charts with HTF = 60. |
| Filters | Volume ≥ 1.0× avg | off | Keep off for index spot charts (no volume). |
| Signals | Max/leg, cooldown | 2, 5 bars | |
| Signals | Strong close ≥ 0.6 | on | |

## Behavior notes

- **Non-repainting:** signals finalize only at bar close; the HTF filter reads only closed HTF bars. What you see in history is what you'd have gotten live.
- **Conservative by design:** a trigger candle that itself breaks the leg extreme does *not* signal (the value entry is gone; that's a breakout, not a pullback).
- **Suggested stop reference** is in each label's tooltip: the Supertrend line at signal time.
- Before trading it, run it as a TradingView **strategy conversion or manual replay** on your instrument/timeframe and check the signal quality visually — defaults are sane starting points, not optimized parameters.

## Next step for the algo (Shoonya)

TradingView alerts can't call the Shoonya API directly. Two options when we get there:
1. TradingView webhook → small FastAPI/Flask listener → Shoonya `place_order` (needs TV Pro plan for webhooks).
2. Port this logic to Python inside the trading engine itself (supertrend + the same filters over the Shoonya WebSocket feed) — no TradingView dependency, lower latency, fully backtestable.

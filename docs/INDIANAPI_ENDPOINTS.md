# Indian API (stock.indianapi.in) — endpoint reference

Auth: header `X-Api-Key: $INDIANAPI_KEY` · Budget: **500 req/month free** —
all calls MUST go through `news/indianapi.py`'s budget guard (cap 480,
persisted monthly counter in `state/indianapi_budget.json`).

## Used now (news module)

| Endpoint | Use | Cache TTL |
|---|---|---|
| `/news` | Indian market news supplement to RSS | 30 min, market hours only |
| `/ipo` | IPO calendar (corporate panel) | 24 h |
| `/corporate_actions` | dividends/results/bonus/split extraction | 24 h |
| `/recent_announcements` | corporate announcements | 1 h |
| `/commodities` | commodities fallback for the tape | 1 h |
| `/trending` | trending stocks (optional UI chip) | 1 h |

## Available, promising for later phases

| Endpoint | Future use idea |
|---|---|
| `/price_shockers` | scanner enrichment: unusual movers → priority evaluation |
| `/stock_forecasts`, `/stock_target_price` | analyst-consensus agent in the quant tree (sentiment family) |
| `/historical_data`, `/historical_stats` | backtest data cross-check vs Yahoo/Shoonya |
| `/NSE_most_active`, `/BSE_most_active` | liquidity/participation context |
| `/fetch_52_week_high_low_data` | 52-week breakout context agent |
| `/statement` | fundamentals for a future fundamental agent |
| `/search`, `/industry_search`, `/stock` | symbol→industry mapping (would upgrade sector tagging from keywords to exact) |
| `/mutual_funds*` | not relevant to this system |

## Budget guidance

~480/month ≈ 16-21/day. Current plan spends ~15-20/day. Any new endpoint
usage must fit inside that or reduce another consumer. `/stock` per-symbol
sector lookups are tempting but 200+ symbols would eat half a month —
only do it as a one-time backfill spread over weeks, cached forever.

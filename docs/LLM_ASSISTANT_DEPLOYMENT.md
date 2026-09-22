# LLM Assistant — RunPod Deployment & Integration Guide

> **Status:** PROJECT SIDE BUILT & MOCK-TESTED — awaiting the RunPod endpoint.
> **Owner:** Vivek (vivektr@insigniaconsultancy.com)
> **Last updated:** 2026-07-10
> **Audience:** server/deploy team. This doc is the single source of truth for the
> LLM assistant stack. Edit it in place when anything changes and update the
> Change Log at the bottom.

---

## 0. Implementation status (2026-07-10)

The **entire `assistant/` package is built and verified against a mock LLM** —
it goes live the moment `LLM_BASE_URL` / `LLM_API_KEY` are set in `.env` (model
defaults to `Qwen/Qwen3.5-9B`).

| Delivered | File | Verified |
|---|---|---|
| LLM client + mock | `assistant/client.py` | model = `Qwen/Qwen3.5-9B` |
| Tool layer (13 tools) | `assistant/tools.py` | positions, recent_trades, **get_score (segment-aware)**, quote, vix, option_chain, candles, news_macro, **swing_signals**, **oi_signals**, **watchlist (per-strategy P&L)**, search_docs |
| RAG over docs/ | `assistant/rag.py` | BM25, smoke-tested on real docs |
| Guardrails | `assistant/guardrails.py` | numbers-from-tools validator + advice/disclaimer |
| Audit ring buffer | `assistant/audit.py` | 100-convo, results hashed |
| Agent loop | `assistant/agent.py` | tool loop + validate + one correction → refuse |
| e2e test | `assistant/test_assistant.py` | tool-grounded answer; fabrication refused; advice flagged; audit logged |
| Macrostructure agent | `assistant/macro_job.py` → `state/macro_llm.json` → `MacroLLM` leaf in `quant/agents/macro.py` | structured bias parsed; leaf scores bias×confidence, skips when stale |
| Routes | `server/app.py` | `POST /api/assistant/chat`, `GET /api/assistant/health`, `GET /api/assistant/audit` |
| UI | `server/static/index.html` | Assistant chat tab |
| Wiring | `run_app.py` | `scanner.assistant` + `MacroJob(...).start()` |
| Dep | `requirements.txt` | `openai>=1.30` |

**To go live:** deploy the endpoint per `RUNPOD_TEAM_DEPLOYMENT.md`, put the URL
+ key in `.env`, `pip install openai`, restart. `GET /api/assistant/health`
returns `configured: true` when ready.

---

## 1. What we are building

An LLM assistant integrated into Trading_project_2.0 that can:

1. **Interact with user trades** — answer "what's my P&L?", "why did the last trade fire?" using live project data.
2. **Educate the user** — explain signals, indicators, and market terminology using the agent-tree details and `docs/` corpus.
3. **Macro / market-structure analysis** — periodically synthesize news + macro calendar + market tape into a structured bias score consumed by the `MacroBranch` agent family.

**Hard guardrails (non-negotiable, from product requirements):**

- Every number the model states (prices, P&L, news figures) MUST come from a tool
  call, never from model memory or estimation.
- Every interaction is audit-logged: which tool calls fed which response
  (provenance, last 100 conversations retained).
- Hard line between *explaining what happened* and *giving investment advice*.
  The assistant explains; it does not advise. (SEBI registered-investment-adviser
  boundary — legal requirement, not just UX.)

---

## 2. Architecture

```
┌─ RunPod Serverless (vLLM worker) ───┐        ┌─ Mumbai VPS / dev Mac ──────────────────────┐
│  Qwen/Qwen3.5-9B                    │  HTTPS │  FastAPI (server/app.py)                    │
│  weights on network volume          │◄───────┤   └ POST /api/assistant/chat   (new)        │
│  OpenAI-compatible /openai/v1 API   │        │       └ assistant/agent.py (tool loop)      │
│  min workers: 1 mkt-hrs, else 0     │        │           ├ assistant/tools.py              │
└─────────────────────────────────────┘        │           ├ assistant/rag.py  (docs/ BM25)  │
                                               │           ├ assistant/guardrails.py         │
                                               │           └ state/assistant_audit.jsonl     │
                                               │  background macro job (~15 min)             │
                                               │   └ writes state/macro_llm.json             │
                                               │       └ read by MacroLLM leaf agent in      │
                                               │         quant/agents/macro.py               │
                                               └─────────────────────────────────────────────┘
```

Key design rules:

- **The trading path never awaits the LLM.** The scanner/order path only ever
  reads `state/macro_llm.json` (cached file). If the pod is down, the `MacroLLM`
  leaf marks itself unavailable and the tree degrades gracefully — same behavior
  as a dead news feed.
- Tools call project Python functions **directly** (imports), not HTTP-to-self.
- The model is swappable: everything speaks the OpenAI-compatible protocol, so
  upgrading to Qwen3.5-27B / Qwen3-30B-A3B is a config change only (see §9).

---

## 3. Model choice (context for the team)

| | Decision |
|---|---|
| Model | **Qwen/Qwen3.5-9B** (Apache 2.0) |
| Why | Best measured tool-calling in its size class (BFCL-V4 66.1, TAU2-Bench 79.1), 262k-native context, cheap to run. Finance fine-tunes evaluated (SUFE Fin-R1, DragonLLM Qwen-Open-Finance-R-8B) were rejected: no tool-calling training/benchmarks, and our domain knowledge comes from tools + RAG, not weights. |
| Known issue | Some runtimes (Ollama) mis-parse its tool calls. **Use vLLM only**, with the parser flag below. |
| Upgrade path | Qwen3.5-27B or Qwen3-30B-A3B on a 48GB GPU if analysis quality is insufficient. Same serving stack. |

---

## 4. RunPod deployment (server team)

> **Full deployment instructions live in `docs/RUNPOD_TEAM_DEPLOYMENT.md`** —
> that is the doc to hand to the deployment team. Summary of the decisions:

- **Approach: RunPod Serverless** (vLLM quick-deploy worker), not a persistent
  pod. Billed per-second while a worker runs; scales to zero when idle.
- **Model weights** are cached on an attached **network volume** (≥ 40 GB,
  same datacenter as the endpoint workers). **KV cache is GPU VRAM only** —
  it cannot be persisted to storage; it is rebuilt on each worker start and
  sized by `MAX_MODEL_LEN` / `GPU_MEMORY_UTILIZATION`.
- **Key env vars** (UPPERCASED vLLM engine args): `MODEL_NAME=Qwen/Qwen3.5-9B`,
  `MAX_MODEL_LEN=32768`, `GPU_MEMORY_UTILIZATION=0.95`,
  `ENABLE_AUTO_TOOL_CHOICE=true`, `TOOL_CALL_PARSER=qwen3_coder` (the last two
  are critical — without them function calling silently breaks).
- **Endpoint**: `https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1`,
  authenticated with a **restricted RunPod API key** (Bearer).
- **Cold starts**: 0→serving loads ~18 GB from the volume (30 s–2 min even with
  FlashBoot). Mitigation: min workers = 1 during Indian market hours
  (09:00–15:45 IST), 0 otherwise. The client must use a generous timeout on
  the first call after idle.
- Verification tests (including the critical tool-calling test) are in the
  team doc §4 and must pass before handover.

| Field | Value |
|---|---|
| Endpoint ID | `TODO` |
| Base URL | `TODO` |
| API key | stored in project `.env` only — **never commit, never paste in chat/tickets** |
| Deployed on | `TODO (date)` |

---

## 5. Project-side configuration

### 5.1 `.env` additions

```bash
LLM_BASE_URL=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
LLM_API_KEY=<restricted RunPod API key>
LLM_MODEL=Qwen/Qwen3.5-9B
LLM_CHAT_TEMPERATURE=0.4
LLM_MACRO_TEMPERATURE=0.2
LLM_MACRO_REFRESH_MIN=15
```

### 5.2 `requirements.txt` additions

```
openai        # client SDK only; vLLM speaks its protocol
rank_bm25     # lightweight RAG scoring over docs/ (no vector DB in v1)
```

### 5.3 New modules (implementation plan)

| File | Purpose |
|---|---|
| `assistant/__init__.py` | package |
| `assistant/client.py` | OpenAI client from `.env`; health check helper |
| `assistant/tools.py` | tool JSON schemas + Python executors (see §6) |
| `assistant/agent.py` | tool-calling loop (max 6 iterations), session history |
| `assistant/rag.py` | startup chunking of `docs/` + `docs/specs/*.json`, BM25 search |
| `assistant/guardrails.py` | system prompt, number-grounding validator, advice refusal |
| `assistant/audit.py` | JSONL audit log, 100-conversation ring buffer |
| `assistant/macro_job.py` | background structured-output job → `state/macro_llm.json` |
| `quant/agents/macro.py` | add `MacroLLM` leaf beside `MacroNews` (auto-registered) |
| `server/app.py` | add routes in §7 |
| `server/static/index.html` | Assistant chat tab |

---

## 6. Tool catalogue

Each tool = JSON schema (sent to the model) + Python executor (direct import of
existing project code). All tools are **read-only** — the assistant must never
place, modify, or cancel orders.

| Tool name | Backed by | Serves |
|---|---|---|
| `get_positions` | `state/paper_positions.json` / live day state | trade interaction |
| `get_pnl` | `engine/pnl.py` | trade interaction |
| `get_trade_history` | `paper_trades.jsonl` (includes per-trade agent-tree score breakdown) | education — "why did this trade fire?" |
| `get_agent_tree` | evaluated tree (same source as `/api/tree`); every node has human-readable `detail` | education |
| `get_news_and_macro` | `state/news_state.json` (weighted news, risk_score, FII/DII, market tape) | live news awareness |
| `get_macro_calendar` | `state/macro_calendar.json`, `state/av_calendar.json` | macro events |
| `get_quote` | `quant/datahub.py` | live prices |
| `get_option_chain` | `quant/datahub.py` | option analysis |
| `get_vix` | `quant/datahub.py` / `state/vix_daily.json` | volatility context |
| `get_candles` | existing candles source | chart questions |
| `search_docs` | `assistant/rag.py` over `docs/`, `docs/specs/*.json` | private-data reasoning, terminology education |

Rule for future tools: **read-only, always.** Anything that mutates state
(orders, killswitch, config) is out of scope for the assistant by design.

---

## 7. HTTP endpoints (project FastAPI, `server/app.py`)

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/assistant/chat` | body `{session_id, message}` → `{reply, tools_used, mode, disclaimer}` |
| `GET` | `/api/assistant/health` | pings pod `/v1/models`; used by UI badge & monitoring |
| `GET` | `/api/assistant/audit?n=20` | recent audit records (provenance review) |
| `GET` | `/api/assistant/macro` | current contents of `state/macro_llm.json` |

`mode` is `"explanation"` or `"refused_advice"` — every reply is stamped.

---

## 8. Guardrail implementation spec

1. **Numbers-from-tools-only** — two layers:
   - System prompt forbids stating any price/P&L/news figure not present in this
     turn's tool results.
   - **Post-hoc validator** (`assistant/guardrails.py`): regex-extract numbers
     from the draft reply; each must match a number in the turn's tool results
     (rounding tolerance ±0.5%; whitelist: dates, ordinals, strike-interval
     arithmetic explicitly derived in-reply). One retry with correction message
     on failure, then refuse with explanation. *The validator is the guarantee;
     the prompt is only the first line of defense.*
2. **Audit log** — `state/assistant_audit.jsonl`, one record/turn:
   ```json
   {"ts": "...", "session_id": "...", "user_msg": "...",
    "tool_calls": [{"name": "...", "args": {...}, "result_digest": "sha256:..."}],
    "reply": "...", "mode": "explanation", "validator": "pass"}
   ```
   Ring buffer: prune to the most recent **100 conversations** (by session_id).
   Full tool results are hashed (digest), not stored, to keep the file small —
   raw state files are the source if replay is ever needed.
3. **Advice boundary** — "should I buy/sell/hold X?" → refusal template + the
   relevant *data* (positions, scores, news) so the answer is still useful.
   Every reply carries a fixed disclaimer string. Mode is logged (see §7).

---

## 9. Macro job & agent integration

- `assistant/macro_job.py` runs every `LLM_MACRO_REFRESH_MIN` (default 15 min,
  market hours; same asyncio pattern as `news/service.py`). Input: news state +
  market tape + calendars. Output (structured JSON, temperature 0.2):

  ```json
  {"bias": -1.0, "confidence": 0.0, "regime": "risk-on|risk-off|event-wait",
   "drivers": ["..."], "summary": "...", "generated_at": "..."}
  ```

  written atomically to `state/macro_llm.json`.
- `MacroLLM` leaf (in `quant/agents/macro.py`, sibling of `MacroNews`): score =
  `bias × confidence`; **unavailable if file older than 30 min** or missing.
  Registry auto-discovers it — no wiring changes.

### Model upgrade procedure

1. Server team updates the serverless endpoint per `RUNPOD_TEAM_DEPLOYMENT.md`
   §8 (larger GPU tier, bigger volume, `MODEL_NAME` change).
2. Update `LLM_MODEL` in `.env`; restart FastAPI.
3. Re-run the team-doc verification tests, especially the tool-calling test
   (the correct `TOOL_CALL_PARSER` can differ per model — check vLLM docs).
4. Record the change in the Change Log below.

---

## 10. Monitoring & troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/assistant/health` red | endpoint has no workers / RunPod issue | check endpoint status + worker logs in RunPod console |
| First reply after idle very slow / times out | cold start (min workers at 0) | expected outside market hours; client uses generous first-call timeout; check min-worker schedule |
| Model answers with numbers but `tools_used` empty | prompt regression or parser issue | check endpoint env vars (§4); run the team-doc tool-calling test |
| Tool call printed as text in reply | wrong/missing `TOOL_CALL_PARSER` env var | fix env var, redeploy workers |
| Replies slow (>20 s) even when warm | KV cache pressure / context too long | trim RAG chunk count; check `MAX_MODEL_LEN` |
| CUDA OOM at worker start | `MAX_MODEL_LEN` too high for GPU | lower to 32768 on 24 GB |
| Validator failures spiking | model drift after upgrade | review audit log samples; tighten system prompt; consider rollback |
| Macro leaf always unavailable | macro job not running / endpoint down | check job logs; file `state/macro_llm.json` mtime |

First end-to-end acceptance test: ask **"What's my current P&L and why did the
last trade fire?"** — must trigger ≥2 tool calls (`get_pnl`,
`get_trade_history`), quote only tool-sourced numbers, and produce an audit
record.

---

## 11. Security checklist

- [ ] Restricted (endpoint-scoped) RunPod API key in use — not an account-wide admin key
- [ ] Key only in `.env` (already gitignored-by-convention; never in this doc/tickets)
- [ ] Assistant tools are read-only — verified no order/killswitch/config mutation paths
- [ ] Audit log contains no secrets (digests, not raw tool payloads)
- [ ] Disclaimer + advice-refusal verified before exposing UI beyond internal users

---

## Change Log

| Date | Who | Change |
|---|---|---|
| 2026-07-09 | Claude (initial draft) | Created doc: model selection, RunPod spec, integration plan |
| 2026-07-09 | Claude | Switched deployment from persistent GPU pod to RunPod Serverless (network volume for weights, `/openai/v1` endpoint, market-hours min-worker schedule); team instructions moved to `RUNPOD_TEAM_DEPLOYMENT.md` |
| | | |

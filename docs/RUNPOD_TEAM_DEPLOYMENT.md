# RunPod Serverless Deployment — LLM Endpoint (for deployment team)

> **Task:** Deploy Qwen3.5-9B as a **RunPod Serverless vLLM endpoint**, with
> model weights cached on a **network volume**, and hand back the endpoint ID +
> API key. This doc contains everything you need — no project knowledge required.
>
> **Requested by:** Vivek (vivektr@insigniaconsultancy.com)
> **Date:** 2026-07-09 (updated: serverless approach)

---

## 0. Storage — read this first

- **Model weights → network volume.** Create a RunPod **Network Volume** and
  attach it to the endpoint. The vLLM worker caches the ~18 GB of Hugging Face
  weights at `/runpod-volume` (the worker's default `BASE_PATH`). First-ever
  request downloads the weights once; every later cold start loads from the
  volume instead of re-downloading.
- **KV cache → GPU VRAM only.** The KV cache is runtime memory inside the GPU,
  rebuilt whenever a worker starts. It **cannot** be persisted to storage —
  there is no setting for this. Its size is controlled by `MAX_MODEL_LEN` and
  `GPU_MEMORY_UTILIZATION` below. Do not spend time looking for a "KV cache on
  volume" option; it doesn't exist.
- **Network volumes are region-locked** — the endpoint's workers must be in the
  same datacenter as the volume. Pick the region at volume creation time.

## 1. Network volume

| Setting | Value |
|---|---|
| Size | **≥ 40 GB** |
| Datacenter | Any with 24 GB GPU availability (note it below — endpoint must match) |
| Created in DC | `TODO` |
| Volume ID | `TODO` |

## 2. Serverless endpoint configuration

Use RunPod's **Serverless → vLLM quick deploy** (worker image
`runpod/worker-v1-vllm`, latest stable tag).

| Setting | Value |
|---|---|
| GPU | **24 GB tier** (RTX 4090 / L4 / A5000 class) |
| Network volume | attach the volume from §1 (same DC) |
| Min (active) workers | **1 during Indian market hours (09:00–15:45 IST), 0 otherwise** — see §5 |
| Max workers | **2** |
| Idle timeout | 120 s |
| FlashBoot | **Enabled** (cuts cold starts) |

### Environment variables

The worker accepts any vLLM engine argument as an UPPERCASED env var. Set
these EXACTLY — every one matters:

| Env var | Value | Why |
|---|---|---|
| `MODEL_NAME` | `Qwen/Qwen3.5-9B` | model to serve (downloads to volume on first run) |
| `MAX_MODEL_LEN` | `32768` | context window; higher will CUDA-OOM on 24 GB |
| `GPU_MEMORY_UTILIZATION` | `0.95` | leaves the rest of VRAM for KV cache |
| `ENABLE_AUTO_TOOL_CHOICE` | `true` | **critical** — enables function calling |
| `TOOL_CALL_PARSER` | `qwen3_coder` | **critical** — without it tool calls silently break |

### Authentication

Serverless endpoints are authenticated with a **RunPod API key** (Bearer
token), not a self-generated key. Create a **dedicated, restricted API key**
for this endpoint (Settings → API Keys) — do not hand over an account-wide
admin key.

## 3. Endpoint URL

After deploy, the OpenAI-compatible base URL is:

```
https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
```

| Field | Value |
|---|---|
| Endpoint ID | `TODO` |
| Base URL | `TODO` |
| Deployed on | `TODO` |

## 4. Verification — all 3 tests must pass before handover

```bash
BASE_URL=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
KEY=<the restricted RunPod API key>

# Test 1 — model is served (expect JSON listing Qwen/Qwen3.5-9B)
curl -s $BASE_URL/models -H "Authorization: Bearer $KEY"

# Test 2 — basic completion (expect a normal chat response;
# first-ever call also triggers the one-time weight download — allow several minutes)
curl -s $BASE_URL/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3.5-9B","messages":[{"role":"user","content":"Say OK"}],"max_tokens":10}'

# Test 3 — TOOL CALLING (the most important test)
curl -s $BASE_URL/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3.5-9B","messages":[{"role":"user","content":"What is the NIFTY spot price?"}],"tools":[{"type":"function","function":{"name":"get_quote","description":"Get live quote","parameters":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}}}]}'

# Test 4 — cold start timing: scale workers to 0 (or wait past idle timeout),
# re-run Test 2, and record time-to-first-token below.
```

**Test 3 pass criteria:** the response must contain a
`choices[0].message.tool_calls` **array** with `function.name == "get_quote"`.
If the tool call appears as plain **text** in `message.content` instead, the
`ENABLE_AUTO_TOOL_CHOICE` / `TOOL_CALL_PARSER` env vars are wrong or missing —
fix and re-test before handover.

| Measured cold start (0 → first token) | `TODO` s |
|---|---|

## 5. Scaling & cost expectations

- Billing is **per-second while a worker is running** — scale-to-zero means
  zero cost when idle. That's the point of serverless.
- **But**: a cold start (0 → serving) loads 18 GB from the network volume and
  warms vLLM — typically **30 s – 2 min even with FlashBoot**. The trading
  system polls this endpoint every ~15 min during market hours, so
  cold-starting on every call would be slow and negate the savings.
- Therefore: **min workers = 1 during market hours (09:00–15:45 IST Mon–Fri),
  min workers = 0 outside them.** Automate this via RunPod API/cron if
  possible and record the schedule here:

  | Schedule | Value |
  |---|---|
  | Min workers → 1 (IST) | TODO |
  | Min workers → 0 (IST) | TODO |
  | Automation method | TODO |

## 6. Handover — send back to Vivek

Via a **secure channel** (password manager / secret share — NOT email or
tickets for the key):

- [ ] Endpoint ID and Base URL (`https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1`)
- [ ] The restricted RunPod API key
- [ ] Output of Tests 1–3 + measured cold-start time from Test 4
- [ ] Network volume ID + datacenter
- [ ] Worker scaling schedule (if automated)

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| CUDA OOM at worker start | `MAX_MODEL_LEN` too high for 24 GB | set to `32768` |
| Tool call returned as plain text | `ENABLE_AUTO_TOOL_CHOICE`/`TOOL_CALL_PARSER` wrong or missing | set per §2, redeploy workers |
| First request very slow / times out | cold start or first-time weight download | expected; see §5; raise client timeout for first call |
| Every request slow | min workers at 0 → cold start each time | set min workers = 1 during market hours |
| 401 from endpoint | wrong/expired RunPod API key | use the restricted key from §2 |
| Worker can't find volume / re-downloads weights | volume in different DC than workers, or not attached | endpoint and volume must share a datacenter |
| `IN_QUEUE` forever | no GPU availability in the DC | check DC capacity; consider second GPU type in endpoint config |

## 8. Future model upgrade (only when requested)

If asked to upgrade to a larger model (e.g. Qwen3.5-27B / Qwen3-30B-A3B):

1. Switch endpoint GPU tier to **48 GB** (L40S / A6000 class).
2. Grow the network volume (27B ≈ 55 GB of weights → volume ≥ 80 GB).
3. Change `MODEL_NAME`; `MAX_MODEL_LEN` may go up to 65536.
4. Confirm the correct `TOOL_CALL_PARSER` for the new model in vLLM docs.
5. Re-run all verification tests (especially Test 3) before handover.


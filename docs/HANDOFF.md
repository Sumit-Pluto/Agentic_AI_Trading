# Session Handoff — Agentic Intraday Trading

Last updated: 2026-10-07. Read this first when resuming work.

## Where things stand

- HEAD `dcae2f0` is pushed to `origin/main` (Sumit-Pluto/Agentic_AI_Trading).
- **Uncommitted on top of HEAD** (Orders + Funds feature, all verified):
  - `frontend/src/App.tsx`, `frontend/src/lib/live.ts`
  - `intraday/gateway_client/client.py`, `intraday/server/app.py`, `intraday/server/runner.py`
  - `intraday/tests/test_server.py`
  - New: `frontend/src/components/Funds.tsx`, `frontend/src/components/Orders.tsx`,
    `intraday/tests/test_gateway_client.py`
- VPS (`root@192.168.133.205`, code at `/opt/ai_intraday` — note: NOT
  `/opt/ai_trading`, service `ai-intraday` on :8040) is updated to `c501e59`
  (2026-10-09). Deploy = `git pull` there, `npm run build` in `frontend/`,
  `systemctl restart ai-intraday`. SSH needs `require_escalated` + local
  WireGuard up; VPS `main` now tracks `origin/main`.

## What was built (in order)

1. **Cockpit correctness (committed, pushed)** — `dcae2f0`
   - Positions show lots × lot-size, qty, R-multiple, premium stop + underlying
     stop, entry time, formatted age; new Closed Today table.
   - Exit machine: `min_hold_seconds` (default 90, Settings-editable) defers
     noise exits I2/I5/I6 on fresh fills; I0/I1/I3 stay instant.
   - Every close/partial logs symbol, price, reason, hold, P&L to Activity.
   - Partial-fill accounting: WORKING / short-FILLED exits book only confirmed
     shares and keep managing the rest (no double-sell on retry).
   - Failed flatten re-arms itself; flatten accounts partials and logs closes.
   - Short-option premium stop (I1b mirror); failed placements logged.
   - Fixed table alignment, tile grids, Journal/Reporting/Chain nits.
2. **Orders + Funds views (uncommitted, verified)** — 98/98 tests green
   - `GET /api/orderbook`, `GET /api/broker-positions` (3s cache,
     stale-on-failure, `connected:false` offline).
   - **Critical fix**: gateway returns `{symbol_groups:[...]}`; client now
     flattens it — previously live restarts would ghost-close all positions.
   - New `Orders` tab (open orders, today's history, broker positions with
     tracked/external flags) and `Funds` tab (margin breakdown + utilisation).

## Verify

- Backend: `/opt/miniconda3/envs/trading_testing/bin/python -m pytest intraday/tests -q`
- Frontend: `cd frontend && npm run build` + `npx eslint <changed files>`
- Run: `./run_cockpit.sh` → http://127.0.0.1:8080 (sim); VPS :8040 (live data).

## Open product decisions (need user)

- Broker-side SL/GTT leg in addition to the engine stop, or engine stop only.
- Freeze-quantity order splitting (no handling exists today).
- Next feature blocks offered: manual trading ticket, market watchlist.

## Gotchas

- Push over HTTPS fails in sandbox (keychain); push via SSH remote instead.
- Direct SSH/VPS access needs `require_escalated` + local WireGuard up.
- `frontend/dist/` is git-ignored; rebuilt by `run_cockpit.sh` or `npm run build`.

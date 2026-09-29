#!/usr/bin/env bash
# Launch the intraday agentic cockpit (demo mode: simulated market, no broker).
#
#   ./run_cockpit.sh              # builds the UI if needed, serves on :8080
#   PORT=9000 ./run_cockpit.sh    # custom port
#   PYTHON=/path/to/python ./run_cockpit.sh
#
# Then open http://127.0.0.1:8080 — the whole agentic system trades live on a
# simulated NIFTY/BANKNIFTY feed. To drive the REAL Shoonya feed instead, set
# ENGINE_MODE=live with the Gateway running (see gateway/README.md) — same code.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8080}"
PYTHON="${PYTHON:-/opt/miniconda3/envs/trading_testing/bin/python}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python3"

# 1. build the cockpit if it isn't built yet
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "==> building the cockpit (frontend/dist)…"
  ( cd "$ROOT/frontend" && [ -d node_modules ] || npm install; npm run build )
fi

# 2. serve engine + cockpit
echo "==> cockpit → http://127.0.0.1:$PORT   (Ctrl-C to stop)"
cd "$ROOT"
exec "$PYTHON" -m uvicorn intraday.server.app:app --host 0.0.0.0 --port "$PORT"

#!/usr/bin/env bash
# llm_tunnel.sh — keep the LLM tunnel alive: localhost:8001 -> RunPod vLLM :8000.
# The .env points LLM_BASE_URL at http://localhost:8001/v1; this loop reconnects
# automatically whenever the link drops (pod hiccup, network blip, sleep/wake).
# Usage:  bash llm_tunnel.sh &        (or run it in its own terminal)
# Update POD_HOST/POD_PORT when the pod is recreated with a new address.

POD_HOST="${POD_HOST:-69.30.85.151}"
POD_PORT="${POD_PORT:-22120}"
KEY="${KEY:-$HOME/.ssh/runpod_key}"

echo "LLM tunnel: localhost:8001 -> ${POD_HOST}:${POD_PORT} (vLLM :8000). Ctrl-C to stop."
while true; do
  ssh -i "$KEY" -o IdentitiesOnly=yes \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new \
      -p "$POD_PORT" -N -L 8001:localhost:8000 "root@${POD_HOST}"
  echo "$(date '+%H:%M:%S') tunnel dropped (exit $?) — reconnecting in 5s…"
  sleep 5
done

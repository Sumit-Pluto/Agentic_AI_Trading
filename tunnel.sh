#!/bin/bash
# Self-healing SSH SOCKS tunnel to your static-IP machine.
#
# The bot's broker traffic exits from that machine's IP (register THAT IP as
# Primary IP in the Shoonya portal). Keep this running in its own terminal,
# and set in .env:  SHOONYA_PROXY=socks5h://127.0.0.1:1080
#
# Usage:   ./tunnel.sh user@static-machine [ssh-port]
# Example: ./tunnel.sh vivek@203.0.113.42
#          ./tunnel.sh vivek@office-server.example.com 2222
#
# Tip: set up SSH keys first (ssh-copy-id user@host) so it reconnects
# without asking for a password.

HOST=${1:?usage: ./tunnel.sh user@host [ssh-port]}
PORT=${2:-22}
SOCKS_PORT=1080

while true; do
    echo "$(date '+%H:%M:%S') tunnel up → $HOST (SOCKS5 on 127.0.0.1:$SOCKS_PORT)"
    ssh -p "$PORT" -D "$SOCKS_PORT" -N \
        -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -o ConnectTimeout=10 \
        "$HOST"
    echo "$(date '+%H:%M:%S') tunnel dropped — reconnecting in 3s (Ctrl-C to stop)"
    sleep 3
done

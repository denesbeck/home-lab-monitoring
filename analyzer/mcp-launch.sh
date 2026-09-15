#!/usr/bin/env bash
# Opens an SSH tunnel so the MCP server can reach Prometheus/Loki on 127.0.0.1,
# then starts it. SSH_TARGET is an ~/.ssh/config Host alias so no server details
# live in this repo -- see README to define it.
set -euo pipefail

SSH_TARGET="${MONITORING_SSH:-homelab-monitoring}"
CTRL="${HOME}/.ssh/cm-monitoring.sock"

# Reuse an existing tunnel if the control socket is alive; otherwise open one.
if ! ssh -O check -S "$CTRL" "$SSH_TARGET" >/dev/null 2>&1; then
  ssh -M -S "$CTRL" -fNT \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -L 127.0.0.1:9090:127.0.0.1:9090 \
    -L 127.0.0.1:3100:127.0.0.1:3100 \
    "$SSH_TARGET"
fi
# To close the tunnel later:  ssh -O exit -S ~/.ssh/cm-monitoring.sock homelab-monitoring

DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer uv (zero-setup, ephemeral deps); fall back to a local venv if uv is absent.
if command -v uv >/dev/null 2>&1; then
  exec uv run --with mcp --with httpx --directory "$DIR" python mcp_server.py
fi

VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" -q install mcp httpx
fi
exec "$VENV/bin/python" "$DIR/mcp_server.py"

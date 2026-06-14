#!/usr/bin/env bash
# Launcher for the home-lab monitoring MCP server (interactive use from Claude Code).
#
# Brings up an SSH tunnel to the server (idempotently) so the MCP server can reach
# Prometheus/Loki on 127.0.0.1, then starts the server. No manual port-forwarding.
#
# The SSH target is an ~/.ssh/config Host alias, so NO server details (host, port,
# user) live in this repo. Define the alias in ~/.ssh/config (see README), e.g.:
#
#   Host homelab-monitoring
#     HostName <your-server-ip>
#     Port     <your-ssh-port>
#     User     <your-user>
#
# Override the alias name via the MONITORING_SSH env var if you prefer a different one.
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

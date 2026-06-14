"""MCP server exposing the monitoring tools for interactive use from Claude Code.

Runs on the laptop via .mcp.json (subscription auth, no API key). Point it at the
port-forwarded endpoints (127.0.0.1:9090 / 127.0.0.1:3100).

FastMCP infers each tool's schema from the function signature + docstring.
"""

from mcp.server.fastmcp import FastMCP

import monitoring_tools as mt

mcp = FastMCP("home-lab-monitoring")

for _fn in mt.TOOL_FUNCS.values():
    mcp.add_tool(_fn)

if __name__ == "__main__":
    mcp.run()

"""Shared, token-efficient tools over Prometheus and Loki.

Reused by both consumers:
- mcp_server.py  -> interactive debugging from Claude Code (subscription auth)
- webhook_app.py -> automated alert investigation (API key)

Tools return aggregates and clustered samples, never raw log firehoses, so the
model reasons over digests instead of dragging everything into context.

Endpoints are configured via env:
  PROM_URL (default http://127.0.0.1:9090)  -- server: http://prometheus:9090
  LOKI_URL (default http://127.0.0.1:3100)  -- server: http://loki:3100
"""

import collections
import datetime
import os
import re
import time

import httpx

PROM_URL = os.environ.get("PROM_URL", "http://127.0.0.1:9090").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://127.0.0.1:3100").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))

# Matches genuine error-LEVEL lines, not the substring "error" inside info logs
# (the distinction that mattered in the manual 7-day review).
ERROR_RE = r'(?i)(level=err|\[err|"error"|level=fatal|panic:|exception)'

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _window_seconds(window: str) -> int:
    return int(window[:-1]) * _UNIT_SECONDS[window[-1]]


def _client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT)


def _selector(container: str) -> str:
    """Loki container labels carry a leading slash, e.g. /jellyfin."""
    return container if container.startswith("/") else f"/{container}"


def prom_instant(query: str) -> dict:
    """Run an instant PromQL query and return a compact result.

    Args:
        query: A PromQL expression, e.g. 'up' or
            'container_memory_working_set_bytes{name="jellyfin"}'.
    """
    with _client() as c:
        r = c.get(f"{PROM_URL}/api/v1/query", params={"query": query})
        r.raise_for_status()
        data = r.json()["data"]
    result = [{"metric": s.get("metric", {}), "value": s["value"][1]} for s in data.get("result", [])]
    return {"resultType": data.get("resultType"), "result": result}


def prom_range(query: str, window: str = "1h", step: str = "60s") -> dict:
    """Run a range PromQL query and return per-series min/max/avg/last.

    Returns a summary (not every datapoint) to stay token-efficient.

    Args:
        query: A PromQL expression.
        window: Lookback like '30m', '6h', '7d'.
        step: Resolution like '60s', '5m'.
    """
    end = int(time.time())
    start = end - _window_seconds(window)
    with _client() as c:
        r = c.get(
            f"{PROM_URL}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
        )
        r.raise_for_status()
        data = r.json()["data"]
    series = []
    for s in data.get("result", []):
        vals = [float(v[1]) for v in s["values"] if v[1] != "NaN"]
        if not vals:
            continue
        series.append(
            {
                "metric": s.get("metric", {}),
                "min": min(vals),
                "max": max(vals),
                "avg": sum(vals) / len(vals),
                "last": vals[-1],
                "points": len(vals),
            }
        )
    return {"window": window, "series": series}


def prom_alerts() -> dict:
    """Return currently active Prometheus alerts (firing and pending)."""
    with _client() as c:
        r = c.get(f"{PROM_URL}/api/v1/alerts")
        r.raise_for_status()
        alerts = r.json()["data"]["alerts"]
    return {
        "alerts": [
            {
                "name": a["labels"].get("alertname"),
                "state": a["state"],
                "labels": a["labels"],
                "annotations": a.get("annotations", {}),
                "activeAt": a.get("activeAt"),
            }
            for a in alerts
        ]
    }


def _loki_range(query: str, window: str, limit: int = 500) -> list:
    end = int(time.time() * 1e9)
    start = end - _window_seconds(window) * 10**9
    with _client() as c:
        r = c.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "limit": limit, "direction": "backward"},
        )
        r.raise_for_status()
        return r.json()["data"]["result"]


def loki_error_summary(container: str, window: str = "24h") -> dict:
    """Count genuine error-level log lines for a container and cluster them.

    Clustering normalizes digits so near-identical messages collapse into one
    representative sample with a count -- the core reduction from the manual review.

    Args:
        container: Container name, with or without leading slash (e.g. 'jellyfin').
        window: Lookback like '1h', '24h', '7d'.
    """
    sel = _selector(container)
    count_q = f'sum(count_over_time({{container="{sel}"}} |~ `{ERROR_RE}` [{window}]))'
    with _client() as c:
        r = c.get(f"{LOKI_URL}/loki/api/v1/query", params={"query": count_q, "time": int(time.time() * 1e9)})
        r.raise_for_status()
        res = r.json()["data"]["result"]
    count = int(float(res[0]["value"][1])) if res else 0

    streams = _loki_range(f'{{container="{sel}"}} |~ `{ERROR_RE}`', window, limit=500)
    clusters: collections.Counter = collections.Counter()
    for s in streams:
        for _ts, line in s["values"]:
            norm = re.sub(r"\d", " ", line)
            norm = re.sub(r"\s+", " ", norm).strip()[:160]
            clusters[norm] += 1
    top = [{"count": n, "sample": m} for m, n in clusters.most_common(12)]
    return {"container": sel, "window": window, "error_lines": count, "top_clusters": top}


def loki_logs(container: str, filter: str = "", window: str = "1h", limit: int = 50) -> dict:
    """Fetch a bounded, newest-first sample of log lines for a container.

    Args:
        container: Container name, with or without leading slash.
        filter: Optional case-insensitive regex to match lines, e.g. '(?i)timeout'.
        window: Lookback like '15m', '1h', '24h'.
        limit: Max lines to return (default 50).
    """
    sel = _selector(container)
    query = f'{{container="{sel}"}}'
    if filter:
        query += f" |~ `{filter}`"
    streams = _loki_range(query, window, limit=limit)
    lines = []
    for s in streams:
        for ts, line in s["values"]:
            iso = datetime.datetime.utcfromtimestamp(int(ts) / 1e9).isoformat()
            lines.append({"ts": iso, "line": line[:500]})
    lines.sort(key=lambda x: x["ts"], reverse=True)
    return {"container": sel, "window": window, "count": len(lines), "lines": lines[:limit]}


def list_containers() -> dict:
    """List the container names known to Loki (leading-slash form)."""
    with _client() as c:
        r = c.get(f"{LOKI_URL}/loki/api/v1/label/container/values")
        r.raise_for_status()
        return {"containers": r.json().get("data", [])}


# --- Tool registry for the Anthropic tool-use loop (webhook_app) ---------------
# mcp_server.py registers the same functions via FastMCP's signature inference.

TOOL_FUNCS = {
    "prom_instant": prom_instant,
    "prom_range": prom_range,
    "prom_alerts": prom_alerts,
    "loki_error_summary": loki_error_summary,
    "loki_logs": loki_logs,
    "list_containers": list_containers,
}

ANTHROPIC_TOOLS = [
    {
        "name": "prom_instant",
        "description": "Run an instant PromQL query; returns compact metric/value pairs.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "PromQL expression"}},
            "required": ["query"],
        },
    },
    {
        "name": "prom_range",
        "description": "Run a range PromQL query; returns per-series min/max/avg/last (a summary, not every point).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression"},
                "window": {"type": "string", "description": "Lookback like 30m, 6h, 7d", "default": "1h"},
                "step": {"type": "string", "description": "Resolution like 60s, 5m", "default": "60s"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "prom_alerts",
        "description": "Return currently active Prometheus alerts (firing and pending).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "loki_error_summary",
        "description": "Count genuine error-level log lines for a container over a window and return clustered top messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Container name, e.g. jellyfin"},
                "window": {"type": "string", "description": "Lookback like 1h, 24h, 7d", "default": "24h"},
            },
            "required": ["container"],
        },
    },
    {
        "name": "loki_logs",
        "description": "Fetch a bounded, newest-first sample of a container's log lines, optionally filtered by regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Container name, e.g. jellyfin"},
                "filter": {"type": "string", "description": "Optional case-insensitive regex", "default": ""},
                "window": {"type": "string", "description": "Lookback like 15m, 1h", "default": "1h"},
                "limit": {"type": "integer", "description": "Max lines", "default": 50},
            },
            "required": ["container"],
        },
    },
    {
        "name": "list_containers",
        "description": "List container names known to Loki (leading-slash form).",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def run_tool(name: str, args: dict) -> dict:
    """Dispatch a tool call by name (used by the Anthropic tool-use loop)."""
    if name not in TOOL_FUNCS:
        return {"error": f"unknown tool: {name}"}
    return TOOL_FUNCS[name](**args)

"""FastAPI service that investigates Alertmanager alerts with Claude.

Flow: Alertmanager POSTs firing alerts to /alert -> we run a Claude tool-use loop
over the Prometheus/Loki tools -> post the hypothesis to Discord.

The raw alert still reaches Discord via Alertmanager's own discord_configs; this
adds an enriched follow-up. If this service is down, the raw alert is unaffected.
"""

import hashlib
import hmac
import json
import logging
import os
import time

import httpx
from anthropic import Anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

import monitoring_tools as mt
from prompts import SYSTEM_PROMPT

logger = logging.getLogger("claude-analyzer")

MODEL = os.environ.get("ANALYZER_MODEL", "claude-sonnet-4-6")
MAX_TOOL_TURNS = int(os.environ.get("MAX_TOOL_TURNS", "8"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1024"))
DEDUP_TTL = int(os.environ.get("DEDUP_TTL", "3600"))
DISCORD_WEBHOOK_FILE = os.environ.get("DISCORD_WEBHOOK_FILE", "/run/discord_webhook")


def _load_token() -> str:
    path = os.environ.get("WEBHOOK_TOKEN_FILE", "/run/analyzer_token")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("WEBHOOK_TOKEN", "")


WEBHOOK_TOKEN = _load_token()

client = Anthropic()  # reads ANTHROPIC_API_KEY from env
app = FastAPI(title="claude-analyzer")
_recent: dict[str, float] = {}  # alert fingerprint -> last-investigated epoch


def _discord_post(content: str) -> None:
    with open(DISCORD_WEBHOOK_FILE) as f:
        url = f.read().strip()
    with httpx.Client(timeout=15) as c:
        # parse: [] neutralizes @everyone/@here/role mentions that could be
        # smuggled in via untrusted log content the model summarized.
        c.post(url, json={"content": content[:1900], "allowed_mentions": {"parse": []}})


def _investigate(alert: dict) -> str:
    context = json.dumps(
        {
            "alertname": alert.get("labels", {}).get("alertname"),
            "labels": alert.get("labels", {}),
            "annotations": alert.get("annotations", {}),
            "startsAt": alert.get("startsAt"),
        },
        indent=2,
    )
    messages = [{"role": "user", "content": f"An alert just fired. Investigate and report.\n\n{context}"}]

    for _ in range(MAX_TOOL_TURNS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=mt.ANTHROPIC_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                output = mt.run_tool(block.name, dict(block.input))
            except Exception as exc:  # surface tool failures to the model
                output = {"error": str(exc)}
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)[:8000]}
            )
        messages.append({"role": "user", "content": tool_results})

    return "Investigation reached the tool-turn limit without a firm conclusion."


def _handle(alert: dict) -> None:
    name = alert.get("labels", {}).get("alertname", "alert")
    try:
        summary = _investigate(alert)
        _discord_post(f"🤖 **Analysis — {name}**\n{summary}")
    except Exception:
        # Don't leak internal details (paths, stack frames) to the channel.
        logger.exception("investigation failed for alert %s", name)
        _discord_post(f"🤖 **Analysis — {name}** failed (see analyzer logs).")


@app.post("/alert")
async def alert(request: Request, background_tasks: BackgroundTasks):
    # Fail closed: with no token configured, accept nothing (don't run unauthenticated).
    if not WEBHOOK_TOKEN:
        raise HTTPException(status_code=503, detail="analyzer not configured: missing webhook token")
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {WEBHOOK_TOKEN}"):
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    now = time.time()
    # prune stale dedup entries
    for fp in [fp for fp, ts in _recent.items() if now - ts > DEDUP_TTL]:
        _recent.pop(fp, None)

    queued = 0
    for item in payload.get("alerts", []):
        if item.get("status") != "firing":
            continue
        fp = hashlib.sha1(json.dumps(item.get("labels", {}), sort_keys=True).encode()).hexdigest()
        if now - _recent.get(fp, 0) < DEDUP_TTL:
            continue  # same firing episode already investigated
        _recent[fp] = now
        background_tasks.add_task(_handle, item)
        queued += 1

    return {"status": "accepted", "queued": queued}


@app.get("/health")
def health():
    return {"ok": True}

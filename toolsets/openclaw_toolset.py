"""
OpenClaw Toolset for Hermes Agent.

Registers OpenClaw Gateway capabilities as native Hermes tools,
allowing Hermes to leverage OpenClaw's:

  - 11+ channel adapters (WhatsApp, Telegram, Slack, Discord, ...)
  - Agent routing and session management
  - Canvas/A2UI visualization
  - 5,400+ ClawHub skills
  - Memory search across OpenClaw agents
  - Exec sandboxing (Docker, SSH, Daytona, Modal)

These tools communicate with OpenClaw via the Redis bridge,
not direct API calls — so both systems remain loosely coupled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
BRIDGE_TIMEOUT = int(os.getenv("BRIDGE_TIMEOUT", "120"))

# Channel for Hermes → OpenClaw messages
CHANNEL_HERMES_TO_OC = "bridge:hermes→openclaw"


async def _call_openclaw(action: str, params: dict, timeout: int = 0) -> str:
    """Send a request to OpenClaw via the Redis bridge and wait for reply."""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return json.dumps({"error": "redis package not installed"})

    timeout = timeout or BRIDGE_TIMEOUT
    msg_id = uuid.uuid4().hex[:16]
    reply_channel = f"bridge:reply:{msg_id}"

    msg = {
        "id": msg_id,
        "direction": "hermes→openclaw",
        "action": action,
        "sender": "hermes",
        "target": "openclaw",
        "params": params,
        "reply_channel": reply_channel,
        "timestamp": time.time(),
    }

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_channel)
        await r.publish(CHANNEL_HERMES_TO_OC, json.dumps(msg, ensure_ascii=False))

        deadline = time.time() + timeout
        async for raw in pubsub.listen():
            if time.time() > deadline:
                return json.dumps({"error": f"Timeout ({timeout}s)"})
            if raw["type"] != "message":
                continue
            try:
                data = json.loads(raw["data"])
                if data.get("id") == msg_id:
                    if data.get("error"):
                        return json.dumps({"error": data["error"]})
                    result = data.get("result")
                    if isinstance(result, dict):
                        return json.dumps(result, ensure_ascii=False)
                    return str(result) if result else "OK"
            except Exception:
                continue
    finally:
        await pubsub.unsubscribe(reply_channel)
        await r.aclose()


def _sync_call(action: str, params: dict, timeout: int = 0) -> str:
    """Synchronous wrapper for async Redis call."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _call_openclaw(action, params, timeout))
            return future.result(timeout=(timeout or BRIDGE_TIMEOUT) + 10)
    else:
        return asyncio.run(_call_openclaw(action, params, timeout))


# ── Tool Definitions ──

OPENCLAW_TOOLS = [
    {
        "name": "openclaw_send_message",
        "description": (
            "Send a message through an OpenClaw channel adapter. "
            "Supports 11+ platforms: WhatsApp, Telegram, Slack, Discord, "
            "Signal, iMessage, Google Chat, Teams, Matrix, Feishu, LINE."
        ),
        "params": {
            "channel": {
                "type": "string",
                "description": "Channel name (telegram, slack, discord, whatsapp, ...)",
            },
            "recipient": {
                "type": "string",
                "description": "Recipient ID or chat ID",
            },
            "message": {
                "type": "string",
                "description": "Message text to send",
            },
        },
        "required": ["channel", "message"],
        "handler": lambda args: _sync_call("gateway_send", {
            "channel_id": args.get("channel", ""),
            "recipient": args.get("recipient", ""),
            "text": args.get("message", ""),
        }),
    },
    {
        "name": "openclaw_invoke_agent",
        "description": (
            "Invoke a specific OpenClaw agent to perform a task. "
            "Agents include persistent agents, sub-agents, and "
            "any agent defined in the OpenClaw workspace."
        ),
        "params": {
            "agent_id": {
                "type": "string",
                "description": "OpenClaw agent ID to invoke",
            },
            "prompt": {
                "type": "string",
                "description": "Task prompt for the agent",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session ID for context",
                "default": "",
            },
        },
        "required": ["agent_id", "prompt"],
        "handler": lambda args: _sync_call("agent_invoke", {
            "agent_id": args.get("agent_id", ""),
            "action": "chat",
            "params": {"prompt": args.get("prompt", "")},
            "session_id": args.get("session_id", ""),
        }),
    },
    {
        "name": "openclaw_canvas",
        "description": (
            "Render interactive HTML content on the OpenClaw Canvas. "
            "Use A2UI patterns: generate HTML without JavaScript. "
            "Great for dashboards, charts, data visualization, and reports."
        ),
        "params": {
            "title": {
                "type": "string",
                "description": "Title of the canvas content",
            },
            "html": {
                "type": "string",
                "description": "HTML content to render (no JS needed for A2UI)",
            },
            "session_id": {
                "type": "string",
                "description": "Session to render in",
                "default": "",
            },
        },
        "required": ["title", "html"],
        "handler": lambda args: _sync_call("canvas_render", {
            "title": args.get("title", ""),
            "html": args.get("html", ""),
            "session_id": args.get("session_id", ""),
        }),
    },
    {
        "name": "openclaw_search_skills",
        "description": (
            "Search the OpenClaw skill registry (ClawHub, 5400+ skills). "
            "Find skills for specific tasks, platforms, or domains."
        ),
        "params": {
            "query": {
                "type": "string",
                "description": "Search query for skills",
            },
            "category": {
                "type": "string",
                "description": "Filter by category",
                "default": "",
            },
        },
        "required": ["query"],
        "handler": lambda args: _sync_call("skill_search", {
            "query": args.get("query", ""),
            "category": args.get("category", ""),
        }),
    },
    {
        "name": "openclaw_memory_search",
        "description": (
            "Search OpenClaw agent memory — includes long-term facts "
            "(MEMORY.md), daily notes, and session history. Uses vector "
            "similarity + keyword matching."
        ),
        "params": {
            "query": {
                "type": "string",
                "description": "Memory search query",
            },
            "limit": {
                "type": "integer",
                "description": "Max results",
                "default": 5,
            },
        },
        "required": ["query"],
        "handler": lambda args: _sync_call("session_query", {
            "query": args.get("query", ""),
            "limit": args.get("limit", 5),
        }),
    },
    {
        "name": "openclaw_install_skill",
        "description": (
            "Install a skill from Hermes into the OpenClaw workspace. "
            "The skill will be available to all OpenClaw agents."
        ),
        "params": {
            "skill_name": {
                "type": "string",
                "description": "Name of the Hermes skill to install into OpenClaw",
            },
        },
        "required": ["skill_name"],
        "handler": lambda args: _sync_call("skill_install", {
            "skill_name": args.get("skill_name", ""),
        }),
    },
]


def register_tools():
    """
    Register all OpenClaw tools with the Hermes tool registry.

    Call this at startup or import time to make OpenClaw
    capabilities available to Hermes agents.
    """
    try:
        from tools.registry import registry
    except ImportError:
        logger.error("Hermes tool registry not available")
        return

    for tool_def in OPENCLAW_TOOLS:
        schema = {
            "type": "function",
            "function": {
                "name": tool_def["name"],
                "description": tool_def["description"],
                "parameters": {
                    "type": "object",
                    "properties": tool_def["params"],
                    "required": tool_def.get("required", []),
                },
            },
        }
        registry.register(
            name=tool_def["name"],
            toolset="openclaw",
            schema=schema,
            handler=tool_def["handler"],
            check_fn=lambda: True,
            is_async=False,
            description=tool_def["description"],
            emoji="\U0001f980",
        )

    logger.info(f"Registered {len(OPENCLAW_TOOLS)} OpenClaw tools with Hermes")


# Auto-register on import
try:
    register_tools()
except Exception:
    pass

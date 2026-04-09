"""
OpenClaw Domain Tools — 将 OpenClaw 104 个命令注册为 Hermes 原生工具

代替原来的单个 openclaw_agent 桥接，每个领域注册为独立工具:
- openclaw_market: 行情数据 (涨停/连板/板块/热股/概况)
- openclaw_analysis: 深度分析
- openclaw_strategy: 策略评估
- openclaw_backtest: 回测
- openclaw_news: 新闻搜索
- openclaw_dev: 开发工具 (Claude Code/SSH/Deploy)
- openclaw_monitor: 系统监控
- openclaw_pipeline: Pipeline 执行
- openclaw_apple: Apple 设备控制
- openclaw_general: 通用工具

每个工具通过 Redis Pub/Sub 调用 OpenClaw Orchestrator。
PTC 可以在单次推理中链式调用这些工具。
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
OPENCLAW_TIMEOUT = int(os.getenv("OPENCLAW_TIMEOUT", "180"))


async def _call_openclaw(command: str, args: str = "", timeout: int = 0) -> str:
    """通过 Redis Pub/Sub 调用 OpenClaw Orchestrator"""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return "Error: redis package not installed"

    timeout = timeout or OPENCLAW_TIMEOUT
    msg_id = uuid.uuid4().hex[:12]
    sender = "hermes"

    msg = {
        "id": msg_id, "sender": sender, "target": "orchestrator",
        "action": "route",
        "params": {"command": command, "args": args, "uid": "hermes_agent", "user_name": "Hermes"},
        "reply_to": "", "timestamp": time.time(), "result": None, "error": "",
    }

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        pubsub = r.pubsub()
        await pubsub.subscribe(f"openclaw:{sender}")
        await r.publish("openclaw:orchestrator", json.dumps(msg, ensure_ascii=False))

        deadline = time.time() + timeout
        async for raw in pubsub.listen():
            if time.time() > deadline:
                return f"Timeout ({timeout}s)"
            if raw["type"] != "message":
                continue
            try:
                data = json.loads(raw["data"])
            except Exception:
                continue
            if data.get("id") != msg_id:
                continue
            result = data.get("result")
            if isinstance(result, dict) and result.get("_progress"):
                continue
            error = data.get("error", "")
            if error:
                return f"Error: {error}"
            if isinstance(result, dict):
                text = result.get("text", "")
                if text:
                    return text
                return json.dumps(result, ensure_ascii=False, indent=2)[:4000]
            return str(result)[:4000] if result else "Empty response"
    finally:
        await pubsub.unsubscribe()
        await r.aclose()


def _sync_call(command: str, args: str = "", timeout: int = 0) -> str:
    """同步包装器"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _call_openclaw(command, args, timeout))
            return future.result(timeout=(timeout or OPENCLAW_TIMEOUT) + 10)
    else:
        return asyncio.run(_call_openclaw(command, args, timeout))


# ── 领域工具定义 ──

DOMAIN_TOOLS = [
    {
        "name": "openclaw_market",
        "toolset": "openclaw",
        "description": "A-share market data: limitup stocks (zt), consecutive boards (lb), sector concepts (bk), hot stocks (hot), market summary (summary)",
        "emoji": "\U0001f4ca",
        "params": {
            "action": {"type": "string", "description": "Action: zt (涨停), lb (连板), bk (板块), hot (热股), summary (市场概况)", "enum": ["zt", "lb", "bk", "hot", "summary"]},
            "args": {"type": "string", "description": "Optional arguments", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_analysis",
        "toolset": "openclaw",
        "description": "Deep stock market analysis. Ask questions about market trends, stock analysis, sector rotation.",
        "emoji": "\U0001f9e0",
        "params": {
            "question": {"type": "string", "description": "Analysis question in Chinese"},
        },
        "required": ["question"],
    },
    {
        "name": "openclaw_strategy",
        "toolset": "openclaw",
        "description": "Trading strategy evaluation and recommendations for A-share market.",
        "emoji": "\U0001f4c8",
        "params": {
            "query": {"type": "string", "description": "Strategy query or evaluation request"},
        },
        "required": ["query"],
    },
    {
        "name": "openclaw_backtest",
        "toolset": "openclaw",
        "description": "Strategy backtesting, ledger management, strategy listing for A-share quant trading.",
        "emoji": "\U0001f52c",
        "params": {
            "action": {"type": "string", "description": "Action: backtest, ledger, strategy_list, strategy_detail, qv", "enum": ["backtest", "ledger", "strategy_list", "strategy_detail", "qv"]},
            "args": {"type": "string", "description": "Arguments (strategy code, factor code, etc.)", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_news",
        "toolset": "openclaw",
        "description": "News and web search. Get latest financial news or search the web.",
        "emoji": "\U0001f4f0",
        "params": {
            "action": {"type": "string", "description": "Action: news (latest news), web_search/ws (web search), research/deep (deep research)", "enum": ["news", "web_search", "ws", "research", "deep"]},
            "query": {"type": "string", "description": "Search query or topic", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_dev",
        "toolset": "openclaw",
        "description": "Development tools: Claude Code (cc), SSH, deploy, code review, git operations.",
        "emoji": "\U0001f4bb",
        "params": {
            "action": {"type": "string", "description": "Action: cc (Claude Code), ssh, deploy, code_review, git_status, git_pull", "enum": ["cc", "ssh", "deploy", "code_review", "cr", "git_status", "git_pull", "git_log", "fix", "test", "explain"]},
            "args": {"type": "string", "description": "Command arguments", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_monitor",
        "toolset": "openclaw",
        "description": "System monitoring: alerts, host health, Grafana, Prometheus metrics, patrol checks.",
        "emoji": "\U0001f50d",
        "params": {
            "action": {"type": "string", "description": "Action: alerts, patrol, host_health, cert, targets, metrics, grafana_dash", "enum": ["alerts", "patrol", "host_health", "host", "cert", "ssl", "targets", "metrics", "grafana_dash", "alert_history"]},
            "args": {"type": "string", "description": "Arguments (host IP, domain, etc.)", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_pipeline",
        "toolset": "openclaw",
        "description": "Execute multi-step pipelines: morning_briefing, close_review, deep_research, health_selfheal.",
        "emoji": "\U0001f504",
        "params": {
            "action": {"type": "string", "description": "Action: pipeline (execute), pipeline_list (list available)", "enum": ["pipeline", "pipeline_list"]},
            "args": {"type": "string", "description": "Pipeline name and params (e.g., 'morning_briefing' or 'deep_research topic=AI')", "default": ""},
        },
        "required": ["action"],
    },
    {
        "name": "openclaw_system",
        "toolset": "openclaw",
        "description": "System status, policy status, adaptive tuning status, reflection engine.",
        "emoji": "\u2699\ufe0f",
        "params": {
            "action": {"type": "string", "description": "Action: status, policy_status, adaptive_status, reflect, llm_status, memory_health", "enum": ["status", "policy_status", "adaptive_status", "reflect", "reflect_stats", "llm_status", "embed_status", "memory_health", "memory_hygiene"]},
        },
        "required": ["action"],
    },
]


def _make_handler(tool_def):
    """为每个领域工具创建处理函数"""
    name = tool_def["name"]

    def handler(args_dict=None, **kwargs):
        if args_dict and isinstance(args_dict, dict):
            params = args_dict
        else:
            params = kwargs

        if name == "openclaw_analysis":
            return _sync_call("ask", params.get("question", ""))
        elif name == "openclaw_strategy":
            return _sync_call("strategy", params.get("query", ""))
        else:
            action = params.get("action", "")
            extra = params.get("args", "") or params.get("query", "") or params.get("question", "")
            return _sync_call(action, extra)

    return handler


# ── 注册所有领域工具 ──
for tool_def in DOMAIN_TOOLS:
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
        toolset=tool_def["toolset"],
        schema=schema,
        handler=_make_handler(tool_def),
        check_fn=lambda: True,
        is_async=False,
        description=tool_def["description"],
        emoji=tool_def.get("emoji", "\U0001f980"),
    )

# Keep backward-compatible single tool as well
def openclaw_agent(command: str, args: str = "", timeout: int = 0) -> str:
    """Legacy single-tool bridge (backward compatible)"""
    return _sync_call(command, args, timeout)

registry.register(
    name="openclaw_agent",
    toolset="openclaw",
    schema={
        "type": "function",
        "function": {
            "name": "openclaw_agent",
            "description": "Call OpenClaw multi-agent system with any command (legacy catch-all)",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command name"},
                    "args": {"type": "string", "description": "Arguments", "default": ""},
                    "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 180},
                },
                "required": ["command"],
            },
        },
    },
    handler=openclaw_agent,
    check_fn=lambda: True,
    is_async=False,
    description="Call OpenClaw multi-agent system (legacy)",
    emoji="\U0001f980",
)

# Export domain tool names for use by toolsets.py
OPENCLAW_DOMAIN_TOOL_NAMES = [t["name"] for t in DOMAIN_TOOLS]

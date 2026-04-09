"""
OpenClaw <-> Hermes Bridge Server

让 OpenClaw Orchestrator 可以通过 Redis 调用 Hermes Agent 的完整能力:
- 文件操作、终端命令、网页搜索
- 代码执行 (PTC)、任务委托
- 技能学习和自进化
- 同时 Hermes 也可以调用 OpenClaw 的 12 个 Agent

双向桥接架构:
  OpenClaw -> Redis -> hermes_bridge_server -> Hermes AIAgent -> 结果
  Hermes -> openclaw_tool -> Redis -> OpenClaw Orchestrator -> 结果
"""

import asyncio
import json
import logging
import os
import sys
import signal
import time

# Ensure hermes-agent is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HermesBridge] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hermes_bridge")

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CHANNEL = "openclaw:hermes"


async def _run_hermes_task(prompt: str, max_iterations: int = 30) -> str:
    """运行 Hermes Agent 处理任务"""
    try:
        from run_agent import AIAgent
        from hermes_cli.config import load_config

        config = load_config()

        agent = AIAgent(
            model=config.get("model", {}).get("default", "qwen3.5-plus"),
            provider=config.get("model", {}).get("provider", "custom"),
            base_url=config.get("model", {}).get("base_url", ""),
            max_iterations=max_iterations,
            enabled_toolsets=["openclaw", "core"],
        )

        # AIAgent.chat() is synchronous — run in executor to avoid blocking
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, agent.chat, prompt)
        return result or "Task completed (no text output)"

    except Exception as e:
        logger.error(f"Hermes task error: {e}", exc_info=True)
        return f"Hermes Error: {e}"


async def main():
    """监听 Redis 频道，处理来自 OpenClaw 的任务请求"""
    import redis.asyncio as aioredis

    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.hermes/.env"))

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(CHANNEL)

    logger.info(f"Hermes Bridge Server listening on {CHANNEL}")

    stop = asyncio.Event()

    def _handle_signal(sig):
        logger.info(f"Received {sig}, shutting down...")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    async def _heartbeat():
        while not stop.is_set():
            try:
                await r.hset("openclaw:heartbeats", "hermes", json.dumps({
                    "ts": time.time(),
                    "pid": os.getpid(),
                    "type": "hermes-agent",
                }))
            except Exception:
                pass
            await asyncio.sleep(10)

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        async for raw in pubsub.listen():
            if stop.is_set():
                break
            if raw["type"] != "message":
                continue

            try:
                msg = json.loads(raw["data"])
            except Exception:
                continue

            msg_id = msg.get("id", "")
            sender = msg.get("sender", "")
            action = msg.get("action", "")
            params = msg.get("params", {})
            reply_channel = msg.get("reply_channel", "")

            logger.info(f"Received: id={msg_id}, sender={sender}, action={action}")

            if action == "hermes_task":
                prompt = params.get("prompt", "") or params.get("args", "")
                max_iter = params.get("max_iterations", 30)

                if not prompt:
                    result_text = "Missing prompt"
                else:
                    logger.info(f"Running Hermes task: {prompt[:100]}...")
                    result_text = await _run_hermes_task(prompt, max_iter)

                reply = {
                    "id": msg_id,
                    "sender": "hermes",
                    "target": sender,
                    "action": f"{action}:response",
                    "params": {},
                    "reply_to": msg_id,
                    "timestamp": time.time(),
                    "result": {"text": result_text},
                    "error": "",
                }
                # Prefer dedicated reply channel to avoid deadlock on shared channel
                dest = reply_channel or f"openclaw:{sender}"
                await r.publish(dest, json.dumps(reply, ensure_ascii=False))
                logger.info(f"Replied to {dest}: {len(result_text)} chars")

    except asyncio.CancelledError:
        pass
    finally:
        heartbeat_task.cancel()
        await pubsub.unsubscribe()
        await r.aclose()
        logger.info("Hermes Bridge Server stopped")


if __name__ == "__main__":
    asyncio.run(main())

"""
Hermes-OpenClaw Bridge Server — Main entry point.

Orchestrates all bridge components:
  1. Connects to OpenClaw Gateway via WebSocket
  2. Initializes Hermes Agent runtime
  3. Starts Redis Pub/Sub message broker
  4. Launches skill synchronization
  5. Starts heartbeat broadcasting

Usage:
    python -m bridge.server
    python -m bridge.server --config bridge.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from bridge.gateway_client import GatewayClient
from bridge.hermes_runtime import HermesRuntime
from bridge.redis_broker import RedisBroker
from bridge.skill_bridge import SkillBridge
from bridge.memory_bridge import MemoryBridge
from bridge.protocol import (
    Action, BridgeMessage, Direction,
    CHANNEL_OC_TO_HERMES, CHANNEL_HERMES_TO_OC,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bridge.server")

DEFAULT_CONFIG = {
    "gateway": {
        "url": "ws://127.0.0.1:18789",
        "auth_token": "",
    },
    "hermes": {
        "model": "",
        "provider": "",
        "base_url": "",
        "profile": "bridge",
        "max_workers": 4,
        "default_toolsets": ["core", "web", "terminal", "browser"],
    },
    "redis": {
        "url": "redis://127.0.0.1:6379/0",
    },
    "skills": {
        "auto_sync": True,
        "sync_interval_hours": 6,
    },
}


class BridgeServer:
    """Main bridge server orchestrating all components."""

    def __init__(self, config: dict):
        self.config = config
        self._stop = asyncio.Event()

        # Components
        self.broker = RedisBroker(config["redis"]["url"])
        self.hermes = HermesRuntime(
            model=config["hermes"]["model"],
            provider=config["hermes"]["provider"],
            base_url=config["hermes"]["base_url"],
            profile=config["hermes"]["profile"],
            max_workers=config["hermes"]["max_workers"],
            default_toolsets=config["hermes"]["default_toolsets"],
        )
        self.gateway = GatewayClient(
            gateway_url=config["gateway"]["url"],
            auth_token=config["gateway"]["auth_token"],
            on_message=self._handle_gateway_message,
        )
        self.skills = SkillBridge()
        self.memory = MemoryBridge()

    async def start(self):
        """Start all bridge components."""
        logger.info("Starting Hermes-OpenClaw Bridge Server...")

        # 1. Connect Redis broker
        await self.broker.connect()
        self.broker.on(CHANNEL_OC_TO_HERMES, self._handle_oc_to_hermes)
        self.broker.on(CHANNEL_HERMES_TO_OC, self._handle_hermes_to_oc)

        # 2. Initialize Hermes runtime
        await self.hermes.initialize()

        # 3. Connect to OpenClaw Gateway
        try:
            await self.gateway.connect()
        except Exception as e:
            logger.warning(
                f"Gateway connection failed ({e}). "
                f"Bridge will operate in Redis-only mode."
            )

        # 4. Initial skill sync
        if self.config["skills"]["auto_sync"]:
            try:
                exported = self.skills.export_hermes_skills()
                imported = self.skills.import_openclaw_skills()
                logger.info(f"Skill sync: {exported} exported, {imported} imported")
            except Exception as e:
                logger.warning(f"Skill sync failed: {e}")

        # 5. Launch background tasks
        tasks = [
            asyncio.create_task(self.broker.listen(), name="redis-listen"),
            asyncio.create_task(
                self.broker.start_heartbeat("hermes-bridge"),
                name="heartbeat",
            ),
        ]

        # Gateway listener (if connected)
        if self.gateway._connected.is_set():
            tasks.append(
                asyncio.create_task(self.gateway.listen(), name="gateway-listen")
            )

        # Periodic skill sync
        if self.config["skills"]["auto_sync"]:
            tasks.append(
                asyncio.create_task(self._periodic_skill_sync(), name="skill-sync")
            )

        logger.info("Bridge server is running. Press Ctrl+C to stop.")

        # Wait for stop signal
        await self._stop.wait()

        # Cleanup
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.hermes.shutdown()
        await self.broker.close()
        await self.gateway.close()
        logger.info("Bridge server stopped.")

    # ── Message handlers ──

    async def _handle_gateway_message(self, msg: BridgeMessage):
        """Handle messages from OpenClaw Gateway (WebSocket)."""
        # Forward to Redis for processing
        await self.broker.publish_to_hermes(msg)

    async def _handle_oc_to_hermes(self, msg: BridgeMessage):
        """
        Handle OpenClaw → Hermes messages.

        Dispatches based on action type:
          - DELEGATE_TASK: Full agent loop
          - CALL_TOOL: Single tool invocation
          - SEARCH_SKILLS: Skill registry search
          - QUERY_MEMORY: Session memory search
        """
        logger.info(f"OC→Hermes: action={msg.action}, sender={msg.sender}")

        try:
            if msg.action == Action.DELEGATE_TASK:
                # Inject OpenClaw memory context
                context = msg.params.get("context", {})
                if context:
                    oc_memory = self.memory.inject_openclaw_context_to_hermes(
                        msg.params.get("prompt", "")
                    )
                    if oc_memory:
                        context["openclaw_memory"] = oc_memory
                        msg.params["context"] = context

                result = await self.hermes.run_task(
                    prompt=msg.params.get("prompt", ""),
                    toolsets=msg.params.get("toolsets"),
                    max_iterations=msg.params.get("max_iterations", 30),
                    session_id=msg.params.get("session_id", ""),
                    context=context,
                )
                await self.broker.reply(msg, result=result)

                # Also send result back to Gateway session if available
                session_id = msg.params.get("session_id", "")
                if session_id and self.gateway._connected.is_set():
                    await self.gateway.send_agent_response(
                        session_id,
                        result.get("text", ""),
                        metadata={
                            "source": "hermes",
                            "tools_used": len(result.get("tool_calls", [])),
                            "iterations": result.get("iterations", 0),
                        },
                    )

            elif msg.action == Action.CALL_TOOL:
                result = await self.hermes.call_tool(
                    tool_name=msg.params.get("tool", ""),
                    arguments=msg.params.get("arguments", {}),
                )
                await self.broker.reply(msg, result=result)

            elif msg.action == Action.SEARCH_SKILLS:
                results = await self.hermes.search_skills(
                    query=msg.params.get("query", ""),
                    category=msg.params.get("category", ""),
                    limit=msg.params.get("limit", 10),
                )
                await self.broker.reply(msg, result=results)

            elif msg.action == Action.QUERY_MEMORY:
                results = await self.memory.search(
                    query=msg.params.get("query", ""),
                    limit=msg.params.get("limit", 5),
                )
                await self.broker.reply(msg, result=results)

            else:
                await self.broker.reply(msg, error=f"Unknown action: {msg.action}")

        except Exception as e:
            logger.error(f"Error handling OC→Hermes: {e}", exc_info=True)
            await self.broker.reply(msg, error=str(e))

    async def _handle_hermes_to_oc(self, msg: BridgeMessage):
        """
        Handle Hermes → OpenClaw messages.

        Dispatches to Gateway WebSocket:
          - GATEWAY_SEND: Send message through OC channel
          - AGENT_INVOKE: Invoke an OpenClaw agent
          - CANVAS_RENDER: Render to Canvas/A2UI
          - SKILL_INSTALL: Install skill into OpenClaw
        """
        logger.info(f"Hermes→OC: action={msg.action}, target={msg.target}")

        if not self.gateway._connected.is_set():
            await self.broker.reply(msg, error="Gateway not connected")
            return

        try:
            if msg.action in (
                Action.GATEWAY_SEND,
                Action.AGENT_INVOKE,
                Action.CANVAS_RENDER,
            ):
                await self.gateway.send_to_gateway(msg)
                await self.broker.reply(msg, result={"status": "dispatched"})

            elif msg.action == Action.SKILL_INSTALL:
                # Install a Hermes-learned skill into OpenClaw
                skill_name = msg.params.get("skill_name", "")
                self.skills.export_hermes_skills()
                await self.broker.reply(
                    msg, result={"status": "installed", "skill": skill_name}
                )

            else:
                await self.broker.reply(msg, error=f"Unknown action: {msg.action}")

        except Exception as e:
            logger.error(f"Error handling Hermes→OC: {e}", exc_info=True)
            await self.broker.reply(msg, error=str(e))

    # ── Background tasks ──

    async def _periodic_skill_sync(self):
        """Periodically sync skills between systems."""
        interval = self.config["skills"]["sync_interval_hours"] * 3600
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                self.skills.export_hermes_skills()
                self.skills.import_openclaw_skills()
                logger.info("Periodic skill sync completed")
            except Exception as e:
                logger.error(f"Periodic skill sync error: {e}")

    def stop(self):
        self._stop.set()


def load_config(path: str = "") -> dict:
    """Load configuration from YAML file with defaults."""
    config = DEFAULT_CONFIG.copy()

    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
        for section, values in user_config.items():
            if section in config and isinstance(values, dict):
                config[section].update(values)
            else:
                config[section] = values

    # Environment variable overrides
    if os.getenv("GATEWAY_URL"):
        config["gateway"]["url"] = os.getenv("GATEWAY_URL")
    if os.getenv("GATEWAY_TOKEN"):
        config["gateway"]["auth_token"] = os.getenv("GATEWAY_TOKEN")
    if os.getenv("REDIS_URL"):
        config["redis"]["url"] = os.getenv("REDIS_URL")
    if os.getenv("HERMES_MODEL"):
        config["hermes"]["model"] = os.getenv("HERMES_MODEL")
    if os.getenv("HERMES_PROVIDER"):
        config["hermes"]["provider"] = os.getenv("HERMES_PROVIDER")

    return config


def main():
    parser = argparse.ArgumentParser(description="Hermes-OpenClaw Bridge Server")
    parser.add_argument("--config", "-c", default="bridge.yaml", help="Config file path")
    args = parser.parse_args()

    config = load_config(args.config)
    server = BridgeServer(config)

    loop = asyncio.new_event_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, server.stop)

    try:
        loop.run_until_complete(server.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()

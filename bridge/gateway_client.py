"""
OpenClaw Gateway WebSocket Client.

Connects to the OpenClaw Gateway control plane (default ws://127.0.0.1:18789)
and translates Gateway frames into BridgeMessages.

OpenClaw Gateway protocol:
  - JSON frames validated against JSON Schema
  - Session-based routing with deterministic agent binding
  - Streaming responses via event bus
  - Tools: exec, read, write, edit, browser, memory_search, ...

This client registers as a "bridge channel adapter" so OpenClaw agents
can discover and invoke Hermes capabilities natively.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from bridge.protocol import (
    BridgeMessage, Direction, Action,
    CHANNEL_OC_TO_HERMES, reply_channel_for,
)

logger = logging.getLogger("bridge.gateway")


class GatewayClient:
    """WebSocket client that connects to OpenClaw Gateway."""

    def __init__(
        self,
        gateway_url: str = "ws://127.0.0.1:18789",
        auth_token: str = "",
        agent_id: str = "hermes-bridge",
        on_message: Optional[Callable] = None,
    ):
        self.gateway_url = gateway_url
        self.auth_token = auth_token
        self.agent_id = agent_id
        self.on_message = on_message
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = asyncio.Event()

    # ── Connection lifecycle ──

    async def connect(self):
        """Establish WebSocket connection to Gateway."""
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            self._ws = await websockets.connect(
                self.gateway_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
            )
            # Register as bridge channel
            await self._send_frame({
                "type": "channel.register",
                "channel": "hermes-bridge",
                "capabilities": [
                    "tool_calling",
                    "code_execution",
                    "web_search",
                    "file_operations",
                    "skill_learning",
                    "browser_automation",
                    "image_generation",
                    "delegation",
                ],
                "metadata": {
                    "runtime": "hermes-agent",
                    "version": "0.8.0",
                    "tools_count": 47,
                    "skills_count": 118,
                },
            })
            self._connected.set()
            logger.info(f"Connected to OpenClaw Gateway at {self.gateway_url}")
        except Exception as e:
            logger.error(f"Gateway connection failed: {e}")
            raise

    async def listen(self):
        """Listen for Gateway frames and dispatch to handler."""
        await self._connected.wait()
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                    msg = self._frame_to_bridge_message(frame)
                    if msg and self.on_message:
                        asyncio.create_task(self.on_message(msg))
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from Gateway: {raw[:200]}")
                except Exception as e:
                    logger.error(f"Error processing Gateway frame: {e}")
        except ConnectionClosed as e:
            logger.warning(f"Gateway connection closed: {e}")
            self._connected.clear()

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._connected.clear()

    # ── Send messages back to OpenClaw ──

    async def send_to_gateway(self, msg: BridgeMessage):
        """Send a BridgeMessage back to OpenClaw Gateway as a frame."""
        await self._connected.wait()
        frame = self._bridge_message_to_frame(msg)
        await self._send_frame(frame)

    async def send_agent_response(
        self, session_id: str, text: str, metadata: Optional[dict] = None
    ):
        """Send a text response to an OpenClaw session."""
        await self._send_frame({
            "type": "agent.response",
            "sessionId": session_id,
            "content": text,
            "metadata": metadata or {},
        })

    async def invoke_openclaw_tool(
        self, tool_name: str, params: dict, session_id: str = ""
    ) -> dict:
        """Invoke an OpenClaw native tool through the Gateway."""
        frame = {
            "type": "tool.invoke",
            "tool": tool_name,
            "params": params,
            "sessionId": session_id,
        }
        await self._send_frame(frame)
        # Response arrives via listen() callback
        return {"status": "dispatched", "tool": tool_name}

    # ── Frame translation ──

    def _frame_to_bridge_message(self, frame: dict) -> Optional[BridgeMessage]:
        """Convert an OpenClaw Gateway frame to a BridgeMessage."""
        frame_type = frame.get("type", "")

        if frame_type == "agent.delegate":
            return BridgeMessage(
                direction=Direction.OPENCLAW_TO_HERMES,
                action=Action.DELEGATE_TASK,
                sender=frame.get("agentId", "openclaw"),
                target="hermes",
                params={
                    "prompt": frame.get("prompt", ""),
                    "session_id": frame.get("sessionId", ""),
                    "max_iterations": frame.get("maxIterations", 30),
                    "toolsets": frame.get("toolsets", ["core"]),
                    "context": frame.get("context", {}),
                },
                reply_channel=reply_channel_for(frame.get("runId", "")),
                metadata=frame.get("metadata", {}),
            )

        elif frame_type == "tool.call":
            return BridgeMessage(
                direction=Direction.OPENCLAW_TO_HERMES,
                action=Action.CALL_TOOL,
                sender=frame.get("agentId", "openclaw"),
                target="hermes",
                params={
                    "tool": frame.get("tool", ""),
                    "arguments": frame.get("arguments", {}),
                    "session_id": frame.get("sessionId", ""),
                },
                reply_channel=reply_channel_for(frame.get("callId", "")),
            )

        elif frame_type == "skill.search":
            return BridgeMessage(
                direction=Direction.OPENCLAW_TO_HERMES,
                action=Action.SEARCH_SKILLS,
                sender="openclaw",
                target="hermes",
                params={
                    "query": frame.get("query", ""),
                    "category": frame.get("category", ""),
                    "limit": frame.get("limit", 10),
                },
            )

        elif frame_type == "memory.query":
            return BridgeMessage(
                direction=Direction.OPENCLAW_TO_HERMES,
                action=Action.QUERY_MEMORY,
                sender="openclaw",
                target="hermes",
                params={
                    "query": frame.get("query", ""),
                    "session_id": frame.get("sessionId", ""),
                    "limit": frame.get("limit", 5),
                },
            )

        elif frame_type in ("channel.ack", "heartbeat"):
            return None  # internal, skip

        logger.debug(f"Unhandled Gateway frame type: {frame_type}")
        return None

    def _bridge_message_to_frame(self, msg: BridgeMessage) -> dict:
        """Convert a BridgeMessage to an OpenClaw Gateway frame."""
        if msg.action == Action.GATEWAY_SEND:
            return {
                "type": "channel.send",
                "channelId": msg.params.get("channel_id", ""),
                "content": msg.result or msg.params.get("text", ""),
                "metadata": msg.metadata,
            }
        elif msg.action == Action.CANVAS_RENDER:
            return {
                "type": "canvas.render",
                "html": msg.params.get("html", ""),
                "title": msg.params.get("title", ""),
                "sessionId": msg.params.get("session_id", ""),
            }
        elif msg.action == Action.AGENT_INVOKE:
            return {
                "type": "agent.invoke",
                "agentId": msg.params.get("agent_id", ""),
                "action": msg.params.get("action", ""),
                "params": msg.params.get("params", {}),
            }
        else:
            # Generic result frame
            return {
                "type": "bridge.result",
                "messageId": msg.id,
                "result": msg.result,
                "error": msg.error,
            }

    async def _send_frame(self, frame: dict):
        if self._ws:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))

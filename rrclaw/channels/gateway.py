"""
OpenClaw Gateway Channel — WebSocket connection for message routing.

Refactored from bridge/gateway_client.py to work with ConversationRuntime.
Gateway is now a pure channel layer; RRCLAW controls the LLM loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("rrclaw.channels.gateway")


class GatewayChannel:
    """
    WebSocket client for OpenClaw Gateway.

    In the new architecture, Gateway only routes messages.
    RRCLAW's ConversationRuntime handles all agent logic.
    """

    def __init__(
        self,
        gateway_url: str = "ws://127.0.0.1:18789",
        auth_token: str = "",
        agent_id: str = "rrclaw",
        on_user_message: Callable | None = None,
    ):
        self.gateway_url = gateway_url
        self.auth_token = auth_token
        self.agent_id = agent_id
        self.on_user_message = on_user_message
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = asyncio.Event()
        self._reconnect_delay = 5.0
        self._max_reconnect_delay = 60.0
        self._should_run = True

    async def connect(self):
        """Connect and register as RRCLAW agent."""
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
            # Register as RRCLAW harness (not just a bridge)
            await self._send({
                "type": "channel.register",
                "channel": self.agent_id,
                "capabilities": [
                    "tool_calling",
                    "code_execution",
                    "web_search",
                    "file_operations",
                    "skill_learning",
                    "browser_automation",
                    "delegation",
                    "self_learning",
                    "context_compression",
                    "multi_provider",
                ],
                "metadata": {
                    "runtime": "rrclaw-harness",
                    "version": "0.1.0",
                    "architecture": "conversation_runtime",
                },
            })
            self._connected.set()
            self._reconnect_delay = 5.0
            logger.info(f"Connected to Gateway: {self.gateway_url}")
        except Exception as e:
            logger.error(f"Gateway connection failed: {e}")
            raise

    async def listen(self):
        """Main listen loop with auto-reconnect."""
        while self._should_run:
            try:
                if not self._connected.is_set():
                    await self.connect()

                await self._connected.wait()
                async for raw in self._ws:
                    try:
                        frame = json.loads(raw)
                        await self._dispatch(frame)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from Gateway: {raw[:200]}")
                    except Exception as e:
                        logger.error(f"Error processing frame: {e}")

            except ConnectionClosed as e:
                logger.warning(f"Gateway disconnected: {e}")
                self._connected.clear()
            except Exception as e:
                logger.error(f"Gateway error: {e}")
                self._connected.clear()

            if self._should_run:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _dispatch(self, frame: dict):
        """Route incoming Gateway frames to handlers."""
        frame_type = frame.get("type", "")

        if frame_type == "agent.delegate":
            if self.on_user_message:
                await self.on_user_message(
                    session_id=frame.get("sessionId", ""),
                    prompt=frame.get("prompt", ""),
                    context=frame.get("context", {}),
                    metadata=frame.get("metadata", {}),
                )

        elif frame_type == "user.message":
            if self.on_user_message:
                await self.on_user_message(
                    session_id=frame.get("sessionId", ""),
                    prompt=frame.get("content", ""),
                    context=frame.get("context", {}),
                    metadata=frame.get("metadata", {}),
                )

        elif frame_type in ("channel.ack", "heartbeat", "pong"):
            pass  # internal

        else:
            logger.debug(f"Unhandled frame type: {frame_type}")

    # ── Outbound methods ──

    async def send_text(self, session_id: str, text: str, metadata: dict | None = None):
        """Send text response to a session."""
        await self._connected.wait()
        await self._send({
            "type": "agent.response",
            "sessionId": session_id,
            "content": text,
            "metadata": metadata or {},
        })

    async def send_stream_delta(self, session_id: str, delta: str):
        """Send streaming text delta."""
        await self._connected.wait()
        await self._send({
            "type": "agent.stream",
            "sessionId": session_id,
            "delta": delta,
        })

    async def send_stream_end(self, session_id: str):
        """Signal end of streaming response."""
        await self._connected.wait()
        await self._send({
            "type": "agent.stream.end",
            "sessionId": session_id,
        })

    async def send_tool_status(
        self, session_id: str, tool_name: str, status: str, result: str = ""
    ):
        """Notify Gateway about tool execution status."""
        await self._connected.wait()
        await self._send({
            "type": "agent.tool_status",
            "sessionId": session_id,
            "tool": tool_name,
            "status": status,
            "result": result,
        })

    async def canvas_present(self, session_id: str, html: str, title: str = ""):
        """Render HTML on OpenClaw Canvas."""
        await self._connected.wait()
        await self._send({
            "type": "canvas.render",
            "sessionId": session_id,
            "html": html,
            "title": title,
        })

    async def invoke_agent(self, agent_id: str, action: str, params: dict):
        """Invoke another OpenClaw agent."""
        await self._connected.wait()
        await self._send({
            "type": "agent.invoke",
            "agentId": agent_id,
            "action": action,
            "params": params,
        })

    async def close(self):
        self._should_run = False
        if self._ws:
            await self._ws.close()
        self._connected.clear()

    async def _send(self, frame: dict):
        if self._ws:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

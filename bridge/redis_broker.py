"""
Redis Pub/Sub Message Broker.

Central message bus that connects the Gateway client and Hermes runtime
through Redis channels.  Provides:

  - Typed publish/subscribe on bridge channels
  - Dedicated per-message reply channels (avoids deadlocks)
  - Heartbeat broadcasting for health monitoring
  - Message serialization with BridgeMessage protocol
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Optional

import redis.asyncio as aioredis

from bridge.protocol import (
    BridgeMessage,
    CHANNEL_OC_TO_HERMES,
    CHANNEL_HERMES_TO_OC,
    CHANNEL_HEARTBEAT,
    reply_channel_for,
)

logger = logging.getLogger("bridge.redis")

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


class RedisBroker:
    """
    Async Redis Pub/Sub broker for bridge messages.

    Manages subscriptions on the two main channels plus
    dynamic per-message reply channels.
    """

    def __init__(self, redis_url: str = REDIS_URL):
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._handlers: dict[str, Callable] = {}
        self._running = False

    async def connect(self):
        """Establish Redis connection and subscribe to bridge channels."""
        self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(
            CHANNEL_OC_TO_HERMES,
            CHANNEL_HERMES_TO_OC,
            CHANNEL_HEARTBEAT,
        )
        logger.info(f"Redis broker connected: {self.redis_url}")

    def on(self, channel: str, handler: Callable):
        """Register a handler for a specific channel."""
        self._handlers[channel] = handler

    async def listen(self):
        """Main listen loop — dispatches messages to registered handlers."""
        self._running = True
        try:
            async for raw in self._pubsub.listen():
                if not self._running:
                    break
                if raw["type"] != "message":
                    continue

                channel = raw["channel"]
                try:
                    data = json.loads(raw["data"])
                    msg = BridgeMessage.from_dict(data)
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(f"Invalid message on {channel}: {e}")
                    continue

                handler = self._handlers.get(channel)
                if handler:
                    asyncio.create_task(handler(msg))
                else:
                    logger.debug(f"No handler for channel: {channel}")
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    # ── Publishing ──

    async def publish(self, channel: str, msg: BridgeMessage):
        """Publish a BridgeMessage to a Redis channel."""
        data = json.dumps(msg.to_dict(), ensure_ascii=False)
        await self._redis.publish(channel, data)

    async def publish_to_hermes(self, msg: BridgeMessage):
        await self.publish(CHANNEL_OC_TO_HERMES, msg)

    async def publish_to_openclaw(self, msg: BridgeMessage):
        await self.publish(CHANNEL_HERMES_TO_OC, msg)

    async def reply(self, original: BridgeMessage, result: Any = None, error: str = ""):
        """Send a reply on the dedicated reply channel."""
        reply_msg = original.make_reply(result=result, error=error)
        channel = original.reply_channel or reply_channel_for(original.id)
        await self.publish(channel, reply_msg)

    # ── Request/Reply pattern ──

    async def request(
        self, channel: str, msg: BridgeMessage, timeout: float = 180.0
    ) -> BridgeMessage:
        """
        Publish a message and wait for a reply on a dedicated channel.

        Uses a per-message reply channel to avoid cross-talk.
        """
        reply_ch = reply_channel_for(msg.id)
        msg.reply_channel = reply_ch

        # Subscribe to reply channel before publishing
        reply_pubsub = self._redis.pubsub()
        await reply_pubsub.subscribe(reply_ch)

        try:
            await self.publish(channel, msg)

            deadline = time.time() + timeout
            async for raw in reply_pubsub.listen():
                if time.time() > deadline:
                    return msg.make_reply(error=f"Timeout ({timeout}s)")
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                    reply = BridgeMessage.from_dict(data)
                    if reply.id == msg.id:
                        return reply
                except Exception:
                    continue
        finally:
            await reply_pubsub.unsubscribe(reply_ch)
            await reply_pubsub.aclose()

    # ── Heartbeat ──

    async def start_heartbeat(self, component: str, interval: float = 10.0):
        """Periodically publish heartbeat on the heartbeat channel."""
        while self._running:
            try:
                msg = BridgeMessage(
                    action="heartbeat",
                    sender=component,
                    result={
                        "ts": time.time(),
                        "pid": os.getpid(),
                        "component": component,
                    },
                )
                await self.publish(CHANNEL_HEARTBEAT, msg)
                # Also store in hash for quick lookup
                await self._redis.hset(
                    "bridge:heartbeats",
                    component,
                    json.dumps(msg.result, ensure_ascii=False),
                )
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(interval)

    # ── Cleanup ──

    async def close(self):
        self._running = False
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.aclose()
        if self._redis:
            await self._redis.aclose()
        logger.info("Redis broker closed")

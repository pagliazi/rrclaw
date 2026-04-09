"""
Message protocol for Hermes-OpenClaw Bridge.

Defines the canonical envelope that travels between the two systems
over Redis Pub/Sub.  Both OpenClaw Gateway frames and Hermes tool
schemas are normalised into this format before crossing the bridge.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Direction(str, Enum):
    """Which system originated the message."""
    OPENCLAW_TO_HERMES = "openclaw→hermes"
    HERMES_TO_OPENCLAW = "hermes→openclaw"


class Action(str, Enum):
    """Supported bridge actions."""
    # OpenClaw → Hermes
    DELEGATE_TASK = "delegate_task"          # Run full Hermes agent loop
    CALL_TOOL = "call_tool"                  # Call a single Hermes tool
    SEARCH_SKILLS = "search_skills"          # Search Hermes skill registry
    QUERY_MEMORY = "query_memory"            # Search Hermes session memory

    # Hermes → OpenClaw
    GATEWAY_SEND = "gateway_send"            # Send message through OC channel
    AGENT_INVOKE = "agent_invoke"            # Invoke an OpenClaw agent/skill
    CANVAS_RENDER = "canvas_render"          # Render to OpenClaw Canvas/A2UI
    SKILL_INSTALL = "skill_install"          # Install a skill into OpenClaw
    SESSION_QUERY = "session_query"          # Query OpenClaw session state

    # Bidirectional
    HEARTBEAT = "heartbeat"
    ACK = "ack"
    ERROR = "error"


@dataclass
class BridgeMessage:
    """Canonical message envelope for cross-system communication."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    direction: str = ""
    action: str = ""
    sender: str = ""                 # originating component
    target: str = ""                 # destination component
    params: dict[str, Any] = field(default_factory=dict)
    reply_channel: str = ""          # dedicated reply Pub/Sub channel
    timestamp: float = field(default_factory=time.time)
    result: Optional[Any] = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BridgeMessage":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def make_reply(self, result: Any = None, error: str = "") -> "BridgeMessage":
        """Create a reply message for this request."""
        return BridgeMessage(
            id=self.id,
            direction=Direction.HERMES_TO_OPENCLAW
            if self.direction == Direction.OPENCLAW_TO_HERMES
            else Direction.OPENCLAW_TO_HERMES,
            action=Action.ACK if not error else Action.ERROR,
            sender=self.target,
            target=self.sender,
            result=result,
            error=error,
            metadata={"reply_to": self.id},
        )


# ── Redis channel names ──

CHANNEL_OC_TO_HERMES = "bridge:openclaw→hermes"
CHANNEL_HERMES_TO_OC = "bridge:hermes→openclaw"
CHANNEL_HEARTBEAT = "bridge:heartbeat"

def reply_channel_for(msg_id: str) -> str:
    return f"bridge:reply:{msg_id}"

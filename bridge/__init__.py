"""
Hermes-OpenClaw Bridge — Cross-runtime bidirectional integration.

Connects OpenClaw Gateway (Node.js, WebSocket :18789) with
Hermes Agent (Python, AIAgent runtime) via Redis Pub/Sub.

Both systems retain full autonomy; the bridge acts as a thin
translation layer that maps OpenClaw agent-loop events to
Hermes tool calls and vice versa.
"""

__version__ = "0.1.0"

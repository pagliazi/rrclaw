"""
Hermes-OpenClaw Bidirectional Bridge

Architecture:
  OpenClaw Orchestrator -> Redis Pub/Sub -> Hermes Bridge Server -> AIAgent
  Hermes AIAgent -> OpenClaw Domain Tools -> Redis Pub/Sub -> OpenClaw Orchestrator

Channels:
  openclaw:hermes          — OpenClaw dispatches tasks to Hermes
  openclaw:orchestrator    — Hermes calls back into OpenClaw agents
  openclaw:heartbeats      — Hermes publishes liveness heartbeats
"""

<div align="center">

# Hermes-OpenClaw Bridge

**Bidirectional Runtime Bridge Between [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [OpenClaw](https://github.com/openclaw/openclaw)**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://python.org)
[![Redis](https://img.shields.io/badge/Redis-Pub%2FSub-red.svg)](https://redis.io)
[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-v0.8-purple.svg)](https://github.com/NousResearch/hermes-agent)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-2026.4-orange.svg)](https://github.com/openclaw/openclaw)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

*Connects two of the most capable open-source agent frameworks through a thin Redis Pub/Sub translation layer — giving Hermes access to OpenClaw's 5,400+ skills and 11 channel adapters, and giving OpenClaw access to Hermes' 47 tools, PTC code execution, and self-improving learning loop.*

[Why](#why-this-bridge) | [Architecture](#architecture) | [Quick Start](#quick-start) | [Deployment](#deployment) | [Customization](#customization) | [Protocol](#redis-protocol)

</div>

---

## Why This Bridge

**Hermes Agent** (by [Nous Research](https://nousresearch.com)) is a self-improving Python agent with 47 tools, PTC (Programmatic Tool Calling), 40+ LLM providers, and a learning loop that creates reusable skills autonomously.

**OpenClaw** (by [openclaw](https://github.com/openclaw/openclaw)) is a Node.js/TypeScript AI gateway with 11+ channel adapters (WhatsApp, Telegram, Slack, Discord, Signal, iMessage...), 5,400+ skills on ClawHub, Canvas/A2UI visualization, and enterprise governance.

Alone, each is powerful. Together, they're complementary:

| Capability | Hermes | OpenClaw | **Bridge** |
|---|---|---|---|
| Code execution (PTC sandbox) | 47 tools, 6 backends | `exec` tool with Docker sandbox | Cross-system PTC |
| Messaging platforms | 15+ via gateway | 11+ channel adapters | Any-to-any routing |
| Skill ecosystem | 118 built-in + self-learned | 5,400+ on ClawHub | Bidirectional sync |
| Memory | SQLite FTS5 + Honcho user model | Markdown files + vector search | Unified search |
| Browser automation | Camofox stealth + Chrome | Chrome DevTools attach | Shared sessions |
| LLM providers | 40+ with runtime switching | ClawRouter + OAuth | Best-of-both |
| Learning loop | Autonomous skill creation | Skill install from ClawHub | Skills flow both ways |
| Canvas / A2UI | No | Interactive HTML workspace | Hermes renders to Canvas |
| Exec sandboxing | Docker, SSH, Daytona, Modal, Singularity | Docker, SSH, OpenShell | Shared backends |
| Governance | Session-level | Enterprise (risk, approvals, audit) | Policy enforcement |

**The bridge is a ~1,200-line Python package** that sits between the two systems and translates messages over Redis Pub/Sub. Neither system needs modification.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│   User Interfaces                                                        │
│   WhatsApp · Telegram · Slack · Discord · Signal · iMessage · WebChat    │
│   Feishu · LINE · Matrix · Teams · Google Chat · ...                     │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │  Normalized messages
                           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   OpenClaw Gateway (Node.js :18789)                       │
│                                                                          │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐  │
│  │  Channel   │  │  Agent   │  │  Canvas  │  │   Exec Sandboxing     │  │
│  │  Adapters  │  │  Router  │  │  A2UI    │  │   Docker/SSH/Daytona  │  │
│  │   (11+)    │  │  (bind)  │  │          │  │                       │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────────────────┘  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐  │
│  │  Skills    │  │  Memory  │  │  Cron &  │  │   Governance          │  │
│  │  (5,400+)  │  │  (MD+Vec)│  │  Webhook │  │   Risk/Approve/Audit  │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────────────────┘  │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │  WebSocket frames
                           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   Hermes-OpenClaw Bridge (Python)                         │
│                                                                          │
│  ┌────────────────┐  ┌─────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ GatewayClient  │  │ RedisBroker │  │ SkillBridge│  │ MemoryBridge │  │
│  │ (WebSocket)    │  │ (Pub/Sub)   │  │ (sync)     │  │ (unified)    │  │
│  └────────┬───────┘  └──────┬──────┘  └──────┬─────┘  └──────┬───────┘  │
│           │                 │                │               │           │
│           └─────────────────┴────────────────┴───────────────┘           │
│                                    │                                     │
│                          ┌─────────▼──────────┐                          │
│                          │  HermesRuntime     │                          │
│                          │  (AIAgent wrapper) │                          │
│                          └─────────┬──────────┘                          │
└────────────────────────────────────┼─────────────────────────────────────┘
                                     │  Python imports
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   Hermes Agent Runtime (Python)                           │
│                                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ AIAgent  │  │ Tools    │  │ Skills   │  │ Memory   │  │ Learning │  │
│  │ (ReAct   │  │ (47 in   │  │ (118+    │  │ (SQLite  │  │ Loop     │  │
│  │  loop)   │  │  20 sets)│  │  self-   │  │  FTS5 +  │  │ (auto    │  │
│  │          │  │          │  │  learned)│  │  Honcho)  │  │  skill)  │  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │ PTC Code │  │ Browser  │  │ Terminal │  │ 40+ LLM  │               │
│  │ Execution│  │ Camofox  │  │ 6 Backs  │  │ Providers│               │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘               │
└──────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

**OpenClaw → Hermes** (delegate complex tasks)
```
User @Telegram: "Search for the latest AI chip papers and write a research report"
  → Telegram adapter → OpenClaw Gateway → Agent router
  → Agent invokes hermes-bridge skill
  → Bridge receives WebSocket frame, creates BridgeMessage
  → Publishes to Redis: bridge:openclaw→hermes
  → HermesRuntime spawns AIAgent with toolsets=[core, web, browser]
  → Agent executes: web_search → web_extract → write_file (PTC chain)
  → Result published to bridge:reply:{msg_id}
  → Bridge sends result back through Gateway → Telegram
```

**Hermes → OpenClaw** (use Gateway channels & Canvas)
```
Hermes agent needs to notify a Slack channel:
  → Calls tool: openclaw_send_message(channel="slack", message="Report ready")
  → Tool publishes to Redis: bridge:hermes→openclaw
  → Bridge receives, translates to Gateway frame
  → WebSocket → Gateway → Slack adapter → Slack API
  → Confirmation flows back through Redis
```

**Skill Sync** (bidirectional, periodic)
```
Both systems use AgentSkills-compatible SKILL.md format
  → SkillBridge scans ~/.hermes/skills/ and ~/.openclaw/skills/
  → Translates tool references (web_search ↔ webSearch)
  → Copies new/updated skills across
  → Hermes self-learned skills automatically available to OpenClaw
  → ClawHub skills automatically available to Hermes
```

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Node.js 22+** (for OpenClaw)
- **Redis 7+**

### 1. Install OpenClaw

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
openclaw onboard --install-daemon
```

### 2. Install Hermes Agent

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
hermes setup     # Configure your LLM provider
```

### 3. Install the Bridge

```bash
git clone https://github.com/pagliazi/hermes-openclaw-bridge.git
cd hermes-openclaw-bridge
pip install -e .

# Configure
cp config.example.yaml bridge.yaml
# Edit bridge.yaml with your settings
```

### 4. Start Everything

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: OpenClaw Gateway
openclaw daemon

# Terminal 3: Bridge
python -m bridge.server --config bridge.yaml
```

### 5. Verify

```bash
# Check bridge heartbeat
redis-cli HGET bridge:heartbeats hermes-bridge

# Check bridge channels
redis-cli SUBSCRIBE bridge:heartbeat
```

---

## Deployment

### Docker Compose (Recommended)

The fastest way to get the full stack running:

```bash
cd deploy/

# Set your LLM API key
export HERMES_MODEL=gpt-4o
export HERMES_PROVIDER=openai

docker compose up -d
```

This starts 4 containers:
| Container | Port | Role |
|-----------|------|------|
| `redis` | 6379 | Message broker |
| `openclaw` | 18789 | Gateway + channel adapters |
| `hermes-agent` | — | AI agent runtime |
| `bridge` | — | Translation layer |

### macOS (launchd)

```bash
# Copy the bridge
sudo cp -r . /opt/hermes-openclaw-bridge
cd /opt/hermes-openclaw-bridge
python -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml bridge.yaml

# Install LaunchAgent
cp deploy/com.hermes-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hermes-bridge.plist
```

### Linux (systemd)

```bash
sudo cp -r . /opt/hermes-openclaw-bridge
cd /opt/hermes-openclaw-bridge
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml bridge.yaml

sudo cp deploy/hermes-bridge.service /etc/systemd/system/
sudo systemctl enable --now hermes-bridge
```

### Configuration

The bridge uses a 3-tier configuration: `bridge.yaml` → environment variables → defaults.

```yaml
# bridge.yaml
gateway:
  url: "ws://127.0.0.1:18789"    # OpenClaw Gateway WebSocket
  auth_token: ""                   # Optional auth token

hermes:
  model: "gpt-4o"                 # Any of 40+ supported models
  provider: "openai"              # openai, anthropic, ollama, custom, ...
  profile: "bridge"               # Hermes profile (isolated config/skills/memory)
  max_workers: 4                  # Parallel agent instances
  default_toolsets:               # Tools available to bridge tasks
    - core                        # terminal, file ops, process
    - web                         # web_search, web_extract
    - terminal                    # shell execution
    - browser                     # Chrome automation

redis:
  url: "redis://127.0.0.1:6379/0"

skills:
  auto_sync: true                 # Sync skills between systems
  sync_interval_hours: 6
```

Environment variable overrides:
```bash
export GATEWAY_URL=ws://192.168.1.100:18789
export GATEWAY_TOKEN=your-token
export REDIS_URL=redis://redis-host:6379/0
export HERMES_MODEL=claude-sonnet-4-20250514
export HERMES_PROVIDER=anthropic
export HERMES_AGENT_PATH=/path/to/hermes-agent
```

---

## Bridge Components

### `bridge/server.py` — Main Server

Orchestrates all components. Connects Gateway WebSocket + Redis broker + Hermes runtime. Handles message routing between systems.

### `bridge/gateway_client.py` — OpenClaw WebSocket Client

Connects to the OpenClaw Gateway as a "bridge channel adapter". Translates Gateway JSON frames into `BridgeMessage` protocol and back.

Exposes OpenClaw capabilities:
- **Channel sending** → any of 11+ messaging platforms
- **Agent invocation** → trigger OpenClaw agents
- **Canvas rendering** → push interactive HTML to A2UI
- **Skill search** → query ClawHub registry

### `bridge/hermes_runtime.py` — Hermes Agent Wrapper

Wraps the Hermes `AIAgent` class for bridge use. Runs agent loops in a thread pool to stay async-friendly.

Exposes Hermes capabilities:
- **Task delegation** → full ReAct agent loop with 47 tools
- **Single tool calls** → direct tool invocation without agent loop
- **Skill search** → search 118+ built-in and self-learned skills
- **Memory search** → FTS5 search over session history

### `bridge/redis_broker.py` — Message Broker

Redis Pub/Sub broker with typed channels, request/reply pattern using dedicated per-message channels (prevents deadlocks), and heartbeat broadcasting.

### `bridge/skill_bridge.py` — Skill Synchronization

Both systems use the AgentSkills-compatible `SKILL.md` format. This module:
- Exports Hermes skills → OpenClaw workspace (with tool name translation)
- Imports OpenClaw/ClawHub skills → Hermes directory
- Tracks sync state to avoid redundant copies

### `bridge/memory_bridge.py` — Memory Bridge

Bridges two different memory architectures:
- **Hermes**: SQLite FTS5 + persistent nudge + Honcho user modeling
- **OpenClaw**: Markdown files (MEMORY.md, daily notes, DREAMS.md) + vector search

Provides unified cross-system search and context injection.

### `toolsets/openclaw_toolset.py` — Hermes Toolset

Registers 6 OpenClaw-native tools in the Hermes tool registry:
- `openclaw_send_message` — Send via any of 11+ channel adapters
- `openclaw_invoke_agent` — Trigger any OpenClaw agent
- `openclaw_canvas` — Render interactive HTML on Canvas/A2UI
- `openclaw_search_skills` — Search ClawHub (5,400+ skills)
- `openclaw_memory_search` — Search OpenClaw memory
- `openclaw_install_skill` — Push a Hermes skill to OpenClaw

### `skills/hermes-bridge/SKILL.md` — OpenClaw Skill

An OpenClaw-compatible skill that teaches OpenClaw agents how to delegate tasks to Hermes. Includes documentation of all available Hermes toolsets and usage patterns.

---

## Redis Protocol

### Channels

| Channel | Direction | Purpose |
|---------|-----------|---------|
| `bridge:openclaw→hermes` | OC → H | Task delegation, tool calls |
| `bridge:hermes→openclaw` | H → OC | Channel sending, agent invocation |
| `bridge:reply:{msg_id}` | Both | Per-message reply (prevents deadlock) |
| `bridge:heartbeat` | Both | Health monitoring |

### Message Envelope

```json
{
  "id": "a1b2c3d4e5f67890",
  "direction": "openclaw→hermes",
  "action": "delegate_task",
  "sender": "openclaw-agent-123",
  "target": "hermes",
  "params": {
    "prompt": "Search for AI chip news and write a report",
    "toolsets": ["core", "web"],
    "max_iterations": 30
  },
  "reply_channel": "bridge:reply:a1b2c3d4e5f67890",
  "timestamp": 1712649600.0,
  "result": null,
  "error": "",
  "metadata": {}
}
```

### Actions

| Action | Direction | Description |
|--------|-----------|-------------|
| `delegate_task` | OC→H | Run full Hermes agent loop |
| `call_tool` | OC→H | Invoke a single Hermes tool |
| `search_skills` | OC→H | Search Hermes skill registry |
| `query_memory` | OC→H | Search Hermes session memory |
| `gateway_send` | H→OC | Send message through channel adapter |
| `agent_invoke` | H→OC | Invoke an OpenClaw agent |
| `canvas_render` | H→OC | Render HTML on Canvas/A2UI |
| `skill_install` | H→OC | Install skill into OpenClaw |
| `heartbeat` | Both | Liveness signal |

---

## Customization

### Add Hermes Tools to OpenClaw

Edit `toolsets/openclaw_toolset.py` — add to `OPENCLAW_TOOLS` list:

```python
{
    "name": "openclaw_your_tool",
    "description": "What this tool does",
    "params": {...},
    "required": ["param1"],
    "handler": lambda args: _sync_call("your_action", {
        "key": args.get("param1", ""),
    }),
},
```

### Add OpenClaw Skills to Hermes

Drop a `SKILL.md` file into `skills/your-skill/`:

```markdown
---
name: your-skill
description: What this skill does
version: 1.0.0
---
# Instructions for the agent
```

### Change LLM Model

Runtime switching without restart:

```bash
# In Hermes CLI
hermes model    # Interactive selector

# Or via config
export HERMES_MODEL=claude-sonnet-4-20250514
export HERMES_PROVIDER=anthropic
```

Supported providers include OpenAI, Anthropic, Google Gemini, DeepSeek, Ollama (local), vLLM, OpenRouter (200+ models), and many more.

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Bridge can't connect to Gateway | OpenClaw not running | `openclaw daemon` or `openclaw onboard --install-daemon` |
| `hermes-agent` import error | Path not set | `export HERMES_AGENT_PATH=~/hermes-agent` |
| Redis timeout | Redis not running | `redis-server` or `brew services start redis` |
| Tool call returns empty | OpenClaw agent not bound | Check agent bindings with `openclaw status` |
| Skills not syncing | Wrong directory | Check `HERMES_SKILLS_DIR` and `OPENCLAW_SKILLS_DIR` |
| WebSocket disconnects | Network issue or auth | Check `GATEWAY_TOKEN` in bridge.yaml |

### Debug

```bash
# Monitor all bridge messages
redis-cli SUBSCRIBE "bridge:openclaw→hermes" "bridge:hermes→openclaw"

# Check heartbeats
redis-cli HGETALL bridge:heartbeats

# Check bridge logs
journalctl -u hermes-bridge -f    # Linux
tail -f /var/log/hermes-bridge.log # macOS
```

---

## Project Structure

```
hermes-openclaw-bridge/
├── README.md
├── LICENSE                        # MIT
├── pyproject.toml                 # Package metadata & dependencies
├── config.example.yaml            # Configuration template
├── .gitignore
│
├── bridge/                        # Core bridge package
│   ├── __init__.py               # Package info
│   ├── __main__.py               # python -m bridge entry point
│   ├── server.py                 # Main server orchestrating all components
│   ├── protocol.py               # BridgeMessage envelope & channel definitions
│   ├── gateway_client.py         # OpenClaw Gateway WebSocket client
│   ├── hermes_runtime.py         # Hermes AIAgent wrapper
│   ├── redis_broker.py           # Redis Pub/Sub message broker
│   ├── skill_bridge.py           # Bidirectional skill synchronization
│   └── memory_bridge.py          # Cross-system memory search & injection
│
├── toolsets/                      # Hermes toolset for OpenClaw capabilities
│   ├── __init__.py
│   └── openclaw_toolset.py       # 6 tools: send_message, invoke_agent,
│                                 #   canvas, search_skills, memory, install
│
├── skills/                        # OpenClaw-compatible skill definitions
│   └── hermes-bridge/
│       └── SKILL.md              # Teaches OpenClaw agents to use Hermes
│
└── deploy/                        # Deployment configurations
    ├── docker-compose.yaml        # Full stack: Redis + OpenClaw + Hermes + Bridge
    ├── Dockerfile                 # Bridge container image
    ├── hermes-bridge.service      # Linux systemd unit
    └── com.hermes-bridge.plist    # macOS launchd agent
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

Built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research + [OpenClaw](https://github.com/openclaw/openclaw)

</div>

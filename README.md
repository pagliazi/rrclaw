<div align="center">

# Hermes-OpenClaw Bridge

**Bidirectional Integration Between Hermes AI Agent and OpenClaw Multi-Agent System**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://python.org)
[![Redis](https://img.shields.io/badge/Redis-Pub%2FSub-red.svg)](https://redis.io)
[![Hermes Agent](https://img.shields.io/badge/Hermes-Agent-purple.svg)](https://github.com/NousResearch/hermes-agent)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

*将 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的通用 AI 能力（文件操作、终端、网页搜索、代码执行）与 [OpenClaw](https://github.com/openclaw/openclaw) 的 13 个专业领域 Agent 通过 Redis Pub/Sub 双向桥接，实现能力互补。*

[Architecture](#architecture) | [How It Works](#how-it-works) | [Deployment](#deployment) | [API Reference](#api-reference) | [Customization](#customization)

</div>

---

## Why This Bridge

| | Hermes Agent (Standalone) | OpenClaw (Standalone) | **Hermes + OpenClaw (Bridge)** |
|---|---|---|---|
| Terminal/File ops | Yes | No | Yes |
| Web search | Yes | Limited | Full |
| Code execution (PTC) | Yes | No | Yes |
| A-stock market data | No | Yes (13 agents) | Yes |
| Strategy backtesting | No | Yes | Yes |
| Multi-agent routing | No | L0/L1/L2 cascade | Cross-system cascade |
| Memory | Session-only | 3-tier (Vector+Graph+Timeline) | Shared via Redis |
| Skill learning | Yes (self-evolving) | Predefined skills | Combined |

**Key insight**: Hermes excels at general-purpose agentic tasks (tool calling, code execution, file manipulation); OpenClaw excels at domain-specific financial intelligence. The bridge lets each system invoke the other's full capability set.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        User / Frontend                               │
│              (Telegram, Feishu, WebChat, N8N)                        │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    OpenClaw Orchestrator                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────────────┐  │
│  │ L0 Regex │→ │ L1 Triage│→ │ L2 Plan  │  │  SystemCommands     │  │
│  │ (< 5ms)  │  │ (1-3s)   │  │ (5-30s)  │  │  ._call_hermes()   │  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┬──────────┘  │
└────────────────────┬────────────────────────────────────┼────────────┘
                     │                                    │
          Redis Pub/Sub                         Redis Pub/Sub
      (openclaw:orchestrator)                  (openclaw:hermes)
                     │                                    │
   ┌─────────┬──────┼──────┬──────────┐                   │
   ▼         ▼      ▼      ▼          ▼                   ▼
┌──────┐ ┌──────┐ ┌────┐ ┌──────┐ ┌──────┐   ┌────────────────────┐
│Market│ │ News │ │Dev │ │ Back │ │ ...  │   │ Hermes Bridge      │
│Agent │ │Agent │ │Agt │ │ test │ │+8more│   │ Server             │
└──────┘ └──────┘ └────┘ └──────┘ └──────┘   │                    │
                                              │  ┌──────────────┐  │
                                              │  │  AIAgent     │  │
                                              │  │  (run_agent) │  │
                                              │  └──────┬───────┘  │
                                              │         │          │
                                              │  ┌──────▼───────┐  │
                                              │  │ OpenClaw     │  │
                                              │  │ Domain Tools │◄─┤─ Redis callback
                                              │  │ (10 tools)   │  │  to orchestrator
                                              │  └──────────────┘  │
                                              └────────────────────┘
```

### Data Flow

**Direction 1: OpenClaw → Hermes** (delegate complex tasks)

```
User: "/hermes 搜索最新 AI 芯片新闻并生成投资摘要"
  → Orchestrator.system_commands._call_hermes()
  → Redis PUBLISH openclaw:hermes {action: "hermes_task", prompt: "..."}
  → Hermes Bridge Server receives message
  → AIAgent.chat(prompt) with toolsets=["openclaw", "core"]
  → Agent uses web_search, file tools, AND openclaw_* tools
  → Result published to openclaw:orchestrator:replies:{msg_id}
  → Orchestrator returns result to user
```

**Direction 2: Hermes → OpenClaw** (call domain agents)

```
Hermes Agent decides to check market data
  → Calls tool: openclaw_market(action="zt")
  → _call_openclaw() publishes to Redis openclaw:orchestrator
  → OpenClaw routes to MarketAgent.get_limitup()
  → Result returned to Hermes via Redis openclaw:hermes
  → Agent incorporates data into its reasoning chain
```

---

## How It Works

### 1. Bridge Server (`hermes/openclaw_bridge_server.py`)

The bridge server is a long-running async process that:
- Subscribes to the `openclaw:hermes` Redis channel
- On receiving a task, instantiates a Hermes `AIAgent` with `enabled_toolsets=["openclaw", "core"]`
- Runs the agent's `chat()` method (synchronous) in a thread pool executor
- Publishes results back via a dedicated reply channel
- Sends heartbeat every 10s to `openclaw:heartbeats` for monitoring

```python
agent = AIAgent(
    model="qwen3.5-plus",           # Configurable via ~/.hermes/config.yaml
    provider="custom",
    enabled_toolsets=["openclaw", "core"],  # Both systems' tools available
    max_iterations=30,
)
result = await loop.run_in_executor(None, agent.chat, prompt)
```

### 2. Domain Tools (`hermes/tools/openclaw_tools.py`)

10 domain-specific tools registered as Hermes native tools:

| Tool | Domain | Actions |
|------|--------|---------|
| `openclaw_market` | A-stock market data | zt (涨停), lb (连板), bk (板块), hot (热股), summary |
| `openclaw_analysis` | Market analysis | Deep question answering |
| `openclaw_strategy` | Strategy evaluation | Trading recommendations |
| `openclaw_backtest` | Backtesting | backtest, ledger, strategy_list, strategy_detail |
| `openclaw_news` | News & search | news, web_search, deep research |
| `openclaw_dev` | Development | Claude Code, SSH, deploy, code review |
| `openclaw_monitor` | Infrastructure | alerts, patrol, host health, metrics |
| `openclaw_pipeline` | Workflow execution | morning_briefing, close_review, health_selfheal |
| `openclaw_system` | System status | status, policy, adaptive tuning, reflection |
| `openclaw_general` | General utilities | Catch-all for misc commands |

Each tool calls `_call_openclaw(command, args)` which:
1. Publishes a message to `openclaw:orchestrator` via Redis
2. Subscribes to `openclaw:{sender}` for the reply
3. Waits up to 180s with timeout
4. Returns the text result to Hermes agent

### 3. Skill Templates (`hermes/tools/openclaw_skills.py`)

Three preset skill templates that teach Hermes common OpenClaw workflows:

- **Morning Briefing**: market summary → limitup stocks → news → synthesis
- **Deep Stock Research**: web search → deep research → analysis → strategy → backtest
- **System Health Check**: agent status → patrol → alerts → adaptive tuning

Skills auto-install to `~/.hermes/skills/openclaw/` on import.

### 4. OpenClaw Side (`openclaw/agents/system_commands.py`)

The `_call_hermes()` method in SystemCommandHandler:
1. Generates a unique `msg_id`
2. Creates a dedicated reply channel: `openclaw:orchestrator:replies:{msg_id}`
3. Publishes task to `openclaw:hermes`
4. Waits up to 300s for Hermes response
5. Returns result text to user

---

## Deployment

### Prerequisites

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.12+ | Runtime |
| Redis | 7.0+ | Message broker (Pub/Sub) |
| Ollama | Latest | Local embeddings (BGE-M3) |
| Hermes Agent | Latest | AI agent framework ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)) |
| OpenClaw | Latest | Multi-agent system ([openclaw/openclaw](https://github.com/openclaw/openclaw)) |

### Step 1: Install Dependencies

```bash
# Clone this repo
git clone https://github.com/pagliazi/hermes-openclaw-bridge.git
cd hermes-openclaw-bridge

# Clone Hermes Agent (if not already installed)
git clone https://github.com/NousResearch/hermes-agent.git ~/hermes-agent
cd ~/hermes-agent
pip install -e .

# Verify Redis is running
redis-cli ping  # Should return PONG
```

### Step 2: Configure Hermes Agent

```bash
# Create Hermes config directory
mkdir -p ~/.hermes

# Configure model provider
cat > ~/.hermes/config.yaml << 'EOF'
model:
  default: qwen3.5-plus          # Or any model you prefer
  provider: custom                # custom / openai / anthropic
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1  # Your provider URL

tools:
  enabled_toolsets:
    - core                        # Hermes built-in tools
    - openclaw                    # OpenClaw domain tools
EOF

# Set API keys
cat > ~/.hermes/.env << 'EOF'
DASHSCOPE_API_KEY=sk-your-key-here
REDIS_URL=redis://127.0.0.1:6379/0
OPENCLAW_TIMEOUT=180
EOF
```

### Step 3: Deploy Bridge Files

```bash
# Copy OpenClaw domain tools to Hermes
cp hermes/tools/openclaw_tools.py ~/hermes-agent/tools/
cp hermes/tools/openclaw_skills.py ~/hermes-agent/tools/

# Register openclaw toolset in Hermes (add to toolsets.py if not present)
# The tools auto-register via registry.py on import

# Copy bridge server
cp hermes/openclaw_bridge_server.py ~/hermes-agent/

# Copy OpenClaw integration handler (if setting up OpenClaw fresh)
# The _call_hermes() method in system_commands.py handles OpenClaw → Hermes calls
```

### Step 4: Start Services

```bash
# Terminal 1: Start Redis (if not running as service)
redis-server

# Terminal 2: Start OpenClaw Orchestrator
cd /path/to/openclaw
source .venv/bin/activate
python -m agents.orchestrator

# Terminal 3: Start Hermes Bridge Server
cd ~/hermes-agent
source .venv/bin/activate
python openclaw_bridge_server.py
```

### Step 5: Verify Integration

```bash
# Check bridge server heartbeat
redis-cli HGET openclaw:heartbeats hermes
# Should return JSON with ts, pid, type

# Test OpenClaw → Hermes (via webchat or API)
curl -X POST http://localhost:7789/api/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message": "/hermes 你好，测试桥接是否正常"}'

# Test Hermes → OpenClaw (via Hermes CLI)
cd ~/hermes-agent
python -c "
from tools.openclaw_tools import _sync_call
print(_sync_call('status'))
"
```

### Production Deployment (launchd)

For macOS, create a LaunchAgent to keep the bridge server running:

```bash
cat > ~/Library/LaunchAgents/com.openclaw.hermes-bridge.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.openclaw.hermes-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/clawagent/hermes-agent/.venv/bin/python3</string>
    <string>/Users/clawagent/hermes-agent/openclaw_bridge_server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/clawagent/hermes-agent</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>REDIS_URL</key>
    <string>redis://127.0.0.1:6379/0</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/clawagent/openclaw/logs/hermes-bridge.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/clawagent/openclaw/logs/hermes-bridge.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.openclaw.hermes-bridge.plist
```

### Systemd (Linux)

```bash
sudo cat > /etc/systemd/system/hermes-bridge.service << 'EOF'
[Unit]
Description=Hermes-OpenClaw Bridge Server
After=redis.service

[Service]
Type=simple
User=clawagent
WorkingDirectory=/home/clawagent/hermes-agent
Environment=REDIS_URL=redis://127.0.0.1:6379/0
ExecStart=/home/clawagent/hermes-agent/.venv/bin/python3 openclaw_bridge_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now hermes-bridge
```

---

## Redis Protocol

### Message Format

All messages follow the same envelope:

```json
{
  "id": "a1b2c3d4e5f6",
  "sender": "orchestrator",
  "target": "hermes",
  "action": "hermes_task",
  "params": {
    "prompt": "搜索最新 AI 芯片新闻",
    "max_iterations": 30
  },
  "reply_to": "",
  "reply_channel": "openclaw:orchestrator:replies:a1b2c3d4e5f6",
  "timestamp": 1712649600.0,
  "result": null,
  "error": ""
}
```

### Channels

| Channel | Direction | Purpose |
|---------|-----------|---------|
| `openclaw:hermes` | OpenClaw → Hermes | Task delegation |
| `openclaw:orchestrator` | Hermes → OpenClaw | Domain tool calls |
| `openclaw:orchestrator:replies:{id}` | Hermes → OpenClaw | Dedicated reply (avoids deadlock) |
| `openclaw:{sender}` | OpenClaw → Hermes | Tool call replies |
| `openclaw:heartbeats` | Hermes → Redis HASH | Liveness monitoring |

### Heartbeat

The bridge server publishes to `openclaw:heartbeats` every 10 seconds:

```json
// HGET openclaw:heartbeats hermes
{
  "ts": 1712649600.0,
  "pid": 12345,
  "type": "hermes-agent"
}
```

---

## API Reference

### OpenClaw → Hermes

**Command**: `/hermes <prompt>`

Invokes the Hermes agent with full tool access. The agent can use both Hermes core tools (terminal, file, web search, code execution) and OpenClaw domain tools.

**Implementation**: `openclaw/agents/system_commands.py` → `_call_hermes()`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| prompt | str | required | Task description for Hermes |
| max_iterations | int | 30 | Max tool-calling iterations |
| timeout | int | 300s | Reply wait timeout |

### Hermes → OpenClaw Domain Tools

Each tool follows the Hermes function-calling schema:

```python
# Example: Get today's limitup stocks
openclaw_market(action="zt")

# Example: Deep analysis question
openclaw_analysis(question="分析今天新能源板块为什么集体涨停")

# Example: Run a backtest
openclaw_backtest(action="backtest", args="momentum_strategy period=30d")

# Example: System health
openclaw_system(action="status")
```

| Parameter | Type | Description |
|-----------|------|-------------|
| REDIS_URL | env | Redis connection URL (default: `redis://127.0.0.1:6379/0`) |
| OPENCLAW_TIMEOUT | env | Seconds to wait for OpenClaw reply (default: 180) |

---

## Customization

### Adding New Domain Tools

Edit `hermes/tools/openclaw_tools.py`:

```python
# Add to DOMAIN_TOOLS list:
{
    "name": "openclaw_your_domain",
    "toolset": "openclaw",
    "description": "Description for the LLM to understand when to use this tool",
    "emoji": "\U0001f4a1",
    "params": {
        "action": {"type": "string", "description": "Action name"},
        "args": {"type": "string", "description": "Arguments", "default": ""},
    },
    "required": ["action"],
},
```

The tool is automatically registered with Hermes via the registry system.

### Adding New Skills

Edit `hermes/tools/openclaw_skills.py`:

```python
# Add to PRESET_SKILLS list:
{
    "name": "your-skill-name",
    "category": "openclaw",
    "content": '''---
name: Your Skill Name
description: What this skill does
trigger: "keyword1|keyword2"
tools: [openclaw_market, openclaw_analysis]
---

# Your Skill

## Steps
1. Call `openclaw_market` with action="summary"
2. Call `openclaw_analysis` with question="..."
''',
},
```

### Changing the LLM Model

Edit `~/.hermes/config.yaml`:

```yaml
model:
  default: deepseek-chat           # DeepSeek
  # default: gpt-4o               # OpenAI
  # default: claude-sonnet-4-20250514          # Anthropic
  # default: qwen3-max            # Alibaba Qwen
  provider: custom
  base_url: https://api.deepseek.com/v1
```

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Bridge server not starting | Missing dependencies | `pip install redis python-dotenv` in hermes-agent venv |
| `_call_openclaw` timeout | OpenClaw orchestrator not running | Start orchestrator: `python -m agents.orchestrator` |
| `_call_hermes` timeout (300s) | Bridge server not running | Start bridge: `python openclaw_bridge_server.py` |
| Heartbeat missing | Bridge crashed | Check logs, restart bridge server |
| Tool not found | openclaw_tools.py not in tools/ | Copy to `~/hermes-agent/tools/` |
| Redis connection refused | Redis not running | `redis-server` or `brew services start redis` |

### Debug Commands

```bash
# Monitor all Redis messages in real-time
redis-cli SUBSCRIBE openclaw:hermes openclaw:orchestrator

# Check bridge heartbeat
redis-cli HGET openclaw:heartbeats hermes

# Check all heartbeats
redis-cli HGETALL openclaw:heartbeats

# Test Redis connectivity
redis-cli PING
```

---

## Project Structure

```
hermes-openclaw-bridge/
├── README.md                           # This file
├── LICENSE                             # MIT License
├── config.yaml                         # OpenClaw base configuration
│
├── hermes/                             # Hermes-side integration
│   ├── __init__.py                     # Bridge architecture docstring
│   ├── openclaw_bridge_server.py       # Redis bridge server (155 lines)
│   │                                   #   Listens on openclaw:hermes
│   │                                   #   Spawns AIAgent per task
│   │                                   #   Heartbeat every 10s
│   └── tools/
│       ├── __init__.py
│       ├── openclaw_tools.py           # 10 domain tools (286 lines)
│       │                               #   _call_openclaw() Redis RPC
│       │                               #   Auto-registers with Hermes
│       ├── openclaw_skills.py          # 3 preset skills (110 lines)
│       │                               #   Auto-installs to ~/.hermes/skills/
│       └── registry.py                 # Tool registration system (335 lines)
│
└── openclaw/                           # OpenClaw-side integration
    ├── __init__.py
    └── agents/
        ├── __init__.py
        ├── app_config.py               # 3-tier config loader
        ├── base.py                     # BaseAgent class (Redis messaging)
        ├── bridge_client.py            # HMAC-authenticated bridge client
        ├── orchestrator.py             # Core orchestrator (L0/L1/L2 routing)
        └── system_commands.py          # _call_hermes() implementation
                                        #   30+ system commands
                                        #   Hermes delegation handler
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

**Part of the [RRClaw](https://github.com/pagliazi/rrclaw) ecosystem**

Built with [Hermes Agent](https://github.com/NousResearch/hermes-agent) + [OpenClaw](https://github.com/openclaw/openclaw) + Redis Pub/Sub

</div>

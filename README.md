<div align="center">

# RRCLAW

**A股量化智能体框架 / A-Share Quant Trading Agent**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Redis](https://img.shields.io/badge/Redis-7+-red.svg)](https://redis.io)
[![LLM](https://img.shields.io/badge/LLM-Claude%20%7C%20Qwen-purple.svg)](https://anthropic.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

*RRCLAW 是一套 A 股量化交易框架。用大模型跑行情分析、策略回测、因子挖掘、条件选股，对接 [ReachRich](https://rr.zayl.net) 数据平台，覆盖沪深京 5000+ 标的。*

[功能](#features) | [快速开始](#quick-start) | [架构](#architecture) | [用法](#usage) | [API 接口](#reachrich-api-integration) | [部署](#deployment)

</div>

> **[中文文档](README.zh-CN.md)** | English

---

## Features

- **实时行情** — 全市场报价、涨跌停板、板块轮动、异动监控，盘中秒级更新
- **策略回测** — backtrader / vectorbt 双引擎，支持 PBO 交叉验证
- **因子挖掘** — core_engine 自动扫描 Alpha 因子，滚动窗口验证
- **条件选股** — 200+ 因子的 DSL 组合筛选（技术面 / 情绪面 / 基本面）
- **多通道** — Telegram、飞书、WebChat、REST API，同一套逻辑
- **API Key 认证** — `rk_` Bearer token，给外部服务调数据用

---

## What Is RRCLAW

RRCLAW 跑的是 LLM 推理主循环 —— 接收用户指令，调工具拿数据，跑回测，返回结果。不是消息转发器，是决策层。

### Key Capabilities

| Domain | Features |
|--------|----------|
| **Market Analysis** | Limitup board, consecutive limits, sector rotation, hot stocks, market sentiment, K-line indicators |
| **Quantitative** | Strategy backtesting, factor mining (PBO cross-validation), Alpha signals, multi-condition screener |
| **Development** | Code generation, review, refactoring, deployment, Git operations via Claude Code |
| **Self-Learning** | Background review, pattern detection, automatic skill creation, GEPA prompt evolution |
| **Fault Tolerance** | 7-layer resilience: retry → circuit breaker → recovery recipes → provider fallback → death spiral prevention |
| **Context Engineering** | 5-layer compression: tool result budget → history snip → microcompact → context collapse → autocompact |

### Architectural Lineage

| Source | What RRCLAW Takes |
|--------|-------------------|
| **claude-code** `query.ts` | Async generator LLM loop, 5-layer context compression, ToolSearch lazy loading |
| **claw-code** `conversation.rs` | `ConversationRuntime<C, T>` generic pattern (Protocol injection), Worker Boot state machine, Recovery Recipes |
| **hermes-agent** `run_agent.py` | Background Review daemon thread, PTC iteration budget with refund, Credential Pool (4 strategies), Error Classification |
| **autoresearch** | Keep/discard experiment loop for strategy optimization, git-as-experiment-tracker |

---

## Architecture

```
┌─ OpenClaw Gateway ──────────────────────────────────────┐
│  Role: Pure channel layer                                │
│  Telegram · WebChat · Feishu · Slack · API               │
│  Does NOT control LLM loop                               │
└──────────────┬──────────────────────────────────────────┘
               │ WebSocket / ACP
┌──────────────▼──────────────────────────────────────────┐
│  RRCLAW Harness (Python, 10,800 lines)                   │
│                                                          │
│  ConversationRuntime ─── async generator LLM loop        │
│  ├── ContextEngine ──── 5-layer compression              │
│  ├── ToolExecutor ───── concurrent/serial partitioning   │
│  ├── ToolSearch ─────── tier 0/1/2 lazy loading          │
│  ├── ErrorClassifier ── structured recovery decisions    │
│  ├── CircuitBreaker ─── failure storm prevention         │
│  ├── RecoveryEngine ─── 7 scenario recovery recipes      │
│  ├── HealthMonitor ──── component health + degradation   │
│  ├── BackgroundReview ── session-level self-learning     │
│  ├── EvolutionEngine ── cross-session pattern learning   │
│  └── ProviderRouter ─── Anthropic → DashScope → Ollama  │
│                                                          │
│  Tool Tiers:                                             │
│  ├── NATIVE (zero overhead): PyAgent 71 cmd, Hermes 47  │
│  ├── SKILL (on demand): bundled + auto-generated         │
│  └── MCP (external): ClawHub, third-party, ReachRich     │
└──────────────────────────────────────────────────────────┘
```

### Data Flow (Telegram Example)

```
User @Telegram: "今天涨停板有哪些半导体？"
  → OpenClaw Gateway → WebSocket → RRCLAW GatewayChannel
  → ConversationRuntime.run_turn()
    → ContextEngine.prepare() → 5-layer compression
    → LLM stream → tool_use: market_query(type="limitup")
    → ToolExecutor → Redis Pub/Sub → PyAgent market agent
    → tool_result → LLM stream → "今天有3只半导体涨停..."
  → GatewayChannel.send_stream_delta() → Telegram
```

### Channel Architecture

| Channel | Protocol | Direction | Use Case |
|---------|----------|-----------|----------|
| **GatewayChannel** | WebSocket client | RRCLAW → Gateway | Telegram, WebChat, Feishu via OpenClaw |
| **ACPRuntime** | WebSocket server (:7790) | Gateway → RRCLAW | Full LLM loop takeover (ACP protocol) |
| **MCP Server** | JSON-RPC stdio | External → RRCLAW | Claude Desktop, Cursor call RRCLAW tools |
| **ReachRich MCP** | JSON-RPC stdio | External → ReachRich | Fast A-share market data queries |
| **Webhook** | HTTP POST | Bidirectional | Evolution notifications, health alerts |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Redis 7+
- OpenClaw Gateway (for IM channels)

### Install

```bash
git clone https://github.com/pagliazi/rrclaw.git
cd rrclaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Configure

```bash
cp config.example.yaml rrclaw.yaml
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# LLM Provider (pick one or more)
ANTHROPIC_API_KEY=sk-ant-...
# or DASHSCOPE_API_KEY=sk-...

# ReachRich Market Data API
REACHRICH_URL=https://rr.zayl.net/api        # Public API
REACHRICH_TOKEN=rk_your_api_key_here          # Get from ReachRich Settings → API Key

# Redis
REDIS_URL=redis://127.0.0.1:6379/0
```

### Run

```bash
# Start Redis
redis-server &

# Start RRCLAW harness
python -m rrclaw --config rrclaw.yaml

# Or start individual MCP servers
rrclaw-mcp --backend pyagent     # Expose PyAgent tools via MCP
rrclaw-market                     # Expose ReachRich market data via MCP
```

### Verify

```bash
# Check RRCLAW is running
redis-cli PING

# Monitor tool executions
redis-cli SUBSCRIBE "harness:executions"

# Test ReachRich API connection
curl -H "Authorization: Bearer $REACHRICH_TOKEN" https://rr.zayl.net/api/bridge/snapshot/
```

---

## Usage

### Natural Language Quantitative Analysis

RRCLAW understands natural language commands for market analysis:

```
User: "今天涨停板有哪些半导体？"
RRCLAW → calls market_query(type="limitup")
       → filters by sector="半导体"
       → "今天有3只半导体涨停: ..."

User: "帮我回测一个突破20日均线的策略"
RRCLAW → generates strategy code
       → calls backtest/run (vectorbt engine)
       → returns PnL curve, Sharpe ratio, max drawdown

User: "用pct_chg > 5 AND volume_ratio > 3筛选股票"
RRCLAW → calls DSL screener
       → returns matching stocks with fundamentals
```

### Programmatic API Access

Use your API Key to call ReachRich data directly:

```python
import httpx

headers = {"Authorization": "Bearer rk_your_api_key"}
base = "https://rr.zayl.net/api"

# Real-time market snapshot (5000+ stocks)
r = httpx.get(f"{base}/fast/realtime/", headers=headers)

# Limit-up board analysis
r = httpx.get(f"{base}/bridge/limitup/", headers=headers)

# DSL stock screening
r = httpx.post(f"{base}/bridge/screener/", headers=headers, json={
    "payload": {"rules": [{"field": "pct_chg", "op": ">", "value": 5}]},
    "limit": 20
})

# Strategy backtesting
r = httpx.post(f"{base}/bridge/backtest/run/", headers=headers, json={
    "strategy_code": "your_backtrader_code",
    "stock": "000001.SZ",
    "start_date": "2025-01-01",
    "end_date": "2026-01-01"
})
```

---

## ReachRich API Integration

RRCLAW connects to [ReachRich](https://rr.zayl.net) for A-share market data via authenticated API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/bridge/snapshot/` | GET | Full market snapshot (5000+ stocks) |
| `/bridge/limitup/` | GET | Limit-up/down board with sector analysis |
| `/bridge/dragon-tiger/` | GET | Dragon-tiger list (institutional flow) |
| `/bridge/concepts/` | GET | Sector/concept board rankings |
| `/bridge/sentiment/` | GET | Market sentiment & news digest |
| `/bridge/kline/` | GET | K-line data (daily/weekly/monthly) |
| `/bridge/indicators/` | GET | Technical indicators (MA/MACD/RSI/BOLL) |
| `/bridge/presets/` | GET | 200+ pre-built screening strategies |
| `/bridge/screener/` | POST | DSL-based multi-factor stock screening |
| `/bridge/backtest/run/` | POST | Strategy backtesting (backtrader/vectorbt) |
| `/bridge/backtest/run_alpha/` | POST | Alpha factor backtesting |
| `/bridge/backtest/run_mining/` | POST | Automated factor mining |
| `/bridge/ledger/` | GET | AI strategy decision ledger |
| `/bridge/llm/config/` | GET | LLM model routing configuration |
| `/fast/realtime/` | GET | Real-time quotes (sub-second updates) |
| `/sse/realtime/` | GET | Server-Sent Events live stream |

**Authentication**: `Authorization: Bearer rk_your_api_key` — generate your key at ReachRich Settings → API Key.

---

## Module Reference

### Core Runtime (`rrclaw/runtime/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `conversation.py` | 341 | **ConversationRuntime** — async generator LLM loop. Handles streaming, tool dispatch, budget management, error recovery. Yields `TurnEvent` (text_delta, tool_start, tool_result, warning, error, turn_complete). |
| `session.py` | 200 | **Session** — JSONL append-only persistence with 256KB auto-rotation and gzip archival. Crash-safe recovery via `Session.restore()`. |
| `server.py` | 354 | **RRClawServer** — main entry point. Initializes all components, connects Gateway, manages per-session runtimes. |
| `config.py` | 136 | **RRClawConfig** — 3-tier config merge: YAML file → environment variables → defaults. |
| `prompt.py` | 103 | **PromptBuilder** — constructs system prompt with SOUL.md + Tier 1 tool index injection (~7.5K tokens vs ~50K full injection). |
| `hooks.py` | 200 | **HookRegistry** — PreToolUse / PostToolUse lifecycle hooks with built-in logging and metrics. |

### Resilience (`rrclaw/runtime/resilience/`)

7-layer fault tolerance system:

| Layer | Module | What It Does |
|-------|--------|-------------|
| L1 | `api_retry.py` | Exponential backoff (500ms base, 32s cap, 25% jitter). 529 × 3 triggers model fallback. |
| L2 | `error_classifier.py` | Classifies errors into `FailoverReason` enum → returns `ClassifiedError` with recovery hints (`should_compress`, `should_rotate_credential`, `should_fallback`). |
| L3 | `circuit_breaker.py` | Generic circuit breaker. 3 consecutive failures → trip. Optional cooldown for auto-reset. Applied to autocompact, per-tool execution, evolution engine. |
| L4 | `recovery_recipes.py` | Structured recipes for 7 failure scenarios: Redis lost, Gateway disconnected, PyAgent timeout, Hermes crash, model overloaded, tool degraded, memory corruption. |
| L5 | `health_monitor.py` | Periodic health checks (Redis PING, agent heartbeat, WS ping/pong). States: healthy → degraded → down. Degraded tools get warning annotations for LLM. |
| L6 | `api_retry.py` | Death spiral prevention: API errors set `_skip_hooks = True` to break error → hook → error loops. |
| L7 | `health_monitor.py` | Degradation routing: down backends → fallback route or UNAVAILABLE. |

### Providers (`rrclaw/runtime/providers/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `router.py` | 135 | **ProviderRouter** — prefix-based routing (`anthropic/` → Anthropic, `dashscope/` → DashScope, `ollama/` → local). Automatic fallback chain on consecutive failures. |
| `anthropic.py` | 134 | Anthropic Claude provider with streaming and prompt caching. |
| `dashscope.py` | 178 | Alibaba DashScope (Qwen) provider via OpenAI-compatible API. |
| `openai_compat.py` | 28 | Generic OpenAI-compatible provider (Ollama, vLLM, etc.). |
| `credential_pool.py` | 116 | **CredentialPool** — 4 rotation strategies: `fill_first`, `round_robin`, `random`, `least_used`. 1-hour cooldown on 429/402. |

### Tools (`rrclaw/tools/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `base.py` | 96 | `Tool` ABC + `ToolSpec` + `ToolResult` + `ToolUse` dataclasses. |
| `registry.py` | 163 | **GlobalToolRegistry** — manages Tier 0/1/2 tools. `search()` for keyword matching. |
| `executor.py` | 136 | **ToolExecutor** — concurrent/serial partitioning based on `is_concurrent_safe`. Errors returned as tool_result (never breaks loop). Timeout enforcement. |
| `search.py` | 127 | **ToolSearchTool** — Tier 0 meta-tool. LLM calls `tool_search("回测")` → returns matching tool schemas. 3-layer scoring: keyword exact (3.0) → description substring (1.5) → category match (1.0). |

#### Built-in Tools (Tier 0)

| Tool | File | Purpose |
|------|------|---------|
| `bash` | `builtin/bash.py` | Shell command execution with timeout and working directory control. |
| `read_file` | `builtin/file_ops.py` | Read files with offset/limit support. |
| `write_file` | `builtin/file_ops.py` | Write/create files. |
| `edit_file` | `builtin/file_ops.py` | Exact string replacement in files. |
| `market_query` | `builtin/market_query.py` | Unified A-share query entry point (limitup, concepts, hot, summary, kline, indicators). Routes to PyAgent via Redis. |
| `canvas` | `builtin/canvas.py` | ECharts HTML generation for heatmap, line, bar, table, sankey, dashboard. Sends to Gateway Canvas. |
| `tool_search` | `search.py` | Discover Tier 1 tools by keyword search. |
| `memory` | `context/memory/tier2_user.py` | Read/write persistent user preferences (USER.md). |

#### PyAgent Integration (`tools/pyagent/`)

`PyAgentBridge` connects to 12 Python agents via Redis Pub/Sub, exposing 71 commands as Tier 1 tools. Agents include: market, backtest, dev, news, monitor, calendar, email, deploy, etc.

#### Hermes Integration (`tools/hermes/`)

`HermesNativeRuntime` wraps the synchronous Hermes `AIAgent` in a `ThreadPoolExecutor`. Exposes 47 tools + PTC (Programmatic Tool Calling) as Tier 1 tools.

#### MCP (`tools/mcp/`)

| Module | Purpose |
|--------|---------|
| `server.py` | RRCLAW as MCP **server** — exposes PyAgent/Hermes tools to Claude Desktop, Cursor, etc. |
| `client.py` | RRCLAW as MCP **client** — connects to external MCP servers (ClawHub, third-party). |
| `reachrich_server.py` | Dedicated MCP server for fast A-share market queries (limitup, concepts, kline, indicators, sentiment). Long-running operations (backtest, factor mining) excluded. |

#### ReachRich Data Sources (`data_sources/`)

ReachRich market data is accessed through **three layers**, each suited to different operation characteristics:

| Layer | Operations | Protocol | Auth | Latency |
|-------|-----------|----------|------|---------|
| **MCP Server** | Limitup, concepts, kline, indicators, sentiment, hot, summary | JSON-RPC stdio (MCP) | HMAC-SHA256 (BridgeClient) | < 10s |
| **Native HTTP** | Backtest (≤300s), factor mining (≤620s), Alpha signals | Direct BridgeClient HTTP | HMAC-SHA256 + nonce | ≤ 620s |
| **Redis Stream** | DolphinDB intraday scan (3-6s updates) | XREADGROUP consumer | Token-based HMAC per message | Real-time |

**Authentication model:**

- **BridgeClient HTTP** — Every request is HMAC-SHA256 signed with `REACHRICH_SECRET`. Headers: `X-Bridge-Timestamp` + `X-Bridge-Key`. Response integrity verified via `X-Data-Hash` (SHA256).
- **Redis Stream** — Two-layer auth: (1) Redis connection AUTH via password in `REDIS_URL`, (2) each stream message carries `_sig` field — HMAC-SHA256 of payload signed with `REACHRICH_TOKEN`. Registered ReachRich users receive their personal token from the platform.

| Module | Purpose |
|--------|---------|
| `reachrich_stream.py` | Consumer for real-time Redis Stream data. Verifies HMAC signatures, rejects stale messages (>30s), supports consumer groups for horizontal scaling. |
| `reachrich_publish.py` | Publisher utility for PyAgent/DolphinDB side. Signs each message with user token before XADD. |

### Context Engineering (`rrclaw/context/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `engine.py` | 246 | **ContextEngine** — orchestrates 5-layer compression before every LLM call. Implements `ContextProvider` protocol for `ConversationRuntime`. |

**5 Compression Layers:**

| Layer | Name | Method | Cost |
|-------|------|--------|------|
| L1 | Tool Result Budget | Truncate oversized tool results (>50K chars) with preview | Free |
| L2 | History Snip | Keep first 2 + last 16 messages, snip middle | Free |
| L3 | Microcompact | Rule-based folding of old tool results in first half | Free |
| L4 | Context Collapse | Summary blocks for old messages (placeholder for LLM summarizer) | Free |
| L5 | Autocompact | Full LLM-powered conversation summary. Circuit breaker protected (3 failures → skip). | 1 LLM call |

#### 3-Tier Memory (`context/memory/`)

| Tier | Scope | Storage | TTL |
|------|-------|---------|-----|
| `tier1_session.py` | Current session | In-memory LRU (max 100) | Session lifetime |
| `tier2_user.py` | Per-user persistent | USER.md file | Permanent |
| `tier3_system.py` | System-wide | JSON files in `~/.rrclaw/memory/` | Confidence decay (1.4%/day), prune at 0.3 |

### Self-Learning (`rrclaw/evolution/`)

4-layer closed loop from instant correction to daily system evolution:

| Loop | Timescale | Module | Mechanism |
|------|-----------|--------|-----------|
| **Loop 1** | Seconds | `executor.py` | Tool error → error as tool_result → LLM self-corrects (max 3 retries) |
| **Loop 2** | Minutes | `background_review.py` | Counter-driven (10 turns / 10 iterations). Forks Hermes daemon thread (`max_iterations=8`). Creates Memory entries and Skills from conversation patterns. |
| **Loop 3** | Hours | `engine.py` | **EvolutionEngine** — background asyncio task consuming Redis Stream `harness:executions`. Runs `PatternDetector` (repeated tool chains → Skill) and `FailureDetector` (repeated failures → Recovery Recipe). 5-minute check interval. |
| **Loop 4** | Days | `gepa_pipeline.py` | **GEPA Pipeline** — collects 24h execution traces, identifies failures, LLM-optimizes SOUL.md, A/B tests on historical cases, deploys if >5% improvement. |

| Module | Lines | Purpose |
|--------|-------|---------|
| `correction_tracker.py` | 206 | Records tool errors and corrections. Extracts `CorrectionPattern` (grouped by normalized error, with success rate). |
| `pattern_detector.py` | 223 | N-gram detection on per-session tool sequences. Returns `ToolChainPattern` with occurrence count and common params. |
| `failure_detector.py` | 218 | Groups failures by tool + normalized error. Time correlation (market open/close patterns). Cascading failure detection (60s window). Maps to `FailureScenario`. |
| `background_review.py` | 316 | `MEMORY_NUDGE_INTERVAL=10` turns, `SKILL_NUDGE_INTERVAL=10` iterations. Three review prompts (memory, skill, correction). Spawns daemon threads. |
| `skill_guard.py` | 334 | Security scanner for auto-generated skills. 5 scan categories (exfiltration, injection, destructive, persistence, obfuscation). Trust matrix: `bundled` → allow all; `agent-created` → ask user on dangerous; `hub-installed` → deny dangerous. |
| `skill_creator.py` | 274 | Creates skills from `ToolChainPattern` or `FailurePattern`. Two modes: LLM-based (via Hermes) or template-based. All skills scanned by SkillGuard before writing. |
| `autoresearch_loop.py` | 316 | Karpathy-pattern keep/discard experiments: modify strategy → backtest → if sharpe improves → git commit, else → git checkout. Results saved to TSV. |
| `perf_detector.py` | 118 | Detects latency increases and success rate drops >30% vs 3-day rolling baseline. |

### Skills (`rrclaw/skills/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `loader.py` | 198 | Loads YAML frontmatter + Markdown body skill files from bundled, user (`~/.rrclaw/skills/`), and workspace (`~/.openclaw/workspace/skills/`) directories. |
| `executor.py` | 205 | Prepares skills for execution by injecting instructions into conversation. Tracks active skills per session. 60% keyword match for auto-trigger. |
| `sync.py` | 138 | Bidirectional sync between RRCLAW, OpenClaw, and Hermes skill directories. Newer file wins on conflict. |

### Workers (`rrclaw/workers/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `boot.py` | 246 | **Worker Boot** state machine: INIT → DISCOVERING → VALIDATING → READY → RUNNING → SHUTDOWN. Concrete workers: RedisWorker, PyAgentWorker, HermesWorker, GatewayWorker. |
| `coordinator.py` | 212 | **WorkerCoordinator** — concurrent boot with 30s timeout. Required workers must reach READY; optional failures → degraded state. Health monitoring loop every 30s with auto-restart. |
| `task_packet.py` | 202 | **TaskPacket** — priority (CRITICAL/HIGH/NORMAL/LOW), acceptance tests, dependencies. **TaskQueue** — priority ordering with dependency satisfaction check. |

### Channels (`rrclaw/channels/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `gateway.py` | 221 | **GatewayChannel** — WebSocket client connecting to OpenClaw Gateway. Registers capabilities, receives `user.message` / `agent.delegate` frames, streams responses back (`agent.stream`, `agent.stream.end`). Auto-reconnect with exponential backoff. |
| `acp_runtime.py` | 209 | **ACPRuntime** — WebSocket server (:7790) implementing ACP protocol. Gateway connects as client. Receives `{type: "message"}` → runs `ConversationRuntime` → streams `{type: "delta"/"tool_use"/"tool_result"/"done"}` back. |
| `webhook.py` | 120 | **WebhookHandler** — incoming/outgoing HTTP webhooks for evolution notifications and health alerts. |

### Permissions (`rrclaw/permissions/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `policy.py` | 117 | 4-tier permission model: `SAFE` (auto-allow), `AWARE` (log), `CONSENT` (ask user), `CRITICAL` (deny by default). Tool → tier mapping. |
| `enforcer.py` | 98 | Workspace boundary enforcement. Bash command read-only detection. |

### Commands (`rrclaw/commands/`)

| Command | Module | Subcommands |
|---------|--------|-------------|
| `/research` | `research.py` | `start <strategy>` — launch autoresearch experiment loop; `stop` — halt; `status` — show results |
| `/evolve` | `evolve.py` | `status` — show stats; `run` — trigger evolution check; `gepa` — run GEPA pipeline; `skills` — list auto-generated skills; `prune` — remove low-confidence skills |

---

## Configuration

### YAML (`rrclaw.yaml`)

```yaml
gateway:
  url: "ws://127.0.0.1:18789"
  auth_token: ""
  agent_id: "rrclaw"

providers:
  primary: "anthropic/claude-sonnet-4-6"
  fallback_chain:
    - "dashscope/qwen3.5-plus"
    - "ollama/qwen2.5-coder:14b"

redis:
  url: "redis://127.0.0.1:6379/0"

hermes:
  agent_path: "/opt/hermes-agent"
  iteration_budget: 90

context:
  max_tokens: 200000
  autocompact_threshold: 0.8
  tool_result_max_chars: 50000

session:
  dir: "~/.rrclaw/sessions"
  rotation_size: 262144

resilience:
  health_check_interval: 10
```

### Environment Overrides

```bash
ANTHROPIC_API_KEY=sk-...         # Anthropic API key
REDIS_URL=redis://host:6379/0    # Redis connection
GATEWAY_URL=ws://host:18789      # OpenClaw Gateway
HERMES_AGENT_PATH=/path/to/hermes
```

---

## Deployment

### Docker Compose

```bash
cd deploy/
docker compose up -d
```

Starts: Redis, OpenClaw Gateway, RRCLAW Harness.

### macOS (launchd)

```bash
sudo cp -r . /opt/rrclaw
cd /opt/rrclaw && python -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml rrclaw.yaml
cp deploy/com.hermes-bridge.plist ~/Library/LaunchAgents/com.rrclaw.plist
launchctl load ~/Library/LaunchAgents/com.rrclaw.plist
```

### Linux (systemd)

```bash
sudo cp -r . /opt/rrclaw
cd /opt/rrclaw && python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml rrclaw.yaml
sudo cp deploy/hermes-bridge.service /etc/systemd/system/rrclaw.service
sudo systemctl enable --now rrclaw
```

### OpenClaw Integration

**Option A: MCP Server** — RRCLAW tools appear in OpenClaw's tool catalog:
```json5
// ~/.openclaw/openclaw.json
{
  "mcp": {
    "servers": {
      "rrclaw": {
        "command": "/opt/rrclaw/.venv/bin/python",
        "args": ["-m", "rrclaw.tools.mcp.server", "--backend", "pyagent"]
      }
    }
  }
}
```

**Option B: ACP Runtime** — RRCLAW fully takes over the LLM loop:
```json5
// ~/.openclaw/openclaw.json
{
  "agents": {
    "list": [{
      "id": "rrclaw",
      "runtime": { "type": "acp", "url": "ws://127.0.0.1:7790" }
    }]
  }
}
```

---

## Testing

### Run All Tests

```bash
# Unit + functional tests
.venv/bin/python -m pytest tests/

# Channel integration tests (IM + Web)
.venv/bin/python tests/test_channels.py
```

### Test Coverage

| Test Area | Tests | Status |
|-----------|-------|--------|
| Core Runtime (ConversationRuntime, Session, Config) | 21 | Pass |
| Context Engine (5-layer compression) | 4 | Pass |
| Resilience (CircuitBreaker, ErrorClassifier, Recovery) | 6 | Pass |
| Tools (Registry, Executor, Search, Builtins) | 8 | Pass |
| Evolution (PatternDetector, FailureDetector, BackgroundReview, SkillGuard) | 5 | Pass |
| Providers (Router, CredentialPool) | 3 | Pass |
| Workers (Boot, Coordinator, TaskQueue) | 3 | Pass |
| Skills (Loader, Executor, Sync) | 4 | Pass |
| Memory (3-Tier) | 3 | Pass |
| Channels (Gateway IM, ACP Web, Concurrent Sessions) | 3 | Pass |
| **Total** | **60** | **All Pass** |

### Channel Tests Detail

```
Test IM: Gateway (Telegram/IM) Channel
  ✓ Registration with capabilities
  ✓ Tool call (market_query) → streaming response
  ✓ Stream deltas + tool status + stream end

Test Web: ACP (WebChat) Channel
  ✓ Ping/pong protocol
  ✓ Tool call → streaming text response
  ✓ Multi-turn conversation (same session)
  ✓ Error handling (empty message, invalid type)

Test Concurrent: Multiple Users
  ✓ 3 simultaneous users with session isolation
```

---

## Project Structure

```
rrclaw/
├── __init__.py                          # Package: v0.1.0
├── __main__.py                          # python -m rrclaw
│
├── runtime/                             # Core LLM loop + infrastructure
│   ├── conversation.py                  # ConversationRuntime (async generator)
│   ├── session.py                       # JSONL persistence + rotation
│   ├── server.py                        # Main server entry point
│   ├── config.py                        # 3-tier config merge
│   ├── prompt.py                        # System prompt builder
│   ├── hooks.py                         # Pre/Post tool hooks
│   ├── providers/                       # LLM providers
│   │   ├── router.py                    # Prefix routing + fallback chain
│   │   ├── anthropic.py                 # Claude provider
│   │   ├── dashscope.py                 # Qwen provider
│   │   ├── openai_compat.py             # OpenAI-compatible (Ollama, vLLM)
│   │   ├── credential_pool.py           # 4-strategy rotation + cooldown
│   │   └── base.py                      # Provider ABC
│   └── resilience/                      # Fault tolerance
│       ├── api_retry.py                 # Exponential backoff
│       ├── error_classifier.py          # FailoverReason → ClassifiedError
│       ├── circuit_breaker.py           # Generic circuit breaker
│       ├── recovery_recipes.py          # 7 scenario RecoveryEngine
│       └── health_monitor.py            # Component health + degradation
│
├── tools/                               # Tool ecosystem
│   ├── base.py                          # Tool ABC + ToolSpec/ToolResult/ToolUse
│   ├── registry.py                      # GlobalToolRegistry (Tier 0/1/2)
│   ├── executor.py                      # Concurrent/serial dispatch
│   ├── search.py                        # ToolSearch (Tier 0 meta-tool)
│   ├── builtin/                         # Tier 0 built-in tools
│   │   ├── bash.py                      # Shell execution
│   │   ├── file_ops.py                  # read/write/edit
│   │   ├── market_query.py              # Unified A-share query
│   │   └── canvas.py                    # ECharts visualization
│   ├── pyagent/                         # PyAgent integration (71 commands)
│   │   └── bridge.py                    # Redis Pub/Sub bridge
│   ├── hermes/                          # Hermes integration (47 tools)
│   │   └── runtime.py                   # ThreadPool wrapper for sync AIAgent
│   └── mcp/                             # Model Context Protocol
│       ├── server.py                    # RRCLAW as MCP server
│       ├── client.py                    # Connect to external MCP servers
│       └── reachrich_server.py          # A-share market data MCP server
│
├── data_sources/                        # Market data ingestion
│   ├── reachrich_stream.py              # Real-time Redis Stream consumer (HMAC-verified)
│   └── reachrich_publish.py             # Signed publisher for PyAgent side
│
├── context/                             # Context engineering
│   ├── engine.py                        # 5-layer compression orchestrator
│   └── memory/                          # 3-tier memory system
│       ├── tier1_session.py             # In-memory LRU (session scope)
│       ├── tier2_user.py                # USER.md persistent preferences
│       └── tier3_system.py              # JSON files with confidence decay
│
├── evolution/                           # Self-learning (4-loop)
│   ├── background_review.py             # Loop 2: daemon thread review
│   ├── engine.py                        # Loop 3: cross-session evolution
│   ├── gepa_pipeline.py                 # Loop 4: daily GEPA optimization
│   ├── autoresearch_loop.py             # Strategy experiment loop
│   ├── pattern_detector.py              # Repeated tool chain detection
│   ├── failure_detector.py              # Failure pattern + time correlation
│   ├── correction_tracker.py            # Error/correction recording
│   ├── skill_creator.py                 # Auto skill generation
│   ├── skill_guard.py                   # Security scan for generated skills
│   └── perf_detector.py                 # Latency/success rate degradation
│
├── skills/                              # Skill management
│   ├── loader.py                        # YAML+Markdown loader (3 directories)
│   ├── executor.py                      # Skill → conversation injection
│   └── sync.py                          # Bidirectional sync (RRCLAW ↔ OpenClaw ↔ Hermes)
│
├── workers/                             # Multi-agent coordination
│   ├── boot.py                          # Worker state machine (6 states)
│   ├── coordinator.py                   # Concurrent boot + health loop
│   └── task_packet.py                   # Priority queue + dependencies
│
├── channels/                            # Gateway integration
│   ├── gateway.py                       # OpenClaw WebSocket client
│   ├── acp_runtime.py                   # ACP WebSocket server (:7790)
│   └── webhook.py                       # HTTP webhooks
│
├── permissions/                         # Access control
│   ├── policy.py                        # 4-tier: safe/aware/consent/critical
│   └── enforcer.py                      # Workspace boundary enforcement
│
└── commands/                            # Slash commands
    ├── research.py                      # /research start|stop|status
    └── evolve.py                        # /evolve status|run|gepa|skills|prune
```

### Code Statistics

| Module | Files | Lines | Share |
|--------|-------|-------|-------|
| Runtime (core + providers + resilience) | 16 | 3,166 | 29% |
| Evolution (self-learning) | 10 | 2,531 | 23% |
| Tools (registry + builtins + integrations) | 15 | 2,114 | 20% |
| Context (compression + memory) | 5 | 863 | 8% |
| Skills | 3 | 541 | 5% |
| Channels | 3 | 530 | 5% |
| Workers | 3 | 450 | 4% |
| Commands | 2 | 272 | 3% |
| Permissions | 2 | 215 | 2% |
| Package init | 2 | 13 | <1% |
| **Total** | **61** | **10,827** | **100%** |

Legacy `bridge/` package (9 files, 1,716 lines) retained for backward compatibility.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

Built on patterns from [Claude Code](https://github.com/anthropics/claude-code) · [claw-code](https://github.com/anthropics/claw-code) · [Hermes Agent](https://github.com/NousResearch/hermes-agent) · [OpenClaw](https://github.com/openclaw/openclaw) · [Autoresearch](https://github.com/karpathy/autoresearch)

</div>

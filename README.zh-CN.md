<div align="center">

# RRCLAW

**A股多智能体量化交易系统**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Redis](https://img.shields.io/badge/Redis-Pub%2FSub-red.svg)](https://redis.io)
[![Anthropic](https://img.shields.io/badge/Anthropic-Claude-purple.svg)](https://anthropic.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

*RRCLAW（ReachRich Claw）是面向**中国A股市场**的生产级多智能体系统。通过 LLM 驱动的智能体协作，实现实时行情分析、策略回测、因子挖掘和自动交易信号生成 —— 并具备从每次决策中自主进化的能力。*

[核心功能](#核心功能) | [一键部署](#一键部署) | [系统架构](#系统架构) | [使用示例](#使用示例) | [API 接口](#reachrich-api-接口) | [常见问题](#常见问题)

</div>

[English](README.md) | **中文**

---

## 项目简介

RRCLAW 是量化交易系统的**大脑** —— 它不是桥接器或插件。RRCLAW 拥有完整的 LLM 推理循环，管理上下文压缩，执行工具调用，处理故障恢复，并从自身错误中持续学习。

系统架构源自多个成熟项目的核心模式：

| 来源 | RRCLAW 继承的能力 |
|------|-------------------|
| **claude-code** `query.ts` | 异步生成器 LLM 循环、5层上下文压缩、ToolSearch 懒加载 |
| **claw-code** `conversation.rs` | `ConversationRuntime<C, T>` 泛型模式（协议注入）、Worker 启动状态机、恢复方案 |
| **hermes-agent** `run_agent.py` | 后台审查守护线程、PTC 迭代预算退还、凭证池（4种策略） |
| **autoresearch** | 保留/丢弃实验循环、git-as-experiment-tracker |

---

## 核心功能

- **实时行情数据** — 5000+ A股标的实时报价、涨跌停板、板块轮动、热门股票、市场情绪雷达
- **策略回测** — backtrader / vectorbt 双引擎沙盒，支持 PBO 交叉验证
- **因子挖掘** — 通过 core_engine 自动发现 Alpha 因子，支持滚动窗口优化
- **DSL 选股** — 200+ 技术面/情绪面/基本面因子，多条件组合筛选
- **自进化系统** — GEPA 流水线（生成 → 评估 → 推广 → 归档），自动优化提示词和策略
- **7层容错** — 重试 → 熔断器 → 恢复方案 → 提供商切换 → 死亡螺旋防护
- **多通道接入** — Telegram、微信、飞书、WebChat、API —— 所有通道共享同一智能内核
- **API Key 认证** — 安全的 `rk_` Bearer Token 认证，用于外部服务集成

---

## 系统架构

```
┌─ OpenClaw 网关 ─────────────────────────────────────────┐
│  角色：纯通道层                                          │
│  Telegram · WebChat · 飞书 · Slack · API                 │
│  不控制 LLM 循环                                         │
└──────────────┬──────────────────────────────────────────┘
               │ WebSocket / ACP
┌──────────────▼──────────────────────────────────────────┐
│  RRCLAW 引擎 (Python, 10,800 行)                         │
│                                                          │
│  ConversationRuntime ─── 异步生成器 LLM 循环              │
│  ├── ContextEngine ──── 5层上下文压缩                     │
│  ├── ToolExecutor ───── 并发/串行任务分区                  │
│  ├── ToolSearch ─────── 0/1/2 级工具懒加载                │
│  ├── ErrorClassifier ── 结构化故障恢复决策                 │
│  ├── CircuitBreaker ─── 故障风暴熔断器                    │
│  ├── RecoveryEngine ─── 7种场景恢复方案                   │
│  ├── HealthMonitor ──── 组件健康度与降级路由               │
│  ├── BackgroundReview ── 会话级自学习                     │
│  ├── EvolutionEngine ── 跨会话模式学习                    │
│  └── ProviderRouter ─── Anthropic → 通义千问 → Ollama    │
│                                                          │
│  工具分层：                                               │
│  ├── 原生层（零开销）: PyAgent 71条命令, Hermes 47个工具    │
│  ├── 技能层（按需加载）: 内置 + 自动生成                    │
│  └── MCP层（外部接入）: ClawHub、第三方、ReachRich          │
└──────────────────────────────────────────────────────────┘
```

### 数据流示例（Telegram）

```
用户 @Telegram: "今天涨停板有哪些半导体？"
  → OpenClaw 网关 → WebSocket → RRCLAW GatewayChannel
  → ConversationRuntime.run_turn()
    → ContextEngine.prepare() → 5层压缩
    → LLM 流式输出 → tool_use: market_query(type="limitup")
    → ToolExecutor → Redis Pub/Sub → PyAgent 行情智能体
    → tool_result → LLM 流式输出 → "今天有3只半导体涨停..."
  → GatewayChannel.send_stream_delta() → Telegram
```

### 通道架构

| 通道 | 协议 | 方向 | 适用场景 |
|------|------|------|----------|
| **GatewayChannel** | WebSocket 客户端 | RRCLAW → 网关 | 通过 OpenClaw 接入 Telegram/微信/飞书 |
| **ACPRuntime** | WebSocket 服务端 (:7790) | 网关 → RRCLAW | 完整 LLM 循环接管（ACP 协议） |
| **MCP Server** | JSON-RPC stdio | 外部 → RRCLAW | Claude Desktop、Cursor 调用 RRCLAW 工具 |
| **ReachRich MCP** | JSON-RPC stdio | 外部 → ReachRich | 快速 A股行情查询 |
| **Webhook** | HTTP POST | 双向 | 进化通知、健康告警 |

---

## 一键部署

最快的启动方式 —— 运行一键部署脚本：

```bash
git clone https://github.com/pagliazi/rrclaw.git
cd rrclaw
./deploy.sh
```

脚本会自动完成以下步骤：
1. 检查 Python 3.11+、Redis、git 等前置依赖
2. 创建 Python 虚拟环境并安装所有依赖
3. 生成配置文件（不会覆盖已有配置）
4. 交互式填写 API Key 和服务地址
5. 验证 Redis 连接和 ReachRich API 连通性

如果你使用 Docker：

```bash
./deploy.sh --with-docker
```

---

## 手动安装

### 前置条件

- Python 3.11+
- Redis 7+（用于智能体间通信）
- OpenClaw Gateway（用于 IM 通道，可选）

### 第一步：克隆仓库

```bash
git clone https://github.com/pagliazi/rrclaw.git
cd rrclaw
```

### 第二步：创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate   # macOS / Linux
# Windows: .venv\Scripts\activate
```

### 第三步：安装依赖

```bash
# 基础安装
pip install -e .

# 开发环境（含测试和代码检查工具）
pip install -e ".[dev]"

# 完整安装（含通义千问等额外提供商）
pip install -e ".[full]"
```

### 第四步：配置

```bash
cp config.example.yaml rrclaw.yaml
cp .env.example .env
```

编辑 `.env` 文件，填入你的凭证（详见下方[配置说明](#配置说明)）。

### 第五步：启动 Redis

```bash
# macOS (Homebrew)
brew services start redis

# Linux (systemd)
sudo systemctl start redis

# 手动启动
redis-server &
```

### 第六步：启动 RRCLAW

```bash
python -m rrclaw --config rrclaw.yaml
```

---

## 配置说明

### 环境变量（`.env`）

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `ANTHROPIC_API_KEY` | 至少填一个 LLM Key | — | Anthropic Claude API 密钥，格式 `sk-ant-...` |
| `DASHSCOPE_API_KEY` | 可选 | — | 阿里云通义千问 API Key，作为备选 LLM 提供商 |
| `OLLAMA_BASE_URL` | 可选 | `http://127.0.0.1:11434` | 本地 Ollama 服务地址 |
| `GATEWAY_URL` | 可选 | `ws://127.0.0.1:18789` | OpenClaw 网关 WebSocket 地址 |
| `GATEWAY_TOKEN` | 可选 | — | 网关认证 Token |
| `REDIS_URL` | 推荐 | `redis://127.0.0.1:6379/0` | Redis 连接地址，支持密码认证 `redis://:密码@主机:端口/库号` |
| `REACHRICH_URL` | 推荐 | — | ReachRich 行情 API 地址，公网: `https://rr.zayl.net/api` |
| `REACHRICH_TOKEN` | 推荐 | — | ReachRich API Key，在设置页获取，格式 `rk_...` |
| `BRIDGE_CLIENT_PATH` | 可选 | — | bridge_client.py 所在目录路径 |
| `HERMES_AGENT_PATH` | 可选 | — | hermes-agent 安装路径 |
| `HERMES_MODEL` | 可选 | `claude-sonnet-4-6` | Hermes 使用的 LLM 模型 |
| `HERMES_PROVIDER` | 可选 | `anthropic` | Hermes 使用的 LLM 提供商 |
| `RRCLAW_PRIMARY_MODEL` | 可选 | — | 覆盖主 LLM 模型，格式 `提供商/模型名` |
| `RRCLAW_SESSION_DIR` | 可选 | `~/.rrclaw/sessions` | 会话存储目录 |

### YAML 配置（`rrclaw.yaml`）

```yaml
# ── OpenClaw 网关 ──
gateway:
  url: "ws://127.0.0.1:18789"      # 网关 WebSocket 地址
  auth_token: ""                     # 网关认证 Token（可选）
  agent_id: "rrclaw"                 # 在网关注册的智能体 ID

# ── LLM 提供商 ──
providers:
  # 主模型（前缀决定提供商）
  #   anthropic/claude-sonnet-4-6    → Anthropic
  #   dashscope/qwen3.5-plus          → 通义千问
  #   ollama/qwen2.5-coder:14b        → 本地 Ollama
  primary: "anthropic/claude-sonnet-4-6"
  # 备选链：主模型失败时按顺序尝试
  fallback_chain:
    - "dashscope/qwen3.5-plus"

# ── Redis ──
redis:
  url: "redis://127.0.0.1:6379/0"   # 与 PyAgent 共享的 Redis 实例

# ── Hermes 智能体 ──
hermes:
  agent_path: ""                     # hermes-agent 安装路径
  iteration_budget: 90               # 每轮对话的迭代预算
  default_toolsets: [core, web, terminal]

# ── 上下文工程 ──
context:
  max_tokens: 200000                 # 上下文窗口最大 Token 数
  autocompact_threshold: 0.8         # Token 使用率超过此阈值触发自动压缩
  tool_result_max_chars: 50000       # 工具返回结果超过此字符数则截断

# ── 会话持久化 ──
session:
  dir: "~/.rrclaw/sessions"          # JSONL 会话文件目录
  rotation_size: 262144              # 256KB 时自动轮转

# ── 容错 ──
resilience:
  health_check_interval: 10          # 健康检查间隔（秒）

# ── 技能 ──
skills:
  auto_sync: true                    # 自动同步 RRCLAW/OpenClaw/Hermes 技能
  sync_interval_hours: 6             # 同步间隔（小时）

# ── ReachRich 行情数据 ──
reachrich:
  base_url: ""                       # Bridge API 地址
  token: ""                          # 用户 Token（HMAC 签名）
  stream_verify_hmac: true           # 验证实时流消息的 HMAC 签名

# ── ACP 运行时（可选）──
acp:
  enabled: false                     # 启用 ACP WebSocket 服务端
  host: "127.0.0.1"
  port: 7790
```

> **优先级**：环境变量 > YAML 配置 > 默认值。在 `.env` 中设置的值会覆盖 `rrclaw.yaml` 中的对应字段。

---

## 使用示例

### 自然语言量化分析

RRCLAW 理解自然语言指令，自动调用对应工具完成分析：

```
用户: "今天涨停板有哪些半导体？"
RRCLAW → 调用 market_query(type="limitup")
       → 按板块="半导体"过滤
       → "今天有3只半导体涨停: ..."

用户: "帮我回测一个突破20日均线的策略"
RRCLAW → 生成策略代码
       → 调用 backtest/run（vectorbt 引擎）
       → 返回收益曲线、夏普比率、最大回撤

用户: "用 pct_chg > 5 AND volume_ratio > 3 筛选股票"
RRCLAW → 调用 DSL 选股器
       → 返回符合条件的股票及基本面数据
```

### Python API 调用

使用 API Key 直接调用 ReachRich 行情数据：

```python
import httpx

headers = {"Authorization": "Bearer rk_your_api_key"}
base = "https://rr.zayl.net/api"

# 实时行情快照（5000+ 标的）
r = httpx.get(f"{base}/fast/realtime/", headers=headers)
print(r.json())

# 涨停板分析
r = httpx.get(f"{base}/bridge/limitup/", headers=headers)

# DSL 多因子选股
r = httpx.post(f"{base}/bridge/screener/", headers=headers, json={
    "payload": {"rules": [{"field": "pct_chg", "op": ">", "value": 5}]},
    "limit": 20
})

# 策略回测
r = httpx.post(f"{base}/bridge/backtest/run/", headers=headers, json={
    "strategy_code": "你的backtrader策略代码",
    "stock": "000001.SZ",
    "start_date": "2025-01-01",
    "end_date": "2026-01-01"
})

# K线数据
r = httpx.get(f"{base}/bridge/kline/", headers=headers, params={
    "stock": "000001.SZ",
    "period": "daily",
    "count": 60
})
```

### MCP 模式

将 RRCLAW 工具暴露给 Claude Desktop 或 Cursor：

```bash
# 启动 PyAgent 工具的 MCP 服务
rrclaw-mcp --backend pyagent

# 启动 ReachRich 行情 MCP 服务
rrclaw-market
```

在 Claude Desktop 配置中添加：

```json
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

---

## ReachRich API 接口

RRCLAW 通过认证 API 连接 [ReachRich](https://rr.zayl.net) 获取 A股行情数据：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/bridge/snapshot/` | GET | 全市场快照（5000+ 标的） |
| `/bridge/limitup/` | GET | 涨跌停板，含板块分析 |
| `/bridge/dragon-tiger/` | GET | 龙虎榜（机构资金流向） |
| `/bridge/concepts/` | GET | 板块/概念排行榜 |
| `/bridge/sentiment/` | GET | 市场情绪与新闻摘要 |
| `/bridge/kline/` | GET | K线数据（日/周/月线） |
| `/bridge/indicators/` | GET | 技术指标（MA/MACD/RSI/BOLL） |
| `/bridge/presets/` | GET | 200+ 预置选股策略 |
| `/bridge/screener/` | POST | DSL 多因子选股 |
| `/bridge/backtest/run/` | POST | 策略回测（backtrader/vectorbt） |
| `/bridge/backtest/run_alpha/` | POST | Alpha 因子回测 |
| `/bridge/backtest/run_mining/` | POST | 自动因子挖掘 |
| `/bridge/ledger/` | GET | AI 策略决策日志 |
| `/bridge/llm/config/` | GET | LLM 模型路由配置 |
| `/fast/realtime/` | GET | 实时报价（亚秒级更新） |
| `/sse/realtime/` | GET | SSE 实时推送流 |

**认证方式**：请求头 `Authorization: Bearer rk_your_api_key` —— 在 ReachRich 设置 → API Key 页面生成你的密钥。

---

## 模块说明

### 核心运行时 (`rrclaw/runtime/`)

| 模块 | 功能 |
|------|------|
| `conversation.py` | **ConversationRuntime** — 异步生成器 LLM 循环，处理流式输出、工具调度、预算管理、错误恢复 |
| `session.py` | **Session** — JSONL 追加写入持久化，256KB 自动轮转 + gzip 归档 |
| `server.py` | **RRClawServer** — 主入口，初始化所有组件、连接网关、管理会话运行时 |
| `config.py` | **RRClawConfig** — 三层配置合并：YAML → 环境变量 → 默认值 |
| `prompt.py` | **PromptBuilder** — 系统提示词构建，注入 SOUL.md + 一级工具索引 |
| `hooks.py` | **HookRegistry** — 工具执行前/后生命周期钩子 |

### 容错系统 (`rrclaw/runtime/resilience/`)

| 层级 | 模块 | 功能 |
|------|------|------|
| L1 | `api_retry.py` | 指数退避重试（500ms 基础，32s 上限，25% 抖动） |
| L2 | `error_classifier.py` | 错误分类 → 结构化恢复建议 |
| L3 | `circuit_breaker.py` | 熔断器，连续 3 次失败后断路 |
| L4 | `recovery_recipes.py` | 7 种故障场景恢复方案 |
| L5 | `health_monitor.py` | 组件健康检查（healthy → degraded → down） |

### 提供商路由 (`rrclaw/runtime/providers/`)

| 模块 | 功能 |
|------|------|
| `router.py` | 前缀路由 + 自动备选链切换 |
| `anthropic.py` | Anthropic Claude，支持流式和提示词缓存 |
| `dashscope.py` | 通义千问（Qwen），通过 OpenAI 兼容 API |
| `openai_compat.py` | 通用 OpenAI 兼容提供商（Ollama、vLLM 等） |
| `credential_pool.py` | 凭证池，4种轮转策略，429 错误自动冷却 |

### 工具系统 (`rrclaw/tools/`)

| 模块 | 功能 |
|------|------|
| `registry.py` | 全局工具注册表，管理 0/1/2 级工具 |
| `executor.py` | 并发/串行调度，超时控制 |
| `search.py` | 工具搜索元工具，关键词匹配 |
| `builtin/` | 内置工具：bash、文件操作、行情查询、ECharts 可视化 |
| `pyagent/` | PyAgent 集成（12 个 Python 智能体，71 条命令） |
| `hermes/` | Hermes 集成（47 个工具 + PTC） |
| `mcp/` | MCP 服务端/客户端 |

### 自进化系统 (`rrclaw/evolution/`)

| 循环 | 时间尺度 | 机制 |
|------|----------|------|
| **循环1** | 秒级 | 工具错误 → 返回 LLM → 自动纠正（最多3次） |
| **循环2** | 分钟级 | 后台审查守护线程，提取记忆和技能 |
| **循环3** | 小时级 | 跨会话模式检测（重复工具链 → 技能） |
| **循环4** | 天级 | GEPA 流水线：收集 → 评估 → A/B 测试 → 部署 |

---

## 常见问题

### Redis 连不上怎么办？

确认 Redis 正在运行：

```bash
redis-cli PING
# 应返回 PONG
```

如果 Redis 需要密码认证，在 `.env` 中设置：

```env
REDIS_URL=redis://:你的密码@127.0.0.1:6379/0
```

macOS 安装 Redis：`brew install redis && brew services start redis`

### 如何获取 ReachRich API Key？

1. 访问 [ReachRich](https://rr.zayl.net) 并注册/登录
2. 进入 设置 → API Key 页面
3. 点击「生成 API Key」，复制以 `rk_` 开头的密钥
4. 填入 `.env` 文件的 `REACHRICH_TOKEN` 字段

### 如何切换 LLM 模型？

在 `rrclaw.yaml` 中修改 `providers.primary`，或设置环境变量：

```bash
# 使用通义千问
export RRCLAW_PRIMARY_MODEL=dashscope/qwen3.5-plus

# 使用本地 Ollama
export RRCLAW_PRIMARY_MODEL=ollama/qwen2.5-coder:14b
```

支持的提供商：
- `anthropic/` — Claude 系列（需要 `ANTHROPIC_API_KEY`）
- `dashscope/` — 通义千问系列（需要 `DASHSCOPE_API_KEY`）
- `ollama/` — 本地模型（需要运行 Ollama 服务）

### 如何只使用行情数据，不接入 IM？

不需要启动 OpenClaw 网关。直接使用 MCP 服务或 Python API：

```bash
# 方式1：MCP 服务（给 Claude Desktop / Cursor 用）
rrclaw-market

# 方式2：Python API（自己写脚本调用）
python -c "
import httpx
r = httpx.get('https://rr.zayl.net/api/bridge/limitup/',
              headers={'Authorization': 'Bearer rk_your_key'})
print(r.json())
"
```

### 回测超时怎么办？

复杂策略的回测可能需要较长时间（最长 300 秒），因子挖掘最长 620 秒。如果超时：

1. 减少回测时间范围
2. 简化策略逻辑
3. 检查网络到 ReachRich API 的延迟

### 如何部署到服务器？

**Docker（推荐）**：

```bash
cd deploy/
docker compose up -d
```

**systemd（Linux）**：

```bash
sudo cp -r . /opt/rrclaw
cd /opt/rrclaw && python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml rrclaw.yaml
# 编辑 rrclaw.yaml 和 .env
sudo cp deploy/hermes-bridge.service /etc/systemd/system/rrclaw.service
sudo systemctl enable --now rrclaw
```

**launchd（macOS）**：

```bash
sudo cp -r . /opt/rrclaw
cd /opt/rrclaw && python -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml rrclaw.yaml
cp deploy/com.hermes-bridge.plist ~/Library/LaunchAgents/com.rrclaw.plist
launchctl load ~/Library/LaunchAgents/com.rrclaw.plist
```

### 测试怎么跑？

```bash
# 激活虚拟环境
source .venv/bin/activate

# 运行全部测试（60个用例）
python -m pytest tests/

# 运行通道集成测试
python tests/test_channels.py
```

---

## 许可证

MIT 许可证。详见 [LICENSE](LICENSE)。

---

<div align="center">

基于 [Claude Code](https://github.com/anthropics/claude-code) · [claw-code](https://github.com/anthropics/claw-code) · [Hermes Agent](https://github.com/NousResearch/hermes-agent) · [OpenClaw](https://github.com/openclaw/openclaw) · [Autoresearch](https://github.com/karpathy/autoresearch) 的核心模式构建

</div>

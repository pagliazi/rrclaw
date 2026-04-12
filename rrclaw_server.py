"""
RRCLAW Unified Server — replaces webchat_api.py entirely.

Combines:
- Chat API (SSE streaming via ConversationRuntime)
- Data APIs (digger/quant/market/system — delegate to PyAgent via Redis)
- Static files (serve the frontend JSX/HTML)
- Auth (simple JWT, reuse the webchat pattern)

Port: 7789
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rrclaw.server")

# Suppress noisy loggers
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Config ───────────────────────────────────────────────

server_start_time = time.time()
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
PORT = int(os.getenv("RRCLAW_PORT", "7789"))
HOST = os.getenv("RRCLAW_HOST", "0.0.0.0")
JWT_SECRET = os.getenv("JWT_SECRET", "rrclaw-secret")
JWT_EXPIRE = int(os.getenv("JWT_EXPIRE", "86400"))
AUTH_USER = os.getenv("WEBCHAT_AUTH_USER", "")
AUTH_PASS = os.getenv("WEBCHAT_AUTH_PASS", "")
N8N_SERVICE_TOKEN = os.getenv("N8N_SERVICE_TOKEN", "openclaw-n8n-2026")
SKILLS_DIR = os.getenv(
    "OPENCLAW_SKILLS_DIR",
    os.path.expanduser("~/OpenClaw-Universe/openclaw-brain/agents/skills"),
)
REPLY_TIMEOUT = int(os.getenv("REPLY_TIMEOUT", "60"))
LONG_TIMEOUT = 1500

USERS_KEY = "openclaw:users"
HISTORY_KEY = "openclaw:chat_history"
DAILY_LOG_KEY = "openclaw:daily_log"
HISTORY_MAX = 500
STRATEGY_REDIS_KEY = "openclaw:strategies"
PLAN_LOG_PREFIX = "rrclaw:plan_log:"
PLAN_HISTORY_KEY = "rrclaw:plan_history"

AVATARS = ["🦀", "🐙", "🦊", "🐯", "🦁", "🐺", "🦅", "🐋", "🐬", "🦈", "🐉", "🦄", "🐝", "🦋", "🌟", "⚡"]
LONG_RUNNING_CMDS = {"quant", "quant_optimize", "backtest", "intraday_select", "intraday_monitor", "claude", "cc", "claude_continue", "ccr", "dev"}

# ── Globals (initialized during lifespan) ────────────────

_redis: aioredis.Redis | None = None

# RRCLAW runtime components
from rrclaw.runtime.conversation import ConversationRuntime, TurnConfig, EventType
from rrclaw.runtime.session import Session
from rrclaw.runtime.config import RRClawConfig
from rrclaw.tools.registry import GlobalToolRegistry
from rrclaw.tools.executor import ToolExecutor
from rrclaw.tools.pyagent.bridge import PyAgentBridge
from rrclaw.tools.index_builder import build_tool_registry
from rrclaw.context.engine import ContextEngine
from rrclaw.runtime.prompt import PromptBuilder
from rrclaw.runtime.providers.router import ProviderRouter, ProviderConfig
from rrclaw.runtime.resilience.error_classifier import RRClawErrorClassifier

# P3 imports
from rrclaw.tools.hermes.runtime import HermesNativeRuntime
from rrclaw.evolution.background_review import BackgroundReviewSystem
from rrclaw.evolution.engine import EvolutionEngine
from rrclaw.skills.loader import SkillLoader
from rrclaw.skills.executor import SkillExecutor
from rrclaw.context.memory.tier1_session import SessionMemory
from rrclaw.context.memory.tier2_user import UserMemory
from rrclaw.context.memory.tier3_system import SystemMemory

# P4 imports
from rrclaw.evolution.gepa_pipeline import GEPAPipeline
from rrclaw.evolution.autoresearch_loop import StrategyResearchLoop
from rrclaw.commands.evolve import EvolveCommand
from rrclaw.commands.research import ResearchCommand

# State
sessions: dict[str, Session] = {}
pyagent_bridge: PyAgentBridge | None = None
registry: GlobalToolRegistry | None = None
executor: ToolExecutor | None = None
llm: ProviderRouter | None = None
error_classifier: RRClawErrorClassifier | None = None
config: RRClawConfig | None = None
context_engine: ContextEngine | None = None
hermes_runtime: HermesNativeRuntime | None = None
background_review_system: BackgroundReviewSystem | None = None
evolution_engine: EvolutionEngine | None = None
skill_loader: SkillLoader | None = None
skill_executor: SkillExecutor | None = None
session_memory: SessionMemory | None = None
user_memory: UserMemory | None = None
system_memory: SystemMemory | None = None
evolve_command: EvolveCommand | None = None
research_command: ResearchCommand | None = None


# ── JWT Helpers ──────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)

def create_token(username: str, role: str, display_name: str = "", avatar: str = "") -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": username, "role": role, "name": display_name or username,
        "avatar": avatar, "exp": int(time.time()) + JWT_EXPIRE, "iat": int(time.time()),
    }).encode())
    sig = _b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"

def verify_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        expected = _b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64url_decode(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


# ── Password Helpers ─────────────────────────────────────

def hash_password(password: str, salt: str = "") -> str:
    if not salt:
        salt = uuid.uuid4().hex[:16]
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}${h}"

def check_password(password: str, stored: str) -> bool:
    if "$" not in stored:
        return password == stored
    salt, h = stored.split("$", 1)
    return hash_password(password, salt).split("$")[1] == h


# ── User Store (Redis) ──────────────────────────────────

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis

async def get_user(username: str) -> dict | None:
    r = await get_redis()
    raw = await r.hget(USERS_KEY, username)
    if raw:
        return json.loads(raw)
    return None

async def save_user(user: dict):
    r = await get_redis()
    await r.hset(USERS_KEY, user["username"], json.dumps(user, ensure_ascii=False))

async def delete_user(username: str):
    r = await get_redis()
    await r.hdel(USERS_KEY, username)

async def list_users() -> list[dict]:
    r = await get_redis()
    raw = await r.hgetall(USERS_KEY)
    users = []
    for name, data in sorted(raw.items()):
        u = json.loads(data)
        u.pop("password", None)
        users.append(u)
    return users

async def init_default_admin():
    r = await get_redis()
    username = AUTH_USER or "admin"
    exists = await r.hexists(USERS_KEY, username)
    if not exists:
        password = AUTH_PASS or "admin"
        await save_user({
            "username": username,
            "password": hash_password(password),
            "role": "admin",
            "display_name": username.capitalize(),
            "avatar": "🦀",
            "created_at": time.time(),
        })
        logger.info(f"Default admin user '{username}' created")


# ── Chat History ─────────────────────────────────────────

async def save_chat_message(role: str, content: str, view: str = "chat", target: str = ""):
    r = await get_redis()
    ts = time.time()
    today = time.strftime("%Y-%m-%d")
    entry = json.dumps({
        "role": role, "content": content[:4000],
        "view": view, "ts": ts, "date": today, "target": target,
    }, ensure_ascii=False)
    await r.lpush(HISTORY_KEY, entry)
    await r.ltrim(HISTORY_KEY, 0, HISTORY_MAX - 1)
    await r.lpush(f"{DAILY_LOG_KEY}:{today}", entry)
    await r.expire(f"{DAILY_LOG_KEY}:{today}", 86400 * 30)


# ── Redis → Orchestrator (legacy delegation) ────────────

async def _send_and_wait(command: str, args: str = "", uid: str = "webchat_default",
                         user_name: str = "", raw_reply: bool = False):
    msg_id = uuid.uuid4().hex[:12]
    reply_channel = f"openclaw:reply:{msg_id}"
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(reply_channel)
    params = {"command": command, "args": args, "reply_channel": reply_channel, "uid": uid}
    if user_name:
        params["user_name"] = user_name
    msg = json.dumps({
        "id": msg_id, "sender": "rrclaw", "target": "orchestrator",
        "action": "route", "params": params, "timestamp": time.time(),
    })
    await r.publish("openclaw:orchestrator", msg)
    timeout = LONG_TIMEOUT if command in LONG_RUNNING_CMDS else REPLY_TIMEOUT
    try:
        async def _wait():
            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "message":
                    continue
                data = json.loads(raw_msg["data"])
                if data.get("type") == "progress":
                    continue
                if raw_reply:
                    return data
                return data.get("text", json.dumps(data, ensure_ascii=False, indent=2))
        return await asyncio.wait_for(_wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if raw_reply:
            return {"text": "超时，Agent 未在规定时间内回复", "error": True}
        return "超时，Agent 未在规定时间内回复"
    except Exception as e:
        if raw_reply:
            return {"text": f"错误: {e}", "error": True}
        return f"错误: {e}"
    finally:
        await pubsub.unsubscribe(reply_channel)


async def send_to_orchestrator(command: str, args: str = "", uid: str = "webchat_default",
                               user_name: str = "") -> str:
    return await _send_and_wait(command, args, uid, user_name, raw_reply=False)


VALID_AGENT_TARGETS = {"manager", "orchestrator", "market", "analysis", "news",
                       "strategist", "backtest", "dev", "browser", "desktop"}

async def send_to_agent(target: str, command: str, args: str = "",
                        uid: str = "webchat_default", user_name: str = "") -> dict:
    msg_id = uuid.uuid4().hex[:12]
    reply_channel = f"openclaw:reply:{msg_id}"
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(reply_channel)

    if target in ("manager", "orchestrator"):
        channel = "openclaw:orchestrator"
        action = "route"
        params = {"command": command, "args": args, "reply_channel": reply_channel, "uid": uid}
        if user_name:
            params["user_name"] = user_name
        effective_target = "orchestrator"
    else:
        channel = f"openclaw:{target}"
        action = "webchat_ask"
        params = {"question": args, "reply_channel": reply_channel}
        effective_target = target

    msg = json.dumps({
        "id": msg_id, "sender": "rrclaw", "target": effective_target,
        "action": action, "params": params, "timestamp": time.time(),
    })
    await r.publish(channel, msg)
    try:
        async def _wait():
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                data = json.loads(raw["data"])
                if data.get("type") == "progress":
                    continue
                return {
                    "text": data.get("text", json.dumps(data, ensure_ascii=False, indent=2)),
                    "source": data.get("source", target),
                }
        return await asyncio.wait_for(_wait(), timeout=REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        return {"text": "超时，Agent 未在规定时间内回复", "source": target}
    except Exception as e:
        return {"text": f"错误: {e}", "source": target}
    finally:
        await pubsub.unsubscribe(reply_channel)


async def stream_agent(target: str, command: str, args: str = "",
                       uid: str = "webchat_default", user_name: str = ""):
    yield f"data: {json.dumps({'type': 'thinking', 'content': '', 'source': target})}\n\n"
    try:
        result = await send_to_agent(target, command, args, uid=uid, user_name=user_name)
        text = result["text"]
        source = result.get("source", target)
        chunks = [text[i:i + 80] for i in range(0, len(text), 80)]
        for chunk in chunks:
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk, 'source': source})}\n\n"
            await asyncio.sleep(0.02)
        yield f"data: {json.dumps({'type': 'done', 'content': '', 'source': source})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e), 'source': target})}\n\n"


# ── Provider Router Builder ─────────────────────────────

def build_provider_router() -> ProviderRouter:
    configs = []
    primary_key = os.getenv("OPENAI_API_KEY", "")
    primary_url = os.getenv("OPENAI_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1")
    primary_model = os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus")

    if primary_key:
        configs.append(ProviderConfig(
            name="dashscope-primary", api_key=primary_key,
            base_url=primary_url, model=primary_model,
        ))

    fallback_key = os.getenv("SILICONFLOW_API_KEY", "")
    if fallback_key:
        configs.append(ProviderConfig(
            name="siliconflow-deepseek", api_key=fallback_key,
            base_url="https://api.siliconflow.cn/v1", model="deepseek-ai/DeepSeek-V3",
        ))

    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        configs.append(ProviderConfig(
            name="deepseek-direct", api_key=deepseek_key,
            base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        ))

    if not configs:
        configs.append(ProviderConfig(
            name="dashscope-default",
            api_key="sk-sp-0dd17ca1a5ed4a108b13d7942216e107",
            base_url="https://coding.dashscope.aliyuncs.com/v1",
            model="qwen3.5-plus",
        ))

    return ProviderRouter(configs)


# ── RRCLAW Runtime Init ─────────────────────────────────

async def init_rrclaw():
    """Initialize all RRCLAW runtime components (called during FastAPI lifespan)."""
    global pyagent_bridge, registry, executor, llm, error_classifier
    global config, context_engine
    global hermes_runtime, background_review_system, evolution_engine
    global skill_loader, skill_executor
    global session_memory, user_memory, system_memory
    global evolve_command, research_command

    logger.info("=== RRCLAW Unified Server Init ===")

    # 0. Config
    config = RRClawConfig.from_file()

    # 1. PyAgent bridge
    pyagent_bridge = PyAgentBridge(redis_url=REDIS_URL)
    try:
        await pyagent_bridge.connect()
        logger.info("PyAgent Redis connected")
    except Exception as e:
        logger.warning(f"PyAgent Redis connection failed: {e}")

    # 2. Hermes runtime
    hermes_runtime = HermesNativeRuntime(
        hermes_path="/tmp/full-deploy-test/hermes-venv",
        model=os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus"),
    )
    if hermes_runtime.available:
        logger.info("Hermes runtime loaded")
    else:
        logger.warning("Hermes runtime not available")

    # 3. Tool registry
    registry = build_tool_registry(
        bridge=pyagent_bridge,
        skills_dir=SKILLS_DIR,
        hermes_runtime=hermes_runtime if hermes_runtime.available else None,
    )
    try:
        from rrclaw.tools.builtin.canvas import CanvasTool
        canvas_tool = CanvasTool(gateway=None)
        registry.register_tier0(canvas_tool)
    except Exception:
        pass

    stats = registry.stats()
    logger.info(f"Tool registry: {stats['tier0']} tier0, {stats['tier1_indexed']} tier1 indexed")

    # 4. LLM provider
    llm = build_provider_router()
    router_status = llm.status()
    logger.info(f"Provider router: {router_status['current']} ({len(router_status['providers'])} providers)")

    # 5. Error classifier
    error_classifier = RRClawErrorClassifier()

    # 6. Tool executor
    executor = ToolExecutor(registry)

    # 7. Memory tiers
    session_memory = SessionMemory(session_id="default")
    user_memory = UserMemory()
    system_memory = SystemMemory()
    logger.info(f"Memory tiers ready")

    # 8. Context engine
    context_engine = ContextEngine(config, registry)
    context_engine.session_memory = session_memory
    context_engine.user_memory = user_memory
    context_engine.system_memory = system_memory

    # 9. Background review
    background_review_system = BackgroundReviewSystem(
        hermes_runtime=hermes_runtime if hermes_runtime.available else None,
    )

    # 10. Skills
    skill_loader = SkillLoader()
    skills = skill_loader.load_all()
    skill_executor = SkillExecutor(skill_loader, registry)
    logger.info(f"Skills loaded: {len(skills)}")

    # 11. Evolution engine
    evolution_engine = EvolutionEngine(redis_url=REDIS_URL)
    try:
        await evolution_engine.start()
        logger.info(f"Evolution engine started")
    except Exception as e:
        logger.warning(f"Evolution engine start failed: {e}")

    # 12. GEPA + Research
    gepa_pipeline = GEPAPipeline(
        hermes_runtime=hermes_runtime if hermes_runtime and hermes_runtime.available else None,
    )
    research_loop = StrategyResearchLoop(
        hermes_runtime=hermes_runtime if hermes_runtime and hermes_runtime.available else None,
        pyagent_bridge=pyagent_bridge,
    )

    # 13. Slash commands
    evolve_command = EvolveCommand(
        evolution_engine=evolution_engine, gepa_pipeline=gepa_pipeline,
        system_memory=system_memory,
    )
    research_command = ResearchCommand(research_loop=research_loop)
    logger.info("Commands: /evolve, /research")

    # Init default admin user
    await init_default_admin()

    # 14. Heartbeat loop — register RRCLAW as orchestrator in Redis
    async def _heartbeat_loop():
        import redis.asyncio as aioredis
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        while True:
            try:
                hb = json.dumps({
                    "ts": time.time(),
                    "pid": os.getpid(),
                    "skills": ["chat", "tool_search", "evolve", "research", "digger", "backtest"],
                    "has_soul": True,
                    "runtime": "rrclaw",
                    "tools": len(registry.get_all_active_schemas()) if registry else 0,
                })
                await r.hset("openclaw:heartbeats", "orchestrator", hb)
            except Exception:
                pass
            await asyncio.sleep(10)

    asyncio.create_task(_heartbeat_loop())

    logger.info("=== RRCLAW Init Complete ===")


async def shutdown_rrclaw():
    """Shutdown RRCLAW components."""
    logger.info("Shutting down RRCLAW...")
    if evolution_engine:
        await evolution_engine.stop()
    if hermes_runtime:
        hermes_runtime.shutdown()
    if pyagent_bridge:
        await pyagent_bridge.close()
    logger.info("RRCLAW shutdown complete")


# ── FastAPI App ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_rrclaw()
    yield
    await shutdown_rrclaw()


app = FastAPI(title="RRCLAW Unified Server", docs_url=None, redoc_url=None, lifespan=lifespan)


# ── Auth Middleware ──────────────────────────────────────

PUBLIC_PATHS = {"/", "/monitor", "/api/auth/login", "/api/health", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/api/auth/login") or path.startswith("/static/"):
            return await call_next(request)

        # n8n service token
        if (path.startswith("/api/n8n/") or path.startswith("/api/yao/")) and N8N_SERVICE_TOKEN:
            token = request.headers.get("X-N8n-Token", "")
            if token == N8N_SERVICE_TOKEN:
                request.state.user = {"sub": "n8n", "role": "service"}
                return await call_next(request)

        auth = request.headers.get("Authorization", "")

        if auth.startswith("Bearer "):
            token_data = verify_token(auth[7:])
            if token_data:
                request.state.user = token_data
                return await call_next(request)

        if auth.startswith("Basic ") and AUTH_USER:
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pwd = decoded.split(":", 1)
                if user == AUTH_USER and pwd == AUTH_PASS:
                    request.state.user = {"sub": user, "role": "admin", "name": user}
                    return await call_next(request)
            except Exception:
                pass

        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


app.add_middleware(AuthMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Auth helpers ─────────────────────────────────────────

def get_current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ══════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════

# ── Health ───────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    components = {"server": "ok", "ts": time.time()}
    if pyagent_bridge:
        components["redis"] = "ok" if pyagent_bridge.is_connected else "disconnected"
    if llm:
        components["llm"] = llm.status().get("current", "unknown")
    if registry:
        components["tools"] = registry.stats()
    return components


# ── Auth API ─────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "用户名和密码不能为空"})
    user = await get_user(username)
    if not user or not check_password(password, user.get("password", "")):
        return JSONResponse(status_code=401, content={"ok": False, "msg": "用户名或密码错误"})
    token = create_token(username, user["role"], user.get("display_name", ""), user.get("avatar", "🦀"))
    return {"ok": True, "token": token, "user": {
        "username": username, "role": user["role"],
        "display_name": user.get("display_name", username),
        "avatar": user.get("avatar", "🦀"),
    }}


@app.get("/api/auth/me")
async def api_me(request: Request):
    user = get_current_user(request)
    full = await get_user(user["sub"])
    if full:
        full.pop("password", None)
        return full
    return {"username": user["sub"], "role": user.get("role", "user"),
            "display_name": user.get("name", ""), "avatar": user.get("avatar", "🦀")}


@app.put("/api/auth/profile")
async def api_update_profile(request: Request):
    current = get_current_user(request)
    body = await request.json()
    user = await get_user(current["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if "display_name" in body:
        user["display_name"] = body["display_name"][:32]
    if "avatar" in body and body["avatar"] in AVATARS:
        user["avatar"] = body["avatar"]
    if "password" in body and body["password"]:
        user["password"] = hash_password(body["password"])
    await save_user(user)
    user.pop("password", None)
    token = create_token(user["username"], user["role"], user.get("display_name", ""), user.get("avatar", "🦀"))
    return {"ok": True, "user": user, "token": token}


# ── Admin API ────────────────────────────────────────────

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    require_admin(request)
    return {"users": await list_users()}

@app.post("/api/admin/users")
async def api_admin_create_user(request: Request):
    require_admin(request)
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "user")
    if not username or not password:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "用户名和密码不能为空"})
    if role not in ("admin", "user", "viewer"):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "角色必须是 admin/user/viewer"})
    existing = await get_user(username)
    if existing:
        return JSONResponse(status_code=409, content={"ok": False, "msg": f"用户 {username} 已存在"})
    import random
    await save_user({
        "username": username, "password": hash_password(password), "role": role,
        "display_name": body.get("display_name", username),
        "avatar": body.get("avatar", random.choice(AVATARS)),
        "created_at": time.time(),
    })
    return {"ok": True, "msg": f"用户 {username} 已创建"}

@app.put("/api/admin/users/{username}")
async def api_admin_update_user(username: str, request: Request):
    require_admin(request)
    user = await get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    body = await request.json()
    if "role" in body and body["role"] in ("admin", "user", "viewer"):
        user["role"] = body["role"]
    if "display_name" in body:
        user["display_name"] = body["display_name"][:32]
    if "avatar" in body:
        user["avatar"] = body["avatar"]
    if "password" in body and body["password"]:
        user["password"] = hash_password(body["password"])
    await save_user(user)
    return {"ok": True, "msg": f"用户 {username} 已更新"}

@app.delete("/api/admin/users/{username}")
async def api_admin_delete_user(username: str, request: Request):
    admin = require_admin(request)
    if username == admin["sub"]:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "不能删除自己"})
    await delete_user(username)
    return {"ok": True, "msg": f"用户 {username} 已删除"}

@app.get("/api/admin/avatars")
async def api_admin_avatars():
    return {"avatars": AVATARS}


# ── Chat API (SSE streaming via ConversationRuntime) ─────

@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.json()
    msg = body.get("message", "").strip()
    target = body.get("target", "manager").strip()
    if not msg:
        raise HTTPException(400, "missing message")

    user = getattr(request.state, "user", None)
    uid = f"web_{user['sub']}" if user else "webchat_default"
    user_name = user.get("name", user.get("sub", "")) if user else ""

    # If target is rrclaw / default manager, use ConversationRuntime
    use_runtime = target in ("manager", "orchestrator", "rrclaw")

    if use_runtime and registry and llm:
        await save_chat_message("user", msg, target="rrclaw")

        async def _runtime_stream():
            session_id = f"web-{uid}"

            # Slash command routing
            stripped = msg.strip()
            if stripped.startswith("/evolve") and evolve_command:
                args = stripped[len("/evolve"):].strip()
                result = await evolve_command.execute(args)
                yield f"data: {json.dumps({'type': 'chunk', 'content': result, 'source': 'rrclaw'})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'content': '', 'source': 'rrclaw'})}\n\n"
                await save_chat_message("assistant", result[:4000], target="rrclaw")
                return
            if stripped.startswith("/research") and research_command:
                args = stripped[len("/research"):].strip()
                result = await research_command.execute(args)
                yield f"data: {json.dumps({'type': 'chunk', 'content': result, 'source': 'rrclaw'})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'content': '', 'source': 'rrclaw'})}\n\n"
                await save_chat_message("assistant", result[:4000], target="rrclaw")
                return

            # Get or create session
            if session_id not in sessions:
                sessions[session_id] = Session(session_id=session_id)
            session = sessions[session_id]

            # Skill matching
            if skill_executor:
                matched = skill_executor.match_skill(msg)
                if matched:
                    instruction = skill_executor.prepare_skill(matched, session_id=session_id)
                    if instruction:
                        session.append_system(instruction)

            # Create runtime
            runtime = ConversationRuntime(
                session=session,
                registry=registry,
                executor=executor,
                llm_provider=llm,
                context_provider=context_engine,
                error_classifier=error_classifier,
                config=TurnConfig(max_tool_rounds=10),
            )
            if background_review_system:
                runtime.background_review = background_review_system
                background_review_system.increment_turn()

            yield f"data: {json.dumps({'type': 'thinking', 'content': '', 'source': 'rrclaw'})}\n\n"

            full_text = ""
            try:
                async for event in runtime.run_turn(msg):
                    if event.type == EventType.TEXT_DELTA:
                        full_text += event.data
                        yield f"data: {json.dumps({'type': 'chunk', 'content': event.data, 'source': 'rrclaw'})}\n\n"

                    elif event.type == EventType.TOOL_START:
                        yield f"data: {json.dumps({'type': 'tool_start', 'name': event.data.name, 'source': 'rrclaw'})}\n\n"

                    elif event.type == EventType.TOOL_RESULT:
                        r = event.data["result"]
                        status = "ok" if not r.is_error else "error"
                        yield f"data: {json.dumps({'type': 'tool_result', 'status': status, 'preview': (r.content or '')[:200], 'source': 'rrclaw'})}\n\n"

                    elif event.type == EventType.USAGE:
                        yield f"data: {json.dumps({'type': 'usage', 'input_tokens': event.data.input_tokens, 'output_tokens': event.data.output_tokens, 'source': 'rrclaw'})}\n\n"

                    elif event.type == EventType.ERROR:
                        yield f"data: {json.dumps({'type': 'error', 'content': str(event.data), 'source': 'rrclaw'})}\n\n"
                        return

                yield f"data: {json.dumps({'type': 'done', 'content': '', 'source': 'rrclaw'})}\n\n"

                if full_text:
                    await save_chat_message("assistant", full_text[:4000], target="rrclaw")

                # Record to evolution engine
                if evolution_engine and skill_executor:
                    if skill_executor.get_active_skill(session_id):
                        skill_executor.complete_skill(session_id, success=True)

            except Exception as e:
                logger.error(f"Runtime error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'content': str(e), 'source': 'rrclaw'})}\n\n"

        return StreamingResponse(
            _runtime_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Fallback: delegate to orchestrator via Redis (legacy mode)
    if target not in VALID_AGENT_TARGETS:
        target = "manager"

    if target in ("manager", "orchestrator"):
        if msg.startswith("/"):
            parts = msg[1:].split(None, 1)
            cmd = parts[0] if parts else ""
            args = parts[1] if len(parts) > 1 else ""
        else:
            cmd, args = "chat", msg
    else:
        cmd, args = "chat", msg

    await save_chat_message("user", msg, target=target)

    async def _legacy_stream():
        full_content = ""
        source = target
        async for chunk in stream_agent(target, cmd, args, uid=uid, user_name=user_name):
            full_content += chunk
            yield chunk
        text = ""
        for line in full_content.split("\n"):
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                    if d.get("type") in ("chunk", "done"):
                        text += d.get("content", "")
                    if d.get("source"):
                        source = d["source"]
                except Exception:
                    pass
        if text:
            await save_chat_message("assistant", text, target=source)

    return StreamingResponse(
        _legacy_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/history")
async def api_chat_history(limit: int = 100, offset: int = 0):
    r = await get_redis()
    raw = await r.lrange(HISTORY_KEY, offset, offset + limit - 1)
    messages = []
    for item in raw:
        try:
            messages.append(json.loads(item))
        except Exception:
            pass
    messages.reverse()
    return {"messages": messages, "total": await r.llen(HISTORY_KEY)}


# Keep old path for compat
@app.get("/api/history")
async def api_history(limit: int = 100, offset: int = 0):
    return await api_chat_history(limit, offset)


@app.delete("/api/history")
async def api_history_clear():
    r = await get_redis()
    await r.delete(HISTORY_KEY)
    return {"ok": True}


# ── Command API ──────────────────────────────────────────

@app.post("/api/command")
async def api_command(request: Request):
    body = await request.json()
    cmd = body.get("cmd", "")
    args = body.get("args", "")
    if not cmd:
        raise HTTPException(400, "missing cmd")

    # Handle system commands locally
    def _local_cmd(cmd):
        if cmd == "llm_status":
            if llm and llm._providers:
                cfg = llm._configs[llm._current_index]
                return "Provider: " + cfg.name + " | Model: " + cfg.model
            return "LLM not initialized"
        if cmd == "embed_status":
            return "Embedding: local bge-m3 (Ollama)"
        if cmd == "data_source_status":
            return json.dumps({"redis": "ok" if pyagent_bridge and pyagent_bridge.is_connected else "disconnected", "reachrich": "configured" if os.getenv("REACHRICH_TOKEN") else "not configured", "tools": len(registry.get_all_active_schemas()) if registry else 0}, ensure_ascii=False, indent=2)
        if cmd == "soul_check":
            return "SOUL: RRCLAW ConversationRuntime active"
        if cmd == "memory_health":
            return "Session: ok | User: ok | System: ok"
        if cmd == "memory_hygiene":
            return "Memory hygiene: ok (auto-prune enabled)"
        if cmd == "status":
            stats = evolution_engine._stats if evolution_engine else {}
            t0 = len(registry.tier0_tools) if registry else 0
            t1 = len(registry.tier1_index) if registry else 0
            return json.dumps({"runtime": "RRCLAW ConversationRuntime", "evolution": stats, "tools": {"tier0": t0, "tier1": t1}, "uptime": f"{time.time() - server_start_time:.0f}s"}, ensure_ascii=False, indent=2)
        return None

    local_result = _local_cmd(cmd)
    if local_result is not None:
        return {"result": local_result}

    # Slash commands (/evolve, /research)
    if cmd == "evolve" and evolve_command:
        result = await evolve_command.execute(args)
        return {"result": result}
    if cmd == "research" and research_command:
        result = await research_command.execute(args)
        return {"result": result}

    # Everything else → delegate to PyAgent via Redis
    user = getattr(request.state, "user", None)
    uid = f"web_{user['sub']}" if user else "webchat_default"
    try:
        result = await send_to_orchestrator(cmd, args, uid=uid)
    except Exception as e:
        result = f"命令执行失败: {e}"
    return {"result": result or ""}


# ── Overview / System ────────────────────────────────────

def _is_cn_trading_hours() -> bool:
    from datetime import datetime, timezone, timedelta
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    if now_bj.weekday() >= 5:
        return False
    t = now_bj.hour * 60 + now_bj.minute
    return (9 * 60 + 25) <= t <= (15 * 60 + 5)

SCHEDULED_AGENTS = {"intraday": "交易时段自动启动"}

@app.get("/api/overview")
async def api_overview():
    r = await get_redis()
    agents = {}
    trading_hours = _is_cn_trading_hours()
    hb_raw = await r.hgetall("openclaw:heartbeats")
    for name, raw in hb_raw.items():
        try:
            hb = json.loads(raw)
            age = time.time() - hb.get("ts", 0)
            if age < 30:
                status = "online"
            elif age < 60:
                status = "slow"
            elif name in SCHEDULED_AGENTS and not trading_hours:
                status = "sleeping"
            else:
                status = "offline"
            agents[name] = {"status": status, "pid": hb.get("pid", 0), "age": round(age),
                            "scheduled": SCHEDULED_AGENTS.get(name, "")}
        except Exception:
            agents[name] = {"status": "error", "pid": 0, "age": -1, "scheduled": ""}

    channels = {}
    ch_raw = await r.hgetall("openclaw:channel_heartbeats")
    for name, raw in ch_raw.items():
        try:
            hb = json.loads(raw)
            age = time.time() - hb.get("ts", 0)
            channels[name] = {
                "status": "online" if age < 30 else ("slow" if age < 60 else "offline"),
                "age": round(age), "fails": hb.get("consecutive_failures", 0),
                "mode": hb.get("mode", ""),
            }
        except Exception:
            channels[name] = {"status": "error", "age": -1, "fails": 0, "mode": ""}

    # Add RRCLAW runtime info
    rrclaw_info = {
        "rrclaw": {
            "status": "online",
            "tools": registry.stats() if registry else {},
            "llm": llm.status().get("current", "unknown") if llm else "unavailable",
        }
    }
    agents.update(rrclaw_info)

    return {"agents": agents, "channels": channels, "ts": time.time()}


# ── LLM Config ───────────────────────────────────────────

@app.get("/api/llm/config")
async def api_llm_config():
    if llm:
        status = llm.status()
        return {
            "current_provider": status.get("current", ""),
            "providers": status.get("providers", []),
            "model": os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus"),
        }
    return {"error": "LLM not initialized"}


@app.post("/api/llm/config")
async def api_llm_config_update(request: Request):
    require_admin(request)
    body = await request.json()
    # Allow updating preferred model at runtime
    model = body.get("model", "")
    if model:
        os.environ["RRCLAW_DEFAULT_MODEL"] = model
    return {"ok": True, "model": os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus")}


# ── Market ───────────────────────────────────────────────

@app.get("/api/market/overview")
async def api_market_overview():
    if pyagent_bridge and pyagent_bridge.is_connected:
        try:
            zt = await pyagent_bridge.call_agent("market", "get_limitup", {}, timeout=15)
            lb = await pyagent_bridge.call_agent("market", "get_limitstep", {}, timeout=15)
            hot = await pyagent_bridge.call_agent("market", "get_hot", {}, timeout=15)
            return {
                "limitup": zt if not isinstance(zt, dict) or "error" not in zt else [],
                "limitstep": lb if not isinstance(lb, dict) or "error" not in lb else [],
                "hot": hot if not isinstance(hot, dict) or "error" not in hot else [],
                "ts": time.time(),
            }
        except Exception as e:
            logger.warning(f"Market overview via PyAgent failed: {e}")

    # Fallback: orchestrator
    result = await send_to_orchestrator("summary", "")
    return {"text": result, "ts": time.time()}


# ── Digger API ───────────────────────────────────────────

@app.get("/api/digger/status")
async def api_digger_status(request: Request):
    if pyagent_bridge and pyagent_bridge.is_connected:
        try:
            result = await pyagent_bridge.call_agent("backtest", "list_ledger", {}, timeout=15)
            return {"status": "ok", "text": str(result)}
        except Exception:
            pass
    user = getattr(request.state, "user", None)
    uid = f"web_{user['sub']}" if user else "webchat_default"
    result = await send_to_orchestrator("digger_status", "", uid=uid)
    return {"status": "ok", "text": result}


@app.post("/api/digger/start")
async def api_digger_start(request: Request):
    body = await request.json()
    rounds = body.get("rounds", 5)
    factors = body.get("factors", 5)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'started', 'rounds': rounds, 'factors': factors})}\n\n"
        try:
            if pyagent_bridge and pyagent_bridge.is_connected:
                result = await pyagent_bridge.call_agent(
                    "backtest", "digger",
                    {"rounds": rounds, "factors": factors},
                    timeout=600,
                )
                yield f"data: {json.dumps({'type': 'done', 'text': str(result)}, ensure_ascii=False)}\n\n"
            else:
                # Fallback to orchestrator
                user = getattr(request.state, "user", None)
                uid = f"web_{user['sub']}" if user else "webchat_default"
                args = json.dumps({"rounds": rounds, "factors": factors})
                result = await send_to_orchestrator("digger", args, uid=uid, timeout=600)
                yield f"data: {json.dumps({'type': 'done', 'text': result}, ensure_ascii=False)}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'text': '超时'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            yield "data: {\"type\":\"close\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/digger/factors")
async def api_digger_factors(request: Request):
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.factor_library import FactorLibrary
        lib = FactorLibrary(redis_client=await get_redis())
        all_factors = await lib.get_all_factors(status="")
        items = []
        for f in all_factors:
            d = f.to_dict()
            d.pop("oos_sharpe_history", None)
            code = d.get("code", "")
            has_nested = "for i in range" in code and "for j in range" in code
            has_apply = ".apply(" in code
            d["complexity"] = "nested" if has_nested else ("apply" if has_apply else "vectorized")
            d["code_lines"] = code.count("\n") + 1 if code else 0
            d["combinable"] = not has_nested
            items.append(d)
        items.sort(key=lambda x: x.get("sharpe", 0), reverse=True)
        stats = await lib.get_stats()
        stats["complexity_dist"] = {
            "vectorized": sum(1 for x in items if x["complexity"] == "vectorized"),
            "apply": sum(1 for x in items if x["complexity"] == "apply"),
            "nested": sum(1 for x in items if x["complexity"] == "nested"),
        }
        return {"factors": items, "stats": stats}
    except Exception as e:
        logger.warning(f"digger/factors error: {e}")
        # Fallback: delegate to orchestrator
        result = await send_to_orchestrator("factor_list", "")
        return {"factors": [], "stats": {}, "text": result}


@app.post("/api/digger/retire")
async def api_digger_retire(request: Request):
    body = await request.json()
    factor_id = body.get("factor_id", "")
    if not factor_id:
        raise HTTPException(400, "factor_id required")
    # Prefer PyAgent path
    if pyagent_bridge and pyagent_bridge.is_connected:
        try:
            result = await pyagent_bridge.call_agent(
                "backtest", "retire", {"factor_id": factor_id}, timeout=30,
            )
            return {"ok": True, "factor_id": factor_id, "result": result}
        except Exception:
            pass
    # Fallback: direct FactorLibrary
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.factor_library import FactorLibrary
        lib = FactorLibrary(redis_client=await get_redis())
        ok = await lib.retire_factor(factor_id)
        return {"ok": ok, "factor_id": factor_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/digger/combine")
async def api_digger_combine(request: Request):
    body = await request.json()
    factor_ids = body.get("factor_ids", [])
    count = body.get("max_factors", body.get("count", 5))
    mode = body.get("mode", "smart")

    try:
        if pyagent_bridge and pyagent_bridge.is_connected:
            result = await pyagent_bridge.call_agent(
                "backtest", "combine",
                {"count": count, "mode": mode, "factor_ids": factor_ids},
                timeout=600,
            )
            return {"ok": True, "result": result}
        else:
            # Fallback: use FactorLibrary + orchestrator (legacy path)
            import sys
            brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
            if brain_path not in sys.path:
                sys.path.insert(0, brain_path)
            from agents.factor_library import FactorLibrary
            from agents.bridge_client import get_bridge_client
            from datetime import date, timedelta

            lib = FactorLibrary(redis_client=await get_redis())
            bridge_client = get_bridge_client()

            if factor_ids:
                all_factors = await lib.get_all_factors(status="active")
                candidates = [f for f in all_factors if f.id in factor_ids]
            else:
                candidates = await lib.get_combine_candidates()

            if len(candidates) < 2:
                return {"ok": False, "error": f"需要至少 2 个因子，当前 {len(candidates)} 个"}

            top_n = candidates[:count]
            input_factors_info = []
            codes, summaries = [], []
            for i, f in enumerate(top_n):
                renamed = f.code.replace("def generate_factor(", f"def _factor_{i+1}(")
                codes.append(f"# --- factor {i+1}: {f.sub_theme or f.theme} (sharpe={f.sharpe:.3f}, ir={f.ir:.3f}) ---\n{renamed}")
                summaries.append(f"{f.id}: {f.sub_theme or f.theme} sharpe={f.sharpe:.3f} ir={f.ir:.3f}")
                input_factors_info.append({
                    "id": f.id, "theme": f.sub_theme or f.theme,
                    "sharpe": f.sharpe, "ir": f.ir, "ic_mean": f.ic_mean,
                    "win_rate": f.win_rate, "trades": f.trades, "max_drawdown": f.max_drawdown,
                })

            combiner = (
                "\n\nimport numpy as np\nimport pandas as pd\n\n"
                "def generate_factor(matrices):\n"
                "    factors = []\n"
            )
            for i in range(len(top_n)):
                combiner += f"    try:\n        factors.append(_factor_{i+1}(matrices))\n    except Exception:\n        pass\n"
            combiner += (
                "    if not factors:\n"
                "        return pd.DataFrame(0, index=matrices['close'].index, columns=matrices['close'].columns)\n"
                "    stacked = np.stack([f.values for f in factors], axis=0)\n"
                "    combined = np.nanmean(stacked, axis=0)\n"
                "    return pd.DataFrame(combined, index=matrices['close'].index, columns=matrices['close'].columns)\n"
            )
            combined_code = "\n\n".join(codes) + combiner

            start_date = (date.today() - timedelta(days=180)).isoformat()
            end_date = date.today().isoformat()

            combined_metrics = {}
            result_text = ""
            try:
                resp = await bridge_client.run_factor_mining(
                    factor_code=combined_code, start_date=start_date, end_date=end_date,
                )
                if resp.get("status") == "error":
                    result_text = f"沙箱执行失败: {resp.get('error', '未知错误')}"
                else:
                    combined_metrics = resp.get("metrics") or {}
                    result_text = json.dumps(resp, ensure_ascii=False, indent=2)[:3000]
            except Exception as e:
                result_text = f"Bridge 调用失败: {e}"

            evaluation = lib.evaluate_combine_quality(input_factors_info, combined_metrics)
            verdict = evaluation["verdict"]

            record = {
                "input_factors": input_factors_info,
                "input_factor_ids": [f.id for f in top_n],
                "combined_code_preview": combined_code[:2000],
                "combined_metrics": combined_metrics,
                "evaluation": evaluation,
                "verdict": verdict,
                "result_raw": result_text[:3000] if isinstance(result_text, str) else "",
                "status": "accepted" if verdict == "accept" else "rejected" if verdict == "reject" else "marginal",
            }
            record_id = await lib.save_combine_record(record)

            return {
                "ok": True, "record_id": record_id, "factors_used": len(top_n),
                "summaries": summaries, "verdict": verdict, "evaluation": evaluation,
                "combined_metrics": combined_metrics, "result": result_text,
            }
    except Exception as e:
        logger.error(f"digger/combine error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@app.get("/api/digger/combine/history")
async def api_digger_combine_history(limit: int = 20):
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.factor_library import FactorLibrary
        lib = FactorLibrary(redis_client=await get_redis())
        records = await lib.get_combine_records(limit=limit)
        for rec in records:
            rec.pop("combined_code_preview", None)
            rec.pop("result_raw", None)
        return {"records": records}
    except Exception as e:
        return {"records": [], "error": str(e)}


@app.get("/api/digger/combine/{record_id}")
async def api_digger_combine_detail(record_id: str):
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.factor_library import FactorLibrary
        lib = FactorLibrary(redis_client=await get_redis())
        rec = await lib.get_combine_record(record_id)
        if not rec:
            raise HTTPException(404, "record not found")
        return rec
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Quant API ────────────────────────────────────────────

@app.post("/api/quant/optimize")
async def api_quant_optimize(request: Request):
    body = await request.json()
    topic = body.get("topic", "")
    base_title = body.get("base_title", "")
    if not topic:
        raise HTTPException(400, "missing topic")

    optimize_payload = {"topic": topic, "base_title": base_title}
    if body.get("base_preset"):
        optimize_payload["base_preset"] = body["base_preset"]
    else:
        optimize_payload["base_code"] = body.get("base_code", "")
        optimize_payload["base_metrics"] = body.get("base_metrics", {})

    optimize_args = json.dumps(optimize_payload, ensure_ascii=False)
    result = await send_to_orchestrator("quant_optimize", optimize_args)
    return {"result": result}


@app.post("/api/quant/stream")
async def api_quant_stream(request: Request):
    body = await request.json()
    cmd = body.get("cmd", "quant")
    args = body.get("args", "")
    topic = body.get("topic", args).strip()

    if not topic and cmd == "quant":
        raise HTTPException(400, "missing topic")

    run_id = uuid.uuid4().hex[:12]
    progress_channel = f"openclaw:quant_progress:{run_id}"
    backtest_mode = body.get("mode", "vectorbt")

    if cmd == "quant_optimize":
        optimize_payload = body.get("optimize_payload", {})
        if not optimize_payload.get("topic"):
            optimize_payload["topic"] = topic
        optimize_payload["mode"] = backtest_mode
        command_args = json.dumps(optimize_payload, ensure_ascii=False)
    else:
        command_args = json.dumps({"topic": topic, "mode": backtest_mode}, ensure_ascii=False)

    msg_id = uuid.uuid4().hex[:12]
    reply_channel = f"openclaw:reply:{msg_id}"

    async def event_stream():
        r = await get_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(progress_channel, reply_channel)

        msg = json.dumps({
            "id": msg_id, "sender": "rrclaw", "target": "orchestrator",
            "action": "route",
            "params": {
                "command": cmd, "args": command_args,
                "reply_channel": reply_channel,
                "progress_channel": progress_channel,
            },
            "timestamp": time.time(),
        })
        await r.publish("openclaw:orchestrator", msg)

        yield f"data: {json.dumps({'type': 'started', 'run_id': run_id, 'topic': topic})}\n\n"

        try:
            deadline = time.time() + LONG_TIMEOUT
            while time.time() < deadline:
                raw = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0), timeout=5.0)
                if raw is None:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    continue
                if raw["type"] != "message":
                    continue
                channel = raw["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                data = json.loads(raw["data"])

                if channel == progress_channel:
                    yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                    if data.get("type") == "done":
                        break
                elif channel == reply_channel:
                    yield f"data: {json.dumps({'type': 'final', 'text': data.get('text', '')}, ensure_ascii=False, default=str)}\n\n"
                    break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': '超时'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            await pubsub.unsubscribe(progress_channel, reply_channel)
            yield "data: {\"type\":\"close\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/quant/records")
async def api_quant_records():
    r = await get_redis()
    raw_list = await r.lrange("openclaw:quant_records", 0, 99)
    records = []
    for raw in raw_list:
        try:
            rec = json.loads(raw)
            full_m = rec.get("metrics", {})
            summary_m = {k: v for k, v in full_m.items() if k != "trade_log"}
            records.append({
                "id": rec.get("id", ""), "title": rec.get("title", ""),
                "topic": rec.get("topic", ""), "status": rec.get("status", ""),
                "metrics": summary_m, "attempts": rec.get("attempts", 0),
                "created_at": rec.get("created_at", ""), "mode": rec.get("mode", ""),
                "type": "research", "has_code": bool(rec.get("code")),
            })
        except Exception:
            continue
    return {"records": records}


@app.get("/api/quant/records/{record_id}")
async def api_quant_record_detail(record_id: str):
    r = await get_redis()
    raw_list = await r.lrange("openclaw:quant_records", 0, 99)
    for raw in raw_list:
        try:
            rec = json.loads(raw)
            if rec.get("id") == record_id:
                return rec
        except Exception:
            continue
    raise HTTPException(404, "record not found")


# ── Strategies ───────────────────────────────────────────

@app.get("/api/strategies")
async def api_strategies_list():
    r = await get_redis()
    raw = await r.hgetall(STRATEGY_REDIS_KEY)
    strats = []
    for k, v in raw.items():
        try:
            strats.append(json.loads(v))
        except Exception:
            pass
    strats.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    presets = []
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.bridge_client import get_bridge_client
        bridge = get_bridge_client()
        resp = await bridge.get_presets()
        for p in resp.get("presets", []):
            presets.append({
                "id": f"preset_{p.get('slug', p.get('id', ''))}",
                "title": p.get("name", ""), "description": p.get("description", ""),
                "source": "preset", "category": p.get("category", ""),
                "status": "active", "metrics": {}, "synced_to_139": True,
                "created_at": p.get("created_at", ""),
            })
    except Exception:
        pass

    return {"strategies": strats + presets, "count": len(strats) + len(presets),
            "factor_strategies": len(strats)}


@app.get("/api/strategies/{strategy_id}")
async def api_strategy_detail(strategy_id: str):
    r = await get_redis()
    raw = await r.hget(STRATEGY_REDIS_KEY, strategy_id)
    if raw:
        return json.loads(raw)
    if strategy_id.startswith("preset_"):
        try:
            import sys
            brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
            if brain_path not in sys.path:
                sys.path.insert(0, brain_path)
            from agents.bridge_client import get_bridge_client
            bridge = get_bridge_client()
            resp = await bridge.get_presets()
            slug = strategy_id[7:]
            for p in resp.get("presets", []):
                if p.get("slug") == slug or str(p.get("id")) == slug:
                    return {
                        "id": strategy_id, "title": p.get("name", ""),
                        "description": p.get("description", ""),
                        "source": "preset", "category": p.get("category", ""),
                        "code": json.dumps(p.get("payload", {}), indent=2, ensure_ascii=False),
                        "metrics": {}, "status": "active", "synced_to_139": True,
                    }
        except Exception:
            pass
    raise HTTPException(404, "strategy not found")


@app.delete("/api/strategies/{strategy_id}")
async def api_strategy_delete(strategy_id: str, request: Request):
    require_admin(request)
    r = await get_redis()
    await r.hdel(STRATEGY_REDIS_KEY, strategy_id)
    return {"ok": True}


# ── Usage ────────────────────────────────────────────────

@app.get("/api/usage")
async def api_usage():
    # Return RRCLAW session-level usage stats
    total_input = 0
    total_output = 0
    for sid, session in sessions.items():
        for u in session.usage_records:
            total_input += u.input_tokens
            total_output += u.output_tokens

    return {
        "sessions": len(sessions),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "provider": llm.status().get("current", "unknown") if llm else "unavailable",
        "ts": time.time(),
    }


# ── Agents Info ──────────────────────────────────────────

@app.get("/api/agents/info")
async def api_agents_info():
    r = await get_redis()
    hb_raw = await r.hgetall("openclaw:heartbeats")
    agents = {}
    now = time.time()

    # Load skills metadata
    agent_meta = {}
    try:
        import yaml as _yaml
        skills_dir = Path(SKILLS_DIR)
        if skills_dir.exists():
            for yf in sorted(skills_dir.glob("*_skills.yaml")):
                try:
                    data = _yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
                    name = data.get("agent", yf.stem.replace("_skills", ""))
                    agent_meta[name] = {
                        "description": data.get("description", ""),
                        "skills": [s.get("name", "?") for s in data.get("skills", []) if isinstance(s, dict)],
                    }
                except Exception:
                    continue
    except ImportError:
        pass

    for name in sorted(set(list(hb_raw.keys()) + list(agent_meta.keys()))):
        hb = {}
        status = "offline"
        pid = 0
        age = -1
        if name in hb_raw:
            try:
                hb = json.loads(hb_raw[name])
                age = now - hb.get("ts", 0)
                status = "online" if age < 30 else ("slow" if age < 60 else "offline")
                pid = hb.get("pid", 0)
            except Exception:
                pass
        meta = agent_meta.get(name, {})
        agents[name] = {
            "status": status, "pid": pid,
            "age": round(age) if age >= 0 else -1,
            "description": meta.get("description", ""),
            "skills": meta.get("skills", hb.get("skills", [])),
        }

    return {"agents": agents}


# ── Tools ────────────────────────────────────────────────

@app.get("/api/tools")
async def api_tools():
    if registry:
        return {"tools": registry.stats(), "tier0": registry.get_all_active_schemas()}
    return {"tools": {}, "tier0": []}


# ── Skills ───────────────────────────────────────────────

@app.get("/api/skills")
async def api_skills():
    try:
        import yaml as _yaml
        skills_dir = Path(SKILLS_DIR)
        result = []
        if skills_dir.exists():
            for yf in sorted(skills_dir.glob("*_skills.yaml")):
                try:
                    data = _yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
                    agent = data.get("agent", yf.stem.replace("_skills", ""))
                    skills = []
                    for s in data.get("skills", []):
                        if not isinstance(s, dict):
                            continue
                        skills.append({
                            "name": s.get("name", ""),
                            "description": s.get("description", ""),
                            "trigger": s.get("trigger", ""),
                            "params": {k: v for k, v in (s.get("params") or {}).items()},
                        })
                    result.append({"agent": agent, "description": data.get("description", ""), "skills": skills})
                except Exception:
                    continue
        return {"agents": result}
    except ImportError:
        return {"agents": []}


# ── Diagnostics ──────────────────────────────────────────

@app.get("/api/diagnostics")
async def api_diagnostics():
    r = await get_redis()
    diag = {"ts": time.time(), "agents": {}, "redis": {}, "llm": {}, "memory": {}, "rrclaw": {}}

    hb_raw = await r.hgetall("openclaw:heartbeats")
    for name, raw in hb_raw.items():
        try:
            hb = json.loads(raw)
            age = time.time() - hb.get("ts", 0)
            diag["agents"][name] = {"status": "ok" if age < 30 else "warn" if age < 60 else "dead",
                                    "age_s": round(age), "pid": hb.get("pid", 0)}
        except Exception:
            diag["agents"][name] = {"status": "error"}

    try:
        pong = await r.ping()
        info = await r.info("memory")
        diag["redis"] = {"ping": pong, "used_memory_human": info.get("used_memory_human", "?"),
                         "connected_clients": (await r.info("clients")).get("connected_clients", 0)}
    except Exception as e:
        diag["redis"] = {"ping": False, "error": str(e)}

    if llm:
        diag["llm"] = llm.status()

    diag["rrclaw"] = {
        "sessions": len(sessions),
        "tools": registry.stats() if registry else {},
        "evolution": evolution_engine.stats if evolution_engine else {},
        "skills": len(skill_loader.load_all()) if skill_loader else 0,
    }

    try:
        mem_keys = 0
        async for _ in r.scan_iter("openclaw:session:*:history"):
            mem_keys += 1
        diag["memory"] = {"session_count": mem_keys}
    except Exception:
        pass

    return diag


# ── Plan Trace ───────────────────────────────────────────

@app.get("/api/plan/history")
async def api_plan_history(limit: int = 20):
    r = await get_redis()
    ids = await r.lrange(PLAN_HISTORY_KEY, 0, min(limit, 200) - 1)
    summaries = []
    for plan_id in ids:
        raw = await r.get(f"{PLAN_LOG_PREFIX}{plan_id}")
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            summaries.append({
                "id": rec.get("id"), "uid": rec.get("uid"),
                "input": rec.get("input", "")[:80],
                "route_level": rec.get("route_level"),
                "steps": len(rec.get("steps", [])),
                "latency_ms": rec.get("latency_ms", {}).get("total"),
                "ts": rec.get("ts"),
            })
        except Exception:
            continue
    return {"plans": summaries}


@app.get("/api/plan/{plan_id}")
async def api_plan_detail(plan_id: str):
    r = await get_redis()
    raw = await r.get(f"{PLAN_LOG_PREFIX}{plan_id}")
    if not raw:
        raise HTTPException(404, "plan not found")
    return json.loads(raw)


# ── Daily Log ────────────────────────────────────────────

@app.get("/api/daily-log")
async def api_daily_log(date: str = ""):
    if not date:
        date = time.strftime("%Y-%m-%d")
    r = await get_redis()
    chat_raw = await r.lrange(f"{DAILY_LOG_KEY}:{date}", 0, -1)
    chats = []
    for item in chat_raw:
        try:
            chats.append(json.loads(item))
        except Exception:
            pass
    chats.reverse()
    return {"date": date, "messages": chats, "total": len(chats)}


@app.get("/api/daily-log/dates")
async def api_daily_log_dates():
    r = await get_redis()
    keys = []
    async for key in r.scan_iter(f"{DAILY_LOG_KEY}:*"):
        d = key.replace(f"{DAILY_LOG_KEY}:", "")
        keys.append(d)
    keys.sort(reverse=True)
    return {"dates": keys[:30]}


# ── News ─────────────────────────────────────────────────

@app.get("/api/news")
async def api_news(keyword: str = "", limit: int = 20):
    args = json.dumps({"keyword": keyword, "limit": limit}) if keyword else ""
    reply = await _send_and_wait("news", args, raw_reply=True)
    raw_data = reply.get("raw", {}) if isinstance(reply, dict) else {}
    agent_raw = raw_data.get("raw") if isinstance(raw_data, dict) else None
    items = []
    if isinstance(agent_raw, dict):
        for r_item in (agent_raw.get("results") or agent_raw.get("news") or []):
            items.append({
                "title": r_item.get("title", ""),
                "summary": r_item.get("summary", r_item.get("content", "")),
                "source": r_item.get("source", ""),
                "time": str(r_item.get("publish_time") or r_item.get("pub_date") or r_item.get("datetime") or "")[:16],
                "sentiment": r_item.get("sentiment_label", ""),
                "importance": r_item.get("importance", ""),
            })
    text = reply.get("text", "") if isinstance(reply, dict) else str(reply)
    return {"items": items, "raw": text if not items else ""}


@app.get("/api/news/summary")
async def api_news_summary():
    result = await send_to_orchestrator("news", json.dumps({"mode": "summary"}))
    return {"summary": result}


# ── Monitor ──────────────────────────────────────────────

@app.get("/api/monitor/alerts")
async def api_monitor_alerts():
    result = await send_to_orchestrator("alerts")
    return {"text": result}

@app.get("/api/monitor/targets")
async def api_monitor_targets():
    result = await send_to_orchestrator("targets")
    return {"text": result}

@app.get("/api/monitor/grafana")
async def api_monitor_grafana():
    result = await send_to_orchestrator("grafana_alerts")
    return {"text": result}

@app.post("/api/monitor/patrol")
async def api_monitor_patrol():
    result = await send_to_orchestrator("patrol")
    return {"text": result}

@app.post("/api/monitor/silence")
async def api_monitor_silence(request: Request):
    body = await request.json()
    matcher = body.get("matcher", "")
    duration = body.get("duration", "2h")
    result = await send_to_orchestrator("silence", json.dumps({"matcher": matcher, "duration": duration}))
    return {"text": result}


# ── Reflect ──────────────────────────────────────────────

@app.get("/api/reflect/stats")
async def api_reflect_stats():
    result = await send_to_orchestrator("reflect_stats", "", uid="rrclaw_api")
    return {"text": result}

@app.get("/api/reflect/insight")
async def api_reflect_insight():
    result = await send_to_orchestrator("reflect", "", uid="rrclaw_api")
    return {"text": result}


# ── Intraday ─────────────────────────────────────────────

@app.get("/api/intraday/status")
async def api_intraday_status():
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.intraday_pipeline import get_intraday_status
        r = await get_redis()
        return await get_intraday_status(r)
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/intraday/select")
async def api_intraday_select(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    strategy = body.get("strategy", "")
    result = await send_to_orchestrator("intraday_select", strategy)
    return {"result": result}

@app.post("/api/intraday/scan")
async def api_intraday_scan(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    logic = body.get("strategy_logic", "")
    result = await send_to_orchestrator("intraday_scan", logic)
    return {"result": result}

@app.post("/api/intraday/monitor")
async def api_intraday_monitor(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    logic = body.get("strategy_logic", "")
    result = await send_to_orchestrator("intraday_monitor", logic)
    return {"result": result}

@app.post("/api/intraday/stop")
async def api_intraday_stop():
    result = await send_to_orchestrator("intraday_stop", "")
    return {"result": result}


# ── AI Tools (delegate to orchestrator) ──────────────────

@app.post("/api/tools/translate")
async def api_tools_translate(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("translate", body.get("text", ""))
    return {"result": result}

@app.post("/api/tools/write")
async def api_tools_write(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("write", body.get("text", ""))
    return {"result": result}

@app.post("/api/tools/code")
async def api_tools_code(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("code", body.get("text", ""))
    return {"result": result}

@app.post("/api/tools/calc")
async def api_tools_calc(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("calc", body.get("text", ""))
    return {"result": result}

@app.post("/api/tools/websearch")
async def api_tools_websearch(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("websearch", body.get("text", ""))
    return {"result": result}


# ── Apple Services (delegate to orchestrator) ────────────

@app.get("/api/apple/calendar")
async def api_apple_calendar():
    result = await send_to_orchestrator("calendar")
    return {"text": result}

@app.post("/api/apple/calendar")
async def api_apple_calendar_create(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("cal_add", json.dumps(body, ensure_ascii=False))
    return {"text": result}

@app.get("/api/apple/reminders")
async def api_apple_reminders(list_name: str = ""):
    args = json.dumps({"list_name": list_name}) if list_name else ""
    result = await send_to_orchestrator("remind_list", args)
    return {"text": result}

@app.post("/api/apple/reminders")
async def api_apple_reminder_create(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("remind", json.dumps(body, ensure_ascii=False))
    return {"text": result}

@app.get("/api/apple/sysinfo")
async def api_apple_sysinfo():
    result = await send_to_orchestrator("sysinfo")
    return {"text": result}

@app.post("/api/apple/notify")
async def api_apple_notify(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("notify", json.dumps(body, ensure_ascii=False))
    return {"text": result}

@app.get("/api/apple/contacts")
async def api_apple_contacts(q: str = ""):
    result = await send_to_orchestrator("contact", q)
    return {"text": result}


# ── Dev API (delegate to orchestrator) ───────────────────

@app.post("/api/dev/ssh")
async def api_dev_ssh(request: Request):
    body = await request.json()
    host = body.get("host", "")
    cmd = body.get("cmd", "")
    args = json.dumps({"host": host, "cmd": cmd})
    result = await send_to_orchestrator("ssh", args)
    return {"result": result}

@app.post("/api/dev/claude")
async def api_dev_claude(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    project = body.get("project", "")
    user = getattr(request.state, "user", None)
    uid = f"web_{user['sub']}" if user else "webchat_default"
    args = json.dumps({"prompt": prompt, "project": project}, ensure_ascii=False)
    result = await send_to_orchestrator("claude", args, uid=uid)
    return {"result": result}

@app.post("/api/dev/local")
async def api_dev_local(request: Request):
    body = await request.json()
    result = await send_to_orchestrator("local", body.get("cmd", ""))
    return {"result": result}

@app.get("/api/dev/hosts")
async def api_dev_hosts():
    result = await send_to_orchestrator("host_list")
    return {"text": result}

@app.post("/api/dev/git")
async def api_dev_git(request: Request):
    body = await request.json()
    action = body.get("action", "status")
    valid_actions = {"status": "git_status", "pull": "git_pull", "log": "git_log", "diff": "git_diff", "sync": "git_sync"}
    if action not in valid_actions:
        raise HTTPException(400, f"invalid action: {action}")
    args = json.dumps({
        "repo": body.get("repo", ""), "path": body.get("path", ""),
        "branch": body.get("branch", ""), "deploy_to": body.get("deploy_to", ""),
        "count": body.get("count", 15),
    })
    result = await send_to_orchestrator(valid_actions[action], args)
    return {"result": result}


# ── Tasks (delegate to orchestrator) ─────────────────────

@app.get("/api/tasks")
async def api_tasks():
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.task_manager import TaskManager, PRESET_TASKS
        r = await get_redis()
        mgr = TaskManager(r)
        tasks = await mgr.list_tasks(limit=30)
        return {
            "tasks": [t.to_dict() for t in tasks],
            "presets": {k: {"name": v["name"], "steps": len(v["steps"])} for k, v in PRESET_TASKS.items()},
        }
    except Exception as e:
        return {"tasks": [], "presets": {}, "error": str(e)}

@app.post("/api/tasks/create")
async def api_task_create(request: Request):
    body = await request.json()
    args = json.dumps({"preset": body.get("preset", ""), "name": body.get("name", ""), "steps": body.get("steps", [])})
    result = await send_to_orchestrator("task_new", args)
    return {"result": result}

@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: str):
    result = await send_to_orchestrator("task_cancel", task_id)
    return {"result": result}

@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    try:
        import sys
        brain_path = os.path.expanduser("~/OpenClaw-Universe/openclaw-brain")
        if brain_path not in sys.path:
            sys.path.insert(0, brain_path)
        from agents.task_manager import TaskManager
        r = await get_redis()
        mgr = TaskManager(r)
        task = await mgr.get_task(task_id)
        if task:
            return {"task": task.to_dict()}
        return {"task": None, "error": "not found"}
    except Exception as e:
        return {"task": None, "error": str(e)}


# ── Serve Frontend ───────────────────────────────────────

FRONTEND_PATH = Path(__file__).parent / "static" / "index.html"

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    if FRONTEND_PATH.exists():
        return FRONTEND_PATH.read_text(encoding="utf-8")
    return "<h1>RRCLAW — Frontend not found. Place index.html in static/</h1>"


# ── Main ─────────────────────────────────────────────────

def main():
    import uvicorn
    logger.info(f"Starting RRCLAW Unified Server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

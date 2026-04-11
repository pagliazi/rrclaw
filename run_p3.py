"""
P3 运行 — Self-Learning (自学习)

Upgrades from P2:
- BackgroundReviewSystem wired into ConversationRuntime
- EvolutionEngine started as background async task
- SkillLoader + SkillExecutor for skill matching
- Execution event publishing after each turn
- Memory tiers (session/user/system) initialized
- Hermes runtime registered as Tier 1 tool (hermes_delegate)
"""
import asyncio
import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rrclaw.p3")

# Suppress noisy loggers
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from rrclaw.runtime.conversation import ConversationRuntime, TurnConfig, EventType
from rrclaw.runtime.providers.router import ProviderRouter, ProviderConfig
from rrclaw.runtime.resilience.error_classifier import RRClawErrorClassifier
from rrclaw.runtime.session import Session
from rrclaw.runtime.config import RRClawConfig
from rrclaw.tools.registry import GlobalToolRegistry
from rrclaw.tools.executor import ToolExecutor
from rrclaw.tools.pyagent.bridge import PyAgentBridge
from rrclaw.tools.index_builder import build_tool_registry
from rrclaw.context.engine import ContextEngine
from rrclaw.runtime.prompt import PromptBuilder
from rrclaw.channels.gateway import GatewayChannel

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
from rrclaw.workers.boot import RedisWorker, PyAgentWorker, HermesWorker, GatewayWorker
from rrclaw.workers.coordinator import WorkerCoordinator
from rrclaw.commands.evolve import EvolveCommand
from rrclaw.commands.research import ResearchCommand

# Config
GATEWAY_URL = os.getenv("GATEWAY_URL", "ws://127.0.0.1:18789")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
SKILLS_DIR = os.getenv(
    "OPENCLAW_SKILLS_DIR",
    os.path.expanduser("~/OpenClaw-Universe/openclaw-brain/agents/skills"),
)

# State
sessions: dict[str, Session] = {}
pyagent_bridge: PyAgentBridge = None
registry: GlobalToolRegistry = None
executor: ToolExecutor = None
llm: ProviderRouter = None
error_classifier: RRClawErrorClassifier = None
gateway: GatewayChannel = None
config: RRClawConfig = None
context_engine: ContextEngine = None

# P3 state
hermes_runtime: HermesNativeRuntime = None
background_review_system: BackgroundReviewSystem = None
evolution_engine: EvolutionEngine = None
skill_loader: SkillLoader = None
skill_executor: SkillExecutor = None
session_memory: SessionMemory = None
user_memory: UserMemory = None
system_memory: SystemMemory = None

# P4 state
gepa_pipeline: GEPAPipeline = None
research_loop: StrategyResearchLoop = None
worker_coordinator: WorkerCoordinator = None
evolve_command: EvolveCommand = None
research_command: ResearchCommand = None


def build_provider_router() -> ProviderRouter:
    """Build ProviderRouter with fallback chain from env vars."""
    configs = []

    # Primary: DashScope (qwen3.5-plus)
    primary_key = os.getenv("OPENAI_API_KEY", "")
    primary_url = os.getenv("OPENAI_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1")
    primary_model = os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus")

    if primary_key:
        configs.append(ProviderConfig(
            name="dashscope-primary",
            api_key=primary_key,
            base_url=primary_url,
            model=primary_model,
        ))

    # Fallback 1: DeepSeek via SiliconFlow
    fallback_key = os.getenv("SILICONFLOW_API_KEY", "")
    if fallback_key:
        configs.append(ProviderConfig(
            name="siliconflow-deepseek",
            api_key=fallback_key,
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V3",
        ))

    # Fallback 2: DeepSeek direct
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        configs.append(ProviderConfig(
            name="deepseek-direct",
            api_key=deepseek_key,
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        ))

    if len(configs) == 0:
        configs.append(ProviderConfig(
            name="dashscope-default",
            api_key="sk-sp-0dd17ca1a5ed4a108b13d7942216e107",
            base_url="https://coding.dashscope.aliyuncs.com/v1",
            model="qwen3.5-plus",
        ))

    return ProviderRouter(configs)


async def handle_user_message(session_id: str, text: str):
    """Handle incoming message from Gateway or stdin."""
    if not text.strip():
        return

    logger.info(f"User [{session_id[:8]}]: {text[:80]}")

    # --- P4: Slash command routing ---
    stripped = text.strip()
    if stripped.startswith("/evolve") and evolve_command:
        args = stripped[len("/evolve"):].strip()
        result = await evolve_command.execute(args)
        print(f"\n{result}\n")
        if gateway and gateway.is_connected:
            await gateway.send_text_complete(session_id, result)
        return
    if stripped.startswith("/research") and research_command:
        args = stripped[len("/research"):].strip()
        result = await research_command.execute(args)
        print(f"\n{result}\n")
        if gateway and gateway.is_connected:
            await gateway.send_text_complete(session_id, result)
        return

    # Get or create session
    if session_id not in sessions:
        sessions[session_id] = Session(session_id=session_id)
    session = sessions[session_id]

    # --- P3: Skill matching before runtime ---
    if skill_executor:
        matched_skill = skill_executor.match_skill(text)
        if matched_skill:
            logger.info(f"Skill matched: {matched_skill}")
            skill_instruction = skill_executor.prepare_skill(
                matched_skill, session_id=session_id
            )
            if skill_instruction:
                # Inject skill instruction as system message into session
                session.append_system(skill_instruction)

    # Create runtime for this turn
    runtime = ConversationRuntime(
        session=session,
        registry=registry,
        executor=executor,
        llm_provider=llm,
        context_provider=context_engine,
        error_classifier=error_classifier,
        config=TurnConfig(max_tool_rounds=10),
    )

    # --- P3: Wire background review ---
    if background_review_system:
        runtime.background_review = background_review_system
        background_review_system.increment_turn()

    # Run the turn and stream results back
    full_text = ""
    tool_calls_in_turn = []
    turn_start = time.time()

    try:
        async for event in runtime.run_turn(text):
            if event.type == EventType.TEXT_DELTA:
                full_text += event.data
                if gateway and gateway.is_connected:
                    await gateway.send_text_delta(session_id, event.data)

            elif event.type == EventType.TOOL_START:
                logger.info(f"  Tool: {event.data.name}")
                tool_calls_in_turn.append({
                    "tool": event.data.name,
                    "start": time.time(),
                })
                if gateway and gateway.is_connected:
                    await gateway.send_tool_status(
                        session_id, event.data.name, "running"
                    )

            elif event.type == EventType.TOOL_RESULT:
                r = event.data["result"]
                status = "OK" if not r.is_error else "ERROR"
                preview = r.content[:100] if r.content else ""
                logger.info(f"  Result: {status} -- {preview}")

                # Track for evolution engine
                if tool_calls_in_turn:
                    tc = tool_calls_in_turn[-1]
                    tc["success"] = not r.is_error
                    tc["latency_ms"] = (time.time() - tc["start"]) * 1000
                    tc["result_summary"] = preview

                # Increment skill counter for background review
                if background_review_system:
                    background_review_system.increment_iterations(1)

            elif event.type == EventType.USAGE:
                logger.info(
                    f"  Tokens: in={event.data.input_tokens} out={event.data.output_tokens}"
                )

            elif event.type == EventType.ERROR:
                logger.error(f"  Error: {event.data}")
                if gateway and gateway.is_connected:
                    await gateway.send_text_complete(
                        session_id, f"错误: {event.data}"
                    )
                return

        # Send complete signal
        if gateway and gateway.is_connected:
            await gateway.send_stream_end(session_id)

        if full_text:
            logger.info(f"Reply [{session_id[:8]}]: {full_text[:120]}...")

        # Log provider status
        router_status = llm.status()
        logger.info(f"  Provider: {router_status['current']}")

        # --- P3: Publish execution events to Redis Stream for Evolution Engine ---
        if evolution_engine and tool_calls_in_turn:
            for tc in tool_calls_in_turn:
                try:
                    await evolution_engine.record_execution(
                        tool_name=tc.get("tool", ""),
                        action="call",
                        params={},
                        result_summary=tc.get("result_summary", ""),
                        success=tc.get("success", True),
                        latency_ms=tc.get("latency_ms", 0),
                        session_id=session_id,
                    )
                except Exception as e:
                    logger.debug(f"Failed to record execution event: {e}")

        # --- P3: Complete skill if one was active ---
        if skill_executor and skill_executor.get_active_skill(session_id):
            skill_executor.complete_skill(session_id, success=True)

        # --- P3: Store discoveries in session memory ---
        if session_memory and full_text:
            # Store last response topic as session context
            session_memory.set(
                "last_topic",
                text[:200],
                category="context",
                source="conversation",
            )

    except Exception as e:
        logger.error(f"Turn error: {e}", exc_info=True)
        if gateway and gateway.is_connected:
            await gateway.send_text_complete(session_id, f"内部错误: {e}")


async def main():
    global pyagent_bridge, registry, executor, llm, error_classifier
    global gateway, config, context_engine
    global hermes_runtime, background_review_system, evolution_engine
    global skill_loader, skill_executor
    global session_memory, user_memory, system_memory
    global gepa_pipeline, research_loop, worker_coordinator
    global evolve_command, research_command

    logger.info("=== RRCLAW P3 启动 (Self-Learning) ===")

    # 0. Load config
    config = RRClawConfig.from_file()

    # 1. PyAgent bridge (with auto-reconnect)
    pyagent_bridge = PyAgentBridge(redis_url=REDIS_URL)
    try:
        await pyagent_bridge.connect()
        logger.info("PyAgent Redis connected")
    except Exception as e:
        logger.warning(f"PyAgent Redis connection failed: {e}")
        logger.info("PyAgent tools will be unavailable until Redis reconnects")

    # 2. Hermes runtime (P3)
    hermes_runtime = HermesNativeRuntime(
        hermes_path="/tmp/full-deploy-test/hermes-venv",
        model=os.getenv("RRCLAW_DEFAULT_MODEL", "qwen3.5-plus"),
    )
    if hermes_runtime.available:
        logger.info("Hermes runtime loaded")
    else:
        logger.warning("Hermes runtime not available (will run without delegation)")

    # 3. Build tool registry (with hermes_delegate)
    registry = build_tool_registry(
        bridge=pyagent_bridge,
        skills_dir=SKILLS_DIR,
        hermes_runtime=hermes_runtime if hermes_runtime.available else None,
    )
    stats = registry.stats()
    logger.info(
        f"Tool registry: {stats['tier0']} tier0, "
        f"{stats['tier1_indexed']} tier1 indexed"
    )

    # 4. LLM provider
    llm = build_provider_router()
    router_status = llm.status()
    logger.info(
        f"Provider router ready: {router_status['current']} "
        f"({len(router_status['providers'])} providers in chain)"
    )
    for p in router_status["providers"]:
        logger.info(f"  - {p['name']} (model: {p['model']})")

    # 5. Error classifier
    error_classifier = RRClawErrorClassifier()
    logger.info("Error classifier ready")

    # 6. Tool executor
    executor = ToolExecutor(registry)

    # 7. Memory tiers (P3)
    session_memory = SessionMemory(session_id="default")
    user_memory = UserMemory()
    system_memory = SystemMemory()
    logger.info(
        f"Memory tiers: session={session_memory.size}, "
        f"user={user_memory.stats['total_entries']}, "
        f"system={system_memory.stats['total_entries']}"
    )

    # 8. Context engine (5-layer compression + memory injection)
    context_engine = ContextEngine(config, registry)
    context_engine.session_memory = session_memory
    context_engine.user_memory = user_memory
    context_engine.system_memory = system_memory

    # Verify system prompt
    builder = PromptBuilder(registry, config)
    system_prompt = builder.build_system_prompt()
    prompt_tokens = len(system_prompt) // 3
    logger.info(f"System prompt: {len(system_prompt)} chars, ~{prompt_tokens} tokens")
    if prompt_tokens > 8000:
        logger.warning(f"System prompt exceeds 8K token target ({prompt_tokens})")

    # 9. Background Review System (P3)
    background_review_system = BackgroundReviewSystem(
        hermes_runtime=hermes_runtime if hermes_runtime.available else None,
    )
    logger.info(
        f"Background review: available={background_review_system.available}, "
        f"stats={background_review_system.stats}"
    )

    # 10. Skill Loader + Executor (P3)
    skill_loader = SkillLoader()
    skills = skill_loader.load_all()
    skill_executor = SkillExecutor(skill_loader, registry)
    logger.info(f"Skills loaded: {len(skills)}")

    # 11. Evolution Engine (P3) — background task
    evolution_engine = EvolutionEngine(redis_url=REDIS_URL)
    try:
        await evolution_engine.start()
        logger.info(f"Evolution engine started: {evolution_engine.stats}")
    except Exception as e:
        logger.warning(f"Evolution engine start failed: {e}")

    # --- P4: GEPA Pipeline ---
    gepa_pipeline = GEPAPipeline(
        hermes_runtime=hermes_runtime if hermes_runtime and hermes_runtime.available else None,
    )
    logger.info("GEPA pipeline initialized")

    # --- P4: Autoresearch Loop ---
    research_loop = StrategyResearchLoop(
        hermes_runtime=hermes_runtime if hermes_runtime and hermes_runtime.available else None,
        pyagent_bridge=pyagent_bridge,
    )
    logger.info("Autoresearch loop initialized")

    # --- P4: Slash commands ---
    evolve_command = EvolveCommand(
        evolution_engine=evolution_engine,
        gepa_pipeline=gepa_pipeline,
        system_memory=system_memory,
    )
    research_command = ResearchCommand(research_loop=research_loop)
    logger.info("Commands registered: /evolve, /research")

    # --- P4: Worker Boot ---
    worker_coordinator = WorkerCoordinator()
    redis_worker = RedisWorker(redis_url=REDIS_URL)
    pyagent_worker = PyAgentWorker(redis_url=REDIS_URL)
    hermes_worker = HermesWorker(
        hermes_path="/tmp/full-deploy-test/hermes-venv",
    )
    hermes_worker.set_runtime(hermes_runtime)
    gateway_worker = GatewayWorker(gateway_url=GATEWAY_URL)

    worker_coordinator.register(redis_worker)
    worker_coordinator.register(pyagent_worker)
    worker_coordinator.register(hermes_worker)
    worker_coordinator.register(gateway_worker)

    try:
        boot_ok = await worker_coordinator.boot_all()
        if boot_ok:
            await worker_coordinator.start_all()
            logger.info(f"Worker coordinator: all workers started (state={worker_coordinator.status.state})")
        else:
            logger.warning(f"Worker coordinator: boot incomplete (state={worker_coordinator.status.state})")
            # Log individual worker states
            for wname, wstatus in worker_coordinator.status.workers.items():
                logger.info(f"  Worker [{wname}]: {wstatus.state.value} (error: {wstatus.error or 'none'})")
    except Exception as e:
        logger.warning(f"Worker coordinator boot failed: {e}")

    # 12. Gateway channel
    gateway = GatewayChannel(
        gateway_url=GATEWAY_URL,
        auth_token=GATEWAY_TOKEN,
        on_user_message=handle_user_message,
    )

    if GATEWAY_TOKEN:
        try:
            await gateway.connect()
            logger.info("Gateway connected")
            asyncio.create_task(gateway.listen())
        except Exception as e:
            logger.warning(f"Gateway connection failed: {e}")
            logger.info("Running in standalone mode (no IM channels)")
    else:
        logger.info("No GATEWAY_TOKEN -- running standalone (stdin mode)")

    # 13. Stdin mode (for testing)
    logger.info("\n=== RRCLAW P3 Ready. Type messages below (Ctrl+C to quit) ===\n")

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, input, "You> ")
                if line.strip():
                    await handle_user_message("stdin-session", line.strip())
                    print()
            except EOFError:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # Shutdown
    logger.info("Shutting down...")
    if worker_coordinator:
        await worker_coordinator.shutdown_all()
    if evolution_engine:
        await evolution_engine.stop()
    if hermes_runtime:
        hermes_runtime.shutdown()
    if gateway:
        await gateway.close()
    await pyagent_bridge.close()


if __name__ == "__main__":
    asyncio.run(main())

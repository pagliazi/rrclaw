"""
Orchestrator — 统筹 Agent (thin shell)
职责: 命令路由（精确命令 + LLM 意图识别柔性路由）、规则引擎、跨Agent编排、结果聚合
      Redis 行为画像、全局图谱拓扑健康监控、记忆降级告警
      跨Agent记忆提醒引擎、数据源健康监控
      SOUL 身份守护、LLM 智能路由、主动综合简报

业务逻辑委托给:
  - command_registry   (命令注册表/路由/超时)
  - intent_router      (L0/L1/L2 分层意图路由)
  - system_commands    (系统诊断/管理命令)
  - channel_manager    (渠道通信/进度推送)
  - session_manager    (会话历史/记忆上下文/行为画像)
  - response_formatter (响应格式化)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import yaml

from agents.base import BaseAgent, AgentMessage, run_agent
from agents.command_registry import command_registry, ACTION_TIMEOUTS
from agents.intent_router import IntentRouter
from agents.system_commands import SystemCommandHandler
from agents.channel_manager import ChannelManager
from agents.session_manager import (
    SessionManager, resolve_canonical_uid,
    SESSION_HISTORY_KEY, BEHAVIOR_CMD_FREQ_KEY, BEHAVIOR_TIME_KEY,
    BEHAVIOR_RECENT_KEY, DEGRADATION_LOG_KEY,
)
from agents.response_formatter import result_to_text, dict_to_readable, humanize, needs_polish

logger = logging.getLogger("agent.orchestrator")

RULES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules.yaml")


# ── 规则引擎函数 (module-level) ────────────────────────────


def load_rules() -> list[dict]:
    try:
        with open(RULES_PATH) as f:
            data = yaml.safe_load(f)
        return [r for r in data.get("rules", []) if r.get("enabled", True)]
    except Exception as e:
        logger.error(f"Failed to load rules: {e}")
        return []


def parse_schedule(schedule_str: str) -> dict:
    parts = schedule_str.split()
    if len(parts) != 5:
        return {}
    return {
        "minute": parts[0],
        "hour": parts[1],
        "dom": parts[2],
        "month": parts[3],
        "dow": parts[4],
    }


def _match_field(spec: str, value: int) -> bool:
    """匹配 cron 字段: *, N, */N, N-M"""
    if spec == "*":
        return True
    if "/" in spec:
        interval = int(spec.split("/")[1])
        return value % interval == 0
    if "-" in spec:
        lo, hi = map(int, spec.split("-"))
        return lo <= value <= hi
    return value == int(spec)


def _match_hour_range(spec: str, now: datetime) -> bool:
    """匹配 hour 字段，支持 H:MM-H:MM 带分钟的范围（如 9:30-15:00）"""
    if ":" in spec:
        parts = spec.replace(":", ".").split("-")
        start_f, end_f = float(parts[0]), float(parts[1])
        current = now.hour + now.minute / 60.0
        return start_f <= current <= end_f
    return _match_field(spec, now.hour)


def match_schedule(schedule: dict, now: datetime) -> bool:
    if not _match_field(schedule.get("minute", "*"), now.minute):
        return False
    if not _match_hour_range(schedule.get("hour", "*"), now):
        return False
    if not _match_field(schedule.get("dow", "*"), now.isoweekday()):
        return False
    return True


# ── Orchestrator class ────────────────────────────────────


class Orchestrator(BaseAgent):
    name = "orchestrator"

    def __init__(self):
        super().__init__()
        self.rules = load_rules()
        self._notify_callbacks: dict[str, list] = {}
        self._last_limitup_count: int | None = None
        self._notify_router = None
        self._task_manager = None
        self._reflection_engine = None

        # ── Delegated module instances ──
        self._sys_cmd_handler = SystemCommandHandler(self)
        self._intent_router = IntentRouter(self)
        self._session_mgr = SessionManager(self.get_redis)
        self._channel_mgr = ChannelManager(self.get_redis, self._get_notify_router)

    # ── Lazy getters ──

    def _get_reflection_engine(self):
        if self._reflection_engine is None:
            try:
                from agents.memory.reflection_engine import get_reflection_engine
                self._reflection_engine = get_reflection_engine()
            except Exception as e:
                logger.debug("ReflectionEngine init failed (non-fatal): %s", e)
        return self._reflection_engine

    async def _get_task_manager(self):
        if self._task_manager is None:
            from agents.task_manager import TaskManager
            r = await self.get_redis()
            self._task_manager = TaskManager(r)
        return self._task_manager

    async def _get_notify_router(self):
        if self._notify_router is None:
            from agents.notify_router import get_notify_router
            r = await self.get_redis()
            self._notify_router = await get_notify_router(r)
        return self._notify_router

    def _get_policy_engine(self):
        if not hasattr(self, "_policy_engine"):
            try:
                from agents.policy import PolicyEngine
                policy_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policies.yaml")
                self._policy_engine = PolicyEngine.from_yaml(policy_path)
            except Exception as e:
                logger.warning(f"PolicyEngine load failed: {e}")
                self._policy_engine = None
        return self._policy_engine

    def _get_adaptive_tuner(self):
        if not hasattr(self, "_adaptive_tuner"):
            try:
                from agents.adaptive import AdaptiveTuner
                self._adaptive_tuner = AdaptiveTuner()
            except Exception as e:
                logger.warning(f"AdaptiveTuner load failed: {e}")
                self._adaptive_tuner = None
        return self._adaptive_tuner

    # ── Backward-compat thin wrappers (used by IntentRouter/SystemCommandHandler) ──

    async def _notify(self, text: str, topic: str = "market", priority: str = "normal"):
        from agents.notify_router import Priority
        router = await self._get_notify_router()
        prio = Priority(priority)
        await router.broadcast(text, topic=topic, priority=prio, source="orchestrator")

    async def _session_history_get(self, uid: str, max_rounds: int = 6) -> list[dict]:
        return await self._session_mgr.history_get(uid, max_rounds)

    async def _session_history_append(self, uid: str, role: str, content: str):
        await self._session_mgr.history_append(uid, role, content)

    async def _build_memory_context(self, uid: str, user_input: str) -> str:
        return await self._session_mgr.build_memory_context(uid, user_input)

    async def _persist_to_memory(self, uid: str, user_input: str, reply_text: str):
        await self._session_mgr.persist_to_memory("orchestrator", uid, user_input, reply_text)

    async def _record_behavior(self, uid: str, cmd: str):
        await self._session_mgr.record_behavior(uid, cmd)

    def _result_to_text(self, result, source: str = "", action: str = "") -> str:
        return result_to_text(result, source, action)

    async def _send_with_progress(self, target: str, action: str, params: dict,
                                   timeout: float = 0, reply_channel: str = "", msg_id: str = ""):
        from agents.base import AgentMessage, MSG_TIMEOUT
        out_msg = AgentMessage.create(self.name, target, action, params=params or {})
        if reply_channel:
            async def _on_progress(text):
                await self._progress_to_channel(reply_channel, msg_id or out_msg.id, text, source=target)
            self._progress_callbacks[out_msg.id] = _on_progress
        try:
            resp = await self.request(out_msg, timeout=timeout or MSG_TIMEOUT)
        finally:
            self._progress_callbacks.pop(out_msg.id, None)
        return resp

    async def _progress_to_channel(self, reply_channel: str, msg_id: str, text: str, source: str = "manager"):
        if not reply_channel:
            return
        try:
            r = await self.get_redis()
            payload = {"type": "progress", "text": text, "in_reply_to": msg_id,
                       "timestamp": time.time(), "source": source}
            await r.publish(reply_channel, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            pass

    async def _reply_to_channel(self, reply_channel: str, msg_id: str, text: str, raw=None, source: str = "manager"):
        if not reply_channel:
            return
        r = await self.get_redis()
        payload = {"type": "done", "text": text, "in_reply_to": msg_id, "timestamp": time.time(), "source": source}
        if raw is not None:
            payload["raw"] = raw
        await r.publish(reply_channel, json.dumps(payload, ensure_ascii=False, default=str))

    # ── handle() action dispatch ──

    async def handle(self, msg: AgentMessage):
        action = msg.action

        if action == "route":
            await self._route_command(msg)

        elif action == "notify":
            text = msg.params.get("text", "")
            topic = msg.params.get("topic", "market")
            priority = msg.params.get("priority", "normal")
            await self._notify(text, topic=topic, priority=priority)
            await self.reply(msg, result={"notified": "all_active"})

        elif action == "status":
            r = await self.get_redis()
            hb = await r.hgetall("openclaw:heartbeats")
            agents_status = {}
            now = time.time()
            for name, val in hb.items():
                try:
                    info = json.loads(val)
                    alive = (now - info.get("ts", 0)) < 30
                    agents_status[name] = {"alive": alive, "pid": info.get("pid"), "last_seen": info.get("ts")}
                except Exception:
                    agents_status[name] = {"alive": False}
            await self.reply(msg, result=agents_status)

        elif action == "reload_rules":
            self.rules = load_rules()
            await self.reply(msg, result={"rules_count": len(self.rules)})

        elif action == "memory_health":
            health = await self._get_memory_health()
            await self.reply(msg, result=health)

        elif action == "memory_backup":
            try:
                from agents.memory.lifecycle import daily_backup
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, daily_backup)
                await self.reply(msg, result={"backed_up": True})
            except Exception as e:
                logger.error(f"Memory backup failed: {e}")
                await self.reply(msg, error=str(e))

        elif action == "memory_compress":
            try:
                from agents.memory.lifecycle import compress_old_memories, fix_orphan_nodes
                results = {}
                for agent_name in ("analysis", "news", "dev"):
                    results[agent_name] = await compress_old_memories(agent_name)
                loop = asyncio.get_running_loop()
                fix_result = await loop.run_in_executor(None, fix_orphan_nodes)
                results["orphan_fix"] = fix_result
                await self.reply(msg, result=results)
            except Exception as e:
                logger.error(f"Memory compress failed: {e}")
                await self.reply(msg, error=str(e))

        elif action == "memory_remind":
            try:
                from agents.memory.reminder import MemoryReminder
                reminder = MemoryReminder()
                result = await reminder.scan_and_remind()
                await self.reply(msg, result=result)
            except Exception as e:
                logger.error(f"Memory remind failed: {e}")
                await self.reply(msg, error=str(e))

        elif action == "data_source_status":
            try:
                from agents.data_sources.source_router import get_router
                router = get_router()
                await self.reply(msg, result=router.get_status())
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "embed_status":
            try:
                from agents.memory.embedding import EmbeddingClient
                client = EmbeddingClient()
                await self.reply(msg, result=client.get_status())
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "embed_cache_prune":
            try:
                from agents.memory.embedding import EmbedCache
                from agents.memory.config import CLOUD_EMBEDDING_CONFIG
                cache = EmbedCache()
                cache.prune(max_entries=CLOUD_EMBEDDING_CONFIG.get("max_cache_entries", 10000))
                await self.reply(msg, result={"pruned": True})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "soul_check":
            try:
                from agents.memory.soul_guardian import SoulGuardian
                guardian = SoulGuardian()
                result = await guardian.check_integrity()
                await self.reply(msg, result=result)
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "soul_accept":
            try:
                from agents.memory.soul_guardian import SoulGuardian
                guardian = SoulGuardian()
                await guardian.accept_changes()
                await self.reply(msg, result={"accepted": True})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "llm_status":
            try:
                from agents.llm_router import get_llm_router
                router = get_llm_router()
                await self.reply(msg, result=router.get_status())
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "daily_briefing":
            try:
                result = await self._generate_daily_briefing()
                await self.reply(msg, result=result)
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "memory_hygiene":
            try:
                from agents.memory.lifecycle import memory_hygiene_report
                result = await memory_hygiene_report()
                await self.reply(msg, result=result)
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "memory_tier":
            try:
                from agents.memory.lifecycle import tier_all_agents
                result = await tier_all_agents()
                total = sum(r.get("changes", 0) for r in result.values())
                lines = [f"🧠 记忆分层完成 — {total} 条变更"]
                for ag, r in result.items():
                    s = r.get("stats", {})
                    lines.append(f"  {ag}: 🔥HOT={s.get('hot',0)} 🌡️WARM={s.get('warm',0)} ❄️COLD={s.get('cold',0)}")
                await self.reply(msg, result={"text": "\n".join(lines), "details": result})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "memory_tier_status":
            try:
                from agents.memory.lifecycle import get_tiering_summary
                result = get_tiering_summary()
                lines = [
                    f"🧠 记忆分层概况 (共{result.get('total', 0)}条)",
                    f"  🔥 HOT: {result.get('hot', 0)}  🌡️ WARM: {result.get('warm', 0)}  ❄️ COLD: {result.get('cold', 0)}  ❓ 未分层: {result.get('untiered', 0)}",
                ]
                for ag, s in result.get("by_agent", {}).items():
                    lines.append(f"  {ag}: H={s['hot']} W={s['warm']} C={s['cold']}")
                await self.reply(msg, result={"text": "\n".join(lines), "raw": result})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "learnings":
            try:
                engine = self._get_reflection_engine()
                if not engine:
                    await self.reply(msg, error="ReflectionEngine 不可用")
                    return
                query = msg.params.get("query", "")
                if query:
                    results = engine.search_learnings(query, limit=10)
                    if results:
                        lines = [f"📚 教训搜索 \"{query}\" — {len(results)} 条匹配"]
                        for r in results:
                            lines.append(f"  [{r['id']}] {r['summary']}")
                        await self.reply(msg, result={"text": "\n".join(lines), "entries": results})
                    else:
                        await self.reply(msg, result={"text": f"未找到与 \"{query}\" 相关的教训"})
                else:
                    stats = engine.get_learnings_stats()
                    lines = [
                        "📚 教训库统计",
                        f"  教训: {stats.get('LEARNINGS.md', 0)} 条",
                        f"  错误: {stats.get('ERRORS.md', 0)} 条",
                        f"  需求: {stats.get('FEATURE_REQUESTS.md', 0)} 条",
                    ]
                    await self.reply(msg, result={"text": "\n".join(lines), "stats": stats})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "log_learning":
            try:
                engine = self._get_reflection_engine()
                if not engine:
                    await self.reply(msg, error="ReflectionEngine 不可用")
                    return
                summary = msg.params.get("summary", "")
                if not summary:
                    await self.reply(msg, error="缺少教训内容")
                    return
                category = msg.params.get("category", "correction")
                lid = engine.log_learning(summary=summary, category=category, source="user_correction")
                await self.reply(msg, result={"text": f"✅ 已记录教训 {lid}: {summary[:80]}", "id": lid})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "task_create":
            await self._handle_task_create(msg)

        elif action == "task_status":
            await self._handle_task_status(msg)

        elif action == "task_list":
            await self._handle_task_list(msg)

        elif action == "task_cancel":
            await self._handle_task_cancel(msg)

        elif action == "channel_status":
            try:
                router = await self._get_notify_router()
                await router.refresh_channel_states()
                await self.reply(msg, result=router.get_status())
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "channel_flush":
            try:
                router = await self._get_notify_router()
                flushed = await router.flush_pending()
                await self.reply(msg, result={"flushed": flushed})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "reflect_insight":
            try:
                engine = self._get_reflection_engine()
                if engine:
                    result = engine.generate_daily_insight()
                    await self.reply(msg, result={"text": result})
                else:
                    await self.reply(msg, result={"text": "反思引擎未就绪"})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "reflect_weekly":
            try:
                engine = self._get_reflection_engine()
                if engine:
                    result = engine.generate_weekly_report()
                    engine.flush()
                    await self.reply(msg, result={"text": result})
                else:
                    await self.reply(msg, result={"text": "反思引擎未就绪"})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "reflect_stats":
            try:
                engine = self._get_reflection_engine()
                if engine:
                    stats = engine.get_stats_summary()
                    lines = [
                        "🔍 **反思引擎状态**",
                        f"  今日查询: {stats['today_queries']}  失败: {stats['today_failures']}",
                        f"  跟踪路由数: {stats['tracked_routes']}  历史天数: {stats['days_tracked']}",
                        f"  平均成功率: {stats['avg_success_rate']:.1%}",
                        f"  对话日志: {stats['conv_log_size']} 条",
                    ]
                    if stats["failure_prone_agents"]:
                        lines.append(f"  ⚠️ 不稳定 Agent: {', '.join(stats['failure_prone_agents'])}")
                    hints = engine.get_routing_hints(top_k=5)
                    if hints:
                        lines.append("\n" + hints)
                    await self.reply(msg, result={"text": "\n".join(lines)})
                else:
                    await self.reply(msg, result={"text": "反思引擎未就绪"})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "list_skills":
            try:
                manifest = self._sys_cmd_handler._build_skills_manifest()
                await self.reply(msg, result={"text": manifest})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "list_agents":
            try:
                info = await self._sys_cmd_handler._build_agents_info()
                await self.reply(msg, result={"text": info})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "factor_list":
            try:
                text = await self._sys_cmd_handler._build_factor_list()
                await self.reply(msg, result={"text": text})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action == "factor_detail":
            try:
                params = msg.params
                factor_id = params.get("factor_id", "")
                text = await self._sys_cmd_handler._build_factor_detail(factor_id)
                await self.reply(msg, result={"text": text})
            except Exception as e:
                await self.reply(msg, error=str(e))

        elif action.endswith(":response"):
            pass

        else:
            await self.reply(msg, error=f"Unknown orchestrator action: {action}")

    # ── _route_command ──

    async def _route_command(self, msg: AgentMessage):
        cmd = msg.params.get("command", "")
        args = msg.params.get("args", "")
        reply_channel = msg.params.get("reply_channel", "")
        uid = msg.params.get("uid", msg.sender)
        user_name = msg.params.get("user_name", "")

        if user_name and uid:
            try:
                r = await self.get_redis()
                await r.hset("openclaw:user_profiles", uid,
                             json.dumps({"name": user_name, "uid": uid}, ensure_ascii=False))
            except Exception:
                pass

        if args:
            from agents.memory.input_guard import guard_input
            guard = guard_input(args)
            if not guard.safe:
                err = f"输入安全检查未通过: {guard.reason}"
                await self._reply_to_channel(reply_channel, msg.id, f"❌ {err}")
                await self.reply(msg, error=err)
                return
            args = guard.sanitized

        await self._record_behavior(uid, cmd)

        # ── chat / ask / status shortcuts ──
        if cmd == "chat":
            await self._progress_to_channel(reply_channel, msg.id, "💬 正在理解你的意图...")
            await self._intent_router.intent_route(args, uid, reply_channel, msg.id, user_name=user_name)
            await self.reply(msg, result={"text": "intent_routed"})
            return

        if cmd == "ask":
            await self._progress_to_channel(reply_channel, msg.id, "🧠 正在分析问题...", source="analysis")
            result = await self._cross_agent_ask(args)
            await self._reply_to_channel(reply_channel, msg.id, result, source="analysis")
            await self.reply(msg, result={"text": result})
            return

        if cmd == "status":
            r = await self.get_redis()
            hb = await r.hgetall("openclaw:heartbeats")
            lines = ["📊 Agent 状态:"]
            now = time.time()
            for name, val in sorted(hb.items()):
                try:
                    info = json.loads(val)
                    alive = (now - info.get("ts", 0)) < 30
                    icon = "✅" if alive else "❌"
                    lines.append(f"  {icon} {name} (pid:{info.get('pid','?')})")
                except Exception:
                    lines.append(f"  ❓ {name}")
            text = "\n".join(lines)
            await self._reply_to_channel(reply_channel, msg.id, text)
            await self.reply(msg, result={"text": text})
            return

        # ── task management shortcuts ──
        if cmd in ("jobs", "task_list"):
            await self._handle_task_status(msg)
            return

        if cmd == "task_new":
            preset = args.strip() if args else ""
            msg.params["preset"] = preset
            await self._handle_task_create(msg)
            return

        if cmd == "task_cancel":
            msg.params["task_id"] = args.strip()
            await self._handle_task_cancel(msg)
            return

        if cmd == "task_status":
            msg.params["task_id"] = args.strip()
            await self._handle_task_status(msg)
            return

        # ── channel status ──
        if cmd == "channel":
            try:
                router = await self._get_notify_router()
                await router.refresh_channel_states()
                status = router.get_status()
                lines = ["📡 渠道状态:"]
                now_ts = time.time()
                for ch_name, ch_info in sorted(status["channels"].items()):
                    age = ch_info.get("age") or 999
                    icon = "✅" if ch_info["online"] else "❌"
                    if ch_info.get("degraded"):
                        icon = "⚠️"
                    lines.append(
                        f"  {icon} {ch_name}: "
                        f"{'在线' if ch_info['online'] else '离线'} "
                        f"(心跳 {age:.0f}s前, 失败 {ch_info.get('consecutive_failures', 0)}次)"
                    )
                lines.append(f"\n活跃渠道: {status['active']}")
                stats = status.get("stats", {})
                lines.append(f"统计: 已发 {stats.get('total_sent', 0)} | "
                             f"转移 {stats.get('failovers', 0)} | "
                             f"积压 {stats.get('pending_queued', 0)}")
                text = "\n".join(lines)
            except Exception as e:
                text = f"❌ 渠道状态获取失败: {e}"
            await self._reply_to_channel(reply_channel, msg.id, text)
            await self.reply(msg, result={"text": text})
            return

        # ── system commands (delegate to SystemCommandHandler) ──
        progress_channel = msg.params.get("progress_channel", "")
        system_result = await self._sys_cmd_handler.handle(cmd, args, progress_channel=progress_channel)
        if system_result is not None:
            text = self._result_to_text(system_result, source="system", action=cmd)
            raw = system_result if isinstance(system_result, dict) else None
            await self._reply_to_channel(reply_channel, msg.id, text, raw=raw)
            await self.reply(msg, result={"text": text} if raw is None else system_result)
            return

        # ── command registry lookup ──
        resolved = command_registry.resolve(cmd, args)
        if not resolved:
            # 传原始文本给 intent_route，不加 / 前缀，以便 L0 regex 能匹配
            combined_input = f"{cmd} {args}".strip() if args else cmd
            await self._progress_to_channel(reply_channel, msg.id, f"🤔 正在分析意图...")
            await self._intent_router.intent_route(combined_input, uid, reply_channel, msg.id, user_name=user_name)
            await self.reply(msg, result={"text": "intent_routed"})
            return

        target_agent, target_action, params, _timeout = resolved

        # ── Policy 检查 ──
        policy = self._get_policy_engine()
        if policy:
            decision = await policy.check(target_agent, target_action, params, user=uid)
            if not decision.allowed:
                block_msg = f"⛔ 策略拦截 [{decision.policy_name}]: {decision.reason}"
                await self._reply_to_channel(reply_channel, msg.id, block_msg)
                await self.reply(msg, error=block_msg)
                return

        # ── Adaptive 超时/熔断 ──
        tuner = self._get_adaptive_tuner()
        if tuner:
            is_open, reason = tuner.is_circuit_open(target_agent)
            if is_open:
                block_msg = f"🔴 熔断保护: {reason}"
                await self._reply_to_channel(reply_channel, msg.id, block_msg)
                await self.reply(msg, error=block_msg)
                return
            timeout = tuner.get_timeout(target_agent)
        else:
            timeout = ACTION_TIMEOUTS.get(target_action, 0)

        await self._progress_to_channel(reply_channel, msg.id,
                                        f"🔄 正在执行 /{cmd}...", source=target_agent)

        t0 = time.time()
        resp = await self._send_with_progress(
            target_agent, target_action, params, timeout=timeout,
            reply_channel=reply_channel, msg_id=msg.id,
        )
        latency = time.time() - t0

        # ── Adaptive 数据采集 ──
        if tuner:
            is_timeout = resp.error and "Timeout" in resp.error
            await tuner.record(
                target_agent, target_action,
                success=not resp.error,
                latency=latency,
                error_type="timeout" if is_timeout else "",
            )

        if resp.error:
            await self._reply_to_channel(reply_channel, msg.id, f"❌ {resp.error}", source=target_agent)
            await self.reply(msg, error=resp.error)
        else:
            result = resp.result
            text = self._result_to_text(result, source=target_agent, action=target_action)
            await self._reply_to_channel(reply_channel, msg.id, text, raw=result, source=target_agent)
            await self.reply(msg, result=result)

    # ── Cross-agent ask ──

    async def _cross_agent_ask(self, question: str) -> str:
        market_resp = await self.send("market", "get_all_raw")
        if market_resp.error:
            return f"获取行情数据失败: {market_resp.error}"
        analysis_resp = await self.send("analysis", "ask", {
            "question": question,
            "market_data": market_resp.result,
        })
        if analysis_resp.error:
            return f"分析失败: {analysis_resp.error}"
        return self._result_to_text(analysis_resp.result, source="analysis", action="ask")

    # ── Task management ──

    async def _handle_task_create(self, msg: AgentMessage):
        from agents.task_manager import PRESET_TASKS
        params = msg.params
        preset = params.get("preset", "")
        name = params.get("name", "")
        steps = params.get("steps", [])
        reply_channel = params.get("reply_channel", "")
        try:
            mgr = await self._get_task_manager()
            if preset and preset in PRESET_TASKS:
                tpl = PRESET_TASKS[preset]
                task = await mgr.create_task(tpl["name"], tpl["steps"], created_by=msg.sender)
            elif name and steps:
                task = await mgr.create_task(name, steps, created_by=msg.sender)
            else:
                presets = ", ".join(PRESET_TASKS.keys())
                text = f"请指定 preset 或自定义 name+steps。\n可用预设: {presets}"
                await self._reply_to_channel(reply_channel, msg.id, text)
                await self.reply(msg, result={"text": text})
                return
            asyncio.create_task(
                mgr.run_task(task.id, send_fn=self.send, notify_fn=self._notify),
                name=f"task_{task.id}",
            )
            text = f"✅ 任务已创建并开始执行\nID: {task.id}\n名称: {task.name}\n步骤: {len(task.steps)}"
            await self._reply_to_channel(reply_channel, msg.id, text)
            await self.reply(msg, result={"text": text, "task_id": task.id})
        except Exception as e:
            err_text = f"❌ 任务创建失败: {e}"
            await self._reply_to_channel(reply_channel, msg.id, err_text)
            await self.reply(msg, error=str(e))

    async def _handle_task_status(self, msg: AgentMessage):
        task_id = msg.params.get("task_id", "")
        reply_channel = msg.params.get("reply_channel", "")
        try:
            mgr = await self._get_task_manager()
            if task_id:
                task = await mgr.get_task(task_id)
                if task:
                    text = task.format_status()
                    await self._reply_to_channel(reply_channel, msg.id, text)
                    await self.reply(msg, result={"text": text})
                else:
                    text = f"任务 {task_id} 不存在"
                    await self._reply_to_channel(reply_channel, msg.id, text)
                    await self.reply(msg, error=text)
            else:
                tasks = await mgr.list_tasks(limit=10)
                if tasks:
                    lines = ["📋 最近任务:"]
                    for t in tasks:
                        icon = {"completed": "✅", "running": "🔄", "failed": "❌",
                                "pending": "⏳", "cancelled": "🚫", "paused": "⏸️"}.get(t.status.value, "❓")
                        lines.append(f"  {icon} {t.id}: {t.name} [{t.progress}%]")
                    text = "\n".join(lines)
                else:
                    text = "暂无任务记录"
                await self._reply_to_channel(reply_channel, msg.id, text)
                await self.reply(msg, result={"text": text})
        except Exception as e:
            err_text = f"❌ 任务查询失败: {e}"
            await self._reply_to_channel(reply_channel, msg.id, err_text)
            await self.reply(msg, error=str(e))

    async def _handle_task_list(self, msg: AgentMessage):
        await self._handle_task_status(msg)

    async def _handle_task_cancel(self, msg: AgentMessage):
        task_id = msg.params.get("task_id", "")
        reply_channel = msg.params.get("reply_channel", "")
        if not task_id:
            text = "请指定 task_id"
            await self._reply_to_channel(reply_channel, msg.id, text)
            await self.reply(msg, error=text)
            return
        try:
            mgr = await self._get_task_manager()
            ok = await mgr.cancel_task(task_id)
            if ok:
                text = f"✅ 任务 {task_id} 已取消"
            else:
                text = f"无法取消任务 {task_id}（可能已完成或不存在）"
            await self._reply_to_channel(reply_channel, msg.id, text)
            await self.reply(msg, result={"text": text})
        except Exception as e:
            err_text = f"❌ 任务取消失败: {e}"
            await self._reply_to_channel(reply_channel, msg.id, err_text)
            await self.reply(msg, error=str(e))

    # ── Memory health ──

    async def _get_memory_health(self) -> dict:
        health = {"graph": {}, "degradation_recent": []}
        try:
            from agents.memory.knowledge_graph import get_shared_graph
            graph = get_shared_graph()
            health["graph"] = graph.health_report()
        except Exception as e:
            health["graph"] = {"error": str(e)}
        try:
            r = await self.get_redis()
            logs = await r.lrange(DEGRADATION_LOG_KEY, 0, 9)
            health["degradation_recent"] = [json.loads(l) for l in logs]
        except Exception:
            pass
        return health

    # ── Daily briefing ──

    async def _generate_daily_briefing(self) -> dict:
        results = {}
        market_task = self.send("market", "get_summary")
        news_task = self.send("news", "get_news", {"keyword": ""})
        market_resp, news_resp = await asyncio.gather(market_task, news_task)
        if not market_resp.error:
            results["market"] = market_resp.result
        if not news_resp.error:
            results["news"] = news_resp.result
        try:
            health = await self._get_memory_health()
            results["memory_health"] = {
                "nodes": health.get("graph", {}).get("total_nodes", 0),
                "orphans": health.get("graph", {}).get("orphan_nodes", 0),
                "degradations": len(health.get("degradation_recent", [])),
            }
        except Exception:
            pass
        try:
            from agents.memory.soul_guardian import SoulGuardian
            guardian = SoulGuardian()
            results["soul_status"] = (await guardian.check_integrity()).get("status", "unknown")
        except Exception:
            results["soul_status"] = "unchecked"
        try:
            from agents.llm_router import get_llm_router
            results["llm_status"] = get_llm_router().get_status()
        except Exception:
            pass
        try:
            from agents.data_sources.source_router import get_router
            results["data_source"] = get_router().get_status()
        except Exception:
            pass
        market_text = results.get("market", {}).get("text", "暂无") if isinstance(results.get("market"), dict) else str(results.get("market", "暂无"))
        news_text = results.get("news", {}).get("text", "暂无") if isinstance(results.get("news"), dict) else str(results.get("news", "暂无"))
        briefing = (
            f"📋 每日综合简报 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 市场: {market_text[:300]}\n"
            f"📰 新闻: {news_text[:300]}\n"
            f"🧠 记忆: {results.get('memory_health', {}).get('nodes', '?')}节点, "
            f"孤立{results.get('memory_health', {}).get('orphans', '?')}\n"
            f"🛡️ 身份: {results.get('soul_status', 'N/A')}\n"
            f"🤖 LLM: local={results.get('llm_status', {}).get('stats', {}).get('local_calls', 0)}, "
            f"cloud={results.get('llm_status', {}).get('stats', {}).get('cloud_calls', 0)}\n"
            f"📡 数据源: {results.get('data_source', {}).get('active_source', 'primary')}"
        )
        await self._notify(briefing, topic="market", priority="high")
        return {"briefing": briefing, "details": results}

    # ── Background loops ──

    async def _schedule_loop(self):
        while self._running:
            now = datetime.now()
            for rule in self.rules:
                trigger = rule.get("trigger", {})
                schedule_str = trigger.get("schedule")
                if not schedule_str:
                    continue
                schedule = parse_schedule(schedule_str)
                if not schedule:
                    continue
                if match_schedule(schedule, now):
                    logger.info(f"Rule triggered: {rule.get('name')}")
                    asyncio.create_task(self._execute_rule(rule))
            await asyncio.sleep(60)

    async def _event_monitor_loop(self):
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                from agents.market_agent import api_get
                zt = await loop.run_in_executor(None, lambda: api_get("limitup", {"page_size": 1}))
                if zt:
                    count = zt.get("count", 0)
                    if self._last_limitup_count is not None:
                        for rule in self.rules:
                            trigger = rule.get("trigger", {})
                            if trigger.get("event") == "limitup_count_change":
                                condition = trigger.get("condition", "")
                                if "count > " in condition:
                                    threshold = int(condition.split(">")[1].strip())
                                    if count > threshold and self._last_limitup_count <= threshold:
                                        logger.info(f"Event rule triggered: {rule.get('name')} (count={count})")
                                        asyncio.create_task(self._execute_rule(rule))
                    self._last_limitup_count = count
            except Exception as e:
                logger.error(f"Event monitor error: {e}")
            await asyncio.sleep(60)

    async def _memory_health_loop(self):
        while self._running:
            try:
                health = await self._get_memory_health()
                graph_health = health.get("graph", {})
                degradations = health.get("degradation_recent", [])
                if degradations:
                    recent_count = sum(1 for d in degradations if time.time() - d.get("ts", 0) < 300)
                    if recent_count >= 3:
                        await self._notify(
                            f"⚠️ 记忆系统降级告警: 5分钟内 {recent_count} 次降级\n"
                            f"图谱: {graph_health.get('total_nodes', 0)} 节点, "
                            f"{graph_health.get('orphan_nodes', 0)} 孤立节点",
                            topic="system", priority="high",
                        )
                orphans = graph_health.get("orphan_nodes", 0)
                total = graph_health.get("total_nodes", 0)
                if total > 10 and orphans / total > 0.2:
                    logger.warning(f"Graph health: {orphans}/{total} orphan nodes ({orphans/total:.0%})")
            except Exception as e:
                logger.debug(f"Memory health check error: {e}")
            await asyncio.sleep(300)

    async def _memory_remind_loop(self):
        from agents.memory.config import REMINDER_CONFIG
        if not REMINDER_CONFIG.get("enabled"):
            return
        interval = REMINDER_CONFIG.get("interval_seconds", 600)
        await asyncio.sleep(30)
        while self._running:
            try:
                from agents.memory.reminder import MemoryReminder
                reminder = MemoryReminder()
                result = await reminder.scan_and_remind()
                if result.get("reminded", 0) > 0:
                    logger.info(f"Memory remind: {result.get('reminded')} reminders pushed")
            except Exception as e:
                logger.debug(f"Memory remind loop error: {e}")
            await asyncio.sleep(interval)

    async def _soul_guardian_loop(self):
        await asyncio.sleep(10)
        try:
            from agents.memory.soul_guardian import SoulGuardian
            loop = asyncio.get_running_loop()
            guardian = SoulGuardian()
            await loop.run_in_executor(None, guardian.compute_baseline)
            await guardian.save_baseline()
            logger.info("Soul Guardian: baseline initialized")
        except Exception as e:
            logger.warning(f"Soul Guardian init failed: {e}")
            return
        while self._running:
            try:
                guardian = SoulGuardian()
                result = await guardian.check_integrity()
                if result.get("status") == "tampered":
                    changes = result.get("changes", [])
                    logger.warning(f"Soul Guardian: {len(changes)} identity changes detected")
            except Exception as e:
                logger.debug(f"Soul Guardian check error: {e}")
            await asyncio.sleep(3600)

    async def _channel_health_loop(self):
        await asyncio.sleep(15)
        was_offline: set[str] = set()
        while self._running:
            try:
                router = await self._get_notify_router()
                await router.refresh_channel_states()
                status = router.get_status()
                for ch_name, ch_info in status["channels"].items():
                    if not ch_info["online"]:
                        if ch_name not in was_offline:
                            was_offline.add(ch_name)
                            age = ch_info.get("age")
                            logger.warning(f"Channel {ch_name} went offline (age={age}s)")
                            other_active = [c for c in status["active"] if c != ch_name]
                            if other_active:
                                await self._notify(
                                    f"⚠️ 渠道 [{ch_name}] 离线，已切换到 {other_active}",
                                    topic="system", priority="high",
                                )
                    else:
                        if ch_name in was_offline:
                            was_offline.discard(ch_name)
                            logger.info(f"Channel {ch_name} recovered")
                            flushed = await router.flush_pending()
                            if flushed:
                                await self._notify(
                                    f"✅ 渠道 [{ch_name}] 恢复，已重发 {flushed} 条积压消息",
                                    topic="system", priority="normal",
                                )
            except Exception as e:
                logger.debug(f"Channel health loop error: {e}")
            await asyncio.sleep(30)

    async def _ack_listener(self):
        await asyncio.sleep(5)
        try:
            r = await self.get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe("openclaw:notify:ack")
            while self._running:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
                if msg and msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"])
                        router = await self._get_notify_router()
                        await router.handle_ack(data)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"ACK listener error: {e}")

    # ── Rule execution ──

    def _rule_topic(self, rule: dict) -> str:
        name = rule.get("name", "")
        topic = rule.get("topic", "")
        if topic:
            return topic
        if any(k in name for k in ("记忆", "SOUL", "嵌入", "卫生")):
            return "system"
        if any(k in name for k in ("策略", "复盘", "研判")):
            return "strategy"
        return "market"

    async def _execute_rule(self, rule: dict):
        actions = rule.get("actions", [])
        results = []
        topic = self._rule_topic(rule)
        for act in actions:
            agent = act.get("agent", "")
            action = act.get("action", "")
            params = act.get("params", {})
            if agent == "orchestrator":
                if action == "notify":
                    text = "\n".join(str(r) for r in results)
                    priority = params.get("priority", "normal")
                    await self._notify(
                        f"📋 {rule.get('name')}:\n{text}",
                        topic=topic, priority=priority,
                    )
            else:
                resp = await self.send(agent, action, params)
                if resp.error:
                    results.append(f"[{agent}] Error: {resp.error}")
                else:
                    results.append(self._result_to_text(resp.result, source=agent, action=action))

    # ── start (fast-path: critical loops first, heavy subsystems deferred) ──

    async def start(self):
        self._running = True
        self.logger.info(f"Orchestrator starting (pid={os.getpid()}, rules={len(self.rules)})")

        # Phase 1: Core message handling — must be responsive immediately
        core_tasks = [
            self._listen(),
            self._heartbeat(),
            self._ack_listener(),
        ]
        # Phase 2: Deferred background — heavier subsystems start after core is live
        async def _deferred_loops():
            """Start non-critical background loops after a brief yield to let core tasks settle."""
            await asyncio.sleep(2)  # let _listen() subscribe first
            self.logger.info("Orchestrator: starting deferred background loops")
            await asyncio.gather(
                self._schedule_loop(),
                self._event_monitor_loop(),
                self._channel_health_loop(),
                self._memory_health_loop(),
                self._memory_remind_loop(),
                self._soul_guardian_loop(),
            )

        await asyncio.gather(*core_tasks, _deferred_loops())


if __name__ == "__main__":
    run_agent(Orchestrator())

"""
SystemCommandHandler — 系统命令处理

从 orchestrator.py 提取的系统诊断/管理命令。
包含: LLM状态、嵌入状态、数据源状态、反思引擎、
技能/Agent展示、因子库、Pipeline/Policy/Adaptive、Hermes委托等。
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger("agents.system_commands")


class SystemCommandHandler:
    """处理系统诊断和管理命令"""

    def __init__(self, orchestrator):
        """
        Args:
            orchestrator: Orchestrator instance, provides send(), get_redis(),
                         _get_policy_engine(), _get_adaptive_tuner(), etc.
        """
        self._orc = orchestrator

    async def handle(self, cmd: str, args: str, progress_channel: str = ""):
        """处理系统命令，返回结果 dict/str 或 None（非系统命令）"""
        handler = self._HANDLERS.get(cmd)
        if handler:
            return await handler(self, cmd, args, progress_channel)
        return None

    # ── 各命令处理方法 ──

    async def _llm_status(self, cmd, args, pc):
        from agents.llm_router import get_llm_router
        return get_llm_router().get_status()

    async def _embed_status(self, cmd, args, pc):
        from agents.memory.embedding import EmbeddingClient
        return EmbeddingClient().get_status()

    async def _data_source_status(self, cmd, args, pc):
        from agents.data_sources.source_router import get_router
        return get_router().get_status()

    async def _soul_check(self, cmd, args, pc):
        from agents.memory.soul_guardian import SoulGuardian
        return await SoulGuardian().check_integrity()

    async def _memory_health(self, cmd, args, pc):
        from agents.session_manager import SessionManager
        sm = SessionManager(self._orc.get_redis)
        return await sm.get_memory_health()

    async def _memory_hygiene(self, cmd, args, pc):
        from agents.memory.lifecycle import memory_hygiene_report
        return await memory_hygiene_report()

    async def _status(self, cmd, args, pc):
        r = await self._orc.get_redis()
        hb = await r.hgetall("openclaw:heartbeats")
        lines = ["📊 Agent 状态:"]
        now = time.time()
        for name, val in sorted(hb.items()):
            try:
                info = json.loads(val)
                age = now - info.get("ts", 0)
                mark = "🟢" if age < 30 else ("🟡" if age < 60 else "🔴")
                lines.append(f"  {mark} {name}: {age:.0f}s ago (pid={info.get('pid', 0)})")
            except Exception:
                lines.append(f"  ❓ {name}")
        return "\n".join(lines)

    async def _quant(self, cmd, args, pc):
        from agents.quant_pipeline import run_quant_pipeline
        try:
            q_params = json.loads(args) if args.strip().startswith("{") else {"topic": args}
        except (json.JSONDecodeError, AttributeError):
            q_params = {"topic": args if args else ""}
        topic = q_params.get("topic", "").strip() or "今日市场热点板块策略"
        bt_mode = q_params.get("mode") or "vectorbt"
        max_rounds = int(q_params.get("max_rounds") or 5)
        base_strat = q_params.get("base_strategy")

        async def _notify_fn(text):
            await self._orc._notify(text, topic="strategy", priority="normal")

        result = await run_quant_pipeline(
            self._orc, topic, notify_fn=_notify_fn,
            base_strategy=base_strat if isinstance(base_strat, dict) else None,
            progress_channel=pc, backtest_mode=bt_mode, max_rounds=max_rounds,
        )
        return {
            "text": result.get("summary", str(result)),
            "metrics": result.get("metrics", {}),
            "status": result.get("status", ""),
            "code": result.get("code", ""),
        }

    async def _reflect(self, cmd, args, pc):
        engine = self._orc._get_reflection_engine()
        if engine:
            return {"text": engine.generate_daily_insight()}
        return {"text": "反思引擎未就绪"}

    async def _reflect_weekly(self, cmd, args, pc):
        engine = self._orc._get_reflection_engine()
        if engine:
            report = engine.generate_weekly_report()
            engine.flush()
            return {"text": report}
        return {"text": "反思引擎未就绪"}

    async def _reflect_stats(self, cmd, args, pc):
        engine = self._orc._get_reflection_engine()
        if engine:
            stats = engine.get_stats_summary()
            hints = engine.get_routing_hints(top_k=5)
            lines = [
                "🔍 反思引擎状态",
                f"  今日查询: {stats['today_queries']}  失败: {stats['today_failures']}",
                f"  跟踪路由: {stats['tracked_routes']}  历史天数: {stats['days_tracked']}",
                f"  平均成功率: {stats['avg_success_rate']:.1%}",
            ]
            if stats["failure_prone_agents"]:
                lines.append(f"  ⚠️ 不稳定: {', '.join(stats['failure_prone_agents'])}")
            if hints:
                lines.append("\n" + hints)
            return {"text": "\n".join(lines)}
        return {"text": "反思引擎未就绪"}

    async def _skills(self, cmd, args, pc):
        return self._build_skills_manifest()

    async def _agents(self, cmd, args, pc):
        return await self._build_agents_info()

    async def _factor_list(self, cmd, args, pc):
        return await self._build_factor_list()

    async def _factor_detail(self, cmd, args, pc):
        return await self._build_factor_detail(args.strip() if args else "")

    async def _quant_optimize(self, cmd, args, pc):
        from agents.quant_pipeline import run_quant_pipeline
        try:
            params = json.loads(args) if args.strip().startswith("{") else {"topic": args}
        except json.JSONDecodeError:
            params = {"topic": args}
        topic = params.get("topic", "策略优化")
        bt_mode = params.get("mode", "vectorbt")
        max_rounds = int(params.get("max_rounds") or 5)
        base_strategy = {"title": params.get("base_title", "")}
        if params.get("base_preset"):
            base_strategy["preset"] = params["base_preset"]
        else:
            base_strategy["code"] = params.get("base_code", "")
            base_strategy["metrics"] = params.get("base_metrics", {})

        async def _notify_opt(text):
            await self._orc._notify(text, topic="strategy", priority="normal")

        result = await run_quant_pipeline(
            self._orc, topic, notify_fn=_notify_opt, base_strategy=base_strategy,
            progress_channel=pc, backtest_mode=bt_mode, max_rounds=max_rounds,
        )
        return {
            "text": result.get("summary", str(result)),
            "metrics": result.get("metrics", {}),
            "status": result.get("status", ""),
            "code": result.get("code", ""),
        }

    async def _digger(self, cmd, args, pc):
        from agents.alpha_digger import run_alpha_digger
        try:
            d_params = json.loads(args) if args and args.strip().startswith("{") else {}
        except (json.JSONDecodeError, AttributeError):
            d_params = {}

        async def _notify_dig(text):
            await self._orc._notify(text, topic="strategy", priority="normal")
            if pc:
                try:
                    r = await self._orc.get_redis()
                    await r.publish(pc, json.dumps({"type": "progress", "text": text}, ensure_ascii=False))
                except Exception:
                    pass

        result = await run_alpha_digger(
            orchestrator=self._orc, notify_fn=_notify_dig,
            max_rounds=d_params.get("rounds", 10),
            factors_per_round=d_params.get("factors", 5),
            round_interval=d_params.get("interval", 60),
        )
        if pc:
            try:
                r = await self._orc.get_redis()
                await r.publish(pc, json.dumps({"type": "done", "text": result.get("summary", "")}, ensure_ascii=False))
            except Exception:
                pass
        return result.get("summary", str(result))

    async def _digger_status(self, cmd, args, pc):
        from agents.alpha_digger import get_digger_status
        stats = await get_digger_status()
        lines = ["📊 因子库状态:"]
        lines.append(f"  活跃因子: {stats.get('active_count', 0)}")
        lines.append(f"  衰减因子: {stats.get('decayed_count', 0)}")
        lines.append(f"  总数: {stats.get('total_count', 0)}")
        if stats.get("best_sharpe"):
            lines.append(f"  最佳 Sharpe: {stats['best_sharpe']:.3f}")
        if stats.get("best_ir"):
            lines.append(f"  最佳 IR: {stats['best_ir']:.3f}")
        if stats.get("ready_to_combine"):
            lines.append("  🔮 已达融合阈值!")
        if stats.get("theme_distribution"):
            lines.append("  主题分布:")
            for theme, cnt in stats["theme_distribution"].items():
                lines.append(f"    {theme}: {cnt}")
        return "\n".join(lines)

    async def _security_audit(self, cmd, args, pc):
        from agents.memory.input_guard import audit_skills, check_url
        import glob
        parts = ["🛡️ 安全审计报告:\n"]
        audit_result = audit_skills()
        if audit_result:
            parts.append("── ClawDefender 审计 ──")
            parts.append(audit_result[:3000])
        else:
            parts.append("⚠️ ClawDefender 未就绪，使用内置检查")
        skill_dirs = glob.glob(os.path.expanduser("~/skills/*/")) + glob.glob(os.path.expanduser("~/openclaw/skills/*/"))
        parts.append(f"\n── 已安装 Skills ({len(skill_dirs)}) ──")
        for sd in sorted(skill_dirs):
            name = os.path.basename(sd.rstrip("/"))
            parts.append(f"  • {name}")
        return {"text": "\n".join(parts)}

    async def _multi_research(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        topic = args or "市场热点"
        results = []
        r1 = await self._orc.send("news", "web_search", {"query": topic}, timeout=60)
        if not r1.error:
            results.append(("news.web_search", result_to_text(r1.result)))
        search_context = results[0][1][:2000] if results else topic
        r2 = await self._orc.send("analysis", "ask", {"question": f"基于以下信息分析: {search_context}", "args": search_context}, timeout=120)
        if not r2.error:
            results.append(("analysis.ask", result_to_text(r2.result)))
        analysis_context = results[-1][1][:2000] if len(results) > 1 else search_context
        r3 = await self._orc.send("strategist", "ask_strategy", {"args": f"基于分析给出策略建议: {analysis_context}"}, timeout=120)
        if not r3.error:
            results.append(("strategist.ask_strategy", result_to_text(r3.result)))
        summary_parts = [f"🔗 多 Agent 协作研究: {topic}\n"]
        for src, text in results:
            summary_parts.append(f"── {src} ──\n{text[:1500]}\n")
        return {"text": "\n".join(summary_parts)}

    async def _combine_exhaustive(self, cmd, args, pc):
        from agents.factor_library import FactorLibrary
        from agents.bridge_client import get_bridge_client
        from itertools import combinations
        from datetime import date, timedelta
        try:
            ce_params = json.loads(args) if args and args.strip().startswith("{") else {}
        except (json.JSONDecodeError, AttributeError):
            ce_params = {}
        group_size = min(max(ce_params.get("group_size", 2), 2), 5)
        max_combos = min(ce_params.get("max_combos", 30), 200)
        r = await self._orc.get_redis()
        lib = FactorLibrary(redis_client=r)
        bridge = get_bridge_client()
        candidates = await lib.get_combine_candidates()
        if len(candidates) < group_size:
            return f"可融合因子仅 {len(candidates)} 个，不足 {group_size}"
        history = await lib.get_combine_records(limit=500)
        tested = set(tuple(sorted(rec.get("input_factor_ids", []))) for rec in history)
        all_combos = list(combinations(range(len(candidates)), group_size))
        combos = [c for c in all_combos if tuple(sorted(candidates[i].id for i in c)) not in tested][:max_combos]
        start_d = (date.today() - timedelta(days=180)).isoformat()
        end_d = date.today().isoformat()
        accepted, tested_n = 0, 0
        for combo in combos:
            factors = [candidates[i] for i in combo]
            codes = [f.code.replace("def generate_factor(", f"def _factor_{j+1}(") for j, f in enumerate(factors)]
            combiner = "\n\nimport numpy as np\nimport pandas as pd\n\ndef generate_factor(matrices):\n    factors = []\n"
            for j in range(len(factors)):
                combiner += f"    try:\n        factors.append(_factor_{j+1}(matrices))\n    except Exception:\n        pass\n"
            combiner += "    if not factors:\n        return pd.DataFrame(0, index=matrices['close'].index, columns=matrices['close'].columns)\n    stacked = np.stack([f.values for f in factors], axis=0)\n    combined = np.nanmean(stacked, axis=0)\n    return pd.DataFrame(combined, index=matrices['close'].index, columns=matrices['close'].columns)\n"
            combined_code = "\n\n".join(codes) + combiner
            try:
                resp = await bridge.run_factor_mining(factor_code=combined_code, start_date=start_d, end_date=end_d)
                metrics = resp.get("metrics") or {} if resp.get("status") != "error" else {}
            except Exception:
                metrics = {}
            input_info = [{"id": f.id, "theme": f.sub_theme or f.theme, "sharpe": f.sharpe, "ir": f.ir, "ic_mean": f.ic_mean} for f in factors]
            evaluation = lib.evaluate_combine_quality(input_info, metrics)
            record = {"input_factors": input_info, "input_factor_ids": [f.id for f in factors], "combined_metrics": metrics, "evaluation": evaluation, "verdict": evaluation["verdict"], "status": "accepted" if evaluation["verdict"] == "accept" else "rejected", "source": "rule_exhaustive"}
            await lib.save_combine_record(record)
            if evaluation["verdict"] == "accept":
                accepted += 1
            tested_n += 1
            await asyncio.sleep(1)
        summary = f"穷举融合完成: 测试 {tested_n}/{len(combos)} 组合, {accepted} 个被采纳 (group_size={group_size})"
        await self._orc._notify(summary, topic="strategy", priority="normal")
        return summary

    async def _intraday_select(self, cmd, args, pc):
        from agents.intraday_pipeline import run_post_market_selection
        from agents.response_formatter import result_to_text
        strategy = args.strip() if args else ""
        async def _notify_sel(text):
            await self._orc._notify(text, topic="strategy", priority="normal")
        result = await run_post_market_selection(self._orc, strategy_name=strategy, notify_fn=_notify_sel, progress_channel=pc)
        return result_to_text(result, source="intraday", action="select")

    async def _intraday_monitor(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        logic = args.strip() if args else ""
        resp = await self._orc.send("intraday", "start_monitor", {"strategy_logic": logic})
        return result_to_text(resp.result, "intraday", "monitor") if resp.result else resp.error or "❌ intraday agent 无响应"

    async def _intraday_status(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        resp = await self._orc.send("intraday", "get_status", {})
        return result_to_text(resp.result, "intraday", "status") if resp.result else resp.error or "❌ intraday agent 无响应"

    async def _intraday_scan(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        logic = args.strip() if args else ""
        resp = await self._orc.send("intraday", "scan", {"strategy_logic": logic})
        return result_to_text(resp.result, "intraday", "scan") if resp.result else resp.error or "❌ intraday agent 无响应"

    async def _intraday_stop(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        resp = await self._orc.send("intraday", "stop_monitor", {})
        return result_to_text(resp.result, "intraday", "stop") if resp.result else resp.error or "❌ intraday agent 无响应"

    async def _intraday_pool(self, cmd, args, pc):
        from agents.response_formatter import result_to_text
        resp = await self._orc.send("intraday", "get_pool", {})
        return result_to_text(resp.result, "intraday", "pool") if resp.result else resp.error or "❌ intraday agent 无响应"

    async def _hermes(self, cmd, args, pc):
        return await self._call_hermes(args)

    async def _pipeline(self, cmd, args, pc):
        return await self._handle_pipeline_cmd(args)

    async def _pipeline_list(self, cmd, args, pc):
        from agents.pipeline.loader import list_pipelines
        pipelines = list_pipelines()
        if not pipelines:
            return "📋 暂无可用 Pipeline"
        lines = ["📋 可用 Pipeline:"]
        for p in pipelines:
            trigger = p.get("trigger", {})
            cron = trigger.get("cron", "")
            manual = "✋" if trigger.get("manual") else ""
            lines.append(f"  • **{p['name']}** — {p['description']} ({p['steps']} 步) {manual} {cron}")
        return "\n".join(lines)

    async def _policy_status(self, cmd, args, pc):
        policy = self._orc._get_policy_engine()
        if not policy:
            return "⛔ 策略引擎未就绪"
        lines = ["🛡️ 策略引擎状态:"]
        for p in policy._policies:
            enabled = "🟢" if p.get("enabled", True) else "⚫"
            target = p.get("target", {})
            lines.append(f"  {enabled} {p.get('name')} [{p.get('type')}] → {target.get('agent', '*')}.{target.get('action', '*')}")
        return "\n".join(lines)

    async def _adaptive_status(self, cmd, args, pc):
        tuner = self._orc._get_adaptive_tuner()
        return tuner.status_report() if tuner else "自适应引擎未就绪"

    # ── Hermes 委托 ──

    async def _call_hermes(self, prompt: str):
        if not prompt:
            return "用法: /hermes <任务描述>\n例: /hermes 搜索最近AI芯片相关新闻并生成摘要"
        import uuid as _uuid
        r = await self._orc.get_redis()
        msg_id = _uuid.uuid4().hex[:12]
        reply_channel = f"openclaw:orchestrator:replies:{msg_id}"
        msg = {
            "id": msg_id, "sender": "orchestrator", "target": "hermes",
            "action": "hermes_task", "params": {"prompt": prompt, "max_iterations": 30},
            "reply_to": "", "reply_channel": reply_channel,
            "timestamp": time.time(), "result": None, "error": "",
        }
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_channel)
        await r.publish("openclaw:hermes", json.dumps(msg, ensure_ascii=False))
        deadline = time.time() + 300
        try:
            async for raw in pubsub.listen():
                if time.time() > deadline:
                    return "⏰ Hermes Agent 响应超时 (300s)"
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                except Exception:
                    continue
                if data.get("id") != msg_id:
                    continue
                result = data.get("result", {})
                if isinstance(result, dict):
                    return result.get("text", str(result))
                return str(result)
        finally:
            await pubsub.unsubscribe(reply_channel)
        return "Hermes Agent 无响应"

    # ── Pipeline 处理 ──

    async def _handle_pipeline_cmd(self, args: str):
        from agents.pipeline import PipelineEngine, load_pipeline
        parts = args.strip().split(maxsplit=1)
        if not parts:
            from agents.pipeline.loader import list_pipelines
            pipelines = list_pipelines()
            names = [p["name"] for p in pipelines]
            return f"用法: /pipeline <name> [params]\n可用: {', '.join(names)}"
        pipeline_name = parts[0]
        extra_args = parts[1] if len(parts) > 1 else ""
        ctx = {}
        if extra_args:
            for pair in extra_args.split():
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    ctx[k] = v
                else:
                    ctx["topic"] = extra_args
                    break
        pipeline_def = load_pipeline(pipeline_name)
        if not pipeline_def:
            return f"❌ Pipeline 未找到: {pipeline_name}"
        tuner = self._orc._get_adaptive_tuner()

        async def _agent_caller(agent, action, params, timeout):
            if tuner:
                timeout = tuner.get_timeout(agent) or timeout
            t0 = time.time()
            resp = await self._orc.send(agent, action, params, timeout=timeout)
            if tuner:
                latency = time.time() - t0
                is_timeout = resp.error and "Timeout" in resp.error
                await tuner.record(agent, action, success=not resp.error,
                                   latency=latency, error_type="timeout" if is_timeout else "")
            return resp

        engine = PipelineEngine(_agent_caller)
        policy = self._orc._get_policy_engine()
        if policy:
            decision = await policy.check("orchestrator", "pipeline", {"pipeline": pipeline_name})
            if not decision.allowed:
                return f"⛔ 策略拦截: {decision.reason}"
        result = await engine.execute(pipeline_def, context=ctx)
        return result.summary()

    # ── 展示方法 ──

    def _build_skills_manifest(self) -> str:
        skills_dir = Path(__file__).parent / "skills"
        if not skills_dir.exists():
            return "未找到 skills 目录"
        sections = []
        for yf in sorted(skills_dir.glob("*_skills.yaml")):
            try:
                data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
                agent = data.get("agent", yf.stem.replace("_skills", ""))
                desc = data.get("description", "")
                skills = data.get("skills", [])
                lines = [f"🤖 **{agent}** — {desc}"]
                for s in skills:
                    if not isinstance(s, dict):
                        continue
                    name = s.get("name", "?")
                    sdesc = s.get("description", "")
                    trigger = s.get("trigger", "")
                    line = f"  ▸ {name}: {sdesc}"
                    if trigger:
                        line += f" [{trigger}]"
                    lines.append(line)
                sections.append("\n".join(lines))
            except Exception:
                continue
        return "\n\n".join(sections) if sections else "暂无 skills 数据"

    async def _build_agents_info(self) -> str:
        r = await self._orc.get_redis()
        hb_raw = await r.hgetall("openclaw:heartbeats")
        skills_dir = Path(__file__).parent / "skills"
        agent_meta = {}
        if skills_dir.exists():
            for yf in sorted(skills_dir.glob("*_skills.yaml")):
                try:
                    data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
                    name = data.get("agent", yf.stem.replace("_skills", ""))
                    agent_meta[name] = {
                        "description": data.get("description", ""),
                        "skills": [s.get("name", "?") for s in data.get("skills", []) if isinstance(s, dict)],
                    }
                except Exception:
                    continue
        lines = ["📋 **Agent 全景**\n"]
        now = time.time()
        for name in sorted(set(list(hb_raw.keys()) + list(agent_meta.keys()))):
            hb = {}
            status = "offline"
            if name in hb_raw:
                try:
                    hb = json.loads(hb_raw[name])
                    age = now - hb.get("ts", 0)
                    status = "online" if age < 30 else ("slow" if age < 60 else "offline")
                except Exception:
                    pass
            icon = "🟢" if status == "online" else ("🟡" if status == "slow" else "⚪")
            meta = agent_meta.get(name, {})
            desc = meta.get("description", "")
            skills = meta.get("skills", hb.get("skills", []))
            lines.append(f"{icon} **{name}** [{status}]")
            if desc:
                lines.append(f"  目标: {desc}")
            if skills:
                lines.append(f"  技能: {', '.join(skills[:10])}")
            lines.append("")
        return "\n".join(lines)

    async def _build_factor_list(self) -> str:
        try:
            from agents.factor_library import get_factor_library
            lib = get_factor_library()
            factors = await lib.get_all_factors(status="")
            stats = await lib.get_stats()
        except Exception as e:
            return f"因子库加载失败: {e}"
        if not factors:
            return "📊 因子库为空，尚无入库因子"
        lines = [
            f"📊 **因子库** (活跃: {stats.get('active_count', 0)} / 衰减: {stats.get('decayed_count', 0)} / 总数: {stats.get('total_count', 0)})",
        ]
        if stats.get("best_sharpe"):
            lines.append(f"  最佳 Sharpe: {stats['best_sharpe']:.3f}  最佳 IR: {stats.get('best_ir', 0):.3f}  平均 Sharpe: {stats.get('avg_sharpe', 0):.3f}")
        if stats.get("ready_to_combine"):
            lines.append("  🔮 已达融合阈值!")
        lines.append("")
        for i, f in enumerate(sorted(factors, key=lambda x: x.sharpe, reverse=True), 1):
            s = "🟢" if f.status == "active" else "🔴"
            lines.append(f"{s} {i}. [{f.id}] {f.theme}/{f.sub_theme} — Sharpe {f.sharpe:.3f}  IR {f.ir:.3f}  WR {f.win_rate:.1%}  DD {f.max_drawdown:.1%}")
        lines.append(f"\n查看详情: /factor_detail <factor_id>")
        return "\n".join(lines)

    async def _build_factor_detail(self, factor_id: str) -> str:
        if not factor_id:
            return "用法: /factor_detail <factor_id>"
        try:
            from agents.factor_library import get_factor_library
            lib = get_factor_library()
            factors = await lib.get_all_factors(status="")
        except Exception as e:
            return f"因子库加载失败: {e}"
        target = None
        for f in factors:
            if f.id == factor_id:
                target = f
                break
        if not target:
            return f"未找到因子: {factor_id}"
        lines = [
            f"📊 **因子详情: {target.id}**",
            f"  状态: {target.status}",
            f"  主题: {target.theme} / {target.sub_theme}",
            f"  Sharpe: {target.sharpe:.4f}",
            f"  Win Rate: {target.win_rate:.2%}",
            f"  IC Mean: {target.ic_mean:.6f}",
            f"  IR: {target.ir:.4f}",
            f"  单调性: {target.monotonicity:.4f}",
            f"  换手率: {target.turnover:.4f}",
            f"  最大回撤: {target.max_drawdown:.2%}",
            f"  交易次数: {target.trades}",
            f"  分位价差: {target.quantile_spread:.4f}",
        ]
        if target.decay_halflife is not None:
            lines.append(f"  衰减半衰期: {target.decay_halflife}")
        from datetime import datetime as _dt
        if target.created_at:
            lines.append(f"  创建时间: {_dt.fromtimestamp(target.created_at).strftime('%Y-%m-%d %H:%M')}")
        if target.last_validated:
            lines.append(f"  最后验证: {_dt.fromtimestamp(target.last_validated).strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"  验证次数: {target.validation_count}")
        if target.code:
            code_preview = target.code[:500]
            if len(target.code) > 500:
                code_preview += "\n... (截断)"
            lines.append(f"\n📝 **因子代码:**\n```python\n{code_preview}\n```")
        return "\n".join(lines)

    # ── 命令路由表 ──
    _HANDLERS = {
        "llm_status": _llm_status,
        "embed_status": _embed_status,
        "data_source_status": _data_source_status,
        "soul_check": _soul_check,
        "memory_health": _memory_health,
        "memory_hygiene": _memory_hygiene,
        "status": _status,
        "quant": _quant,
        "reflect": _reflect,
        "reflect_weekly": _reflect_weekly,
        "reflect_stats": _reflect_stats,
        "skills": _skills,
        "agents": _agents,
        "factor_list": _factor_list,
        "factor_detail": _factor_detail,
        "quant_optimize": _quant_optimize,
        "digger": _digger,
        "digger_status": _digger_status,
        "security_audit": _security_audit,
        "multi_research": _multi_research,
        "combine_exhaustive": _combine_exhaustive,
        "intraday_select": _intraday_select,
        "intraday_monitor": _intraday_monitor,
        "intraday_status": _intraday_status,
        "intraday_scan": _intraday_scan,
        "intraday_stop": _intraday_stop,
        "intraday_pool": _intraday_pool,
        "hermes": _hermes,
        "pipeline": _pipeline,
        "pipeline_list": _pipeline_list,
        "policy_status": _policy_status,
        "adaptive_status": _adaptive_status,
    }

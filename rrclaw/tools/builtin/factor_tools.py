"""
Factor mining tools — direct integration with alpha_digger.

These tools bypass PyAgent Redis and call the factor library/digger
modules directly for maximum performance.
"""
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

# Add openclaw-brain to path for alpha_digger import
BRAIN_PATH = os.getenv("BRAIN_PATH", "/Users/zayl/OpenClaw-Universe/openclaw-brain")
if BRAIN_PATH not in sys.path:
    sys.path.insert(0, BRAIN_PATH)

from rrclaw.tools.base import Tool, ToolSpec, ToolResult

logger = logging.getLogger("rrclaw.tools.factor")


class FactorMineTool(Tool):
    """挖掘新因子 — 调用 alpha_digger.run_alpha_digger()"""

    spec = ToolSpec(
        name="factor_mine",
        description="挖掘新的 Alpha 因子。指定轮数和每轮因子数。直接调用核心挖掘引擎，返回发现的因子及 Sharpe/IC/IR 指标。",
        input_schema={
            "type": "object",
            "properties": {
                "rounds": {"type": "integer", "default": 3, "description": "挖掘轮数"},
                "factors": {"type": "integer", "default": 5, "description": "每轮因子数"},
            },
        },
        is_tier0=True,
        timeout=600,
    )

    async def call(self, input: dict[str, Any]) -> ToolResult:
        rounds = input.get("rounds", 3)
        factors_per_round = input.get("factors", 5)

        try:
            from agents.alpha_digger import run_alpha_digger
            results = []

            async def _on_progress(text):
                results.append({"type": "progress", "text": text})

            final = await run_alpha_digger(
                rounds=rounds,
                factors_per_round=factors_per_round,
                on_progress=_on_progress,
            )
            return ToolResult.success(json.dumps(final, ensure_ascii=False, default=str))
        except ImportError:
            return ToolResult.error("alpha_digger 模块未安装。请确认 BRAIN_PATH 配置正确。")
        except Exception as e:
            return ToolResult.error(f"因子挖掘失败: {e}")


class FactorEvaluateTool(Tool):
    """评估因子质量"""

    spec = ToolSpec(
        name="factor_evaluate",
        description="评估因子代码质量：Sharpe、IC、IR、胜率、最大回撤、PBO 过拟合检测。",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "因子 Python 代码"},
            },
            "required": ["code"],
        },
        is_tier0=True,
        timeout=300,
    )

    def __init__(self, bridge=None):
        self.bridge = bridge

    async def call(self, input: dict[str, Any]) -> ToolResult:
        code = input.get("code", "")
        if not code:
            return ToolResult.error("请提供因子代码")

        if self.bridge and self.bridge.is_connected:
            result = await self.bridge.call_agent(
                "backtest", "quant_validate", {"code": code}, timeout=300
            )
            return ToolResult.success(json.dumps(result, ensure_ascii=False, default=str))

        return ToolResult.error("backtest agent 不可用")


class FactorCombineTool(Tool):
    """智能融合因子"""

    spec = ToolSpec(
        name="factor_combine",
        description="从因子库中选取表现最好的因子进行智能融合（加权/乘法/排名），生成新组合因子。",
        input_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 2, "description": "融合因子数"},
                "mode": {"type": "string", "default": "smart", "description": "模式: smart/add/multiply/rank"},
            },
        },
        is_tier0=True,
        timeout=600,
    )

    async def call(self, input: dict[str, Any]) -> ToolResult:
        count = input.get("count", 2)
        mode = input.get("mode", "smart")

        try:
            import redis.asyncio as aioredis
            from agents.factor_library import FactorLibrary

            r = aioredis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
            lib = FactorLibrary(redis_client=r)
            all_factors = await lib.get_all_factors(status="active")

            if len(all_factors) < count:
                await r.aclose()
                return ToolResult.error(f"因子库中只有 {len(all_factors)} 个活跃因子，不足 {count} 个")

            # 选 top N by sharpe
            sorted_factors = sorted(all_factors, key=lambda f: float(getattr(f, 'sharpe', 0) or 0), reverse=True)
            selected = sorted_factors[:count]

            names = [getattr(f, 'sub_theme', getattr(f, 'theme', '?')) for f in selected]
            sharpes = [getattr(f, 'sharpe', 0) for f in selected]

            result = {
                "mode": mode,
                "selected_factors": [{"name": n, "sharpe": s} for n, s in zip(names, sharpes)],
                "count": count,
                "status": "ready_to_combine",
                "message": f"已选择 {count} 个因子进行 {mode} 融合: {', '.join(names)}",
            }
            await r.aclose()
            return ToolResult.success(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            return ToolResult.error(f"因子融合失败: {e}")


class FactorListTool(Tool):
    """查看因子库"""

    spec = ToolSpec(
        name="factor_list",
        description="列出因子库所有活跃因子，包含 Sharpe、胜率、评级、主题分类。",
        input_schema={
            "type": "object",
            "properties": {
                "top": {"type": "integer", "default": 20, "description": "返回前 N 个"},
                "sort_by": {"type": "string", "default": "sharpe", "description": "排序字段: sharpe/win_rate/ic_mean"},
            },
        },
        is_tier0=True,
        timeout=30,
    )

    async def call(self, input: dict[str, Any]) -> ToolResult:
        top = input.get("top", 20)

        try:
            import redis.asyncio as aioredis
            from agents.factor_library import FactorLibrary

            r = aioredis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
            lib = FactorLibrary(redis_client=r)
            factors = await lib.get_all_factors(status="active")

            sorted_f = sorted(factors, key=lambda f: float(getattr(f, 'sharpe', 0) or 0), reverse=True)

            lines = [f"因子库: {len(factors)} 个活跃因子\n"]
            for i, f in enumerate(sorted_f[:top]):
                lines.append(
                    f"{i+1}. {getattr(f, 'sub_theme', getattr(f, 'theme', '?'))[:30]:30s} "
                    f"Sharpe={getattr(f, 'sharpe', '?'):>6} "
                    f"WinRate={getattr(f, 'win_rate', '?')}"
                )

            await r.aclose()
            return ToolResult.success("\n".join(lines))
        except Exception as e:
            return ToolResult.error(f"查询因子库失败: {e}")

"""
OpenClaw Skill Templates — 预置的 A股分析技能

这些技能让 Hermes 学会使用 OpenClaw 工具组合完成常见任务。
"""

import os
from pathlib import Path

SKILLS_DIR = Path(os.path.expanduser("~/.hermes/skills/openclaw"))

PRESET_SKILLS = [
    {
        "name": "morning-briefing",
        "category": "openclaw",
        "content": '''---
name: Morning Market Briefing
description: Generate a comprehensive morning briefing for A-share market
trigger: "morning briefing|早盘简报|开盘简报|今日行情"
tools: [openclaw_market, openclaw_news, openclaw_analysis]
---

# Morning Market Briefing

## Steps
1. Call `openclaw_market` with action="summary" to get market overview
2. Call `openclaw_market` with action="zt" to get limitup stocks
3. Call `openclaw_news` with action="news" to get latest news
4. Call `openclaw_analysis` with question="综合以上数据，分析今日市场走势和投资机会"

## Notes
- Execute steps 1-3 in parallel for efficiency
- Use step 4 to synthesize all gathered data
- If using PTC, chain all calls in a single script for optimal performance
''',
    },
    {
        "name": "deep-stock-research",
        "category": "openclaw",
        "content": '''---
name: Deep Stock Research
description: Multi-source deep research on a specific stock or topic
trigger: "deep research|深度研究|详细分析|个股研究"
tools: [openclaw_news, openclaw_analysis, openclaw_strategy, openclaw_backtest]
---

# Deep Stock Research

## Steps
1. Call `openclaw_news` with action="web_search" query="<topic>"
2. Call `openclaw_news` with action="deep" query="<topic>"
3. Call `openclaw_analysis` with the research results
4. Call `openclaw_strategy` for strategy recommendations
5. Optionally call `openclaw_backtest` to validate strategy

## Notes
- This is a complex multi-step workflow
- PTC can chain steps 1-3 efficiently
- Always cross-reference news with analysis
''',
    },
    {
        "name": "system-health-check",
        "category": "openclaw",
        "content": '''---
name: System Health Check
description: Comprehensive system health audit
trigger: "system health|系统健康|巡检|patrol"
tools: [openclaw_monitor, openclaw_system]
---

# System Health Check

## Steps
1. Call `openclaw_system` with action="status" for agent health
2. Call `openclaw_monitor` with action="patrol" for monitoring summary
3. Call `openclaw_monitor` with action="alerts" for active alerts
4. Call `openclaw_system` with action="adaptive_status" for tuning status

## Notes
- Run steps 1-3 in parallel
- Alert on any degraded agents or active alerts
''',
    },
]


def install_skills():
    """Install preset OpenClaw skills to Hermes skill directory"""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0
    for skill in PRESET_SKILLS:
        skill_dir = SKILLS_DIR / skill["name"]
        skill_dir.mkdir(exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            skill_file.write_text(skill["content"], encoding="utf-8")
            installed += 1
    return installed


# Auto-install on import
try:
    _count = install_skills()
    if _count > 0:
        import logging
        logging.getLogger(__name__).info(f"Installed {_count} OpenClaw skills to Hermes")
except Exception:
    pass

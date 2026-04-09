"""
AppConfig — 三层配置系统 (参考 claw-code user/project/local 模式)

层级优先级 (高→低):
  1. config.local.yaml  (本地覆盖，不入 git，最高优先)
  2. config.yaml        (项目级，入 git)
  3. 内置默认值          (代码内 hardcode)

环境变量仍优先于 yaml（向后兼容）。
最终优先级: env > config.local.yaml > config.yaml > defaults

使用:
    from agents.app_config import cfg
    budget = cfg("llm.daily_budget_yuan", 50.0)
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agents.app_config")

_BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_PATH = _BASE_DIR / "config.yaml"
_LOCAL_PATH = _BASE_DIR / "config.local.yaml"

_merged: dict = {}
_loaded = False


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_yaml(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Config load failed ({path}): {e}")
    return {}


def _load():
    global _merged, _loaded
    project = _load_yaml(_CONFIG_PATH)
    local = _load_yaml(_LOCAL_PATH)
    _merged = _deep_merge(project, local)
    _loaded = True
    if project or local:
        logger.info(f"Config loaded: project={'yes' if project else 'no'}, local={'yes' if local else 'no'}")


_ENV_MAP = {
    "redis.url": "REDIS_URL",
    "llm.daily_budget_yuan": "LLM_DAILY_BUDGET_YUAN",
    "llm.providers.bailian.base_url": "BAILIAN_BASE_URL",
    "llm.providers.bailian.api_key": "BAILIAN_API_KEY",
    "llm.providers.siliconflow.base_url": "SILICONFLOW_BASE_URL",
    "llm.providers.siliconflow.api_key": "SILICONFLOW_API_KEY",
    "llm.providers.deepseek.base_url": "DEEPSEEK_BASE_URL",
    "llm.providers.deepseek.api_key": "DEEPSEEK_API_KEY",
    "llm.providers.dashscope.base_url": "DASHSCOPE_BASE_URL",
    "llm.providers.dashscope.api_key": "DASHSCOPE_API_KEY",
    "llm.providers.openai.base_url": "OPENAI_BASE_URL",
    "llm.providers.openai.api_key": "OPENAI_API_KEY",
    "webchat.host": "WEBCHAT_HOST",
    "webchat.port": "WEBCHAT_PORT",
    "webchat.jwt_secret": "JWT_SECRET",
}


def cfg(key: str, default: Any = None) -> Any:
    """获取配置值。key 用点分路径如 'llm.daily_budget_yuan'。"""
    if not _loaded:
        _load()

    env_var = _ENV_MAP.get(key)
    if env_var:
        val = os.getenv(env_var)
        if val is not None:
            if isinstance(default, bool):
                return val.lower() in ("true", "1", "yes")
            if isinstance(default, int):
                try:
                    return int(val)
                except ValueError:
                    pass
            if isinstance(default, float):
                try:
                    return float(val)
                except ValueError:
                    pass
            return val

    parts = key.split(".")
    node = _merged
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return default
    return node if node is not None else default


def reload():
    global _loaded
    _loaded = False
    _load()


def get_all() -> dict:
    if not _loaded:
        _load()
    return dict(_merged)

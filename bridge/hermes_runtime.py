"""
Hermes Agent Runtime Wrapper.

Wraps the Hermes AIAgent class to provide a bridge-friendly interface.
Handles agent instantiation, tool filtering, session management,
and result extraction.

Hermes AIAgent capabilities exposed:
  - 47 tools across 20 toolsets (web, terminal, browser, media, ...)
  - PTC (Programmatic Tool Calling) via code execution sandbox
  - Self-improving skill learning loop
  - FTS5 session search across conversation history
  - 40+ LLM provider support with runtime switching
  - Persistent memory with pluggable backends
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bridge.hermes")

# Ensure hermes-agent is importable
HERMES_PATH = os.getenv("HERMES_AGENT_PATH", os.path.expanduser("~/hermes-agent"))
if HERMES_PATH not in sys.path:
    sys.path.insert(0, HERMES_PATH)


class HermesRuntime:
    """
    Bridge-friendly wrapper around Hermes AIAgent.

    Manages a pool of agent instances and dispatches tasks
    from OpenClaw into the Hermes execution loop.
    """

    def __init__(
        self,
        model: str = "",
        provider: str = "",
        base_url: str = "",
        profile: str = "bridge",
        max_workers: int = 4,
        default_toolsets: Optional[list[str]] = None,
    ):
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.profile = profile
        self.default_toolsets = default_toolsets or ["core", "web", "terminal", "browser"]
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hermes")
        self._config: Optional[dict] = None

    async def initialize(self):
        """Load Hermes configuration and verify the runtime."""
        try:
            from hermes_cli.config import load_config
            self._config = load_config(profile=self.profile)

            if not self.model:
                self.model = self._config.get("model", {}).get("default", "")
            if not self.provider:
                self.provider = self._config.get("model", {}).get("provider", "")
            if not self.base_url:
                self.base_url = self._config.get("model", {}).get("base_url", "")

            logger.info(
                f"Hermes runtime initialized: model={self.model}, "
                f"provider={self.provider}, toolsets={self.default_toolsets}"
            )
        except ImportError:
            logger.error(
                f"Cannot import hermes-agent from {HERMES_PATH}. "
                f"Set HERMES_AGENT_PATH or install hermes-agent."
            )
            raise

    # ── Task execution ──

    async def run_task(
        self,
        prompt: str,
        toolsets: Optional[list[str]] = None,
        max_iterations: int = 30,
        session_id: str = "",
        context: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Run a full Hermes agent loop for a given prompt.

        This is the primary bridge entry point for OpenClaw → Hermes
        delegation.  The AIAgent runs in a thread pool to avoid
        blocking the async event loop.

        Returns:
            {
                "text": str,          # Final agent response
                "tool_calls": list,   # Tools invoked during execution
                "iterations": int,    # Number of reasoning iterations
                "model": str,         # Model used
                "session_id": str,    # Hermes session ID
                "skills_used": list,  # Skills loaded during execution
            }
        """
        toolsets = toolsets or self.default_toolsets
        loop = asyncio.get_running_loop()

        try:
            result = await loop.run_in_executor(
                self._executor,
                self._run_agent_sync,
                prompt, toolsets, max_iterations, session_id, context,
            )
            return result
        except Exception as e:
            logger.error(f"Hermes task error: {e}", exc_info=True)
            return {"text": f"Hermes Error: {e}", "error": str(e)}

    def _run_agent_sync(
        self,
        prompt: str,
        toolsets: list[str],
        max_iterations: int,
        session_id: str,
        context: Optional[dict],
    ) -> dict[str, Any]:
        """Synchronous agent execution in thread pool."""
        from run_agent import AIAgent

        agent = AIAgent(
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            max_iterations=max_iterations,
            enabled_toolsets=toolsets,
        )

        # Inject OpenClaw context into the agent's system prompt
        if context:
            agent.extra_context = self._format_context(context)

        # Run the agent's chat loop
        response = agent.chat(prompt)

        return {
            "text": response or "Task completed (no text output)",
            "tool_calls": getattr(agent, "tool_call_history", []),
            "iterations": getattr(agent, "iteration_count", 0),
            "model": self.model,
            "session_id": getattr(agent, "session_id", session_id),
            "skills_used": getattr(agent, "loaded_skills", []),
        }

    # ── Single tool invocation ──

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Call a single Hermes tool without running the full agent loop.

        Useful when OpenClaw knows exactly which tool to invoke
        (e.g., web_search, terminal, image_generate).
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor,
                self._call_tool_sync,
                tool_name, arguments,
            )
            return {"result": result, "tool": tool_name}
        except Exception as e:
            return {"error": str(e), "tool": tool_name}

    def _call_tool_sync(self, tool_name: str, arguments: dict) -> str:
        """Direct tool invocation via Hermes registry."""
        from tools.registry import registry

        handler = registry.get_handler(tool_name)
        if not handler:
            return json.dumps({"error": f"Tool '{tool_name}' not found"})

        if callable(handler):
            return handler(arguments)
        return json.dumps({"error": f"Tool '{tool_name}' handler not callable"})

    # ── Skill search ──

    async def search_skills(
        self, query: str, category: str = "", limit: int = 10
    ) -> list[dict]:
        """Search the Hermes skill registry."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._search_skills_sync, query, category, limit
        )

    def _search_skills_sync(self, query: str, category: str, limit: int) -> list[dict]:
        """Synchronous skill search."""
        skills_dir = Path(os.path.expanduser("~/.hermes/skills"))
        results = []

        if not skills_dir.exists():
            return results

        query_lower = query.lower()
        for skill_md in skills_dir.rglob("SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")
                if query_lower in content.lower():
                    # Extract name from frontmatter
                    name = skill_md.parent.name
                    desc = ""
                    for line in content.split("\n"):
                        if line.strip().startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"')
                            break
                    results.append({
                        "name": name,
                        "description": desc,
                        "path": str(skill_md),
                        "category": skill_md.parent.parent.name,
                    })
                    if len(results) >= limit:
                        break
            except Exception:
                continue

        return results

    # ── Session memory search ──

    async def search_memory(
        self, query: str, session_id: str = "", limit: int = 5
    ) -> list[dict]:
        """Search Hermes session history via FTS5."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._search_memory_sync, query, session_id, limit
        )

    def _search_memory_sync(self, query: str, session_id: str, limit: int) -> list[dict]:
        """FTS5 search over Hermes session database."""
        try:
            from hermes_state import SessionDB
            db = SessionDB()
            results = db.search(query, limit=limit)
            return [
                {
                    "session_id": r.get("session_id", ""),
                    "role": r.get("role", ""),
                    "content": r.get("content", "")[:500],
                    "timestamp": r.get("timestamp", ""),
                }
                for r in results
            ]
        except Exception as e:
            logger.error(f"Memory search error: {e}")
            return []

    # ── Available tools listing ──

    def get_available_tools(self) -> list[dict]:
        """Return list of all available Hermes tools with schemas."""
        try:
            from tools.registry import registry
            return [
                {
                    "name": name,
                    "description": info.get("description", ""),
                    "toolset": info.get("toolset", ""),
                    "schema": info.get("schema", {}),
                }
                for name, info in registry.list_all().items()
                if info.get("available", True)
            ]
        except Exception:
            return []

    # ── Helpers ──

    @staticmethod
    def _format_context(context: dict) -> str:
        """Format OpenClaw context for injection into Hermes prompt."""
        parts = ["[OpenClaw Bridge Context]"]
        if "session_history" in context:
            parts.append(f"Recent conversation:\n{context['session_history']}")
        if "agent_id" in context:
            parts.append(f"Requesting agent: {context['agent_id']}")
        if "user_info" in context:
            parts.append(f"User: {context['user_info']}")
        if "channel" in context:
            parts.append(f"Channel: {context['channel']}")
        return "\n".join(parts)

    async def shutdown(self):
        self._executor.shutdown(wait=False)
